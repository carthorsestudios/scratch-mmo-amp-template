#!/usr/bin/env bash
# Restart-triggered AMP bootstrap: optional GitHub release swap, then start the game.
set -euo pipefail

timestamp() {
	date -u +%Y-%m-%dT%H%M:%SZ
}

resolve_deploy_root() {
	if [[ -n "${SCRATCH_DEPLOY_ROOT:-}" ]]; then
		printf '%s\n' "${SCRATCH_DEPLOY_ROOT}"
		return 0
	fi

	local script_dir
	script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
	if [[ "$(basename "${script_dir}")" == "control" ]]; then
		cd "${script_dir}/.." && pwd
		return 0
	fi

	pwd
}

load_deploy_env_file() {
	local env_file="$1"
	[[ -f "${env_file}" ]] || return 0
	log "Loading optional overrides from ${env_file} (existing env vars are preserved)"
	while IFS= read -r raw_line || [[ -n "${raw_line}" ]]; do
		local line="${raw_line#"${raw_line%%[![:space:]]*}"}"
		line="${line%"${line##*[![:space:]]}"}"
		[[ -z "${line}" || "${line}" == \#* ]] && continue
		[[ "${line}" == export\ * ]] && line="${line#export }"
		[[ "${line}" == *"="* ]] || continue
		local key="${line%%=*}"
		key="${key#"${key%%[![:space:]]*}"}"
		key="${key%"${key##*[![:space:]]}"}"
		local value="${line#*=}"
		value="${value#"${value%%[![:space:]]*}"}"
		value="${value%"${value##*[![:space:]]}"}"
		value="${value#\"}"
		value="${value%\"}"
		value="${value#\'}"
		value="${value%\'}"
		if [[ -n "${key}" && -z "${!key:-}" ]]; then
			export "${key}=${value}"
		fi
	done < "${env_file}"
}

ROOT="$(resolve_deploy_root)"
export SCRATCH_DEPLOY_ROOT="${ROOT}"
CONTROL_DIR="${ROOT}/control"
CURRENT_START="${ROOT}/current/scripts/amp_start.sh"
LOG_FILE="${ROOT}/scratchmmo-bootstrap.log"
DEPLOY_SCRIPT="${CONTROL_DIR}/scratch_mmo_deploy_latest.py"

log() {
	echo "[$(timestamp)] $*" | tee -a "${LOG_FILE}"
}

: > "${LOG_FILE}"
log "==== Scratch MMO AMP bootstrap start ===="
log "ROOT=${ROOT}"
log "CONTROL_DIR=${CONTROL_DIR}"
log "DEPLOY_SCRIPT=${DEPLOY_SCRIPT}"

load_deploy_env_file "${CONTROL_DIR}/deploy.env"

UPDATER_EXIT=0
if [[ -f "${DEPLOY_SCRIPT}" ]]; then
	log "Running restart-triggered deploy check"
	if python3 "${DEPLOY_SCRIPT}" --deploy --yes >> "${LOG_FILE}" 2>&1; then
		log "Deploy updater finished successfully (or already current)"
	else
		UPDATER_EXIT=$?
		log "Deploy updater failed (exit=${UPDATER_EXIT}); keeping existing current/ if present"
	fi
else
	log "WARNING: missing ${DEPLOY_SCRIPT}; skipping auto-update"
fi

if [[ ! -f "${CURRENT_START}" ]]; then
	log "ERROR: missing game start script: ${CURRENT_START}"
	exit 127
fi

chmod +x "${CURRENT_START}" 2>/dev/null || true
log "Starting ${CURRENT_START}"
exec "${CURRENT_START}" "$@"
