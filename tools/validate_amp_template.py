#!/usr/bin/env python3
"""Validate Scratch MMO AMP template security and updater wiring."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EMIT_SCRIPT = ROOT / "tools" / "emit_start_command.py"

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


def expected_start_command_args() -> str:
    result = subprocess.run(
        [sys.executable, str(EMIT_SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


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
    expected = expected_start_command_args()
    if not expected:
        fail("tools/emit_start_command.py failed to produce expected start args")
    elif cmd_line != expected:
        fail("App.CommandLineArgs does not match tools/emit_start_command.py")
    else:
        ok("start command matches inline installer definition")

    if cmd_line == "control/amp_bootstrap_start.sh":
        fail("start command must not assume control/amp_bootstrap_start.sh already exists")
    elif "-lc" not in cmd_line:
        fail("start command must use bash -lc inline installer")
    else:
        ok("start command uses inline bash installer")

    raw_base = "raw.githubusercontent.com/carthorsestudios/scratch-mmo-amp-template/main/control"
    if raw_base not in cmd_line:
        fail("start command must download bootstrap files from public template raw GitHub URLs")
    else:
        ok("start command references public template raw GitHub base URL")

    for asset in ("amp_bootstrap_start.sh", "scratch_mmo_deploy_latest.py"):
        if asset not in cmd_line or "$BASE/" not in cmd_line:
            fail(f"start command must install public bootstrap asset: {asset}")
        else:
            ok(f"start command installs {asset}")

    if "curl -fsSL" not in cmd_line:
        fail("start command must prefer curl -fsSL for bootstrap download")
    else:
        ok("start command uses curl -fsSL")

    if "wget -qO" not in cmd_line:
        fail("start command must include wget fallback for bootstrap download")
    else:
        ok("start command includes wget fallback")

    if "current/scripts/amp_start.sh" not in cmd_line:
        fail("start command must fall back to current/scripts/amp_start.sh on install failure")
    else:
        ok("start command includes amp_start.sh fallback")

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
    elif "Setup server listening port=" not in kvp:
        fail("AppReadyRegex must accept setup server ready line")
    elif "WebSocket listening port=" not in kvp or "GameServer" not in kvp:
        fail("AppReadyRegex must still accept game server ready line")
    else:
        ok("AppReadyRegex accepts game server or setup server ready lines")

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
    }
    for key, expected in required_env.items():
        if env_json.get(key) != expected:
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

    if not errors:
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
