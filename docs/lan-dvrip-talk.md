# LAN Two-Way Talk via Dahua DVRIP (Option C)

Goal: push audio (e.g. TTS) to a camera's speaker **directly on the LAN** — no
Imou app, no cloud, no P2P. This targets the cameras physically on the local
network (`192.168.2.20/.21/.22`), which are Dahua-family Imou devices.

## Cloud vs P2P (what the app actually does)

Determined by reading the app's live network table (`/proc/net/tcp|udp` for the
app uid while streaming + talking, via the frida-instrumented build):

- The app's account cameras ("Cam sân" serial `EXAMPLESERIAL01`, "Cam ngoài
  đường") are **remote** (a different site, Viettel Nam Định) and are reached by
  **P2P over UDP** to the camera-side public IP `<camera-public-ip>` (many dynamic
  high ports = the media/talk channels). The serial does **not** match any LAN
  camera realm, confirming these are not the local devices.
- The Imou/Alibaba **cloud is only used for signaling/control** (`47.236.x:443`,
  `8.214.105.88:8883` MQTT) — **media and talk do not flow through Imou's cloud**.
- So the app path is **P2P, not cloud-relayed**. The cloud only does rendezvous.

Consequence: the LAN cameras (`.20-.22`) are a separate concern from the app's
remote cameras. A bridge running on the LAN (the Mac is on `192.168.2.0/24` and
can reach the cameras on 554/37777) can talk to them **directly**, skipping P2P
and cloud entirely.

## DVRIP login — WORKING

`scripts/imou_dvrip.py` logs in to `192.168.2.20:37777` and runs JSON-RPC, with
no app/cloud/P2P. Protocol (derived from mcw0/DahuaConsole):

1. **Challenge request** (binary): `0xA0010000` + 20×`00` + `0x050201010000A1AA`.
   Response body: `Realm:Login to <serial>\r\nRandom:<random>\r\n`.
2. **Hash login** (binary): header `0xA0050000` + `<len>`(LE) + 16×`00` +
   `0x050200080000A1AA`, body = `"<user>&&" + gen2 + gen1dvrip` where
   - `gen2 = MD5(user:random: MD5(user:realm:pass).upper() ).upper()`
   - `gen1dvrip = MD5(user:random: <gen1hash(pass)> ).upper()`
   - `gen1hash` = the legacy Dahua "compressor" hash (haicen/DahuaHashCreator).
   Success when response header bytes `[8:10] == 00 08`; SessionID = `[16:20]` (LE).
3. **JSON-RPC** afterwards: header `0xF6000000` + len(LE) + id(LE) + 0 + len(LE)
   + 0 + sessionId(LE) + 0, body = `{"method":...,"id":...,"session":...}`.

Verified against `192.168.2.20`:
- `magicBox.getDeviceType` → **`IPC-C22E-A`** (Imou indoor, has mic **and speaker**).
- firmware `2.680.0000000.30.R` (2023-10-26).
- `magicBox.getProductDefinition` → `AudioProperties.Support: true`, plus an
  `AudioFileManager.SirenFileManager` (the device can play stored audio files).

Note: this camera answers the **legacy DVRIP** binary login on 37777; it does
**not** answer DHIP (the `0x20000000 'DHIP'` framing) on that port, and the
HTTP/CGI interface on port 80 is disabled.

## Remaining work — the talk audio channel

Login + RPC are done. Two routes to actually get audio onto the speaker:

1. **Real-time talk** (`CLIENT_StartTalkEx`-style): a binary talk sub-protocol —
   claim the talk channel, then stream audio frames (G.711, DHAV-wrapped). This
   is **not plain JSON-RPC** and is the undocumented part. Candidate RPC probes
   (`*.getCaps`, `consoleTalk.*`, `Audio.*`) all returned `InterfaceNotFound`;
   `mediaFileFind.factory.create` works (factory pattern is available). Needs
   either NetSDK protocol detail or a LAN packet capture of a real Dahua client
   (e.g. ConfigTool/SmartPSS/gDMSS) talking to this camera.
2. **Audio-file play** (possibly simpler for TTS): the device exposes
   `AudioFileManager`/`SirenFileManager` and plays `.aac` hint/siren files. If a
   custom file can be uploaded + triggered, that yields TTS-on-camera without the
   real-time talk protocol. Method/config names still need to be found
   (`configManager.getConfig name=AudioFileManager` errored — wrong name).

