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

The pure-protocol path below (a standalone DHHTTP-media client over dh-p2p) is still
documented for an app-free implementation, but the encoder-injection method above is
the working solution today.

---

## 1. The DHAV talk frame format (SOLVED)

Decompiled from `LCDHAVAudioPacker::Pack` — the class's `vtable[2]`. Found via the
Itanium RTTI: typeinfo-name string `N2LC7PlaySDK17LCDHAVAudioPackerE` → typeinfo
struct → vtable (read through `R_AARCH64_RELATIVE` relocations) → `vtable[2]`.

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
| 0x17 | 1 | checksum | `(Σ bytes[0x00..0x14] + 4) & 0xff` |
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
