#!/usr/bin/env python3
"""AMP control-plane shim: verify a GitHub release triple, then hand off to the engine.

This file lives at `control/scratch_mmo_deploy_latest.py` on an AMP instance and is
intentionally small, self-contained, and stable. It performs only the work that must
happen before a release can be trusted, and it prefers to do *nothing* at all:

1. if `current/` already ships a checksum-verified AMP engine, execute that engine
   directly (no GitHub download, no ZIP extraction, no new staging directory)
2. otherwise bootstrap-stage an engine from a GitHub release:
   a. select a release (latest or SCRATCH_RELEASE_TAG / --tag)
   b. require *exactly* the three publish assets
   c. prove there is enough free space **before** the first download byte
   d. stream the assets privately into `incoming/`
   e. verify the external manifest/checksums are byte-identical to the ZIP members and
      that every ZIP member matches its recorded SHA-256 (no extraction yet)
   f. extract only the verified control modules into `state/control-engine/<cache_id>/`
   g. execute `deployment/amp/amp_release_updater.py` for the real deployment
      transaction

There is deliberately **no** legacy directory-rename swap path. When a release does
not ship the AMP engine modules, an existing `current/` is preserved and the run
either warns (current present) or fails with a clear error (no current).

Every filesystem write is fail-closed: restrictive umask, no symlink/hardlink/special
files, mandatory chmod + metadata verification (never `except OSError: pass`), fsync,
and atomic replace. Ownership is the current AMP (non-root) identity; nothing is ever
chowned to root. Large downloads are streamed in chunks and never buffered whole in
memory.

The GitHub token is read only from SCRATCH_GITHUB_TOKEN and never appears in argv,
a URL, or a log line. SCRATCH_MMO_INVITE_CODE stays environment-only.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parent.parent.parent

DEFAULT_OWNER = "carthorsestudios"
DEFAULT_REPO = "scratch-mmo"
DEFAULT_CHANNEL = "main"

BUNDLE_DIR_NAME = "mmo_release"
ASSET_ZIP_NAME = "mmo_release.zip"
ASSET_MANIFEST_NAME = "release_manifest.json"
ASSET_CHECKSUMS_NAME = "checksums.sha256"
REQUIRED_ASSET_NAMES = (ASSET_ZIP_NAME, ASSET_MANIFEST_NAME, ASSET_CHECKSUMS_NAME)
REQUIRED_ASSET_NAME_SET = frozenset(REQUIRED_ASSET_NAMES)

ENGINE_REL_PATH = "deployment/amp/amp_release_updater.py"
ENGINE_SUPPORT_RELS = (
    "deployment/amp/amp_transaction.py",
    "deployment/amp/amp_permissions.py",
    "deployment/vps/deployment_state_io.py",
    "deployment/vps/deployment_storage.py",
    "deployment/vps/deployment_permissions.py",
    "tools/release_bundle_lib.py",
)
ENGINE_MODULE_RELS = (ENGINE_REL_PATH,) + ENGINE_SUPPORT_RELS
REQUIRED_BUNDLE_RELS = (
    ASSET_MANIFEST_NAME,
    ASSET_CHECKSUMS_NAME,
    "scripts/amp_start.sh",
    "gateway/mmo_web_gateway",
    "server/mmo_server.x86_64",
    "web/index.html",
)

CURRENT_DIR_NAME = "current"
STATE_DIR_NAME = "state"
INCOMING_DIR_NAME = "incoming"
CONTROL_DIR_NAME = "control"
CONTROL_ENGINE_DIR_NAME = "control-engine"
CONTROL_ENGINE_LEGACY_DIR_NAME = "control-engine-legacy"
LEGACY_STAGING_PREFIX = "engine-staging-"
STAGING_TEMP_PREFIX = ".staging-"
# Active bootstrap engine set plus at most one rollback/control set.
MAX_CONTROL_ENGINE_SETS = 2

TOKEN_ENV_KEY = "SCRATCH_GITHUB_TOKEN"
INVITE_ENV_KEY = "SCRATCH_MMO_INVITE_CODE"
SECRET_ENV_KEYS = (TOKEN_ENV_KEY, INVITE_ENV_KEY, "GITHUB_TOKEN")
BOOTSTRAP_LOG_ENV_KEY = "SCRATCH_BOOTSTRAP_LOG_FILE"

GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "scratch-mmo-amp-control/2.0"

MAX_ZIP_ENTRIES = 10_000
MAX_ZIP_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
READ_CHUNK_BYTES = 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
MAX_ERROR_BODY_BYTES = 2048

MODE_PRIVATE_FILE = 0o600
MODE_PRIVATE_DIR = 0o700
SHIM_UMASK = 0o077

# Free space demanded on top of the download estimate so the control-engine
# extraction (and the engine's own later apply peak) cannot wedge the instance.
CONTROL_STAGE_RESERVE_BYTES = 128 * 1024 * 1024
HEADROOM_FLOOR_BYTES = 256 * 1024 * 1024
HEADROOM_FRACTION = 0.25
DEFAULT_MIN_FREE_BYTES = 1 * 1024 * 1024 * 1024

REQUIRED_MANIFEST_KEYS = (
    "schema_version",
    "release_channel",
    "commit",
    "short_commit",
    "build_number",
    "built_at_utc",
    "source",
)
PLACEHOLDER_MANIFEST_VALUES = frozenset({"local-dev", "local"})

_SECRETS: set[str] = set()


class DeployError(Exception):
    """Fatal control-plane shim error."""


class MissingEngineError(DeployError):
    """The selected release does not ship the AMP deployment engine."""


def register_secret(value: str) -> None:
    text = str(value or "").strip()
    if len(text) >= 8:
        _SECRETS.add(text)


def redact(message: str) -> str:
    text = str(message)
    for secret in _SECRETS:
        if secret and secret in text:
            text = text.replace(secret, "***redacted***")
    return text


def log(message: str) -> None:
    print(redact(message), flush=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Dual console/file logging (replaces the removed `python | tee` bootstrap pipe)
# ---------------------------------------------------------------------------


class TeeStream(io.TextIOBase):
    """Mirror a text stream into an already-opened log file descriptor.

    The process identity is preserved: there is no `tee` child and no shell
    pipeline, so AMP's direct child stays the Python supervisor and signals are
    delivered to it rather than to a pipeline member.
    """

    def __init__(self, primary: Any, log_fd: int) -> None:
        super().__init__()
        self._primary = primary
        self._log_fd = log_fd

    def write(self, text: str) -> int:  # type: ignore[override]
        written = self._primary.write(text)
        try:
            os.write(self._log_fd, text.encode("utf-8", errors="replace"))
        except OSError:
            # Losing the mirror must never break the supervised process; the
            # console stream remains the authoritative sink.
            pass
        return int(written if written is not None else len(text))

    def flush(self) -> None:  # type: ignore[override]
        self._primary.flush()

    def isatty(self) -> bool:  # type: ignore[override]
        return bool(getattr(self._primary, "isatty", lambda: False)())

    def fileno(self) -> int:  # type: ignore[override]
        return int(self._primary.fileno())

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return str(getattr(self._primary, "encoding", "utf-8") or "utf-8")

    def writable(self) -> bool:  # type: ignore[override]
        return True


def open_bootstrap_log_fd(path: Path) -> int:
    """Open the bootstrap log for append with fail-closed type checks."""
    path = Path(path)
    parent = path.parent
    if parent.is_symlink():
        raise DeployError(f"Refusing bootstrap log inside a symlinked directory: {parent}")
    if not parent.is_dir():
        raise DeployError(f"Bootstrap log parent is not a directory: {parent}")
    if path.is_symlink():
        raise DeployError(f"Refusing to append to a symlinked bootstrap log: {path}")
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, MODE_PRIVATE_FILE)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise DeployError(f"Bootstrap log is not a regular file: {path}")
    except Exception:
        os.close(fd)
        raise
    return fd


def install_dual_logging(path: str | Path | None = None) -> bool:
    """Mirror stdout/stderr into the bootstrap log without a shell pipeline."""
    target = str(path or os.environ.get(BOOTSTRAP_LOG_ENV_KEY, "")).strip()
    if not target:
        return False
    if isinstance(sys.stdout, TeeStream) or isinstance(sys.stderr, TeeStream):
        return True
    try:
        fd = open_bootstrap_log_fd(Path(target))
    except (DeployError, OSError) as exc:
        log(f"WARNING: bootstrap log mirroring disabled: {exc}")
        return False
    sys.stdout = TeeStream(sys.stdout, fd)
    sys.stderr = TeeStream(sys.stderr, fd)
    return True


# ---------------------------------------------------------------------------
# Fail-closed filesystem primitives (no root, no chown to root)
# ---------------------------------------------------------------------------

_FS_HOOKS: dict[str, Any] = {}


def install_secure_fs_test_hooks(
    *,
    chmod_fn: Callable[[str, int], None] | None = None,
    lstat_fn: Callable[[str], os.stat_result] | None = None,
    chown_fn: Callable[[str, int, int], None] | None = None,
    identity: tuple[int, int] | None = None,
    enforce_metadata: bool = True,
) -> None:
    """Offline-validator hooks so mode/ownership enforcement is testable anywhere."""
    _FS_HOOKS.clear()
    if chmod_fn is not None:
        _FS_HOOKS["chmod"] = chmod_fn
    if lstat_fn is not None:
        _FS_HOOKS["lstat"] = lstat_fn
    if chown_fn is not None:
        _FS_HOOKS["chown"] = chown_fn
    if identity is not None:
        _FS_HOOKS["identity"] = (int(identity[0]), int(identity[1]))
    _FS_HOOKS["enforce"] = bool(enforce_metadata)


def reset_secure_fs_test_hooks() -> None:
    _FS_HOOKS.clear()


def metadata_enforced() -> bool:
    """True when mode/ownership can be enforced (POSIX, or an injected hook)."""
    if "enforce" in _FS_HOOKS:
        return bool(_FS_HOOKS["enforce"])
    return os.name == "posix"


def shim_identity() -> tuple[int, int] | None:
    """Current AMP (never root) uid/gid, or None when the platform has none."""
    if "identity" in _FS_HOOKS:
        return _FS_HOOKS["identity"]
    if not hasattr(os, "getuid") or not hasattr(os, "getgid"):
        return None
    return int(os.getuid()), int(os.getgid())  # type: ignore[attr-defined]


def establish_shim_umask() -> int:
    """Restrictive umask before any sensitive write. Returns the previous value."""
    if os.name != "posix":
        return 0
    return os.umask(SHIM_UMASK)


def _fs_lstat(path: Path) -> os.stat_result:
    hook = _FS_HOOKS.get("lstat")
    try:
        if hook is not None:
            return hook(str(path))
        return os.lstat(str(path))
    except OSError as exc:
        raise DeployError(f"Cannot stat required path {path}: {exc}") from exc


def _fs_chmod(path: Path, mode: int) -> None:
    """Mandatory chmod: a failure is fatal, never silently ignored."""
    hook = _FS_HOOKS.get("chmod")
    try:
        if hook is not None:
            hook(str(path), mode)
        else:
            os.chmod(str(path), mode)
    except OSError as exc:
        raise DeployError(
            f"Refusing to continue: required chmod {oct(mode)} failed for {path}: {exc}"
        ) from exc


def _fs_chown(path: Path, uid: int, gid: int) -> None:
    hook = _FS_HOOKS.get("chown")
    try:
        if hook is not None:
            hook(str(path), uid, gid)
        elif hasattr(os, "chown"):
            os.chown(str(path), uid, gid)  # type: ignore[attr-defined]
        else:
            raise OSError("os.chown is unavailable on this platform")
    except OSError as exc:
        raise DeployError(
            f"Refusing to continue: required ownership repair failed for {path}: {exc}"
        ) from exc


def verify_path_metadata(
    path: Path,
    *,
    mode: int | None = None,
    require_regular: bool = False,
    require_dir: bool = False,
    allow_symlink: bool = False,
    require_single_link: bool = False,
) -> os.stat_result:
    """Fail-closed type/ownership/mode verification. Never follows symlinks."""
    path = Path(path)
    info = _fs_lstat(path)
    file_type = stat.S_IFMT(info.st_mode)
    if stat.S_ISLNK(info.st_mode) and not allow_symlink:
        raise DeployError(f"Refusing symlink at security boundary: {path}")
    if require_regular and not stat.S_ISREG(info.st_mode):
        raise DeployError(f"Expected a regular file at security boundary: {path}")
    if require_dir and not stat.S_ISDIR(info.st_mode):
        raise DeployError(f"Expected a directory at security boundary: {path}")
    if file_type not in (stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK):
        raise DeployError(f"Refusing special filesystem entry: {path} type={oct(file_type)}")
    if require_single_link and stat.S_ISREG(info.st_mode) and int(info.st_nlink) > 1:
        raise DeployError(
            f"Refusing hard-linked file at security boundary: {path} links={info.st_nlink}"
        )
    if not metadata_enforced():
        return info
    identity = shim_identity()
    if identity is not None:
        uid, gid = identity
        if int(info.st_uid) != uid:
            raise DeployError(
                f"Ownership verification failed for {path}: uid {info.st_uid} != {uid}"
            )
        if int(info.st_gid) != gid:
            _fs_chown(path, -1, gid)
            info = _fs_lstat(path)
            if int(info.st_gid) != gid:
                raise DeployError(
                    f"Ownership verification failed for {path}: gid {info.st_gid} != {gid}"
                )
    if mode is not None and (info.st_mode & 0o7777) != (mode & 0o7777):
        raise DeployError(
            f"Mode verification failed for {path}: got {oct(info.st_mode & 0o7777)}, "
            f"expected {oct(mode & 0o7777)}"
        )
    return info


def apply_and_verify_mode(
    path: Path,
    mode: int,
    *,
    require_regular: bool = False,
    require_dir: bool = False,
) -> None:
    """Apply a required mode and prove it stuck (or fail closed)."""
    verify_path_metadata(path, require_regular=require_regular, require_dir=require_dir)
    _fs_chmod(path, mode)
    verify_path_metadata(
        path, mode=mode, require_regular=require_regular, require_dir=require_dir
    )


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def ensure_private_dir(path: Path) -> Path:
    """Create (or adopt) an owner-private directory with verified metadata."""
    path = Path(path)
    if path.is_symlink():
        raise DeployError(f"Refusing symlinked directory: {path}")
    parent = path.parent
    if parent != path and parent.exists() and parent.is_symlink():
        raise DeployError(f"Refusing directory under a symlinked parent: {parent}")
    path.mkdir(parents=True, exist_ok=True)
    apply_and_verify_mode(path, MODE_PRIVATE_DIR, require_dir=True)
    return path


PRIVATE_CREATE_FLAGS = (
    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
)


def open_private_file(path: Path, flags: int = PRIVATE_CREATE_FLAGS) -> int:
    """Create a new private file without relying on umask alone."""
    return os.open(str(path), flags, MODE_PRIVATE_FILE)


def _discard_temp(path: Path) -> None:
    try:
        info = os.lstat(str(path))
    except OSError:
        return
    if stat.S_ISDIR(info.st_mode):
        raise DeployError(f"Refusing to replace a directory with a temp file: {path}")
    try:
        os.unlink(str(path))
    except OSError as exc:
        raise DeployError(f"Cannot clear stale temp file {path}: {exc}") from exc


def stream_to_private_file(dest: Path, reader: Any, *, digest: Any | None = None) -> int:
    """Stream `reader` into `dest` through a private `.partial` temp, chunk by chunk.

    Nothing is buffered whole in memory; the temp is fsynced, mode-verified, and
    atomically moved into place.
    """
    dest = Path(dest)
    ensure_private_dir(dest.parent)
    if dest.is_symlink():
        raise DeployError(f"Refusing to write through symlink: {dest}")
    temp = dest.with_name(f".{dest.name}.partial")
    _discard_temp(temp)
    total = 0
    fd = open_private_file(temp)
    try:
        while True:
            chunk = reader.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            os.write(fd, chunk)
            if digest is not None:
                digest.update(chunk)
            total += len(chunk)
        os.fsync(fd)
    except OSError as exc:
        os.close(fd)
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise DeployError(f"Streaming write failed for {dest}: {exc}") from exc
    else:
        os.close(fd)
    try:
        apply_and_verify_mode(temp, MODE_PRIVATE_FILE, require_regular=True)
        os.replace(str(temp), str(dest))
    except (DeployError, OSError):
        # A refused write must not leave a stale `.partial` behind.
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise
    verify_path_metadata(
        dest, mode=MODE_PRIVATE_FILE, require_regular=True, require_single_link=True
    )
    _sync_directory(dest.parent)
    return total


def write_private_bytes(path: Path, data: bytes) -> None:
    """Small-payload convenience wrapper over the streaming writer."""
    stream_to_private_file(Path(path), io.BytesIO(data))


def copy_private_file(source: Path, dest: Path) -> int:
    """Chunked private copy; the source is never read entirely into memory."""
    source = Path(source)
    verify_path_metadata(source, require_regular=True)
    with open(source, "rb") as handle:
        return stream_to_private_file(dest, handle)


# ---------------------------------------------------------------------------
# Configuration (deploy.env is secured before it is read)
# ---------------------------------------------------------------------------


def _mode_is_private(mode_bits: int) -> bool:
    return (mode_bits & 0o077) == 0


def ensure_deploy_env_secure_before_read(path: Path) -> bool:
    """Prove a secret-bearing config file is safe, repairing only safe metadata.

    Returns True when the file exists and may be read. Raises DeployError for any
    unsafe type, parent, ownership, or unrepairable permissiveness. Config values
    are never logged.
    """
    path = Path(path)
    parent = path.parent
    if parent.is_symlink():
        raise DeployError(
            f"Refusing to read {path.name}: its parent directory is a symlink ({parent})."
        )
    if parent.exists() and not parent.is_dir():
        raise DeployError(f"Refusing to read {path.name}: parent is not a directory ({parent}).")
    try:
        info = os.lstat(str(path))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DeployError(f"Cannot inspect {path}: {exc}") from exc

    if stat.S_ISLNK(info.st_mode):
        raise DeployError(f"Refusing to read config through a symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise DeployError(f"Refusing config that is not a regular file: {path}")
    if int(info.st_nlink) > 1:
        raise DeployError(
            f"Refusing hard-linked config file (links={info.st_nlink}): {path}"
        )

    if metadata_enforced():
        identity = shim_identity()
        if identity is not None and int(info.st_uid) != identity[0]:
            raise DeployError(
                f"Refusing config not owned by the instance account: {path} "
                f"(uid {info.st_uid} != {identity[0]})"
            )
        if not _mode_is_private(info.st_mode & 0o7777):
            # Only a plain, correctly-owned regular file is safely repairable.
            _fs_chmod(path, MODE_PRIVATE_FILE)
            info = _fs_lstat(path)
            if not _mode_is_private(info.st_mode & 0o7777):
                raise DeployError(
                    f"Refusing overly permissive config that could not be repaired: {path}"
                )
            log(f"Repaired config permissions to {oct(MODE_PRIVATE_FILE)}: {path}")
    return True


def _open_config_no_follow(path: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW  # type: ignore[attr-defined]
    fd = os.open(str(path), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise DeployError(f"Refusing config that is not a regular file: {path}")
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as handle:
            return handle.read()
    finally:
        os.close(fd)


def load_deploy_env_file(path: Path) -> None:
    """Load KEY=value overrides after the file is proven safe. Values are never logged."""
    path = Path(path)
    if not ensure_deploy_env_secure_before_read(path):
        return
    log(f"Loading config: {path}")
    for raw_line in _open_config_no_follow(path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or key in os.environ:
            continue
        os.environ[key] = value


def resolve_deploy_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_root = os.environ.get("SCRATCH_DEPLOY_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    control_dir = SCRIPT_PATH.parent
    if control_dir.name == CONTROL_DIR_NAME:
        return control_dir.parent.resolve()
    return Path.cwd().resolve()


def assert_no_secrets_in_argv(argv: list[str]) -> None:
    values = {
        os.environ.get(key, "").strip()
        for key in SECRET_ENV_KEYS
        if len(os.environ.get(key, "").strip()) >= 8
    }
    for arg in argv:
        if str(arg) in values:
            raise DeployError(
                "Refusing to run: a secret environment value was passed on the command line."
            )


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


def _github_headers(token: str, accept: str) -> dict[str, str]:
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_request(
    url: str,
    token: str,
    *,
    accept: str = "application/vnd.github+json",
    timeout: float = 60.0,
) -> tuple[int, dict[str, str], bytes]:
    """Small-body JSON request. Asset payloads use github_stream_asset instead."""
    request = urllib.request.Request(url, headers=_github_headers(token, accept), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return (
                int(response.status),
                {k.lower(): v for k, v in response.headers.items()},
                body,
            )
    except urllib.error.HTTPError as exc:
        body = exc.read()
        headers_out = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return int(exc.code), headers_out, body
    except urllib.error.URLError as exc:
        raise DeployError(redact(f"GitHub unreachable: {exc.reason}")) from exc


def github_stream_asset(url: str, token: str, dest: Path, *, timeout: float = 300.0) -> int:
    """Stream a release asset straight to a private file, never into RAM."""
    request = urllib.request.Request(
        url, headers=_github_headers(token, "application/octet-stream"), method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            if status != 200:
                detail = response.read(MAX_ERROR_BODY_BYTES).decode("utf-8", errors="replace")
                raise DeployError(f"Asset download failed ({status}): {redact(detail)}")
            return stream_to_private_file(dest, response)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(MAX_ERROR_BODY_BYTES).decode("utf-8", errors="replace")
        except OSError:
            pass
        raise DeployError(
            f"Asset download failed ({int(exc.code)}): {redact(detail)}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DeployError(redact(f"GitHub unreachable: {exc.reason}")) from exc


def fetch_release(config: dict[str, Any]) -> dict[str, Any]:
    fetch_fn = config.get("github_fetch_fn")
    if fetch_fn is not None:
        release = fetch_fn(config)
        if not isinstance(release, dict):
            raise DeployError("Injected GitHub fetch returned a non-object release.")
        return _validate_release_payload(release)

    repo_slug = str(config["repo_slug"])
    token = str(config.get("token", ""))
    tag = str(config.get("tag", "")).strip()
    if tag:
        url = f"https://api.github.com/repos/{repo_slug}/releases/tags/{tag}"
        log(f"Querying GitHub release tag: {repo_slug}@{tag}")
    else:
        url = f"https://api.github.com/repos/{repo_slug}/releases/latest"
        log(f"Querying GitHub latest release: {repo_slug}")

    status, _headers, body = github_request(url, token)
    if status in (401, 403):
        raise DeployError(
            f"GitHub auth failed ({status}). Set the AMP GitHub Release Token field "
            f"or {TOKEN_ENV_KEY} in control/deploy.env."
        )
    if status == 404:
        raise DeployError(f"Release not found or repo inaccessible: {repo_slug} ({status}).")
    if status != 200:
        raise DeployError(
            f"GitHub API error {status}: {redact(body.decode('utf-8', errors='replace')[:300])}"
        )
    try:
        release = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeployError(f"GitHub returned invalid JSON: {exc}") from exc
    return _validate_release_payload(release)


def _validate_release_payload(release: object) -> dict[str, Any]:
    if not isinstance(release, dict):
        raise DeployError("GitHub API returned non-object JSON.")
    if release.get("draft"):
        raise DeployError("Release is a draft; refusing to deploy.")
    if release.get("prerelease"):
        raise DeployError("Release is a prerelease; refusing to deploy.")
    return release


def select_required_assets(release: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Require the asset name set to be *exactly* the publish triple.

    Duplicates, missing assets, and unexpected extras are all fatal: a release
    that does not match the published contract is not deployable.
    """
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        raise DeployError("Release assets missing or invalid.")
    found: dict[str, dict[str, Any]] = {}
    extras: list[str] = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise DeployError("Release asset entry is not an object.")
        name = str(asset.get("name", "")).strip()
        if not name:
            raise DeployError("Release publishes an asset with an empty name; refusing.")
        if name not in REQUIRED_ASSET_NAME_SET:
            extras.append(name)
            continue
        if name in found:
            raise DeployError(f"Release publishes duplicate asset {name!r}; refusing.")
        found[name] = asset
    missing = [name for name in REQUIRED_ASSET_NAMES if name not in found]
    if missing:
        raise DeployError("Release is missing required assets: " + ", ".join(missing))
    if extras:
        raise DeployError(
            "Release publishes unexpected assets and does not match the required "
            "three-asset set: " + ", ".join(sorted(extras))
        )
    if set(found) != REQUIRED_ASSET_NAME_SET:
        raise DeployError("Release asset set does not match the required three-asset set.")
    return found


