#!/usr/bin/env python3
"""Probe Imou/Dahua LAN RTSP streams without printing credentials."""

from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import json
import os
import socket
import subprocess
from dataclasses import dataclass


@dataclass
class ProbeResult:
    ip: str
    port: int
    channel: int
    subtype: int
    ok: bool
    status: str
    streams: list[dict]

    @property
    def safe_url(self) -> str:
        return (
            f"rtsp://<user>:<password>@{self.ip}:{self.port}"
            f"/cam/realmonitor?channel={self.channel}&subtype={self.subtype}"
        )


def parse_csv_ints(value: str) -> list[int]:
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def parse_ips(values: list[str]) -> list[str]:
    ips: list[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                ips.append(part)
    return ips


def is_port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_subnet(subnet: str, port: int, timeout: float, workers: int) -> list[str]:
    network = ipaddress.ip_network(subnet, strict=False)
    hosts = [str(host) for host in network.hosts()]
    open_hosts: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(is_port_open, host, port, timeout): host for host in hosts
        }
        for future in concurrent.futures.as_completed(future_map):
            host = future_map[future]
            if future.result():
                open_hosts.append(host)
    return sorted(open_hosts, key=lambda item: tuple(int(part) for part in item.split(".")))


def ffprobe_stream(
    ip: str,
    port: int,
    channel: int,
    subtype: int,
    user: str,
    password: str,
    timeout: float,
) -> ProbeResult:
    url = (
        f"rtsp://{user}:{password}@{ip}:{port}"
        f"/cam/realmonitor?channel={channel}&subtype={subtype}"
    )
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-rtsp_transport",
        "tcp",
        "-rw_timeout",
        str(int(timeout * 1_000_000)),
        "-show_entries",
        "stream=codec_type,codec_name,width,height",
        "-of",
        "json",
        url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout + 2,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(ip, port, channel, subtype, False, "timeout", [])

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "ffprobe failed").strip().splitlines()
        status = detail[-1] if detail else "ffprobe failed"
        status = status.replace(password, "<password>").replace(user, "<user>")
        return ProbeResult(ip, port, channel, subtype, False, status, [])

    try:
        streams = json.loads(proc.stdout or "{}").get("streams", [])
    except json.JSONDecodeError:
        return ProbeResult(ip, port, channel, subtype, False, "invalid ffprobe json", [])

    if not streams:
        return ProbeResult(ip, port, channel, subtype, False, "no streams", [])

    return ProbeResult(ip, port, channel, subtype, True, "ok", streams)


def print_result(result: ProbeResult, as_json: bool) -> None:
    payload = {
        "ip": result.ip,
        "port": result.port,
        "channel": result.channel,
        "subtype": result.subtype,
        "ok": result.ok,
        "status": result.status,
        "streams": result.streams,
        "url": result.safe_url,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
        return

    stream_text = ",".join(
        f"{stream.get('codec_type')}:{stream.get('codec_name')}:"
        f"{stream.get('width', '')}x{stream.get('height', '')}"
        for stream in result.streams
    )
    suffix = f" {stream_text}" if stream_text else f" {result.status}"
    print(
        f"{result.ip}:{result.port} channel={result.channel} subtype={result.subtype} "
        f"{'OK' if result.ok else 'ERR'}{suffix}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=os.environ.get("IMOU_RTSP_USER", "admin"))
    parser.add_argument("--password", default=os.environ.get("IMOU_RTSP_PASSWORD"))
    parser.add_argument("--ips", nargs="*", default=[], help="IP list or comma-separated IPs")
    parser.add_argument("--subnet", help="Scan subnet first, for example 192.168.2.0/24")
    parser.add_argument("--port", type=int, default=554)
    parser.add_argument("--channels", default="1", help="Comma-separated channels, default 1")
    parser.add_argument("--subtypes", default="0,1", help="Comma-separated subtypes, default 0,1")
    parser.add_argument("--scan-timeout", type=float, default=0.35)
    parser.add_argument("--probe-timeout", type=float, default=5.0)
    parser.add_argument("--workers", type=int, default=128)
    parser.add_argument("--json", action="store_true", help="Emit JSON lines")
    args = parser.parse_args()

    if not args.password:
        raise SystemExit("Set IMOU_RTSP_PASSWORD or pass --password.")

    ips = parse_ips(args.ips)
    if args.subnet:
        ips.extend(scan_subnet(args.subnet, args.port, args.scan_timeout, args.workers))
    ips = sorted(set(ips), key=lambda item: tuple(int(part) for part in item.split(".")))
    if not ips:
        raise SystemExit("Provide --ips or --subnet.")

    channels = parse_csv_ints(args.channels)
    subtypes = parse_csv_ints(args.subtypes)

    for ip in ips:
        for channel in channels:
            for subtype in subtypes:
                result = ffprobe_stream(
                    ip,
                    args.port,
                    channel,
                    subtype,
                    args.user,
                    args.password,
                    args.probe_timeout,
                )
                print_result(result, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
