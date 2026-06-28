package com.imoutts;

import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.widget.FrameLayout;

import java.lang.reflect.Field;
import java.lang.reflect.Method;

/**
 * Standalone Imou TTS — reuses libCommonSDK.so + the bundled SDK dex (no Imou UI).
 *
 * Pipeline (all via reflection so we don't compile against the obfuscated SDK):
 *   1. System.loadLibrary the native libs + the embedded Frida gadget (gadget auto-runs
 *      assets/inject.js which hooks LCAACAudioEncoder::Encode and replaces the mic PCM
 *      with our TTS — the proven injection method, now in-process, no external Frida).
 *   2. LCSDK_Login.getInstance().init(p2pHost, port, ...)  — SDK + P2P init.
 *   3. LCSDK_Login.getInstance().addDevices(DEVICE_JSON)   — import the device session
 *      (Sn/User/Pwd/DevP2PAk/DevP2PSk/DevP2PInfo — captured; the .so computes DevAuth).
 *   4. LCSDK_PlayWindow.playRealTimeStream(serial, channel, ...) on a hidden Surface —
 *      the .so opens the P2P stream and yields a handleKey.
 *   5. LCSDK_Talk.INSTANCE.startTalkByHandleKey(handleKey, ...) — start talk; the gadget
 *      script feeds our AAC/PCM, so the camera speaks our clip.
 *
 * NOTE: exact init() args and the play->handleKey wiring still need on-device iteration;
 * this is the working skeleton, not a finished binary. See README.md.
 */
public class MainActivity extends Activity {
    static final String TAG = "ImouTTS";

    // ---- imported session (fill from scratchpad/device_session.json; secrets NOT in repo) ----
    static final String SERIAL = System.getenv("IMOU_SERIAL") != null ? System.getenv("IMOU_SERIAL") : "<SERIAL>";
    static final String DEVICE_JSON =
        "[{\"Sn\":\"<SERIAL>\",\"User\":\"admin\",\"Pwd\":\"<DEVICE_PW>\",\"Port\":8086,\"Type\":1," +
        "\"DevP2PAk\":\"LeChange\\\\v2\\\\Base\\\\phone\\\\easy4ipbaseapp\\\\<ACCT>\\\\<P2PAK>\"," +
        "\"DevP2PSk\":\"<P2PSK_B64>\"," +
        "\"DevP2PInfo\":\"{\\\"devSn\\\":\\\"<SERIAL>\\\",\\\"p2pSalt\\\":\\\"<SALT>\\\",\\\"p2pVer\\\":\\\"6.0.10005\\\"}\\n\"," +
        "\"extP2PInfo\":[]}]";

    static final String P2P_HOST = "www.easy4ipcloud.com";
    static final int    P2P_PORT = 8800;

    SurfaceView surface;

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        // hidden 1x1 surface so playRealTimeStream has somewhere to render
        FrameLayout root = new FrameLayout(this);
        surface = new SurfaceView(this);
        root.addView(surface, new FrameLayout.LayoutParams(2, 2));
        setContentView(root);

        loadNativeLibs();

