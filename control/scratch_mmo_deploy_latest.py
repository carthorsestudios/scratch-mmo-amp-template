#!/usr/bin/env python3
"""AMP control-plane shim: verify a GitHub release triple, then hand off to the engine.

This file lives at `control/scratch_mmo_deploy_latest.py` on an AMP instance and is
intentionally small and stable. It performs only the work that must happen before a
release can be trusted:

1. select a GitHub release (latest or SCRATCH_RELEASE_TAG / --tag)
2. require exactly the three publish assets
3. download them privately into `incoming/`
4. verify the external manifest/checksums are byte-identical to the ZIP members and
   that every ZIP member matches its recorded SHA-256 (no extraction yet)
5. safely extract the verified ZIP into a staging area
6. execute `deployment/amp/amp_release_updater.py` from that release for the real
   deployment transaction

There is deliberately **no** legacy directory-rename swap path. When a release does
not ship the AMP engine modules, an existing `current/` is preserved and the run
either warns (current present) or fails with a clear error (no current).

The GitHub token is read only from SCRATCH_GITHUB_TOKEN and never appears in argv,
a URL, or a log line. SCRATCH_MMO_INVITE_CODE stays environment-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

ENGINE_REL_PATH = "deployment/amp/amp_release_updater.py"
ENGINE_SUPPORT_RELS = (
    "deployment/amp/amp_transaction.py",
    "deployment/amp/amp_permissions.py",
    "deployment/vps/deployment_state_io.py",
    "deployment/vps/deployment_storage.py",
    "deployment/vps/deployment_permissions.py",
    "tools/release_bundle_lib.py",
)
REQUIRED_BUNDLE_RELS = (
    ASSET_MANIFEST_NAME,
    ASSET_CHECKSUMS_NAME,
    "scripts/amp_start.sh",
    "gateway/mmo_web_gateway",
    "server/mmo_server.x86_64",
    "web/index.html",
)

TOKEN_ENV_KEY = "SCRATCH_GITHUB_TOKEN"
INVITE_ENV_KEY = "SCRATCH_MMO_INVITE_CODE"
SECRET_ENV_KEYS = (TOKEN_ENV_KEY, INVITE_ENV_KEY, "GITHUB_TOKEN")

GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "scratch-mmo-amp-control/2.0"

MAX_ZIP_ENTRIES = 10_000
MAX_ZIP_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
READ_CHUNK_BYTES = 1024 * 1024

MODE_PRIVATE_FILE = 0o600
MODE_PRIVATE_DIR = 0o700

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
# Configuration
# ---------------------------------------------------------------------------


def load_deploy_env_file(path: Path) -> None:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return
    log(f"Loading config: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
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
    if control_dir.name == "control":
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


def github_request(
    url: str,
    token: str,
    *,
    accept: str = "application/vnd.github+json",
    timeout: float = 60.0,
) -> tuple[int, dict[str, str], bytes]:
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
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


def fetch_release(repo_slug: str, token: str, tag: str) -> dict[str, Any]:
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
    if not isinstance(release, dict):
        raise DeployError("GitHub API returned non-object JSON.")
    if release.get("draft"):
        raise DeployError("Release is a draft; refusing to deploy.")
    if release.get("prerelease"):
        raise DeployError("Release is a prerelease; refusing to deploy.")
    return release


def select_required_assets(release: dict[str, Any]) -> dict[str, dict[str, Any]]:
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        raise DeployError("Release assets missing or invalid.")
    found: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise DeployError("Release asset entry is not an object.")
        name = str(asset.get("name", "")).strip()
        if name not in REQUIRED_ASSET_NAMES:
            continue
        if name in found:
            raise DeployError(f"Release publishes duplicate asset {name!r}; refusing.")
        found[name] = asset
    missing = [name for name in REQUIRED_ASSET_NAMES if name not in found]
    if missing:
        raise DeployError("Release is missing required assets: " + ", ".join(missing))
    return found


def download_asset(repo_slug: str, asset: dict[str, Any], token: str, dest: Path) -> None:
    asset_id = asset.get("id")
    name = str(asset.get("name", "?"))
    if asset_id is None:
        raise DeployError(f"Release asset {name} is missing an id.")
    url = f"https://api.github.com/repos/{repo_slug}/releases/assets/{asset_id}"
    log(f"Downloading asset {name} -> {dest.name}")
    status, _headers, body = github_request(
        url, token, accept="application/octet-stream", timeout=300.0
    )
    if status != 200:
        raise DeployError(
            f"Asset download failed for {name} ({status}): "
            f"{redact(body.decode('utf-8', errors='replace')[:200])}"
        )
    write_private_bytes(dest, body)
    log(f"Download complete: {dest.name} ({dest.stat().st_size} bytes)")


# ---------------------------------------------------------------------------
# Private filesystem writes (no root, no chown)
# ---------------------------------------------------------------------------


def ensure_private_dir(path: Path) -> Path:
    path = Path(path)
    if path.is_symlink():
        raise DeployError(f"Refusing symlinked directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, MODE_PRIVATE_DIR)
    except OSError:
        pass
    return path


def write_private_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise DeployError(f"Refusing to write through symlink: {path}")
    temp_path = path.with_name(f".{path.name}.partial")
    temp_path.unlink(missing_ok=True)
    fd = os.open(str(temp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, MODE_PRIVATE_FILE)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(temp_path, MODE_PRIVATE_FILE)
    except OSError:
        pass
    os.replace(str(temp_path), str(path))
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise DeployError(f"Refusing non-regular file after write: {path}")


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


def release_id_from_manifest(manifest: dict[str, str]) -> str:
    release_id = (
        f"{manifest['release_channel']}-{manifest['short_commit']}-run{manifest['build_number']}"
    )
    for char in release_id:
        if not (char.isalnum() or char in "-_."):
            raise DeployError(f"Unsafe release id derived from manifest: {release_id!r}")
    return release_id


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
    return {
        "channel": channel,
        "short_commit": short_commit.lower(),
        "build_number": build_number,
        "attempt": attempt,
        "cache_id": f"{channel}-{short_commit.lower()}-run{build_number}",
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


def safe_extract_bundle(zip_path: Path, destination: Path) -> Path:
    """Extract a pre-verified ZIP with explicit containment checks."""
    members = inspect_zip_members(zip_path)
    destination = Path(destination)
    if destination.exists():
        raise DeployError(f"Extraction destination already exists: {destination}")
    ensure_private_dir(destination)
    root = destination.resolve()

    with zipfile.ZipFile(zip_path) as archive:
        for info in members:
            relative = info.filename.rstrip("/")
            target = (destination / Path(*relative.split("/"))).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise DeployError(
                    f"ZIP member escapes destination: {info.filename!r}"
                ) from exc
            if info.is_dir() or info.filename.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with archive.open(info, "r") as source, target.open("wb") as out:
                while True:
                    chunk = source.read(READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
            if written != info.file_size:
                raise DeployError(
                    f"ZIP member size mismatch for {info.filename!r}: "
                    f"wrote {written}, expected {info.file_size}"
                )
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                cleaned = (mode & 0o777) | 0o600
                cleaned &= ~(stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
                try:
                    os.chmod(target, cleaned)
                except OSError:
                    pass

    bundle_root = destination / BUNDLE_DIR_NAME
    if not bundle_root.is_dir():
        raise DeployError(f"Release ZIP did not extract to {BUNDLE_DIR_NAME}/.")
    return bundle_root


# ---------------------------------------------------------------------------
# Engine handoff
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


def run_engine(argv: list[str], *, exec_replace: bool) -> int:
    log("Handing off to release engine: " + " ".join(argv[1:]))
    if exec_replace and os.name == "posix" and hasattr(os, "execv"):
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(argv[0], argv)  # noqa: S606 - fixed interpreter + verified engine path
    result = subprocess.run(argv)  # noqa: S603 - fixed interpreter + verified engine path
    return int(result.returncode)


def current_release_present(deploy_root: Path) -> bool:
    start_script = deploy_root / "current" / "scripts" / "amp_start.sh"
    return start_script.is_file()


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def read_deployed_release_id(deploy_root: Path) -> str:
    state_path = deploy_root / "state" / "deployed_release.json"
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
    release = fetch_release(config["repo_slug"], config["token"], config["tag"])
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


def stage_engine_from_release(config: dict[str, Any]) -> tuple[Path, dict[str, Path], str]:
    """Download, verify, and stage the release engine. Returns (engine, cache, tag)."""
    deploy_root: Path = config["deploy_root"]
    incoming = ensure_private_dir(deploy_root / "incoming")
    state_dir = ensure_private_dir(deploy_root / "state")

    release = fetch_release(config["repo_slug"], config["token"], config["tag"])
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
    log(f"Selected release: tag={tag} cache_id={parsed_tag['cache_id']}")

    downloads = {
        "manifest": incoming / f"release_manifest-download-{tag}.json",
        "checksums": incoming / f"checksums-download-{tag}.sha256",
        "zip": incoming / f"mmo_release-download-{tag}.zip",
    }
    download_asset(config["repo_slug"], assets[ASSET_MANIFEST_NAME], config["token"], downloads["manifest"])
    download_asset(config["repo_slug"], assets[ASSET_CHECKSUMS_NAME], config["token"], downloads["checksums"])
    download_asset(config["repo_slug"], assets[ASSET_ZIP_NAME], config["token"], downloads["zip"])

    manifest_bytes = downloads["manifest"].read_bytes()
    checksums_bytes = downloads["checksums"].read_bytes()
    manifest = verify_release_triple(
        downloads["zip"],
        manifest_bytes,
        checksums_bytes,
        expected_channel=config["channel"],
        tag=tag,
    )
    cache_id = release_id_from_manifest(manifest)
    if cache_id != parsed_tag["cache_id"]:
        raise DeployError(
            f"Release identity mismatch: tag implies {parsed_tag['cache_id']!r}, "
            f"manifest derives {cache_id!r}."
        )

    cache = {
        "zip": incoming / f"mmo_release-{cache_id}.zip",
        "manifest": incoming / f"release_manifest-{cache_id}.json",
        "checksums": incoming / f"checksums-{cache_id}.sha256",
    }
    write_private_bytes(cache["zip"], downloads["zip"].read_bytes())
    write_private_bytes(cache["manifest"], manifest_bytes)
    write_private_bytes(cache["checksums"], checksums_bytes)
    for path in downloads.values():
        path.unlink(missing_ok=True)

    staging_parent = state_dir / f"engine-staging-{cache_id}-{utc_stamp()}"
    bundle_root = safe_extract_bundle(cache["zip"], staging_parent)
    engine = staged_engine_path(bundle_root)
    if engine is None:
        raise MissingEngineError(
            f"Release {tag} does not ship {ENGINE_REL_PATH}; refusing legacy swap."
        )
    return engine, cache, tag


def run(config: dict[str, Any]) -> int:
    deploy_root: Path = config["deploy_root"]
    mode: str = config["mode"]
    ensure_private_dir(deploy_root / "state")

    if mode == "check-only":
        repo_engine = repo_engine_path()
        if repo_engine is not None:
            return run_engine(
                engine_argv(
                    repo_engine,
                    deploy_root=deploy_root,
                    mode_flag="--check-only",
                    tag=config["tag"],
                    cache=None,
                ),
                exec_replace=False,
            )
        return run_check_only(config)

    repo_engine = repo_engine_path()
    if repo_engine is not None:
        log(f"Using repository engine: {repo_engine}")
        mode_flag = {
            "dry-run": "--dry-run",
            "deploy": "--deploy",
            "deploy-and-supervise": "--deploy-and-supervise",
        }[mode]
        return run_engine(
            engine_argv(
                repo_engine,
                deploy_root=deploy_root,
                mode_flag=mode_flag,
                tag=config["tag"],
                cache=None,
            ),
            exec_replace=(mode == "deploy-and-supervise"),
        )

    if not config["token"]:
        raise DeployError(
            f"{TOKEN_ENV_KEY} is required to download private release assets. "
            "Set the AMP GitHub Release Token field or control/deploy.env."
        )

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

    mode_flag = {
        "dry-run": "--dry-run",
        "deploy": "--deploy",
        "deploy-and-supervise": "--deploy-and-supervise",
    }[mode]
    return run_engine(
        engine_argv(
            engine,
            deploy_root=deploy_root,
            mode_flag=mode_flag,
            tag=tag,
            cache=cache,
        ),
        exec_replace=(mode == "deploy-and-supervise"),
    )


def load_config(args: argparse.Namespace) -> dict[str, Any]:
    deploy_root = resolve_deploy_root(args.deploy_root)
    load_deploy_env_file(deploy_root / "control" / "deploy.env")
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
