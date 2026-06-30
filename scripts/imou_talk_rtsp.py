#!/usr/bin/env python3
"""Experimental RTSP ANNOUNCE talkback to a Dahua/Imou camera speaker.

Pushes audio (e.g. TTS) to the camera's speaker over RTSP `ANNOUNCE` on port 554.
Works directly on the LAN, or over a `dh-p2p` cloud-P2P tunnel (point --host/--port
at the local tunnel listener — see docs/cloud-p2p-stream.md).

STATUS: the camera accepts `ANNOUNCE` (200 OK) and the interleaved RTP/PCMA bytes
without resetting, but clean speaker output is not yet confirmed (the RTP framing
likely needs SETUP/RECORD or timing/DHAV refinement). See docs/two-way-audio.md
and docs/lan-dvrip-talk.md.

Audio input: raw G.711 A-law @ 8000 Hz mono (produce with e.g.
  ffmpeg -i in.wav -ar 8000 -ac 1 -f alaw out.alaw).

Usage:
  IMOU_RTSP_PASSWORD=... python3 scripts/imou_talk_rtsp.py <host> out.alaw \
      [--user admin] [--port 554]
"""
import argparse
import hashlib
import os
import re
import socket
import struct
import sys
import time

PATH_TMPL = "/cam/realmonitor?channel=1&subtype=0&proto=Private3"

def md5(s): return hashlib.md5(s.encode()).hexdigest()

class RTSPTalk:
    def __init__(self, host, port, user, pw):
        self.host, self.port, self.user, self.pw = host, port, user, pw
        self.base = f"rtsp://{host}:{port}{PATH_TMPL}"
        self.s = socket.create_connection((host, port), 5); self.s.settimeout(5)
        self.cseq = 0; self.realm = self.nonce = None

    def _recv(self):
        buf = b""
        try:
            while b"\r\n\r\n" not in buf:
                c = self.s.recv(4096)
                if not c: break
                buf += c
        except socket.timeout:
            pass
        return buf.decode("latin1", "replace")

    def req(self, method, headers="", body=""):
        self.cseq += 1
        h = f"{method} {self.base} RTSP/1.0\r\nCSeq: {self.cseq}\r\nUser-Agent: imou-talk\r\n"
        if self.realm:
            ha1 = md5(f"{self.user}:{self.realm}:{self.pw}"); ha2 = md5(f"{method}:{self.base}")
            h += (f'Authorization: Digest username="{self.user}", realm="{self.realm}", '
                  f'nonce="{self.nonce}", uri="{self.base}", response="{md5(f"{ha1}:{self.nonce}:{ha2}")}"\r\n')
        if body:
            h += "Content-Type: application/sdp\r\n" + f"Content-Length: {len(body)}\r\n"
        h += headers + "\r\n" + body
        self.s.sendall(h.encode())
        d = self._recv()
        m = re.search(r'realm="([^"]+)",\s*nonce="([^"]+)"', d)
        if m: self.realm, self.nonce = m.groups()
        return d

    def announce(self):
        self.req("OPTIONS")  # 401 -> realm/nonce
        sdp = ("v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=talk\r\nc=IN IP4 0.0.0.0\r\nt=0 0\r\n"
               "m=audio 0 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\na=control:trackID=1\r\na=sendonly\r\n")
        return self.req("ANNOUNCE", body=sdp).split("\r\n", 1)[0]

    def stream_alaw(self, pcma, chunk=160):
        ssrc, seq, ts = 0x11223344, 0, 0
        start = time.time(); n = 0
        for off in range(0, len(pcma), chunk):
            payload = pcma[off:off + chunk]
            if len(payload) < chunk:
                payload += b"\xd5" * (chunk - len(payload))  # alaw silence
            rtp = struct.pack("!BBHII", 0x80, 8, seq & 0xffff, ts & 0xffffffff, ssrc) + payload
            pkt = b"$" + bytes([0]) + struct.pack("!H", len(rtp)) + rtp  # interleaved ch0
            self.s.sendall(pkt)
            seq += 1; ts += chunk; n += 1
            d = start + n * 0.02 - time.time()
            if d > 0: time.sleep(d)
        return n

    def close(self):
        self.s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("alaw", help="raw G.711 A-law @8000 mono file")
    ap.add_argument("--user", default=os.environ.get("IMOU_RTSP_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("IMOU_RTSP_PASSWORD"))
    ap.add_argument("--port", type=int, default=554)
    args = ap.parse_args()
    if not args.password:
        sys.exit("set IMOU_RTSP_PASSWORD or pass --password")
    pcma = open(args.alaw, "rb").read()
    print(f"audio: {len(pcma)} bytes (~{len(pcma)/8000.0:.1f}s)")
    t = RTSPTalk(args.host, args.port, args.user, args.password)
    print("ANNOUNCE:", t.announce())
    n = t.stream_alaw(pcma)
    print(f"streamed {n} RTP packets")
    t.close()


if __name__ == "__main__":
    main()
