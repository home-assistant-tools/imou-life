# Imou Native Protocol Reduction Plan

Status: 2026-06-28. Goal: reduce dependence on Imou/Dahua Android native
libraries for P2P live stream and two-way audio bridge work.

## Current Finding

The one-way TTS-to-camera-speaker path and Frigate talkback path are now proven
without `libCommonSDK.so` for tested cameras. The original native dependency
split into clear layers:

```text
cloud account/device data
  -> P2P/DevAuth/PTCP tunnel
  -> DHHTTP visualtalk/live session
  -> talk-enable state machine
  -> audio codec
  -> DHAV audio frame
  -> DHHTTP interleave frame
```

The lower audio framing layers, visualtalk handshake, and public relay P2P path
are now implemented outside native code. The hard native pieces are the newer
account P2P v2 key selection path and full receive-side media crypto, neither of
which is required for one-way TTS to the camera speaker on the tested device.

Important update: the accepted talk codec is camera-specific. The tested
`lan_camera` LAN camera accepts Frigate mic audio only after the bridge
re-encodes go2rtc `PCMA/8000` to `AAC/ADTS 16000`; older G.711 profiles remain
valid candidates for other models and must not be globally overwritten.

## Evidence From The Current `.so`

`libCommonSDK.so` is stripped, but keeps enough RTTI, source paths, JNI exports,
and log strings to map the stack.

Important exported JNI boundary:

```text
NativeAudioTalker.createAudioTalker(json)
NativeAudioTalker.startTalk(handle)
NativeAudioTalker.startSampleAudio(handle)
NativeAudioTalker.playSound(handle)
NativeAudioTalker.pushMediaData(handle, type, bytes, len, softEncode)
LoginManager.jniAddDevices(json)
LoginManager.jniGetP2PPortAndState(json, state[], timeout, count[])
PlayManager.getStreamHandle(...)
```

Important native strings/classes:

```text
Src/P2PSDK/P2PClient.cpp
Src/P2PSDK/Kdf.cpp
Src/PTCP/PhonyTcpReactor.cpp
Src/Proxy/ProxyP2PClient.cpp
Src/Http/HttpDh/Client/HttpClientSessionWrapper.cpp
Src/Http/HttpDh/Utils/HttpDhClientStateMachine.cpp
Src/Media/Transformat/TransformatDHInterleave.cpp
Src/AudioPacker/LCDHAVAudioPacker.cpp
Src/AudioEncode/LCG711UAudioEncoder.cpp
Src/AudioEncode/LCAACAudioEncoder.cpp
DHHTTPTalker::sendAudioData
http_client_init_sdp_for_talk
http_client_put_frame
MSG_HTTPDH_START_TALK / HTTPDH_MEDIA_TALK / MSG_HTTPDH_STOP_TALK
DevP2PAk / DevP2PSk / DevP2PInfo / DevAuth
PasswordDigest / LightweightDigest / WSSE
```

Java-side config also matters: `TalkerParam.AUDIO_SAMPLE_RATE_8000` is actually
`16000`; default talk config is `encodeType=22` (G.711 mu-law), `sampleDepth=16`,
`packType=0` (DHAV). The Android recorder can show 8 kHz internally while the SDK
talker still configures 16 kHz packets.

## What Is Already De-Native

| Layer | Native needed? | Status |
| --- | --- | --- |
| Cloud login / device listing | No | Existing Python scripts cover account/device data. Keep secrets out of repo. |
| Audio source conversion | No | FFmpeg or Python can feed mono s16le PCM. |
| G.711 mu-law / A-law encode | No | Added pure Python in `scripts/imou_dhav.py`. |
| DHAV audio frame packer | No | Added pure Python in `scripts/imou_dhav.py`; checksum verified against a captured frame. |
| DHHTTP interleaved media wrapper | No | Added pure Python `$ + channel + 4-byte BE length + DHAV`; talk track defaults to `trackID=5`, channel `0x0a`. |
| DHP2P WSSE auth | No | Added pure Python in `scripts/imou_wsse.py`; native-compatible nonce shape and `base64(sha1(...))` digest. |
| Device auth fields | No | Added pure Python in `scripts/imou_wsse.py`: `Login to` MD5 key, HMAC-SHA256 `DevAuth`, PBKDF2-SHA256 and AES-256/OFB LocalAddr encryption. |
| PTCP packet/session/multi-realm bridge | No | Added pure Python in `scripts/imou_dhp2p.py`: packet parse/serialize, counters, bind/status/payload, heartbeat and local TCP listener. Relay `Bind` now returns `CONN` on the real camera. |
| DHP2P rendezvous signaling | Mostly no | Added pure Python in `scripts/imou_dhp2p.py`; real-device relay handshake succeeds with type-0 serial-only. |
| Live RTSP read-only tunnel | Mostly no | Public Rust `dh-p2p` works; pure Python tunnel now reaches `CONN` and moves payloads through the relay. RTSP still needs a targeted read test. |
| DHHTTP visualtalk auth | No | Added pure Python in `scripts/imou_wsse.py`: `visualtalk.xav` digest is `base64(sha1(nonce+created+MD5("user:<realm>:password").upper()))`. Verified Cseq 0 returns `200 OK`. |
| DHHTTP visualtalk start + talk-send frames | No | `scripts/imou_visualtalk.py` opens Cseq 0 and START TALK through the pure Python tunnel, then sends Python-generated interleaved DHAV frames. Real-device tests returned Cseq 0/1 `200 OK`; recent TTS tests sent 45 and 95 DHAV frames and were audible on `remote_camera`. |

