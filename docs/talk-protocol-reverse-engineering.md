# Imou Two-Way Talk — Binary-Level Reverse Engineering

Definitive reverse-engineering of the Imou Life two-way talk (talkback) pipeline,
done by **reading the native code** (radare2 + capstone on `libCommonSDK.so`) and
**hooking the running app** (Frida on the patched APK, S21). No network MITM needed.

Goal: push arbitrary audio (e.g. TTS) to a camera speaker without the app — turn the
camera into a media player.

**TL;DR:** The on-air talk **frame format is fully solved** (verified against real
frames captured live from the app). The talk is **not** a standalone connection — it
is multiplexed (`http_client_put_frame`, trackID=5) into the **same P2P live-view
stream session**. Local direct talk is therefore blocked: `talk.xav` is half-duplex
and `/videotalk` needs the P2P session.

## ✅ WORKING TTS (the shortcut that actually plays audio)

Rather than reimplement the cloud protocol, **let the app do everything and replace
the mic PCM** it encodes. Frida-hook the AAC encoder's input and overwrite the PCM
with our own audio; the app encodes it to AAC and ships it over P2P; the camera plays
our voice. **Verified: the "Kiara-Phòng khách" camera spoke an injected TTS clip.**

- Hook point: `LCAACAudioEncoder::Encode` = **`libCommonSDK.so` vtable[3] @ patched
  `0x995240`** (typeinfo `N2LC7PlaySDK17LCAACAudioEncoderE` → vtable → `vt[3]`; the
  method whose 2nd arg is a ~1280-byte buffer). `vt[2]` = `Open(bits, rate=16000,
  bitrate=256000)`.
- Signature: `Encode(this, int16_pcm*, byte_len, ...)`; PCM = **mono s16le @ 16 kHz**.
- Overwrite `pcm[0:byte_len]` each call with successive bytes of the clip → the app's
  own AAC encode + `HTTPDH_START_TALK` handshake + framing + DHEncrypt3 + P2P all run
  unchanged, so the camera accepts and plays it.
- Tool: [`scripts/imou_tts_inject.py`](../scripts/imou_tts_inject.py)
  (`--text "..."` / `--audio file`), then toggle the app's talk button ON.
- Note: this camera's talk codec is **AAC @ 16 kHz** (payload showed `ff f1` ADTS +
  `Lavc60.31.102`), not G.711 — another reason PCM-level injection (codec-agnostic)
  beats injecting encoded frames.

The pure-protocol path below is now also working for the tested relay route:
`scripts/imou_dhp2p.py` opens the P2P/PTCP realm in pure Python, and
`scripts/imou_visualtalk.py` opens `visualtalk.xav`, starts talk, and sends
interleaved DHAV audio frames. The encoder-injection method remains the quickest
audible app-assisted path, while the pure Python route is the add-on/container
candidate.

## App-free talk protocol (`visualtalk.xav` over the P2P realm) — captured

Captured by hooking libc `sendto`/`recvfrom` on the patched app (the app writes clean
DHHTTP to the realm fd with normal libc calls — only the UDP/PTCP layer below uses
inline `svc`). This is the exact sequence to reproduce over a `dh-p2p` realm tunnel.

The talk endpoint is **`/live/visualtalk.xav`** (not `/videotalk`, not `talk.xav`).
It is RTSP-like over the realm: a single keep-alive connection, incrementing `Cseq`.

