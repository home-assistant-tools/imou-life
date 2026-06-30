# Two-Way Audio (Talkback)

Reverse-engineering notes for the Imou Life two-way talk flow, derived from the
decompiled app (`artifacts/imou_apk/jadx/sources/`) and runtime/MITM captures.
Goal: build a bridge that pushes microphone audio to the camera speaker without
the Android app.

## Main Finding

Two-way audio is handled by a dedicated native talk component, not the audio
track of the live RTSP stream. Crucially, **the mic is captured inside the native
`.so` (`startSampleAudio`)** — Java does not push raw mic PCM for the audio path.
Talk media flows over the same `127.0.0.1:<port>` P2P loopback proxy that the
receive path uses, via `/live/talk.xav` (audio-only) or `/live/visualtalk.xav`
(visual talk) instead of `/live/realmonitor.xav`.

Current bridge status: one-way TTS and Frigate talkback both work without the
Android app or native `.so` on tested cameras.

- Remote `remote_camera`: opens a DHP2P `type 0` relay tunnel to port `8086`, starts
  `/live/visualtalk.xav`, and sends DHAV-wrapped audio frames. Repeated sessions
  need a short pause; if reopened too quickly, the camera may close the socket
  before headers.
- LAN `lan_camera`: Frigate/go2rtc receives browser mic audio as `PCMA/8000`,
  pipes it into `frigate_imou_talk_exec.py`, the helper re-encodes to
  `AAC/ADTS 16000` with low-delay ffmpeg, then sends DHAV interleaved frames
  over direct LAN `visualtalk.xav` (`<camera-ip>:8086`). This has been audible
  end-to-end from the Frigate mic button; gain is currently set to `5.0`.

Treat codec as a per-camera profile. Static app defaults, runtime share-link
captures, and actual speaker acceptance can differ between models.

## Class / Wrapper Layering

```text
UI (LCMediaVideoTalkFragment / MediaCallLivePreviewFragment)
  -> LCSDK_Talk            (Kotlin singleton entrypoint)  com/lc/lcsdk/LCSDK_Talk.java
     -> AudioTalker        (thin wrapper, holds long mTalkHandle)  com/lc/common/talk/AudioTalker.java
        -> NativeAudioTalker (JNI bindings)  com/iotcom/commonsdk/talk/NativeAudioTalker.java
           -> native .so    (transport, codec, mic capture)  libCommonSDK.so

listener path: native -> AudioTalker(TalkerListener) -> LCSDK_Talk.mListener -> observers
report  path:  AudioTalker -> LCSDK_TalkReportListener.onDataAnalysis(json)
```

- `LCSDK_Talk` owns one lazy `AudioTalker`, one `TalkParameter`, one listener.
- `AudioTalker.create(TalkerParam)` -> `NativeAudioTalker.createAudioTalker(json)`,
  stores the returned `long mTalkHandle`; every other native call passes it.
- Video push loop lives in `Y8/c.java` (`pushMediaData(type=1=video, ...)`).
  There is **no Java audio push loop** — audio is native.

## Talk Session Setup (URL acquisition)

`LCSDK_Talk.startMediaTalk(...)` / `startTalk(...)` branch on `isOPT`:

- `isOPT == 1` -> `startDHHttpTalk` -> `TalkUtils.getDHHTTPTalkUrl(...)` (DHHTTP/XAV)
- `isOPT == 0` -> `startRtspTalk`  -> `TalkUtils.getRTSPTalkUrl(...)`  (RTSP)
- share-link  -> `startTalkByHandleKey(...)` (talkerType=2, native uses handleKey)
- OpenSDK     -> `startOPSDTalk(...)` (parses playToken)

Both URL builders run **P2P-first, MTS-relay fallback**:

1. `GetTalkP2PUrlTask(deviceSn, channelId, uuid, devicePid)` returns a local P2P
   proxy port. If a port is returned, build a loopback URL:
   - DHHTTP visual: `http://127.0.0.1:<port>/live/visualtalk.xav?channel=<ch+1>&subtype=<n>&encrypt=<2|3|4>&level=2&talktype=<t>&proto=Private3`
   - DHHTTP audio:  `http://127.0.0.1:<port>/live/talk.xav?channel=<ch+1>&subtype=<n>&...&talktype=<t>&proto=Private3`
   - RTSP:          `rtsp://127.0.0.1:<port>/cam/realmonitor?channel=<ch+1>&subtype=<n>&encrypt=<n>&level=2&proto=Private3`
2. No P2P port -> MTS relay via `GetTalkDHHttpUrlTask` / `GetTalkRestUrlTask`,
   returning a `URLData` (`resource`, `tlsResource`, `quicResource`, + ipv6).

