#!/usr/bin/env bash
# Restart-triggered AMP bootstrap: hand the process off to the Python supervisor.
#
# This script deliberately does NOT supervise anything itself. On the supervised
# path it `exec`s the Python control-plane shim so AMP's direct child *is* the
# Python supervisor: no `tee`, no pipeline, no second launch of the release. That
# keeps signal delivery correct (SIGTERM reaches the supervisor, which forwards it
# to the exact current/scripts/amp_start.sh it launched) and means a candidate that
# failed but rolled back to a healthy previous release stays supervised by Python
# instead of being relaunched here.
#
# If no token/current release exists yet, a setup HTTP server holds the web port
# (default 9090).
set -euo pipefail

LOG_FILE=""

timestamp() {
	date -u +%Y-%m-%dT%H:%M:%SZ
}

log() {
	local line="[$(timestamp)] $*"
	printf '%s\n' "${line}"
	if [[ -n "${LOG_FILE}" ]]; then
		printf '%s\n' "${line}" >> "${LOG_FILE}"
	fi
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

stat_field() {
	local fmt="$1"
	local path="$2"
	if stat -c "${fmt}" "${path}" 2>/dev/null; then
		return 0
	fi
	case "${fmt}" in
	'%a') stat -f '%Lp' "${path}" 2>/dev/null && return 0 ;;
	'%u') stat -f '%u' "${path}" 2>/dev/null && return 0 ;;
	'%h') stat -f '%l' "${path}" 2>/dev/null && return 0 ;;
	esac
	return 1
}

