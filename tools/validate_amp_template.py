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
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
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

REQUIRED_CONFIG_VERSION = 6

# Fault points the inline installer honours through SCRATCH_INSTALL_FAULT. Each one
# must leave the *complete* previous control pair behind, never a mixed pair.
PAIR_FAULT_POINTS = (
    "before_first_replace",
    "during_first_replace",
    "after_first_replace",
    "during_second_replace",
    "during_metadata",
    "during_cleanup",
)

# Fault points that abort mid-install *and* break a specific step of the rollback, so
# the installer has to decide what is safe to run with an unproven control directory.
RESTORE_FAULT_POINTS = (
    "restore_first",
    "restore_second",
    "restore_mode",
    "restore_digest",
    "restore_cleanup",
)

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

    # The supervised handoff must replace this process. A pipeline (`| tee`) would make
    # AMP's direct child a pipeline member, so SIGTERM would never reach the Python
    # supervisor that owns current/scripts/amp_start.sh.
    if re.search(r"\|\s*tee\b", text):
        fail("bootstrap must not pipe the supervised handoff through tee")
    else:
        ok("bootstrap uses no tee pipeline")

    supervised_exec = re.search(
        r"^\s*exec\s+python3\s+-u\s+\"\$\{DEPLOY_SCRIPT\}\"\s+--deploy\s+--supervise\s+--yes\s*$",
        text,
        re.MULTILINE,
    )
    if not supervised_exec:
        fail("bootstrap must exec python3 into the shim on the supervised path")
    else:
        ok("bootstrap execs python3 into the shim (process identity preserved)")

    # Nothing may run after the exec on the supervised path: no relaunch of the game,
    # no second supervisor.
    after_exec = text[supervised_exec.end() :]
    if re.search(r"UPDATER_EXIT|Supervised release engine exited cleanly", text):
        fail("bootstrap still carries the pre-exec supervised return path")
    elif re.search(r"^\s*exec\s+python3\b", after_exec, re.MULTILINE):
        fail("bootstrap execs the supervisor more than once")
    else:
        ok("bootstrap has no post-handoff relaunch path")

    if "SCRATCH_BOOTSTRAP_LOG_FILE" not in text:
        fail("bootstrap must pass its log file to the Python supervisor for dual logging")
    else:
        ok("bootstrap hands its log file to Python dual logging instead of tee")

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

    # Installation must only happen inside the verified branch: the authoritative
    # mutation has to be textually dominated by both fetch_verified calls.
    verify_pos = installer.rfind("fetch_verified \"$BASE/scratch_mmo_deploy_latest.py\"")
    install_pos = installer.rfind("install_control_pair \"$TMP_BOOTSTRAP\" \"$TMP_DEPLOY\"")
    if verify_pos < 0 or install_pos < 0:
        fail("installer must fetch-verify both files, then call install_control_pair")
    elif install_pos < verify_pos:
        fail("installer installs the control pair before verifying both digests")
    else:
        ok("installer installs the control pair only after both digests verify")

    if re.search(r"-o\s+control/(?:amp_bootstrap_start\.sh|scratch_mmo_deploy_latest\.py)", installer):
        fail("installer must not download directly onto control/ files")
    else:
        ok("installer downloads to temp files, never straight onto control/")

    validate_pair_installer_structure(installer)


def _shell_function_body(text: str, name: str) -> str | None:
    start = text.find(f"\n{name}() {{\n")
    if start < 0:
        return None
    end = text.find("\n}\n", start)
    if end < 0:
        return None
    return text[start:end]


def validate_pair_installer_structure(installer: str) -> None:
    """The control pair must be replaced as one unit, with a restorable snapshot."""
    for name in (
        "install_control_pair",
        "snapshot_control_pair",
        "restore_control_pair",
        "restore_one",
        "verify_backup",
        "verify_installed",
        "verify_snapshot_state",
        "verify_restored_state",
        "retire_backup",
        "abandon_snapshot",
        "apply_mode",
        "refuse_unsafe_control_path",
        "current_start_is_safe",
        "bootstrap_is_executable",
        "secure_control_dir",
        "link_count_of",
    ):
        if _shell_function_body(installer, name) is None:
            fail(f"installer must define {name}()")
        else:
            ok(f"installer defines {name}()")

    body = _shell_function_body(installer, "install_control_pair")
    if body is None:
        return

    order = {
        "mode": body.find('apply_mode "$tmp_bootstrap"'),
        "snapshot": body.find("snapshot_control_pair"),
        "first_mv": body.find('mv -f "$tmp_bootstrap"'),
        "second_mv": body.find('mv -f "$tmp_deploy"'),
        "cleanup": body.find('retire_backup "$PAIR_BACKUP_BOOTSTRAP"'),
    }
    missing = sorted(key for key, pos in order.items() if pos < 0)
    if missing:
        fail(f"install_control_pair is missing required steps: {missing}")
        return
    if not (
        order["mode"] < order["snapshot"] < order["first_mv"] < order["second_mv"] < order["cleanup"]
    ):
        fail(
            "install_control_pair must verify temp modes, snapshot the previous pair, "
            "replace both files, and only then clean up"
        )
    else:
        ok("install_control_pair verifies modes, snapshots, replaces the pair, then cleans up")

    if body.find("refuse_unsafe_control_path") > order["snapshot"]:
        fail("install_control_pair must reject unsafe control paths before snapshotting")
    else:
        ok("install_control_pair rejects symlinked/non-regular control paths first")

    # Every step that can fail after the first authoritative replacement must roll the
    # complete previous pair back.
    tail = body[order["first_mv"] :]
    rollback_steps = re.findall(r"\|\|\s*\{[^}]*\}", tail)
    unguarded = [
        step for step in rollback_steps if "restore_control_pair" not in step and "return 1" in step
    ]
    if unguarded:
        fail(f"failure paths after the first replacement do not restore the pair: {unguarded[:2]}")
    elif len(rollback_steps) < 4:
        fail("install_control_pair has too few guarded failure paths after the first replacement")
    else:
        ok("every failure path after the first replacement restores the previous pair")

    verify_pos = body.find("verify_installed control/amp_bootstrap_start.sh")
    if 'retire_backup "$PAIR_BACKUP_BOOTSTRAP"' in body[: order["second_mv"]]:
        fail("install_control_pair retires the snapshot before both files are installed")
    elif verify_pos < 0:
        fail("install_control_pair must re-verify the installed bootstrap against its pin")
    elif verify_pos > order["cleanup"]:
        fail("install_control_pair retires the snapshot before re-verifying the installed pair")
    else:
        ok("snapshot backups survive until the installed pair is re-verified against its pins")

    for needle, label in [
        ("mktemp control/.bak-", "private snapshot paths inside control/"),
        ("mktemp control/.tmp-", "private download temp paths inside control/"),
        ("refusing symlinked control directory", "symlinked control/ refusal"),
        ("refusing symlinked control file", "symlinked control file refusal"),
        ("refusing non-regular control file", "non-regular control file refusal"),
        ("refusing hard-linked control file", "hard-linked control file refusal"),
        ("mode verification failed", "mode verification"),
        ("SCRATCH_INSTALL_FAULT", "rollback fault-injection seam"),
    ]:
        if needle not in installer:
            fail(f"installer missing {label} ({needle!r})")
        else:
            ok(f"installer implements {label}")

    # The fault seam must only ever abort; it can never select what gets installed.
    fault_stop = _shell_function_body(installer, "fault_stop") or ""
    if "return 1" not in fault_stop or re.search(r"\b(curl|wget|mv|chmod|exec)\b", fault_stop):
        fail("fault_stop() must only abort, never perform install work")
    else:
        ok("fault-injection seam can only abort an install, never install bytes")

    exec_pos = installer.rfind("exec /bin/bash control/amp_bootstrap_start.sh")
    if exec_pos < installer.rfind("install_ok=1"):
        fail("installer execs the bootstrap before the pair install is confirmed")
    else:
        ok("installer execs the bootstrap only after the pair install is confirmed")

    if re.search(r"^\s*exec\s+/bin/bash\s+control/amp_bootstrap_start\.sh", installer, re.MULTILINE):
        ok("installer execs control/amp_bootstrap_start.sh")
    else:
        fail("installer must exec control/amp_bootstrap_start.sh")

    validate_restore_structure(installer)


