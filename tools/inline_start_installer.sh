#!/usr/bin/env bash
# Readable source for the AMP inline start installer embedded in scratchmmo.kvp.
# AMP does not copy template support files into instances; Start runs this logic via bash -lc base64 wrapper in scratchmmo.kvp.
#
# Trust model: the two control files are fetched into temporary files and are only
# installed once their SHA-256 digests match the pins below. Mutable raw `main`
# bytes are never executed unverified, and an unverifiable download always falls
# back to something already on disk instead of running new code.
#
# BOOTSTRAP_SHA256 / DEPLOY_SHA256 are generated from the committed control/ files
# by tools/generate_bootstrap_pins.py. Never hand-edit them.
set -e

mkdir -p control
REF=${SCRATCH_TEMPLATE_REF:-main}
BASE=${SCRATCH_TEMPLATE_BASE_URL:-https://raw.githubusercontent.com/carthorsestudios/scratch-mmo-amp-template/$REF/control}
BOOTSTRAP_SHA256=a37fda82a0a234b77f97913f54471c4717b6cdb9f6589428d81f1499d2983ace
DEPLOY_SHA256=9b92377e9a5f99cf3f87f8cb5acbe3ee2c4c0ecd433c30cf2f9a36a9d61948e0

digest_of() {
	if command -v sha256sum >/dev/null 2>&1; then
		sha256sum "$1" | cut -d' ' -f1
	elif command -v shasum >/dev/null 2>&1; then
		shasum -a 256 "$1" | cut -d' ' -f1
	elif command -v python3 >/dev/null 2>&1; then
		python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
	else
		return 1
	fi
}

fetch_verified() {
	url=$1
	want=$2
	tmp=$3
	rm -f "$tmp"
	if command -v curl >/dev/null 2>&1; then
		curl -fsSL "$url" -o "$tmp" || return 1
	elif command -v wget >/dev/null 2>&1; then
		wget -qO "$tmp" "$url" || return 1
	else
		echo "ERROR: curl or wget required to install bootstrap files" >&2
		return 1
	fi
	test -s "$tmp" || { echo "ERROR: empty download: $url" >&2; return 1; }
	test -n "$want" || { echo "ERROR: missing SHA-256 pin for $url" >&2; return 1; }
	got=$(digest_of "$tmp") || { echo "ERROR: no SHA-256 tool available to verify $url" >&2; return 1; }
	test "$got" = "$want" || { echo "ERROR: SHA-256 mismatch for $url" >&2; return 1; }
}

TMP_BOOTSTRAP=$(mktemp control/.amp_bootstrap_start.sh.XXXXXX 2>/dev/null || true)
TMP_DEPLOY=$(mktemp control/.scratch_mmo_deploy_latest.py.XXXXXX 2>/dev/null || true)
install_ok=0
if test -n "$TMP_BOOTSTRAP" && test -n "$TMP_DEPLOY" &&
	fetch_verified "$BASE/amp_bootstrap_start.sh" "$BOOTSTRAP_SHA256" "$TMP_BOOTSTRAP" &&
	fetch_verified "$BASE/scratch_mmo_deploy_latest.py" "$DEPLOY_SHA256" "$TMP_DEPLOY"; then
	mv "$TMP_BOOTSTRAP" control/amp_bootstrap_start.sh
	mv "$TMP_DEPLOY" control/scratch_mmo_deploy_latest.py
	chmod +x control/amp_bootstrap_start.sh control/scratch_mmo_deploy_latest.py
	install_ok=1
fi
rm -f "$TMP_BOOTSTRAP" "$TMP_DEPLOY"

if test "$install_ok" -eq 0; then
	if test -s control/amp_bootstrap_start.sh; then
		echo "WARNING: verified bootstrap download unavailable; using existing control/amp_bootstrap_start.sh" >&2
	elif test -s current/scripts/amp_start.sh; then
		echo "WARNING: verified bootstrap download unavailable; falling back to current/scripts/amp_start.sh" >&2
		exec /bin/bash current/scripts/amp_start.sh
	else
		echo "ERROR: bootstrap could not be verified and no current release exists at current/scripts/amp_start.sh" >&2
		exit 1
	fi
fi

test -s control/amp_bootstrap_start.sh || { echo "ERROR: refusing to execute empty or missing control/amp_bootstrap_start.sh" >&2; exit 1; }
exec /bin/bash control/amp_bootstrap_start.sh
