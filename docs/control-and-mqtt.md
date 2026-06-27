# PTZ, media player, and local MQTT integration

Research + design for richer control beyond live view: PTZ, a media-player
(speaker/TTS) entity, and pushing everything to a **local MQTT broker** so Home
Assistant auto-discovers entities (and sends commands back).

## 1. PTZ — confirmed

PTZ is driven over the **Dahua device RPC channel** (the same `0xf6` JSON-RPC used
by `scripts/imou_dvrip.py`), reachable either directly on the LAN (`:37777`) or
over a `dh-p2p` tunnel for remote cameras.

Verified against the account camera "Cam sân" (`IPC-PS70F-10M0`, PanTilt) via a
P2P tunnel to `:37777`:

```
ptz.factory.instance {"channel":0}   -> {"result":2}     # object id = 2  (PTZ supported)
```

Control pattern (standard Dahua PTZ RPC; call with the object id):

```jsonc
// continuous move: start then stop
{"method":"ptz.start","object":<id>,"params":{"code":"Left","arg1":0,"arg2":<speed 1-8>,"arg3":0},"session":<s>}
{"method":"ptz.stop", "object":<id>,"params":{"code":"Left","arg1":0,"arg2":<speed>,"arg3":0},"session":<s>}
// codes: Up, Down, Left, Right, LeftUp, LeftDown, RightUp, RightDown,
//        ZoomTele, ZoomWide, FocusNear, FocusFar, Iris*, GotoPreset, SetPreset
```

Notes:
- Fixed cameras (e.g. LAN `IPC-C22E-A`) return `InterfaceNotFound` for `ptz.*` —
  only expose PTZ for cameras where `ptz.factory.instance` succeeds.
- Native alternative (cloud, via libCloudClient): `MoveDevicePTZLocation` /
  `GetDevicePTZLocation` / `SetDevicePTZLocation` — not needed; the RPC path
  above is simpler and reuses our DVRIP client.
- The bridge keeps one logged-in RPC connection per PTZ camera (re-login on drop).

## 2. Media player (speaker / TTS)

Expose each talk-capable camera as an HA `media_player` so `tts.speak` /
`media_player.play_media` plays on the camera speaker:

```
play_media(url) -> ffmpeg: <url> -> G.711 A-law 8 kHz mono -> RTSP ANNOUNCE talk
                  (scripts/imou_talk_rtsp.py path) -> camera speaker
```

Status gate: RTSP `ANNOUNCE` is accepted (200) and bytes stream without reset, but
**clean speaker output is unconfirmed** (see `two-way-audio.md`). Ship the
media_player entity behind a config flag until a human confirms audio, or after
the RTP framing is refined (SETUP/RECORD, RTP timing, or DHAV packaging).

## 3. Local MQTT integration (design)

The add-on/stack already publishes a little MQTT discovery. Extend the supervisor
with a **persistent MQTT client** (paho `Client`, not one-shot publish) that:

- connects to the local broker (HA `core-mosquitto`, or the NAS broker),
- publishes **retained discovery** configs under `homeassistant/`,
- publishes **state** (online/offline, RTSP URL, PTZ availability),
- **subscribes** to command topics and acts on them (PTZ RPC, TTS talk).

### Topics (per camera `<slug>`, base `imou/<slug>`)

| Kind | Topic | Payload |
|---|---|---|
| state | `imou/<slug>/state` | `online` / `offline` (retained) |
| rtsp url | `imou/<slug>/rtsp_url` | `rtsp://<host>:8654/<slug>` (retained) |
| ptz cmd | `imou/<slug>/ptz/set` | `Up`/`Down`/`Left`/`Right`/`ZoomTele`/`ZoomWide`/`Stop`/`Preset:<n>` |
| tts cmd | `imou/<slug>/tts` | text (TTS) or a media URL |
| mp state | `imou/<slug>/mediaplayer/state` | `idle`/`playing` |

### HA discovery entities (retained `homeassistant/.../config`)

- `binary_sensor` connectivity (← `state`).
- `sensor` RTSP URL (← `rtsp_url`).
- PTZ as **buttons** (`button/imou_<slug>_ptz_left/config` … each
  `command_topic: imou/<slug>/ptz/set`, `payload_press: "Left"`), only for PTZ
  cameras. (A `cover` with open/close/stop maps poorly to pan/tilt; buttons +
  optional zoom buttons are clearer.)
- `media_player` for TTS (← `mediaplayer/state`, `command_topic`
  `imou/<slug>/tts`) — gated on talk being confirmed.
- The camera image/stream itself stays in go2rtc (WebRTC/RTSP) or HA's
  Generic/go2rtc camera; MQTT carries control + status, not video.

### Supervisor changes (implementation plan)

1. `mqtt_bridge.py`: paho `Client` with `loop_start`; helpers to publish
   discovery + state and to register command handlers.
2. Per PTZ camera: a small DVRIP RPC session (reuse `imou_dvrip.py` logic) kept
   alive; on `ptz/set` → `ptz.start`+`ptz.stop` (or `Stop` → just stop).
3. Per talk camera: on `tts` → synth/transcode → `imou_talk_rtsp` push.
4. Config: `mqtt.enabled`, broker host/port/creds (already in options); add
   `ptz: true`/`media_player: true` per camera (auto-detected via
   `ptz.factory.instance` at startup).

This keeps video on go2rtc and adds PTZ + TTS + status over local MQTT, so the
cameras show up in HA with controls without any cloud dependency.
