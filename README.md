# Scratch MMO — AMP Generic Module Template

Public [AMP](https://cubecoders.com/AMP) Generic Module template for the Scratch MMO Godot dedicated server. Linux (x86_64) only, for hosts such as Ubuntu 24.04.

This repository contains **only** AMP template files — no gameplay source, **no GitHub tokens**, and **no release zip**. First deploy uses a manual upload of the release zip through AMP File Manager.

**Do not use AMP Update for first deploy.** Start performs local prep automatically.

## Quick reference

| Setting | Default |
|--------|---------|
| Launcher | `/bin/bash` (wrapper) |
| Game binary | `current/server/mmo_server.x86_64` |
| Working directory | AMP instance root |
| Port | `19080` |
| Bind address | `0.0.0.0` (Docker-backed AMP) |
| Max players | `200` |
| Registration | `invite` |
| Data directory | `server_data` (instance root) |
| Control directory | `control` (instance root) |
| Stop method | `SIGTERM` |
| Console / admin | `STDIO` |

Start runs this effective command:

```bash
/bin/bash -lc "mkdir -p server_data control && chmod +x current/server/mmo_server.x86_64 && exec current/server/mmo_server.x86_64 --headless -- --server --port=19080 --bind-address=0.0.0.0 --data-dir=server_data --control-dir=control --registration=invite --max-players=200"
```

Expected instance root layout:

```text
current/
  server/
    mmo_server.x86_64
  web/
  release_manifest.json
  checksums.sha256
server_data/    (created on Start)
control/        (created on Start)
```

---

## 1. Add this template repository to AMP

1. In AMP, open **Configuration → Instance Deployment**.
2. Click **Add** under **Configuration Repositories**.
3. Enter the repository in AMP's `user/repo:branch` format:

   ```text
   carthorsestudios/scratch-mmo-amp-template:main
   ```

4. Click **Fetch**, then refresh the AMP browser tab.
5. When creating a new instance, **Scratch MMO** (prefix `SCRATCH`) should appear in the application dropdown.

### Template files in this repository

| File | Purpose |
|------|---------|
| `manifest.json` | AMP deployment repository manifest |
| `scratchmmo.kvp` | Generic module application definition |
| `scratchmmoconfig.json` | User-visible AMP settings |
| `scratchmmoupdates.json` | Empty (Update not used) |
| `scratchmmoports.json` | Default port definitions |

---

## 2. If AMP cannot fetch this repository

This repo is **public** and should fetch without GitHub authentication. If fetch still fails (firewall, offline host, or AMP GitHub access issue):

**Option A — Copy templates locally on the AMP host**

1. On the AMP server, create a local deployment-templates folder (path varies by AMP install; common pattern):

   ```text
   /__VDS__ADS01/Plugins/ADSModule/DeploymentTemplates/LOCAL-main/
   ```

2. Copy these files from this repository into that folder:

   - `manifest.json`
   - `scratchmmo.kvp`
   - `scratchmmoconfig.json`
   - `scratchmmoupdates.json`
   - `scratchmmoports.json`

3. Refresh AMP and create the instance from the local `LOCAL` template source.

**Option B — Manual clone**

```bash
git clone https://github.com/carthorsestudios/scratch-mmo-amp-template.git
```

Copy the six template files from the clone into the AMP local templates folder above.

---

## 3. Create the Scratch MMO instance in AMP

1. **Create Instance** → choose **Scratch MMO** / **Scratch MMO Godot Server** (Generic module).
2. Set the instance root directory (AMP's application files path).
3. Review defaults under instance settings:
   - **Server Port:** `19080`
   - **Bind Address:** `0.0.0.0` (required for Docker-backed AMP)
   - **Player Limit:** `200`
   - **Registration Mode:** `invite`
   - **Invite Code:** set when using invite registration
4. Do **not** start the server yet — upload the release bundle first.

---

## 4. Upload and extract the release zip

1. Build or download the release zip from the private game repo's GitHub Actions **Release main** workflow (`mmo_release.zip` or `MMO_Release.zip`).
2. Open the instance **File Manager** in AMP.
3. Upload the zip to the **instance root**.
4. **Extract** the zip in File Manager. AMP leaves the contents in a top-level folder (for example `mmo_release/`).
5. **Rename** that extracted folder to `current`.
6. Confirm this file exists:

   ```text
   current/server/mmo_server.x86_64
   ```

No GitHub authentication is configured in this template; AMP will not download the zip for you.

---

## 5. Start the server (skip Update)

**Do not click Update.** AMP Update is not required and may fail on this template.

After upload/extract/rename to `current`:

1. Set **Invite Code** in AMP if registration mode is `invite`.
2. Click **Start**.

Start automatically:

1. Creates `server_data/` at the instance root
2. Creates `control/` at the instance root
3. Runs `chmod +x current/server/mmo_server.x86_64`
4. Launches the Godot server with your configured port, bind address, and registration settings

3. Watch the instance console for:

   ```text
   [GameServer] WebSocket listening port=19080
   ```

AMP treats that line as the "application ready" signal.

### Manual prep alternative (SSH)

Only needed if Start fails before prep runs:

```bash
cd /path/to/amp/instance/root
mkdir -p server_data control
chmod +x current/server/mmo_server.x86_64
```

---

## 6. Confirm the server is listening on port `19080`

Docker-backed AMP instances bind the Godot server to `0.0.0.0:19080` inside the container. On the AMP host, check the exposed/mapped port:

```bash
ss -tlnp | grep 19080
# or inspect the AMP instance port mapping in the AMP UI
```

The port should be in `LISTEN` state on the host or container bridge address AMP publishes.

---

## 7. Reverse proxy: point `/ws` to port `19080`

The browser client does not connect to AMP's UI port directly. Terminate TLS on Caddy or Nginx and proxy WebSocket traffic to the **host- or container-exposed** service on port `19080`. Confirm the exact target in AMP's instance port mapping if needed.

Serve static client files from `current/web`:

**Caddy** (example):

```caddyfile
www.pipenpoob.com {
    root * /path/to/instance/root/current/web
    file_server

    handle /ws* {
        reverse_proxy 127.0.0.1:19080
    }
}
```

**Nginx** (example):

```nginx
location /ws {
    proxy_pass http://127.0.0.1:19080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
}
```

Adjust the static file root to `current/web` and the proxy target to match AMP's published port mapping.

---

## 8. Public URLs (unchanged)

| Role | URL |
|------|-----|
| Browser entry | `https://www.pipenpoob.com/` |
| WebSocket endpoint | `wss://www.pipenpoob.com/ws` |

Players still use the public site and `wss://` endpoint.

---

## 9. Automated private GitHub Release updates (deferred)

This template **intentionally does not** download from GitHub Releases. Deploy new builds by uploading a fresh release zip, extracting, renaming the folder to `current`, and clicking **Start** (not Update).

---

## Troubleshooting

| Symptom | Check |
|---------|--------|
| Update fails with "Performing Upgrade" | Expected — skip Update; use **Start** instead |
| Start fails immediately | Confirm `current/server/mmo_server.x86_64` exists |
| Port already in use | Another service on `19080`; change **Server Port** in AMP and update the reverse proxy |
| Clients cannot connect | Proxy `/ws` → host/container port `19080`; verify `wss://www.pipenpoob.com/ws`; static files from `current/web` |
| Registration fails | Registration mode and invite code in AMP settings |
| Docker networking | Keep **Bind Address** at `0.0.0.0` |
| AMP dropdown missing Scratch MMO | Re-fetch `carthorsestudios/scratch-mmo-amp-template:main` or use local template copy |

---

## Repository note

Game source, CI, and VPS/systemd deployment live in the private [scratch-mmo](https://github.com/carthorsestudios/scratch-mmo) repository. This public repo is template-only for AMP instance deployment.