def validate_restore_structure(installer: str) -> None:
    """Rollback must be verified, not merely attempted, before anything is executed."""
    missing_faults = [
        name
        for name in (*RESTORE_FAULT_POINTS, "control_dir_mode")
        if name not in installer
    ]
    if missing_faults:
        fail(f"installer does not honour restoration fault points: {missing_faults}")
    else:
        ok("installer honours every restoration and control-directory fault-injection point")

    snapshot = _shell_function_body(installer, "snapshot_control_pair") or ""
    snap_order = {
        "digest": snapshot.find("PAIR_DIGEST_BOOTSTRAP=$(digest_of"),
        "backup": snapshot.find("mktemp control/.bak-amp_bootstrap_start.sh"),
        "copy": snapshot.find('cat control/amp_bootstrap_start.sh > "$PAIR_BACKUP_BOOTSTRAP"'),
        "verify": snapshot.find('verify_backup "$PAIR_BACKUP_BOOTSTRAP"'),
    }
    missing = sorted(key for key, pos in snap_order.items() if pos < 0)
    if missing:
        fail(f"snapshot_control_pair is missing required steps: {missing}")
    elif not (
        snap_order["digest"] < snap_order["backup"] < snap_order["copy"] < snap_order["verify"]
    ):
        fail(
            "snapshot_control_pair must record the digest, copy the file into a private "
            "backup, and prove the backup matches that digest, in that order"
        )
    else:
        ok("snapshot_control_pair records presence + digest and proves each backup copy")

    if 'verify_backup "$PAIR_BACKUP_DEPLOY"' not in snapshot:
        fail("snapshot_control_pair must prove the shim backup too, not just the bootstrap")
    elif "PAIR_DIGEST_DEPLOY=$(digest_of" not in snapshot:
        fail("snapshot_control_pair must record the shim's pre-install digest")
    else:
        ok("snapshot_control_pair snapshots and proves both halves of the pair")

    restore_one = _shell_function_body(installer, "restore_one") or ""
    if 'mv -f "$src"' in restore_one or 'mv -f "$backup"' in restore_one:
        fail("restore_one must copy the backup, not move it; the backup has to outlive the restore")
    elif 'cat "$src" > "$dest"' not in restore_one:
        fail("restore_one must restore content from the private snapshot copy")
    elif "apply_mode" not in restore_one:
        fail("restore_one must reapply and verify the required mode")
    elif "digest_of" not in restore_one or "does not match its snapshot digest" not in restore_one:
        fail("restore_one must re-hash the restored file and compare it to the snapshot digest")
    elif 'rm -f "$dest"' not in restore_one:
        fail("restore_one must restore prior absence by removing the file again")
    else:
        ok("restore_one restores presence/absence, reapplies modes, and re-hashes the result")

    restore_pair = _shell_function_body(installer, "restore_control_pair") or ""
    verified_pos = restore_pair.find("RESTORE_VERIFIED=1")
    proof_pos = restore_pair.find("verify_restored_state")
    retire_pos = restore_pair.find("retire_backup")
    if verified_pos < 0 or proof_pos < 0 or retire_pos < 0:
        fail("restore_control_pair must prove the restored state before retiring the backups")
    elif not (proof_pos < verified_pos < retire_pos):
        fail(
            "restore_control_pair must re-verify the whole pair, then mark the restoration "
            "verified, and only then retire the snapshots"
        )
    elif restore_pair.count("restore_one ") != 2:
        fail("restore_control_pair must restore both authoritative files")
    elif "return 1" not in restore_pair:
        fail("restore_control_pair must return nonzero when a restoration operation fails")
    else:
        ok("restore_control_pair verifies both restored files before releasing the snapshots")

    verify_state = _shell_function_body(installer, "verify_snapshot_state") or ""
    if "was absent before this run" not in verify_state:
        fail("verify_snapshot_state must prove a previously absent file is absent again")
    elif "pre-install snapshot digest" not in verify_state:
        fail("verify_snapshot_state must prove a restored file matches its snapshot digest")
    else:
        ok("verify_snapshot_state proves restored presence/absence and digest against the snapshot")

    # An injected fault may only ever make verification fail.
    fault_digest = _shell_function_body(installer, "fault_digest") or ""
    if "NULL_DIGEST" not in fault_digest or re.search(r"\b(curl|wget|mv|chmod|exec|cat)\b", fault_digest):
        fail("fault_digest() must only substitute an unreachable digest, never install work")
    else:
        ok("digest fault seam can only make a comparison fail, never pass")

    retire = _shell_function_body(installer, "retire_backup") or ""
    if "WARNING" not in retire or "return 1" not in retire:
        fail("retire_backup must warn and report failure instead of silently dropping a backup")
    elif re.search(r"\bexec\b", retire):
        fail("retire_backup must not execute anything")
    else:
        ok("retire_backup leaves an unremovable snapshot identifiable in control/ and warns")

    # Nothing under control/ may be executed without the safety gate in front of it.
    unguarded_execs = []
    for match in re.finditer(r"exec /bin/bash control/amp_bootstrap_start\.sh", installer):
        window = installer[max(0, match.start() - 600) : match.start()]
        if "bootstrap_is_executable" not in window:
            unguarded_execs.append(installer[: match.start()].count("\n") + 1)
    if unguarded_execs:
        fail(f"control bootstrap is executed without the safety gate at lines {unguarded_execs}")
    else:
        ok("every control bootstrap exec is gated by bootstrap_is_executable")

    # The unproven-restoration branch must never reach for a control file.
    branch = re.search(
        r'if test "\$RESTORE_ATTEMPTED" -eq 1 && test "\$RESTORE_VERIFIED" -ne 1; then(.*?)\nfi\n',
        installer,
        re.DOTALL,
    )
    if branch is None:
        fail("installer must have an explicit unproven-restoration branch")
    elif "control/amp_bootstrap_start.sh" in branch.group(1):
        fail("the unproven-restoration branch must not execute any control file")
    elif "current_start_is_safe" not in branch.group(1) or "exit 1" not in branch.group(1):
        fail(
            "the unproven-restoration branch must fall back only to a safe current release, "
            "and otherwise exit nonzero"
        )
    else:
        ok("unproven restoration refuses every control file and exits nonzero without a safe current")

    safe_current = _shell_function_body(installer, "current_start_is_safe") or ""
    for needle, label in [
        ("refusing symlinked current/scripts/amp_start.sh", "symlinked release start script"),
        ("refusing empty current/scripts/amp_start.sh", "empty release start script"),
        ("mode_allows_foreign_write", "group/world-writable release start script"),
    ]:
        if needle not in safe_current:
            fail(f"current_start_is_safe must reject a {label}")
        else:
            ok(f"current_start_is_safe rejects a {label}")

    secure_dir = _shell_function_body(installer, "secure_control_dir") or ""
    if "REQUIRE_MODES" not in secure_dir or "exit 1" not in secure_dir:
        fail("secure_control_dir must exit nonzero when Linux control/ hardening cannot be proven")
    elif 'test "$(uname -s 2>/dev/null || printf unknown)" = Linux' not in installer:
        fail("installer must decide mode enforcement is mandatory from the running OS")
    else:
        ok("control/ hardening is fatal on Linux/AMP and only a warning on developer checkouts")


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
EXISTING_DEPLOY = "#!/usr/bin/env python3\nprint('existing deploy shim')\n"
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
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SCRATCH_TEMPLATE_BASE_URL"] = base_url
    env.pop("SCRATCH_TEMPLATE_REF", None)
    env.pop("SCRATCH_INSTALL_FAULT", None)
    env.update(env_extra or {})
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
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    trace = bool(os.environ.get("SCRATCH_VALIDATE_TRACE"))
    if os.name == "nt":
        # Git for Windows silently truncates a single argv entry at ~8 KiB, so the
        # decoded installer is handed to bash through a file here. AMP never uses this
        # path: it passes the space-free base64 wrapper, which scenario 9 covers, and
        # Linux argv limits are two orders of magnitude larger.
        holder = tempfile.mkdtemp()
        try:
            script = Path(holder) / "payload.sh"
            write_lf(script, payload)
            argv = [bash_bin, "-x", str(script)] if trace else [bash_bin, str(script)]
            return _run_bash(argv, instance, base_url, env_extra)
        finally:
            shutil.rmtree(holder, ignore_errors=True)
    flag = "-xc" if trace else "-c"
    return _run_bash([bash_bin, flag, payload], instance, base_url, env_extra)


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
        write_start_script(instance / "current" / "scripts" / "amp_start.sh", EXISTING_CURRENT_START)
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

    validate_pair_install_faults(bash_bin, payload, prepare_remote)
    validate_restore_faults(bash_bin, payload, prepare_remote)
    validate_pair_install_modes(bash_bin, payload, prepare_remote)