```
# Cseq 0 — open session (SDP offer + auth)
PLAY /live/visualtalk.xav?channel=1&subtype=0&encrypt=3&imagesize=18&audioType=1&trackID=31&method=0 HTTP/1.1
Accpet-Sdp: Private                         (sic: misspelled by the app)
Authorization: WSSE profile="UsernameToken"
Connect-Type: P2P
Connection: keep-alive
Cseq: 0
Host: 127.0.0.1:<realm-port>
Private-Length: 717                          (client SDP offer body follows)
Private-Type: application/sdp
Speed: 1.000000
User-Agent: Http Stream Client/1.0
WSSE: UsernameToken Username="admin", PasswordDigest="<b64>", LightweightDigest="<b64>"
<blank line>
<717-byte SDP offer>
  -> 200 OK, Private-Type: application/sdp, Private-Length: 644, Content-Type: video/e-xav
     <server SDP: v=0 ... m=video 0 RTP/AVP 96 a=control:trackID=.. + audio tracks>

# Cseq 1 — start a track
PLAY ...&trackID=6&method=0 HTTP/1.1 ... Cseq: 1     -> 200 OK, then media frames stream

# Cseq 2 — start a track
PLAY ...&method=2 HTTP/1.1 ... Cseq: 2               -> 200 OK + interleaved frames

# Cseq 3 — START TALK
PLAY ...&talktype=talk&trackID=64&method=0 HTTP/1.1 ... Cseq: 3   -> 200 OK
# after this 200, push talk audio frames interleaved on the realm:
#   "$" + channel + length(4 BE) + DHAV(AAC) frame
```

Notes / remaining unknowns for a standalone client:
- **Transport:** `scripts/imou_dhp2p.py <SERIAL> --relay --bind 127.0.0.1:<p>
  --remote-port 8086` gives the realm pipe in pure Python. The older Rust
  `dh-p2p -p 127.0.0.1:<p>:8086 --relay <SERIAL>` remains a useful comparison
  implementation.
- **Auth:** the cloud/P2P WSSE and device-auth helpers now have a pure-Python
  implementation in [`scripts/imou_wsse.py`](../scripts/imou_wsse.py). Native
  confirms `/dev/urandom` alphanumeric nonces for the SDK WSSE client, while the
  public DHP2P path uses a decimal nonce and
  `Base64(SHA1(nonce+created+"DHP2P:<user>:<key>"))`. Device-auth `Login to`
  keying, HMAC-SHA256 `DevAuth`, PBKDF2-SHA256 and AES-256/OFB LocalAddr encryption
  are also implemented. For `visualtalk.xav`, the tested digest is
  `Base64(SHA1(nonce + created + MD5("user:<realm>:password").upper()))`.
- **Encryption (TODO):** the app uses `encrypt=3` (DHEncrypt3) — the on-realm media
  payloads are encrypted (recv frames are high-entropy with no ADTS). For an app-free
  client either (a) try `encrypt=0` in the PLAY URL for a plaintext session, or
  (b) reverse `CDHEncrypt3` (Src/StreamSource/DHEncrypt3.cpp). NOTE the *send* path in
  the running app appeared unencrypted in earlier tests; confirm whether `encrypt=3`
  applies to the talk-send frames or only the recv/video.
- **Frame format:** the 28-byte DHAV header (above), AAC payload @ 16 kHz, trackID=5
  on the wire (interleave channel `0x0a` = 2×5).

The standalone client path is now proven through an audible TTS test:
Cseq 0 and START TALK both returned `200 OK`, then AAC/ADTS DHAV frames on
trackID=5 were sent through the pure Python relay tunnel and heard on the camera.
The returned SDP for this camera marks trackID=5 as `sendonly`
`MPEG4-GENERIC/16000`; G.711 frames complete the protocol but are silent here.

## Feasibility: REUSE `libCommonSDK.so` instead of reimplementing the crypto

A from-scratch standalone client used to look blocked at **DevAuth** (the
device-auth in the P2P `local-channel` rendezvous; `p2p-channel` can 404 for
account-bound devices). The older/public device-auth variant is now pure Python:
`MD5("<user>:Login to <RandSalt>:<password>").upper()`, PBKDF2-SHA256,
AES-256/OFB and HMAC-SHA256. The remaining native-heavy part is the newer
account-issued P2P v2 material (`DevP2PAk`, `DevP2PSk`, `p2pSalt`) and exact key
selection for models that reject the public/type-0 path.

**The pragmatic feasible path is to not reimplement any of it — reuse the .so**, which
already does DevAuth + P2P + visualtalk + DHEncrypt3 internally. Three flavors, easiest
to hardest:

