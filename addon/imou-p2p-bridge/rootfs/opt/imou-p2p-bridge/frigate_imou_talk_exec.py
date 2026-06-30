#!/usr/bin/env python3
"""go2rtc exec backchannel -> Imou visualtalk.

go2rtc writes microphone audio to stdin when a stream source is marked with:
  #backchannel=1#audio=alaw/8000

This script opens an Imou visualtalk session, converts incoming Frigate/go2rtc
microphone audio to the camera's expected talk codec, and writes DHAV
interleaved audio frames until stdin closes.

The proven Frigate path for the tested example LAN camera is:
  browser mic -> WebRTC/go2rtc PCMA/8000 -> this script -> AAC/ADTS 16000
  -> DHAV -> DHHTTP visualtalk.xav

Other models may expect G.711 A-law/u-law rather than AAC. Keep the codec
profile per camera and pass --output-codec/--sample-rate explicitly.

For cameras reachable on the LAN, use --direct --host <camera-ip> --port 8086
to skip the P2P tunnel and reduce latency.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from imou_dhav import pack_dhav_audio, pack_dhhttp_interleaved, pcm_s16le_to_g711
    from imou_dhp2p import DHP2PTunnel, p2p_handshake
    from imou_visualtalk import VisualTalkClient
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from imou_dhav import pack_dhav_audio, pack_dhhttp_interleaved, pcm_s16le_to_g711  # type: ignore[no-redef]
    from imou_dhp2p import DHP2PTunnel, p2p_handshake  # type: ignore[no-redef]
    from imou_visualtalk import VisualTalkClient  # type: ignore[no-redef]


LOG_PATH = Path("/tmp/frigate_imou_talk_exec.log")


def log(msg: str) -> None:
    line = f"[imou-talk-exec] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except OSError:
        pass


def read_exact(stream, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = stream.read(size - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def upsample_alaw_x2(payload: bytes) -> bytes:
    out = bytearray(len(payload) * 2)
    for index, sample in enumerate(payload):
        out[index * 2] = sample
        out[index * 2 + 1] = sample
    return bytes(out)


def read_adts_frame(stream) -> bytes:
    header = read_exact(stream, 7)
    if not header:
        return b""
    if len(header) < 7 or header[0] != 0xFF or (header[1] & 0xF0) != 0xF0:
        raise ValueError("invalid ADTS sync from ffmpeg")
    protection_absent = header[1] & 0x01
    header_len = 7 if protection_absent else 9
    frame_len = ((header[3] & 0x03) << 11) | (header[4] << 3) | ((header[5] & 0xE0) >> 5)
    if frame_len < header_len:
        raise ValueError(f"invalid ADTS frame length {frame_len}")
    rest = read_exact(stream, frame_len - 7)
    if len(rest) < frame_len - 7:
        return b""
    return header + rest


def start_aac_encoder(args: argparse.Namespace) -> tuple[subprocess.Popen, threading.Thread]:
    ffmpeg = shutil.which("ffmpeg") or "/usr/lib/ffmpeg/7.0/bin/ffmpeg"
    proc = subprocess.Popen(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-f",
            "alaw" if args.input_codec == "alaw" else "s16le",
            "-ar",
            str(args.input_sample_rate),
            "-ac",
            "1",
            "-i",
            "-",
            "-af",
            f"volume={args.volume_gain}",
            "-ar",
            str(args.sample_rate),
            "-ac",
            "1",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            args.aac_bitrate,
            "-flush_packets",
            "1",
            "-f",
            "adts",
            "-",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    def feed() -> None:
        assert proc.stdin is not None
        try:
            while True:
                data = sys.stdin.buffer.read(4096)
                if not data:
                    break
                proc.stdin.write(data)
                proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    th = threading.Thread(target=feed, daemon=True)
    th.start()
    return proc, th


def start_drain(client: VisualTalkClient, stop: threading.Event) -> threading.Thread:
    th = threading.Thread(target=client._drain_interleaved, args=(stop,), daemon=True)
    th.start()
    return th


async def run(args: argparse.Namespace) -> int:
    if args.direct:
        log(f"direct visualtalk mode: {args.host}:{args.port}")
        return await asyncio.to_thread(run_talk, args)

    log(f"opening DHP2P tunnel for {args.serial} -> remote port {args.remote_port}")
    ptcp = await p2p_handshake(
        args.serial,
        relay_mode=args.relay,
        dtype=args.type,
        username=args.username,
        password=args.password,
        debug=args.debug,
    )
    tunnel = DHP2PTunnel(ptcp, args.remote_port, debug=args.debug)
    server_task = asyncio.create_task(tunnel.start(args.host, args.port))
    await asyncio.sleep(args.startup_delay)
    try:
        return await asyncio.to_thread(run_talk, args)
    finally:
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass


def run_talk(args: argparse.Namespace) -> int:
    ns = argparse.Namespace(
        channel=args.channel,
        subtype=args.subtype,
        encrypt=args.encrypt,
        sdp=args.sdp,
        open_only=False,
        track1=args.track1,
        track2=0,
        talk_track=args.talk_track,
        media_track=args.media_track,
    )
    client = VisualTalkClient(
        args.host,
        args.port,
        username=args.username,
        password=args.password,
        nonce=None,
        created=None,
        password_digest_override=None,
        lightweight_digest=None,
        timeout=args.timeout,
    )
    seq = 0
    tick = int(time.monotonic() * 1000)
    timestamp = int(time.time())
    samples_per_frame = args.input_sample_rate * args.frame_ms // 1000
    bytes_per_frame = samples_per_frame if args.input_codec == "alaw" else samples_per_frame * 2
    frame_interval = args.frame_ms / 1000.0
    next_send = time.monotonic()
    drain_stop = threading.Event()
    drain_thread = None
    encoder = None
    encoder_thread = None
    try:
        responses = client.start_talk(ns)
        for index, response in enumerate(responses):
            log(f"Cseq {index}: {response.status_line} body={len(response.body)}")
            if response.code >= 400:
                log(f"visualtalk failed: {response.status_line}")
                return 1
        drain_thread = start_drain(client, drain_stop)
        log(
            f"talk started for {args.serial}; reading stdin as "
            f"{args.input_codec}/{args.input_sample_rate}, output={args.output_codec}/{args.sample_rate}"
        )
        if args.output_codec == "aac-adts":
            encoder, encoder_thread = start_aac_encoder(args)
            assert encoder.stdout is not None
        while True:
            if args.output_codec == "aac-adts":
                assert encoder is not None and encoder.stdout is not None
                payload = read_adts_frame(encoder.stdout)
                if not payload:
                    break
            else:
                raw = read_exact(sys.stdin.buffer, bytes_per_frame)
                if not raw:
                    break
                if len(raw) < bytes_per_frame:
                    raw = raw.ljust(bytes_per_frame, b"\x00")
                if args.input_codec == "alaw":
                    payload = raw
                else:
                    payload = pcm_s16le_to_g711(raw, "alaw")
                if args.input_codec == "alaw" and args.input_sample_rate == 8000 and args.sample_rate == 16000:
                    payload = upsample_alaw_x2(payload)
            frame = pack_dhav_audio(payload, seq=seq, sample_rate=args.sample_rate, timestamp=timestamp, tick=tick)
            client.sock.sendall(pack_dhhttp_interleaved(frame, track_id=args.media_track))
            seq += 1
            if seq == 1 or seq % 100 == 0:
                log(f"sent {seq} frame(s)")
            tick += 64 if args.output_codec == "aac-adts" else args.frame_ms
            if args.output_codec != "aac-adts":
                next_send += frame_interval
                sleep_for = next_send - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
        log(f"stdin closed; sent {seq} frames")
        return 0
    finally:
        if encoder:
            encoder.terminate()
            try:
                encoder.wait(timeout=1)
            except subprocess.TimeoutExpired:
                encoder.kill()
        if encoder_thread:
            encoder_thread.join(timeout=0.2)
        drain_stop.set()
        if drain_thread:
            drain_thread.join(timeout=0.2)
        client.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--serial", required=True)
    p.add_argument("--username", default="admin")
    p.add_argument("--password", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18086)
    p.add_argument("--remote-port", type=int, default=8086)
    p.add_argument("--direct", action="store_true", help="connect directly to --host/--port instead of opening a DHP2P tunnel")
    p.add_argument("--type", type=int, default=0)
    p.add_argument("--relay", action="store_true", default=True)
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--subtype", type=int, default=0)
    p.add_argument("--encrypt", type=int, default=3)
    p.add_argument("--track1", type=int, default=6)
    p.add_argument("--talk-track", type=int, default=64)
    p.add_argument("--media-track", type=int, default=5)
    p.add_argument("--input-codec", choices=("s16le", "alaw"), default="alaw")
    p.add_argument("--input-sample-rate", type=int, default=8000)
    p.add_argument("--output-codec", choices=("alaw", "aac-adts"), default="alaw")
    p.add_argument("--aac-bitrate", default="32k")
    p.add_argument("--volume-gain", default="2.0")
    p.add_argument("--sample-rate", type=int, default=16000)
    p.add_argument("--frame-ms", type=int, default=20)
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--startup-delay", type=float, default=0.2)
    p.add_argument("--sdp")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