### Cloud talk-URL fetch

Two transport options inside `GetTalkRestUrlTask` / `GetTalkDHHttpUrlTask`:

- **IoT service call**: `cm_getTalkTransferStreamUrl` via `iotManager.doServiceSync(...)`.
- **Plain REST**: cloud method `things.media.GetTalkTransferStreamUrl`
  (`RestApi.asynGetTalkPlayAddress`, gateway `app-sg1-v3.easy4ipcloud.com/pcs/v1/...`),
  **20000 ms** timeout.

Request fields: `deviceId, channelId, encrypt, deviceType, talkType,
type (RTSP->"RTSP", DHHTTP->"RTSV1"), productId, audioType, design ("first"=unshared /
"second"=shared), streamId(=subType), assistStream, quic, bindDid, bindPid, bindCid`.

Response: `resource, ipv6Resource, quicResource, ipv6QuicResource,
quicInternalResource, internalResource, tlsResource, ipv6TlsResource, region`.

Transport constants (`TalkUtils`): `RTSP=1, HLS=2, NETSDK=3, LOCAL=4, P2P=5, DHHTTP=6`.

## createAudioTalker(json) Config

`TalkerParam.toJsonString()` emits one of three shapes by `talkerType`:

- **talkerType 0 (RTSP)**: `talkerType, isEncrypt, isTls, psk, url, encodeType,
  packType, streamSaveDirectory, sampleDepth, sampleRate, userName, psw,
  requestId, terminalId, deviceSn`
- **talkerType 1 (NetSDK direct)**: `talkerType, loginHandle, isTalkWithChannel,
  channel, isAutoDecideParam, encodeType, packType, sampleDepth, sampleRate`
- **talkerType 2 (DHHTTP / visualtalk / shared-link)**: adds `quicUrl, talkType,
  wsseKey, sharedLinkMode, handleKey, videoSampleEnable, videoSampleCfg`

Defaults: `psw="lc2014"`, username = OEM RTSP digest default, when not supplied.
`videoSampleCfg`: `{width, height, I_frame_interval, encodeType, frameRate, cameraStatus}`.

## Audio Codec & Sample Format

What the talk path actually sets (RTSP & DHHTTP branches in `LCSDK_Talk.mHandler`):

```text
setSampleRate(8000); setSampleDepth(16); setPackType(0); setEncodeType(14);
=> G.711a (alaw), 8000 Hz, 16-bit, DHAV packaging (packType 0)
```

Constants: `G711A=14, G711U=22, PCM=7`; `PACK_DHAV=0, PACK_OLD=1`;
`DEPTH_16=16, DEPTH_8=8`. (TalkerParam default ctor uses G711u/16000, overwritten.)

`pushMediaData(int type, byte[] data, boolean softEncode)`: `type 0`=audio,
`type 1`=video; `softEncode` = data is pre-encoded in software vs handed raw to
native encoder.

### Working Bridge Codec Profiles

| Camera/path | Input to bridge | Output to visualtalk | Notes |
| --- | --- | --- | --- |
| `remote_camera` remote TTS | generated/converted file audio | `AAC/ADTS 16000` in DHAV | Proven through DHP2P type-0 relay. |
| `lan_camera` Frigate mic | go2rtc `PCMA/8000` backchannel | `AAC/ADTS 16000` in DHAV | Proven from Frigate WebRTC mic; volume gain `5.0`. |
| G.711 profile | `alaw/8000` or `s16le` | DHAV G.711 A-law/16k or A-law/8k | Kept as a selectable profile for other models; do not overwrite globally. |

The Frigate helper therefore has explicit codec flags:

```bash
frigate_imou_talk_exec.py \
  --input-codec alaw --input-sample-rate 8000 \
  --output-codec aac-adts --sample-rate 16000 \
  --volume-gain 5.0
```

AEC / audio controls: `setAecEnable` (software AEC, via `setSoftwareAecEnable`),
`setHardwareAecEnable`, `setAudioRecScaling` (mic gain), `setSpeechChange`
(voice changer), `setAceDebugSavePath`.

## Lifecycle / State Machine

```text
startMediaTalk
  -> stopTalk(false)                 (tear down any prior session)
  -> getDHHTTPTalkUrl / getRTSPTalkUrl (async, P2P-first then MTS)
  -> mHandler: new TalkerParam(8000/16/pack0/encode14)
  -> AudioTalker.create() -> createAudioTalker(json)  (store handle)
  -> setListener(); setSoftwareAecEnable()
  -> AudioTalker.startTalk() -> NativeAudioTalker.startTalk(handle)  (1 = ok)
  -> native: startSampleAudio() captures mic, playSound() plays rx audio
  -> (visual talk only) pushMediaData(1, encodedVideoFrame, soft) loop
stop:
  mListener.K() -> stopTalk() -> destroyAudioTalker(handle); handle=0
```