def _control_state(instance: Path) -> tuple[str | None, str | None, list[str]]:
    """Return (bootstrap text, deploy text, leftover dot-files) for an instance."""
    control = instance / "control"

    def read_or_none(name: str) -> str | None:
        path = control / name
        return path.read_text(encoding="utf-8") if path.is_file() else None

    leftovers = sorted(p.name for p in control.glob(".*")) if control.is_dir() else []
    return (
        read_or_none("amp_bootstrap_start.sh"),
        read_or_none("scratch_mmo_deploy_latest.py"),
        leftovers,
    )


def validate_pair_install_faults(bash_bin: str, payload: str, prepare_remote) -> None:
    """Inject a failure at every mid-install point and require the previous pair back.

    A mixed pair (new bootstrap + old shim, or the reverse) is the failure mode these
    scenarios exist to rule out, so every case asserts *both* files together.
    """
    for fault in PAIR_FAULT_POINTS:
        # A complete pre-existing pair must survive a failure at any injection point.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            remote = prepare_remote(root, STUB_BOOTSTRAP, STUB_DEPLOY)
            instance = root / "instance"
            write_lf(instance / "control" / "amp_bootstrap_start.sh", EXISTING_BOOTSTRAP)
            write_lf(instance / "control" / "scratch_mmo_deploy_latest.py", EXISTING_DEPLOY)
            with _serve_directory(remote) as base_url:
                proc = _run_payload(
                    bash_bin, payload, instance, base_url, {"SCRATCH_INSTALL_FAULT": fault}
                )
            boot, deploy, leftovers = _control_state(instance)
            if boot == STUB_BOOTSTRAP or deploy == STUB_DEPLOY:
                fail(f"fault {fault}: newly downloaded control bytes survived a failed install")
            elif boot != EXISTING_BOOTSTRAP or deploy != EXISTING_DEPLOY:
                fail(
                    f"fault {fault}: previous control pair not fully restored "
                    f"(bootstrap_restored={boot == EXISTING_BOOTSTRAP}, "
                    f"shim_restored={deploy == EXISTING_DEPLOY})"
                )
            elif "STUB_BOOTSTRAP_RAN" in proc.stdout:
                fail(f"fault {fault}: the un-installed new bootstrap was executed anyway")
            elif proc.returncode != 0 or "EXISTING_BOOTSTRAP_RAN" not in proc.stdout:
                fail(f"fault {fault}: did not fall back to the existing control bootstrap")
            elif leftovers:
                fail(f"fault {fault}: temp/backup files left in control/: {leftovers}")
            else:
                ok(f"pair install fault {fault}: complete previous pair restored and reused")

        # With no pre-existing pair, a failure must leave *neither* file behind.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            remote = prepare_remote(root, STUB_BOOTSTRAP, STUB_DEPLOY)
            instance = root / "instance"
            (instance / "control").mkdir(parents=True)
            with _serve_directory(remote) as base_url:
                proc = _run_payload(
                    bash_bin, payload, instance, base_url, {"SCRATCH_INSTALL_FAULT": fault}
                )
            boot, deploy, leftovers = _control_state(instance)
            if boot is not None or deploy is not None:
                fail(f"fault {fault}: partial control pair left behind on a fresh instance")
            elif proc.returncode == 0 or "ERROR" not in proc.stderr:
                fail(f"fault {fault}: fresh instance did not fail closed with an error")
            elif leftovers:
                fail(f"fault {fault}: temp/backup files left in control/: {leftovers}")
            else:
                ok(f"pair install fault {fault}: fresh instance left with no control pair")

    # Only the bootstrap pre-exists: restoring must not invent a shim.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        remote = prepare_remote(root, STUB_BOOTSTRAP, STUB_DEPLOY)
        instance = root / "instance"
        write_lf(instance / "control" / "amp_bootstrap_start.sh", EXISTING_BOOTSTRAP)
        with _serve_directory(remote) as base_url:
            proc = _run_payload(
                bash_bin,
                payload,
                instance,
                base_url,
                {"SCRATCH_INSTALL_FAULT": "after_first_replace"},
            )
        boot, deploy, leftovers = _control_state(instance)
        if boot != EXISTING_BOOTSTRAP:
            fail("half-populated pair: existing bootstrap was not restored")
        elif deploy is not None:
            fail("half-populated pair: a shim appeared that did not exist before")
        elif proc.returncode != 0 or "EXISTING_BOOTSTRAP_RAN" not in proc.stdout:
            fail("half-populated pair: did not fall back to the existing bootstrap")
        elif leftovers:
            fail(f"half-populated pair: temp/backup files left in control/: {leftovers}")
        else:
            ok("pre-existing bootstrap only: rollback restores exactly that state")

    # Only the shim pre-exists: the new bootstrap must be removed again, not kept.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        remote = prepare_remote(root, STUB_BOOTSTRAP, STUB_DEPLOY)
        instance = root / "instance"
        write_lf(instance / "control" / "scratch_mmo_deploy_latest.py", EXISTING_DEPLOY)
        write_start_script(instance / "current" / "scripts" / "amp_start.sh", EXISTING_CURRENT_START)
        with _serve_directory(remote) as base_url:
            proc = _run_payload(
                bash_bin,
                payload,
                instance,
                base_url,
                {"SCRATCH_INSTALL_FAULT": "during_second_replace"},
            )
        boot, deploy, leftovers = _control_state(instance)
        if boot is not None:
            fail("half-populated pair: the new bootstrap was kept without its matching shim")
        elif deploy != EXISTING_DEPLOY:
            fail("half-populated pair: existing shim was not restored")
        elif "STUB_BOOTSTRAP_RAN" in proc.stdout:
            fail("half-populated pair: the rolled-back bootstrap was executed")
        elif proc.returncode != 0 or "CURRENT_START_RAN" not in proc.stdout:
            fail("half-populated pair: did not fall back to current/scripts/amp_start.sh")
        elif leftovers:
            fail(f"half-populated pair: temp/backup files left in control/: {leftovers}")
        else:
            ok("pre-existing shim only: rollback removes the unmatched new bootstrap")

    # A symlinked control file is refused before anything is replaced.
    if os.name == "posix":
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            remote = prepare_remote(root, STUB_BOOTSTRAP, STUB_DEPLOY)
            instance = root / "instance"
            write_lf(instance / "control" / "amp_bootstrap_start.sh", EXISTING_BOOTSTRAP)
            outside = root / "outside.py"
            write_lf(outside, EXISTING_DEPLOY)
            (instance / "control" / "scratch_mmo_deploy_latest.py").symlink_to(outside)
            with _serve_directory(remote) as base_url:
                proc = _run_payload(bash_bin, payload, instance, base_url)
            boot, _, leftovers = _control_state(instance)
            if "refusing symlinked control file" not in proc.stderr:
                fail("symlinked control file was not refused")
            elif outside.read_text(encoding="utf-8") != EXISTING_DEPLOY:
                fail("installer wrote through a symlinked control file")
            elif boot != EXISTING_BOOTSTRAP:
                fail("symlink refusal still replaced the bootstrap")
            elif leftovers:
                fail(f"symlink refusal left temp/backup files in control/: {leftovers}")
            else:
                ok("symlinked control file refused before any authoritative replacement")
    else:
        ok("symlink refusal scenario skipped: needs POSIX symlinks")


