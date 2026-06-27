# Phase 2 Local P2P Bridge

## Status

Phase 2 is viable for read-only live RTSP.

Public Dahua P2P research already implements enough of the protocol to open a
TCP tunnel to a remote camera/NVR and forward RTSP through it:

- https://github.com/khoanguyen-3fc/dh-p2p

Runtime test against the remote yard camera reached:

```text
Easy4IP/P2P discovery
  -> relay agent allocation
  -> PTCP session established
  -> local listener on 127.0.0.1:1554
  -> RTSP OPTIONS / DESCRIBE / SETUP / PLAY
  -> RTP payload over interleaved TCP
```

The SDP reported H265 video and AAC audio, matching what the Android app's local
XAV endpoint exposed earlier.

## Why This Matters

The earlier APK analysis showed that Imou Life eventually creates a local media
endpoint backed by the native P2P SDK. The public `dh-p2p` PoC proves we can
recreate the critical transport layer on Linux without running the Android app
or loading Android native libraries.

That changes the bridge target:

```text
Home Assistant add-on
  -> run one P2P tunnel process per camera/channel
  -> expose stable local RTSP URLs
  -> let go2rtc/Frigate/Home Assistant consume those URLs
  -> publish bridge state through MQTT
```

## Working Command Shape

Clone/build the research PoC under ignored artifacts:

```bash
git clone --depth 1 https://github.com/khoanguyen-3fc/dh-p2p artifacts/research/dh-p2p
```

Run the wrapper:

```bash
python3 scripts/imou_p2p_rtsp_bridge.py --relay --bind 127.0.0.1:1554 <camera-serial>
```

Start the first RTSP consumer soon after the bridge reports ready. In testing,
waiting around the relay allocation window could leave the first TCP bind
stalled; restarting the worker fixed it.

Then consume the forwarded camera RTSP service:

```bash
ffprobe -rtsp_transport tcp \
  "rtsp://<camera-user>:<camera-password>@127.0.0.1:1554/cam/realmonitor?channel=1&subtype=0"
```

For go2rtc:

```yaml
streams:
  imou_yard:
    - rtsp://<camera-user>:<camera-password>@127.0.0.1:1554/cam/realmonitor?channel=1&subtype=0
```

## Protocol Pieces Confirmed

The public implementation uses the older Dahua/Easy4IP flow:

```text
DHGET /probe/p2psrv
DHGET /online/p2psrv/<serial>
DHGET /online/relay
DHGET /probe/device/<serial>
DHPOST /device/<serial>/p2p-channel
DHGET /relay/agent
DHPOST /relay/start/<token>
DHPOST /device/<serial>/relay-channel
PTCP sync / heartbeat / bind / payload
```

The PTCP tunnel binds remote TCP port `554`, so normal RTSP digest auth happens
inside the tunnel. The bridge itself does not need the camera password.

## APK Cross-Check

This matches the decompiled APK at a conceptual level:

- `GetP2PUrlTask` requests a local P2P port.
- `Login.getP2PPort(...)` creates/keeps the P2P tunnel.
- URL builders point the player at `127.0.0.1:<port>`.
- Native strings mention `Src/PTCP/PhonyTcpReactor.cpp`,
  `p2p-channel`, `relay-channel`, `p2p,udprelay`, and relay/direct link modes.

Imou Life 10.x has additional `p2p-v2`, `DevP2PAk`, `DevP2PSk`, MTS, QUIC, and
Private3 paths, but the tested live RTSP path did not require reproducing those
fields for this camera.

## Add-On Design

Recommended add-on shape:

```text
imou-p2p-bridge add-on
  config.yaml
    cameras[]
      name
      serial
      bind
      remote_port: 554
      rtsp_path
      force_relay
  supervisor
    starts/restarts tunnel workers
    exposes health/status JSON
    publishes MQTT availability
  go2rtc
    consumes local RTSP URLs
```

Scaffold path:

```text
addon/imou-p2p-bridge/
```

For the first production pass, keep stream decoding outside the tunnel. The
tunnel should only forward bytes. go2rtc/Frigate can handle RTSP, RTP, H265, and
AAC.

## Production Gaps

- Replace or fork the PoC logging so runtime tokens and protocol chatter are not
  printed.
- Add reconnect with backoff when relay or PTCP sessions stall.
- Probe the local RTSP URL immediately after startup and restart the worker if
  the first remote bind does not return `CONN`.
- Support multiple camera workers in one supervisor.
- Add health probes that check RTSP DESCRIBE/PLAY, not just process liveness.
- Decide whether to vendor the Rust code, pin a commit, or reimplement the small
  subset needed for the add-on.
- Add MQTT discovery/availability after the RTSP tunnel is reliable.

## Security Notes

Do not commit camera passwords, Imou account tokens, relay tokens, packet
captures, APK binaries, or native SDK libraries. The wrapper redacts known
transient auth fields when verbose logging is enabled, but production logging
should be quieter by default.
