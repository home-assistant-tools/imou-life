#!/usr/bin/env python3
"""Stream Imou app-local HTTP/XAV media as raw DHAV on stdout.

This is intended to be piped into ffmpeg:

    IMOU_RTSP_PASSWORD='<password>' scripts/imou_xav_bridge.py --auto \
      | ffmpeg -f dhav -i pipe:0 -c copy -f mpegts cam_san.ts
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass


DEFAULT_PATH = "/live/realmonitor.xav?channel={channel}&subtype={subtype}&audioType=1&proto=Private3"


@dataclass
class Endpoint:
    device_port: int
    host_port: int
    path: str


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def adb(args: list[str], serial: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def local_listen_ports(serial: str | None) -> list[int]:
    proc = adb(["shell", "ss -ltnp 2>/dev/null || netstat -an 2>/dev/null"], serial=serial)
    ports: list[int] = []
    for match in re.finditer(r"127\.0\.0\.1:(\d+)", proc.stdout):
        port = int(match.group(1))
        if port not in ports and port not in {27042}:
            ports.append(port)
    return ports


def forward(serial: str | None, host_port: int, device_port: int) -> None:
    adb(["forward", f"tcp:{host_port}", f"tcp:{device_port}"], serial=serial, check=False)


def read_until(sock: socket.socket, marker: bytes, limit: int) -> bytes:
    data = b""
    while marker not in data and len(data) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def request_headers(host_port: int, path: str, auth_header: str | None = None, timeout: float = 3.0) -> bytes:
    with socket.create_connection(("127.0.0.1", host_port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        headers = [
            f"GET {path} HTTP/1.1",
            f"Host: 127.0.0.1:{host_port}",
            "User-Agent: imou-xav-bridge",
            "Connection: close",
        ]
        if auth_header:
            headers.append("Authorization: " + auth_header)
        sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
        return read_until(sock, b"\r\n\r\n", 8192)


def parse_digest(header_text: str) -> dict[str, str] | None:
    match = re.search(r"WWW-Authenticate:\s*Digest\s+([^\r\n]+)", header_text, re.I)
    if not match:
        return None
    return dict(re.findall(r'(\w+)="?([^",]+)"?', match.group(1)))


def digest_auth(user: str, password: str, values: dict[str, str], path: str) -> str:
    realm = values["realm"]
    nonce = values["nonce"]
    qop = values.get("qop")
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"GET:{path}".encode()).hexdigest()
    if qop:
        qop = qop.split(",", 1)[0].strip()
        nc = "00000001"
        cnonce = "abcdef0123456789"
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
        return (
            f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
            f'uri="{path}", response="{response}", qop={qop}, nc={nc}, cnonce="{cnonce}"'
        )
    response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{path}", response="{response}"'


def parse_private_length(header_text: str) -> int:
    for line in header_text.split("\r\n"):
        if line.lower().startswith("private-length:"):
            return int(line.split(":", 1)[1].strip())
    return 0


def probe_endpoint(
    serial: str | None,
    device_port: int,
    host_port: int,
    path: str,
    user: str,
    password: str,
    timeout: float,
) -> Endpoint | None:
    forward(serial, host_port, device_port)
    try:
        first = request_headers(host_port, path, timeout=timeout)
    except OSError:
        return None
    values = parse_digest(first.decode("latin1", errors="replace"))
    if not values:
        return None
    auth = digest_auth(user, password, values, path)
    try:
        second = request_headers(host_port, path, auth_header=auth, timeout=timeout)
    except OSError:
        return None
    first_line = second.decode("latin1", errors="replace").split("\r\n", 1)[0]
    if " 200 " in first_line or first_line.endswith(" 200 OK"):
        return Endpoint(device_port, host_port, path)
    return None


def open_stream(endpoint: Endpoint, user: str, password: str, timeout: float) -> socket.socket:
    first = request_headers(endpoint.host_port, endpoint.path, timeout=timeout)
    values = parse_digest(first.decode("latin1", errors="replace"))
    if not values:
        raise RuntimeError("endpoint did not return Digest challenge")
    auth = digest_auth(user, password, values, endpoint.path)

    sock = socket.create_connection(("127.0.0.1", endpoint.host_port), timeout=timeout)
    sock.settimeout(None)
    headers = [
        f"GET {endpoint.path} HTTP/1.1",
        f"Host: 127.0.0.1:{endpoint.host_port}",
        "User-Agent: imou-xav-bridge",
        "Connection: close",
        "Authorization: " + auth,
    ]
    sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
    return sock


def stream_dhav(endpoint: Endpoint, user: str, password: str, timeout: float) -> int:
    sock = open_stream(endpoint, user, password, timeout)
    try:
        header_blob = read_until(sock, b"\r\n\r\n", 8192)
        header, _, buffered = header_blob.partition(b"\r\n\r\n")
        header_text = header.decode("latin1", errors="replace")
        first_line = header_text.split("\r\n", 1)[0]
        if " 200 " not in first_line and not first_line.endswith(" 200 OK"):
            raise RuntimeError(first_line)
        private_length = parse_private_length(header_text)

        remaining_sdp = private_length
        if buffered:
            drop = min(len(buffered), remaining_sdp)
            buffered = buffered[drop:]
            remaining_sdp -= drop
        while remaining_sdp > 0:
            chunk = sock.recv(min(8192, remaining_sdp))
            if not chunk:
                raise RuntimeError("stream ended before SDP was fully skipped")
            remaining_sdp -= len(chunk)

        out = sys.stdout.buffer
        if buffered:
            out.write(buffered)
            out.flush()
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            out.write(chunk)
            out.flush()
        return 0
    finally:
        sock.close()


def choose_endpoint(args: argparse.Namespace, path: str, user: str, password: str) -> Endpoint:
    if args.device_port:
        host_port = args.host_port or (42000 + (args.device_port % 1000))
        forward(args.serial, host_port, args.device_port)
        return Endpoint(args.device_port, host_port, path)

    ports = local_listen_ports(args.serial)
    if not ports:
        raise SystemExit("No app-local 127.0.0.1 listen ports found. Open the camera in Imou Life first.")
    for device_port in ports:
        host_port = args.host_port or (42000 + (device_port % 1000))
        endpoint = probe_endpoint(
            args.serial,
            device_port,
            host_port,
            path,
            user,
            password,
            args.timeout,
        )
        if endpoint:
            return endpoint
    raise SystemExit("No working HTTP/XAV endpoint found. Make sure the target camera is playing.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL"))
    parser.add_argument("--user", default=os.environ.get("IMOU_RTSP_USER", "admin"))
    parser.add_argument("--password", default=os.environ.get("IMOU_RTSP_PASSWORD"))
    parser.add_argument("--channel", type=int, default=1)
    parser.add_argument("--subtype", type=int, default=0)
    parser.add_argument("--path", help="Override the XAV path")
    parser.add_argument("--device-port", type=int, help="Known phone-local XAV port")
    parser.add_argument("--host-port", type=int, help="adb forward host port")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--auto", action="store_true", help="Auto-discover the phone-local XAV port")
    args = parser.parse_args()

    if not args.password:
        raise SystemExit("Set IMOU_RTSP_PASSWORD or pass --password.")
    if not args.auto and not args.device_port:
        raise SystemExit("Pass --auto or --device-port.")

    path = args.path or DEFAULT_PATH.format(channel=args.channel, subtype=args.subtype)
    endpoint = choose_endpoint(args, path, args.user, args.password)
    log(
        f"Using phone port {endpoint.device_port} via host port {endpoint.host_port}; "
        f"path={endpoint.path}"
    )
    return stream_dhav(endpoint, args.user, args.password, args.timeout)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        os._exit(0)
