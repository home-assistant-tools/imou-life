/*
 * Frida probe for Imou/Dahua DHHTTP talk internals in libCommonSDK.so.
 *
 * Run shape:
 *   adb forward tcp:27420 tcp:27420
 *   frida -H 127.0.0.1:27420 -n Gadget -l scripts/imou_hook_talk_protocol.js --runtime=v8
 *
 * Offsets are for the current standalone-app/out/lib/arm64-v8a/libCommonSDK.so
 * build in this repo. Recompute after APK/native repatching.
 */

"use strict";

const MODULE = "libCommonSDK.so";
const OFF = {
  httpClientPutFrameWrapper: 0x88dad8,
  httpClientPutFrameImpl: 0x891dd4,
  httpClientInitSdpForTalkA: 0x88d7fc,
  httpClientInitSdpForTalkB: 0x891144,
  shareHandleStartTalk: 0x54b844,
  msgHttpDhStartTalkCallsite: 0x54bca4,
};

function now() {
  return new Date().toISOString();
}

function safe(label, fn) {
  try {
    return fn();
  } catch (e) {
    return `${label}:ERR:${e}`;
  }
}

function shortHex(ptrValue, len) {
  if (ptrValue.isNull() || len <= 0) return "";
  const n = Math.min(len, 192);
  return hexdump(ptrValue, { offset: 0, length: n, header: false, ansi: false });
}

function dumpFrameDesc(desc) {
  if (desc.isNull()) {
    console.log(`[${now()}] put_frame desc=NULL`);
    return;
  }

  const trackId = safe("track", () => desc.readU32());
  const flags = safe("flags", () => desc.add(4).readU32());
  const len = safe("len", () => desc.add(8).readU32());
  const data = safe("data", () => desc.add(0x10).readPointer());
  const aux = safe("aux", () => desc.add(0x18).readPointer());
  console.log(
    `[${now()}] put_frame desc=${desc} track=${trackId} flags=0x${Number(flags).toString(16)} ` +
      `len=${len} data=${data} aux=${aux}`
  );

  if (typeof len === "number" && len > 0 && len < 1024 * 1024 && !data.isNull()) {
    const magic = safe("magic", () => data.readByteArray(Math.min(4, len)));
    if (magic !== "" && magic !== null) {
      console.log(shortHex(data, len));
    }
  }
}

function attachWhenReady() {
  const m = Process.findModuleByName(MODULE);
  if (!m) {
    setTimeout(attachWhenReady, 250);
    return;
  }

  console.log(`[${now()}] ${MODULE} base=${m.base} size=${m.size}`);

  Interceptor.attach(m.base.add(OFF.shareHandleStartTalk), {
    onEnter(args) {
      console.log(`[${now()}] ShareHandle::startTalk this=${args[0]} arg1=${args[1]}`);
    },
    onLeave(retval) {
      console.log(`[${now()}] ShareHandle::startTalk -> ${retval}`);
    },
  });

  Interceptor.attach(m.base.add(OFF.msgHttpDhStartTalkCallsite), {
    onEnter() {
      console.log(`[${now()}] MSG_HTTPDH_START_TALK callsite`);
    },
  });

  Interceptor.attach(m.base.add(OFF.httpClientInitSdpForTalkA), {
    onEnter(args) {
      console.log(`[${now()}] http_client_init_sdp_for_talk wrapperA x0=${args[0]} x1=${args[1]}`);
    },
    onLeave(retval) {
      console.log(`[${now()}] http_client_init_sdp_for_talk wrapperA -> ${retval}`);
    },
  });

  Interceptor.attach(m.base.add(OFF.httpClientInitSdpForTalkB), {
    onEnter(args) {
      console.log(`[${now()}] http_client_init_sdp_for_talk implB x0=${args[0]} x1=${args[1]}`);
    },
    onLeave(retval) {
      console.log(`[${now()}] http_client_init_sdp_for_talk implB -> ${retval}`);
    },
  });

  Interceptor.attach(m.base.add(OFF.httpClientPutFrameWrapper), {
    onEnter(args) {
      console.log(`[${now()}] http_client_put_frame wrapper x0=${args[0]} desc=${args[1]}`);
    },
    onLeave(retval) {
      console.log(`[${now()}] http_client_put_frame wrapper -> ${retval}`);
    },
  });

  Interceptor.attach(m.base.add(OFF.httpClientPutFrameImpl), {
    onEnter(args) {
      dumpFrameDesc(args[1]);
    },
    onLeave(retval) {
      console.log(`[${now()}] http_client_put_frame impl -> ${retval}`);
    },
  });
}

if (typeof Process === "undefined") {
  console.log("Frida runtime required; syntax/load guard passed.");
} else {
  attachWhenReady();
}