## What Still Needs Native Or More RE

| Layer | Why it is still hard | Best next step |
| --- | --- | --- |
| P2P v2 app/device key derivation | Uses `DevP2PAk`, `DevP2PSk`, `p2pSalt`, KDF/HKDF/PBKDF paths inside native. Basic device-auth XML is now pure Python, but v2 key selection still needs confirmation. | Keep `dh-p2p` for old relay path and `libCommonSDK.so` for v2 path until derivation is isolated. |
| PTCP/P2P session robustness | Basic relay path is now proven, but long-running reconnect, loss, and duplicate-packet behavior still need hardening. | Add soak tests for repeated bind/disconnect, multiple realms, and RTSP reads. |
| DHHTTP talk-enable handshake | Solved through pure Python relay tunnel and direct LAN port 8086: Cseq 0 and START TALK return `200 OK`; Python sends DHAV frames after that. | Harden repeated-session cleanup and build a codec/track compatibility matrix per camera model. |
| Frigate/go2rtc talkback | No | `frigate_imou_talk_exec.py` works as a go2rtc `exec` backchannel. go2rtc negotiates browser mic as `PCMA/8000`; the helper can convert to camera-specific output codecs. Proven profile for `lan_camera`: `AAC/ADTS 16000`, direct LAN `8086`, gain `5.0`. |
| Custom audio injection through public API | `pushMediaData(type=0/1/2, PCM/G711, soft true/false)` returned false in standalone tests. | Treat it as non-viable unless a different talker state is discovered. |
| Full audio receive decrypt | Not required for TTS/speaker output. | Defer; only needed if we want camera-mic return audio from the same talk session. |

## New Pure-Python Frame Tool

`scripts/imou_dhav.py` can generate DHAV audio frames or fully interleaved
DHHTTP media bytes:

```bash
ffmpeg -y -i message.mp3 -ar 16000 -ac 1 -f s16le message.s16le
python3 scripts/imou_dhav.py message.s16le --codec mulaw --sample-rate 16000 --interleaved > talk.frames
```

The output stream is not enough by itself. It must be written after the native or
reimplemented `HTTPDH_START_TALK` state machine has enabled talk on the live
session.

## New Pure-Python Auth Tool

`scripts/imou_wsse.py` contains the auth pieces recovered from the native WSSE
client and the cloud P2P research path:

```bash
python3 scripts/imou_wsse.py --self-test
python3 scripts/imou_wsse.py dhp2p-wsse --username "$DHP2P_USER" --userkey "$DHP2P_KEY"
python3 scripts/imou_wsse.py device-auth --username "$CAM_USER" --password "$CAM_PASS"
```

The AES-OFB implementation has a pure-Python AES-256 fallback, so this helper
does not require OpenSSL, Android, or `libCommonSDK.so`. The native WSSE client
builds 32-byte alphanumeric nonces from `/dev/urandom`; the DHP2P public
rendezvous path uses a decimal nonce. Both shapes are supported.

## New Pure-Python Tunnel Tool

`scripts/imou_dhp2p.py` is the native/Rust replacement candidate for the DHP2P
transport:

```bash
python3 scripts/imou_dhp2p.py --self-test dummy
python3 scripts/imou_dhp2p.py <SERIAL> --bind 127.0.0.1:1554 --remote-port 554
python3 scripts/imou_dhp2p.py <SERIAL> --bind 127.0.0.1:18086 --remote-port 8086
```

It ports the DHP2P UDP rendezvous, direct UDP hole-punch, PTCP auth, heartbeat,
multi-realm TCP multiplexing and relay fallback shape into Python. The module
passes protocol self-tests and now completes relay `Bind -> CONN` on the real
camera. The bug was in the local accept path: the first client bytes could arrive
before `CONN`, and the tunnel incorrectly treated that as a bind timeout and sent
`DISC`. The fix buffers early client data while still waiting for `CONN`.

## New Pure-Python Visualtalk Probe

`scripts/imou_visualtalk.py` opens the `visualtalk.xav` state machine through a
remote-port-8086 tunnel:

```bash
python3 scripts/imou_dhp2p.py <SERIAL> --bind 127.0.0.1:18086 --remote-port 8086
python3 scripts/imou_visualtalk.py 127.0.0.1 --port 18086 --password "$CAM_PASS" --audio message.s16le
```

