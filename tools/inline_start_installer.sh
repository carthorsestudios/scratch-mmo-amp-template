#!/usr/bin/env bash
# Readable source for the AMP inline start installer embedded in scratchmmo.kvp.
# AMP does not copy template support files into instances; Start runs this logic via bash -lc base64 wrapper in scratchmmo.kvp.
#
# Trust model: the two control files are fetched into temporary files and are only
# installed once their SHA-256 digests match the pins below. Mutable raw `main`
# bytes are never executed unverified, and an unverifiable download always falls
# back to something already on disk instead of running new code.
#
# Pair atomicity: control/amp_bootstrap_start.sh and control/scratch_mmo_deploy_latest.py
# are a matched set. Both are downloaded, digest-verified and mode-verified in private
# temporary files before either authoritative path is touched; the previous pair is
# snapshotted first; and any failure during replacement, mode application or cleanup
# restores the complete previous pair. An instance therefore never ends up running a
# new bootstrap against an old shim (or the reverse), and a network or verification
# failure always leaves the previously working pair in place.
#
# BOOTSTRAP_SHA256 / DEPLOY_SHA256 are generated from the committed control/ files
# by tools/generate_bootstrap_pins.py. Never hand-edit them.
set -e

test ! -L control || { echo "ERROR: refusing symlinked control directory" >&2; exit 1; }
mkdir -p control
test -d control || { echo "ERROR: control exists but is not a directory" >&2; exit 1; }
chmod 700 control || echo "WARNING: could not restrict control/ directory permissions" >&2

