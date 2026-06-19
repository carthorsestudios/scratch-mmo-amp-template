# Scratch MMO — AMP Generic Module Template

Public [AMP](https://cubecoders.com/AMP) Generic Module template for the Scratch MMO Godot dedicated server. Linux (x86_64) only, for Docker-backed AMP hosts such as Ubuntu 24.04.

This repository contains **only** AMP template files — no gameplay source, **no GitHub tokens**, and **no release zip**. First deploy uses a manual upload of the release zip through AMP File Manager.

**Do not use AMP Update.** Start runs `current/scripts/amp_start.sh`, which launches both the Godot server and the bundled web gateway.

## Quick reference

| Setting | Default |
|--------|---------|
| Launcher | `/bin/bash current/scripts/amp_start.sh` |
| Game server | `current/server/mmo_server.x86_64` on port **19080** |
| Web gateway | `current/gateway/mmo_web_gateway` on port **9090** |
| Public-facing AMP port | **9090** (`WebPort`) |
| Bind address | `0.0.0.0` (Docker-backed AMP) |
| Max players | `200` |
| Registration | `invite` |
| Data directory | `<instance-root>/server_data` |
| Control directory | `<instance-root>/control` |
| Logs | `scratchmmo-start.log`, `scratchmmo-web.log` |

Start launches:

| Process | Port | Role |
|---------|------|------|
| Godot server | `19080` | Internal WebSocket game server |
| `mmo_web_gateway` | `9090` | Serves `current/web` and proxies `/ws` → `127.0.0.1:19080` |

Expected instance root layout:

```text
current/
  server/mmo_server.x86_64
  server/mmo_server.pck
  web/
  gateway/mmo_web_gateway
  scripts/amp_start.sh
  release_manifest.json
  checksums.sha256
server_data/
control/
scratchmmo-start.log
scratchmmo-web.log
```

---

## 1. Add this template repository to AMP

```text
carthorsestudios/scratch-mmo-amp-template:main
```

In AMP: **Configuration → Instance Deployment → Add → Fetch → refresh**.

---

## 2. Create the instance and upload the release

1. **Create Instance** → **Scratch MMO Godot Server**
2. Defaults:
   - **Server Port:** `19080`
   - **Web Port:** `9090`
   - **Bind Address:** `0.0.0.0`
3. Upload `mmo_release.zip` / `MMO_Release.zip` to the instance root
4. Extract, rename the extracted folder to `current`
5. Confirm:
   - `current/server/mmo_server.x86_64`
   - `current/gateway/mmo_web_gateway`
   - `current/scripts/amp_start.sh`
   - `current/web/index.html`

---

## 3. Start (skip Update)

1. Set **Invite Code** if registration mode is `invite`
2. Click **Start** (not Update)

`amp_start.sh` creates `server_data/` and `control/`, chmods binaries, starts the Godot server, then starts the web gateway.

### Logs

| File | Contents |
|------|----------|
| `scratchmmo-start.log` | Startup diagnostics, server command, Godot stdout/stderr |
| `scratchmmo-web.log` | Web gateway stdout/stderr |

If Start stops immediately, open **`scratchmmo-start.log`** in File Manager. Expect `--port=19080`, absolute `--data-dir=.../server_data`, and `[GameServer] WebSocket listening port=19080`.

If **`scratchmmo-start.log` does not exist**, AMP did not run the start script at all.

Gateway health check inside the container/host:

```bash
curl -s http://127.0.0.1:9090/healthz
```

---

## 4. Public routing prototype

AMP does not provide app-level reverse proxy/domain mapping. For the prototype:

1. Friend forwards **public TCP 80** on the VPS to AMP **Web Port 9090**
2. Cloudflare DNS: `www` → VPS IP, **proxied**
3. Enable **WebSockets** in Cloudflare
4. Prototype may use **Flexible SSL** if origin is HTTP-only on port 9090

| URL | Purpose |
|-----|---------|
| `https://www.pipenpoob.com/` | Browser client (gateway serves `current/web`) |
| `wss://www.pipenpoob.com/ws` | WebSocket via gateway → Godot on `19080` |

Production should eventually use HTTPS on origin, Cloudflare Tunnel, or Caddy/Nginx with **Full (Strict)** SSL.

---

## 5. Redeploy after a new release build

1. Re-fetch `carthorsestudios/scratch-mmo-amp-template:main` if the template changed
2. Upload fresh release zip through File Manager
3. Extract and rename to `current` (replace old `current/` contents)
4. Click **Start** (not Update)

---

## Troubleshooting

| Symptom | Check |
|---------|--------|
| Update fails | Expected — skip Update |
| Missing log after Start | Template/instance start config |
| Missing gateway binary | Release zip must include `gateway/mmo_web_gateway` |
| Site loads but WS fails | Gateway `/ws` proxy; Cloudflare WebSockets enabled |
| Docker networking | Keep bind address `0.0.0.0` |

---

## Repository note

Game source, CI, and release builds live in the private [scratch-mmo](https://github.com/carthorsestudios/scratch-mmo) repository.
