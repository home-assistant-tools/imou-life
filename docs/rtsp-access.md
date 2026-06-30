# RTSP Stream Access

## Confirmed LAN RTSP

Some Imou cameras expose Dahua-style RTSP directly on the LAN. Use this first
when Home Assistant, go2rtc, or Frigate can reach the camera network.

Main stream:

```text
rtsp://<user>:<password>@<camera-ip>:554/cam/realmonitor?channel=1&subtype=0
```

Sub stream:

```text
rtsp://<user>:<password>@<camera-ip>:554/cam/realmonitor?channel=1&subtype=1
```

Runtime probe on the local network found:

```text
<camera-ip> channel=1 subtype=0 video:h264:1920x1080,audio:aac
<camera-ip> channel=1 subtype=1 video:h264:640x360,audio:aac
<camera-ip-2> channel=1 subtype=0 video:hevc:2304x1296,audio:aac
<camera-ip-2> channel=1 subtype=1 video:h264:640x352,audio:aac
<camera-ip-3> channel=1 subtype=0 video:hevc:2304x1296,audio:aac
<camera-ip-3> channel=1 subtype=1 video:h264:640x352,audio:aac
```

Channels are one-based for RTSP. `channel=0` did not work in the local tests.

Current visual mapping:

```text
<camera-ip> example indoor camera, not the remote example camera
<camera-ip-2> high-resolution Imou feed, currently pointed at generic indoor scene
<camera-ip-3> high-resolution Imou feed, generic outdoor scene view
```

`remote camera` in cloud inventory is `IPC-PS70F-10M0` with two app channels:
`PT Lens` and `Fixed Lens`. It is not on the current LAN; it is a remote camera.
Do not map it to `<camera-ip>`, `<camera-ip-2>`, or `<camera-ip-3>`. Those are
local-network cameras discovered during LAN probing.

Probe helper:

```bash
IMOU_RTSP_USER=admin IMOU_RTSP_PASSWORD='<password>' \
  scripts/imou_probe_rtsp.py --subnet <camera-subnet>
```

## App P2P Local Media Endpoint

When Imou Life opens a cloud/P2P live view, the native SDK opens local loopback
ports on the phone. The observed ports returned Digest auth challenges and then
served the following endpoint after camera credentials were supplied:

```text
http://127.0.0.1:<port>/live/realmonitor.xav?channel=1&subtype=0&audioType=1&proto=Private3
```

The response shape was:

```text
HTTP/1.1 200 OK
Private-Type: application/sdp
Private-Length: 468
Content-Type: video/x-xav
```

The body starts with SDP (`v=0`, `s=Media Server`) and then private Dahua
`DHAV` frames. The SDP advertised H265 video and AAC/MPEG4-GENERIC audio in the
observed session. `ffprobe` did not parse this HTTP/XAV endpoint directly.

RTSP `DESCRIBE` against the same app-local loopback ports timed out, so the app
session being tested was HTTP/XAV rather than a plain RTSP server. For cameras
not reachable by native LAN RTSP, HACS/go2rtc will need either an Imou native
SDK wrapper or an HTTP/XAV-to-RTSP bridge.

The `DHAV` payload can be parsed by FFmpeg once the HTTP headers and SDP section
are stripped:

```bash
IMOU_RTSP_PASSWORD='<password>' scripts/imou_xav_bridge.py --serial EXAMPLESERIAL01 --auto \
  | ffmpeg -f dhav -i pipe:0 -c copy -f mpegts remote_camera.ts
```

For a real RTSP bridge, publish the same FFmpeg output into MediaMTX/go2rtc:

```bash
IMOU_RTSP_PASSWORD='<password>' scripts/imou_xav_bridge.py --serial EXAMPLESERIAL01 --auto \
  | ffmpeg -f dhav -i pipe:0 -c copy -f rtsp rtsp://127.0.0.1:8554/remote_camera
```

This requires an RTSP server listening on `127.0.0.1:8554`. The phone/app must
keep the camera session alive because it owns the cloud/P2P tunnel.

Live test against a remote camera through the Android app/P2P tunnel:

```text
phone-local XAV port: <phone-local-port>
adb-forwarded host port: <host-forward-port>
path: /live/realmonitor.xav?channel=1&subtype=0&audioType=1&proto=Private3
ffmpeg after XAV->DHAV strip: video:hevc:2304x1296, audio:aac
```

The local port is runtime-assigned, so use `scripts/imou_xav_bridge.py --auto`
instead of hard-coding a port.