REF=${SCRATCH_TEMPLATE_REF:-main}
BASE=${SCRATCH_TEMPLATE_BASE_URL:-https://raw.githubusercontent.com/carthorsestudios/scratch-mmo-amp-template/$REF/control}
BOOTSTRAP_SHA256=adbed9491554c00a982d29c34482831f3c689a48d0c48d2bb2755a86490ddfd0
DEPLOY_SHA256=5d37d781b35ce15f025518d55ffa8ca79662a2ab4e8c04a8795cd6297a7e150c
CONTROL_MODE=700
MODE_ENFORCED=0

# Test seam for the pair-install rollback paths. It can only ever abort an install
# (which falls back to the previously verified pair); it can never cause unverified
# bytes to be installed or executed.
INSTALL_FAULT=${SCRATCH_INSTALL_FAULT:-}
PAIR_HAD_BOOTSTRAP=0
PAIR_HAD_DEPLOY=0
PAIR_BACKUP_BOOTSTRAP=
PAIR_BACKUP_DEPLOY=

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

mode_of() {
	stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null
}

# Prove the filesystem actually honours modes before treating a mode read-back as
# authoritative. Linux instances always enforce; developer checkouts on filesystems
# without POSIX permissions degrade to "apply, warn, do not verify".
detect_mode_enforcement() {
	MODE_ENFORCED=0
	chmod "$CONTROL_MODE" "$1" 2>/dev/null || return 0
	probe=$(mode_of "$1" 2>/dev/null || printf '')
	test "$probe" = "$CONTROL_MODE" || return 0
	MODE_ENFORCED=1
}

apply_mode() {
	chmod "$CONTROL_MODE" "$1" || { echo "ERROR: required chmod $CONTROL_MODE failed for $1" >&2; return 1; }
	test "$MODE_ENFORCED" -eq 1 || return 0
	got=$(mode_of "$1") || { echo "ERROR: cannot verify mode for $1" >&2; return 1; }
	test "$got" = "$CONTROL_MODE" || { echo "ERROR: mode verification failed for $1: got $got, want $CONTROL_MODE" >&2; return 1; }
}

refuse_unsafe_control_path() {
	test ! -L "$1" || { echo "ERROR: refusing symlinked control file: $1" >&2; return 1; }
	test ! -e "$1" || test -f "$1" || { echo "ERROR: refusing non-regular control file: $1" >&2; return 1; }
}

discard() {
	test -z "$1" || rm -f "$1"
}

fault_stop() {
	test "$INSTALL_FAULT" = "$1" || return 0
	echo "ERROR: control pair install aborted at injected fault point: $1" >&2
	return 1
}

fault_path() {
	if test "$INSTALL_FAULT" = "$1"; then
		printf '%s' "control/.fault-no-such-dir/$2"
	else
		printf '%s' "$2"
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

# Snapshot both authoritative paths into private temporaries. Either the whole pair is
# captured or nothing is: a partial snapshot is abandoned before anything is replaced.
snapshot_control_pair() {
	if test -e control/amp_bootstrap_start.sh; then
		PAIR_BACKUP_BOOTSTRAP=$(mktemp control/.bak-amp_bootstrap_start.sh.XXXXXX) || return 1
		cat control/amp_bootstrap_start.sh > "$PAIR_BACKUP_BOOTSTRAP" || return 1
		apply_mode "$PAIR_BACKUP_BOOTSTRAP" || return 1
		PAIR_HAD_BOOTSTRAP=1
	fi
	if test -e control/scratch_mmo_deploy_latest.py; then
		PAIR_BACKUP_DEPLOY=$(mktemp control/.bak-scratch_mmo_deploy_latest.py.XXXXXX) || return 1
		cat control/scratch_mmo_deploy_latest.py > "$PAIR_BACKUP_DEPLOY" || return 1
		apply_mode "$PAIR_BACKUP_DEPLOY" || return 1
		PAIR_HAD_DEPLOY=1
	fi
}

abandon_snapshot() {
	echo "ERROR: could not snapshot the existing control pair; leaving it untouched" >&2
	discard "$PAIR_BACKUP_BOOTSTRAP"
	discard "$PAIR_BACKUP_DEPLOY"
	PAIR_BACKUP_BOOTSTRAP=
	PAIR_BACKUP_DEPLOY=
	PAIR_HAD_BOOTSTRAP=0
	PAIR_HAD_DEPLOY=0
}

restore_one() {
	backup=$1
	dest=$2
	existed=$3
	if test -n "$backup" && test -e "$backup"; then
		mv -f "$backup" "$dest" || echo "ERROR: could not restore $dest" >&2
	elif test "$existed" -eq 0; then
		rm -f "$dest"
	else
		echo "ERROR: no usable snapshot to restore $dest" >&2
	fi
}

# Put back exactly the pair that existed before this run: a file that was present is
# restored byte-for-byte, and a file that was absent is removed again.
restore_control_pair() {
	echo "WARNING: restoring the previous control pair after a failed install" >&2
	restore_one "$PAIR_BACKUP_BOOTSTRAP" control/amp_bootstrap_start.sh "$PAIR_HAD_BOOTSTRAP"
	restore_one "$PAIR_BACKUP_DEPLOY" control/scratch_mmo_deploy_latest.py "$PAIR_HAD_DEPLOY"
	PAIR_BACKUP_BOOTSTRAP=
	PAIR_BACKUP_DEPLOY=
}

install_control_pair() {
	tmp_bootstrap=$1
	tmp_deploy=$2
	detect_mode_enforcement "$tmp_bootstrap"
	test "$MODE_ENFORCED" -eq 1 || echo "WARNING: filesystem cannot enforce control file modes; applying without verification" >&2
	refuse_unsafe_control_path control/amp_bootstrap_start.sh || return 1
	refuse_unsafe_control_path control/scratch_mmo_deploy_latest.py || return 1
	apply_mode "$tmp_bootstrap" || return 1
	apply_mode "$tmp_deploy" || return 1
	snapshot_control_pair || { abandon_snapshot; return 1; }
	fault_stop before_first_replace || { restore_control_pair; return 1; }
	dest_bootstrap=$(fault_path during_first_replace control/amp_bootstrap_start.sh)
	mv -f "$tmp_bootstrap" "$dest_bootstrap" || { echo "ERROR: could not install control/amp_bootstrap_start.sh" >&2; restore_control_pair; return 1; }
	fault_stop after_first_replace || { restore_control_pair; return 1; }
	dest_deploy=$(fault_path during_second_replace control/scratch_mmo_deploy_latest.py)
	mv -f "$tmp_deploy" "$dest_deploy" || { echo "ERROR: could not install control/scratch_mmo_deploy_latest.py" >&2; restore_control_pair; return 1; }
	apply_mode "$(fault_path during_metadata control/amp_bootstrap_start.sh)" || { restore_control_pair; return 1; }
	apply_mode "$(fault_path during_metadata control/scratch_mmo_deploy_latest.py)" || { restore_control_pair; return 1; }
	fault_stop during_cleanup || { restore_control_pair; return 1; }
	discard "$PAIR_BACKUP_BOOTSTRAP"
	discard "$PAIR_BACKUP_DEPLOY"
	PAIR_BACKUP_BOOTSTRAP=
	PAIR_BACKUP_DEPLOY=
}

TMP_BOOTSTRAP=$(mktemp control/.tmp-amp_bootstrap_start.sh.XXXXXX 2>/dev/null || true)
TMP_DEPLOY=$(mktemp control/.tmp-scratch_mmo_deploy_latest.py.XXXXXX 2>/dev/null || true)
install_ok=0
if test -n "$TMP_BOOTSTRAP" && test -n "$TMP_DEPLOY" &&
	fetch_verified "$BASE/amp_bootstrap_start.sh" "$BOOTSTRAP_SHA256" "$TMP_BOOTSTRAP" &&
	fetch_verified "$BASE/scratch_mmo_deploy_latest.py" "$DEPLOY_SHA256" "$TMP_DEPLOY" &&
	install_control_pair "$TMP_BOOTSTRAP" "$TMP_DEPLOY"; then
	install_ok=1
fi
discard "$TMP_BOOTSTRAP"
discard "$TMP_DEPLOY"
discard "$PAIR_BACKUP_BOOTSTRAP"
discard "$PAIR_BACKUP_DEPLOY"

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
