# Imou P2P Bridge Add-On

Brings Imou/Dahua cameras into **Home Assistant** and **Frigate** as RTSP /
WebRTC streams — both **remote cameras over cloud P2P** (no port-forwarding) and
**LAN cameras** directly.

How it works:

```
remote cam ──cloud P2P (dh-p2p)──► 127.0.0.1:86xx ─┐
                                                    ├─► go2rtc ─► rtsp://addon:8554/<name>
LAN cam ──────────RTSP 554─────────────────────────┘            + WebRTC/HLS/snapshot
```

- `dh-p2p` builds a PTCP tunnel to a remote camera from its **serial only**
  (see `docs/cloud-p2p-stream.md`). It is single-client, so a bundled **go2rtc**
  consumes each source once and restreams it to many consumers (HA, Frigate,
  snapshots) and adds WebRTC/HLS.
- LAN cameras skip the tunnel; go2rtc sources them directly.

## Install

This add-on is not in a hosted store yet. Install it as a **local add-on**:

1. Copy the `imou-p2p-bridge/` folder into your Home Assistant `/addons` share
   (via the Samba or SSH/Terminal add-on).
2. Settings → Add-ons → Add-on Store → ⋮ → **Check for updates**.
3. It appears under **Local add-ons** → install → start.

(Or split this `addon/` directory into its own GitHub repo — `repository.yaml`
is included — and add it as a custom add-on repository.)

## Discover devices (serials) — no manual lookup

The add-on ships a small web UI exposed through Home Assistant **ingress**: open
the add-on and click **"Open Web UI"** (or the *Imou Devices* sidebar panel).

1. Enter your Imou account email + password and solve the captcha in the browser.
2. It logs in to the Imou cloud, lists your cameras with their **serials**, and
   prints a ready-to-paste `cameras:` block.
3. Copy that block into the add-on **Configuration** (fill the camera password),
   save, and restart.

Login runs inside the add-on; credentials and tokens are kept in memory only and
never written to disk. See `docs/login-api.md`.

## Configuration

```yaml
log_level: info
go2rtc:
  rtsp_port: 8554
  api_port: 1984
  webrtc_port: 8555
bridge:
  base_port: 8600       # first local port for P2P tunnels (one per p2p camera)
  restart_seconds: 5
  verbose: false
cameras:
  - name: yard            # -> stream id "yard"
    mode: p2p             # cloud P2P by serial
    serial: ABCDEF1234567
    username: admin
    password: !secret cam_yard_password
    channel: 1
    subtype: 0
    relay: false          # set true only if direct P2P fails
  - name: garage
    mode: lan             # camera on the same LAN
    host: 192.168.2.20
    username: admin
    password: !secret cam_garage_password
    channel: 1
    subtype: 0
mqtt:
  enabled: false          # optional: publishes discovery + status
  host: core-mosquitto
```

Notes:
- `name` becomes the go2rtc stream id (slugified). `subtype: 0` = main stream,
  `1` = sub stream.
- The bridge only needs the **serial** to tunnel; the camera **username/password**
  are used by go2rtc for RTSP digest auth — keep them in HA secrets.

## Use in Home Assistant

The add-on runs go2rtc with a web UI at `http://<addon-host>:1984` — open it to
confirm each stream is online.

- **WebRTC (low latency, recommended):** install the
  [WebRTC Camera](https://github.com/AlexxIT/WebRTC) custom integration (HACS),
  or add the go2rtc add-on pointing at this one.
- **Generic Camera:** Settings → Devices → Add Integration → *Generic Camera* →
  Stream source URL:
  ```
  rtsp://<addon-host>:8554/yard
  ```

## Use in Frigate

In your Frigate config, point `ffmpeg` inputs at the restream:

```yaml
go2rtc:                       # optional: let Frigate's own go2rtc consume it
  streams:
    yard: rtsp://<addon-host>:8554/yard

cameras:
  yard:
    ffmpeg:
      inputs:
        - path: rtsp://<addon-host>:8554/yard
          roles: [detect, record]
```

## Status / limitations

- Live RTSP receive (main + sub) is solid for LAN and cloud P2P.
- `dh-p2p` is a PoC (single-client, can be unstable); the supervisor restarts it
  on exit. go2rtc shields consumers from the single-client limit.
- Two-way talk (TTS to speaker) is researched but not wired into the add-on yet —
  the camera accepts RTSP `ANNOUNCE` but clean speaker output is unconfirmed
  (see `docs/two-way-audio.md`, `docs/lan-dvrip-talk.md`).
