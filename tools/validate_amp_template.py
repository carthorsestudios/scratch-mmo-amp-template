#!/usr/bin/env python3
"""Validate Scratch MMO AMP template security and updater wiring."""

from __future__ import annotations

import contextlib
import functools
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EMIT_SCRIPT = ROOT / "tools" / "emit_start_command.py"
sys.path.insert(0, str(ROOT / "tools"))

from emit_start_command import (  # noqa: E402
    build_start_command_args,
    decode_installer_from_start_args,
    encode_installer_script,
    simulate_amp_arg_split,
)
from generate_bootstrap_pins import (  # noqa: E402
    PINNED_CONTROL_FILES,
    PinError,
    expected_pins,
    git_blob_matches_normalized,
    read_installer_pins,
)

REQUIRED_CONFIG_VERSION = 4

errors: list[str] = []


def fail(message: str) -> None:
    errors.append(message)
    print(f"FAIL  {message}")


def ok(message: str) -> None:
    print(f"OK    {message}")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def find_config_field(config: list[dict], field_name: str) -> dict | None:
    for entry in config:
        if entry.get("FieldName") == field_name:
            return entry
    return None


def _bash_runs_scripts_verbatim(candidate: str) -> bool:
    """Reject bash shims that re-evaluate `-c` text through an extra shell layer.

    The Windows Store / WSL interop `bash.exe` expands `$VAR` and `$(...)` before the
    real shell sees the script, which silently invalidates every behavioural test.
    """
    try:
        proc = subprocess.run(
            [candidate, "-c", 'A=1; B=$(printf 2); printf "%s" "ok$A$B"'],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "ok12"


@functools.lru_cache(maxsize=1)
def find_bash() -> str | None:
    """Locate a real bash, including Git for Windows shells that are not on PATH."""
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    on_path = shutil.which("bash")
    if on_path:
        candidates.insert(0 if os.name != "nt" else len(candidates), on_path)
    for candidate in candidates:
        if Path(candidate).is_file() and _bash_runs_scripts_verbatim(candidate):
            return candidate
    return None


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def bash_syntax_error(bash_bin: str, script: str) -> str | None:
    """Run `bash -n` over a script without letting Windows inject CR bytes."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        probe = Path(tmp) / "probe.sh"
        write_lf(probe, script.replace("\r\n", "\n"))
        result = subprocess.run(
            [bash_bin, "-n", str(probe)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    if result.returncode == 0:
        return None
    return result.stderr.strip() or f"bash -n exited {result.returncode}"


def validate_json_files() -> None:
    for rel in ("scratchmmoconfig.json", "scratchmmoports.json", "scratchmmoupdates.json", "manifest.json"):
        path = ROOT / rel
        if not path.is_file():
            fail(f"Missing {rel}")
            continue
        try:
            json.loads(read_text(path))
        except json.JSONDecodeError as exc:
            fail(f"Invalid JSON in {rel}: {exc}")
        else:
            ok(f"{rel} parses as JSON")

    updates_path = ROOT / "scratchmmoupdates.json"
    if updates_path.is_file():
        try:
            updates = json.loads(read_text(updates_path))
        except json.JSONDecodeError:
            return
        if updates:
            fail("scratchmmoupdates.json must stay empty (AMP Update is not supported)")
        else:
            ok("scratchmmoupdates.json is empty (AMP Update unsupported)")


def validate_kvp_json_consistency() -> None:
    """Every {{Placeholder}} in the KVP must resolve to a config field or AMP builtin."""
    kvp_path = ROOT / "scratchmmo.kvp"
    config_path = ROOT / "scratchmmoconfig.json"
    ports_path = ROOT / "scratchmmoports.json"
    if not (kvp_path.is_file() and config_path.is_file() and ports_path.is_file()):
        return

    kvp = read_text(kvp_path)
    config = json.loads(read_text(config_path))
    ports = json.loads(read_text(ports_path))

    field_names = {str(entry.get("FieldName") or "") for entry in config}
    placeholders = set(re.findall(r"\{\{([^}]+)\}\}", kvp))
    unknown = sorted(name for name in placeholders if name not in field_names)
    if unknown:
        fail(f"KVP placeholders with no scratchmmoconfig.json field: {unknown}")
    else:
        ok("every KVP placeholder maps to a scratchmmoconfig.json field")

    port_refs = {str(entry.get("Ref") or "") for entry in ports}
    kvp_refs = set(re.findall(r"^App\.\w*PortRef=(\w+)$", kvp, re.MULTILINE))
    missing_refs = sorted(ref for ref in kvp_refs if ref not in port_refs)
    if missing_refs:
        fail(f"KVP references ports missing from scratchmmoports.json: {missing_refs}")
    else:
        ok("KVP port refs exist in scratchmmoports.json")

    if {"ServerPort", "WebPort"} - port_refs:
        fail("scratchmmoports.json must keep ServerPort and WebPort refs")
    else:
        ok("port mappings keep ServerPort and WebPort")

    expected_ports = {"ServerPort": 19080, "WebPort": 9090}
    for entry in ports:
        ref = str(entry.get("Ref") or "")
        if ref in expected_ports and entry.get("Port") != expected_ports[ref]:
            fail(f"Port {ref} changed from {expected_ports[ref]} to {entry.get('Port')}")
    ok("port numbers unchanged (19080 internal, 9090 public)")

    manifest = json.loads(read_text(ROOT / "manifest.json"))
    if manifest.get("prefix") != "SCRATCH":
        fail("manifest.json prefix must remain SCRATCH")
    else:
        ok("manifest.json prefix is SCRATCH")


def validate_control_files() -> None:
    bootstrap = ROOT / "control" / "amp_bootstrap_start.sh"
    deploy_py = ROOT / "control" / "scratch_mmo_deploy_latest.py"
    for path, label in [
        (bootstrap, "bootstrap script"),
        (deploy_py, "deploy updater script"),
    ]:
        if not path.is_file():
            fail(f"Missing {label}: {path.relative_to(ROOT).as_posix()}")
        else:
            ok(f"{label} present")

    if not bootstrap.is_file():
        return

    text = read_text(bootstrap)
    if "current/scripts/amp_start.sh" not in text:
        fail("bootstrap must exec current/scripts/amp_start.sh")
    else:
        ok("bootstrap starts current/scripts/amp_start.sh")
    if "run_setup_server" not in text and "Setup server listening port=" not in text:
        fail("bootstrap must include setup holding server mode")
    else:
        ok("bootstrap includes setup holding server mode")
    if "SCRATCH_GITHUB_TOKEN" not in text:
        fail("bootstrap must gate deploy on SCRATCH_GITHUB_TOKEN")
    else:
        ok("bootstrap gates deploy on SCRATCH_GITHUB_TOKEN")
    if re.search(r"(?:expose|open|listen).{0,20}19080", text, re.IGNORECASE):
        fail("bootstrap must not expose port 19080 in setup mode")
    else:
        ok("bootstrap does not expose port 19080")

    if "--deploy --supervise --yes" not in text:
        fail("bootstrap must hand off to the shim with --deploy --supervise --yes")
    else:
        ok("bootstrap uses supervised handoff (--deploy --supervise --yes)")

    if not re.search(r"SCRATCH_MMO_INVITE_CODE|--invite-code", text):
        ok("bootstrap never touches the invite code")
    else:
        fail("bootstrap must not read or forward the invite code")

    bash_bin = find_bash()
    if bash_bin is None:
        ok("bash not available locally; skipping bootstrap syntax check")
    else:
        problem = bash_syntax_error(bash_bin, text)
        if problem:
            fail(f"bootstrap fails bash -n: {problem}")
        else:
            ok("bootstrap passes bash -n syntax check")


def validate_bootstrap_pins(decoded_installer: str | None) -> None:
    """The installer must verify downloaded control bytes against deterministic pins."""
    installer_path = ROOT / "tools" / "inline_start_installer.sh"
    if not installer_path.is_file():
        fail("Missing tools/inline_start_installer.sh")
        return
    installer = read_text(installer_path)

    try:
        wanted = expected_pins()
    except PinError as exc:
        fail(f"cannot compute expected bootstrap pins: {exc}")
        return

    try:
        source_pins = read_installer_pins(installer)
    except PinError as exc:
        fail(f"installer pin assignments unreadable: {exc}")
        return

    for name, rel in PINNED_CONTROL_FILES.items():
        if not re.fullmatch(r"[0-9a-f]{64}", source_pins.get(name, "")):
            fail(f"{name} in inline_start_installer.sh is not a 64-hex SHA-256")
        elif source_pins[name] != wanted[name]:
            fail(
                f"{name} is stale for {rel}; run "
                "python tools/emit_start_command.py --write-kvp"
            )
        else:
            ok(f"{name} pins committed {rel}")

    if decoded_installer is not None:
        try:
            payload_pins = read_installer_pins(decoded_installer)
        except PinError as exc:
            fail(f"base64 start payload pin assignments unreadable: {exc}")
        else:
            if payload_pins != wanted:
                fail("base64 start payload carries different bootstrap pins than control/")
            else:
                ok("base64 start payload carries the committed bootstrap pins")

    for name, rel in PINNED_CONTROL_FILES.items():
        matched = git_blob_matches_normalized(rel)
        if matched is None:
            ok(f"git unavailable; skipping blob normalization check for {rel}")
        elif not matched:
            fail(
                f"{rel} would be stored with different bytes than the pinned digest "
                "(check .gitattributes eol=lf)"
            )
        else:
            ok(f"{rel} blob bytes match the pinned digest")

    for needle, label in [
        ("sha256sum", "sha256sum verification"),
        ("shasum -a 256", "shasum fallback"),
        ("SHA-256 mismatch", "digest mismatch abort"),
        ("missing SHA-256 pin", "empty-pin abort"),
        ("empty download", "empty download abort"),
        ("refusing to execute empty or missing", "empty bootstrap exec guard"),
    ]:
        if needle not in installer:
            fail(f"installer missing {label} ({needle!r})")
        else:
            ok(f"installer implements {label}")

    if "SCRATCH_TEMPLATE_REF:-main" not in installer:
        fail("installer must default its raw URL ref to main")
    else:
        ok("installer defaults raw URL ref to main (commit pin optional via SCRATCH_TEMPLATE_REF)")

    # Installation must only happen inside the verified branch: the mv into control/ has
    # to be textually dominated by both fetch_verified calls.
    verify_pos = installer.rfind("fetch_verified \"$BASE/scratch_mmo_deploy_latest.py\"")
    install_pos = installer.find("mv \"$TMP_BOOTSTRAP\" control/amp_bootstrap_start.sh")
    if verify_pos < 0 or install_pos < 0:
        fail("installer must fetch-verify then mv temp files into control/")
    elif install_pos < verify_pos:
        fail("installer installs control/amp_bootstrap_start.sh before verifying digests")
    else:
        ok("installer installs control files only after both digests verify")

    if re.search(r"-o\s+control/(?:amp_bootstrap_start\.sh|scratch_mmo_deploy_latest\.py)", installer):
        fail("installer must not download directly onto control/ files")
    else:
        ok("installer downloads to temp files, never straight onto control/")

    if re.search(r"^\s*exec\s+/bin/bash\s+control/amp_bootstrap_start\.sh", installer, re.MULTILINE):
        ok("installer execs control/amp_bootstrap_start.sh")
    else:
        fail("installer must exec control/amp_bootstrap_start.sh")


def validate_installer_regeneration(cmd_line: str) -> None:
    installer_path = ROOT / "tools" / "inline_start_installer.sh"
    if not installer_path.is_file():
        return
    script = read_text(installer_path)
    first = encode_installer_script(script)
    second = encode_installer_script(script)
    if first != second:
        fail("base64 installer encoding is not deterministic")
    else:
        ok("base64 installer encoding is deterministic")

    if cmd_line != build_start_command_args():
        fail("App.CommandLineArgs is not a byte-exact regeneration of the installer source")
    else:
        ok("App.CommandLineArgs regenerates byte-exactly from installer source")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _serve_directory(directory: Path):
    handler = functools.partial(_QuietHandler, directory=str(directory))
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{httpd.server_address[1]}/control"
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D102 - silence test server noise
        pass


STUB_BOOTSTRAP = "#!/usr/bin/env bash\necho STUB_BOOTSTRAP_RAN\n"
STUB_DEPLOY = "#!/usr/bin/env python3\nprint('stub deploy shim')\n"
EXISTING_BOOTSTRAP = "#!/usr/bin/env bash\necho EXISTING_BOOTSTRAP_RAN\n"
EXISTING_CURRENT_START = "#!/usr/bin/env bash\necho CURRENT_START_RAN\n"


def _patch_pins(payload: str, pins: dict[str, str]) -> str:
    for name, digest in pins.items():
        payload = re.sub(rf"\b{name}=[0-9a-f]*", f"{name}={digest}", payload)
    return payload


def _stub_pins() -> dict[str, str]:
    import hashlib

    return {
        "BOOTSTRAP_SHA256": hashlib.sha256(STUB_BOOTSTRAP.encode()).hexdigest(),
        "DEPLOY_SHA256": hashlib.sha256(STUB_DEPLOY.encode()).hexdigest(),
    }


def _run_bash(
    argv: list[str],
    instance: Path,
    base_url: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SCRATCH_TEMPLATE_BASE_URL"] = base_url
    env.pop("SCRATCH_TEMPLATE_REF", None)
    proc = subprocess.run(
        argv,
        cwd=str(instance),
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if os.environ.get("SCRATCH_VALIDATE_TRACE"):
        print(f"TRACE exit={proc.returncode}\n{proc.stderr}\n{proc.stdout}")
    return proc


def _run_payload(
    bash_bin: str,
    payload: str,
    instance: Path,
    base_url: str,
) -> subprocess.CompletedProcess[str]:
    flag = "-xc" if os.environ.get("SCRATCH_VALIDATE_TRACE") else "-c"
    return _run_bash([bash_bin, flag, payload], instance, base_url)


def validate_installer_behaviour(decoded_installer: str, wrapper_arg: str) -> None:
    """Run the shipped installer payload in throwaway instance roots."""
    bash_bin = find_bash()
    if bash_bin is None:
        ok("bash not available locally; skipping installer behaviour tests")
        return

    payload = _patch_pins(decoded_installer, _stub_pins())

    def prepare_remote(root: Path, bootstrap: str | None, deploy: str | None) -> Path:
        remote = root / "remote" / "control"
        remote.mkdir(parents=True)
        if bootstrap is not None:
            write_lf(remote / "amp_bootstrap_start.sh", bootstrap)
        if deploy is not None:
            write_lf(remote / "scratch_mmo_deploy_latest.py", deploy)
        return root / "remote"

    # 1. Happy path: matching digests install both files and exec the bootstrap.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        remote = prepare_remote(root, STUB_BOOTSTRAP, STUB_DEPLOY)
        instance = root / "instance"
        instance.mkdir()
        with _serve_directory(remote) as base_url:
            proc = _run_payload(bash_bin, payload, instance, base_url)
        installed_boot = instance / "control" / "amp_bootstrap_start.sh"
        installed_deploy = instance / "control" / "scratch_mmo_deploy_latest.py"
        if proc.returncode != 0:
            fail(f"verified installer run failed (exit={proc.returncode}): {proc.stderr.strip()}")
        elif "STUB_BOOTSTRAP_RAN" not in proc.stdout:
            fail("verified installer did not exec control/amp_bootstrap_start.sh")
        elif installed_boot.read_text(encoding="utf-8") != STUB_BOOTSTRAP:
            fail("installed control/amp_bootstrap_start.sh does not match verified bytes")
        elif installed_deploy.read_text(encoding="utf-8") != STUB_DEPLOY:
            fail("installed control/scratch_mmo_deploy_latest.py does not match verified bytes")
        elif list((instance / "control").glob(".*")):
            fail("installer left temporary files behind in control/")
        else:
            ok("installer installs control/amp_bootstrap_start.sh + "
               "control/scratch_mmo_deploy_latest.py and execs the bootstrap")

    # 2. Tampered bootstrap bytes must not be installed or executed.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        remote = prepare_remote(root, STUB_BOOTSTRAP + "echo PWNED\n", STUB_DEPLOY)
        instance = root / "instance"
        write_lf(instance / "control" / "amp_bootstrap_start.sh", EXISTING_BOOTSTRAP)
        with _serve_directory(remote) as base_url:
            proc = _run_payload(bash_bin, payload, instance, base_url)
        kept = (instance / "control" / "amp_bootstrap_start.sh").read_text(encoding="utf-8")
        if "SHA-256 mismatch" not in proc.stderr:
            fail("tampered bootstrap download did not report a SHA-256 mismatch")
        elif "PWNED" in proc.stdout:
            fail("tampered bootstrap bytes were executed")
        elif kept != EXISTING_BOOTSTRAP:
            fail("tampered bootstrap bytes overwrote the existing control file")
        elif "EXISTING_BOOTSTRAP_RAN" not in proc.stdout or proc.returncode != 0:
            fail("tampered download did not fall back to the existing control bootstrap")
        else:
            ok("tampered bootstrap bytes rejected; existing control bootstrap reused")

    # 3. A bad second file must not leave a half-installed control/.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        remote = prepare_remote(root, STUB_BOOTSTRAP, STUB_DEPLOY + "# tampered\n")
        instance = root / "instance"
        write_lf(instance / "control" / "amp_bootstrap_start.sh", EXISTING_BOOTSTRAP)
        with _serve_directory(remote) as base_url:
            proc = _run_payload(bash_bin, payload, instance, base_url)
        kept = (instance / "control" / "amp_bootstrap_start.sh").read_text(encoding="utf-8")
        if (instance / "control" / "scratch_mmo_deploy_latest.py").exists():
            fail("unverified scratch_mmo_deploy_latest.py was installed")
        elif kept != EXISTING_BOOTSTRAP:
            fail("control/amp_bootstrap_start.sh replaced even though the shim failed verification")
        elif "EXISTING_BOOTSTRAP_RAN" not in proc.stdout:
            fail("partial verification failure did not fall back to the existing bootstrap")
        else:
            ok("control install is all-or-nothing across both pinned files")

    # 4. Empty (but HTTP-200) downloads must never be installed or executed.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        remote = prepare_remote(root, "", "")
        instance = root / "instance"
        write_lf(instance / "control" / "amp_bootstrap_start.sh", EXISTING_BOOTSTRAP)
        with _serve_directory(remote) as base_url:
            proc = _run_payload(bash_bin, payload, instance, base_url)
        kept = (instance / "control" / "amp_bootstrap_start.sh").read_text(encoding="utf-8")
        if "empty download" not in proc.stderr:
            fail("empty download was not rejected")
        elif kept != EXISTING_BOOTSTRAP:
            fail("empty download overwrote the existing control bootstrap")
        elif "EXISTING_BOOTSTRAP_RAN" not in proc.stdout or proc.returncode != 0:
            fail("empty download did not fall back to the existing control bootstrap")
        else:
            ok("empty downloads rejected; existing control bootstrap reused")

    # 5. No usable control file: fall back to the installed release start script.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        remote = prepare_remote(root, STUB_BOOTSTRAP + "echo PWNED\n", STUB_DEPLOY)
        instance = root / "instance"
        write_lf(instance / "current" / "scripts" / "amp_start.sh", EXISTING_CURRENT_START)
        with _serve_directory(remote) as base_url:
            proc = _run_payload(bash_bin, payload, instance, base_url)
        if proc.returncode != 0 or "CURRENT_START_RAN" not in proc.stdout:
            fail("unverified download did not fall back to current/scripts/amp_start.sh")
        elif (instance / "control" / "amp_bootstrap_start.sh").exists():
            fail("unverified bootstrap bytes were written to control/")
        else:
            ok("unverified download falls back to current/scripts/amp_start.sh")

    # 6. Nothing verified and nothing on disk: fail loudly instead of guessing.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        remote = prepare_remote(root, STUB_BOOTSTRAP + "echo PWNED\n", STUB_DEPLOY)
        instance = root / "instance"
        instance.mkdir()
        with _serve_directory(remote) as base_url:
            proc = _run_payload(bash_bin, payload, instance, base_url)
        if proc.returncode == 0:
            fail("installer exited 0 with no verified bootstrap and no current release")
        elif "ERROR" not in proc.stderr:
            fail("installer failed without an explanatory error")
        elif (instance / "control" / "amp_bootstrap_start.sh").exists():
            fail("unverified bootstrap bytes were written to control/")
        else:
            ok("installer fails closed when nothing can be verified or reused")

    # 7. A zero-byte control/amp_bootstrap_start.sh must never be executed.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        remote = prepare_remote(root, STUB_BOOTSTRAP + "echo PWNED\n", STUB_DEPLOY)
        instance = root / "instance"
        write_lf(instance / "control" / "amp_bootstrap_start.sh", "")
        with _serve_directory(remote) as base_url:
            proc = _run_payload(bash_bin, payload, instance, base_url)
        if proc.returncode == 0:
            fail("installer treated an empty control/amp_bootstrap_start.sh as usable")
        elif "ERROR" not in proc.stderr:
            fail("empty existing bootstrap failed without an explanatory error")
        else:
            ok("empty existing control/amp_bootstrap_start.sh is refused, not executed")

    # 8. Unreachable raw host: reuse what is already installed.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        instance = root / "instance"
        write_lf(instance / "control" / "amp_bootstrap_start.sh", EXISTING_BOOTSTRAP)
        proc = _run_payload(
            bash_bin, payload, instance, f"http://127.0.0.1:{_free_port()}/control"
        )
        if proc.returncode != 0 or "EXISTING_BOOTSTRAP_RAN" not in proc.stdout:
            fail("unreachable download host did not reuse the existing control bootstrap")
        else:
            ok("unreachable download host reuses the existing control bootstrap")

    # 9. The exact AMP wrapper argument must decode and run end to end, not just the
    # already-decoded form. The wrapper is fed through a launcher file because Windows
    # argv transport corrupts the base64 blob; AMP's own argv handling is covered by the
    # space-split and no-literal-spaces checks above.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        instance = root / "instance"
        write_lf(instance / "control" / "amp_bootstrap_start.sh", EXISTING_BOOTSTRAP)
        launcher = root / "amp_start_command.sh"
        write_lf(launcher, f"{wrapper_arg}\n")
        proc = _run_bash(
            [bash_bin, str(launcher)], instance, f"http://127.0.0.1:{_free_port()}/control"
        )
        combined = f"{proc.stdout}\n{proc.stderr}"
        if "unexpected EOF while looking for matching" in combined:
            fail("AMP-split start args still trigger unmatched quote error in bash")
        elif "syntax error" in combined.lower():
            fail(f"AMP-split start args fail bash parse: {combined.strip()}")
        elif "EXISTING_BOOTSTRAP_RAN" not in proc.stdout:
            fail("AMP wrapper argument did not run the installer to completion")
        else:
            ok("AMP wrapper argument decodes and runs the installer end to end")


def validate_amp_safe_start_command(cmd_line: str, decoded_installer: str) -> None:
    if "-lc 'set -e" in cmd_line or '-lc "set -e' in cmd_line:
        fail("App.CommandLineArgs must not use old quoted inline installer pattern")

    if re.search(r"-lc\s+['\"]", cmd_line):
        fail("App.CommandLineArgs must not wrap inline script in outer shell quotes")
    else:
        ok("start command avoids outer shell quotes")

    if "eval${IFS}$(printf${IFS}%s${IFS}" not in cmd_line or "|base64${IFS}-d)" not in cmd_line:
        fail("start command must use base64 eval wrapper without literal spaces")
    else:
        ok("start command uses base64 eval wrapper")

    args = simulate_amp_arg_split(cmd_line)
    if len(args) != 2 or args[0] != "-lc":
        fail(f"AMP space split must yield exactly [-lc, wrapper], got {args!r}")
        return
    ok("AMP space split yields bash -lc plus one wrapper argument")

    if " " in args[1]:
        fail("base64 wrapper argument must not contain literal spaces")
    else:
        ok("wrapper argument contains no literal spaces")

    raw_base = "raw.githubusercontent.com/carthorsestudios/scratch-mmo-amp-template/"
    for needle in (
        raw_base,
        "amp_bootstrap_start.sh",
        "scratch_mmo_deploy_latest.py",
        "curl -fsSL",
        "wget -qO",
        "current/scripts/amp_start.sh",
        "control/amp_bootstrap_start.sh",
        "control/scratch_mmo_deploy_latest.py",
        "BOOTSTRAP_SHA256",
        "DEPLOY_SHA256",
    ):
        if needle not in decoded_installer:
            fail(f"decoded inline installer missing {needle!r}")
        else:
            ok(f"decoded inline installer includes {needle}")

    bash_bin = find_bash()
    if bash_bin is None:
        ok("bash not available locally; skipping AMP-split bash syntax probe")
    else:
        problem = bash_syntax_error(bash_bin, decoded_installer)
        if problem:
            fail(f"decoded installer fails bash -n: {problem}")
        else:
            ok("decoded installer passes bash -n syntax check")

    validate_installer_behaviour(decoded_installer, args[1])


def validate_kvp_and_config() -> None:
    kvp_path = ROOT / "scratchmmo.kvp"
    config_path = ROOT / "scratchmmoconfig.json"
    if not kvp_path.is_file() or not config_path.is_file():
        return

    kvp = read_text(kvp_path)
    config = json.loads(read_text(config_path))

    cmd_match = re.search(r"App\.CommandLineArgs=(.*)", kvp)
    if not cmd_match:
        fail("App.CommandLineArgs missing from scratchmmo.kvp")
        return

    cmd_line = cmd_match.group(1).strip()
    expected = build_start_command_args()
    if cmd_line != expected:
        fail("App.CommandLineArgs does not match tools/emit_start_command.py")
    else:
        ok("start command matches inline installer definition")

    if cmd_line == "control/amp_bootstrap_start.sh":
        fail("start command must not assume control/amp_bootstrap_start.sh already exists")
    elif "-lc" not in cmd_line:
        fail("start command must use bash -lc inline installer")
    else:
        ok("start command uses inline bash installer")

    validate_installer_regeneration(cmd_line)

    try:
        decoded_installer = decode_installer_from_start_args(cmd_line)
    except ValueError as exc:
        fail(str(exc))
        return

    validate_bootstrap_pins(decoded_installer)
    validate_amp_safe_start_command(cmd_line, decoded_installer)

    if "{{GitHubToken}}" in cmd_line or "SCRATCH_GITHUB_TOKEN" in cmd_line:
        fail("start command must not reference GitHub token")
    else:
        ok("start command does not reference GitHub token")

    if "App.ExecutableLinux=/bin/bash" not in kvp:
        fail("Linux executable must remain /bin/bash")
    else:
        ok("Linux executable is /bin/bash")

    if "Console.AppReadyRegex=" not in kvp:
        fail("Console.AppReadyRegex missing from scratchmmo.kvp")
    else:
        ready_match = re.search(r"Console\.AppReadyRegex=(.*)", kvp)
        if not ready_match:
            fail("Console.AppReadyRegex missing from scratchmmo.kvp")
        else:
            ready_regex = ready_match.group(1).strip()
            if "Ready web=" not in ready_regex or "ws_target" not in ready_regex:
                fail("AppReadyRegex must accept explicit ScratchMMO ready line")
            elif "Setup server listening port=" not in ready_regex:
                fail("AppReadyRegex must accept setup server ready line")
            else:
                ok("AppReadyRegex accepts ScratchMMO ready or setup server ready lines")

            try:
                ready_pattern = re.compile(ready_regex)
            except re.error as exc:
                fail(f"AppReadyRegex is not valid regex: {exc}")
            else:
                for sample in (
                    "[ScratchMMO] Ready web=9090 ws_target=127.0.0.1:19080",
                    "[ScratchMMO] Setup server listening port=9090",
                ):
                    if not ready_pattern.fullmatch(sample):
                        fail(f"AppReadyRegex does not match sample ready line: {sample!r}")
                # Ready must only be signalled after health/setup succeeds, so deploy
                # progress and supervision chatter must never satisfy the regex.
                for rejected in (
                    "[GameServer] WebSocket listening port=19080",
                    "[2026-06-19T19:00:00Z] Godot WebSocket is listening on 127.0.0.1:19080",
                    "[ScratchMMO] Release triple verified: channel=main build=42 commit=abc1234",
                    "[ScratchMMO] Handing off to release engine: --deploy",
                    "[ScratchMMO] Deploying candidate release",
                    "[ScratchMMO] Health check failed; rolling back",
                    "[ScratchMMO] Setup server listening port=",
                    "[ScratchMMO] Ready web=9090",
                ):
                    if ready_pattern.fullmatch(rejected):
                        fail(f"AppReadyRegex must not match non-ready line: {rejected!r}")
                ok("AppReadyRegex matches ready/setup lines and rejects deploy progress lines")

    env_match = re.search(r"App\.EnvironmentVariables=(\{.*\})", kvp)
    if not env_match:
        fail("App.EnvironmentVariables missing from scratchmmo.kvp")
        return

    env_json = json.loads(env_match.group(1))
    required_env = {
        "SCRATCH_GITHUB_TOKEN": "{{GitHubToken}}",
        "SCRATCH_GITHUB_OWNER": "carthorsestudios",
        "SCRATCH_GITHUB_REPO": "scratch-mmo",
        "SCRATCH_HEALTH_URL": "http://127.0.0.1:9090/healthz",
        "SCRATCH_VERSION_URL": "http://127.0.0.1:9090/version",
        "SCRATCH_RELEASE_TAG": "{{ReleaseTagOverride}}",
        "SCRATCH_MMO_INVITE_CODE": "{{InviteCode}}",
        "SCRATCH_ALLOWED_ORIGINS": "{{AllowedWebOrigins}}",
        "SCRATCH_REGISTRATION": "{{RegistrationMode}}",
        "SCRATCH_SERVER_PORT": "{{$ServerPort}}",
        "SCRATCH_WEB_PORT": "{{$WebPort}}",
        "SCRATCH_BIND_ADDRESS": "{{BindAddress}}",
    }
    for key, expected_env in required_env.items():
        if env_json.get(key) != expected_env:
            fail(f"Environment mapping mismatch for {key}")
        else:
            ok(f"environment maps {key}")

    if "SCRATCH_INVITE_CODE" in env_json:
        fail("Legacy SCRATCH_INVITE_CODE must not remain; use SCRATCH_MMO_INVITE_CODE")
    else:
        ok("legacy SCRATCH_INVITE_CODE env mapping removed")

    if "{{GitHubToken}}" in kvp and cmd_match:
        if "GitHubToken" in cmd_line:
            fail("GitHubToken must not appear in command line args")
        else:
            ok("GitHubToken excluded from command line args")

    github_field = find_config_field(config, "GitHubToken")
    if github_field is None:
        fail("Missing GitHubToken config field")
    else:
        if github_field.get("InputType") != "password":
            fail("GitHubToken must use InputType password")
        elif github_field.get("IncludeInCommandLine") is not False:
            fail("GitHubToken must have IncludeInCommandLine false")
        elif github_field.get("ParamFieldName"):
            fail("GitHubToken must not set ParamFieldName (no CLI mapping)")
        elif github_field.get("SkipIfEmpty") is not True:
            fail("GitHubToken must have SkipIfEmpty true")
        else:
            ok("GitHubToken is password/masked and not on command line")

    invite_field = find_config_field(config, "InviteCode")
    if invite_field is None:
        fail("Missing InviteCode config field")
    elif invite_field.get("FieldName") == "GitHubToken":
        fail("InviteCode must not be reused for GitHubToken")
    else:
        ok("InviteCode remains separate from GitHubToken")
        if invite_field.get("InputType") != "password":
            fail("InviteCode must use InputType password")
        elif invite_field.get("IncludeInCommandLine") is not False:
            fail("InviteCode must have IncludeInCommandLine false")
        elif invite_field.get("ParamFieldName"):
            fail("InviteCode must not set ParamFieldName (no CLI mapping)")
        elif invite_field.get("SkipIfEmpty") is not True:
            fail("InviteCode must have SkipIfEmpty true")
        else:
            ok("InviteCode is password/masked and environment-only")

        invite_desc = str(invite_field.get("Description", ""))
        if "SCRATCH_MMO_INVITE_CODE" not in invite_desc:
            fail("InviteCode description must mention SCRATCH_MMO_INVITE_CODE")
        elif "--invite-code=" in invite_desc or "as --invite-code" in invite_desc.lower():
            fail("InviteCode description must not instruct operators to use --invite-code=")
        else:
            ok("InviteCode description documents SCRATCH_MMO_INVITE_CODE")

    # Prove rendered launch config: registration stays available via env; invite never on CLI.
    sentinel = "PASS1E3A2_INVITE_SENTINEL_NOT_FOR_LOGS"
    rendered_cli_parts: list[str] = []
    for entry in config:
        if entry.get("IncludeInCommandLine") is not True:
            continue
        param = str(entry.get("ParamFieldName") or "").strip()
        if not param:
            continue
        field = str(entry.get("FieldName") or "")
        value = sentinel if field == "InviteCode" else str(entry.get("DefaultValue") or "x")
        rendered_cli_parts.append(f"--{param}={value}")
    rendered_cli = " ".join(rendered_cli_parts)
    if "invite-code" in rendered_cli.lower() or sentinel in rendered_cli:
        fail(f"Rendered command line must not include invite code; got: {rendered_cli!r}")
    else:
        ok("rendered command line excludes invite code")

    if env_json.get("SCRATCH_REGISTRATION") != "{{RegistrationMode}}":
        fail("RegistrationMode must map to SCRATCH_REGISTRATION")
    else:
        ok("RegistrationMode maps to SCRATCH_REGISTRATION (not invite secret)")

    version_match = re.search(r"Meta\.ConfigVersion=(\d+)", kvp)
    if not version_match:
        fail("Meta.ConfigVersion missing from scratchmmo.kvp")
    elif int(version_match.group(1)) < REQUIRED_CONFIG_VERSION:
        fail(
            f"Meta.ConfigVersion must be >= {REQUIRED_CONFIG_VERSION} after the pinned "
            "bootstrap / release-engine handoff change"
        )
    else:
        ok(f"Meta.ConfigVersion={version_match.group(1)}")

    override_field = find_config_field(config, "ReleaseTagOverride")
    if override_field is None:
        fail("Missing ReleaseTagOverride config field")
    elif override_field.get("IncludeInCommandLine") is not False:
        fail("ReleaseTagOverride must have IncludeInCommandLine false")
    else:
        ok("ReleaseTagOverride is optional and not on command line")

    origins_field = find_config_field(config, "AllowedWebOrigins")
    if origins_field is None:
        fail("Missing AllowedWebOrigins config field")
    elif origins_field.get("IncludeInCommandLine") is not False:
        fail("AllowedWebOrigins must have IncludeInCommandLine false")
    elif origins_field.get("Hidden") is True:
        fail("AllowedWebOrigins must be visible in AMP configuration")
    else:
        ok("AllowedWebOrigins is visible and not on command line")

    # Scan template manifests for active --invite-code= CLI mappings (not docs
    # that only say the legacy flag is forbidden).
    banned_cli = re.compile(
        r"--invite-code=\{\{?InviteCode\}?\}|--invite-code=\{InviteCode\}",
        re.IGNORECASE,
    )
    for rel in ("scratchmmoconfig.json", "scratchmmo.kvp"):
        text = read_text(ROOT / rel)
        if banned_cli.search(text) or re.search(r'"ParamFieldName"\s*:\s*"invite-code"', text):
            fail(f"Active --invite-code CLI mapping still present in {rel}")
        if "invite-code" in text.lower() and rel == "scratchmmo.kvp":
            fail(f"{rel} must not contain invite-code CLI fragments")
    ok("no active --invite-code CLI mapping in template manifests")


def validate_deploy_shim() -> None:
    """control/scratch_mmo_deploy_latest.py must be a verify-then-handoff shim only."""
    deploy_py = ROOT / "control" / "scratch_mmo_deploy_latest.py"
    if not deploy_py.is_file():
        return
    text = read_text(deploy_py)

    for needle, label in [
        ("deployment/amp/amp_release_updater.py", "release-bundled engine path"),
        ("MissingEngineError", "missing-engine guard"),
        ("verify_release_triple", "release triple verification"),
        ("checksums.sha256", "checksum asset verification"),
        ("byte-identical", "external asset / ZIP member equality check"),
        ("assert_no_secrets_in_argv", "secret-in-argv guard"),
        ("--check-only", "check-only mode"),
        ("--dry-run", "dry-run mode"),
        ("--supervise", "supervised handoff mode"),
        ("SCRATCH_GITHUB_TOKEN", "token env key"),
    ]:
        if needle not in text:
            fail(f"deploy shim missing {label}")
        else:
            ok(f"deploy shim supports {label}")

    # No independent deploy engine: the shim must not rename/replace current/ itself.
    for pattern, label in [
        (r"swap_current", "legacy swap_current engine"),
        (r"deploy_root\s*/\s*[\"']previous[\"']", "previous/ backup rename engine"),
        (r"\bshutil\b", "shutil tree operations"),
        (r"\brmtree\b", "recursive delete"),
        (r"os\.rename\(", "directory rename"),
        (r"[\"']current[\"']\s*\)?\s*\.\s*rename", "current/ rename"),
    ]:
        if re.search(pattern, text):
            fail(f"deploy shim must not implement its own {label}")
        else:
            ok(f"deploy shim has no {label}")

    if re.search(r"os\.replace\([^)]*current", text):
        fail("deploy shim must not replace current/ itself; the release engine owns that")
    else:
        ok("deploy shim leaves current/ swapping to the release engine")

    token_print = re.compile(
        r"(?:print|log)\s*\(\s*(?:config\[[\'\"](?:github_)?token[\'\"]\]|token)\s*\)"
    )
    if token_print.search(text):
        fail("deploy shim must not print token values")
    else:
        ok("deploy shim does not print token values")

    if "register_secret" not in text or "redact(" not in text:
        fail("deploy shim must redact secrets from log output")
    else:
        ok("deploy shim redacts secrets from log output")


def validate_deploy_shim_behaviour() -> None:
    """Run the shim offline to prove it refuses to act without a verified release."""
    deploy_py = ROOT / "control" / "scratch_mmo_deploy_latest.py"
    if not deploy_py.is_file():
        return

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        # Nest one level so the shim's repo-checkout probe resolves inside the sandbox.
        instance = Path(tmp) / "root" / "instance"
        (instance / "control").mkdir(parents=True)
        shim = instance / "control" / "scratch_mmo_deploy_latest.py"
        shim.write_bytes(deploy_py.read_bytes())
        sentinel = "#!/usr/bin/env bash\necho ORIGINAL_RELEASE\n"
        write_lf(instance / "current" / "scripts" / "amp_start.sh", sentinel)

        env = os.environ.copy()
        for key in ("SCRATCH_GITHUB_TOKEN", "GITHUB_TOKEN", "SCRATCH_RELEASE_TAG"):
            env.pop(key, None)

        proc = subprocess.run(
            [sys.executable, str(shim), "--deploy", "--yes", "--deploy-root", str(instance)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120,
            check=False,
        )
        combined = f"{proc.stdout}\n{proc.stderr}"
        if proc.returncode == 0:
            fail("deploy shim exited 0 without a token or verified release")
        elif "SCRATCH_GITHUB_TOKEN is required" not in combined:
            fail(f"deploy shim gave no token guidance without a token: {combined.strip()[:300]}")
        elif (instance / "previous").exists() or (instance / "incoming").exists():
            fail("deploy shim created deploy-swap directories without a verified release")
        elif (instance / "current" / "scripts" / "amp_start.sh").read_text(
            encoding="utf-8"
        ) != sentinel:
            fail("deploy shim mutated current/ without a verified release")
        else:
            ok("deploy shim fails closed with no token and leaves current/ untouched")

        env_with_token = env.copy()
        env_with_token["SCRATCH_GITHUB_TOKEN"] = "not-a-real-token-value-1234567890"
        proc = subprocess.run(
            [
                sys.executable,
                str(shim),
                "--deploy",
                "--yes",
                "--deploy-root",
                str(instance),
                "--tag",
                env_with_token["SCRATCH_GITHUB_TOKEN"],
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env_with_token,
            timeout=120,
            check=False,
        )
        combined = f"{proc.stdout}\n{proc.stderr}"
        if proc.returncode == 0 or "Refusing to run" not in combined:
            fail("deploy shim accepted a secret env value on the command line")
        elif env_with_token["SCRATCH_GITHUB_TOKEN"] in combined:
            fail("deploy shim echoed the token value in its output")
        else:
            ok("deploy shim refuses secrets passed on the command line and redacts them")

        proc = subprocess.run(
            [sys.executable, str(shim), "--check-only", "--deploy"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120,
            check=False,
        )
        if proc.returncode == 0:
            fail("deploy shim accepted mutually exclusive modes")
        else:
            ok("deploy shim rejects mutually exclusive modes")


def validate_documentation() -> None:
    readme = ROOT / "README.md"
    if not readme.is_file():
        fail("Missing README.md")
        return
    text = read_text(readme)

    for needle, label in [
        (f"Meta.ConfigVersion={REQUIRED_CONFIG_VERSION}", "current ConfigVersion"),
        ("SHA-256", "bootstrap checksum pinning"),
        ("tools/generate_bootstrap_pins.py", "pin generator"),
        ("tools/emit_start_command.py --write-kvp", "kvp regeneration command"),
        ("template refresh", "template refresh migration note"),
        ("Restart", "one-click Restart flow"),
    ]:
        if needle not in text:
            fail(f"README must document {label} ({needle!r})")
        else:
            ok(f"README documents {label}")

    if not re.search(r"rollback[^.\n]{0,80}automatic|automatic[^.\n]{0,80}rollback", text, re.IGNORECASE):
        fail("README must state that rollback of an unhealthy candidate is automatic")
    else:
        ok("README documents automatic rollback of unhealthy candidates")

    if not re.search(r"no routine (?:ssh|shell)", text, re.IGNORECASE):
        fail("README must state that no routine SSH/systemd access is needed")
    else:
        ok("README documents that routine SSH/systemd access is not needed")

    if not re.search(r"systemd", text, re.IGNORECASE):
        fail("README must mention systemd is not part of the AMP path")
    else:
        ok("README addresses systemd expectations")

    if not re.search(r"(?:do not expose|must not expose|internal)", text, re.IGNORECASE):
        fail("README must warn against exposing AMP admin or 19080")
    else:
        ok("README warns against exposing AMP admin / 19080")

    # The README must not promise gameplay content or a bundled release.
    if re.search(r"\bmmo_release\.zip\b.*\bcommitted\b", text, re.IGNORECASE):
        fail("README must not claim a release zip is committed here")
    else:
        ok("README keeps the repo free of release payload claims")


def validate_no_secrets_or_banned_artifacts() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if tracked.returncode != 0:
        fail("git ls-files failed; cannot verify tracked artifacts")
        return

    real_token = re.compile(
        r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{20,})\b"
    )
    tunnel_token = re.compile(
        r"(?:TUNNEL_TOKEN|tunnel\s+token|cloudflared\s+(?:tunnel\s+)?run\s+--token)\s*[=:]\s*['\"]?[A-Za-z0-9_-]{20,}",
        re.IGNORECASE,
    )
    expose_19080 = re.compile(
        r"(?:expose|forward|publish|route|open|map).{0,40}\b19080\b",
        re.IGNORECASE,
    )
    safe_19080 = re.compile(
        r"(?:never|do not|don't|must not|not expose|internal only|blocked|localhost|127\.0\.0\.1)",
        re.IGNORECASE,
    )

    section_errors_before = len(errors)
    for raw in tracked.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="replace")
        path = ROOT / rel
        if path.suffix.lower() == ".zip":
            fail(f"Tracked release zip must not be committed: {rel}")
            continue
        try:
            text = read_text(path)
        except OSError:
            continue
        if real_token.search(text):
            fail(f"Real-looking GitHub token committed in {rel}")
        if tunnel_token.search(text):
            fail(f"Tunnel token example committed in {rel}")
        if rel.endswith(".md") or rel.endswith(".kvp") or rel.endswith(".json"):
            for match in expose_19080.finditer(text):
                start = max(0, match.start() - 120)
                context = text[start : match.end() + 40]
                if safe_19080.search(context):
                    continue
                fail(f"Doc may instruct public exposure of port 19080: {rel}")
                break

    if len(errors) == section_errors_before:
        ok("no tracked zip/token secrets and docs avoid public 19080 exposure")

    gameplay = [
        rel
        for rel in (
            raw.decode("utf-8", errors="replace")
            for raw in tracked.stdout.split(b"\0")
            if raw
        )
        if Path(rel).suffix.lower() in {".gd", ".tscn", ".tres", ".pck", ".godot", ".x86_64"}
    ]
    if gameplay:
        fail(f"Gameplay/engine artifacts must not be committed here: {gameplay[:5]}")
    else:
        ok("no gameplay or engine artifacts tracked in the template repo")


def main() -> int:
    print("Validating Scratch MMO AMP template")
    print(f"Root: {ROOT}")
    print()

    validate_json_files()
    print()
    validate_kvp_json_consistency()
    print()
    validate_control_files()
    print()
    validate_kvp_and_config()
    print()
    validate_deploy_shim()
    print()
    validate_deploy_shim_behaviour()
    print()
    validate_documentation()
    print()
    validate_no_secrets_or_banned_artifacts()
    print()

    if errors:
        print(f"Template validation failed with {len(errors)} error(s).")
        return 1

    print("Template validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