def write_start_script(path: Path, text: str) -> None:
    """A release start script the installer is allowed to treat as a safe fallback."""
    write_lf(path, text)
    if os.name == "posix":
        path.chmod(0o700)


@functools.lru_cache(maxsize=8)
def _bash_uname(bash_bin: str) -> str:
    proc = subprocess.run(
        [bash_bin, "-c", "uname -s 2>/dev/null || printf unknown"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    return proc.stdout.strip()


def _bash_sees_hard_links(bash_bin: str) -> bool:
    """True when the shell under test can read a real st_nlink for a hard-linked file."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        original = root / "original"
        write_lf(original, "probe\n")
        try:
            os.link(original, root / "extra")
        except (OSError, NotImplementedError, AttributeError):
            return False
        proc = subprocess.run(
            [bash_bin, "-c", "stat -c '%h' original 2>/dev/null || stat -f '%l' original 2>/dev/null"],
            cwd=str(root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    return proc.stdout.strip() == "2"


@contextlib.contextmanager
def _fault_run(
    bash_bin: str,
    payload: str,
    prepare_remote,
    fault: str,
    *,
    keep_bootstrap: bool = False,
    keep_deploy: bool = False,
    with_current: bool = False,
    prepare_extra=None,
):
    """Run the shipped payload against a throwaway instance with one fault injected."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        remote = prepare_remote(root, STUB_BOOTSTRAP, STUB_DEPLOY)
        instance = root / "instance"
        (instance / "control").mkdir(parents=True)
        if keep_bootstrap:
            write_lf(instance / "control" / "amp_bootstrap_start.sh", EXISTING_BOOTSTRAP)
        if keep_deploy:
            write_lf(instance / "control" / "scratch_mmo_deploy_latest.py", EXISTING_DEPLOY)
        if with_current:
            write_start_script(
                instance / "current" / "scripts" / "amp_start.sh", EXISTING_CURRENT_START
            )
        if prepare_extra is not None:
            prepare_extra(root, instance)
        env_extra = {"SCRATCH_INSTALL_FAULT": fault} if fault else {}
        with _serve_directory(remote) as base_url:
            proc = _run_payload(bash_bin, payload, instance, base_url, env_extra)
        yield proc, instance


def _check_fail_closed(
    label: str,
    proc: subprocess.CompletedProcess[str],
    instance: Path,
    *,
    with_current: bool,
    expect_backups: bool = True,
) -> None:
    """Shared invariants for every scenario where the control state cannot be proven.

    Nothing freshly downloaded may run, the unproven previous pair may not run either,
    and the only permitted escape is a release start script that passed the safety
    policy. Anything else has to exit nonzero.
    """
    _, _, leftovers = _control_state(instance)
    backups = [name for name in leftovers if name.startswith(".bak-")]
    if "STUB_BOOTSTRAP_RAN" in proc.stdout:
        fail(f"{label}: newly downloaded control bytes were executed")
    elif "EXISTING_BOOTSTRAP_RAN" in proc.stdout:
        fail(f"{label}: an unverified previous control pair was executed")
    elif with_current and (proc.returncode != 0 or "CURRENT_START_RAN" not in proc.stdout):
        fail(
            f"{label}: did not fall back to the safe current/scripts/amp_start.sh "
            f"(exit={proc.returncode})"
        )
    elif not with_current and proc.returncode == 0:
        fail(f"{label}: exited 0 with no provable control state and no safe current release")
    elif not with_current and "ERROR" not in proc.stderr:
        fail(f"{label}: failed without an explanatory error")
    elif not with_current and "CURRENT_START_RAN" in proc.stdout:
        fail(f"{label}: started a current release that does not exist")
    elif expect_backups and not backups:
        fail(f"{label}: control snapshots were deleted before the restoration was verified")
    else:
        ok(f"{label}: fail-closed, snapshots preserved, no mixed or unproven pair executed")


def validate_restore_faults(bash_bin: str, payload: str, prepare_remote) -> None:
    """Break each step of the rollback itself and require the installer to fail closed.

    Rolling back is the last line of defence, so "we tried to restore" is not good
    enough: unless the complete previous pair is proven back in place, no control file
    may run at all.
    """
    # 1. Restoring the bootstrap fails, leaving the new bootstrap beside the old shim.
    for with_current in (True, False):
        with _fault_run(
            bash_bin,
            payload,
            prepare_remote,
            "restore_first",
            keep_bootstrap=True,
            keep_deploy=True,
            with_current=with_current,
        ) as (proc, instance):
            boot, deploy, _ = _control_state(instance)
            label = f"restore fault restore_first (current={with_current})"
            if boot != STUB_BOOTSTRAP or deploy != EXISTING_DEPLOY:
                fail(f"{label}: the scenario did not actually strand a mixed control pair")
            else:
                _check_fail_closed(label, proc, instance, with_current=with_current)

    # 2. Restoring the shim fails when the shim is the only file that existed.
    for with_current in (True, False):
        with _fault_run(
            bash_bin,
            payload,
            prepare_remote,
            "restore_second",
            keep_deploy=True,
            with_current=with_current,
        ) as (proc, instance):
            boot, _, _ = _control_state(instance)
            label = f"restore fault restore_second, shim only (current={with_current})"
            if boot is not None:
                fail(f"{label}: the unmatched new bootstrap was kept")
            else:
                _check_fail_closed(label, proc, instance, with_current=with_current)

    # 3. The first restoration succeeds and the second fails: still not provable.
    for with_current in (True, False):
        with _fault_run(
            bash_bin,
            payload,
            prepare_remote,
            "restore_second",
            keep_bootstrap=True,
            keep_deploy=True,
            with_current=with_current,
        ) as (proc, instance):
            boot, deploy, _ = _control_state(instance)
            label = f"restore fault restore_second, full pair (current={with_current})"
            if boot != EXISTING_BOOTSTRAP or deploy != EXISTING_DEPLOY:
                fail(f"{label}: the first restoration did not run before the second failed")
            elif "could not be verifiably restored" not in proc.stderr:
                fail(f"{label}: a partially proven restoration was not reported as unproven")
            else:
                _check_fail_closed(label, proc, instance, with_current=with_current)

    # 4. Mode reapplication fails during restoration.
    # 5. The restored bytes do not hash to the snapshot digest.
    for fault in ("restore_mode", "restore_digest"):
        for with_current in (True, False):
            with _fault_run(
                bash_bin,
                payload,
                prepare_remote,
                fault,
                keep_bootstrap=True,
                keep_deploy=True,
                with_current=with_current,
            ) as (proc, instance):
                label = f"restore fault {fault} (current={with_current})"
                _check_fail_closed(label, proc, instance, with_current=with_current)

    # 6. Cleanup fails *after* a proven restoration: the good pair stays usable.
    with _fault_run(
        bash_bin,
        payload,
        prepare_remote,
        "restore_cleanup",
        keep_bootstrap=True,
        keep_deploy=True,
    ) as (proc, instance):
        boot, deploy, leftovers = _control_state(instance)
        backups = [name for name in leftovers if name.startswith(".bak-")]
        label = "restore fault restore_cleanup"
        if boot != EXISTING_BOOTSTRAP or deploy != EXISTING_DEPLOY:
            fail(f"{label}: the previous pair was not fully restored")
        elif "STUB_BOOTSTRAP_RAN" in proc.stdout:
            fail(f"{label}: newly downloaded control bytes were executed")
        elif proc.returncode != 0 or "EXISTING_BOOTSTRAP_RAN" not in proc.stdout:
            fail(f"{label}: a verified restoration was invalidated by a cleanup failure")
        elif not backups:
            fail(f"{label}: the un-removable snapshot was not left identifiable in control/")
        elif "WARNING" not in proc.stderr:
            fail(f"{label}: the cleanup failure was not logged")
        else:
            ok(
                "cleanup failure after a proven restoration keeps the known-good pair and "
                "leaves the snapshot identifiable in control/"
            )

    # 7. control/ itself cannot be restricted and verified.
    if _bash_uname(bash_bin) == "Linux":
        for with_current in (True, False):
            with _fault_run(
                bash_bin,
                payload,
                prepare_remote,
                "control_dir_mode",
                keep_bootstrap=True,
                keep_deploy=True,
                with_current=with_current,
            ) as (proc, instance):
                boot, deploy, _ = _control_state(instance)
                label = f"control directory hardening failure (current={with_current})"
                if proc.returncode == 0:
                    fail(f"{label}: unsecurable control/ was not fatal on Linux")
                elif "STUB_BOOTSTRAP_RAN" in proc.stdout or "EXISTING_BOOTSTRAP_RAN" in proc.stdout:
                    fail(f"{label}: a control file was executed from an unsecured control/")
                elif "CURRENT_START_RAN" in proc.stdout:
                    fail(f"{label}: an unsecured control/ fell through to the release fallback")
                elif boot != EXISTING_BOOTSTRAP or deploy != EXISTING_DEPLOY:
                    fail(f"{label}: the existing control pair was modified anyway")
                else:
                    ok(f"{label}: fatal before anything is downloaded, installed, or executed")
    else:
        ok(
            "control directory hardening fatality is Linux/AMP-only; the shell under test "
            f"reports {_bash_uname(bash_bin)!r}, where it degrades to a warning"
        )

    # 8. An authoritative control file carries an extra hard link.
    if not _bash_sees_hard_links(bash_bin):
        ok("hard-linked control file scenario skipped: the shell cannot read st_nlink here")
        return

    def add_hard_link(root: Path, instance: Path) -> None:
        os.link(instance / "control" / "amp_bootstrap_start.sh", root / "shadow_copy.sh")

    for with_current in (True, False):
        with _fault_run(
            bash_bin,
            payload,
            prepare_remote,
            "",
            keep_bootstrap=True,
            keep_deploy=True,
            with_current=with_current,
            prepare_extra=add_hard_link,
        ) as (proc, instance):
            boot, deploy, _ = _control_state(instance)
            label = f"hard-linked control bootstrap (current={with_current})"
            if "refusing hard-linked control file" not in proc.stderr:
                fail(f"{label}: the extra hard link was not detected")
            elif boot != EXISTING_BOOTSTRAP or deploy != EXISTING_DEPLOY:
                fail(f"{label}: the hard-linked pair was replaced anyway")
            else:
                _check_fail_closed(
                    label, proc, instance, with_current=with_current, expect_backups=False
                )


def validate_pair_install_modes(bash_bin: str, payload: str, prepare_remote) -> None:
    """On a mode-enforcing filesystem the installed pair must really be 0700."""
    if os.name != "posix":
        ok("installed control file mode check skipped: filesystem cannot enforce modes")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        remote = prepare_remote(root, STUB_BOOTSTRAP, STUB_DEPLOY)
        instance = root / "instance"
        instance.mkdir()
        with _serve_directory(remote) as base_url:
            proc = _run_payload(bash_bin, payload, instance, base_url)
        modes = {
            name: (instance / "control" / name).stat().st_mode & 0o777
            for name in ("amp_bootstrap_start.sh", "scratch_mmo_deploy_latest.py")
            if (instance / "control" / name).is_file()
        }
        if proc.returncode != 0:
            fail(f"mode scenario: installer run failed (exit={proc.returncode})")
        elif len(modes) != 2:
            fail(f"mode scenario: control pair incomplete after install: {sorted(modes)}")
        elif any(mode != 0o700 for mode in modes.values()):
            rendered = {name: oct(mode) for name, mode in modes.items()}
            fail(f"installed control files are not 0700: {rendered}")
        else:
            ok("installed control pair is owner-private and executable (0700)")


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
    validate_sigterm_process_tree(decoded_installer)


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
            f"Meta.ConfigVersion must be >= {REQUIRED_CONFIG_VERSION} after the verified "
            "fail-closed control-pair restoration change"
        )
    elif int(version_match.group(1)) != REQUIRED_CONFIG_VERSION:
        fail(
            f"Meta.ConfigVersion must be exactly {REQUIRED_CONFIG_VERSION}; the README, "
            "validator, and template refresh notes are written for that version"
        )
    else:
        ok(f"Meta.ConfigVersion={version_match.group(1)} (exact)")

    if "App.ExitMethod=SIGTERM" not in kvp:
        fail("App.ExitMethod must stay SIGTERM so AMP Stop reaches the Python supervisor")
    else:
        ok("App.ExitMethod=SIGTERM (matches the exec-supervised process tree)")

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
        ("current_engine_path", "already-installed engine reuse"),
        ("stage_control_engine", "bounded control-engine staging"),
        ("extract_verified_control_modules", "control-module-only extraction"),
        ("prune_control_engine_sets", "control-engine retention pruning"),
        ("purge_interrupted_control_staging", "interrupted-staging recovery"),
        ("MAX_CONTROL_ENGINE_SETS", "control-engine set ceiling"),
        ("os.execv", "exec-based supervised handoff"),
    ]:
        if needle not in text:
            fail(f"deploy shim missing {label}")
        else:
            ok(f"deploy shim supports {label}")

    # No independent deploy engine: the shim must not rename/replace current/ itself.
    for pattern, label in [
        (r"swap_current", "legacy swap_current engine"),
        (r"deploy_root\s*/\s*[\"']previous[\"']", "previous/ backup rename engine"),
        (r"os\.rename\(", "directory rename"),
        (r"[\"']current[\"']\s*\)?\s*\.\s*rename", "current/ rename"),
        (r"shutil\.(?:move|copytree|copy2|copyfile|copy)\(", "tree copy/move helper"),
    ]:
        if re.search(pattern, text):
            fail(f"deploy shim must not implement its own {label}")
        else:
            ok(f"deploy shim has no {label}")

    if re.search(r"os\.replace\([^)]*current", text):
        fail("deploy shim must not replace current/ itself; the release engine owns that")
    else:
        ok("deploy shim leaves current/ swapping to the release engine")

    validate_deploy_shim_filesystem_scope(text)


