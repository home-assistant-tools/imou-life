#!/usr/bin/env bash
# Manual APK build (no gradle): aapt2 link + javac + d8 + bundle app SDK dex + .so + sign.
# Reuses libCommonSDK.so and the app's SDK classes (bundled as dex) — see README.md.
set -euo pipefail

ASDK="${ASDK:-/opt/homebrew/share/android-commandlinetools}"
BT="$ASDK/build-tools/34.0.0"
ANDROID_JAR="$ASDK/platforms/android-34/android.jar"
D8="$ASDK/cmdline-tools/latest/bin/d8"
HERE="$(cd "$(dirname "$0")" && pwd)"

# Inputs you must stage (large/secret — kept OUT of git):
#   $STAGE/dex/        classes*.dex extracted from the Imou base.apk (SDK classes)
#   $STAGE/jniLibs/arm64-v8a/   libCommonSDK.so + deps + libgadget.so + libgadget.config.so
#   $STAGE/assets/inject.js     Frida-gadget script (encoder hook + TTS), assets/tts.pcm
STAGE="${STAGE:-/private/tmp/claude-501/-Users-baduongvan-dev-smarthome-imou/daca6c13-3a76-4805-8635-fa5f5b868820/scratchpad/ttsapp}"
OUT="$HERE/out"; rm -rf "$OUT"; mkdir -p "$OUT"

echo "[1] compile MainActivity.java"
mkdir -p "$OUT/classes"
javac -source 17 -target 17 -bootclasspath "$ANDROID_JAR" -classpath "$ANDROID_JAR" \
    -d "$OUT/classes" "$HERE"/src/com/imoutts/*.java

echo "[2] d8 my classes -> driver dex (highest classesN+1.dex slot)"
"$D8" --min-api 24 --output "$OUT" "$OUT/classes"/com/imoutts/*.class
# d8 emits classes.dex; rename to a high slot so it doesn't clash with app dex
N=$(( $(ls "$STAGE"/dex/classes*.dex 2>/dev/null | wc -l) + 1 ))
mv "$OUT/classes.dex" "$OUT/classes${N}.dex"

echo "[3] aapt2 link (manifest -> base apk, no dex yet)"
"$BT/aapt2" link -I "$ANDROID_JAR" \
    --manifest "$HERE/AndroidManifest.xml" \
    -o "$OUT/base.apk" --auto-add-overlay

echo "[4] add dex (app SDK + driver) + native libs + assets into the apk"
cd "$OUT"
cp "$STAGE"/dex/classes*.dex .
mkdir -p lib && cp -r "$STAGE/jniLibs/"* lib/
mkdir -p assets && cp -r "$STAGE/assets/"* assets/ 2>/dev/null || true
zip -q -r base.apk classes*.dex lib assets

echo "[5] zipalign + sign (debug key)"
"$BT/zipalign" -f 4 base.apk aligned.apk
KS="$HERE/debug.jks"
[ -f "$KS" ] || keytool -genkeypair -keystore "$KS" -storepass android -keypass android \
    -alias d -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=imoutts" >/dev/null 2>&1
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:android --key-pass pass:android \
    --out "$HERE/imou-tts.apk" aligned.apk
echo "[ok] -> $HERE/imou-tts.apk"