        surface.getHolder().addCallback(new SurfaceHolder.Callback() {
            public void surfaceCreated(SurfaceHolder h) {
                new Thread(() -> {
                    Looper.prepare();          // SDK (LCSDK_Talk) creates Handlers -> needs a Looper
                    runTalk(h);
                    Looper.loop();             // process the SDK's Handler messages
                }).start();
            }
            public void surfaceChanged(SurfaceHolder h, int f, int w, int hh) {}
            public void surfaceDestroyed(SurfaceHolder h) {}
        });
    }

    void loadNativeLibs() {
        // order matters: deps before libCommonSDK
        for (String lib : new String[]{
                "c++_shared", "CommonLog", "netsdk", "jninetsdk", "CloudClient",
                "CommonSDK", "gadget" /* Frida gadget: auto-runs assets/inject.js */ }) {
            try { System.loadLibrary(lib); Log.i(TAG, "loaded lib" + lib); }
            catch (Throwable t) { Log.w(TAG, "load lib" + lib + " failed: " + t); }
        }
    }

    void runTalk(SurfaceHolder holder) {
        try {
            String deviceJson = readAsset("device.json");     // real session (NOT in git)
            String serial = extract(deviceJson, "\"Sn\":\"", "\"");
            Log.i(TAG, "serial=" + serial);

            // 2) SDK + P2P init
            Object login = call(cls("com.lc.lcsdk.LCSDK_Login"), null, "getInstance");
            invoke(login, "init",
                new Class[]{String.class, int.class, String.class, int.class, String.class, String.class, boolean.class},
                P2P_HOST, P2P_PORT, P2P_HOST, P2P_PORT, "/data/data/com.imoutts/files",
                "imoutts-" + System.currentTimeMillis(), false);
            Log.i(TAG, "SDK init done");
            Log.i(TAG, "initLCNetSDK=" + safe(() -> invoke(login, "initLCNetSDK", new Class[]{})));
            try {
                invoke(login, "initP2PSeverAfterSDK",
                    new Class[]{String.class, int.class, String.class, int.class, String.class, String.class, boolean.class},
                    P2P_HOST, P2P_PORT, P2P_HOST, P2P_PORT, "/data/data/com.imoutts/files", "imoutts", false);
                Log.i(TAG, "initP2PSeverAfterSDK ok");
            } catch (Throwable t) { Log.w(TAG, "initP2PSeverAfterSDK: " + t); }

            // 3) import device session
            invoke(login, "addDevices", new Class[]{String.class}, deviceJson);
            Log.i(TAG, "addDevices done; devState=" + tryDevState(login, serial));

            // 4) THE CRUX: getNetSDKHandler does the P2P device-login -> handle (talk needs this != 0)
            //    Same json shape the SDK's startDHTalk uses (relies on the addDevices'd session).
            String h = "{\"Sn\":\"" + serial + "\",\"Type\":0, \"Port\":0,\"User\":\"\",\"Pwd\":\"\",\"LoginType\":0}";
            Object handle = invoke(login, "getNetSDKHandler",
                new Class[]{String.class, int.class, boolean.class}, h, 15000, false);
            // BLOCKER: returns 0 (fast, not a timeout) despite init+initLCNetSDK+initP2PSever+addDevices
            // and devState=2 (online). Missing the exact app startup init — most likely
            // SetNetSDKLogin(callback) and/or precise init() args. Capture via gadget on_load=wait.
            Log.i(TAG, "getNetSDKHandler -> " + handle + "  (0 = P2P device-login FAILED)");

            // 5) start talk via the high-level path (uses getNetSDKHandler internally)
            Object talk = field(cls("com.lc.lcsdk.LCSDK_Talk"), "INSTANCE");
            Object r = invoke(talk, "startDHTalk",
                new Class[]{String.class, int.class, boolean.class, boolean.class},
                serial, 0, false, true);
            Log.i(TAG, "startDHTalk -> " + r);
            for (int i = 0; i < 6; i++) {
                Thread.sleep(1500);
                Log.i(TAG, "curStreamMode=" + safe(() -> invoke(talk, "getCurStreamMode", new Class[]{})) +
                           " devState=" + tryDevState(login, serial));
            }
        } catch (Throwable t) {
            Log.e(TAG, "runTalk error", t);
        }
    }

    String tryDevState(Object login, String serial) { try { return "" + invoke(login, "getDevState", new Class[]{String.class}, serial); } catch (Throwable t) { return "?"; } }
    interface Sup { Object get() throws Throwable; }
    static String safe(Sup s) { try { return "" + s.get(); } catch (Throwable t) { return "?"; } }
    static String extract(String s, String a, String b) { int i = s.indexOf(a); if (i < 0) return ""; i += a.length(); int j = s.indexOf(b, i); return j < 0 ? "" : s.substring(i, j); }
    String readAsset(String n) throws Exception {
        java.io.InputStream in = getAssets().open(n);
        byte[] buf = new byte[in.available()]; in.read(buf); in.close();
        return new String(buf, "UTF-8");
    }

    // ---- tiny reflection helpers ----
    static Class<?> cls(String n) throws Exception { return Class.forName(n); }
    static Object call(Class<?> c, Object o, String m) throws Exception { return c.getMethod(m).invoke(o); }
    static Object field(Class<?> c, String f) throws Exception { Field x = c.getField(f); return x.get(null); }
    static Object invoke(Object o, String m, Class<?>[] sig, Object... a) throws Exception {
        Method x = o.getClass().getMethod(m, sig); x.setAccessible(true); return x.invoke(o, a);
    }
}