def validate_deploy_shim_filesystem_scope(text: str) -> None:
    """Destructive filesystem calls must stay inside bounded control-engine staging.

    C1 banned `shutil`/`rmtree` outright. C1.1 needs both for bounded staging cleanup,
    so the ban is replaced by stricter, targeted rules: an allowlist of `shutil`
    attributes, a single guarded `rmtree`, and proof that no mutating call can name
    `current/`.
    """
    shutil_attrs = set(re.findall(r"shutil\.(\w+)", text))
    unexpected = sorted(shutil_attrs - {"disk_usage", "rmtree"})
    if unexpected:
        fail(f"deploy shim uses unexpected shutil helpers: {unexpected}")
    else:
        ok("deploy shim limits shutil to disk_usage + a single guarded rmtree")

    mutating = re.findall(
        r"(?:shutil\.rmtree|shutil\.move|os\.rename|os\.replace|os\.removedirs|os\.rmdir|"
        r"os\.unlink)\s*\([^)]*\)",
        text,
    )
    touching_current = [
        call for call in mutating if re.search(r"CURRENT_DIR_NAME|['\"]current['\"]", call)
    ]
    if touching_current:
        fail(f"deploy shim mutates current/ directly: {touching_current[:2]}")
    else:
        ok(f"no mutating filesystem call in the shim names current/ ({len(mutating)} checked)")

    lines = text.splitlines()
    rmtree_lines = [i for i, line in enumerate(lines) if "shutil.rmtree(" in line]
    if len(rmtree_lines) != 1:
        fail(f"deploy shim must contain exactly one shutil.rmtree call, found {len(rmtree_lines)}")
    else:
        guard_window = "\n".join(lines[max(0, rmtree_lines[0] - 6) : rmtree_lines[0]])
        if "_tree_is_control_engine_shaped" not in guard_window:
            fail("the shim's shutil.rmtree is not guarded by a control-engine shape check")
        else:
            ok("the shim's only rmtree is guarded by a control-engine shape check")

    # An unrecognised staging entry is preserved, never deleted.
    if "quarantine" not in text.lower():
        fail("deploy shim must quarantine unrecognised staging entries instead of deleting them")
    else:
        ok("deploy shim quarantines unrecognised staging entries")

    # The supervised path must exec, not spawn, so signals stay with this pid.
    if not re.search(r"exec_replace\s*=\s*\(\s*mode\s*==\s*[\"']deploy-and-supervise[\"']", text):
        fail("deploy shim must exec-replace itself only on the supervised path")
    else:
        ok("deploy shim exec-replaces itself on the supervised path")

    spawns = re.findall(r"subprocess\.\w+\(\s*\[[^\]]*\]", text)
    spawns += re.findall(r"os\.exec\w+\([^)]*\)", text)
    if any(re.search(r"[\"']tee[\"']", spawn) for spawn in spawns):
        fail("deploy shim must not spawn a tee child for logging")
    elif re.search(r"shell\s*=\s*True|os\.system\(|os\.popen\(", text):
        fail("deploy shim must not run its logging or handoff through a shell")
    elif "TeeStream" not in text or "install_dual_logging" not in text:
        fail("deploy shim must mirror console output into the bootstrap log in-process")
    else:
        ok("deploy shim mirrors its log in-process (no tee child, no shell)")

    # Reaching an unchanged release must not download or extract anything.
    already_current = text.find("installed_engine = current_engine_path(deploy_root)")
    staging = text.find("stage_engine_from_release(config)")
    if already_current < 0 or staging < 0:
        fail("deploy shim must try the installed engine before bootstrap-staging one")
    elif already_current > staging:
        fail("deploy shim stages from a release before checking the installed engine")
    else:
        ok("deploy shim reuses a verified installed engine before downloading anything")

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


