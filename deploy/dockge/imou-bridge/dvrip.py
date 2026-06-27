#!/usr/bin/env python3
"""Standalone Dahua/Imou DVRIP (TCP/37777) client: login + JSON-RPC.

Talks directly to a camera on the LAN — no Imou app, no cloud, no P2P. This is
the base layer for Option C (two-way talk to a LAN camera). The real-time talk
audio channel (CLIENT_StartTalkEx-style binary sub-protocol) is not implemented
yet; this module gets an authenticated session and a working JSON-RPC channel.

Login protocol derived from mcw0/DahuaConsole. Credentials come from env
(IMOU_RTSP_USER / IMOU_RTSP_PASSWORD) or CLI args; nothing is stored in the repo.

Usage:
    IMOU_RTSP_PASSWORD=... python3 scripts/imou_dvrip.py 192.168.2.20 \
        [--user admin] [--rpc magicBox.getDeviceType]
"""
import argparse
import hashlib
import json
import os
import re
import socket
import struct
import sys

PORT = 37777


def gen1_hash(password: str) -> str:
    """Legacy Dahua 'compressor' hash (haicen/DahuaHashCreator)."""
    s = hashlib.md5(password.encode("latin-1")).digest()
    out = [0] * 8
    i = j = 0
    while i < len(s):
        v = (s[i] + s[i + 1]) % 62
        v += 48 if v < 10 else (55 if v < 36 else 61)
        out[j] = v
        i += 2
        j += 1
    return "".join(chr(c) for c in out)


def _gen2(user, realm, rnd, pw):
    g = hashlib.md5(f"{user}:{realm}:{pw}".encode("latin-1")).hexdigest().upper()
    return hashlib.md5(f"{user}:{rnd}:{g}".encode("latin-1")).hexdigest().upper()


def _dvrip_md5(user, rnd, pw):
    g1 = gen1_hash(pw)
    return hashlib.md5(f"{user}:{rnd}:{g1}".encode("latin-1")).hexdigest().upper()


def _recvn(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("socket closed")
        buf += chunk
    return buf


class DvripClient:
    def __init__(self, host, user, password, port=PORT, timeout=4):
        self.host, self.user, self.password, self.port = host, user, password, port
        self.timeout = timeout
        self.sock = None
        self.session_id = 0
        self.realm = None
        self._rid = 1

    def login(self):
        s = socket.create_connection((self.host, self.port), self.timeout)
        s.settimeout(self.timeout)
        self.sock = s
        # 1) realm/random challenge
        s.sendall(struct.pack(">I", 0xA0010000) + b"\x00" * 20 + struct.pack(">Q", 0x050201010000A1AA))
        hdr = _recvn(s, 32)
        blen = struct.unpack("<I", hdr[4:8])[0]
        body = _recvn(s, blen).decode("latin-1", "replace") if blen else ""
        rm = re.search(r"Realm:([^\r\n]*)", body)
        rn = re.search(r"Random:([^\r\n]*)", body)
        if not (rm and rn and rm.group(1) and rn.group(1)):
            raise RuntimeError("login challenge returned empty realm/random")
        self.realm = rm.group(1)
        rnd = rn.group(1)
        # 2) hashed login
        h = self.user + "&&" + _gen2(self.user, self.realm, rnd, self.password) + _dvrip_md5(self.user, rnd, self.password)
        s.sendall(
            struct.pack(">I", 0xA0050000) + struct.pack("<I", len(h)) + b"\x00" * 16
            + struct.pack(">Q", 0x050200080000A1AA) + h.encode("latin-1")
        )
        resp = _recvn(s, 32)
        err = resp[8:10]
        self.session_id = struct.unpack("<I", resp[16:20])[0]
        extra = struct.unpack("<I", resp[4:8])[0]
        if extra:
            try:
                _recvn(s, extra)
            except Exception:
                pass
        if err != b"\x00\x08":
            codes = {b"\x01\x00": "auth failed", b"\x01\x01": "invalid user",
                     b"\x01\x04": "account locked", b"\x03\x03": "already logged in"}
            raise RuntimeError(f"login failed: {err.hex()} ({codes.get(err, 'unknown')})")
        return self.session_id

    def rpc(self, method, params=None, extra=None):
        self._rid += 1
        obj = {"method": method, "id": self._rid, "session": self.session_id}
        if params is not None:
            obj["params"] = params
        if extra:
            obj.update(extra)
        data = json.dumps(obj).encode("latin-1")
        hdr = (struct.pack(">I", 0xF6000000) + struct.pack("<I", len(data)) + struct.pack("<I", self._rid)
               + struct.pack("<I", 0) + struct.pack("<I", len(data)) + struct.pack("<I", 0)
               + struct.pack("<I", self.session_id) + struct.pack("<I", 0))
        self.sock.sendall(hdr + data)
        rh = _recvn(self.sock, 32)
        blen = struct.unpack("<I", rh[4:8])[0]
        body = _recvn(self.sock, blen) if blen else b""
        return json.loads(body.decode("latin-1", "replace")) if body else {}

    def close(self):
        if self.sock:
            self.sock.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--user", default=os.environ.get("IMOU_RTSP_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("IMOU_RTSP_PASSWORD"))
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--rpc", default="magicBox.getDeviceType",
                    help="JSON-RPC method to call after login")
    args = ap.parse_args()
    if not args.password:
        sys.exit("set IMOU_RTSP_PASSWORD or pass --password")
    c = DvripClient(args.host, args.user, args.password, args.port)
    sid = c.login()
    print(f"[+] login OK  session={hex(sid)}  realm={c.realm!r}")
    print(json.dumps(c.rpc(args.rpc), ensure_ascii=False, indent=2)[:800])
    c.close()


if __name__ == "__main__":
    main()
