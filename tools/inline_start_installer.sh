#!/usr/bin/env bash
# Readable source for the AMP inline start installer embedded in scratchmmo.kvp.
# AMP does not copy template support files into instances; Start runs this logic via bash -lc base64 wrapper in scratchmmo.kvp.
set -e

mkdir -p control
BASE=https://raw.githubusercontent.com/carthorsestudios/scratch-mmo-amp-template/main/control

fetch() {
	url=$1
	dest=$2
	tmp=$(mktemp)
	if command -v curl >/dev/null 2>&1; then
		curl -fsSL "$url" -o "$tmp"
	elif command -v wget >/dev/null 2>&1; then
		wget -qO "$tmp" "$url"
	else
		echo "ERROR: curl or wget required to install bootstrap files" >&2
		return 1
	fi
	test -s "$tmp" || {
		echo "ERROR: empty download: $url" >&2
		rm -f "$tmp"
		return 1
	}
	mv "$tmp" "$dest"
}

install_ok=0
if fetch "$BASE/amp_bootstrap_start.sh" control/amp_bootstrap_start.sh &&
	fetch "$BASE/scratch_mmo_deploy_latest.py" control/scratch_mmo_deploy_latest.py; then
	chmod +x control/amp_bootstrap_start.sh control/scratch_mmo_deploy_latest.py
	install_ok=1
fi

if test "$install_ok" -eq 0; then
	if test -f control/amp_bootstrap_start.sh; then
		echo "WARNING: bootstrap download failed; using existing control/amp_bootstrap_start.sh" >&2
	elif test -f current/scripts/amp_start.sh; then
		echo "WARNING: bootstrap download failed; falling back to current/scripts/amp_start.sh" >&2
		exec /bin/bash current/scripts/amp_start.sh
	else
		echo "ERROR: bootstrap could not be installed and no current release exists at current/scripts/amp_start.sh" >&2
		exit 1
	fi
fi

exec /bin/bash control/amp_bootstrap_start.sh