- Session context id `mUUID = generateUUID()`, logged `<uuid>_TalkBegin_<ts>`.
  The talk request id is `mRequestId` (no separate `visualtalk_reqid` token).
- NetSDK login handle timeout 15000 ms; REST talk-url POST timeout 20000 ms.
- No Java-layer talk keepalive; it lives in the native lib.

## Listeners & Event/Error Codes

`TalkerListener` (impl by `AudioTalker`):
- `onTalkResult(String code, int type)` — type: 3=NetSDK, 5=DHHTTP-handle, 99=REST/MTS
- `onTalkBegan`, `onTalkPlayReady`
- `onAudioReceive(...)` — device->app audio; `onAudioRecord(...)` — captured mic
  (a hook here can intercept/replace mic data)
- `onDataLength`, `onSaveSoundDb`, `onIVSInfo`, `onTalkLogMessage`, `onTalkStreamLogInfo`

`LCSDK_TalkReportListener.onDataAnalysis(json)` on stop/failure
(`AnalysisTalkData`: link type, costs, p2p/mts stat, talk_type, voice_change...).

`LogEventStatus` (`LCSDK_StatusCode`): `Start=1000, getP2PPortBegin=1001,
getP2PPortOK=1002, getP2PPortFail=1003, getMTSUrlBegin=1004, getMTSUrlOK=1005,
getMTSUrlFail=1006, getFirstFrame=1007, PlaySuccess=1008, Stop=1009`.

Java result strings: `"0"`=ok, `"-1"`=fail, `"-2"`=param/null, `"-2000"`=startTalk
fail, `"-4000"`=playSound fail, `"-5000"`=exception, `"-1003"`=token parse error.
DHHTTP error codes are 6-digit (`130000`=encrypt error); RTSP `120000`=encrypt
error. When `encryptMode==4`, a stream/encrypt error auto-retries with the
secondary encrypt mode.

## Runtime / MITM Evidence

- **Superseded note:** the first MITM pass did not capture a live talk session.
  Later Frida/runtime work did capture the talk lifecycle and codec/session
  parameters; see "Runtime Findings" below and
  `docs/talk-protocol-reverse-engineering.md`.
- **Loopback-proxy architecture confirmed**: native SDK opens a pool of
  `127.0.0.1:<port>` TCP listeners (e.g. `runtime-check/*-sockets*.txt`).
- **Receive bridge works** (`scripts/imou_xav_bridge.py`): adb-forwards to a
  device `127.0.0.1:<port>` listener, does
  `GET /live/realmonitor.xav?channel=1&subtype=0&audioType=1&proto=Private3`
  with HTTP **Digest auth**, reads `Private-Length`-delimited **DHAV** frames.
  Talk would use the identical channel/auth, swapping `realmonitor.xav` for
  `talk.xav` / `visualtalk.xav`.
- DB schema (logcat) advertises talk capability via
  `DHChannelExtra.vctalkStatus/vctalkValue`, `DHDeviceExtra.vctalkStatus/...`.

## Runtime Findings (Frida, live capture)

Captured on a test Android phone against camera "remote camera" (serial
`EXAMPLESERIAL01`) by instrumenting the app with frida-gadget and hooking the
talk classes. Setup: gadget injected as `DT_NEEDED` of `libCommonSDK.so` in the
arm64 split, signed with the existing `patch.jks` (mitm key) so it installs as
an in-place update (no data loss); gadget listens on device `127.0.0.1:27420`.
The Java bridge is only available through the **frida CLI** (`frida -H
127.0.0.1:27420 -n Gadget -l hook.js`), not raw `create_script`.

Observed talk lifecycle (in order):

```text
LCSDK_Talk.setRequestId(<32-hex>)
LCSDK_Talk.startTalkByHandleKey(
    "EXAMPLESERIAL01+0",   // handleKey = <serial>+<channel>
    "talk",                 // talkType
    "EXAMPLESERIAL01",      // did
    "0",                    // channel
    "PFK9VU6R",             // (cid / short id)
    "LCTalkConfig(screen_name=standard_live_view, talk_type=talk)",
    "", null)
LCSDK_Talk.stopTalk(false)          // tear down prior
NativeAudioTalker.createAudioTalker(<json below>)  -> handle 522663969952
NativeAudioTalker.startTalk(handle)
NativeAudioTalker.startSampleAudio(handle)   // native mic capture starts
NativeAudioTalker.playSound(handle)          // rx audio playback
```

