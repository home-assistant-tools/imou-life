# Imou Bridge — Dockge stack (TrueNAS)

Standalone Docker Compose version of the add-on: go2rtc + dh-p2p tunnels +
device-discovery UI, for running on a NAS via [Dockge](https://github.com/louislam/dockge)
instead of Home Assistant.

## Layout

```
imou-bridge/
  compose.yaml        # the stack (network_mode: host)
  Dockerfile          # builds dh-p2p (Rust) + go2rtc + the supervisor
  supervisor.py       # runs tunnels + go2rtc + discovery UI, restarts on exit
  login_site.py       # discovery web UI
  data/options.json   # config (NOT committed; see options.example.json)
```

## Deploy

1. Copy this folder into the Dockge stacks dir, e.g.
   `/mnt/<pool>/dockge/data/imou-bridge/` (find it with
   `docker inspect <dockge> | grep DOCKGE_STACKS_DIR`).
2. Create `data/options.json` from `data/options.example.json` and fill in the
   camera serials + passwords (or leave one camera and use the discovery UI).
3. In Dockge open the **imou-bridge** stack → **Deploy** (or on the host:
   `docker compose up -d --build`).

## Ports (host network)

| Port  | Service |
|-------|---------|
| 8654  | go2rtc RTSP restream → `rtsp://<nas>:8654/<name>` (Frigate / Generic Camera) |
| 11984 | go2rtc web UI / API → `http://<nas>:11984` |
| 8655  | go2rtc WebRTC (tcp/udp) |
| 8199  | device discovery UI → `http://<nas>:8199` (login → serials) |
| 8600+ | dh-p2p tunnels (loopback, one per p2p camera) |

Ports are set in `options.json` (`go2rtc.*`, `discovery_port`, `bridge.base_port`)
— change them if they clash with an existing go2rtc/Frigate on the NAS.

## Config (`data/options.json`)

Same schema as the add-on. Each camera is either:

- `"mode": "p2p"` with a `serial` (reached over cloud P2P), or
- `"mode": "lan"` with a `host` (direct RTSP on the LAN).

Plus `username`/`password` (camera RTSP creds), `channel`, `subtype`.

## Use it

- **Frigate:** `path: rtsp://<nas>:8654/<name>`
- **Home Assistant:** Generic Camera with `rtsp://<nas>:8654/<name>`, or point a
  go2rtc/WebRTC integration at `http://<nas>:11984`.
