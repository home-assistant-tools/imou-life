# Imou P2P Bridge Add-On

Home Assistant add-on for bridging Imou/Dahua cameras into local RTSP, ONVIF,
and DLNA talk endpoints.

The add-on runs:

- a pure-Python Imou DHP2P tunnel for remote cameras,
- go2rtc for RTSP/WebRTC/HLS restreaming,
- a React/Flask account UI for importing Imou cameras,
- one ONVIF endpoint per enabled camera,
- one DLNA MediaRenderer per talk-enabled camera for Home Assistant TTS/media.

## Responsibility And Credentials

This add-on is for cameras and Imou accounts you own or are authorized to
administer. It requires user-supplied credentials: the Imou account password for
account import/refresh, and each camera's device/RTSP password for stream, relay,
talk, and TTS features.

You are responsible for how you use it, including legal compliance, Imou/Dahua
account/device terms, privacy obligations, and credential handling. The add-on
does not send credentials to this GitHub repository or to the maintainers, but
your local `options.json`, Home Assistant secrets, backups, and logs should be
treated as sensitive.

Do not publish real account emails, passwords, access tokens, camera serials,
P2P keys, credential-bearing RTSP URLs, captures, or APK/vendor artifacts.

Pros:

- No router port forwarding for remote P2P cameras.
- No direct RTSP exposure from the camera to the public Internet.
- Local RTSP/WebRTC/ONVIF/DLNA endpoints for Home Assistant, Frigate, and VLC.
- Stream warming can reduce live-view startup delay.

Cons:

- Remote P2P still depends on Imou cloud availability and protocol behavior.
- Valid Imou and camera credentials are required.
- Cloud/API changes can break the bridge.
- Camera codecs, PTZ, talkback, and stream profiles vary by model.
- This is not an official Imou/Dahua integration.

## Install From GitHub

This repository is a Home Assistant add-on repository.

1. Open **Settings -> Add-ons -> Add-on Store**.
2. Open the top-right menu and choose **Repositories**.
3. Add `https://github.com/home-assistant-tools/imou-life`.
4. Refresh the add-on store.
5. Install **Imou P2P Bridge**.
6. Start the add-on and open the Web UI.

Local development is still possible by copying `addon/imou-p2p-bridge/` into
the Home Assistant `/addons` share.

The add-on uses host networking. The default ports are:

| Port | Service |
|------|---------|
| 8554 | go2rtc RTSP restream |
| 1984 | go2rtc API/UI |
| 8555 | go2rtc WebRTC |
| 8099 | add-on account/camera UI through ingress |
| 8600+ | local DHP2P tunnels |
| 8700+ | per-camera ONVIF endpoints |
| 8800+ | per-camera DLNA media renderers |

## Account UI

Open the add-on Web UI and add an Imou account. The UI lists cameras from the
account, including their home name when Imou returns it.

Newly imported cameras are not enabled automatically. Turn on **Bridge** for the
cameras you want to expose. PTZ probing and talk/TTS endpoints are enabled
automatically for bridged cameras; unsupported PTZ cameras are filtered out after
the DVRIP capability probe. Click an enabled camera row to open its detail modal,
fill the camera RTSP password, then save. Camera stream profiles are inferred
from Imou metadata and are exposed through ONVIF; you do not need to enter
channel/subtype manually.

If a camera is detected on the same LAN, the UI keeps it on direct LAN RTSP and
hides the relay toggle. Remote cameras use P2P.

## Configuration

The UI writes the same options file the add-on uses. A minimal manual config is:

```yaml
log_level: info
discovery_ui: true
advertised_host: ""
onvif_base_port: 8700
dlna_base_port: 8800
go2rtc:
  rtsp_port: 8554
  api_port: 1984
  webrtc_port: 8555
bridge:
  engine: python
  python_bridge: /opt/imou-p2p-bridge/imou_dhp2p.py
  binary: /opt/dh-p2p/dh-p2p
  base_port: 8600
  restart_seconds: 5
  verbose: false
accounts: []
cameras:
  - name: yard
    mode: p2p
    serial: CAMERA_SERIAL
    username: admin
    password: !secret yard_camera_password
    relay: false
    talk_codec: aac-adts
    talk_sample_rate: 16000
```

## Use In Frigate

Point Frigate to the restream:

```yaml
cameras:
  yard:
    ffmpeg:
      inputs:
        - path: rtsp://<addon-host>:8554/yard
          roles: [detect, record]
```

For PTZ/multiple profiles, add the generated ONVIF endpoint for that camera,
for example `http://<addon-host>:8700/onvif/device_service`.

For talk/TTS, Home Assistant can discover the per-camera DLNA renderer named
`Imou <camera name>` and play media to it. The bridge converts supported audio
with ffmpeg and sends it to the camera through Imou visualtalk.

Frigate two-way talkback is supported through go2rtc `exec` backchannel for
tested cameras. go2rtc receives browser mic audio as `PCMA/8000`, then
`frigate_imou_talk_exec.py` converts it into the selected per-camera talk
profile and sends DHAV frames through `visualtalk.xav`. The tested
`lan_camera` LAN camera needs `--output-codec aac-adts --sample-rate 16000
--volume-gain 5.0`; other models may need G.711 A-law/u-law, so keep codec
settings per camera.

The add-on also exposes a direct REST TTS endpoint:

```bash
curl -X POST "http://<addon-host>:8099/api/cameras/<serial>/tts" \
  -H "Content-Type: application/json" \
  --data '{"text":"Hello from Imou Bridge","lang":"en"}'
```

Home Assistant auto-discovers the DLNA `media_player` endpoints. It does not
auto-discover arbitrary HTTP TTS providers without a custom integration, so an
HA-native `tts.imou_bridge` entity should be provided by a small HACS/custom
component that calls this REST endpoint.

## Notes

- Remote P2P streaming still depends on Imou cloud relay/control access.
- The account API and add-on workflow are documented in
  `docs/reverse-api-and-addon-usage.md`.
- Pure-Python talk has been tested with remote P2P using the public relay route
  and with Frigate/go2rtc talkback on a LAN camera.
- The old Rust `dh-p2p` binary is still built as a fallback, but the default
  engine is the Python bridge used by the standalone app.
