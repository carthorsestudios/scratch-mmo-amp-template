# Scratch MMO — AMP Generic Module Template

Public [AMP](https://cubecoders.com/AMP) Generic Module template for the Scratch MMO Godot dedicated server. Linux (x86_64) only, for Docker-backed AMP hosts such as Ubuntu 24.04.

This repository contains **only** AMP template files — no gameplay source, **no GitHub tokens**, and **no release zip**. Release binaries are downloaded from the private `carthorsestudios/scratch-mmo` GitHub Releases on **Start/Restart**.

**Do not use AMP Update.** Start runs a small inline installer that self-installs the bootstrap/updater into `control/`, then runs `control/amp_bootstrap_start.sh`, which checks for a newer release, validates it, swaps `current/`, and launches `current/scripts/amp_start.sh`.

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
  amp_bootstrap_start.sh
  scratch_mmo_deploy_latest.py
current/                 # replaced by restart updater
  server/mmo_server.x86_64
  server/mmo_server.pck
  web/
  gateway/mmo_web_gateway
  scripts/amp_start.sh
  release_manifest.json
  checksums.sha256
previous/                # backups of old current/
releases/<tag>/          # cached downloaded zips
server_data/
scratchmmo-bootstrap.log
scratchmmo-start.log
scratchmmo-web.log
```

**Important:** AMP splits `App.CommandLineArgs` on literal spaces and does not shell-parse outer quotes. The template embeds the inline installer as a **base64 eval wrapper** with no literal spaces so Start works reliably. On Start, the wrapper decodes and runs the installer, which downloads public bootstrap files from this template repo into `control/`, then runs them. The private game release zip is fetched separately by the updater using the **GitHub Release Token**.

---

## 1. Add this template repository to AMP

```text
carthorsestudios/scratch-mmo-amp-template:main
```

In AMP: **Configuration → Instance Deployment → Add → Fetch → refresh**.

If Start fails with `control/amp_bootstrap_start.sh: No such file or directory`, the instance is using a **stale template start command**. Re-fetch the template and recreate or update the instance configuration.

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
5. Set **Invite Code** if registration mode is `invite` (game registration only — **not** used for GitHub)

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
2. Downloads public bootstrap files from `raw.githubusercontent.com/carthorsestudios/scratch-mmo-amp-template/main/control/`
3. Runs `control/amp_bootstrap_start.sh`
4. Updater downloads latest `mmo_release.zip` from private GitHub Releases using **GitHub Release Token**
5. Validates checksums and required files
6. Installs into `current/`
7. Runs `current/scripts/amp_start.sh`

On future **Restart**:

- Bootstrap refreshes public control scripts when raw GitHub download succeeds
- Updater checks GitHub for a newer private release
- If found: download → validate → swap `current/` (old tree moved to `previous/`)
- If already current: skip swap
- Then start the game

If the public bootstrap download fails temporarily:

- Existing local `control/amp_bootstrap_start.sh` is used if present
- Else existing `current/scripts/amp_start.sh` is used if present
- Else Start fails with a clear error

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

If the updater fails validation or download, bootstrap keeps existing `current/` (if present) and still tries to start the game.

Manual rollback:

```bash
mv current current-broken
mv previous/<timestamp>-<tag> current
# Restart AMP
```

Manual deploy fallback (if updater cannot reach GitHub):

1. Download `mmo_release.zip` from a trusted machine with GitHub access
2. Upload through AMP File Manager to the instance root
3. Extract and rename extracted folder to `current`
4. Click **Restart**

---

## 6. Environment mapping (automatic)

AMP maps instance settings to environment variables consumed by bootstrap/updater:

| AMP field | Environment variable |
|-----------|---------------------|
| GitHub Release Token | `SCRATCH_GITHUB_TOKEN` |
| Release Tag Override | `SCRATCH_RELEASE_TAG` (blank = latest) |
| (fixed) | `SCRATCH_GITHUB_OWNER=carthorsestudios` |
| (fixed) | `SCRATCH_GITHUB_REPO=scratch-mmo` |
| (fixed) | `SCRATCH_HEALTH_URL=http://127.0.0.1:9090/healthz` |
| (fixed) | `SCRATCH_VERSION_URL=http://127.0.0.1:9090/version` |

The GitHub token is **environment-only**. It is **not** passed on the command line and is **not** logged by the updater.

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
| Update fails | Expected for AMP Update button — use Start/Restart instead |
| Missing gateway binary | Release zip must include `gateway/mmo_web_gateway` |
| Site loads but WS fails | Gateway `/ws` proxy; Cloudflare WebSockets enabled |
| Docker networking | Keep bind address `0.0.0.0` |

---

## Repository note

Game source, CI, and release builds live in the private [scratch-mmo](https://github.com/carthorsestudios/scratch-mmo) repository.

Public bootstrap/updater sources in this repo:

- `control/amp_bootstrap_start.sh`
- `control/scratch_mmo_deploy_latest.py`
- `tools/inline_start_installer.sh` (readable installer source)

Validate this template locally:

```bash
python tools/validate_amp_template.py
```
