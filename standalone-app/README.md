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
STAGE=/path/to/stage ./build.sh     # -> imou-tts.apk
adb install -r imou-tts.apk
```
Stage these into `$STAGE` (large/secret — NOT in git):
- `dex/classes*.dex` — `unzip base.apk 'classes*.dex'` from the Imou APK.
- `jniLibs/arm64-v8a/` — the `.so` deps above + `libgadget.so` + `libgadget.config.so`
  (config in `"script"` mode pointing at the asset).
- `assets/inject.js` — encoder-hook + embedded TTS (see `scripts/imou_tts_inject.py`).
- `assets/device.json` — the captured `addDevices` payload for the target camera.
- `assets/account.json` — cloud REST session for talk-url fetch:
  `{"cloudHost":"app-sg1-v3.easy4ipcloud.com:443","token":"..."}`.

Do not put real serials, passwords, account tokens, `DevP2PAk`, or `DevP2PSk` in
`MainActivity.java`. The placeholders in source are documentation only; runtime
secrets come from staged assets.

## Status / TODO (VALIDATED on device — S21)
- ✅ **build mechanics** (aapt2/d8/apksigner, no gradle) — `imou-tts.apk` builds/installs.
- ✅ **native libs load**: `libc++_shared, libCommonLog, libnetsdk, libjninetsdk,
  libconfigsdk, libCloudClient, libCommonSDK` (the dep chain is complete).
- ✅ **`LCSDK_Login.init(easy4ipcloud:8800,…)`** runs (logcat "SDK init done").
- ✅ **`addDevices(json)`** runs (logcat "addDevices done").
- ✅ **`LCSDK_Talk.INSTANCE`** obtained (needs `Looper.prepare()` on the worker thread —
  the SDK creates Handlers). The reuse-.so approach is proven: the standalone APK drives
  the Imou SDK with no Imou app.
- ✅ **NetSDK + P2P init**: `initLCNetSDK()=true`, `initP2PSeverAfterSDK()` OK, `addDevices()`
  OK, **`devState=2` (device ONLINE)** — the imported session reaches the cloud.
- ⛔ **ROOT CAUSE FOUND:** the SDK **delegates NetSDK device-login back to the app** via a
  callback `LCSDK_NetsdkLogin{netSDKLoginSyn(int,String), netSDKLoginAsyn(int,String)}`
  registered with `SetNetSDKLogin(cb)`. With no callback, `getNetSDKHandler()` returns 0
  instantly. The callback's real impl (obfuscated app Java — no class implements it in
  jadx) does `getP2PPort(serial)` + NetSDK `INetSDK` CLIENT_Login to 127.0.0.1:p2pport.
  So pure .so-reuse is INSUFFICIENT for device-login — must register a Proxy callback that
  does the NetSDK login, or capture+reimplement it (gadget on_load:wait). Deeper layer.
- ⛔ (was) **`getNetSDKHandler()` returns 0** (fast, not a 15s timeout) → P2P device-login
  fails, so `startDHTalk` can't talk (`curStreamMode=-1`). Despite SDK init + NetSDK init +
  P2P init + addDevices + online. Missing the **exact app startup init sequence** — most
  likely `SetNetSDKLogin(callback)` and/or precise `init(...)` args. Capture it by setting
  the Imou app's gadget `on_load:wait` and hooking `init`/`getNetSDKHandler` at startup.
- ⏳ after the handle works: **play stream → handleKey**: `NativeShareLink` has no open method, so the
  stream must go through the play path. Build a `PlayerParam(...)` (≈25 fields: serial,
  user, pwd, encryptMode, PSK, sharedLinkMode, handleKey=`serial+"+"+channel`, the P2P
  URI from `Utils.buildOptionalP2PUri`, …) and call the native play (see
  `LCSDK_MediaPlayWindow:1970`), bound to the hidden Surface. Then
  `startTalkByHandleKey(handleKey, serial, "0", "0", "", null, "", null)`.
- ⏳ **audio injection**: bundle `libgadget.so`+config (script mode → `assets/inject.js`,
  the encoder hook @0x995240) so the in-process gadget feeds TTS; or test
  `pushMediaData(audioType,…)`.
- ⚠️ real `DEVICE_JSON` from `device_session.json` (DevP2PAk/DevP2PSk/DevP2PInfo).
- ⚠️ current working tree experiments with ARouter/RestApi init, `getP2PPort`,
  `tryNetSDKConnect`, and the 17-arg `LCSDK_Talk.startTalk(...)` path. It compiles,
  but still needs on-device validation before treating the standalone APK as solved.

**Milestone reached: the standalone APK runs the Imou SDK end-to-init.** Remaining =
the play-stream (PlayerParam) wiring + the in-process TTS injection — iterative on-device.