It generates the RTSP-like `PLAY` Cseq sequence and then writes interleaved
DHAV frames generated by `scripts/imou_dhav.py`. Auth is solved for the tested
camera: on the initial `401` challenge, take `realm`, compute
`MD5("user:<realm>:password").upper()`, then use that as the secret in the
WSSE `PasswordDigest`.

Real-device proof through the pure Python relay tunnel:

```text
PTCP status <realm> CONN
Cseq 0: HTTP/1.1 200 OK body=644
Cseq 1: HTTP/1.1 200 OK body=0
sent 50 interleaved DHAV frames
```

Convenience wrapper:

```bash
python3 scripts/imou_pure_talk.py --tone-seconds 1
python3 scripts/imou_pure_talk.py --text "test imou pure python bridge"
python3 scripts/imou_pure_talk.py --audio message.wav
```

Current wrapper proof on the real camera: macOS `say` TTS through
`--codec aac-adts` on `media-track=5` was audible, including the final Vietnamese
test phrase. The server SDP confirms `trackID=5` is `sendonly` and
`MPEG4-GENERIC/16000`, so the wrapper defaults to AAC/ADTS for this camera.
G.711 frames still complete the protocol handshake but were not audible on this
model.

Frigate talkback proof on the LAN example LAN camera:

```text
Frigate WebRTC mic -> go2rtc recvonly PCMA/8000
go2rtc exec -> frigate_imou_talk_exec.py stdin
ffmpeg low-delay PCMA/8000 -> AAC/ADTS 16000, volume gain 5.0
DHAV audio frame -> DHHTTP interleaved trackID=5 -> visualtalk.xav
```

The successful go2rtc source shape keeps the RTSP video source first, marks it
`#backchannel=0`, then adds the exec source as `#backchannel=1#audio=alaw/8000`.
For LAN cameras, `--direct --host <camera-ip> --port 8086` avoids P2P tunnel
setup latency. For remote cameras, the same helper can omit `--direct` and open
a DHP2P type-0 tunnel.

The script accepts explicit
`--nonce`, `--created`, `--password-digest`, and `--lightweight-digest` overrides
so a captured tuple can be replayed exactly while reversing the last auth field.

## Recommended Reduction Milestones

### Milestone 1: Native Harness, Non-Native Audio

Keep `libCommonSDK.so` only for session setup:

```text
LCSDK_Login.addDevices(...)
LCSDK_Login.getP2PPort(...)
LCSDK_Talk.startTalkByHandleKey(...)
```

Then inject PCM at the mic/encoder boundary. This is the current proven audible
route. It is not clean enough for a HA add-on, but it proves the account/device
session and talk path.

### Milestone 2: Native Session, Python Talk Frames

Use native only to open the live P2P session and enable talk, then send Python
generated DHAV/interleaved frames into the already-open media path. This removes
native audio capture, native codec, and native packetization.

Blocker: capture exact talk-enable bytes and the write point/socket/session
handle. Target hooks:

```text
http_client_init_sdp_for_talk        0x88d7fc / 0x891144
http_client_put_frame wrapper/impl   0x88dad8 / 0x891dd4
ShareHandle startTalk                0x54b844
MSG_HTTPDH_START_TALK call-site       0x54bca4
CTransformatDHInterleave ctor/xref    0x78c3ac
```

Probe script: `scripts/imou_hook_talk_protocol.js`. The most useful current
hook is `0x891dd4`: its second argument is the frame descriptor. Current
disassembly indicates:

```text
desc+0x00  u32 track_id
desc+0x04  u32 flags / frame type
desc+0x08  u32 payload length
desc+0x10  pointer to DHAV/media payload
desc+0x18  auxiliary pointer/timestamp context
```

For talk-send frames we expect `track_id=5`, interleave channel `0x0a`, and a
payload beginning with `DHAV`.

### Milestone 3: `dh-p2p` Transport, Python DHHTTP Talk

Use the Python DHP2P/PTCP transport and implement the visualtalk RTSP-like state
machine in Python:

```text
PLAY /live/visualtalk.xav ... Cseq: 0
PLAY track(s) ... Cseq: 1/2
PLAY ... talktype=talk ... Cseq: 3
$ 0x0a <len32be> DHAV(...)
```

This should be the first add-on shaped target if the target camera accepts the
same relay path as read-only RTSP.

### Milestone 4: Full Native-Free P2P v2

Reimplement `DevAuth`, P2P v2 rendezvous and PTCP/relay selection. This is the
least urgent layer because it does not improve media compatibility as much as
Milestone 2/3, and it is the most model/account sensitive.

## Practical Architecture For The Add-On

For production, split the bridge into swappable backends:

```text
bridge supervisor
  -> backend: dh-p2p RTSP read-only
  -> backend: native-sdk talk harness
  -> backend: python-dhhttp-talk (future)
  -> local ONVIF/status endpoints
  -> go2rtc/Frigate local URLs
```

That lets read-only video run without Android native libraries where possible,
while two-way audio can temporarily use the native harness until the talk
handshake is fully captured.