FAKE_SUPERVISOR = '''#!/usr/bin/env python3
"""Stand-in for control/scratch_mmo_deploy_latest.py --deploy --supervise --yes.

Records its own pid and the signals it receives, launches the fake game launcher,
and forwards SIGTERM exactly the way the real supervised handoff must.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ["SCRATCH_DEPLOY_ROOT"])
REC = ROOT / "signals"
REC.mkdir(parents=True, exist_ok=True)
(REC / "supervisor.pid").write_text(str(os.getpid()))
(REC / "supervisor.argv").write_text(" ".join(sys.argv[1:]))

CHILD = subprocess.Popen(["/bin/bash", str(ROOT / "current" / "scripts" / "amp_start.sh")])
(REC / "launcher.pid").write_text(str(CHILD.pid))


def on_term(signum, _frame):
    (REC / "supervisor.sigterm").write_text(str(signum))
    CHILD.send_signal(signal.SIGTERM)
    try:
        CHILD.wait(timeout=15)
    except subprocess.TimeoutExpired:
        CHILD.kill()
        CHILD.wait(timeout=5)
    (REC / "supervisor.exited").write_text(str(CHILD.returncode))
    sys.exit(0)


signal.signal(signal.SIGTERM, on_term)
(REC / "supervisor.ready").write_text("1")
while True:
    time.sleep(0.05)
'''

