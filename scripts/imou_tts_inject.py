#!/usr/bin/env python3
"""
Make an Imou camera speak arbitrary audio (TTS) — WORKING method.

Instead of reimplementing the proprietary cloud talk protocol (DHHTTP-media +
HTTPDH_START_TALK handshake + DHEncrypt3 + P2P), we let the patched Imou Life app
do all of that and simply *replace the microphone PCM* it feeds to its AAC encoder
with our own audio. The app encodes it to AAC and ships it to the camera over P2P;
the camera plays our audio. See docs/talk-protocol-reverse-engineering.md.

Pipeline hook point (the win):
    LCAACAudioEncoder::Encode  (PCM in -> AAC out), libCommonSDK.so vtable[3]
    signature: Encode(this, int16_pcm*, byte_len, ...)   pcm = mono s16le @ 16 kHz
We overwrite the PCM input buffer each call with successive bytes of our clip.

Usage:
    # from text (uses macOS `say`) or from an audio file
    python3 scripts/imou_tts_inject.py --text "Xin chao phong khach"
    python3 scripts/imou_tts_inject.py --audio message.wav
    python3 scripts/imou_tts_inject.py --pcm clip_16k_s16le.raw   # raw, skip convert

Prereqs (verified setup):
  - Imou Life patched with a Frida gadget injected as DT_NEEDED of libCommonSDK.so,
    gadget listening on device 127.0.0.1:27420 (`adb forward tcp:27420 tcp:27420`).
  - ffmpeg in PATH (for --audio/--text conversion). On macOS `say` for --text.
  - frida CLI in PATH.
  - The app open on the target camera's live view; toggle the talk (mic) button
    ON *after* this script reports "inject ready" to play the clip from the start.

IMPORTANT: ENC_OFFSET below is for one specific LIEF-patched build of libCommonSDK.so.
If the app is re-patched/updated, recompute it: find typeinfo
`N2LC7PlaySDK17LCAACAudioEncoderE` -> vtable -> vtable[3] (the method whose 2nd arg
is a ~1280-byte PCM buffer). Helper logic is in the repo's RE notes.
"""
import argparse, os, subprocess, sys, tempfile

ENC_OFFSET = 0x995240          # LCAACAudioEncoder::Encode in the patched libCommonSDK.so
MODULE = "libCommonSDK.so"
GADGET_HOST = "127.0.0.1:27420"
SAMPLE_RATE = 16000            # the app's talk encoder runs at 16 kHz mono


def to_pcm(args):
    if args.pcm:
        return open(args.pcm, "rb").read()
    src = args.audio
    tmp = None
    if args.text:
        if sys.platform != "darwin":
            sys.exit("--text uses macOS `say`; on other OSes pass --audio instead")
        tmp = tempfile.NamedTemporaryFile(suffix=".aiff", delete=False).name
        subprocess.run(["say", "-r", "160", "-o", tmp, args.text], check=True)
        src = tmp
    if not src:
        sys.exit("provide --text, --audio or --pcm")
    out = tempfile.NamedTemporaryFile(suffix=".raw", delete=False).name
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", src, "-ar", str(SAMPLE_RATE),
         "-ac", "1", "-filter:a", f"volume={args.volume}", "-f", "s16le", out],
        check=True,
    )
    data = open(out, "rb").read()
    for f in (tmp, out):
        if f and os.path.exists(f):
            os.unlink(f)
    return data


def build_js(pcm: bytes, lead_silence_s: float) -> str:
    pcm = (b"\x00" * int(SAMPLE_RATE * 2 * lead_silence_s)) + pcm
    hexs = pcm.hex()
    return f'''var m=Process.findModuleByName("{MODULE}");
console.log("[*] base="+m.base);
var HEX="{hexs}";
var pcm=new Uint8Array(HEX.length/2);
for(var i=0;i<pcm.length;i++){{pcm[i]=parseInt(HEX.substr(i*2,2),16);}}
console.log("[*] TTS embedded "+pcm.length+" bytes ({len(pcm)//(SAMPLE_RATE*2)}s)");
var pos=0,done=false;
Interceptor.attach(m.base.add({hex(ENC_OFFSET)}),{{onEnter:function(a){{
  var dst=a[1],n=a[2].toInt32();
  if(n<=0||n>65536)return;
  var b=new Uint8Array(n);
  for(var i=0;i<n;i++){{b[i]=pos<pcm.length?pcm[pos++]:0;}}
  dst.writeByteArray(b.buffer);
  if(pos>=pcm.length&&!done){{done=true;console.log("[*] clip fully injected");}}
}}}});
console.log("[*] inject ready @ AAC encoder PCM in -> toggle the talk button now");
'''


def main():
    ap = argparse.ArgumentParser(description="Inject TTS audio into Imou app talk path")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", help="text to speak (macOS `say`)")
    g.add_argument("--audio", help="audio file (any ffmpeg-readable)")
    g.add_argument("--pcm", help="raw mono s16le 16kHz PCM (no conversion)")
    ap.add_argument("--volume", default="3.0", help="ffmpeg volume gain (default 3.0)")
    ap.add_argument("--lead", type=float, default=0.5, help="lead silence seconds")
    ap.add_argument("--out", default="imou_tts_inject.js", help="output Frida script path")
    ap.add_argument("--run", action="store_true", help="launch frida after building")
    a = ap.parse_args()

    pcm = to_pcm(a)
    js = build_js(pcm, a.lead)
    open(a.out, "w").write(js)
    print(f"[+] wrote {a.out} ({len(js)} bytes, {len(pcm)//(SAMPLE_RATE*2)}s audio)")
    print(f"[i] adb forward tcp:27420 tcp:27420   (once)")
    cmd = ["frida", "-H", GADGET_HOST, "-n", "Gadget", "-l", a.out, "--runtime=v8"]
    if a.run:
        print("[+] launching:", " ".join(cmd))
        os.execvp("frida", cmd)
    else:
        print("[i] run it with:\n    " + " ".join(cmd))
        print("[i] then open the camera live view and toggle the talk (mic) button ON.")


if __name__ == "__main__":
    main()
