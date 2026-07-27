# Scratch MMO — AMP Generic Module Template

Public [AMP](https://cubecoders.com/AMP) Generic Module template for the Scratch MMO Godot dedicated server. Linux (x86_64) only, for Docker-backed AMP hosts such as Ubuntu 24.04.

This repository contains **only** AMP template files — no gameplay source, **no GitHub tokens**, and **no release zip**. Release binaries are downloaded from the private `carthorsestudios/scratch-mmo` GitHub Releases on **Start/Restart**.

**Do not use AMP Update.** Start runs a small inline installer that verifies and installs the two `control/` files, then runs `control/amp_bootstrap_start.sh`, which hands off to the deployment engine shipped inside the verified private release.

## Trust model

Every byte that gets executed is checked against something the template already committed or the private release already signed for:

1. **Pinned bootstrap.** The Start command embeds SHA-256 pins for `control/amp_bootstrap_start.sh` and `control/scratch_mmo_deploy_latest.py`. The installer downloads both to temporary files, hashes them, and installs them **only** when both digests match the pins. Mutable `main` bytes are never executed unverified.
2. **Atomic control-pair install.** The two `control/` files are one matched pair and are replaced as a unit. Both downloads are digest-verified and mode-verified in private temporary files inside `control/`, the previous pair is snapshotted first, and any failure while replacing either file, applying the final permissions, or cleaning up restores the **complete** previous pair. An instance can never end up with a new bootstrap next to an old shim (or the reverse), and a network or verification failure leaves the previously working pair in place and usable.
3. **Release-bound deployment.** `control/scratch_mmo_deploy_latest.py` is a thin compatibility shim. It selects a release, requires exactly the three publish assets (`mmo_release.zip`, `release_manifest.json`, `checksums.sha256`), proves the external manifest/checksums are byte-identical to their ZIP members, verifies every ZIP member against its recorded SHA-256, and only then hands off to `deployment/amp/amp_release_updater.py` **from that verified release**. The shim contains no independent swap engine: a release without the engine leaves an existing `current/` untouched.
4. **Exec supervision, no `tee`.** The bootstrap does not supervise anything itself. On the supervised path it `exec`s into the Python shim, so AMP's direct child *is* the Python supervisor — no pipeline, no `tee`, no second launch of the release. `App.ExitMethod=SIGTERM` therefore reaches the supervisor, which forwards it to the exact `current/scripts/amp_start.sh` it started, and the whole tree exits without orphans. Bootstrap log mirroring happens inside Python instead of through a shell pipe.
5. **Already current means no download.** When `current/` already ships a checksum-verified AMP engine, the shim executes that engine directly: no GitHub download, no ZIP extraction, and no new staging directory. Only a first install or a legacy pre-engine release falls back to bootstrap staging.
6. **Bounded control-engine staging.** Bootstrap staging extracts *only* the verified control modules into `state/control-engine/<release-id>/` — never the whole game release. Staging is capped at the active set plus one rollback set, interrupted attempts are cleaned up on the next Restart, and anything unrecognised is quarantined rather than deleted.
7. **Automatic rollback.** The release engine deploys the candidate and health-checks it; rollback to the previously deployed release is automatic when the candidate is unhealthy, with no operator action.

The pins are generated, never hand-written:

```bash
python tools/generate_bootstrap_pins.py     # digests from committed control/ files
python tools/emit_start_command.py --write-kvp   # refresh pins + base64 Start command
python tools/validate_amp_template.py
```

`tools/emit_start_command.py --write-kvp` regenerates the pins before it rewrites `App.CommandLineArgs`, so a stale pin cannot ship. `python tools/generate_bootstrap_pins.py --check` reports drift without writing.

Operations are **one-click**: Start and Restart in the AMP panel are the whole workflow. **No routine SSH, shell, or systemd access is required** — there is no systemd unit in the AMP path, and manual shell steps exist only for the emergency fallbacks in [Rollback / manual fallback](#5-rollback--manual-fallback).

## Quick reference

| Setting | Default |
|--------|---------|
| Launcher | `/bin/bash -lc eval${IFS}$(printf${IFS}%s${IFS}<base64-installer>|base64${IFS}-d)` |
| Bootstrap log | `scratchmmo-bootstrap.log` |
| Game server | `current/server/mmo_server.x86_64` on port **19080** (internal) |
| Web gateway | `current/gateway/mmo_web_gateway` on port **9090** |
| Public-facing AMP port | **9090** (`WebPort`) |
| Cloudflare Tunnel target | `http://127.0.0.1:9090` |
| Bind address | `0.0.0.0` (Docker-backed AMP) |
| Max players | `200` |
| Registration | `invite` |
| Data directory | `<instance-root>/server_data` |
| Control directory | `<instance-root>/control` (created on first Start) |
| Logs | `scratchmmo-bootstrap.log`, `scratchmmo-start.log`, `scratchmmo-web.log` |

Start launches:

| Process | Port | Role |
|---------|------|------|
| Godot server | `19080` | Internal WebSocket game server (**do not expose publicly**) |
| `mmo_web_gateway` | `9090` | Serves `current/web` and proxies `/ws` → `127.0.0.1:19080` |

Expected instance root layout **after first successful Start**:

```text
control/                 # created on first Start by inline installer
  amp_bootstrap_start.sh          # digest-pinned
  scratch_mmo_deploy_latest.py    # digest-pinned
current/                 # replaced by the release-bundled deployment engine
  server/mmo_server.x86_64
  server/mmo_server.pck
  web/
  gateway/mmo_web_gateway
  scripts/amp_start.sh
  release_manifest.json
  checksums.sha256
incoming/                # verified release triple cache
state/                   # deployment state
  control-engine/        # bounded control-module staging (active + one rollback set)
server_data/
scratchmmo-bootstrap.log
scratchmmo-start.log
scratchmmo-web.log
```

**Important:** AMP splits `App.CommandLineArgs` on literal spaces and does not shell-parse outer quotes. The template embeds the inline installer as a **base64 eval wrapper** with no literal spaces so Start works reliably. On Start, the wrapper decodes and runs the installer, which verifies the public bootstrap files against their pinned digests before installing them into `control/`. The private game release zip is fetched separately by the shim using the **GitHub Release Token**.

---

## 1. Add this template repository to AMP

```text
carthorsestudios/scratch-mmo-amp-template:main
```

In AMP: **Configuration → Instance Deployment → Add → Fetch → refresh**.

If Start fails with `control/amp_bootstrap_start.sh: No such file or directory`, the instance is using a **stale template start command**. Re-fetch the template and recreate or update the instance configuration.

### Migration from an earlier template version

This template is `Meta.ConfigVersion=5`. Version 4 introduced the digest-pinned bootstrap installer and the release-bound deployment handoff. Version 5 makes the `control/` pair install atomic, replaces the bootstrap's `| tee` pipeline with an `exec` into the Python supervisor, and adds the already-current no-download path plus bounded control-engine staging.

- Existing instances created on the old unpinned inline installer **keep working** — they still download the same two control files from `main`, they just do not verify digests yet.
- Because the new `control/` files are also published to `main`, those instances pick up the new supervised bootstrap and shim on their next Restart.
- A **one-time AMP template refresh** is needed for an instance to use the *pinned* Start command and the new `ConfigVersion`. Until you refresh, the instance keeps its old Start command.
- To refresh: **Configuration → Instance Deployment → Fetch**, then update or recreate the instance configuration so AMP re-reads `scratchmmo.kvp`. No data migration and no shell access are involved.

---

## 2. Create or update the instance

1. **Create Instance** → **Scratch MMO Godot Server**
2. Confirm defaults:
   - **Server Port:** `19080`
   - **Web Port:** `9090`
   - **Bind Address:** `0.0.0.0`
3. Enter **GitHub Release Token** (password field):
   - Read-only GitHub personal access token (or fine-grained token) scoped only to **`carthorsestudios/scratch-mmo`**
   - Needs access to download release assets
   - Stored only on the AMP server via AMP configuration
   - **Do not paste into chat. Do not commit to GitHub.**
   - **Do not put this token in Invite Code.**
4. Optional: **Release Tag Override** — leave blank for latest release (for example `main-3865433` to pin a tag)
5. Set **Allowed Web Origins** — comma-separated browser origins permitted to open `/ws` WebSocket connections (see [Environment mapping](#6-environment-mapping-automatic))
6. Set **Invite Code** if registration mode is `invite` (masked password field → `SCRATCH_MMO_INVITE_CODE`; game registration only — **not** used for GitHub)

No manual upload of `control/` files is required.

---

## 3. Start / Restart (skip Update)

1. Click **Start** (not Update)

### First Start without GitHub Release Token

If **GitHub Release Token** is not set yet and `current/` does not exist, the instance enters **setup mode** instead of exiting:

- A setup HTTP server listens on **Web Port** (default **9090**)
- AMP should report the instance as running after: `[ScratchMMO] Setup server listening port=9090`
- `/`, `/healthz`, and `/version` return setup-required responses
- You can open AMP configuration, enter **GitHub Release Token**, save, and **Restart**

Setup mode listens only on **Web Port** (default **9090**). Godot port **19080** remains internal and is not started in setup mode.

### After GitHub Release Token is configured

On first Start or Restart with a valid token:

1. Inline installer creates `control/` if missing
2. Downloads both control files from `raw.githubusercontent.com/carthorsestudios/scratch-mmo-amp-template/main/control/` into private temporary files inside `control/`
3. Verifies each download's SHA-256 against the pin embedded in the Start command, applies and verifies the required `0700` mode on the temporary files, snapshots whatever pair already exists, and only then replaces both control files as one unit
4. `exec`s `control/amp_bootstrap_start.sh`
5. Bootstrap `exec`s `python3 control/scratch_mmo_deploy_latest.py --deploy --supervise --yes`, so AMP's direct child becomes the Python supervisor
6. Shim downloads the release triple from private GitHub Releases using **GitHub Release Token**, verifies the manifest, checksums, and every ZIP member before extracting anything, then stages only the verified control modules into `state/control-engine/<release-id>/`
7. Shim hands off to `deployment/amp/amp_release_updater.py` from the verified release, which installs `current/`, health-checks it, and supervises `current/scripts/amp_start.sh`

On future **Restart**:

- Installer re-verifies the pinned control files, then swaps the pair atomically
- If `current/` already ships a checksum-verified engine, the shim runs it directly and **downloads nothing** unless that engine finds a newer release
- If a newer release is found: download → verify triple → hand off to the release engine → health check
- If the candidate is unhealthy, the engine restores the previous release automatically
- If already current: no download, no extraction, no swap
- Then the game runs under the supervisor, which forwards AMP's `SIGTERM` to the game on Stop/Restart

If the public bootstrap download fails or fails verification:

- Existing local `control/amp_bootstrap_start.sh` is used if present and non-empty
- Else existing `current/scripts/amp_start.sh` is used if present and non-empty
- Else Start fails with a clear error

Unverified or empty downloaded bytes are **never** executed and never overwrite a working `control/` file. If anything goes wrong part-way through installing the pair, the previous pair is restored in full — you never get a new bootstrap running against an old shim.

AMP deploys **release assets only**. The server does **not** build from source and does **not** download the private source repo.

### Logs

| File | Contents |
|------|----------|
| `scratchmmo-bootstrap.log` | Updater + bootstrap diagnostics (check this first on deploy failure) |
| `scratchmmo-start.log` | Startup diagnostics, server command, Godot stdout/stderr |
| `scratchmmo-web.log` | Web gateway stdout/stderr |

If Start stops immediately, check the AMP console and **`scratchmmo-bootstrap.log`** in File Manager.

Gateway health check inside the container/host:

```bash
curl -s http://127.0.0.1:9090/healthz
```

Public checks after start:

- `https://www.pipenpoob.com/healthz`
- `https://www.pipenpoob.com/version`

---

## 4. Public routing

**Do not expose AMP admin publicly.**

**Do not expose Godot port 19080 publicly.** Route public traffic to gateway port **9090** only.

Recommended production path:

- Cloudflare Tunnel → `http://127.0.0.1:9090`
- Enable **WebSockets** in Cloudflare

| URL | Purpose |
|-----|---------|
| `https://www.pipenpoob.com/` | Browser client (gateway serves `current/web`) |
| `wss://www.pipenpoob.com/ws` | WebSocket via gateway → Godot on `19080` (internal) |

Web client changes may still require a **Cloudflare cache purge** and browser hard refresh after deploy.

---

## 5. Rollback / manual fallback

Rollback is automatic. The release engine deploys a candidate, health-checks it against `SCRATCH_HEALTH_URL`, and restores the previously deployed release itself when the candidate is unhealthy — no operator action, no SSH.

If the shim cannot verify a release at all (download failure, checksum mismatch, missing assets), it makes **no changes**: the existing `current/` is preserved and started. If a release ships without the AMP deployment engine, the shim refuses the deploy rather than falling back to a directory rename.

Manual fallback is an emergency path only, not part of normal operation.

Manual deploy fallback (if the shim cannot reach GitHub):

1. Download `mmo_release.zip` from a trusted machine with GitHub access
2. Upload through AMP File Manager to the instance root
3. Extract and rename extracted folder to `current`
4. Click **Restart**

---

## 6. Environment mapping (automatic)

AMP maps instance settings to environment variables consumed by bootstrap/updater:

| AMP field | Environment variable |
|-----------|---------------------|
| Invite Code | `SCRATCH_MMO_INVITE_CODE` (password field; environment-only, never on the command line) |
| Registration Mode | `SCRATCH_REGISTRATION` (`open` / `closed` / `invite`) |
| GitHub Release Token | `SCRATCH_GITHUB_TOKEN` |
| Release Tag Override | `SCRATCH_RELEASE_TAG` (blank = latest) |
| Allowed Web Origins | `SCRATCH_ALLOWED_ORIGINS` |
| (fixed) | `SCRATCH_GITHUB_OWNER=carthorsestudios` |
| (fixed) | `SCRATCH_GITHUB_REPO=scratch-mmo` |
| (fixed) | `SCRATCH_HEALTH_URL=http://127.0.0.1:9090/healthz` |
| (fixed) | `SCRATCH_VERSION_URL=http://127.0.0.1:9090/version` |

**Invite Code** is required when Registration Mode is **Invite**. AMP passes it to the launched process as `SCRATCH_MMO_INVITE_CODE` only. It is **not** placed in command-line arguments, `SCRATCH_MMO_EXTRA_ARGS`, or startup logs. Legacy `--invite-code=` is rejected by the game launcher.
**Allowed Web Origins** is passed to the web gateway as `SCRATCH_ALLOWED_ORIGINS`. Use a comma-separated list with no spaces unless your origin URLs include them. Recommended values:

| Environment | Allowed Web Origins |
|-------------|---------------------|
| Production | `https://www.pipenpoob.com,http://localhost:9090,http://127.0.0.1:9090` |
| Staging | `https://staging.pipenpoob.com,http://localhost:9091,http://127.0.0.1:9091` |

If this field is left empty, the gateway falls back to production defaults and may reject staging origins.

The GitHub token and Invite Code are **environment-only**. They are **not** passed on the command line and are **not** logged by the updater or launcher.

---

## Troubleshooting

| Symptom | Check |
|---------|--------|
| Instance stops immediately on first Start (old template) | Re-fetch template; fresh installs should enter setup mode on port 9090 |
| Server not running before token entered | Expected on very old template; current template enters setup mode — check console for `[ScratchMMO] Setup server listening port=` |
| Setup page at `/` | Enter **GitHub Release Token** in AMP, save, Restart — do not use Invite Code for GitHub |
| `unexpected EOF while looking for matching` on Start | Stale quoted inline installer — re-fetch template commit with base64 start command |
| `control/` missing before first Start | Expected — folder is created on first Start |
| Update fails / auth error | GitHub Release Token field; token scope for private repo releases |
| Missing `current/` on first start | Enter GitHub Release Token and Restart; until then setup mode is normal |
| Deploy failed setup page | Check `scratchmmo-bootstrap.log`; fix token scope, then Restart |
| Bootstrap download failed | AMP console warnings; raw GitHub reachability; curl/wget available |
| `ERROR: SHA-256 mismatch for ...` on Start | The instance's Start command pins a different template commit than `main` currently serves — do a one-time AMP template refresh so the pins and control files line up |
| `refusing legacy swap` in the bootstrap log | The selected release does not ship `deployment/amp/amp_release_updater.py`; publish a release that includes the AMP deployment engine |
| Update fails | Expected for AMP Update button — use Start/Restart instead |
| Missing gateway binary | Release zip must include `gateway/mmo_web_gateway` |
| Site loads but WS fails | Gateway `/ws` proxy; Cloudflare WebSockets enabled |
| `/healthz` and `/version` work but `/ws` fails with `reason=origin status=403` in `scratchmmo-web.log` | Update **Allowed Web Origins** to include the browser origin (for example `https://staging.pipenpoob.com` on staging) |
| Docker networking | Keep bind address `0.0.0.0` |

---

## Repository note

Game source, CI, and release builds live in the private [scratch-mmo](https://github.com/carthorsestudios/scratch-mmo) repository.

Public bootstrap/shim sources in this repo:

- `control/amp_bootstrap_start.sh` — mirrors `deployment/auto_update/amp_bootstrap_start.sh` in the private repo
- `control/scratch_mmo_deploy_latest.py` — mirrors `deployment/auto_update/scratch_mmo_deploy_latest.py` (verify-then-handoff shim)
- `tools/inline_start_installer.sh` — readable installer source, including the SHA-256 pins
- `tools/generate_bootstrap_pins.py` — regenerates the pins from the committed control files
- `tools/emit_start_command.py` — regenerates the base64 Start command (never hand-edit `App.CommandLineArgs`)

After changing anything under `control/` or `tools/inline_start_installer.sh`:

```bash
python tools/emit_start_command.py --write-kvp
python tools/validate_amp_template.py
```

Validation covers KVP/JSON consistency, environment-only secret mapping, deterministic base64 regeneration, pin freshness, and behavioural installer/shim tests in throwaway directories. It also injects a failure at every stage of the control-pair install and asserts the complete previous pair comes back, and on Linux it starts the real bootstrap under a fake supervisor/launcher and proves a single `SIGTERM` to the outer pid tears the whole process tree down with no orphans and no held ports. That last test is skipped (with a reason) on non-Linux hosts.