FAKE_AMP_START = '''#!/usr/bin/env bash
# Stand-in for current/scripts/amp_start.sh: holds the web port through a grandchild.
set -u
REC="${SCRATCH_DEPLOY_ROOT}/signals"
mkdir -p "${REC}"
printf '%s' "$$" > "${REC}/launcher.actual.pid"

on_term() {
	printf 'TERM' > "${REC}/launcher.sigterm"
	kill "${GAME_PID}" 2>/dev/null || true
	wait "${GAME_PID}" 2>/dev/null || true
	printf 'done' > "${REC}/launcher.exited"
	exit 0
}
trap on_term TERM

python3 -u -c 'import os,socket,time
s=socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(os.environ["SCRATCH_TEST_PORT"])))
s.listen(4)
open(os.environ["SCRATCH_DEPLOY_ROOT"] + "/signals/game.ready", "w").write("1")
while True:
    time.sleep(0.05)' &
GAME_PID=$!
printf '%s' "${GAME_PID}" > "${REC}/game.pid"
printf 'ready' > "${REC}/launcher.ready"
wait "${GAME_PID}"
'''


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for(path: Path, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _wait_pids_gone(pids: list[int], timeout: float = 20.0) -> list[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = [pid for pid in pids if _pid_alive(pid)]
        if not alive:
            return []
        time.sleep(0.1)
    return [pid for pid in pids if _pid_alive(pid)]


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def validate_sigterm_process_tree(decoded_installer: str) -> None:
    """Prove AMP's SIGTERM to the outer pid tears the whole tree down on Linux.

    AMP sets App.ExitMethod=SIGTERM and signals only the process it launched. The
    bootstrap therefore has to `exec` into the Python supervisor (no `tee`, no
    pipeline) so that outer pid *is* the supervisor, which forwards the signal to the
    game launcher it started.
    """
    if not sys.platform.startswith("linux"):
        ok(
            "SIGTERM process-tree test skipped: needs real Linux process groups and "
            f"signal semantics (running on {sys.platform}); the test stays enabled for Linux CI"
        )
        return

    bash_bin = find_bash()
    if bash_bin is None:
        fail("SIGTERM process-tree test needs bash but none was found on Linux")
        return

    kvp = read_text(ROOT / "scratchmmo.kvp")
    if "App.ExitMethod=SIGTERM" not in kvp:
        fail("SIGTERM process-tree test assumes App.ExitMethod=SIGTERM")
        return

    bootstrap_src = (ROOT / "control" / "amp_bootstrap_start.sh").read_bytes().replace(b"\r\n", b"\n")
    port = _free_port()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        instance = Path(tmp) / "instance"
        (instance / "control").mkdir(parents=True)
        # Byte-exact copy of the shipped public bootstrap: this is the code under test.
        boot_path = instance / "control" / "amp_bootstrap_start.sh"
        boot_path.write_bytes(bootstrap_src)
        boot_path.chmod(0o700)
        write_lf(instance / "control" / "scratch_mmo_deploy_latest.py", FAKE_SUPERVISOR)
        write_lf(instance / "current" / "scripts" / "amp_start.sh", FAKE_AMP_START)
        (instance / "current" / "scripts" / "amp_start.sh").chmod(0o700)

        env = os.environ.copy()
        env["SCRATCH_TEMPLATE_BASE_URL"] = f"http://127.0.0.1:{_free_port()}/control"
        env["SCRATCH_GITHUB_TOKEN"] = "not-a-real-token-value-1234567890"
        env["SCRATCH_TEST_PORT"] = str(port)
        env.pop("SCRATCH_TEMPLATE_REF", None)
        env.pop("SCRATCH_INSTALL_FAULT", None)

        # Exactly the shipped Start payload. The unreachable raw host makes it reuse the
        # control pair on disk and exec into the bootstrap, so the whole real chain runs.
        proc = subprocess.Popen(
            [bash_bin, "-c", decoded_installer],
            cwd=str(instance),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        rec = instance / "signals"
        try:
            ready = _wait_for(rec / "supervisor.ready") and _wait_for(rec / "game.ready")
            if not ready:
                proc.kill()
                proc.wait(timeout=10)
                fail("SIGTERM process-tree test: supervised process tree never became ready")
                return

            supervisor_pid = int((rec / "supervisor.pid").read_text().strip())
            launcher_pid = int((rec / "launcher.actual.pid").read_text().strip())
            game_pid = int((rec / "game.pid").read_text().strip())

            if supervisor_pid != proc.pid:
                fail(
                    "SIGTERM process-tree test: AMP's direct child is pid "
                    f"{proc.pid} but the Python supervisor is pid {supervisor_pid}; "
                    "the bootstrap did not exec into Python"
                )
                proc.kill()
                proc.wait(timeout=10)
                return
            if _port_free(port):
                fail("SIGTERM process-tree test: the fake game never held the web port")
                proc.kill()
                proc.wait(timeout=10)
                return

            # Exactly what AMP does on Stop/Restart: one SIGTERM, outer pid only.
            os.kill(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=45)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
                fail("SIGTERM process-tree test: outer process did not exit after SIGTERM")
                return

            if not (rec / "supervisor.sigterm").exists():
                fail("SIGTERM process-tree test: the Python supervisor never received SIGTERM")
            elif not _wait_for(rec / "launcher.sigterm", timeout=15):
                fail("SIGTERM process-tree test: the game launcher never received SIGTERM")
            else:
                survivors = _wait_pids_gone([supervisor_pid, launcher_pid, game_pid])
                if survivors:
                    fail(f"SIGTERM process-tree test: orphaned processes survived: {survivors}")
                elif not _port_free(port):
                    fail("SIGTERM process-tree test: the web port was not released")
                elif proc.returncode not in (0, -signal.SIGTERM):
                    fail(
                        "SIGTERM process-tree test: unclean supervisor exit "
                        f"(returncode={proc.returncode})"
                    )
                else:
                    ok(
                        "SIGTERM to the outer pid reaches the Python supervisor, is forwarded "
                        "to the game launcher, and leaves no orphans or held ports"
                    )
        finally:
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=10)


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

    for pattern, label in [
        (r"atomic(?:ally)?[^.\n]{0,120}pair|pair[^.\n]{0,120}atomic", "atomic control-pair install"),
        (r"exec[^.\n]{0,120}supervis|supervis[^.\n]{0,120}exec", "exec-based supervision"),
        (r"\btee\b", "the removed tee pipeline"),
        (r"already current[^.\n]{0,160}(?:no download|nothing|downloads nothing)", "already-current no-download path"),
        (r"control-engine|control engine", "bounded control-engine staging"),
        (r"SIGTERM", "SIGTERM shutdown path"),
    ]:
        if not re.search(pattern, text, re.IGNORECASE):
            fail(f"README must document {label}")
        else:
            ok(f"README documents {label}")

    stale_current = re.search(
        rf"This template is `Meta\.ConfigVersion=(?!{REQUIRED_CONFIG_VERSION}`)", text
    )
    if stale_current:
        fail(f"README must state the template is Meta.ConfigVersion={REQUIRED_CONFIG_VERSION}")
    else:
        ok(f"README states the current template version is {REQUIRED_CONFIG_VERSION}")

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