# Prove a secret-bearing config file is safe before it is opened.
# 0 = safe to read, 1 = absent, 2 = refused. Config values are never printed.
secure_deploy_env_before_read() {
	local env_file="$1"
	local parent
	parent="$(dirname "${env_file}")"

	if [[ -L "${parent}" ]]; then
		log "REFUSING ${env_file}: parent directory is a symlink"
		return 2
	fi
	if [[ -e "${parent}" && ! -d "${parent}" ]]; then
		log "REFUSING ${env_file}: parent is not a directory"
		return 2
	fi
	if [[ -L "${env_file}" ]]; then
		log "REFUSING ${env_file}: config is a symlink"
		return 2
	fi
	if [[ ! -e "${env_file}" ]]; then
		return 1
	fi
	if [[ ! -f "${env_file}" ]]; then
		log "REFUSING ${env_file}: config is not a regular file"
		return 2
	fi

	local mode
	if ! mode="$(stat_field '%a' "${env_file}")"; then
		log "REFUSING ${env_file}: file metadata is unreadable (no usable stat)"
		return 2
	fi

	local links
	links="$(stat_field '%h' "${env_file}" || printf '1')"
	if [[ "${links}" =~ ^[0-9]+$ ]] && ((links > 1)); then
		log "REFUSING ${env_file}: config is hard linked (links=${links})"
		return 2
	fi

	local uid current_uid
	uid="$(stat_field '%u' "${env_file}" || printf '')"
	current_uid="$(id -u)"
	if [[ -n "${uid}" && "${uid}" != "${current_uid}" ]]; then
		log "REFUSING ${env_file}: not owned by the instance account (uid ${current_uid})"
		return 2
	fi

	if ((8#${mode} & 8#077)); then
		log "Repairing overly permissive config mode ${mode} -> 600 for ${env_file}"
		if ! chmod 600 "${env_file}"; then
			log "REFUSING ${env_file}: permissions could not be repaired"
			return 2
		fi
		if ! mode="$(stat_field '%a' "${env_file}")"; then
			log "REFUSING ${env_file}: mode could not be re-verified after repair"
			return 2
		fi
		if ((8#${mode} & 8#077)); then
			log "REFUSING ${env_file}: still group/world accessible after repair"
			return 2
		fi
	fi
	return 0
}

load_deploy_env_file() {
	local env_file="$1"
	local status=0
	secure_deploy_env_before_read "${env_file}" || status=$?
	if ((status == 1)); then
		return 0
	fi
	if ((status != 0)); then
		log "Skipping ${env_file}: refusing to read an unsafe secret-bearing config file"
		return 1
	fi

	log "Loading verified overrides from ${env_file} (existing env vars preserved; values never printed)"
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

run_setup_server() {
	local setup_mode="$1"
	local bind_address="${SCRATCH_BIND_ADDRESS:-0.0.0.0}"
	local web_port="${SCRATCH_WEB_PORT:-9090}"

	log "Entering setup holding mode (${setup_mode}) on ${bind_address}:${web_port}"
	export SCRATCH_SETUP_MODE="${setup_mode}"
	export SCRATCH_BIND_ADDRESS="${bind_address}"
	export SCRATCH_WEB_PORT="${web_port}"

	exec python3 -u - <<'PY'
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = os.environ.get("SCRATCH_BIND_ADDRESS", "0.0.0.0")
PORT = int(os.environ.get("SCRATCH_WEB_PORT", "9090"))
MODE = os.environ.get("SCRATCH_SETUP_MODE", "token_required")

MESSAGES = {
    "token_required": (
        "Scratch MMO setup required.\n\n"
        "Enter the AMP setting \"GitHub Release Token\" (read-only token for "
        "carthorsestudios/scratch-mmo release downloads), save the instance "
        "configuration, then Restart this instance.\n\n"
        "Do not put the GitHub token in Invite Code.\n"
    ),
    "deploy_failed": (
        "Scratch MMO release deploy failed and no current release is installed.\n\n"
        "Check scratchmmo-bootstrap.log in the instance root, fix the GitHub Release "
        "Token or release access, then Restart.\n"
    ),
}


class SetupHandler(BaseHTTPRequestHandler):
    server_version = "ScratchMMOSetup/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[ScratchMMO] {self.address_string()} - {fmt % args}", flush=True)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send_json(
                503,
                {
                    "status": "setup_required",
                    "mode": MODE,
                    "service": "scratch-mmo-setup",
                    "web_port": PORT,
                },
            )
            return
        if path == "/version":
            self._send_json(
                200,
                {
                    "status": "setup_required",
                    "mode": MODE,
                    "service": "scratch-mmo-setup",
                    "release": "not-deployed",
                },
            )
            return

        message = MESSAGES.get(MODE, MESSAGES["token_required"])
        body = (
            "<!DOCTYPE html><html><head><title>Scratch MMO Setup Required</title></head>"
            f"<body><pre>{message}</pre></body></html>"
        ).encode("utf-8")
        self.send_response(503)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer((BIND, PORT), SetupHandler)
    print(f"[ScratchMMO] Setup server listening port={PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
PY
}

ROOT="$(resolve_deploy_root)"
export SCRATCH_DEPLOY_ROOT="${ROOT}"
CONTROL_DIR="${ROOT}/control"
CURRENT_START="${ROOT}/current/scripts/amp_start.sh"
DEPLOY_SCRIPT="${CONTROL_DIR}/scratch_mmo_deploy_latest.py"
BOOTSTRAP_LOG="${ROOT}/scratchmmo-bootstrap.log"

if [[ -L "${BOOTSTRAP_LOG}" ]]; then
	printf '%s\n' "[$(timestamp)] REFUSING symlinked bootstrap log; console-only logging: ${BOOTSTRAP_LOG}"
elif [[ -e "${BOOTSTRAP_LOG}" && ! -f "${BOOTSTRAP_LOG}" ]]; then
	printf '%s\n' "[$(timestamp)] REFUSING non-regular bootstrap log; console-only logging: ${BOOTSTRAP_LOG}"
else
	: > "${BOOTSTRAP_LOG}"
	if ! chmod 600 "${BOOTSTRAP_LOG}"; then
		printf '%s\n' "[$(timestamp)] WARNING: could not restrict bootstrap log permissions"
	fi
	LOG_FILE="${BOOTSTRAP_LOG}"
fi

log "==== Scratch MMO AMP bootstrap start ===="
log "ROOT=${ROOT}"
log "CONTROL_DIR=${CONTROL_DIR}"
log "DEPLOY_SCRIPT=${DEPLOY_SCRIPT}"
log "Token configured: $([[ -n "${SCRATCH_GITHUB_TOKEN:-}" ]] && echo yes || echo no)"

load_deploy_env_file "${CONTROL_DIR}/deploy.env" || true
log "Token configured after deploy.env: $([[ -n "${SCRATCH_GITHUB_TOKEN:-}" ]] && echo yes || echo no)"

if [[ ! -f "${CURRENT_START}" && -z "${SCRATCH_GITHUB_TOKEN:-}" ]]; then
	log "No GitHub token and no current release; starting setup holding server"
	run_setup_server "token_required"
fi

if [[ -n "${SCRATCH_GITHUB_TOKEN:-}" && -f "${DEPLOY_SCRIPT}" ]]; then
	log "Replacing bootstrap with the Python release supervisor (exec, no pipeline)"
	if [[ -n "${LOG_FILE}" ]]; then
		export SCRATCH_BOOTSTRAP_LOG_FILE="${LOG_FILE}"
	fi
	# Dual console/file logging happens inside Python so this process can be
	# replaced outright: nothing after this line runs on the supervised path.
	exec python3 -u "${DEPLOY_SCRIPT}" --deploy --supervise --yes
fi

if [[ -z "${SCRATCH_GITHUB_TOKEN:-}" ]]; then
	log "No GitHub token configured; skipping private release download"
else
	log "WARNING: missing ${DEPLOY_SCRIPT}; skipping auto-update"
fi

if [[ -e "${CURRENT_START}" ]]; then
	if [[ -L "${CURRENT_START}" ]]; then
		log "REFUSING symlinked start script: ${CURRENT_START}"
	elif [[ ! -f "${CURRENT_START}" ]]; then
		log "REFUSING non-regular start script: ${CURRENT_START}"
	elif [[ ! -x "${CURRENT_START}" ]] && ! chmod +x "${CURRENT_START}"; then
		log "ERROR: ${CURRENT_START} is not executable and could not be repaired"
	else
		log "No Python supervisor available; starting committed release directly: ${CURRENT_START}"
		exec "${CURRENT_START}" "$@"
	fi
fi

if [[ -n "${SCRATCH_GITHUB_TOKEN:-}" ]]; then
	log "No supervisor and no current release; starting setup holding server"
	run_setup_server "deploy_failed"
fi

log "No GitHub token and no current release; starting setup holding server"
run_setup_server "token_required"
