# Imou Life Add-on App

Imou Life bridge app for Home Assistant, Frigate, and NAS/Dockge deployments.
It combines the reverse-engineered Imou/Dahua P2P transport with a local
management UI, go2rtc restreaming, ONVIF endpoints, PTZ helpers, and
experimental two-way talk/TTS work.

The app is designed to run as an add-on style container. It keeps camera video
local to your network consumers while reaching remote Imou cameras through the
same cloud P2P relay path the mobile app uses.

## What It Does

- Restreams Imou/Dahua cameras through go2rtc for Frigate and Home Assistant.
- Supports `p2p` cameras by serial and `lan` cameras by local IP.
- Provides a React account/camera management UI.
- Lets you add Imou accounts, list cameras, and choose which cameras are synced
  into the bridge config.
- Does not auto-enable cameras when an account is added.
- Enables PTZ probing and talk/TTS automatically for every camera once Bridge is
  turned on; unsupported PTZ cameras are filtered out by the capability probe.
- Exposes enabled cameras through local go2rtc RTSP/WebRTC and local ONVIF
  endpoints for compatible consumers.
- Advertises a DLNA/UPnP media player per enabled talk-capable camera so Home
  Assistant can discover it and send TTS/media to the camera speaker.
- Includes pure Python Imou P2P/visualtalk tooling that has produced audible
  TTS and Frigate talkback on tested camera speakers.

## Install With Docker Compose

Use the standalone Docker stack when you want to run Imou Bridge on a generic
Linux host, NAS, or VM without installing it as a Home Assistant add-on.

```bash
git clone https://github.com/home-assistant-tools/imou-life.git
cd imou-life/deploy/dockge/imou-bridge
mkdir -p data
cp data/options.example.json data/options.json
docker compose up -d --build
```

Then open:

```text
http://<docker-host>:8099
```

See the full [Docker Deployment Guide](docs/docker-deployment.md) for ports,
volumes, environment variables, Frigate examples, Home Assistant usage, logs,
updates, and troubleshooting.

## Install On TrueNAS / Dockge

1. Copy `deploy/dockge/imou-bridge/` to your Dockge stacks directory on TrueNAS,
   for example:

   ```bash
   rsync -a deploy/dockge/imou-bridge/ <user>@<truenas-ip>:/mnt/<pool>/dockge/data/imou-bridge/
   ```

2. Create the config file:

   ```bash
   cp data/options.example.json data/options.json
   ```

3. Deploy from Dockge, or run from the stack directory:

   ```bash
   docker compose up -d --build
   ```

4. Open the app UI:

   ```text
   http://<truenas-ip>:8199
   ```

5. Click **Add account**, log in to Imou, then enable only the cameras you
   want exposed. Fill the camera device password for enabled cameras.

   If Imou asks for verification, this add-on displays the challenge inside the
   login modal. The PCS API may return either an image-code challenge or a
   Geetest v4 slider challenge; both paths are wired into the app UI.

## Default Ports

| Port | Purpose |
| --- | --- |
| `8654` | go2rtc RTSP restream, e.g. `rtsp://<host>:8654/<camera_slug>` |
| `11984` | go2rtc API/web UI |
| `8655` | go2rtc WebRTC |
| `8099` | Imou account/camera management UI |
| `8600+` | internal P2P tunnels |
| `8700+` | per-camera ONVIF endpoints |
| `8800+` | per-camera DLNA MediaRenderer endpoints for talk/TTS |

## Two-Way Audio / TTS Status

One-way audio to the camera speaker is proven through DHHTTP `visualtalk.xav`.
Two talk profiles are currently known:

| Profile | Tested use | Transport | Camera codec |
| --- | --- | --- | --- |
| P2P TTS | remote camera | DHP2P `type 0` relay to port `8086` | camera-dependent; AAC/ADTS works on tested speakers |
| Frigate talkback | LAN camera | direct LAN camera `8086` from go2rtc `exec` backchannel | browser mic `PCMA/8000` re-encoded to `AAC/ADTS 16000`, volume gain `5.0` |