def download_asset(
    config: dict[str, Any],
    asset: dict[str, Any],
    dest: Path,
) -> None:
    name = str(asset.get("name", "?"))
    download_fn = config.get("download_fn")
    log(f"Downloading asset {name} -> {Path(dest).name}")
    if download_fn is not None:
        payload = download_fn(config, asset)
        if not isinstance(payload, (bytes, bytearray)):
            raise DeployError(f"Injected download for {name} returned non-bytes payload.")
        written = stream_to_private_file(dest, io.BytesIO(bytes(payload)))
    else:
        asset_id = asset.get("id")
        if asset_id is None:
            raise DeployError(f"Release asset {name} is missing an id.")
        url = (
            f"https://api.github.com/repos/{config['repo_slug']}"
            f"/releases/assets/{asset_id}"
        )
        written = github_stream_asset(url, str(config.get("token", "")), dest)
    log(f"Download complete: {Path(dest).name} ({written} bytes)")


# ---------------------------------------------------------------------------
# Capacity: proven before the first download byte
# ---------------------------------------------------------------------------


def _storage_helpers() -> Any:
    """Reuse the real VPS/AMP storage estimator when this is a repo checkout.

    On an AMP instance `control/` is standalone, so the shim falls back to the
    identical arithmetic below rather than importing unverified release code.
    """
    vps_dir = REPO_ROOT / "deployment" / "vps"
    tools_dir = REPO_ROOT / "tools"
    if not (vps_dir / "deployment_storage.py").is_file():
        return None
    for candidate in (vps_dir, tools_dir):
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    try:
        import deployment_storage  # noqa: PLC0415
    except ImportError:
        return None
    return deployment_storage


