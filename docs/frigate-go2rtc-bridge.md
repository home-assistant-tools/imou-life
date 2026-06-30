# Frigate and go2rtc Bridge Notes

## Goal

Expose an Imou Life camera stream as a local URL that Frigate or go2rtc can read,
without opening inbound ports on the camera network.

## Why This Looks Feasible

Imou Life already does this internally:

```text
native P2P setup
  -> obtain local port
  -> construct local RTSP or HTTP/XAV URL
  -> app player reads 127.0.0.1:<port>
```

If a standalone bridge can reproduce the native setup, Frigate/go2rtc can read
the local endpoint like any other RTSP stream.

## Candidate Stream Shape

The target local stream exposed by the bridge would look like:

```text
rtsp://127.0.0.1:<port>/cam/realmonitor?channel=1&subtype=0&proto=Private3
```

or:

```text
http://127.0.0.1:<port>/live/realmonitor.xav?channel=1&subtype=0&audioType=1&proto=Private3
```

The exact transport and URL parameters may vary by camera, stream type, account,
and whether the SDK chooses local, P2P, MTS, QUIC, or relay.

## Example Frigate Shape

Once a bridge exposes a stable local RTSP URL:

```yaml
go2rtc:
  streams:
    imou_front: rtsp://127.0.0.1:1554/cam/realmonitor?channel=1&subtype=0&proto=Private3

cameras:
  imou_front:
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:1554/cam/realmonitor?channel=1&subtype=0&proto=Private3
          roles:
            - detect
            - record
```

## Bridge Approaches

### 1. Use Camera Native ONVIF/RTSP

If the camera exposes LAN RTSP or ONVIF directly, use that first. It is simpler,
more reliable, and avoids cloud/P2P dependencies.

Confirmed LAN RTSP shape:

```text
rtsp://<user>:<password>@<camera-ip>:554/cam/realmonitor?channel=1&subtype=0
```

Use `subtype=1` for the lower-resolution sub stream. See
`docs/rtsp-access.md` for the current probe results and helper script.

### 2. Wrap Imou Native SDK Behavior

Run a local process that uses or recreates the native flow:

```text
login/config
init P2P server
add device with p2pAk/p2pSk
get local P2P port
keep tunnel alive
publish local RTSP endpoint
```

This is likely the fastest route to a working bridge.

### 3. Reuse the Public Dahua P2P/PTCP Work

The public `dh-p2p` project has enough Dahua P2P/PTCP support to open a relay
tunnel and forward camera RTSP for the tested remote camera:

```text
https://github.com/khoanguyen-3fc/dh-p2p
```

The local bridge shape becomes:

```text
dh-p2p tunnel
  -> 127.0.0.1:1554
  -> rtsp://127.0.0.1:1554/cam/realmonitor?channel=1&subtype=0
  -> go2rtc / Frigate
```

This is now the preferred phase-2 path for read-only live streams. See
`docs/phase2-local-p2p-bridge.md`.

For Frigate two-way audio, see
[`frigate-talkback.md`](frigate-talkback.md). That guide covers go2rtc
`exec:` backchannel configuration, helper placement, LAN direct talk, codec
profiles, and troubleshooting.

### 4. Reimplement the Protocol

Fully reimplementing Imou Life P2P would require:

- account/device auth
- server discovery
- STUN/relay negotiation
- PTCP or p2p-channel behavior
- stream request/auth
- encryption/key derivation
- keepalive/reconnect logic

This is the most portable route, but also the most work.

## Important Caveat

Dahua DHP2P public research is more than background for read-only RTSP: the
tested remote camera works through the public PTCP tunnel. Imou Life 10.x still
includes Imou-specific app/cloud data such as `p2p-v2`, `p2pAk`, `p2pSk` or
token, `Private3`, `LoginCFS`, MTS, QUIC, and `visualtalk.xav`, so those paths
may still matter for other models, encrypted streams, talkback, playback, or
future firmware.
