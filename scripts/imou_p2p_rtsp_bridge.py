#!/usr/bin/env python3
"""Run a Dahua/Imou P2P RTSP tunnel from the public dh-p2p PoC.

This wrapper keeps camera credentials out of the bridge process. It only opens
the P2P tunnel to the camera serial. RTSP authentication still happens between
the RTSP client and the camera through the tunnel.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DH_P2P_DIR = REPO_ROOT / "artifacts" / "research" / "dh-p2p"


REDACTIONS = (
    (re.compile(r'(PasswordDigest=")[^"]+'), r"\1<redacted>"),
    (re.compile(r"(<Token>)[^<]+"), r"\1<redacted>"),
    (re.compile(r'("body/Token":\s*")[^"]+'), r"\1<redacted>"),
    (re.compile(r"(/relay/start/)[^\s]+"), r"\1<redacted>"),
)


def redact(line: str) -> str:
    for pattern, replacement in REDACTIONS:
        line = pattern.sub(replacement, line)
    return line


def parse_bind(value: str) -> tuple[str, int]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("bind must look like host:port")
    host, port_text = value.rsplit(":", 1)
    try:
        port = int(port_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bind port must be an integer") from exc
    if not host:
        raise argparse.ArgumentTypeError("bind host cannot be empty")
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("bind port must be in 1..65535")
    return host, port


def build_binary(dh_p2p_dir: Path) -> Path:
    binary = dh_p2p_dir / "target" / "debug" / "dh-p2p"
    if binary.exists():
        return binary
    if not (dh_p2p_dir / "Cargo.toml").exists():
        raise SystemExit(
            f"dh-p2p source not found at {dh_p2p_dir}. "
            "Clone https://github.com/khoanguyen-3fc/dh-p2p there first."
        )
    subprocess.run(["cargo", "build"], cwd=dh_p2p_dir, check=True)
    if not binary.exists():
        raise SystemExit(f"cargo build finished but {binary} was not created")
    return binary


def reader_thread(
    stream,
    *,
    verbose: bool,
    ready: threading.Event,
    tail: deque[str],
) -> None:
    for raw_line in iter(stream.readline, ""):
        line = redact(raw_line.rstrip("\n"))
        tail.append(line)
        if "Ready to connect!" in line or "RTSP URL:" in line:
            ready.set()
        if verbose or "panic" in line.lower() or "error" in line.lower():
            print(line, flush=True)
    stream.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Expose a remote Imou/Dahua camera RTSP port through P2P."
    )
    parser.add_argument("serial", help="camera/NVR serial number")
    parser.add_argument(
        "--bind",
        type=parse_bind,
        default=parse_bind("127.0.0.1:1554"),
        help="local bind address and port, default: 127.0.0.1:1554",
    )
    parser.add_argument(
        "--remote-port",
        type=int,
        default=554,
        help="remote TCP port to tunnel, default: 554 (RTSP)",
    )
    parser.add_argument(
        "--relay",
        action="store_true",
        help="force relay mode; useful when direct UDP P2P is unstable",
    )
    parser.add_argument(
        "--dh-p2p-dir",
        type=Path,
        default=DEFAULT_DH_P2P_DIR,
        help=f"dh-p2p checkout path, default: {DEFAULT_DH_P2P_DIR}",
    )
    parser.add_argument(
        "--binary",
        type=Path,
        help="prebuilt dh-p2p binary; skips cargo build when supplied",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print redacted dh-p2p logs",
    )
    args = parser.parse_args()

    host, bind_port = args.bind
    if not 1 <= args.remote_port <= 65535:
        raise SystemExit("--remote-port must be in 1..65535")

    binary = args.binary or build_binary(args.dh_p2p_dir)
    port_spec = f"{host}:{bind_port}:{args.remote_port}"
    cmd = [str(binary), "-p", port_spec]
    if args.relay:
        cmd.append("--relay")
    cmd.append(args.serial)

    env = os.environ.copy()
    env.setdefault("RUST_BACKTRACE", "0")

    ready = threading.Event()
    tail: deque[str] = deque(maxlen=30)
    proc = subprocess.Popen(
        cmd,
        cwd=args.dh_p2p_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    threads = [
        threading.Thread(
            target=reader_thread,
            args=(proc.stdout,),
            kwargs={"verbose": args.verbose, "ready": ready, "tail": tail},
            daemon=True,
        ),
        threading.Thread(
            target=reader_thread,
            args=(proc.stderr,),
            kwargs={"verbose": True, "ready": ready, "tail": tail},
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()

    def stop_child(_signum=None, _frame=None) -> None:
        if proc.poll() is None:
            proc.terminate()

    signal.signal(signal.SIGINT, stop_child)
    signal.signal(signal.SIGTERM, stop_child)

    print(
        f"Starting P2P tunnel for serial {args.serial} on {host}:{bind_port}...",
        flush=True,
    )
    deadline = time.monotonic() + 45
    while proc.poll() is None and not ready.is_set() and time.monotonic() < deadline:
        time.sleep(0.2)

    if ready.is_set():
        print("Bridge ready.", flush=True)
        if args.remote_port == 554:
            print(
                "Local RTSP base: "
                f"rtsp://{host}:{bind_port}/cam/realmonitor?channel=1&subtype=0",
                flush=True,
            )
        print(
            "Leave this process running while go2rtc/ffmpeg consumes the stream.",
            flush=True,
        )
    elif proc.poll() is None:
        print(
            "Bridge is still starting. Re-run with --verbose for protocol logs.",
            flush=True,
        )
    else:
        print("Bridge exited before it became ready.", file=sys.stderr)
        for line in tail:
            print(line, file=sys.stderr)
        return proc.returncode or 1

    try:
        return proc.wait()
    finally:
        stop_child()


if __name__ == "__main__":
    raise SystemExit(main())