1. **Frida-driven automation (works today, reuses the whole app):** a Frida script
   that (a) calls the app's talk JNI to open talk to a camera programmatically (no
   manual UI tap) and (b) replaces the mic PCM at `LCAACAudioEncoder::Encode`
   (vtable[3] @ patched `0x995240`) with our audio. This is the current working method
   plus auto-triggering. Needs the patched app installed. (`scripts/imou_tts_inject.py`
   already does (b); add a Java hook calling `NativeAudioTalker` start/playSound for (a).)

2. **Minimal harness APK reusing the .so (truly app-free of the Imou UI):** a small APK
   that bundles `libCommonSDK.so` (+ deps `libCloudClient/libnetsdk/...`) and the
   handful of Java glue classes (`com.iotcom.commonsdk.talk.NativeAudioTalker`,
   login/play managers — extract from the decompiled app), logs in with the account,
   opens a talk session to a serial, and feeds our PCM to the encoder. Reuses ALL the
   hard crypto/P2P from the .so; only the orchestration glue is rebuilt.

3. **Native harness (`dlopen` the .so) — hardest:** drive the .so from a tiny native
   process. The talk surface is JNI (needs a JavaVM/JNIEnv), so this still pulls in the
   Java glue; flavor 2 is strictly easier.

Clean API to drive (decompiled from the app — the whole talk stack sits on the .so):
- **`com.lc.lcsdk.LCSDK_Talk.INSTANCE.startDHTalk(deviceSN, channelId, isTalkWithChannel,
  isAutoDecideParam)`** — one call; the .so does P2P + DevAuth + visualtalk + DHEncrypt3
  internally. Also `startTalkByHandleKey(handleKey, …)`, `stopTalk()`, `playSound()`,
  `startSampleAudio()`, `pushMediaData(int type, byte[] data, boolean softEncode)`.
- `com.lc.common.talk.AudioTalker` → `NativeAudioTalker.createAudioTalker(
  talkerParam.toJsonString())` returns the handle; `pushMediaData(handle, type, bytes,
  len, soft)`. **`type=1` is VIDEO** (the visualtalk video encoder pushes type 1); talk
  AUDIO is captured natively by `startSampleAudio()` (mic). So inject audio either by
  the encoder-injection hook (works) or test `pushMediaData(0, aac, …)` for audio.

### Standalone APK recipe (reuse .so) — import the device session, no login/crypto

The whole SDK init reduces to two calls + the talk chain. The importable "session" is
the **`LCSDK_Login.addDevices(json)`** argument — a `List<DeviceLoginParams>`, all
portable strings (captured live):

```json
[{ "Sn":"<serial>", "User":"admin", "Pwd":"<device-password>", "Port":8086, "Type":1,
   "DevP2PAk":"LeChange\\v2\\Base\\phone\\easy4ipbaseapp\\<accountId>\\<p2pAk>",
   "DevP2PSk":"<base64 P2P secret key>",
   "DevP2PInfo":"{\"devSn\":\"<serial>\",\"p2pSalt\":\"<16hex>\",\"p2pVer\":\"6.0.x\"}",
   "extP2PInfo":[] }]
```

This is why the newer SDK route can resist offline cracking with only the password:
some devices appear to require `DevP2PSk` (an account-issued secret) + `p2pSalt` +
nonce, not just the device password. By importing `DevP2PAk`/`DevP2PSk`/`p2pSalt`
(from the cloud device list / captured from the app), the .so computes that route
itself. Native handles
(`handleKey`/`loginHandle`) are NOT importable (in-process pointers) — but these
DeviceLoginParams ARE, and the .so opens its own P2P session from them.

