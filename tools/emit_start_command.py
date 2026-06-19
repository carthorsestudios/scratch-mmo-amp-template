#!/usr/bin/env python3
"""Emit and verify the AMP App.CommandLineArgs value for the inline bootstrap installer."""

from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "tools" / "inline_start_installer.sh"

# AMP splits App.CommandLineArgs on literal spaces and does not shell-parse quotes.
# Wrap the installer in a no-space base64 eval command: bash -lc eval${IFS}$(printf${IFS}%s${IFS}<b64>|base64${IFS}-d)
WRAPPER_PREFIX = "eval${IFS}$(printf${IFS}%s${IFS}"
WRAPPER_SUFFIX = "|base64${IFS}-d)"


def installer_to_one_liner(script: str) -> str:
    lines: list[str] = []
    for raw in script.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        while line.endswith(";"):
            line = line[:-1].rstrip()
        lines.append(line)

    if not lines:
        raise ValueError("installer script is empty")

    result = lines[0]
    join_with_space_after = ("{", "then", "else", "do", "&&", "||")
    for line in lines[1:]:
        if result.endswith(join_with_space_after):
            result = f"{result} {line}"
        else:
            result = f"{result}; {line}"
    return result


def encode_installer_script(script: str) -> str:
    one_liner = installer_to_one_liner(script)
    return base64.b64encode(one_liner.encode("utf-8")).decode("ascii")


def build_start_command_args(installer_script: str | None = None) -> str:
    script = installer_script if installer_script is not None else INSTALLER.read_text(encoding="utf-8")
    encoded = encode_installer_script(script)
    wrapper = f"{WRAPPER_PREFIX}{encoded}{WRAPPER_SUFFIX}"
    return f"-lc {wrapper}"


def decode_installer_from_start_args(cmd_line: str) -> str:
    match = re.search(
        r"printf\$\{IFS\}%s\$\{IFS\}([A-Za-z0-9+/=]+)\|base64\$\{IFS\}-d\)",
        cmd_line,
    )
    if not match:
        raise ValueError("start args do not contain expected base64 installer payload")
    return base64.b64decode(match.group(1)).decode("utf-8")


def simulate_amp_arg_split(cmd_line: str) -> list[str]:
    return cmd_line.split(" ")


def sync_scratchmmo_kvp() -> None:
    kvp_path = ROOT / "scratchmmo.kvp"
    kvp_text = kvp_path.read_text(encoding="utf-8")
    new_args = build_start_command_args()
    updated, count = re.subn(
        r"^App\.CommandLineArgs=.*$",
        f"App.CommandLineArgs={new_args}",
        kvp_text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise RuntimeError("Failed to update App.CommandLineArgs in scratchmmo.kvp")
    kvp_path.write_text(updated, encoding="utf-8", newline="\n")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--write-kvp":
        sync_scratchmmo_kvp()
        print(build_start_command_args())
        return 0
    print(build_start_command_args())
    return 0


if __name__ == "__main__":
    sys.exit(main())