def _min_free_bytes() -> int:
    raw = os.environ.get("SCRATCH_MMO_MIN_FREE_BYTES", "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_MIN_FREE_BYTES


def _free_bytes(deploy_root: Path, free_bytes_fn: Callable[[Path], int] | None) -> int:
    if free_bytes_fn is not None:
        return int(free_bytes_fn(deploy_root))
    probe = deploy_root if deploy_root.exists() else deploy_root.parent
    return int(shutil.disk_usage(str(probe)).free)


def release_asset_sizes(release: dict[str, Any]) -> dict[str, int]:
    helpers = _storage_helpers()
    if helpers is not None:
        try:
            return dict(helpers.github_asset_sizes(release, list(REQUIRED_ASSET_NAMES)))
        except Exception as exc:  # noqa: BLE001 - helper raises its own error type
            raise DeployError(f"Release asset size metadata is unusable: {exc}") from exc
    sizes: dict[str, int] = {}
    for asset in release.get("assets", []) or []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", "")).strip()
        if name not in REQUIRED_ASSET_NAME_SET:
            continue
        raw = asset.get("size", None)
        text = str(raw).strip()
        if not text.isdigit():
            raise DeployError(f"Release asset {name!r} has no usable size metadata.")
        sizes[name] = int(text)
    missing = [name for name in REQUIRED_ASSET_NAMES if name not in sizes]
    if missing:
        raise DeployError(
            "Release is missing size metadata for assets: " + ", ".join(missing)
        )
    return sizes


def require_download_capacity(config: dict[str, Any], release: dict[str, Any]) -> None:
    """Fail closed *before* any download call or extraction when space is short."""
    deploy_root = Path(config["deploy_root"])
    sizes = release_asset_sizes(release)
    free_bytes_fn = config.get("free_bytes_fn")
    helpers = _storage_helpers()
    if helpers is not None:
        policy = helpers.StoragePolicy(min_free_bytes=_min_free_bytes())
        try:
            estimate = helpers.estimate_poll_download_capacity(
                deploy_root, sizes, policy, free_bytes_fn=free_bytes_fn
            )
        except Exception as exc:  # noqa: BLE001 - helper raises its own error type
            raise DeployError(f"Download capacity estimate failed: {exc}") from exc
        available = int(estimate.available_bytes)
        required = int(estimate.required_bytes) + CONTROL_STAGE_RESERVE_BYTES
        details = list(estimate.details)
    else:
        combined = sum(int(value) for value in sizes.values())
        peak = combined * 2
        headroom = max(HEADROOM_FLOOR_BYTES, int(peak * HEADROOM_FRACTION))
        reserve = _min_free_bytes()
        available = _free_bytes(deploy_root, free_bytes_fn)
        required = peak + headroom + reserve + CONTROL_STAGE_RESERVE_BYTES
        details = [
            f"available={available}",
            f"asset_bytes={combined}",
            f"peak_with_duplicates={peak}",
            f"headroom={headroom}",
            f"reserve={reserve}",
        ]
    details.append(f"control_engine_reserve={CONTROL_STAGE_RESERVE_BYTES}")
    details.append(f"required_with_control_reserve={required}")
    for line in details:
        log(f"download capacity: {line}")
    if available < required:
        raise DeployError(
            "Insufficient free space to download and stage the release control engine. "
            f"required={required} available={available} shortage={required - available}"
        )


# ---------------------------------------------------------------------------
# Minimal trusted verification (no dependency on the release's tools/)
# ---------------------------------------------------------------------------


def _reject_member_name(name: str) -> str | None:
    if not name:
        return "empty ZIP member name"
    if "\\" in name:
        return f"ZIP member uses backslashes: {name!r}"
    if name.startswith("/") or name.startswith("//"):
        return f"absolute ZIP member path: {name!r}"
    if len(name) >= 2 and name[1] == ":":
        return f"drive-qualified ZIP member path: {name!r}"
    if "\x00" in name:
        return f"ZIP member name contains NUL: {name!r}"
    parts = [part for part in name.split("/") if part not in ("",)]
    if any(part == ".." for part in parts):
        return f"ZIP member contains '..' traversal: {name!r}"
    if any(part == "." for part in parts):
        return f"ZIP member contains '.' component: {name!r}"
    return None


def inspect_zip_members(zip_path: Path) -> list[zipfile.ZipInfo]:
    """Structural safety pass over every member before any extraction."""
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise DeployError(f"Invalid release ZIP: {exc}") from exc

    members: list[zipfile.ZipInfo] = []
    seen: set[str] = set()
    total = 0
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise DeployError(f"Release ZIP has too many entries ({len(infos)}).")
        for info in infos:
            reason = _reject_member_name(info.filename)
            if reason:
                raise DeployError(reason)
            if info.flag_bits & 0x1:
                raise DeployError(f"Encrypted ZIP member rejected: {info.filename!r}")
            normalized = info.filename.rstrip("/")
            if normalized in seen:
                raise DeployError(f"Duplicate ZIP member path: {normalized!r}")
            seen.add(normalized)
            top = normalized.split("/", 1)[0]
            if top != BUNDLE_DIR_NAME:
                raise DeployError(
                    f"ZIP member outside canonical bundle root {BUNDLE_DIR_NAME}/: "
                    f"{info.filename!r}"
                )
            mode = (info.external_attr >> 16) & 0xFFFF
            is_dir = bool(info.is_dir() or info.filename.endswith("/"))
            if mode and not is_dir:
                file_type = stat.S_IFMT(mode)
                if file_type == stat.S_IFLNK:
                    raise DeployError(f"Symlink ZIP member rejected: {info.filename!r}")
                if file_type and file_type != stat.S_IFREG:
                    raise DeployError(
                        f"Special ZIP member type rejected: {info.filename!r} mode={oct(mode)}"
                    )
            if is_dir:
                members.append(info)
                continue
            if info.file_size < 0 or info.compress_size < 0:
                raise DeployError(f"ZIP member has negative size: {info.filename!r}")
            if info.file_size > MAX_ZIP_MEMBER_BYTES:
                raise DeployError(f"ZIP member too large: {info.filename!r}")
            total += info.file_size
            if total > MAX_ZIP_TOTAL_BYTES:
                raise DeployError("Release ZIP uncompressed size exceeds the safety limit.")
            if info.compress_size > 0 and info.file_size > 1024 * 1024:
                ratio = info.file_size / info.compress_size
                if ratio > MAX_COMPRESSION_RATIO:
                    raise DeployError(
                        f"ZIP member compression ratio {ratio:.1f} exceeds bomb threshold: "
                        f"{info.filename!r}"
                    )
            members.append(info)
    if not members:
        raise DeployError("Release ZIP contains no members.")
    return members


def parse_checksums(content: str) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line_no, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise DeployError(f"Invalid checksum line {line_no}.")
        digest = parts[0].strip().lower()
        rel = parts[1].strip().replace("\\", "/")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise DeployError(f"Invalid SHA-256 on checksum line {line_no}.")
        if rel in checksums:
            raise DeployError(f"Duplicate checksum entry: {rel}")
        checksums[rel] = digest
    return checksums


def sanitize_manifest(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise DeployError("Release manifest must be a JSON object.")
    missing = [key for key in REQUIRED_MANIFEST_KEYS if key not in raw]
    if missing:
        raise DeployError("Release manifest missing keys: " + ", ".join(missing))
    if raw.get("schema_version") != 1:
        raise DeployError("Release manifest schema_version must be 1.")
    manifest = {
        key: str(raw.get(key, "")).strip()
        for key in REQUIRED_MANIFEST_KEYS
        if key != "schema_version"
    }
    for key, value in manifest.items():
        if not value:
            raise DeployError(f"Release manifest key {key!r} must not be empty.")
        if value in PLACEHOLDER_MANIFEST_VALUES:
            raise DeployError(
                f"Release manifest key {key!r} uses a placeholder value; refusing to deploy."
            )
    commit = manifest["commit"].lower()
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise DeployError("Release manifest commit must be a 40-character hex SHA.")
    short_commit = manifest["short_commit"].lower()
    if len(short_commit) != 7 or short_commit != commit[:7]:
        raise DeployError("Release manifest short_commit must be commit[:7].")
    build_number = manifest["build_number"]
    if not build_number.isdigit() or int(build_number) < 1:
        raise DeployError("Release manifest build_number must be a positive integer.")
    if manifest["source"] != "github-actions":
        raise DeployError("Release manifest source must be 'github-actions'.")
    manifest["commit"] = commit
    manifest["short_commit"] = short_commit
    return manifest


def require_safe_path_component(value: str, *, label: str) -> str:
    text = str(value or "").strip()
    if not text or text in (".", ".."):
        raise DeployError(f"Unsafe {label}: {value!r}")
    for char in text:
        if not (char.isalnum() or char in "-_."):
            raise DeployError(f"Unsafe {label}: {value!r}")
    return text


def release_id_from_manifest(manifest: dict[str, str]) -> str:
    release_id = (
        f"{manifest['release_channel']}-{manifest['short_commit']}-run{manifest['build_number']}"
    )
    return require_safe_path_component(release_id, label="release id derived from manifest")


def parse_release_tag(tag: str) -> dict[str, str]:
    text = str(tag or "").strip()
    if "-run" not in text or "-a" not in text.rsplit("-run", 1)[-1]:
        raise DeployError(
            f"Release tag {tag!r} must look like '<channel>-<7hex>-run<build>-a<attempt>'."
        )
    head, remainder = text.rsplit("-run", 1)
    build_number, attempt = remainder.rsplit("-a", 1)
    channel, short_commit = head.rsplit("-", 1)
    if not build_number.isdigit() or int(build_number) < 1:
        raise DeployError(f"Release tag {tag!r} has an invalid build number.")
    if not attempt.isdigit() or int(attempt) < 1:
        raise DeployError(f"Release tag {tag!r} has an invalid attempt number.")
    if len(short_commit) != 7 or any(c not in "0123456789abcdef" for c in short_commit.lower()):
        raise DeployError(f"Release tag {tag!r} has an invalid short commit.")
    if not channel:
        raise DeployError(f"Release tag {tag!r} has an empty channel.")
    cache_id = f"{channel}-{short_commit.lower()}-run{build_number}"
    require_safe_path_component(cache_id, label="release cache id derived from tag")
    return {
        "channel": channel,
        "short_commit": short_commit.lower(),
        "build_number": build_number,
        "attempt": attempt,
        "cache_id": cache_id,
    }


def _member_digest(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    digest = hashlib.sha256()
    with archive.open(info, "r") as source:
        while True:
            chunk = source.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_release_triple(
    zip_path: Path,
    manifest_bytes: bytes,
    checksums_bytes: bytes,
    *,
    expected_channel: str,
    tag: str = "",
) -> dict[str, str]:
    """Trusted verification with no dependency on the release's own tools/.

    Requires the external companion assets to be byte-identical to the ZIP members
    and every ZIP file member to match its recorded SHA-256, all before extraction.
    """
    members = inspect_zip_members(zip_path)
    manifest_member = f"{BUNDLE_DIR_NAME}/{ASSET_MANIFEST_NAME}"
    checksums_member = f"{BUNDLE_DIR_NAME}/{ASSET_CHECKSUMS_NAME}"

    with zipfile.ZipFile(zip_path) as archive:
        names = {info.filename: info for info in members if not info.is_dir()}
        if manifest_member not in names:
            raise DeployError(f"Release ZIP missing {manifest_member}.")
        if checksums_member not in names:
            raise DeployError(f"Release ZIP missing {checksums_member}.")
        zip_manifest_bytes = archive.read(names[manifest_member])
        zip_checksums_bytes = archive.read(names[checksums_member])
        if manifest_bytes != zip_manifest_bytes:
            raise DeployError(
                "External release_manifest.json is not byte-identical to the ZIP member."
            )
        if checksums_bytes != zip_checksums_bytes:
            raise DeployError(
                "External checksums.sha256 is not byte-identical to the ZIP member."
            )

        try:
            parsed = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeployError(f"Release manifest is invalid JSON: {exc}") from exc
        manifest = sanitize_manifest(parsed)
        if manifest["release_channel"] != expected_channel:
            raise DeployError(
                f"Release channel mismatch: expected {expected_channel!r}, "
                f"got {manifest['release_channel']!r}."
            )
        if tag:
            parsed_tag = parse_release_tag(tag)
            if (
                parsed_tag["channel"] != manifest["release_channel"]
                or parsed_tag["short_commit"] != manifest["short_commit"]
                or parsed_tag["build_number"] != manifest["build_number"]
            ):
                raise DeployError(
                    f"Release tag {tag!r} does not match manifest identity "
                    f"{release_id_from_manifest(manifest)}."
                )

        checksum_map = parse_checksums(checksums_bytes.decode("utf-8"))
        if not checksum_map:
            raise DeployError("checksums.sha256 contains no entries.")

        present: dict[str, zipfile.ZipInfo] = {}
        for name, info in names.items():
            rel = name[len(BUNDLE_DIR_NAME) + 1 :]
            if not rel or rel == ASSET_CHECKSUMS_NAME:
                continue
            present[rel] = info

        for rel, expected_digest in sorted(checksum_map.items()):
            info = present.get(rel)
            if info is None:
                raise DeployError(f"Checksum entry missing from ZIP: {rel}")
            actual = _member_digest(archive, info)
            if actual != expected_digest:
                raise DeployError(f"Checksum mismatch for {rel}")
        for rel in sorted(present):
            if rel not in checksum_map:
                raise DeployError(f"ZIP member missing from checksums: {rel}")

        for rel in REQUIRED_BUNDLE_RELS:
            if rel != ASSET_CHECKSUMS_NAME and rel not in present:
                raise DeployError(f"Release ZIP missing required file: {rel}")

    log(
        "Release triple verified: "
        f"channel={manifest['release_channel']} build={manifest['build_number']} "
        f"commit={manifest['short_commit']}"
    )
    return manifest


def _contained_target(destination: Path, relative: str) -> Path:
    root = Path(destination).resolve()
    target = (Path(destination) / Path(*relative.split("/"))).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise DeployError(f"ZIP member escapes destination: {relative!r}") from exc
    return target


def safe_extract_bundle(zip_path: Path, destination: Path) -> Path:
    """Extract a pre-verified ZIP with explicit containment checks."""
    members = inspect_zip_members(zip_path)
    destination = Path(destination)
    if destination.exists():
        raise DeployError(f"Extraction destination already exists: {destination}")
    ensure_private_dir(destination)

    with zipfile.ZipFile(zip_path) as archive:
        for info in members:
            relative = info.filename.rstrip("/")
            target = _contained_target(destination, relative)
            if info.is_dir() or info.filename.endswith("/"):
                ensure_private_dir(target)
                continue
            ensure_private_dir(target.parent)
            with archive.open(info, "r") as source:
                written = stream_to_private_file(target, source)
            if written != info.file_size:
                raise DeployError(
                    f"ZIP member size mismatch for {info.filename!r}: "
                    f"wrote {written}, expected {info.file_size}"
                )

    bundle_root = destination / BUNDLE_DIR_NAME
    if not bundle_root.is_dir():
        raise DeployError(f"Release ZIP did not extract to {BUNDLE_DIR_NAME}/.")
    return bundle_root


def extract_verified_control_modules(
    zip_path: Path,
    destination: Path,
    checksum_map: dict[str, str],
    rels: tuple[str, ...] = ENGINE_MODULE_RELS,
) -> None:
    """Extract only the control modules, verifying each against the release checksums.

    The whole game release is never unpacked to run the control engine. Links,
    specials, traversal, duplicates, and destination escapes are all refused.
    """
    members = inspect_zip_members(zip_path)
    by_rel: dict[str, zipfile.ZipInfo] = {}
    prefix_len = len(BUNDLE_DIR_NAME) + 1
    for info in members:
        if info.is_dir() or info.filename.endswith("/"):
            continue
        rel = info.filename[prefix_len:]
        if not rel:
            continue
        if rel in by_rel:
            raise DeployError(f"Duplicate ZIP member for control module path: {rel}")
        by_rel[rel] = info

    ensure_private_dir(destination)
    with zipfile.ZipFile(zip_path) as archive:
        for rel in rels:
            info = by_rel.get(rel)
            if info is None:
                raise MissingEngineError(
                    f"Release does not ship the required control module: {rel}"
                )
            expected = checksum_map.get(rel)
            if not expected:
                raise DeployError(f"Control module {rel} is not covered by checksums.sha256.")
            target = _contained_target(destination, rel)
            ensure_private_dir(target.parent)
            digest = hashlib.sha256()
            with archive.open(info, "r") as source:
                written = stream_to_private_file(target, source, digest=digest)
            if written != info.file_size:
                raise DeployError(
                    f"Control module size mismatch for {rel}: wrote {written}, "
                    f"expected {info.file_size}"
                )
            if digest.hexdigest() != expected:
                raise DeployError(f"Checksum mismatch for staged control module {rel}")


# ---------------------------------------------------------------------------
# Bounded control-engine staging under state/control-engine/<cache_id>/
# ---------------------------------------------------------------------------


def control_engine_root(deploy_root: Path) -> Path:
    return Path(deploy_root) / STATE_DIR_NAME / CONTROL_ENGINE_DIR_NAME


def verified_control_engine(
    staged_dir: Path,
    checksum_map: dict[str, str],
) -> Path | None:
    """Return the engine path when every staged module matches the release checksums."""
    staged_dir = Path(staged_dir)
    if staged_dir.is_symlink() or not staged_dir.is_dir():
        return None
    for rel in ENGINE_MODULE_RELS:
        expected = checksum_map.get(rel)
        if not expected:
            return None
        candidate = staged_dir / Path(*rel.split("/"))
        if candidate.is_symlink() or not candidate.is_file():
            return None
        try:
            if file_digest(candidate) != expected:
                return None
        except OSError:
            return None
    return staged_dir / Path(*ENGINE_REL_PATH.split("/"))


def _tree_is_control_engine_shaped(path: Path) -> bool:
    """True when a directory contains nothing but recognised control module paths."""
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        return False
    allowed_files = {tuple(rel.split("/")) for rel in ENGINE_MODULE_RELS}
    allowed_dirs: set[tuple[str, ...]] = set()
    for parts in allowed_files:
        for index in range(1, len(parts)):
            allowed_dirs.add(parts[:index])
    for child in path.rglob("*"):
        rel_parts = child.relative_to(path).parts
        if child.is_symlink():
            return False
        if child.is_dir():
            if rel_parts not in allowed_dirs:
                return False
            continue
        if not child.is_file():
            return False
        if rel_parts not in allowed_files:
            return False
    return True


def _quarantine_path(deploy_root: Path, name: str) -> Path:
    quarantine_root = ensure_private_dir(
        Path(deploy_root) / STATE_DIR_NAME / CONTROL_ENGINE_LEGACY_DIR_NAME
    )
    dest = quarantine_root / name
    if dest.exists() or dest.is_symlink():
        dest = quarantine_root / f"{name}-{utc_stamp()}"
    return dest


def _retire_control_engine_dir(deploy_root: Path, path: Path, *, reason: str) -> str:
    """Remove a recognised staging set, or preserve anything unrecognised."""
    path = Path(path)
    if path.is_symlink():
        note = f"preserved symlink in control-engine staging (never followed): {path}"
        log(f"WARNING: {note}")
        return note
    if _tree_is_control_engine_shaped(path):
        shutil.rmtree(path)
        return f"removed obsolete control engine set ({reason}): {path.name}"
    dest = _quarantine_path(deploy_root, path.name)
    os.replace(str(path), str(dest))
    note = (
        f"quarantined unrecognised control-engine entry (contents preserved): "
        f"{path.name} -> {dest}"
    )
    log(f"WARNING: {note}")
    return note


def purge_interrupted_control_staging(deploy_root: Path) -> list[str]:
    """Recover from an interrupted staging attempt on the next Restart."""
    notes: list[str] = []
    root = control_engine_root(deploy_root)
    if not root.is_dir() or root.is_symlink():
        return notes
    for child in sorted(root.iterdir()):
        if not child.name.startswith(STAGING_TEMP_PREFIX):
            continue
        notes.append(
            _retire_control_engine_dir(deploy_root, child, reason="interrupted staging")
        )
    for note in notes:
        log(note)
    return notes


def migrate_legacy_engine_staging(deploy_root: Path) -> list[str]:
    """Preserve and report pre-C1.1 `state/engine-staging-*` full-release extracts."""
    notes: list[str] = []
    state_dir = Path(deploy_root) / STATE_DIR_NAME
    if not state_dir.is_dir() or state_dir.is_symlink():
        return notes
    for child in sorted(state_dir.iterdir()):
        if not child.name.startswith(LEGACY_STAGING_PREFIX):
            continue
        if child.is_symlink():
            notes.append(
                f"preserved legacy staging symlink (never followed): {child}"
            )
            continue
        if not child.is_dir():
            notes.append(f"preserved unexpected legacy staging entry: {child}")
            continue
        dest = _quarantine_path(deploy_root, child.name)
        try:
            os.replace(str(child), str(dest))
        except OSError as exc:
            notes.append(f"could not quarantine legacy staging {child}: {exc}")
            continue
        notes.append(f"quarantined legacy engine staging (preserved): {child.name} -> {dest}")
    for note in notes:
        log(f"WARNING: {note}")
    return notes


def prune_control_engine_sets(deploy_root: Path, *, active: str) -> list[str]:
    """Keep the active bootstrap set plus at most one rollback/control set."""
    notes: list[str] = []
    root = control_engine_root(deploy_root)
    if not root.is_dir() or root.is_symlink():
        return notes
    protected = {active}
    deployed = read_deployed_release_id(deploy_root)
    if deployed and deployed != active and (root / deployed).is_dir():
        protected.add(deployed)
    candidates = [
        child
        for child in sorted(root.iterdir())
        if not child.name.startswith(STAGING_TEMP_PREFIX)
    ]
    unprotected = [child for child in candidates if child.name not in protected]
    if len(protected) < MAX_CONTROL_ENGINE_SETS and unprotected:
        keep = max(unprotected, key=lambda child: child.lstat().st_mtime_ns)
        protected.add(keep.name)
    for child in candidates:
        if child.name in protected:
            continue
        notes.append(_retire_control_engine_dir(deploy_root, child, reason="retention"))
    for note in notes:
        log(note)
    return notes


def stage_control_engine(
    deploy_root: Path,
    *,
    cache_id: str,
    zip_path: Path,
    checksum_map: dict[str, str],
) -> Path:
    """Stage only the verified control modules at a deterministic identity path."""
    require_safe_path_component(cache_id, label="control engine cache id")
    root = ensure_private_dir(control_engine_root(deploy_root))
    purge_interrupted_control_staging(deploy_root)

    target = root / cache_id
    engine = verified_control_engine(target, checksum_map)
    if engine is not None:
        log(f"Reusing verified control engine staging: {target}")
        prune_control_engine_sets(deploy_root, active=cache_id)
        return engine
    if target.exists() or target.is_symlink():
        log(_retire_control_engine_dir(deploy_root, target, reason="unverified staging"))

    temp = root / f"{STAGING_TEMP_PREFIX}{cache_id}-{utc_stamp()}"
    if temp.exists() or temp.is_symlink():
        _retire_control_engine_dir(deploy_root, temp, reason="stale staging temp")
    extract_verified_control_modules(zip_path, temp, checksum_map)
    if verified_control_engine(temp, checksum_map) is None:
        raise DeployError(f"Staged control engine failed verification: {temp}")
    os.replace(str(temp), str(target))
    _sync_directory(root)
    engine = verified_control_engine(target, checksum_map)
    if engine is None:
        raise DeployError(f"Control engine verification failed after staging: {target}")
    log(f"Staged verified control engine: {target}")
    prune_control_engine_sets(deploy_root, active=cache_id)
    return engine


# ---------------------------------------------------------------------------
# Engine selection
# ---------------------------------------------------------------------------


def repo_engine_path() -> Path | None:
    """Engine module in a private-repo checkout (developer / CI runs)."""
    candidate = REPO_ROOT / Path(*ENGINE_REL_PATH.split("/"))
    return candidate if candidate.is_file() else None


def staged_engine_path(bundle_root: Path) -> Path | None:
    candidate = bundle_root / Path(*ENGINE_REL_PATH.split("/"))
    if not candidate.is_file():
        return None
    for rel in ENGINE_SUPPORT_RELS:
        if not (bundle_root / Path(*rel.split("/"))).is_file():
            log(f"WARNING: staged release is missing engine support module: {rel}")
            return None
    return candidate


def current_engine_path(deploy_root: Path) -> Path | None:
    """Locate and verify the AMP engine already installed in `current/`.

    Returns None when `current/` has no engine at all (first install or a legacy
    pre-engine release, which must fall back to bootstrap staging). Raises when an
    engine *is* present but its integrity cannot be proven, so altered control-plane
    code is never executed.
    """
    current = Path(deploy_root) / CURRENT_DIR_NAME
    if not current.exists() or not current.is_dir():
        return None
    engine = current / Path(*ENGINE_REL_PATH.split("/"))
    if engine.is_symlink() or not engine.is_file():
        return None

    checksums_path = current / ASSET_CHECKSUMS_NAME
    manifest_path = current / ASSET_MANIFEST_NAME
    for path in (checksums_path, manifest_path):
        if path.is_symlink() or not path.is_file():
            raise DeployError(
                f"current/ ships {ENGINE_REL_PATH} but {path.name} is missing or unsafe; "
                "refusing to execute unverified control-plane code."
            )
    try:
        checksum_map = parse_checksums(checksums_path.read_text(encoding="utf-8"))
        manifest_bytes = manifest_path.read_bytes()
    except (OSError, UnicodeDecodeError) as exc:
        raise DeployError(f"Cannot verify current/ release integrity: {exc}") from exc
    if not checksum_map:
        raise DeployError("current/checksums.sha256 contains no entries; refusing to execute.")

    try:
        sanitize_manifest(json.loads(manifest_bytes.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeployError(f"current/release_manifest.json is invalid JSON: {exc}") from exc

    expected_manifest_digest = checksum_map.get(ASSET_MANIFEST_NAME)
    if not expected_manifest_digest:
        raise DeployError(
            "current/checksums.sha256 does not cover release_manifest.json; "
            "refusing to execute unverified control-plane code."
        )
    if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_digest:
        raise DeployError(
            "current/release_manifest.json does not match its recorded checksum; "
            "refusing to execute unverified control-plane code."
        )

    for rel in ENGINE_MODULE_RELS:
        candidate = current / Path(*rel.split("/"))
        if candidate.is_symlink() or not candidate.is_file():
            raise DeployError(
                f"current/ is missing control module {rel}; refusing partial engine."
            )
        expected = checksum_map.get(rel)
        if not expected:
            raise DeployError(
                f"current/checksums.sha256 does not cover {rel}; refusing to execute."
            )
        try:
            actual = file_digest(candidate)
        except OSError as exc:
            raise DeployError(f"Cannot hash current control module {rel}: {exc}") from exc
        if actual != expected:
            raise DeployError(
                f"current control module {rel} does not match its recorded checksum; "
                "refusing to execute altered control-plane code."
            )

    for rel in REQUIRED_BUNDLE_RELS:
        candidate = current / Path(*rel.split("/"))
        if candidate.is_symlink() or not candidate.is_file():
            raise DeployError(
                f"current/ is missing required release file {rel}; refusing to start it."
            )
        if rel not in checksum_map and rel != ASSET_CHECKSUMS_NAME:
            raise DeployError(f"current/checksums.sha256 does not cover {rel}; refusing.")

    log(f"Verified AMP engine already present in current/: {engine}")
    return engine


def engine_argv(
    engine: Path,
    *,
    deploy_root: Path,
    mode_flag: str,
    tag: str,
    cache: dict[str, Path] | None,
) -> list[str]:
    argv = [
        sys.executable,
        str(engine),
        mode_flag,
        "--deploy-root",
        str(deploy_root),
    ]
    if tag:
        argv.extend(["--tag", tag])
    if cache is not None:
        argv.extend(
            [
                "--from-cache",
                "--cache-zip",
                str(cache["zip"]),
                "--cache-manifest",
                str(cache["manifest"]),
                "--cache-checksums",
                str(cache["checksums"]),
            ]
        )
    return argv


def run_engine(
    argv: list[str],
    *,
    exec_replace: bool,
    config: dict[str, Any] | None = None,
) -> int:
    log("Handing off to release engine: " + " ".join(argv[1:]))
    run_engine_fn = (config or {}).get("run_engine_fn")
    if run_engine_fn is not None:
        return int(run_engine_fn(argv))
    if exec_replace and os.name == "posix" and hasattr(os, "execv"):
        sys.stdout.flush()
        sys.stderr.flush()
        # execv keeps this pid, so AMP's direct child remains the Python
        # supervisor that owns amp_start.sh and receives SIGTERM itself.
        os.execv(argv[0], argv)  # noqa: S606 - fixed interpreter + verified engine path
    result = subprocess.run(argv)  # noqa: S603 - fixed interpreter + verified engine path
    return int(result.returncode)


def current_release_present(deploy_root: Path) -> bool:
    start_script = Path(deploy_root) / CURRENT_DIR_NAME / "scripts" / "amp_start.sh"
    return start_script.is_file()


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def read_deployed_release_id(deploy_root: Path) -> str:
    state_path = Path(deploy_root) / STATE_DIR_NAME / "deployed_release.json"
    if state_path.is_symlink() or not state_path.is_file():
        return ""
    try:
        parsed = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    return str(parsed.get("release_id", "")).strip()


def run_check_only(config: dict[str, Any]) -> int:
    release = fetch_release(config)
    tag = str(release.get("tag_name", "")).strip()
    if not tag:
        raise DeployError("Release is missing tag_name.")
    select_required_assets(release)
    parsed = parse_release_tag(tag)
    if parsed["channel"] != config["channel"]:
        raise DeployError(
            f"Release tag channel {parsed['channel']!r} does not match configured "
            f"channel {config['channel']!r}."
        )
    deployed = read_deployed_release_id(config["deploy_root"])
    if deployed and deployed == parsed["cache_id"]:
        log(f"Already current: {deployed}")
    else:
        log(f"Update available: deployed={deployed or '(none)'} latest={parsed['cache_id']}")
    return 0


def cache_paths_for(incoming: Path, cache_id: str) -> dict[str, Path]:
    return {
        "zip": incoming / f"mmo_release-{cache_id}.zip",
        "manifest": incoming / f"release_manifest-{cache_id}.json",
        "checksums": incoming / f"checksums-{cache_id}.sha256",
    }


def _verified_cache_triple(
    cache: dict[str, Path], *, channel: str, tag: str, cache_id: str
) -> tuple[bytes, bytes] | None:
    for path in cache.values():
        if path.is_symlink() or not path.is_file():
            return None
    try:
        manifest_bytes = cache["manifest"].read_bytes()
        checksums_bytes = cache["checksums"].read_bytes()
        manifest = verify_release_triple(
            cache["zip"],
            manifest_bytes,
            checksums_bytes,
            expected_channel=channel,
            tag=tag,
        )
    except (DeployError, OSError):
        return None
    if release_id_from_manifest(manifest) != cache_id:
        return None
    return manifest_bytes, checksums_bytes


def stage_engine_from_release(config: dict[str, Any]) -> tuple[Path, dict[str, Path], str]:
    """Bootstrap-stage the release engine. Returns (engine, cache, tag).

    Only reached when there is no usable verified engine in `current/` (first
    install or a legacy pre-engine migration).
    """
    deploy_root: Path = Path(config["deploy_root"])
    incoming = ensure_private_dir(deploy_root / INCOMING_DIR_NAME)
    ensure_private_dir(deploy_root / STATE_DIR_NAME)
    migrate_legacy_engine_staging(deploy_root)

    release = fetch_release(config)
    tag = str(release.get("tag_name", "")).strip()
    if not tag:
        raise DeployError("Release is missing tag_name.")
    assets = select_required_assets(release)
    parsed_tag = parse_release_tag(tag)
    if parsed_tag["channel"] != config["channel"]:
        raise DeployError(
            f"Release tag channel {parsed_tag['channel']!r} does not match configured "
            f"channel {config['channel']!r}."
        )
    cache_id = parsed_tag["cache_id"]
    log(f"Selected release: tag={tag} cache_id={cache_id}")

    cache = cache_paths_for(incoming, cache_id)
    reused = _verified_cache_triple(cache, channel=config["channel"], tag=tag, cache_id=cache_id)
    if reused is not None:
        log(f"Reusing verified cache triple for {cache_id}; no download required.")
        manifest_bytes, checksums_bytes = reused
    else:
        # Capacity is proven after selection but strictly before the first
        # download byte and before any extraction.
        require_download_capacity(config, release)
        downloads = {
            "manifest": incoming / f"release_manifest-download-{cache_id}.json",
            "checksums": incoming / f"checksums-download-{cache_id}.sha256",
            "zip": incoming / f"mmo_release-download-{cache_id}.zip",
        }
        try:
            download_asset(config, assets[ASSET_MANIFEST_NAME], downloads["manifest"])
            download_asset(config, assets[ASSET_CHECKSUMS_NAME], downloads["checksums"])
            download_asset(config, assets[ASSET_ZIP_NAME], downloads["zip"])

            manifest_bytes = downloads["manifest"].read_bytes()
            checksums_bytes = downloads["checksums"].read_bytes()
            manifest = verify_release_triple(
                downloads["zip"],
                manifest_bytes,
                checksums_bytes,
                expected_channel=config["channel"],
                tag=tag,
            )
            if release_id_from_manifest(manifest) != cache_id:
                raise DeployError(
                    f"Release identity mismatch: tag implies {cache_id!r}, "
                    f"manifest derives {release_id_from_manifest(manifest)!r}."
                )
            copy_private_file(downloads["zip"], cache["zip"])
            write_private_bytes(cache["manifest"], manifest_bytes)
            write_private_bytes(cache["checksums"], checksums_bytes)
        finally:
            for path in downloads.values():
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    log(f"WARNING: could not remove download temp {path}: {exc}")

    checksum_map = parse_checksums(checksums_bytes.decode("utf-8"))
    engine = stage_control_engine(
        deploy_root, cache_id=cache_id, zip_path=cache["zip"], checksum_map=checksum_map
    )
    return engine, cache, tag


MODE_FLAGS = {
    "dry-run": "--dry-run",
    "deploy": "--deploy",
    "deploy-and-supervise": "--deploy-and-supervise",
}


def run(config: dict[str, Any]) -> int:
    deploy_root: Path = Path(config["deploy_root"])
    mode: str = str(config["mode"])
    establish_shim_umask()
    ensure_private_dir(deploy_root / STATE_DIR_NAME)

    repo_engine = repo_engine_path()
    if mode == "check-only":
        engine = repo_engine or current_engine_path(deploy_root)
        if engine is not None:
            return run_engine(
                engine_argv(
                    engine,
                    deploy_root=deploy_root,
                    mode_flag="--check-only",
                    tag=str(config.get("tag", "")),
                    cache=None,
                ),
                exec_replace=False,
                config=config,
            )
        return run_check_only(config)

    mode_flag = MODE_FLAGS[mode]
    if repo_engine is not None:
        log(f"Using repository engine: {repo_engine}")
        return run_engine(
            engine_argv(
                repo_engine,
                deploy_root=deploy_root,
                mode_flag=mode_flag,
                tag=str(config.get("tag", "")),
                cache=None,
            ),
            exec_replace=(mode == "deploy-and-supervise"),
            config=config,
        )

    if not config.get("token"):
        raise DeployError(
            f"{TOKEN_ENV_KEY} is required to download private release assets. "
            "Set the AMP GitHub Release Token field or control/deploy.env."
        )

    # Preferred path: an already-installed, checksum-verified engine handles the
    # GitHub comparison itself, so an unchanged release downloads and extracts
    # nothing and creates no new staging directory.
    installed_engine = current_engine_path(deploy_root)
    if installed_engine is not None:
        return run_engine(
            engine_argv(
                installed_engine,
                deploy_root=deploy_root,
                mode_flag=mode_flag,
                tag=str(config.get("tag", "")),
                cache=None,
            ),
            exec_replace=(mode == "deploy-and-supervise"),
            config=config,
        )

    log("No verified AMP engine in current/; bootstrap-staging one from the release.")
    try:
        engine, cache, tag = stage_engine_from_release(config)
    except MissingEngineError as exc:
        if current_release_present(deploy_root):
            log(f"WARNING: {exc} Existing current/ release preserved; no changes made.")
            return 0
        raise DeployError(
            f"{exc} No current/ release exists, so there is nothing safe to start. "
            "Publish a release that includes the AMP deployment engine."
        ) from exc

    return run_engine(
        engine_argv(
            engine,
            deploy_root=deploy_root,
            mode_flag=mode_flag,
            tag=tag,
            cache=cache,
        ),
        exec_replace=(mode == "deploy-and-supervise"),
        config=config,
    )


def load_config(args: argparse.Namespace) -> dict[str, Any]:
    deploy_root = resolve_deploy_root(args.deploy_root)
    load_deploy_env_file(deploy_root / CONTROL_DIR_NAME / "deploy.env")
    token = os.environ.get(TOKEN_ENV_KEY, "").strip()
    register_secret(token)
    register_secret(os.environ.get(INVITE_ENV_KEY, "").strip())

    owner = os.environ.get("SCRATCH_GITHUB_OWNER", DEFAULT_OWNER).strip() or DEFAULT_OWNER
    repo = os.environ.get("SCRATCH_GITHUB_REPO", DEFAULT_REPO).strip() or DEFAULT_REPO
    channel = (
        os.environ.get("SCRATCH_RELEASE_CHANNEL", DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL
    )

    mode = "deploy"
    if args.check_only:
        mode = "check-only"
    elif args.dry_run:
        mode = "dry-run"
    elif args.supervise:
        mode = "deploy-and-supervise"

    return {
        "deploy_root": deploy_root,
        "repo_slug": f"{owner}/{repo}",
        "token": token,
        "channel": channel,
        "tag": (args.tag or os.environ.get("SCRATCH_RELEASE_TAG", "")).strip(),
        "mode": mode,
        "started_at_utc": utc_now_iso(),
        "github_fetch_fn": None,
        "download_fn": None,
        "free_bytes_fn": None,
        "run_engine_fn": None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Scratch MMO AMP control-plane shim: verify a GitHub release and hand off to "
            "the release-bundled AMP deployment engine."
        )
    )
    parser.add_argument("--deploy-root", default=None, help="AMP instance root")
    parser.add_argument("--check-only", action="store_true", help="Compare latest release to state")
    parser.add_argument("--dry-run", action="store_true", help="Verify without changing current/")
    parser.add_argument("--deploy", action="store_true", help="Verify and deploy into current/")
    parser.add_argument(
        "--supervise",
        action="store_true",
        help="Deploy, then let the release engine start and health-check the game",
    )
    parser.add_argument("--tag", default=None, help="Deploy a specific GitHub release tag")
    parser.add_argument("--yes", action="store_true", help="Non-interactive confirmation")
    parser.add_argument(
        "--log-file",
        default=None,
        help="Mirror console output into this file (no shell pipeline)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(raw_argv)

    if args.supervise and not args.deploy:
        args.deploy = True
    selected = sum(1 for flag in (args.check_only, args.dry_run, args.deploy) if flag)
    if selected != 1:
        parser.error("Specify exactly one of --check-only, --dry-run, or --deploy.")

    install_dual_logging(args.log_file)
    establish_shim_umask()

    try:
        config = load_config(args)
        assert_no_secrets_in_argv(raw_argv)
    except DeployError as exc:
        log(f"ERROR: {exc}")
        return 1

    log(f"Deploy root: {config['deploy_root']}")
    log(f"GitHub repo: {config['repo_slug']}")
    log(f"Mode:        {config['mode']}")
    log(f"Token configured: {'yes' if config['token'] else 'no'}")

    try:
        return run(config)
    except DeployError as exc:
        log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