APK flow (own process):
```
LCSDK_Login.getInstance().init(p2pHost, p2pPort, ipv4, port, ipv6, terminalId, bool) // SDK+P2P init
LCSDK_Login.getInstance().addDevices(<DeviceLoginParams json above>)                 // import session
// open a stream to get a handleKey (needs a Surface):
LCSDK_PlayWindow(...).playRealTimeStream(serial, channel, encryptMode, ... )         // .so does P2P/DevAuth
LCSDK_Talk.INSTANCE.startTalkByHandleKey(handleKey, ...)                             // talk
// inject TTS: hook LCAACAudioEncoder::Encode (0x995240) OR pushMediaData(type=audio)
```
`init(...)` P2P host confirmed = **www.easy4ipcloud.com:8800** (same as dh-p2p
MAIN_SERVER; from boot-log TLS + libCommonSDK); terminalId = a self-generated UUID.
Remaining for the build: set up an Android build (apktool repackage, or gradle+SDK) bundling
`libCommonSDK.so`(+deps) and the SDK classes; provide a hidden `Surface` for play.
DeviceLoginParams come from the cloud device-list + p2p-info APIs (see login-api.md).

Recommended: **flavor 1** — Frida-call `LCSDK_Talk.INSTANCE.startDHTalk(<serial>, 0,
false, true)` on the running (logged-in) app to start talk with no UI tap, then inject
TTS at the encoder → fully scriptable today, reusing the entire .so stack (no DevAuth
work). **Flavor 2** — bundle `libCommonSDK.so` (+deps) and the Java classes
(`LCSDK_Talk`, `AudioTalker`, `NativeAudioTalker`, `TalkerParam`, login/play managers)
into a minimal APK for a UI-independent client. Reimplementing DevAuth from scratch
(no .so) is the only genuinely hard path and is not recommended.

### Encryption finding (good news for app-free): the talk SEND is plaintext

