# Standalone Imou TTS app (reuse libCommonSDK.so)

A minimal **pure-Java** Android app that makes an Imou camera speak arbitrary audio
**without the Imou app**, by reusing the camera SDK's native lib + classes. No DevAuth
crack / no protocol reimplementation — the `.so` does P2P/DevAuth/visualtalk/DHEncrypt3;
we just drive it via reflection and import the device session.

See [`../docs/talk-protocol-reverse-engineering.md`](../docs/talk-protocol-reverse-engineering.md)
for the full reverse-engineering and the recipe this implements.

## How it works
1. Bundles `libCommonSDK.so` (+ deps `libCommonLog/libnetsdk/libjninetsdk/libCloudClient/
   libc++_shared`) and the Imou app's `classes*.dex` (the obfuscated SDK classes).
2. `MainActivity` (reflection) runs: `LCSDK_Login.init(easy4ipcloud:8800,…)` →
   `addDevices(DEVICE_JSON)` → `LCSDK_PlayWindow.playRealTimeStream(serial,…)` on a hidden
   `SurfaceView` → `LCSDK_Talk.INSTANCE.startTalkByHandleKey(handleKey,…)`.
3. Audio injection reuses the **proven encoder hook** (`LCAACAudioEncoder::Encode`
   @ `0x995240`) via an **embedded Frida gadget** that auto-runs `assets/inject.js`
   (replaces the mic PCM with our TTS) — in-process, no external Frida.

## Build (no gradle — manual aapt2/d8/apksigner)
```bash
ASDK=/opt/homebrew/share/android-commandlinetools \
STAGE=<stage-dir> ./build.sh        # -> imou-tts.apk
adb install -r imou-tts.apk
```
Stage these into `$STAGE` (large/secret — NOT in git):
- `dex/classes*.dex` — `unzip base.apk 'classes*.dex'` from the Imou APK.
- `jniLibs/arm64-v8a/` — the `.so` deps above + `libgadget.so` + `libgadget.config.so`
  (config in `"script"` mode pointing at the asset).
- `assets/inject.js` — encoder-hook + embedded TTS (see `scripts/imou_tts_inject.py`),
  `assets/tts.pcm`.

Fill `DEVICE_JSON` / `SERIAL` in `MainActivity.java` from your captured
`device_session.json` (`addDevices` payload: `Sn/User/Pwd/Port/DevP2PAk/DevP2PSk/
DevP2PInfo`). The `DevP2PSk` is what lets the `.so` compute DevAuth — no cracking needed.

## Status / TODO (on-device iteration)
- ✅ build mechanics, lib/dex bundling, reflection init/addDevices.
- ⏳ wire `playRealTimeStream(...)` exact args + obtain `handleKey`, then
  `startTalkByHandleKey`. (The `LCSDK_PlayWindow` window/surface binding is the main
  remaining piece.)
- ⏳ confirm the embedded gadget hooks the in-process `libCommonSDK` and the TTS plays;
  alternatively use `pushMediaData(audioType,…)` if it accepts audio.
- ⚠️ the SDK may expect parts of the app's `Application.onCreate` init/Context; add as needed.

This is a working **skeleton**, not a finished binary — the RE recipe is complete; the
remaining work is on-device wiring of play→handleKey + verifying in-process injection.