Do not assume one audio codec for every camera. The bridge keeps codec settings
per talk route (`--output-codec`, `--sample-rate`, `--volume-gain`) because Imou
models may accept AAC, G.711 A-law, or G.711 mu-law differently.
For REST/DLNA TTS, camera config can carry `talk_codec` / `talk_output_codec`
and `talk_sample_rate`; leave existing G.711 profiles intact for cameras that
need them.

Repeated talk sessions may need a short pause between attempts; opening a new
session immediately after closing the previous one can make the camera close the
socket before response headers. The `type 1` relay path is not reliable for this
talk route yet.

The add-on also exposes a direct REST TTS endpoint per camera:

```bash
curl -X POST "http://<addon-host>:8099/api/cameras/<serial>/tts" \
  -H "Content-Type: application/json" \
  --data '{"text":"Hello from Imou Bridge","lang":"en"}'
```

Home Assistant can auto-discover the per-camera DLNA media players, but it does
not have a generic standard for auto-discovering arbitrary HTTP TTS services.
Use the DLNA `media_player` path for discovered playback, add a manual
`rest_command`, or install a small custom integration that registers an HA
`tts` entity backed by this REST endpoint.

### Frigate two-way audio

Frigate/go2rtc talkback is working for the tested LAN example LAN camera using a
go2rtc `exec` backchannel. The video/audio stream stays first so live view works,
then an `exec` producer receives browser mic audio as `PCMA/8000`:

```yaml
go2rtc:
  streams:
    imou_lan_camera:
      - rtsp://admin:<password>@<camera-ip>:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif#backchannel=0
      - exec:python3 /config/imou-talk/frigate_imou_talk_exec.py --direct --host <camera-ip> --port 8086 --serial <serial> --username admin --password <password> --channel 1 --subtype 0 --type 0 --input-codec alaw --input-sample-rate 8000 --output-codec aac-adts --volume-gain 5.0#backchannel=1#audio=alaw/8000
      - ffmpeg:imou_lan_camera#audio=opus
```

For other camera models, change only the per-camera codec flags; do not
overwrite the old G.711 profiles globally.

See the full [Frigate Talkback Integration Guide](docs/frigate-talkback.md) for
LAN direct setup, helper placement, codec profiles, and debugging.

## Frigate

After enabling a camera, use the go2rtc RTSP restream in Frigate:

```yaml
cameras:
  imou_camera:
    ffmpeg:
      inputs:
        - path: rtsp://<truenas-ip>:8654/<camera_slug>
          roles: [detect, record]
```

For PTZ cameras, the stack can also run an ONVIF PTZ shim so Frigate can send
PTZ commands through the bridge.

## Documents

- [Reverse API and Home Assistant Add-on Guide](docs/reverse-api-and-addon-usage.md)
- [Docker Deployment Guide](docs/docker-deployment.md)
- [Frigate Talkback Integration Guide](docs/frigate-talkback.md)
- [Imou Cloud MQTT Realtime Events](docs/imou-cloud-mqtt-events.md)
- [Dockge Stack](deploy/dockge/imou-bridge/README.md)
- [Research Summary](docs/research-summary.md)
- [Cloud API Surface](docs/cloud-api-surface.md)
- [P2P and Media Flow](docs/p2p-media-flow.md)
- [Phase 2 Local P2P Bridge](docs/phase2-local-p2p-bridge.md)
- [Two-Way Audio](docs/two-way-audio.md)
- [Talk Protocol — Binary-Level Reverse Engineering](docs/talk-protocol-reverse-engineering.md)
- [Native Protocol Reduction Plan](docs/native-protocol-reduction-plan.md)
- [Frigate and go2rtc Bridge Notes](docs/frigate-go2rtc-bridge.md)
- [Next Steps](docs/next-steps.md)

## Safety Notes

This project is for interoperability research with devices you own or are
authorized to test. Avoid publishing secrets, account tokens, device serials,
APK binaries, vendor code, or captured private traffic.

The local APK and decompiled artifacts are intentionally kept under `artifacts/`
and ignored by Git. Do not commit APKs, native libraries, or decompiled vendor
code.
