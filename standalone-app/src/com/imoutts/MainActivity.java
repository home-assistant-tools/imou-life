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
                new Thread(() -> runTalk(h)).start();
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
            // 2) SDK + P2P init
            Object login = call(cls("com.lc.lcsdk.LCSDK_Login"), null, "getInstance");
            // init(String p2pHost,int port,String,int,String,String terminalId,boolean)
            invoke(login, "init",
                new Class[]{String.class, int.class, String.class, int.class, String.class, String.class, boolean.class},
                P2P_HOST, P2P_PORT, "", 0, "", "imoutts-" + System.currentTimeMillis(), false);
            Log.i(TAG, "SDK init done");

            // 3) import device session
            invoke(login, "addDevices", new Class[]{String.class}, DEVICE_JSON);
            Log.i(TAG, "addDevices done");

            // 4) open real stream -> handleKey (TODO: confirm playRealTimeStream args / window class)
            //    LCSDK_PlayWindow needs a window bound to `surface`; wiring is on-device work.
            //    Placeholder: once a handleKey is obtained:
            // 5) start talk
            Object talk = field(cls("com.lc.lcsdk.LCSDK_Talk"), "INSTANCE");
            // String handleKey = <from play>;
            // invoke(talk, "startTalkByHandleKey", new Class[]{String.class,String.class,String.class,String.class,String.class,
            //         cls("com.lc.lcsdk.Data.LCTalkConfig"), String.class, cls("com.lc.common.talk.VideoSampleCfg")},
            //         handleKey, SERIAL, "0", "0", "", null, "", null);
            Log.i(TAG, "talk singleton=" + talk + " (wire play->handleKey to finish)");
        } catch (Throwable t) {
            Log.e(TAG, "runTalk error", t);
        }
    }

    // ---- tiny reflection helpers ----
    static Class<?> cls(String n) throws Exception { return Class.forName(n); }
    static Object call(Class<?> c, Object o, String m) throws Exception { return c.getMethod(m).invoke(o); }
    static Object field(Class<?> c, String f) throws Exception { Field x = c.getField(f); return x.get(null); }
    static Object invoke(Object o, String m, Class<?>[] sig, Object... a) throws Exception {
        Method x = o.getClass().getMethod(m, sig); x.setAccessible(true); return x.invoke(o, a);
    }
}