Investigating `CDHEncrypt3` (Src/StreamSource/DHEncrypt3.cpp, typeinfo
`N5Basic9StreamApp11CDHEncrypt3E`, vtable @ patched `0x10dcb40`) showed that during a
talk session **only `vt[10]` (the "decode" path, @ `0x78e8e0`) fires** — that is the
*receive* direction (decrypting the camera's own mic audio coming back). **No encrypt
method fires on the SEND path.** Corroborating evidence that talk send is unencrypted:

- A real captured wire talk frame (gateway-MITM, UDP/PTCP payload) has byte-entropy
  **~4.7** and contains readable ASCII — encrypted data would be ~7.99.
- The recv AAC frame entropy is ~7.0; that earlier-suspected "encryption" is just
  **AAC's natural entropy**, not a cipher.
- At `Pack` the payload is already cleartext AAC (`ff f1` ADTS) and nothing encrypts
  it before the wire.

**Implication:** an app-free talk client needs **no crypto** — only the protocol:
dh-p2p realm + DHHTTP-media client + `HTTPDH_START_TALK` handshake + plaintext AAC
DHAV frames (trackID=5, `$`+ch`0x0a`+len(4 BE)). (Receiving/decrypting the camera's
returned mic audio would need `CDHEncrypt3`, but that is not required for TTS.)

---

## 1. The DHAV talk frame format (SOLVED)

Decompiled from `LCDHAVAudioPacker::Pack` — the class's `vtable[2]`. Found via the
Itanium RTTI: typeinfo-name string `N2LC7PlaySDK17LCDHAVAudioPackerE` → typeinfo
struct → vtable (read through `R_AARCH64_RELATIVE` relocations) → `vtable[2]`.
Pure-Python replacement: [`scripts/imou_dhav.py`](../scripts/imou_dhav.py) implements
G.711 µ-law/A-law encode, DHAV audio packing, and DHHTTP interleaving; its checksum
self-test is verified against the captured frame below.

A talk audio frame = **28-byte header + raw audio payload + 8-byte trailer**
(total length field = `payload_len + 0x24`, i.e. 28 + 8 = 36 overhead bytes):

| Offset | Size | Field | Notes |
|-------:|-----:|-------|-------|
| 0x00 | 4 | `"DHAV"` | magic `44 48 41 56` |
| 0x04 | 1 | frame type | `0xf0` = audio (`0xf1` if a flag bit set) |
| 0x05 | 3 | `00 00 00` | |
| 0x08 | 4 | sequence | LE, increments per frame (packer holds counter at `this+8`) |
| 0x0C | 4 | total length | LE, = `payload_len + 0x24` |
| 0x10 | 4 | timestamp (packed) | observed pattern `~0x69b7xxxx` |
| 0x14 | 2 | tick | 16-bit |
| 0x16 | 1 | `0x04` | constant |
| 0x17 | 1 | checksum | `(Σ bytes[0x00..0x16]) & 0xff`; equivalent to the earlier decompile note `(Σ bytes[0x00..0x15] + 4) & 0xff` |
| 0x18 | 2 | `83 01` | audio-info tag (LE `0x0183`) |
| 0x1A | 1 | `0x1a` | **constant** (NOT codec — same for AAC recv and G711 talk) |
| 0x1B | 1 | sample-rate code | see table below |
| 0x1C.. | n | **payload** | raw G.711 samples |
| +n | 4 | `"dhav"` | trailer magic `64 68 61 76` |
| +n+4 | 4 | total length | LE, repeated |

**Sample-rate code** (from the `Pack` switch): `8000→2`, `11025→3`, `16000→4`,
`20000→5`, `22050→6`, `32000→7`, `44100→8`, `48000→9`.

**The codec (G.711A vs µ-law vs AAC) is NOT encoded in the header** — it is implied
by the negotiated track (the talk track's SDP). Both an AAC recv frame and a G.711
talk frame carry identical `83 01 1a 04` audio-info bytes.

### Real frame captured live (Frida, camera "Cam 3")
```
44484156 f0 000000 | e6070000(seq) | b0010000(len=0x1b0) | b969b769 | aa6d | 04 | 0e(cksum) | 8301 1a 04 | <payload> | dhav <len>
```
`[0x1B]=0x04` → **the app talks at 16 000 Hz G.711**, even though a camera's local
`talk.xav` SDP may advertise `PCMA/8000`. payload ~380–420 bytes/frame.

---

## 2. The full send pipeline (traced end-to-end)

Offsets below are in the **LIEF-patched** `libCommonSDK.so` as installed (the gadget
patch shifts the unpatched artifacts copy by ~0x1000 — always recompute on the
installed split APK).

```
mic capture
  → G.711 encoder (16 kHz)                         LCAudioEncoderManager / LCG711*AudioEncoder
  → LCDHAVAudioPacker::Pack  (DHAV frame)          vtable[2]  @ 0x995dc8
  → LCAudioController                              caller; sends frame via thunk:
        sub_992f08(sink, buf, len):  cb=[sink+0x40]; ctx=[sink+0x48]; cb(buf,len,ctx)
  → BaseTalker::onAudioPacketFromPlaySDK           @ 0x570ee4
        calls  talker->vtable[0xc0](buf, 0, len)
  → DHHTTPTalker::sendAudioData                    @ 0x56a58c  (PlayerManager)
        builds frame-descriptor { trackID=5, flag=0x41, len, dataptr=DHAV }
  → http_client_put_frame                          @ 0x88ead8  (HttpClientSessionWrapper.cpp)
  → HttpClientSessionWrapperImp                    @ 0x892dd4
  → HttpDh client state machine                    HttpDhClientStateMachine.cpp
  → CTransformatDHInterleave                       Src/Media/Transformat/TransformatDHInterleave.cpp
        → wire framing: "$" + channel + len(4 BE) + DHAV
  → PTCP / Easy4IP P2P transport                   final UDP write via inline `svc` (libc hooks blind)
```

Key point: `DHHTTPTalker` runs in **share-link mode** and **reuses the live-view P2P
stream handle** (`[talker+0x468]` / `[talker+0x440]`). The talk frame is
`put_frame`'d as **trackID=5** onto the *same* session that carries live video — it
is *not* a separate socket/request.

---

## 3. Transport / wire framing

- **`talk.xav` (port 8086, AEDA HTTP, digest auth).** `GET /live/talk.xav?channel=1&
  subtype=0&proto=Private3` → `200` + a 340-byte SDP, then the camera streams its own
  mic as `$` + `ch 0x0c` + `len(4 BE)` + DHAV (AAC). SDP advertises the talk track:
  `m=audio RTP/AVP 8 / PCMA/8000 / a=control:trackID=5 / a=sendonly`.
  Interleave channel = `2 × trackID` (mic track 6 → `0x0c`; so the talk send track
  5 → **`0x0a`**).
  **This socket is half-duplex** locally: frames pushed back on it (every channel /
  rate / framing tried on a working-speaker camera) produce no speaker output.

- **`/videotalk` (port 8086).** The real send endpoint template (rodata):
  `GET /videotalk HTTP/1.1` + `HOST: Talk Server/1.0` + `User-Agent:Talk Client` +
  `Content-Type: Audio/PrivateFrame` + `Transfer-Encoding: chunked`. Method is **GET**
  only (GET passes digest → server `500`; POST/NFGET/NFPOST → `401`). It **`500`s
  locally and even over a naive dh-p2p→8086 tunnel** — it belongs to the
  **share-link / P2P** path (`http_client_*`, strings `share_main_link` /
  `share_sub_link`) and needs the P2P stream **session context**, not just a pipe to
  port 8086.

- **State machine.** `HttpDhClientStateMachine` with states `HTTPDH_START_TALK`,
  `HTTPDH_MEDIA_TALK`, `HTTPDH_STOP_TALK`, `MSG_HTTPDH_START_TALK` — the talk-enable
  handshake on the live session (the `http_client_init_sdp_for_talk` →
  `init_back_sdp` → `enable_media` sequence). **Not yet byte-decoded.**

---

## 4. Why each "obvious" path fails (all reconfirmed)

| Path | Result |
|------|--------|
| RTSP 554 ONVIF backchannel (go2rtc) | No `sendonly` track on Imou firmware (probed `proto=Onvif`/`Private3`/none). Community (Frigate #14383) hit the same wall. |
| NetSDK `speak.*` (37777) | `InterfaceNotFound` on consumer models |
| `talk.xav` send (8086) | socket half-duplex — silent |
| `/videotalk` (8086) | `500` — needs P2P session context |
| dh-p2p tunnel → 8086 → `/videotalk` | still `500` (a transparent pipe ≠ the stream session) |

**Nobody public has achieved Imou-consumer two-way audio.** Every public solution is
either for "real" Dahua (which exposes RTSP backchannel) or documents failure on Imou.
This RE goes past the public frontier: the frame format and full send chain are
solved; only the in-session talk-enable handshake remains.

---

## 5. Remaining work for working TTS (implementation, not discovery)

1. **Capture the wire handshake.** Frida-hook `CTransformatDHInterleave`'s process fn
   (above the inline-`svc` write) during a real talk to dump the exact on-wire bytes:
   the `HTTPDH_START_TALK` handshake + the interleaved talk frames.
2. **Build a DHHTTP-media client** over the dh-p2p tunnel (already in repo): open the
   live realmonitor session, replay the talk-enable handshake, then inject
   **16 kHz G.711 DHAV** frames as `$` + `ch 0x0a` + `len(4 BE)` on that session.
3. **Test on an audible camera.** Best target: an indoor account camera with a working
   speaker that is P2P-reachable (e.g. "Kiara-Phòng khách"), with someone nearby to
   listen.

---

## 6. Reproduction notes

- Disassembly: `radare2` + a capstone/pyelftools venv. Resolve vtables by reading
  `R_AARCH64_RELATIVE` relocation addends (the raw pointers are 0 in a PIE `.so`).
- The installed app is **LIEF-patched** (Frida gadget as `DT_NEEDED` of
  `libCommonSDK.so`) → its `.text` is shifted vs the artifacts copy; recompute all
  offsets on the installed split APK (`split_config.arm64_v8a.apk`).
- Frida: `frida -H 127.0.0.1:27420 -n Gadget -l hook.js` (gadget on device port
  27420; `adb forward tcp:27420 tcp:27420`). The CLI loads the Java bridge; raw
  `create_script` does not. Gadget PID changes per app launch — reattach each time.
- Talk button (full live view): `adb shell input tap 140 945` (toggle; the mic icon
  turns orange when ON). Verify state with a screenshot.
- Frame format / pipeline confirmed against frames captured live via the `Pack` hook.
