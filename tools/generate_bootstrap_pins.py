#!/usr/bin/env python3
"""Generate the SHA-256 bootstrap pins embedded in tools/inline_start_installer.sh.

The inline start installer downloads `control/amp_bootstrap_start.sh` and
`control/scratch_mmo_deploy_latest.py` from raw.githubusercontent.com and refuses to
install them unless their digests match pins baked into the installer. Those pins are
derived here from the committed control files so they stay deterministic and are never
hand-edited.

raw.githubusercontent.com serves stored blob bytes, and .gitattributes forces `eol=lf`,
so digests are taken over LF-normalized file content.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "tools" / "inline_start_installer.sh"

# Pin variable name -> repo-relative control file whose served bytes it pins.
PINNED_CONTROL_FILES: dict[str, str] = {
    "BOOTSTRAP_SHA256": "control/amp_bootstrap_start.sh",
    "DEPLOY_SHA256": "control/scratch_mmo_deploy_latest.py",
}

PIN_NAMES = tuple(PINNED_CONTROL_FILES)
_PIN_LINE_RE = re.compile(
    r"^(?P<name>" + "|".join(PIN_NAMES) + r")=(?P<digest>[0-9a-f]*)$",
    re.MULTILINE,
)


class PinError(Exception):
    """The installer pins could not be read or regenerated."""


def normalize_newlines(data: bytes) -> bytes:
    """Match the LF bytes git stores (and therefore raw.githubusercontent.com serves)."""
    return data.replace(b"\r\n", b"\n")


def control_digest(rel: str) -> str:
    path = ROOT / rel
    if not path.is_file():
        raise PinError(f"Missing control file: {rel}")
    return hashlib.sha256(normalize_newlines(path.read_bytes())).hexdigest()


def expected_pins() -> dict[str, str]:
    return {name: control_digest(rel) for name, rel in PINNED_CONTROL_FILES.items()}


def read_installer_pins(installer_text: str) -> dict[str, str]:
    """Read pin assignments from either the readable installer or its one-liner form."""
    pins: dict[str, str] = {}
    for name in PIN_NAMES:
        matches = re.findall(rf"(?:^|[;\s]){name}=([0-9a-f]*)(?=$|[;\s])", installer_text)
        if not matches:
            raise PinError(f"Installer does not assign {name}")
        if len(set(matches)) != 1:
            raise PinError(f"Installer assigns {name} more than once with different values")
        pins[name] = matches[0]
    return pins


def render_installer(installer_text: str, pins: dict[str, str]) -> str:
    remaining = dict(pins)

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        remaining.pop(name, None)
        return f"{name}={pins[name]}"

    updated, count = _PIN_LINE_RE.subn(replace, installer_text)
    if count != len(pins) or remaining:
        raise PinError(
            "Expected exactly one assignment line per pin in tools/inline_start_installer.sh; "
            f"replaced {count} of {len(pins)}"
        )
    return updated


def git_blob_matches_normalized(rel: str) -> bool | None:
    """True when git would store exactly the LF bytes we hash. None when git is unavailable.

    `git hash-object <path>` applies the configured text filters, so its object id is the
    blob GitHub will serve. Hashing our normalized bytes the same way proves the pin
    covers the served content even on a CRLF checkout.
    """
    try:
        result = subprocess.run(
            ["git", "hash-object", "--", rel],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    git_oid = result.stdout.strip()
    if not git_oid:
        return None
    normalized = normalize_newlines((ROOT / rel).read_bytes())
    header = f"blob {len(normalized)}\0".encode("ascii")
    local_oid = hashlib.sha1(header + normalized).hexdigest()  # noqa: S324 - git object id
    return git_oid == local_oid


def write_pins() -> dict[str, str]:
    pins = expected_pins()
    text = INSTALLER.read_text(encoding="utf-8")
    updated = render_installer(text, pins)
    if updated != text:
        INSTALLER.write_text(updated, encoding="utf-8", newline="\n")
    return pins


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in args

    try:
        pins = expected_pins()
        installer_pins = read_installer_pins(INSTALLER.read_text(encoding="utf-8"))
    except PinError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if check_only:
        stale = {name for name in pins if pins[name] != installer_pins.get(name)}
        for name, rel in PINNED_CONTROL_FILES.items():
            status = "STALE" if name in stale else "ok"
            print(f"{status:5} {name} {pins[name]}  ({rel})")
        if stale:
            print(
                "Pins are stale. Run: python tools/generate_bootstrap_pins.py "
                "&& python tools/emit_start_command.py --write-kvp",
                file=sys.stderr,
            )
            return 1
        return 0

    try:
        pins = write_pins()
    except PinError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for name, rel in PINNED_CONTROL_FILES.items():
        print(f"{name}={pins[name]}  ({rel})")
    print(
        "Wrote pins to tools/inline_start_installer.sh. "
        "Now run: python tools/emit_start_command.py --write-kvp"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
