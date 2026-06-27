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

See `two-way-audio.md` for the full reversed flow. Key correction from the
research: **audio is captured inside the native `.so` (`startSampleAudio`), not
pushed from Java** — so `pushMediaData` is the *video* path. For our own audio we
must replicate the device-side `talk.xav` protocol over the loopback P2P port,
not call `pushMediaData`.

Target shape (mirror `scripts/imou_xav_bridge.py`, talk direction):

```text
HA/go2rtc mic (G.711a 8kHz 16-bit)
  -> talk.xav over 127.0.0.1:<port>  (Digest auth, DHAV pack, proto=Private3)
  -> camera speaker
```

Ordered tasks:

1. **Capture a real talk session** (highest priority — blocks everything else):
   tap the talk button in Imou Life while running tcpdump on the loopback proxy
   port. Record: HTTP method, headers, duplex GET vs POST/upload, and the
   outbound (app -> device) DHAV audio frame layout/sequencing.
2. Confirm talk URL params from device abilities (`talkType`, `audioType`,
   `subtype`, `TSV1/TSV2/RTSV1/TCM`).
3. Confirm Digest auth for `talk.xav` matches the receive bridge (same realm/
   nonce flow, RTSP password).
4. Prefer an `encrypt=0` device first; otherwise reproduce PSK = MD5(devicePwd)
   stream encryption for `encryptMode in {2,3,4}`.
5. Build a one-way inject prototype (host audio -> camera speaker), then add
   echo-cancellation handling.

Codec confirmed by the talk path: **G.711a (encodeType 14), 8000 Hz, 16-bit,
DHAV packaging (packType 0)**.
