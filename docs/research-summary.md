# Research Summary

## Artifact Provenance

The observed APK was pulled from a test Android phone over `adb`.

- Package: `com.mm.android.smartlifeiot`
- App label: `Imou Life`
- Version name: `10.0.6`
- Version code: `500527`
- Install source: Google Play (`com.android.vending`)
- Pulled APK splits:
  - `base.apk`
  - `split_config.arm64_v8a.apk`
  - `split_config.xxhdpi.apk`

Local artifacts are stored under `artifacts/imou_apk/` and are ignored by Git.

## High-Value Native Libraries

The ARM64 split contains the libraries that matter for P2P and media:

- `libCloudClient.so`
- `libCommonSDK.so`
- `libIoTClient.so`
- `libHsviewClient.so`
- `libnetsdk.so`
- `libjninetsdk.so`
- `libAudioCapture.so`

The most important pair for P2P/video/talkback is:

- `libCloudClient.so`: cloud service login, P2P server discovery, P2P traversal,
  device metadata, and REST-like cloud calls.
- `libCommonSDK.so`: login manager, local P2P port creation, media player,
  RTSP/HTTP stream handling, and audio talker.

## Java Entry Points

Useful decompiled classes in the local artifact tree:

- `com/iotcom/commonsdk/cloudclient/CloudClient.java`
- `com/iotcom/commonsdk/login/LoginManager.java`
- `com/iotcom/commonsdk/play/PlayManager.java`
- `com/iotcom/commonsdk/talk/NativeAudioTalker.java`
- `com/lc/common/talk/AudioTalker.java`
- `com/lc/lcsdk/LCSDK_Talk.java`
- `com/lc/lcsdk/utils/TalkUtils.java`
- `Ac/a.java`
- `ra/C4763b.java`

## Main Conclusion

Imou Life does not simply fetch a public RTSP URL from the cloud. It initializes
native P2P state, obtains a local port, and then gives the app player a local URL
such as RTSP on `127.0.0.1`. That local endpoint is backed by a native P2P,
LAN, or relay transport to the camera.

