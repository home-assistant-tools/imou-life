# Docker Deployment

This guide runs Imou Bridge as a plain Docker Compose stack without Home
Assistant add-ons or Dockge.

## Requirements

- Linux host with Docker Engine and Docker Compose v2.
- Host networking support.
- Outbound internet access for Imou cloud/P2P relay bootstrap.
- LAN reachability from the Docker host to Home Assistant, Frigate, and any LAN
  cameras you want to bridge directly.

Host networking is recommended because the bridge exposes RTSP, WebRTC, ONVIF,
DLNA/SSDP, and P2P helper ports. It also avoids WebRTC UDP and ONVIF discovery
edge cases caused by Docker port publishing.

## Quick Start

Clone the repository:

```bash
git clone https://github.com/home-assistant-tools/imou-life.git
cd imou-life/deploy/dockge/imou-bridge
```

Create the persistent data directory and options file:

```bash
mkdir -p data
cp data/options.example.json data/options.json
```

Start the stack:

```bash
docker compose up -d --build
```

Open the UI:

```text
http://<docker-host>:8099
```

Use **Add account** to import Imou cameras, then enable only the cameras you
want to expose. Click an enabled camera row to open its detail modal, enter the
camera RTSP password, review the generated URLs/YAML, and save.

## Compose File

The included `deploy/dockge/imou-bridge/compose.yaml` is also the generic Docker
Compose file:

```yaml
services:
  imou-bridge:
    build: .
    container_name: imou-bridge
    network_mode: host
    restart: unless-stopped
    environment:
      IMOU_TERMINAL_ID: ${IMOU_TERMINAL_ID:-}
      IMOU_TTID: ${IMOU_TTID:-}
    volumes:
      - ./data:/data
```

The `./data` directory contains runtime state:

| File | Purpose |
| --- | --- |
| `options.json` | Accounts, enabled cameras, camera passwords, ports, bridge settings |
| `go2rtc.yaml` | Generated go2rtc config |

Do not commit files from `data/`.

## Optional Environment Fingerprint

Some Imou accounts trigger Geetest slider verification when the server sees an
unknown Android client fingerprint. The bridge can reuse known trusted values by
setting these environment variables in `.env` next to `compose.yaml`:

```env
IMOU_TERMINAL_ID=
IMOU_TTID=
```

Leave them blank if you do not have trusted values. The UI will show the
verification challenge when the API requires it.

## Ports

With host networking, the services listen directly on the Docker host:

| Port | Service |
| --- | --- |
| `8554` | go2rtc RTSP restream, e.g. `rtsp://<host>:8554/<camera_slug>` |
| `1984` | go2rtc API/UI |
| `8555/tcp` and `8555/udp` | go2rtc WebRTC |
| `8099` | Imou Bridge account/camera UI |
| `8600+` | local P2P tunnel ports |
| `8700+` | per-camera ONVIF endpoints |
| `8800+` | per-camera DLNA MediaRenderer endpoints |

Change ports in `data/options.json` if they conflict with existing services:

```json
{
  "discovery_port": 8099,
  "go2rtc": {
    "rtsp_port": 8554,
    "api_port": 1984,
    "webrtc_port": 8555
  },
  "bridge": {
    "base_port": 8600
  },
  "onvif_base_port": 8700,
  "dlna_base_port": 8800
}
```

## Manual Camera Configuration

The UI is the easiest path, but `data/options.json` can be edited manually.

P2P camera by serial:

```json
{
  "name": "yard",
  "mode": "p2p",
  "serial": "CAMERA_SERIAL",
  "username": "admin",
  "password": "CAMERA_PASSWORD",
  "relay": false
}
```

LAN camera by IP:

```json
{
  "name": "example_camera",
  "mode": "lan",
  "host": "<camera-ip>",
  "username": "admin",
  "password": "CAMERA_PASSWORD",
  "relay": false
}
```

Restart after manual edits:

```bash
docker compose restart imou-bridge
```

## Frigate

Use the generated sample in the camera detail modal, or add a minimal Frigate
camera manually:

```yaml
cameras:
  yard:
    ffmpeg:
      inputs:
        - path: rtsp://<docker-host>:8554/yard
          roles:
            - detect
            - record
    onvif:
      host: <docker-host>
      port: 8700
      user: admin
      password: <camera-password>
    live:
      stream_name: yard
```

If a camera exposes multiple channels or profiles, the bridge creates multiple
go2rtc streams and multiple ONVIF media profiles. Copy the full generated YAML
from the UI for those cameras.

## Home Assistant

Use one of these approaches:

- Add the RTSP URL as a Generic Camera:
  `rtsp://<docker-host>:8554/<camera_slug>`.
- Point a go2rtc/WebRTC integration at `http://<docker-host>:1984`.
- Let Home Assistant discover the DLNA MediaRenderer endpoints for talk/TTS
  when multicast discovery is available on the same LAN.

For a native Home Assistant add-on install, use the add-on repository instead of
this Docker guide:

```text
https://github.com/home-assistant-tools/imou-life
```

## Logs And Updates

View logs:

```bash
docker compose logs -f imou-bridge
```

Update from GitHub:

```bash
git pull
docker compose up -d --build
```

Back up runtime state:

```bash
tar -czf imou-bridge-data-backup.tgz data/
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| UI opens but stream is offline | Camera password is set in the camera detail modal; check `docker compose logs` |
| P2P tunnel restarts with auth errors | Camera device password is missing or wrong |
| Login asks for slider often | Set trusted `IMOU_TERMINAL_ID` / `IMOU_TTID` if available, otherwise complete the UI slider |
| WebRTC fails | Ensure host networking is used and UDP `8555` is reachable |
| ONVIF client cannot connect | Check the camera's assigned ONVIF port in the UI and firewall rules |
| DLNA player not discovered | Ensure the Docker host and Home Assistant share multicast/SSDP visibility |