Runtime `createAudioTalker` JSON (the authoritative talk config):

```json
{"talkerType":2,"isEncrypt":0,"isTls":false,"psk":"","url":"","quicUrl":"",
 "encodeType":22,"packType":0,"sampleDepth":16,"sampleRate":16000,
 "userName":"","psw":"","talkType":"talk","wsseKey":"null","sharedLinkMode":2,
 "handleKey":"EXAMPLESERIAL01+0","requestId":"null","videoSampleEnable":false,
 "videoSampleCfg":{},"deviceSn":"null"}
```

Decisive facts for the bridge:

- **Unencrypted**: `isEncrypt:0`, `psk:""`, `isTls:false` — talk audio is plaintext.
- **Codec**: `encodeType:22` = **G.711 µ-law**, `sampleRate:16000`, `sampleDepth:16`,
  `packType:0` = **DHAV**. (Note: runtime uses G.711µ/16000, not the
  G.711a/8000 the static defaults suggested — the share-link path differs.)
- **Transport is share-link / handleKey** (`talkerType:2`, `sharedLinkMode:2`,
  `url:""`). Talk does **not** open a fresh `talk.xav`/RTSP connection — it
  **reuses the existing live-view P2P stream handle** (`handleKey =
  <serial>+<channel>`). Audio is multiplexed back into that same stream.
- **Wire bytes are not capturable at libc level**: every libc export
  (`connect/send/sendto/write/sendmsg/writev/__sendto_chk/syscall`) was hooked
  and saw **zero** talk traffic. The Dahua native engine uses **inline `svc`
  syscalls** and the internal send/DHAV functions are **stripped** (only JNI
  wrappers are exported). Capturing the exact DHAV talk frame therefore needs
  Frida **Stalker** (svc tracing) or RE of `libCloudClient.so`/`libCommonSDK.so`.

Environment paths (dead ends, confirmed against the LAN camera):

- HTTP CGI `audio.cgi` / `/videotalk` (port 80): port accepts TCP but never
  answers HTTP — Imou disabled the web/DHHTTP server on LAN.
- ONVIF RTSP backchannel (`#backchannel=1`, even with `&proto=Onvif`): SDP never
  advertises a `sendonly` track; go2rtc reports "can't find consumer".
- go2rtc `dvrip://`: that source is the Sofia/XMeye protocol (port 34567), not
  Dahua 37777 — it does not connect.
- Dahua private **37777**: alive, returns a `Realm:/Random:` login challenge
  (the only camera-native LAN talk interface), but the talk channel still needs
  a full DVRIP implementation.

## Bridge Implications & Next Options

Two-way talk on this Imou model is **confirmed working** (the app does it) and
the audio is **unencrypted G.711µ/16k/DHAV**. What remains is producing the talk
audio without the app. Three viable routes, in order of effort/payoff:

### Option A — Inject TTS via the app's own pipeline (fastest PoC)

The app captures the mic natively (`startSampleAudio`) and ships it over its
already-working P2P talk stream. Instead of reimplementing any protocol, **hook
the native mic-capture read and substitute TTS PCM** (frida, OpenSL ES / AAudio
capture callback inside `libCommonSDK.so`). The app then encodes + frames +
transports the TTS to the camera speaker for us. Requires the app + frida-gadget
running; no protocol RE. Best route to a first audible demo.

### Option B — Capture the DHAV talk frame, then replicate standalone

Get the exact on-wire DHAV talk audio frame via Frida **Stalker** (trace `svc`
in the talk thread — libc hooks are blind, internal symbols stripped). With the
frame format + the fact that talk reuses the realmonitor stream handle and is
unencrypted, build a standalone bridge that opens the stream and multiplexes
DHAV G.711µ audio back into it. Truest standalone architecture; medium-high effort.

### Option C — Implement Dahua 37777 talk

Camera-native LAN talk over the DVRIP binary protocol (37777 returns a
`Realm:/Random:` challenge). Fully app-independent but the most protocol work
(login + talk channel + DHAV audio framing).

### Reusable setup achieved this session

- Frida-gadget is installed in the app (injected as `DT_NEEDED` of
  `libCommonSDK.so`, signed with `patch.jks`, in-place update, login preserved).
  Launch app → `adb forward tcp:27420 tcp:27420` → `frida -H 127.0.0.1:27420 -n
  Gadget -l hook.js`. Use the **CLI** (Java bridge); raw `create_script` lacks it.
- Talk button: full live view → `iv_talk` (`adb shell input tap 323 923`).
- Known-good talk params to target: G.711µ-law, 16000 Hz, 16-bit, DHAV, encrypt 0.
