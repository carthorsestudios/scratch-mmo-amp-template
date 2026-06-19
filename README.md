# Scratch MMO — AMP Generic Module Template

Public [AMP](https://cubecoders.com/AMP) Generic Module template for the Scratch MMO Godot dedicated server. Linux (x86_64) only, for hosts such as Ubuntu 24.04.

This repository contains **only** AMP template files — no gameplay source, **no GitHub tokens**, and **no release zip**. First deploy uses a manual upload of `mmo_release.zip` through AMP File Manager.

## Quick reference

| Setting | Default |
|--------|---------|
| Executable | `server/mmo_server.x86_64` |
| Working directory | AMP instance root |
| Port | `19080` |
| Bind address | `127.0.0.1` |
| Max players | `200` |
| Registration | `invite` |
| Data directory | `server_data` |
| Control directory | `control` |
| Stop method | `SIGTERM` |
| Console / admin | `STDIO` |

Equivalent command line:

```bash
server/mmo_server.x86_64 --headless -- --server --port=19080 --bind-address=127.0.0.1 --data-dir=server_data --control-dir=control --registration=invite --max-players=200
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
| `scratchmmoupdates.json` | Update/preparation steps (no download) |
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
   - **Bind Address:** `127.0.0.1`
   - **Player Limit:** `200`
   - **Registration Mode:** `invite`
   - **Invite Code:** set when using invite registration
4. Do **not** start the server yet — upload the release bundle first.

---

## 4. Upload `mmo_release.zip` via AMP File Manager

1. Build or download `mmo_release.zip` from the private game repo's GitHub Actions **Release main** workflow (artifact name: `mmo_release.zip`).
2. Open the instance **File Manager** in AMP.
3. Upload `mmo_release.zip` to the **instance root** (not a subdirectory).

No GitHub authentication is configured in this template; AMP will not download the zip for you in v1.

---

## 5. Extract the release at the instance root

The zip contains a top-level folder `mmo_release/`. Extract so these paths exist **directly at the instance root**:

```text
server/mmo_server.x86_64
web/
release_manifest.json
checksums.sha256
```

**Correct** (flat layout at instance root):

```text
<instance-root>/server/mmo_server.x86_64
<instance-root>/web/
<instance-root>/release_manifest.json
<instance-root>/checksums.sha256
```

**Incorrect** (nested — AMP will not find the binary):

```text
<instance-root>/mmo_release/server/mmo_server.x86_64
```

In AMP File Manager, extract `mmo_release.zip`, then move everything from `mmo_release/` up one level if needed, and remove the empty `mmo_release/` folder.

---

## 6. Run AMP Update (executable bit and directories)

After extraction, run **Update** on the instance (not a full re-download). The template's update stages:

1. Create `server_data/`
2. Create `control/`
3. `chmod +x` on `server/mmo_server.x86_64` via AMP's `SetExecutableFlag`

### Manual `chmod` alternative

If Update cannot set permissions (or you prefer SSH), on the AMP host:

```bash
cd /path/to/amp/instance/root
chmod +x server/mmo_server.x86_64
mkdir -p server_data control
```

Run that from the **instance root** where `server/mmo_server.x86_64` exists.

---

## 7. Start the server

1. Set **Invite Code** in AMP if registration mode is `invite`.
2. Click **Start** in AMP.
3. Watch the instance console for:

   ```text
   [GameServer] WebSocket listening port=19080
   ```

AMP treats that line as the "application ready" signal.

---

## 8. Confirm the server is listening on `127.0.0.1:19080`

On the AMP host:

```bash
ss -tlnp | grep 19080
# or
curl -v --http0.9 http://127.0.0.1:19080/ 2>&1 | head
```

You should see the process bound to `127.0.0.1:19080` (or your configured bind address). The Godot server speaks WebSocket on that port; a plain HTTP probe may not return HTML, but the port should be in `LISTEN` state.

---

## 9. Reverse proxy: point `/ws` to `127.0.0.1:19080`

The browser client does not connect to AMP's port directly. Terminate TLS on Caddy or Nginx and proxy WebSocket traffic:

**Caddy** (example):

```caddyfile
www.pipenpoob.com {
    root * /path/to/instance/root/web
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

Adjust static file root to the instance `web/` directory.

---

## 10. Public URLs (unchanged)

| Role | URL |
|------|-----|
| Browser entry | `https://www.pipenpoob.com/` |
| WebSocket endpoint | `wss://www.pipenpoob.com/ws` |

Players still use the public site and `wss://` endpoint. The Godot server remains bound locally on `127.0.0.1:19080`; only the reverse proxy is exposed.

---

## 11. Automated private GitHub Release updates (deferred)

This template **intentionally does not** download from GitHub Releases. That avoids storing tokens or release credentials in AMP and matches the first-deploy workflow: manual zip upload via File Manager.

A future revision may add `GithubRelease` or scripted update stages once the manual AMP instance is verified. Until then, deploy new builds by uploading a fresh `mmo_release.zip`, extracting at the instance root, running **Update**, and restarting.

---

## Troubleshooting

| Symptom | Check |
|---------|--------|
| Start fails immediately | Binary missing or not executable — confirm `server/mmo_server.x86_64` at instance root and run Update or `chmod +x` |
| Port already in use | Another service on `19080`; change **Server Port** in AMP and update the reverse proxy |
| Clients cannot connect | Proxy `/ws` → `127.0.0.1:19080`; verify `wss://www.pipenpoob.com/ws` |
| Registration fails | Registration mode and invite code in AMP settings |
| AMP dropdown missing Scratch MMO | Re-fetch `carthorsestudios/scratch-mmo-amp-template:main` or use local template copy |

---

## Repository note

Game source, CI, and VPS/systemd deployment live in the private [scratch-mmo](https://github.com/carthorsestudios/scratch-mmo) repository. This public repo is template-only for AMP instance deployment.
