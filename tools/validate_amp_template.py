#!/usr/bin/env python3
"""Validate Scratch MMO AMP template security and updater wiring."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EMIT_SCRIPT = ROOT / "tools" / "emit_start_command.py"
sys.path.insert(0, str(ROOT / "tools"))

from emit_start_command import (  # noqa: E402
    build_start_command_args,
    decode_installer_from_start_args,
    simulate_amp_arg_split,
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

    if bootstrap.is_file():
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
    else:
        ok("AMP space split yields bash -lc plus one wrapper argument")

    if " " in args[1]:
        fail("base64 wrapper argument must not contain literal spaces")
    else:
        ok("wrapper argument contains no literal spaces")

    raw_base = "raw.githubusercontent.com/carthorsestudios/scratch-mmo-amp-template/main/control"
    for needle in (
        raw_base,
        "amp_bootstrap_start.sh",
        "scratch_mmo_deploy_latest.py",
        "curl -fsSL",
        "wget -qO",
        "current/scripts/amp_start.sh",
        "control/amp_bootstrap_start.sh",
    ):
        if needle not in decoded_installer:
            fail(f"decoded inline installer missing {needle!r}")
        else:
            ok(f"decoded inline installer includes {needle}")

    bash_bin = shutil.which("bash")
    if bash_bin is None:
        ok("bash not available locally; skipping AMP-split bash syntax probe")
        return

    syntax = subprocess.run(
        [bash_bin, "-n", "-s"],
        input=decoded_installer,
        capture_output=True,
        text=True,
        check=False,
    )
    if syntax.returncode != 0:
        fail(f"decoded installer fails bash -n: {syntax.stderr.strip()}")
    else:
        ok("decoded installer passes bash -n syntax check")

    probe = subprocess.run(
        [bash_bin, "-lc", args[1]],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    combined = f"{probe.stderr}\n{probe.stdout}"
    if "unexpected EOF while looking for matching" in combined:
        fail("AMP-split start args still trigger unmatched quote error in bash")
    elif probe.returncode != 0 and "syntax error" in combined.lower():
        fail(f"AMP-split start args fail bash -lc parse/exec: {combined.strip()}")
    else:
        ok("AMP-split start args do not trigger unmatched quote error")


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

    try:
        decoded_installer = decode_installer_from_start_args(cmd_line)
    except ValueError as exc:
        fail(str(exc))
        return

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
                for rejected in (
                    "[GameServer] WebSocket listening port=19080",
                    "[2026-06-19T19:00:00Z] Godot WebSocket is listening on 127.0.0.1:19080",
                ):
                    if ready_pattern.fullmatch(rejected):
                        fail(f"AppReadyRegex must not match non-ready line: {rejected!r}")
                ok("AppReadyRegex matches production and setup ready samples")

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
        "SCRATCH_INVITE_CODE": "{{InviteCode}}",
        "SCRATCH_ALLOWED_ORIGINS": "{{AllowedWebOrigins}}",
    }
    for key, expected_env in required_env.items():
        if env_json.get(key) != expected_env:
            fail(f"Environment mapping mismatch for {key}")
        else:
            ok(f"environment maps {key}")

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


def validate_deploy_script() -> None:
    deploy_py = ROOT / "control" / "scratch_mmo_deploy_latest.py"
    if not deploy_py.is_file():
        return
    text = read_text(deploy_py)
    for needle, label in [
        ("127.0.0.1:9090/healthz", "health URL default"),
        ("--dry-run", "dry-run mode"),
        ('deploy_root / "previous"', "previous backup path"),
    ]:
        if needle not in text:
            fail(f"deploy updater missing {label}")
        else:
            ok(f"deploy updater supports {label}")

    token_print = re.compile(
        r"(?:print|log)\s*\(\s*(?:config\[[\'\"]github_token[\'\"]\]|token)\s*\)"
    )
    if token_print.search(text):
        fail("deploy updater must not print token values")
    else:
        ok("deploy updater does not print token values")


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

    readme = ROOT / "README.md"
    if readme.is_file():
        text = read_text(readme)
        if not re.search(r"(?:do not expose|must not expose|internal)", text, re.IGNORECASE):
            fail("README must warn against exposing AMP admin or 19080")
        else:
            ok("README warns against exposing AMP admin / 19080")


def main() -> int:
    print("Validating Scratch MMO AMP template")
    print(f"Root: {ROOT}")
    print()

    validate_json_files()
    print()
    validate_control_files()
    print()
    validate_kvp_and_config()
    print()
    validate_deploy_script()
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