Codec target (from the app's runtime talk config, see `two-way-audio.md`):
G.711 (µ-law), 16000 Hz, 16-bit, DHAV, unencrypted.

## How the app talks to the camera (static analysis of the .so)

Reading the native libraries (`libCommonSDK.so`, `libnetsdk.so` from the arm64
split) shows exactly what happens when the talk button is pressed. The Java
`NativeAudioTalker` is a thin shim; the real work is in C++ `Talker` classes that
pick one of **three transports** depending on the link:

| Talker (libCommonSDK) | Transport | Mechanism |
|---|---|---|
| `DeviceTalker` (`TalkComponent/talker/DeviceTalker.cpp`) | **NetSDK / TCP 37777** | `CLIENT_StartTalkEx` → `CLIENT_TalkSendData` → `CLIENT_StopTalkEx` |
| `RTSPTalker` | RTSP 554 | `Create RTSPTalker: url=%s EncryptType=%d` |
| `CDHHTTPClient` / httptalker | DHHTTP | `GET /videotalk HTTP/1.1` (same endpoint as the tenable PoC) |

Plus a **share-link** path (`CShareHandleManager::startTalk`, `talk getStream by
share_main_link/share_sub_link, handleKey[%s]`) which reuses an existing stream
handle — this is what the runtime capture showed (handleKey `<serial>+<channel>`).

Audio pipeline (libCommonSDK), identical regardless of transport:

```
mic PCM
  -> LCAudioEncoderManager::CreateAudioEncoder  (default G.711A; also G.711/AAC)
  -> LCDHAVAudioPacker                           (wrap encoded audio in DHAV)
  -> talker sends it (CLIENT_TalkSendData / RTSP / /videotalk)
```

### The NetSDK / 37777 path (`DeviceTalker`) — our LAN target

`libnetsdk.so` is the real Dahua NetSDK (JNI: `Java_com_company_NetSDK_INetSDK_
StartTalkEx/TalkSendData/StopTalkEx`). It keeps C++ symbols, which reveal the flow:

1. **Login** — the DVRIP `a0…/f6…` protocol already implemented in
   `scripts/imou_dvrip.py` (NetSDK `CLIENT_LoginEx`).
2. **Negotiate talk encode** —
   `CDevConfig::GetDevTalkFormatList(DHDEV_TALKFORMAT_LIST)`,
   `CDevConfigEx::GetDevNewConfig_TalkEncode/SetDevNewConfig_TalkEncode`
   (`DHDEV_TALK_ENCODE_CFG`), config param **`Device.Audio.Talkback.Cfg`**
   (and `Device.Network.Talk.General`). Runtime log:
   `DeviceTalker::getStream. CLIENT_QueryDevState + DH_DEVSTATE_TALK_ECTYPE`,
   `set device talk encode mode success`.
3. **Open talk channel** — `CDvrDevice::device_open_talk_channel` → `CLIENT_StartTalkEx`.
4. **Stream audio** — encode → `LCDHAVAudioPacker` (DHAV) → `CLIENT_TalkSendData`
   on the 37777 connection.
5. **Stop** — `CLIENT_StopTalkEx`.

Architecture note: the NetSDK does **control via JSON-RPC** (`AsyncJsonRpcCall`,
the `…ret` methods, `*.factory.instance`) over the same `0xf6` framing my RPC
client already speaks; **media/talk audio rides a binary data channel** (DHAV
frames via `CLIENT_TalkSendData`), not JSON-RPC. Realplay uses the parallel
`AV_RealPlay`/`AV_QueryRealPlayURL` layer.

### What is still needed to implement it

Two concrete unknowns, both obtainable from `libnetsdk.so`:

1. The exact **talk-open** request — what `device_open_talk_channel` /
   `CLIENT_StartTalkEx` puts on the wire (likely a JSON-RPC over `0xf6` to set
   `Device.Audio.Talkback.Cfg` + open an audio talk stream, returning a stream id).
2. The **audio data-channel header** — how `CLIENT_TalkSendData` frames each DHAV
   audio packet on the 37777 socket (the binary header carrying the stream id).

Next step to get these bytes: disassemble `libnetsdk.so` around `CLIENT_StartTalkEx`
/ `CLIENT_TalkSendData` (dynsym addresses are present; a decompiler like
radare2/ghidra is needed — internal builders are unnamed), or capture a live
NetSDK talk session. The DHAV audio frame layout can also be cross-checked
against the existing receive captures (`runtime-check/*.dhav`).

## Decompile results (radare2) + on-device probing

Decompiled `libnetsdk.so` (it ships C++ symbols for `CTalk`/`CReq*`/`CDvr*`) and
probed the real camera over the working DVRIP JSON-RPC channel.

### NetSDK talk = JSON-RPC `speak.*` over the 0xf6 channel

`CLIENT_TalkSendData` → `CTalk::TalkSendData` → `CTalk::SendData2Dev` (vtable send
with flag `0x80000010`). `CTalk::StartChannel` → `CReqStartChannel` →
`CManager::JsonRpcCall(afk_device, IREQ, payload, len, …)` — i.e. control is
JSON-RPC and the audio rides as an appended binary payload (`[JSON]\n[DHAV]`).
The talk method namespace recovered from the binary:

```
speak.startChannel / speak.sendChannel / speak.stopChannel   (channel API, newer)
speak.startPlay / speak.startPlayEx / speak.stopPlay / speak.stopPlayEx  (older)
```

**But this camera does not implement them.** Over the authenticated DVRIP session,
`speak.startChannel`, `speak.startPlay`, `audioTalk.factory.instance`,
`RealPlay.factory.create`, `ptz.*`, `RPC2.*` all return **`InterfaceNotFound`**.
The only JSON-RPC services exposed are a minimal set (`mediaFileFind.factory.create`,
`devVideoInput.factory.instance`, `configManager.getConfig`, `global.getCurrentTime`,
`CoaxialControlIO.getCaps`). So the **NetSDK/`speak` talk path is not available on
the IPC-C22E-A** — consistent with the app using `CDHHTTPClient` (not `DeviceTalker`)
for these consumer devices.

### The app's three talkers (from `TalkComponent/talker/*.cpp`)

- **DHHTTPTalker** (`DHHTTPTalker.cpp`, `Src/Stream/HttpPrivate/TalkHttp.cpp`,
  `Src/Rtsp/HttpTalkBack/`): `GET /videotalk HTTP/1.1` with
  `…&talkbackChannelId=%d&talktype=…&proto=Private3`; a bidirectional DHHTTP stream
  (`CHttpTalkbackClientSession`, `CHttpTalkbackStreamSeparator`). This is the path
  the app actually uses for its (P2P/share-link) cameras — tunneled over P2P, **not
  exposed on a direct LAN TCP port** (port 80 / 37777 `/videotalk` both time out).
- **RTSPTalker** (`RTSPTalker.cpp`, `Src/Stream/Rtsp/TalkRtsp.cpp`): RTSP talk over
  554 using **`ANNOUNCE`** (Dahua-private talkback, distinct from the ONVIF
  backchannel that this camera does not advertise). URL base
  `rtsp://…/cam/realmonitor?channel=N&subtype=M&proto=Private3`.
- **DeviceTalker** (`DeviceTalker.cpp`): NetSDK `speak.*` — not supported here.

Audio pipeline for all: mic → `LCAudioEncoderManager` (G.711A/U; AAC) →
`LCDHAVAudioPacker` (DHAV) → talker.

### Conclusion for LAN-direct talk on IPC-C22E-A

This consumer camera exposes **no direct-LAN talk service**: no NetSDK `speak.*`,
no `/videotalk` HTTP port, no ONVIF RTSP backchannel. The app reaches talk only
through the camera's **P2P/DHHTTP** channel. The one remaining LAN-direct avenue
is **RTSP `ANNOUNCE`/`RECORD` talkback on 554** (the `RTSPTalker`/`TalkRtsp.cpp`
path) — to be tested: `ANNOUNCE rtsp://…/cam/realmonitor?…&proto=Private3` with an
audio-only SDP (G.711), then `RECORD`, then send DHAV/RTP audio. If the camera
rejects RTSP `ANNOUNCE`, LAN-direct talk is not feasible on this model and talk
must go via the P2P/DHHTTP path (i.e. through the app or a P2P bridge).
