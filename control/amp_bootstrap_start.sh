#!/usr/bin/env bash
# Restart-triggered AMP bootstrap: optional GitHub release swap, then start the game.
# If no token/current release yet, runs a setup HTTP server on the web port (default 9090).
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
log "Token configured: $([[ -n "${SCRATCH_GITHUB_TOKEN:-}" ]] && echo yes || echo no)"

load_deploy_env_file "${CONTROL_DIR}/deploy.env"
log "Token configured after deploy.env: $([[ -n "${SCRATCH_GITHUB_TOKEN:-}" ]] && echo yes || echo no)"

if [[ ! -f "${CURRENT_START}" && -z "${SCRATCH_GITHUB_TOKEN:-}" ]]; then
	log "No GitHub token and no current release; starting setup holding server"
	run_setup_server "token_required"
fi

UPDATER_EXIT=0
if [[ -n "${SCRATCH_GITHUB_TOKEN:-}" && -f "${DEPLOY_SCRIPT}" ]]; then
	log "Running restart-triggered deploy check"
	if python3 "${DEPLOY_SCRIPT}" --deploy --yes >> "${LOG_FILE}" 2>&1; then
		log "Deploy updater finished successfully (or already current)"
	else
		UPDATER_EXIT=$?
		log "Deploy updater failed (exit=${UPDATER_EXIT}); keeping existing current/ if present"
	fi
elif [[ -z "${SCRATCH_GITHUB_TOKEN:-}" ]]; then
	log "No GitHub token configured; skipping private release download"
else
	log "WARNING: missing ${DEPLOY_SCRIPT}; skipping auto-update"
fi

if [[ -f "${CURRENT_START}" ]]; then
	chmod +x "${CURRENT_START}" 2>/dev/null || true
	log "Starting ${CURRENT_START}"
	exec "${CURRENT_START}" "$@"
fi

if [[ -n "${SCRATCH_GITHUB_TOKEN:-}" ]]; then
	log "Deploy failed and no current release; starting setup holding server"
	run_setup_server "deploy_failed"
fi

log "No GitHub token and no current release; starting setup holding server"
run_setup_server "token_required"
