# Next Steps

## Phase 2 Bridge Prototype

1. Keep `artifacts/research/dh-p2p` as the ignored research checkout.
2. Run one local tunnel:

   ```bash
   python3 scripts/imou_p2p_rtsp_bridge.py --relay --bind 127.0.0.1:1554 <camera-serial>
   ```

3. Point go2rtc/ffmpeg at:

   ```text
   rtsp://<camera-user>:<camera-password>@127.0.0.1:1554/cam/realmonitor?channel=1&subtype=0
   ```

4. Convert the wrapper into an add-on supervisor after reconnect behavior is
   stable. Initial scaffold lives in `addon/imou-p2p-bridge/`.

## Immediate Runtime Capture

1. Start Imou Life and open a live camera view.
2. Capture Android logcat filtered for:
   - `P2P`
   - `LCSDK`
   - `CloudClient`
   - `LoginManager`
   - `PlayManager`
   - `GetP2PUrlTask`
   - `LOG_PLAY_STEP`
3. Record only redacted observations:
   - selected link type
   - local P2P port
   - generated local RTSP/HTTP URL shape
   - stream mode (`P2P`, `RELAY`, `MTS`, `MTS_QUIC`)
   - server type used (`p2p`, `p2p-v2`, `pss`, `mts`)

Do not commit account tokens, device serials, passwords, `p2pAk`, `p2pSk`,
access tokens, or raw packet captures.

## Verify Local URL Consumption

While Imou Life has an active live view, try reading the generated local URL
from the same device context or a test harness:

```text
ffprobe rtsp://127.0.0.1:<port>/cam/realmonitor?channel=1&subtype=0&proto=Private3
```

If app sandboxing prevents this on Android, reproduce with a small local wrapper
around the native SDK or an instrumented test process.

## Reverse the Data Model

Map request/response fields needed for bridge setup:

- device serial / DID
- PID for NVR or subdevice flows
- username/password or auth name/password
- `p2pAk`
- `p2pSk` / `p2pToken`
- `devP2PInfo`
- `streamEntryAddr`
- `salt`
- `quic`
- `psk`
- media encryption mode

## Bridge Prototype

The first useful prototype should be read-only:

```text
imou-life-bridge
  -> login/config
  -> open P2P tunnel
  -> expose local RTSP
  -> print go2rtc URL
```

Only after live video works reliably should talkback be attempted.

## Talkback Prototype

**✅ WORKING (encoder-injection):** TTS now plays on a real camera. Hook the patched
app's AAC encoder input (`LCAACAudioEncoder::Encode`, libCommonSDK.so vtable[3] @
patched `0x995240`) and replace the mic PCM (mono s16le 16 kHz) with our clip — the
app does AAC + handshake + DHEncrypt3 + P2P itself. Tool: `scripts/imou_tts_inject.py`
(`--text "..."`), then toggle the app talk button ON. Details:
[`talk-protocol-reverse-engineering.md`](talk-protocol-reverse-engineering.md).

The notes below remain for the *app-free* pure-protocol path (a standalone
DHHTTP-media client over dh-p2p), which is still future work.

**Status (binary-level RE complete):** see
[`talk-protocol-reverse-engineering.md`](talk-protocol-reverse-engineering.md) for the
authoritative writeup (frame format, full send pipeline with offsets, transport,
state machine). `two-way-audio.md` is the earlier app-decompile pass.

Settled facts (superseding earlier guesses in this file):

- **Frame format is SOLVED** (`LCDHAVAudioPacker::Pack`, verified against live frames):
  28-byte DHAV header + raw G.711 payload + 8-byte `dhav` trailer. Sample-rate code at
  header `[0x1B]`; the **app talks at 16 kHz** G.711 (`code 4`), not 8 kHz.
- **Talk is NOT a standalone connection.** It is `http_client_put_frame`'d as
  **trackID=5** into the *same* P2P live-view stream session (`DHHTTPTalker`,
  share-link). Wire framing `$` + `ch 0x0a` (= 2×trackID5) + `len(4 BE)` + DHAV.
- **Local direct talk is blocked:** `talk.xav` (8086) is half-duplex; `/videotalk`
  (8086) `500`s because it needs the P2P session context (fails even over a naive
  dh-p2p→8086 pipe). RTSP 554 has no `sendonly` track on Imou firmware.

Remaining work (implementation, not discovery):

1. Frida-hook `CTransformatDHInterleave` during a real talk to dump the exact
   `HTTPDH_START_TALK` handshake + interleaved talk bytes (the final UDP write is
   inline-`svc`, so hook here, above PTCP).
2. Build a DHHTTP-media client over the dh-p2p tunnel: open the live session, replay
   the talk-enable handshake, then inject 16 kHz G.711 DHAV as `$`+ch0x0a+len4BE.
3. Test on a P2P-reachable indoor account camera with a working speaker (e.g.
   "Kiara-Phòng khách"), someone nearby to listen.
