# Imou Life Reverse Engineering Notes

This repository tracks research notes for understanding how the Android Imou Life
application connects to cameras for live video, playback, P2P traversal, and
two-way audio.

The local APK and decompiled artifacts are intentionally kept under
`artifacts/` and ignored by Git. Do not commit APKs, native libraries, or
decompiled vendor code.

## Current Snapshot

- App: Imou Life
- Android package: `com.mm.android.smartlifeiot`
- Version observed: `10.0.6`
- Version code: `500527`
- Source device: Samsung Galaxy S21, package pulled with `adb`
- Local artifacts path: `artifacts/imou_apk/`

## Key Findings

- Imou Life creates a local stream endpoint after native P2P setup.
- The app can use direct P2P, local LAN, or relay paths.
- Live video is exposed to the app player as local RTSP or HTTP/XAV URLs.
- P2P traversal and media setup live mostly in native libraries, especially
  `libCommonSDK.so` and `libCloudClient.so`.
- Two-way audio uses a separate native `NativeAudioTalker` component; it is not
  just an audio track in the live RTSP stream.

## Documents

- [Research Summary](docs/research-summary.md)
- [Cloud API Surface](docs/cloud-api-surface.md)
- [P2P and Media Flow](docs/p2p-media-flow.md)
- [Two-Way Audio](docs/two-way-audio.md)
- [Frigate and go2rtc Bridge Notes](docs/frigate-go2rtc-bridge.md)
- [Next Steps](docs/next-steps.md)

## Safety Notes

This project is for interoperability research with devices you own or are
authorized to test. Avoid publishing secrets, account tokens, device serials,
APK binaries, vendor code, or captured private traffic.

