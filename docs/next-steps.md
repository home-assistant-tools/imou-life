# Next Steps

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

Talkback requires:

- talk session creation
- audio input capture or external audio injection
- codec/sample format confirmation
- `NativeAudioTalker.pushMediaData(...)`
- stop/cleanup lifecycle
- echo cancellation strategy

