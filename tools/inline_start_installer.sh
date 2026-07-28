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
# snapshotted (with digests) first; and any failure during replacement, mode application
# or cleanup restores the complete previous pair. An instance therefore never ends up
# running a new bootstrap against an old shim (or the reverse), and a network or
# verification failure always leaves the previously working pair in place.
#
# Verified restoration: rolling back is itself transactional. The snapshot records
# presence and SHA-256 for each authoritative file and proves each backup copy matches
# what it copied. Restoration puts presence/absence back exactly, reapplies and verifies
# the required mode, re-hashes every restored file, and then re-proves the whole pair
# against the snapshot. Nothing under control/ is executed unless the complete control
# state is proven coherent: either the freshly installed pinned pair, or a fully
# verified restoration of the pair that existed before this run. When restoration cannot
# be proven, a non-empty bootstrap on disk is *not* accepted as evidence of safety; the
# only remaining option is a current/scripts/amp_start.sh that passes the safety policy,
# and otherwise Start fails loudly.
#
# BOOTSTRAP_SHA256 / DEPLOY_SHA256 are generated from the committed control/ files
# by tools/generate_bootstrap_pins.py. Never hand-edit them.
set -e

REF=${SCRATCH_TEMPLATE_REF:-main}
BASE=${SCRATCH_TEMPLATE_BASE_URL:-https://raw.githubusercontent.com/carthorsestudios/scratch-mmo-amp-template/$REF/control}
BOOTSTRAP_SHA256=adbed9491554c00a982d29c34482831f3c689a48d0c48d2bb2755a86490ddfd0
DEPLOY_SHA256=e30fdaed57be70e43764a0130621b604701f2fcbe44cec1c90fafd4a541af8b4
CONTROL_MODE=700
MODE_ENFORCED=0
REQUIRE_MODES=0
NULL_DIGEST=0000000000000000000000000000000000000000000000000000000000000000

# Test seam for the pair-install and restoration rollback paths. Every injection point
# can only ever abort work or make a verification fail; none of them can cause
# unverified bytes to be installed or executed.
INSTALL_FAULT=${SCRATCH_INSTALL_FAULT:-}
PAIR_HAD_BOOTSTRAP=0
PAIR_HAD_DEPLOY=0
PAIR_BACKUP_BOOTSTRAP=
PAIR_BACKUP_DEPLOY=
PAIR_DIGEST_BOOTSTRAP=
PAIR_DIGEST_DEPLOY=
RESTORE_ATTEMPTED=0
RESTORE_VERIFIED=0

# AMP instances are Linux, where POSIX modes are always enforced. Anywhere else this
# is a developer checkout and mode hardening degrades to "apply, warn, do not verify".
if test "$(uname -s 2>/dev/null || printf unknown)" = Linux; then
	REQUIRE_MODES=1
fi

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

link_count_of() {
	stat -c '%h' "$1" 2>/dev/null || stat -f '%l' "$1" 2>/dev/null
}

# Prove the filesystem actually honours modes before treating a mode read-back as
# authoritative. Linux instances always enforce; developer checkouts on filesystems
# without POSIX permissions degrade to "apply, warn, do not verify". Enforcement is
# never downgraded once proven.
detect_mode_enforcement() {
	test "$MODE_ENFORCED" -eq 0 || return 0
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

# Authoritative control paths must be plain, privately owned regular files. A symlink,
# a device/directory, or an extra hard link means something else can observe or swap
# the bytes we are about to execute, so the pair is refused outright.
refuse_unsafe_control_path() {
	test ! -L "$1" || { echo "ERROR: refusing symlinked control file: $1" >&2; return 1; }
	test ! -e "$1" || test -f "$1" || { echo "ERROR: refusing non-regular control file: $1" >&2; return 1; }
	test -e "$1" || return 0
	links=$(link_count_of "$1" 2>/dev/null || printf '')
	test -n "$links" || return 0
	test "$links" -le 1 || { echo "ERROR: refusing hard-linked control file: $1 has $links links" >&2; return 1; }
}

mode_allows_foreign_write() {
	tail2=${1#"${1%??}"}
	grp=${tail2%?}
	oth=${tail2#?}
	test "$grp" = 2 || test "$grp" = 3 || test "$grp" = 6 || test "$grp" = 7 || test "$oth" = 2 || test "$oth" = 3 || test "$oth" = 6 || test "$oth" = 7
}

# The documented last-resort fallback. It is only allowed for a release start script
# that is a plain non-empty regular file which no other user can rewrite.
current_start_is_safe() {
	test ! -L current/scripts || { echo "ERROR: refusing symlinked current/scripts" >&2; return 1; }
	test ! -L current/scripts/amp_start.sh || { echo "ERROR: refusing symlinked current/scripts/amp_start.sh" >&2; return 1; }
	test -f current/scripts/amp_start.sh || return 1
	test -s current/scripts/amp_start.sh || { echo "ERROR: refusing empty current/scripts/amp_start.sh" >&2; return 1; }
	test "$MODE_ENFORCED" -eq 1 || return 0
	start_mode=$(mode_of current/scripts/amp_start.sh) || { echo "ERROR: cannot verify mode for current/scripts/amp_start.sh" >&2; return 1; }
	mode_allows_foreign_write "$start_mode" || return 0
	echo "ERROR: refusing group/world-writable current/scripts/amp_start.sh (mode $start_mode)" >&2
	return 1
}

# The bootstrap immediately hands off to the shim beside it, so both authoritative
# paths have to be safe before either is allowed to run.
bootstrap_is_executable() {
	test -e control/amp_bootstrap_start.sh || return 1
	refuse_unsafe_control_path control/amp_bootstrap_start.sh || return 1
	refuse_unsafe_control_path control/scratch_mmo_deploy_latest.py || return 1
	test -s control/amp_bootstrap_start.sh || { echo "ERROR: refusing to execute empty or missing control/amp_bootstrap_start.sh" >&2; return 1; }
}

discard() {
	test -z "$1" || rm -f "$1"
}

# A snapshot is only released once the pair it protects is proven good. A failure to
# remove it is logged and the backup is left in place, still named control/.bak-*, so
# it stays identifiable; it never invalidates an already-verified pair.
retire_backup() {
	test -n "$1" || return 0
	test -e "$1" || return 0
	if test "$INSTALL_FAULT" = restore_cleanup; then
		echo "WARNING: could not remove control snapshot $1; leaving it in place for inspection" >&2
		return 1
	fi
	rm -f "$1" || { echo "WARNING: could not remove control snapshot $1; leaving it in place for inspection" >&2; return 1; }
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

# Substitutes a digest nothing can ever hash to, so an injected fault can only make a
# verification fail. It can never make an unverified file look verified.
fault_digest() {
	if test "$INSTALL_FAULT" = "$1"; then
		printf '%s' "$NULL_DIGEST"
	else
		printf '%s' "$2"
	fi
}

is_restore_fault() {
	test "$1" = restore_first || test "$1" = restore_second || test "$1" = restore_mode || test "$1" = restore_digest || test "$1" = restore_cleanup
}

fault_restore_trigger() {
	is_restore_fault "$INSTALL_FAULT" || return 0
	echo "ERROR: control pair install aborted to exercise restoration fault: $INSTALL_FAULT" >&2
	return 1
}

# control/ holds the only code this instance executes before the verified release takes
# over, so on Linux/AMP an unrestrictable or unverifiable control directory is fatal.
secure_control_dir() {
	test ! -L control || { echo "ERROR: refusing symlinked control directory" >&2; exit 1; }
	mkdir -p control || { echo "ERROR: could not create control directory" >&2; exit 1; }
	test -d control || { echo "ERROR: control exists but is not a directory" >&2; exit 1; }
	dir_secured=0
	if test "$INSTALL_FAULT" = control_dir_mode; then
		echo "ERROR: control directory hardening aborted at injected fault point: control_dir_mode" >&2
	elif chmod "$CONTROL_MODE" control 2>/dev/null; then
		dir_mode=$(mode_of control 2>/dev/null || printf '')
		if test "$dir_mode" = "$CONTROL_MODE"; then
			dir_secured=1
			MODE_ENFORCED=1
		fi
	fi
	test "$dir_secured" -eq 0 || return 0
	if test "$REQUIRE_MODES" -eq 1; then
		echo "ERROR: could not restrict and verify control/ directory permissions on Linux; refusing to continue" >&2
		exit 1
	fi
	echo "WARNING: could not restrict control/ directory permissions" >&2
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

verify_backup() {
	got=$(digest_of "$1") || { echo "ERROR: cannot hash control snapshot $1" >&2; return 1; }
	test "$got" = "$2" || { echo "ERROR: control snapshot $1 does not match the file it copied" >&2; return 1; }
}

verify_installed() {
	test ! -L "$1" || { echo "ERROR: refusing symlinked control file: $1" >&2; return 1; }
	test -f "$1" || { echo "ERROR: installed control file is missing: $1" >&2; return 1; }
	got=$(digest_of "$1") || { echo "ERROR: cannot hash installed control file $1" >&2; return 1; }
	test "$got" = "$2" || { echo "ERROR: installed $1 does not match its pinned digest" >&2; return 1; }
}

# Snapshot both authoritative paths into private temporaries. Presence and SHA-256 are
# recorded first, every backup copy is proven byte-identical to the file it copied, and
# a partial snapshot is abandoned before anything is replaced.
snapshot_control_pair() {
	if test -e control/amp_bootstrap_start.sh; then
		PAIR_DIGEST_BOOTSTRAP=$(digest_of control/amp_bootstrap_start.sh) || return 1
		PAIR_BACKUP_BOOTSTRAP=$(mktemp control/.bak-amp_bootstrap_start.sh.XXXXXX) || return 1
		cat control/amp_bootstrap_start.sh > "$PAIR_BACKUP_BOOTSTRAP" || return 1
		apply_mode "$PAIR_BACKUP_BOOTSTRAP" || return 1
		verify_backup "$PAIR_BACKUP_BOOTSTRAP" "$PAIR_DIGEST_BOOTSTRAP" || return 1
		PAIR_HAD_BOOTSTRAP=1
	fi
	if test -e control/scratch_mmo_deploy_latest.py; then
		PAIR_DIGEST_DEPLOY=$(digest_of control/scratch_mmo_deploy_latest.py) || return 1
		PAIR_BACKUP_DEPLOY=$(mktemp control/.bak-scratch_mmo_deploy_latest.py.XXXXXX) || return 1
		cat control/scratch_mmo_deploy_latest.py > "$PAIR_BACKUP_DEPLOY" || return 1
		apply_mode "$PAIR_BACKUP_DEPLOY" || return 1
		verify_backup "$PAIR_BACKUP_DEPLOY" "$PAIR_DIGEST_DEPLOY" || return 1
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
	PAIR_DIGEST_BOOTSTRAP=
	PAIR_DIGEST_DEPLOY=
}

# Put one authoritative path back exactly as the snapshot found it. The backup is copied
# rather than moved, so it survives until the restoration is proven. Any failure to
# restore content, mode, or digest is reported to the caller.
restore_one() {
	backup=$1
	dest=$2
	existed=$3
	want=$4
	fault=$5
	if test "$existed" -eq 0; then
		rm -f "$dest" || { echo "ERROR: could not remove $dest while restoring its prior absence" >&2; return 1; }
		test ! -e "$dest" || { echo "ERROR: $dest still exists after restoring its prior absence" >&2; return 1; }
		return 0
	fi
	src=$(fault_path "$fault" "$backup")
	test -n "$backup" || { echo "ERROR: no usable snapshot to restore $dest" >&2; return 1; }
	test -f "$src" || { echo "ERROR: no usable snapshot to restore $dest" >&2; return 1; }
	cat "$src" > "$dest" || { echo "ERROR: could not restore $dest" >&2; return 1; }
	apply_mode "$(fault_path restore_mode "$dest")" || { echo "ERROR: could not reapply the required mode while restoring $dest" >&2; return 1; }
	got=$(digest_of "$dest") || { echo "ERROR: cannot re-hash restored $dest" >&2; return 1; }
	test "$got" = "$(fault_digest restore_digest "$want")" || { echo "ERROR: restored $dest does not match its snapshot digest" >&2; return 1; }
	test -s "$dest" || { echo "ERROR: restored $dest is empty" >&2; return 1; }
}

# Independent re-proof of the whole pair after every move is done.
verify_snapshot_state() {
	path=$1
	existed=$2
	want=$3
	if test "$existed" -eq 0; then
		test ! -e "$path" || { echo "ERROR: $path exists but was absent before this run" >&2; return 1; }
		return 0
	fi
	test ! -L "$path" || { echo "ERROR: refusing symlinked control file: $path" >&2; return 1; }
	test -f "$path" || { echo "ERROR: $path is missing after restoration" >&2; return 1; }
	got=$(digest_of "$path") || { echo "ERROR: cannot hash restored $path" >&2; return 1; }
	test "$got" = "$want" || { echo "ERROR: $path does not match its pre-install snapshot digest" >&2; return 1; }
}

verify_restored_state() {
	verify_snapshot_state control/amp_bootstrap_start.sh "$PAIR_HAD_BOOTSTRAP" "$PAIR_DIGEST_BOOTSTRAP" || return 1
	verify_snapshot_state control/scratch_mmo_deploy_latest.py "$PAIR_HAD_DEPLOY" "$PAIR_DIGEST_DEPLOY" || return 1
}

# Put back exactly the pair that existed before this run and prove it: a file that was
# present is restored byte-for-byte with its required mode, and a file that was absent
# is removed again. Returns nonzero unless the complete previous state is proven, which
# is the only thing that makes the previous bootstrap eligible to run.
restore_control_pair() {
	echo "WARNING: restoring the previous control pair after a failed install" >&2
	RESTORE_ATTEMPTED=1
	RESTORE_VERIFIED=0
	restore_rc=0
	restore_one "$PAIR_BACKUP_BOOTSTRAP" control/amp_bootstrap_start.sh "$PAIR_HAD_BOOTSTRAP" "$PAIR_DIGEST_BOOTSTRAP" restore_first || restore_rc=1
	restore_one "$PAIR_BACKUP_DEPLOY" control/scratch_mmo_deploy_latest.py "$PAIR_HAD_DEPLOY" "$PAIR_DIGEST_DEPLOY" restore_second || restore_rc=1
	if test "$restore_rc" -ne 0; then
		echo "ERROR: the previous control pair could not be restored; leaving the snapshots in control/ for inspection" >&2
		return 1
	fi
	verify_restored_state || { echo "ERROR: restored control pair failed its snapshot re-verification; leaving the snapshots in control/ for inspection" >&2; return 1; }
	RESTORE_VERIFIED=1
	retire_backup "$PAIR_BACKUP_BOOTSTRAP" || true
	retire_backup "$PAIR_BACKUP_DEPLOY" || true
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
	fault_stop before_first_replace || { restore_control_pair || true; return 1; }
	dest_bootstrap=$(fault_path during_first_replace control/amp_bootstrap_start.sh)
	mv -f "$tmp_bootstrap" "$dest_bootstrap" || { echo "ERROR: could not install control/amp_bootstrap_start.sh" >&2; restore_control_pair || true; return 1; }
	fault_stop after_first_replace || { restore_control_pair || true; return 1; }
	fault_restore_trigger || { restore_control_pair || true; return 1; }
	dest_deploy=$(fault_path during_second_replace control/scratch_mmo_deploy_latest.py)
	mv -f "$tmp_deploy" "$dest_deploy" || { echo "ERROR: could not install control/scratch_mmo_deploy_latest.py" >&2; restore_control_pair || true; return 1; }
	apply_mode "$(fault_path during_metadata control/amp_bootstrap_start.sh)" || { restore_control_pair || true; return 1; }
	apply_mode "$(fault_path during_metadata control/scratch_mmo_deploy_latest.py)" || { restore_control_pair || true; return 1; }
	verify_installed control/amp_bootstrap_start.sh "$BOOTSTRAP_SHA256" || { restore_control_pair || true; return 1; }
	verify_installed control/scratch_mmo_deploy_latest.py "$DEPLOY_SHA256" || { restore_control_pair || true; return 1; }
	fault_stop during_cleanup || { restore_control_pair || true; return 1; }
	retire_backup "$PAIR_BACKUP_BOOTSTRAP" || true
	retire_backup "$PAIR_BACKUP_DEPLOY" || true
	PAIR_BACKUP_BOOTSTRAP=
	PAIR_BACKUP_DEPLOY=
}

secure_control_dir

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

# Coherent state 1: the pinned pair was installed and re-verified in place.
if test "$install_ok" -eq 1; then
	bootstrap_is_executable || { echo "ERROR: the freshly installed control bootstrap is not safe to execute" >&2; exit 1; }
	exec /bin/bash control/amp_bootstrap_start.sh
fi

# Incoherent state: something was replaced and the rollback could not be proven. A
# non-empty control/amp_bootstrap_start.sh is not evidence here, because it may be the
# new bootstrap stranded next to the old shim.
if test "$RESTORE_ATTEMPTED" -eq 1 && test "$RESTORE_VERIFIED" -ne 1; then
	echo "ERROR: the previous control pair could not be verifiably restored; refusing to execute any control file" >&2
	if current_start_is_safe; then
		echo "WARNING: falling back to the already-installed current/scripts/amp_start.sh" >&2
		exec /bin/bash current/scripts/amp_start.sh
	fi
	echo "ERROR: no safe current/scripts/amp_start.sh to fall back to after an unproven control restoration" >&2
	exit 1
fi

# Coherent state 2: the pair that existed before this run is proven back in place.
if test "$RESTORE_VERIFIED" -eq 1; then
	if test "$PAIR_HAD_BOOTSTRAP" -eq 1; then
		bootstrap_is_executable || { echo "ERROR: the restored control bootstrap is not safe to execute" >&2; exit 1; }
		echo "WARNING: verified control pair install failed; reusing the restored previous pair" >&2
		exec /bin/bash control/amp_bootstrap_start.sh
	fi
	if current_start_is_safe; then
		echo "WARNING: no previous control bootstrap to restore; falling back to current/scripts/amp_start.sh" >&2
		exec /bin/bash current/scripts/amp_start.sh
	fi
	echo "ERROR: no previous control bootstrap was restored and no safe current release exists at current/scripts/amp_start.sh" >&2
	exit 1
fi

# Coherent state 3: no authoritative path was ever touched, so whatever is on disk is
# exactly the pair the instance was already running.
if bootstrap_is_executable; then
	echo "WARNING: verified bootstrap download unavailable; using existing control/amp_bootstrap_start.sh" >&2
	exec /bin/bash control/amp_bootstrap_start.sh
fi
if current_start_is_safe; then
	echo "WARNING: verified bootstrap download unavailable; falling back to current/scripts/amp_start.sh" >&2
	exec /bin/bash current/scripts/amp_start.sh
fi
echo "ERROR: bootstrap could not be verified and no current release exists at current/scripts/amp_start.sh" >&2
exit 1
