# Imou Bridge — Dockge stack (TrueNAS)

Standalone Docker Compose version of the add-on: go2rtc + pure-Python DHP2P
tunnels + account/camera management UI. It can run on a plain Docker host or on
a NAS via [Dockge](https://github.com/louislam/dockge) instead of Home
Assistant.

For a generic Docker walkthrough, see
[`docs/docker-deployment.md`](../../../docs/docker-deployment.md).

## Responsibility And Credentials

Use this stack only with cameras, accounts, and networks you own or are
authorized to administer. The UI can import cameras from an Imou account, which
requires the Imou account password, and enabled cameras require their local
device/RTSP username and password for stream, relay, talk, and TTS features.

You are responsible for legal compliance, privacy obligations, Imou/Dahua
account/device terms, and keeping credentials safe. Treat `data/options.json`,
Docker volumes, backups, logs, and generated configs as sensitive. Do not publish
real account emails, passwords, tokens, serials, P2P keys, RTSP URLs containing
credentials, captures, or APK/vendor artifacts.

Pros:

- Avoids router port forwarding for remote P2P cameras.
- Avoids direct public exposure of camera RTSP ports.
- Gives Frigate/Home Assistant/VLC normal local RTSP/WebRTC/ONVIF endpoints.
- Can warm streams to reduce startup delay.

Cons:

- Remote P2P still relies on Imou cloud reachability and behavior.
- User-provided Imou and camera credentials are required.
- API/protocol changes can break the bridge.
- Codec, PTZ, talkback, and stream support differs by model.
- This stack is unofficial interoperability software.

## Layout

```
imou-bridge/
  compose.yaml        # the stack (network_mode: host)
  Dockerfile          # installs go2rtc + the Python DHP2P supervisor
  supervisor.py       # runs tunnels + go2rtc + app UI, restarts on exit
  app_ui.py           # React SPA + Flask API for accounts, cameras, and ONVIF
  data/options.json   # config (NOT committed; see options.example.json)
```

## Deploy With Docker Compose

1. Create `data/options.json` from `data/options.example.json` and fill in the
   camera serials + passwords, or open the UI and add accounts interactively.
2. Start the stack:

   ```bash
   docker compose up -d --build
   ```

3. Open `http://<docker-host>:8099`.

## Deploy With Dockge

1. Copy this folder into the Dockge stacks dir, e.g.
   `/mnt/<pool>/dockge/data/imou-bridge/` (find it with
   `docker inspect <dockge> | grep DOCKGE_STACKS_DIR`).
2. Create `data/options.json` from `data/options.example.json`, or use the UI
   after first start.
3. In Dockge open the **imou-bridge** stack → **Deploy**.

## Ports (host network)

| Port  | Service |
|-------|---------|
| 8654  | go2rtc RTSP restream → `rtsp://<nas>:8654/<name>` (Frigate / Generic Camera) |
| 11984 | go2rtc web UI / API → `http://<nas>:11984` |
| 8655  | go2rtc WebRTC (tcp/udp) |
| 8099  | account/camera UI → `http://<nas>:8099` (login → select cameras) |
| 8600+ | DHP2P tunnels (loopback, one or two per p2p camera) |
| 8700+ | per-camera ONVIF endpoints |
| 8800+ | per-camera DLNA MediaRenderer endpoints for talk/TTS |

Ports are set in `options.json` (`go2rtc.*`, `discovery_port`, `bridge.base_port`)
— change them if they clash with an existing go2rtc/Frigate on the NAS.

## Config (`data/options.json`)

Same schema as the add-on. Each camera is either:

- `"mode": "p2p"` with a `serial` (reached over cloud P2P), or
- `"mode": "lan"` with a `host` (direct RTSP on the LAN).

Plus `username`/`password` (camera RTSP creds). Channel/subtype details are
kept as internal stream metadata and are normally inferred from Imou account
device data.

Remote P2P cameras use the bundled pure-Python DHP2P tunnel by default:
`"bridge": { "engine": "python" }`. The restream URL remains ordinary RTSP
through go2rtc, so Frigate/Home Assistant do not need to understand the Imou
P2P protocol.

The UI also stores an `accounts` section. Adding an Imou account lists its
cameras but does **not** enable any camera automatically. Turn on the switch next
to a camera to sync it into the bridge/ONVIF config. PTZ probing and talk/TTS
endpoints are enabled automatically for bridged cameras; unsupported PTZ cameras
are filtered out after the DVRIP capability probe. The account password is used
only during login and is not written to `options.json`; camera passwords are
stored per enabled camera because RTSP and talk need device credentials.

If a camera exposes multiple channels or stream profiles, the bridge declares
them as separate ONVIF media profiles. The first enabled profile keeps the
camera's base RTSP slug, and additional profiles are exposed as separate go2rtc
streams for ONVIF clients to discover.

For every enabled camera, the stack also advertises a local DLNA/UPnP
MediaRenderer named `Imou <camera name>`. Home Assistant can
discover these as media players and send TTS/media URLs to them; the bridge
converts supported audio with ffmpeg and forwards it through Imou visualtalk.

The tested remote `remote_camera` camera has accepted pure-Python TTS through the
DHP2P `type 0` public relay route. A successful run opens
`/live/visualtalk.xav`, gets `200 OK` for the live and talk `PLAY` requests, and
sends interleaved DHAV audio frames to the speaker.

Frigate/go2rtc talkback is also proven on the tested LAN `lan_camera` camera.
That path uses go2rtc `exec` backchannel audio (`PCMA/8000`) and
`frigate_imou_talk_exec.py` to re-encode to the camera's accepted talk profile
(`AAC/ADTS 16000`, gain `5.0` for this camera) before DHAV/visualtalk send.

Talk codec is camera-specific. Keep per-camera settings such as
`--output-codec`, `--sample-rate`, and `--volume-gain`; do not globally replace
older G.711 routes with AAC.

If Imou asks for verification, the UI displays the challenge inside the login
modal. The PCS API may return either an image-code challenge or a Geetest v4
slider challenge; both paths are wired into the app UI.

## Use it

- **Frigate:** `path: rtsp://<nas>:8654/<name>`
- **Home Assistant:** Generic Camera with `rtsp://<nas>:8654/<name>`, or point a
  go2rtc/WebRTC integration at `http://<nas>:11984`.
