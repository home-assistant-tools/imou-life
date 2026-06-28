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
    // Cloud REST session. Put credentials in staged assets/account.json, not in Git:
    // {"cloudHost":"app-sg1-v3.easy4ipcloud.com:443","username":"uuid\\...","token":"...","sessionId":"..."}
    static final String DEFAULT_CLOUD_HOST = "app-sg1-v3.easy4ipcloud.com:443";

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
                "CommonSDK", "LCSign", "HsviewClient" /* cloud REST native (PCS signing) */,
                "gadget" /* Frida gadget: auto-runs assets/inject.js */ }) {
            try { System.loadLibrary(lib); Log.i(TAG, "loaded lib" + lib); }
            catch (Throwable t) { Log.w(TAG, "load lib" + lib + " failed: " + t); }
        }
    }

    void runTalk(SurfaceHolder holder) {
        try {
            String deviceJson = readAsset("device.json");     // real session (NOT in git)
            String serial = extract(deviceJson, "\"Sn\":\"", "\"");
            Log.i(TAG, "serial=" + serial);

            // 1.5) ARouter init — the SDK talk path resolves the talk address via a REST service
            //      looked up through ARouter; without init it throws "Invoke init(context) first".
            try {
                // ARouter launcher obfuscated to class P.a (UPPERCASE P!); P.a.d(Application)==ARouter.init.
                cls("P.a").getMethod("d", android.app.Application.class).invoke(null, getApplication());
                Log.i(TAG, "ARouter init done (P.a.d)");
            } catch (Throwable t) { Log.w(TAG, "ARouter init: " + t); }

            // 1.6) Cloud REST init (reuse login token) so asynGetTalkPlayAddress can fetch the talk ve.
            //      There are two REST clients:
            //      - LCSDK_RestApi/RestApi.mClient uses com.lc.common.rest.client.HsviewClient_inside.
            //      - LCApi/D6.f.h uses the app-level D6.a -> com.hsview.client.HsviewClient.
            //      Talk-address requests go through the second stack, so both must be initialized.
            try {
                String accountJson = readAssetMaybe("account.json");
                String acctToken = firstNonEmpty(System.getenv("IMOU_TOKEN"), jsonString(accountJson, "token"));
                String accountUser = firstNonEmpty(System.getenv("IMOU_USERNAME"), jsonString(accountJson, "username"));
                String sessionId = firstNonEmpty(System.getenv("IMOU_SESSION_ID"), jsonString(accountJson, "sessionId"));
                String restUser = normalizeRestUser(accountUser, acctToken);
                String host = firstNonEmpty(System.getenv("IMOU_CLOUD_HOST"), jsonString(accountJson, "cloudHost"), DEFAULT_CLOUD_HOST);
                if (acctToken.length() == 0) {
                    Log.w(TAG, "RestApi init skipped: missing IMOU_TOKEN or assets/account.json token");
                } else {
                    Object rest = call(cls("com.lc.lcsdk.LCSDK_RestApi"), null, "getInstance");
                    try { cls("com.lc.lcsdk.LCSDK_Utils").getMethod("setEncryptIV", String.class).invoke(null, ""); } catch (Throwable t) {}
                    // all client setters the app calls before init (a null one -> endsWith NPE)
                    callByPrefix(rest, "setClientVersion", "V10.0.6");
                    callByPrefix(rest, "setClientProject", "Base");
                    callByPrefix(rest, "setClientPushId", "");
                    try { invoke(rest, "setClientUaTTid", new Class[]{String.class}, "a9e6993cdd62410bb81a7d8f62211b1a"); } catch (Throwable t) {}
                    Method ua = null;
                    for (Method m : rest.getClass().getMethods())
                        if (m.getName().startsWith("setClientUaInfo")) { ua = m; break; }
                    if (ua != null) ua.invoke(rest, "phone", "V10.0.6", android.os.Build.VERSION.RELEASE,
                        "Android", android.os.Build.BRAND, "bafd56c41d8df3d1", "easy4ipbaseapp", "Base", "vi_VN", "V9.7.2");
                    int type = host.contains(":443") ? 1 : 0;
                    invoke(rest, "init", new Class[]{String.class, int.class, String.class, String.class},
                        host, type, restUser, acctToken);
                    Log.i(TAG, "RestApi init: host=" + host + " getHost=" +
                        safe(() -> { Object ri = call(cls("com.lc.lcsdk.rest.RestApi"), null, "getInstance"); return invoke(ri, "getHost", new Class[]{}); }));
                    initLegacyHttpClient(host, type, restUser, acctToken, sessionId);
                }
            } catch (Throwable t) { Log.w(TAG, "RestApi init: " + t); }

            // 2) SDK + P2P init
            Object login = call(cls("com.lc.lcsdk.LCSDK_Login"), null, "getInstance");
            invoke(login, "init",
                new Class[]{String.class, int.class, String.class, int.class, String.class, String.class, boolean.class},
                P2P_HOST, P2P_PORT, P2P_HOST, P2P_PORT, "/data/data/com.imoutts/files",
                "imoutts-" + System.currentTimeMillis(), false);
            Log.i(TAG, "SDK init done");
            Log.i(TAG, "initLCNetSDK=" + safe(() -> invoke(login, "initLCNetSDK", new Class[]{})));
            try {
                // Use the Ex form WITH the pss signaling server — required for P2P traversal.
                // App (LCSDKHelper) calls: initP2PSeverAfterSDKEx(p2pHost, iPv4, iPv6, p2pPort,
                //   pssHost, pssPort, "", "", isRelay). Without pss, getP2PPort never traverses.
                Object ok = invoke(login, "initP2PSeverAfterSDKEx",
                    new Class[]{String.class, String.class, String.class, int.class, String.class, int.class, String.class, String.class, boolean.class},
                    "www-v2.easy4ipcloud.com", "47.84.202.34", "", 8800,
                    "pss-sg.easy4ipcloud.com", 443, "", "", false);
                Log.i(TAG, "initP2PSeverAfterSDKEx -> " + ok);
            } catch (Throwable t) { Log.w(TAG, "initP2PSeverAfterSDKEx: " + t); }
            // Tell the SDK the network is up so the proxy starts connecting (app does this on
            // network events). Without it the proxy stays idle -> getP2PPort never traverses.
            try { invoke(login, "notifyConnectionChange", new Class[]{}); Log.i(TAG, "notifyConnectionChange ok"); }
            catch (Throwable t) { Log.w(TAG, "notifyConnectionChange: " + t); }

            // 3) import device session
            invoke(login, "addDevices", new Class[]{String.class}, deviceJson);
            Log.i(TAG, "addDevices done; devState=" + tryDevState(login, serial));
            // kick the proxy to (re)connect all registered devices
            try { Object rc = invoke(login, "reConnectAll", new Class[]{}); Log.i(TAG, "reConnectAll -> " + rc); }
            catch (Throwable t) { Log.w(TAG, "reConnectAll: " + t); }
            try { invoke(login, "notifyConnectionChange", new Class[]{}); } catch (Throwable t) {}
            Thread.sleep(3000); // give the proxy time to traverse to the device

            // 3.1) THE REAL TALK CONNECT: tryNetSDKConnect (LCSDKHelper.h()).
            //      Takes the DeviceLoginParams OBJECT (not array): Sn/User/Pwd/Port/DevP2PAk/
            //      DevP2PSk/AK=""/SK=""/extP2PInfo[{dstPort:554}]. Does P2P traversal + NetSDK
            //      login + returns handle, filling code[0]. This is what the app's talk path calls.
            String devObj = deviceJson.trim();
            if (devObj.startsWith("[")) devObj = devObj.substring(1, devObj.length() - 1).trim();
            // ensure AK/SK present (app sets them ""), and add talk extP2PInfo(dstPort 554)
            if (!devObj.contains("\"AK\"")) devObj = devObj.replaceFirst("\\{", "{\"AK\":\"\",\"SK\":\"\",");
            for (int i = 0; i < 6; i++) {
                int[] codeOut = new int[]{0};
                Object tnc = invoke(login, "tryNetSDKConnect",
                    new Class[]{String.class, int.class, boolean.class, int[].class}, devObj, 10000, false, codeOut);
                Log.i(TAG, "tryNetSDKConnect try" + i + " -> handle=" + tnc + " code[0]=" + codeOut[0] +
                           " devState=" + tryDevState(login, serial));
                if (tnc != null && ((Number) tnc).longValue() != 0) break;
                Thread.sleep(2000);
            }

            // 3.5) THE MISSING STEP: open the P2P tunnel first (STUN/ICE hole-punch).
            //      GetTalkP2PUrlTask.request() does exactly this before getNetSDKHandler:
            //      getP2PPort("{Sn,Pid,Type:1,isTalk:true,...}", state[], 50, count[]) -> localPort.
            String pid = extract(deviceJson, "\"devPid\":\"", "\"");
            // EXACT working format (captured from app): timeout 5000 (NOT 50 — 50ms can't traverse!)
            String p2pJson = "{\"Sn\":\"" + serial + "\",\"Type\":1,\"Port\":0,\"User\":\"\",\"Pwd\":\"\",\"Pid\":\"" + pid + "\"}";
            int localPort = 0;
            for (int i = 0; i < 8; i++) {
                int[] state = new int[]{0};
                int[] count = new int[]{0};
                Object lp = invoke(login, "getP2PPort",
                    new Class[]{String.class, int[].class, int.class, int[].class}, p2pJson, state, 5000, count);
                localPort = ((Number) lp).intValue();
                Log.i(TAG, "getP2PPort try" + i + " -> localPort=" + localPort +
                           " p2pState=" + state[0] + " count=" + count[0]);
                if (localPort > 0) break;
                Thread.sleep(1000);
            }
            Log.i(TAG, "P2P tunnel localPort=" + localPort);
            // If tunnel opened: set session (proxy at 127.0.0.1:localPort) like the app does
            if (localPort > 0) {
                try {
                    invoke(login, "setSessionInfo",
                        new Class[]{short.class, String.class, short.class, String.class, String.class},
                        (short) 3, "127.0.0.1", (short) localPort, "visualtalk_reqid", serial);
                    Log.i(TAG, "setSessionInfo(127.0.0.1:" + localPort + ") done");
                } catch (Throwable t) { Log.w(TAG, "setSessionInfo: " + t); }
            }

            // 4) START TALK — self-contained DHHTTP talk (does its OWN getP2PPort + visualtalk
            //    over the tunnel). Public entry LCSDK_Talk.startTalk(deviceSn, channelId, userName,
            //    psw, subType, isEncrypt, PSK, forceMts, deviceType, talkType, isOPT, sharedLinkMode,
            //    isAudioEncode, isTls, severParameter, wsseKey, isAssistInfo). isOPT=1 -> DHHTTP.
            //    isAudioEncode=true -> the AAC encoder runs -> the frida gadget injects our TTS.
            Class<?> talkCls = cls("com.lc.lcsdk.LCSDK_Talk");
            Object talkObj = field(talkCls, "INSTANCE");
            try { talkCls.getMethod("setRequestId", String.class).invoke(null, "visualtalk_reqid"); Log.i(TAG,"setRequestId ok"); }
            catch (Throwable t) { Log.w(TAG, "setRequestId: " + t); }
            String devUser = extract(deviceJson, "\"User\":\"", "\"");
            String devPwd  = extract(deviceJson, "\"Pwd\":\"", "\"");
            Method startTalk = null;
            for (Method m : talkCls.getMethods())
                if (m.getName().equals("startTalk") && m.getParameterTypes().length == 17) { startTalk = m; break; }
            if (startTalk == null) {
                Log.w(TAG, "startTalk overload with 17 params not found");
                return;
            }
            Class<?> proxyT = startTalk.getParameterTypes()[14]; // ProxySeverParameter
            // build a ProxySeverParameter with the cloud host (else asynGetTalkPlayAddress NPEs on null host)
            String accountJson = readAssetMaybe("account.json");
            String cloudHost = firstNonEmpty(System.getenv("IMOU_CLOUD_HOST"), jsonString(accountJson, "cloudHost"), DEFAULT_CLOUD_HOST);
            Object sever = proxyT.getConstructor().newInstance();
            try { proxyT.getMethod("setHost", String.class).invoke(sever, cloudHost.replace(":443","")); } catch (Throwable t) {}
            try { proxyT.getMethod("setProtocol", int.class).invoke(sever, 1); } catch (Throwable t) {}
            try { proxyT.getMethod("setPort", int.class).invoke(sever, 443); } catch (Throwable t) {}
            try { proxyT.getMethod("setKeepAlive", int.class).invoke(sever, 1); } catch (Throwable t) {}
            Object tr = null;
            for (int i = 0; i < 3; i++) {
                tr = startTalk.invoke(talkObj,
                    serial, 0, devUser, devPwd, 0, 3, "", false, "", "talk",
                    1 /*isOPT=1 DHHTTP*/, 0 /*sharedLinkMode=0 direct device*/, true, false,
                    sever, "", false);
                Log.i(TAG, "startTalk try" + i + " -> " + tr +
                           " curStreamMode=" + safe(() -> invoke(talkObj, "getCurStreamMode", new Class[]{})) +
                           " " + talkDebug(talkObj));
                if ("0".equals(String.valueOf(tr))) break;
                Thread.sleep(2500);
            }
            // HOLD the P2P tunnel open so it can be used (in-process or via adb-forward).
            Log.i(TAG, "TUNNEL READY port=" + localPort + " serial=" + serial);
            Handler holdHandler = new Handler(Looper.myLooper());
            final int holdPort = localPort;
            final Object holdTalk = talkObj;
            final Object holdLogin = login;
            final String holdSerial = serial;
            final int[] holdCount = new int[]{0};
            holdHandler.postDelayed(new Runnable() {
                public void run() {
                    if (holdCount[0]++ >= 90) return;
                    if (holdCount[0] % 5 == 0) {
                        Log.i(TAG, "tunnel hold port=" + holdPort +
                            " curStreamMode=" + safe(() -> invoke(holdTalk, "getCurStreamMode", new Class[]{})) +
                            " " + talkDebug(holdTalk) +
                            " devState=" + tryDevState(holdLogin, holdSerial));
                    }
                    holdHandler.postDelayed(this, 2000);
                }
            }, 2000);
        } catch (Throwable t) {
            Log.e(TAG, "runTalk error", t);
        }
    }

    String tryDevState(Object login, String serial) { try { return "" + invoke(login, "getDevState", new Class[]{String.class}, serial); } catch (Throwable t) { return "?"; } }
    static String talkDebug(Object talkObj) {
        try {
            Object audioTalker = invokeAny(talkObj, "getMAudioTalker", new Class[]{});
            Object handle = invokeAny(audioTalker, "getTalkHandle", new Class[]{});
            Object streamMode = invokeAny(audioTalker, "getStreamMode", new Class[]{});
            Object shareLink = invokeAny(audioTalker, "isShareLink", new Class[]{});
            return "talkHandle=" + handle + " nativeStreamMode=" + streamMode + " shareLink=" + shareLink;
        } catch (Throwable t) {
            return "talkHandle=?";
        }
    }
    interface Sup { Object get() throws Throwable; }
    static String safe(Sup s) { try { return "" + s.get(); } catch (Throwable t) { return "?"; } }
    static String extract(String s, String a, String b) { int i = s.indexOf(a); if (i < 0) return ""; i += a.length(); int j = s.indexOf(b, i); return j < 0 ? "" : s.substring(i, j); }
    String readAsset(String n) throws Exception {
        java.io.InputStream in = getAssets().open(n);
        try {
            java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int got;
            while ((got = in.read(buf)) != -1) out.write(buf, 0, got);
            return new String(out.toByteArray(), "UTF-8");
        } finally {
            in.close();
        }
    }
    String readAssetMaybe(String n) {
        try { return readAsset(n); } catch (Throwable t) { return ""; }
    }
    static String firstNonEmpty(String... values) {
        for (String v : values) if (v != null && v.length() > 0) return v;
        return "";
    }
    static String unescapeJson(String value) {
        return value == null ? "" : value.replace("\\\"", "\"").replace("\\\\", "\\");
    }
    static String jsonString(String json, String key) {
        if (json == null || json.length() == 0) return "";
        try {
            java.util.regex.Matcher m = java.util.regex.Pattern
                .compile("\"" + java.util.regex.Pattern.quote(key) + "\"\\s*:\\s*\"((?:\\\\.|[^\"])*)\"")
                .matcher(json);
            return m.find() ? unescapeJson(m.group(1)) : "";
        } catch (Throwable t) {
            return "";
        }
    }
    static String normalizeRestUser(String accountUser, String fallbackToken) {
        String user = accountUser == null ? "" : accountUser;
        if (user.startsWith("token/")) return user.substring("token/".length());
        if (user.startsWith("default/")) return "default\\" + user.substring("default/".length());
        if (user.length() > 0) return user;
        return "uuid\\" + fallbackToken;
    }
    void initLegacyHttpClient(String host, int type, String userName, String token, String sessionId) {
        try {
            Class<?> http = cls("D6.f");
            // Mirrors app global HTTP init enough for LCApi.POST(...)/D6.f.h().
            try { http.getMethod("j", String.class, String.class, String.class, String.class, String.class)
                .invoke(null, "Base", "bafd56c41d8df3d1", "vi_VN", "easy4ipbaseapp", "V9.7.2"); }
            catch (Throwable t) { Log.w(TAG, "D6.f.j client UA: " + t); }
            http.getMethod("m", String.class, int.class).invoke(null, host, type);
            http.getMethod("i", String.class, String.class).invoke(null, userName, token);
            if (sessionId.length() > 0) {
                http.getMethod("n", String.class).invoke(null, sessionId);
            }
            Log.i(TAG, "Legacy HTTP init done: host=" + host + " user=yes session=" + (sessionId.length() > 0));
        } catch (Throwable t) {
            Log.w(TAG, "Legacy HTTP init: " + t);
        }
    }

    // ---- tiny reflection helpers ----
    static Class<?> cls(String n) throws Exception { return Class.forName(n); }
    static Object call(Class<?> c, Object o, String m) throws Exception { return c.getMethod(m).invoke(o); }
    static Object field(Class<?> c, String f) throws Exception { Field x = c.getField(f); return x.get(null); }
    static Object invoke(Object o, String m, Class<?>[] sig, Object... a) throws Exception {
        Method x = o.getClass().getMethod(m, sig); x.setAccessible(true); return x.invoke(o, a);
    }
    static Object invokeAny(Object o, String m, Class<?>[] sig, Object... a) throws Exception {
        Class<?> c = o.getClass();
        while (c != null) {
            try {
                Method x = c.getDeclaredMethod(m, sig);
                x.setAccessible(true);
                return x.invoke(o, a);
            } catch (NoSuchMethodException e) {
                c = c.getSuperclass();
            }
        }
        throw new NoSuchMethodException(m);
    }
    static void callByPrefix(Object o, String prefix, String arg) {
        try { for (Method m : o.getClass().getMethods())
            if (m.getName().startsWith(prefix) && m.getParameterTypes().length==1
                && m.getParameterTypes()[0]==String.class) { m.invoke(o, arg); return; } }
        catch (Throwable t) {}
    }
}
