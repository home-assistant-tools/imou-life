# Frigate LAN Talkback Integration

This guide explains how to add Imou two-way audio to Frigate for cameras that
are reachable directly on the LAN. It uses go2rtc's backchannel support and the
bridge helper `frigate_imou_talk_exec.py`.

## What Works

Frigate's mic button sends browser microphone audio to go2rtc. go2rtc can pipe
that audio into an `exec:` producer. The helper reads that stdin audio, converts
it to the camera's expected talk codec, opens an Imou `visualtalk.xav` session,
and sends DHAV audio frames to the camera speaker.

```text
Frigate WebRTC mic
  -> go2rtc backchannel audio: PCMA/8000
  -> frigate_imou_talk_exec.py stdin
  -> ffmpeg codec/gain conversion
  -> Imou visualtalk.xav
  -> camera speaker
```

The helper does not use the Android app, Imou cloud, P2P, or Imou native `.so`.
It connects directly to `camera-ip:8086`, so this guide applies only when the
Frigate/go2rtc host can reach that LAN address.

## Requirements

- Frigate live view must use go2rtc/WebRTC for the camera.
- Frigate must be able to execute `python3` and `ffmpeg`.
- go2rtc must allow `exec:` sources. In Frigate's Docker/TrueNAS environment,
  set `GO2RTC_ALLOW_ARBITRARY_EXEC=true`.
- `frigate_imou_talk_exec.py` and the bridge Python modules must be visible
  inside the Frigate container or environment running go2rtc.
- The camera device username/password must be known.
- The Frigate/go2rtc host must be able to connect to `camera-ip:8086`.
- The camera's talk codec profile must be known. Tested cameras have used
  `aac-adts` at `16000` Hz, but other models may need G.711 A-law.

## Where To Put The Helper

### Docker / Frigate Container

Copy these files into a directory mounted into Frigate, for example
`/config/imou-talk/`:

```text
frigate_imou_talk_exec.py
imou_dhav.py
imou_dhp2p.py
imou_visualtalk.py
imou_wsse.py
```

If you run Imou Bridge from this repository, the files are under:

```text
deploy/dockge/imou-bridge/
```

Make the helper executable:

```bash
chmod +x /config/imou-talk/frigate_imou_talk_exec.py
```

### Home Assistant Add-on Install

The add-on contains the helper internally, but Frigate cannot execute files from
another add-on's private filesystem. Copy the helper and its modules into
Home Assistant's `/config/imou-talk/` directory, then reference that path from
Frigate.

## Frigate Config Example

Use this when the Frigate host can reach the camera's LAN IP and the camera
accepts `visualtalk.xav` on port `8086`.

```yaml
go2rtc:
  streams:
    imou_yard:
      - rtsp://admin:<camera-password>@<camera-ip>:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif#backchannel=0
      - exec:python3 /config/imou-talk/frigate_imou_talk_exec.py --direct --host <camera-ip> --port 8086 --serial <camera-serial> --username admin --password <camera-password> --channel 1 --subtype 0 --type 0 --startup-delay 0 --input-codec alaw --input-sample-rate 8000 --output-codec aac-adts --sample-rate 16000 --volume-gain 5.0#backchannel=1#audio=alaw/8000
      - ffmpeg:imou_yard#audio=opus

cameras:
  imou_yard:
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/imou_yard
          input_args: preset-rtsp-restream
          roles:
            - detect
            - record
    live:
      stream_name: imou_yard
```

Why there are three go2rtc entries:

| Entry | Purpose |
| --- | --- |
| RTSP source with `#backchannel=0` | Normal video/audio source. Keeping this first avoids live-view startup issues. |
| `exec:` source with `#backchannel=1#audio=alaw/8000` | Receives mic audio from go2rtc and pushes it to Imou talk. |
| `ffmpeg:...#audio=opus` | Gives WebRTC an Opus audio track for browser compatibility. |

## Codec Profiles

The codec is camera-specific. Do not globally replace old profiles for all
cameras.

### AAC/ADTS Output

This profile worked on tested cameras and is usually the first one to try:

```text
--input-codec alaw
--input-sample-rate 8000
--output-codec aac-adts
--sample-rate 16000
--volume-gain 5.0
```

### G.711 A-law Output

Use this for models that reject AAC or expect G.711:

```text
--input-codec alaw
--input-sample-rate 8000
--output-codec alaw
--sample-rate 16000
--volume-gain 2.0
```

For G.711 output, the helper sends timed DHAV frames directly instead of running
the AAC encoder.

## Volume And Latency

- `--volume-gain 5.0` was needed on the tested LAN camera to make Frigate mic
  audio loud enough.
- Lower gain if the camera speaker distorts.
- AAC mode uses ffmpeg low-delay flags, but browser mic -> WebRTC -> go2rtc ->
  ffmpeg -> visualtalk will still have some latency.

## Testing

Check that the helper can start without Frigate first:

```bash
python3 /config/imou-talk/frigate_imou_talk_exec.py \
  --direct \
  --host <camera-ip> \
  --port 8086 \
  --serial <camera-serial> \
  --username admin \
  --password <camera-password> \
  --output-codec aac-adts \
  --volume-gain 5.0 < /dev/zero
```

Stop it after a second or two. You mainly want to see that the helper reaches
`visualtalk.xav` and logs successful `Cseq` responses.

The helper writes logs to:

```text
/tmp/frigate_imou_talk_exec.log
```

In Frigate/go2rtc, also watch:

```bash
docker logs -f frigate
```

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Frigate mic button appears but video reloads/stalls | go2rtc stream/backchannel order or WebRTC source issue | Keep the RTSP/video source first, then `exec:...#backchannel=1`, then optional `ffmpeg:...#audio=opus`. |
| go2rtc reports `restricted source` for `exec` | Arbitrary exec producers are disabled | Set `GO2RTC_ALLOW_ARBITRARY_EXEC=true` in the Frigate container environment and recreate/restart Frigate. |
| No sound from camera | Wrong codec, too low volume, or talk session did not start | Try `aac-adts`/`16000`, raise `--volume-gain`, check helper log for `Cseq` responses. |
| Helper exits immediately | Missing Python modules in Frigate container | Copy `imou_dhav.py`, `imou_dhp2p.py`, `imou_visualtalk.py`, and `imou_wsse.py` next to the helper. |
| `ffmpeg` not found | Frigate environment lacks ffmpeg in PATH | Install/mount ffmpeg or update helper path if your image stores ffmpeg elsewhere. |
| Connection timeout to `8086` | Frigate host cannot reach the camera LAN talk service | Put Frigate on the same LAN/VPN, check firewall rules, and verify `nc -vz <camera-ip> 8086`. |
| Authentication error | Wrong camera device password | Update the camera password in Frigate config and the Imou Bridge UI. |
| Works once, fails when retried quickly | Camera has not fully closed previous talk session | Wait a few seconds before retrying. |

## Security Notes

- Frigate config will contain the camera device password unless you inject it
  from secrets or environment variables.
- The helper talks directly to the camera LAN service using camera credentials;
  it does not require the Imou account password.
- This LAN-direct path does not use Imou cloud/P2P. If the camera is remote,
  place Frigate on a VPN/site-to-site network that can reach `camera-ip:8086`.
