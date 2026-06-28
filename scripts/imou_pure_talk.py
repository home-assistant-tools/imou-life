#!/usr/bin/env python3
"""One-shot pure-Python Imou visualtalk sender.

This is a convenience wrapper around `imou_dhp2p.py` + `imou_visualtalk.py`:
it loads the camera credentials from the standalone app asset, opens a pure
Python DHP2P/PTCP relay tunnel to remote port 8086, then sends either a PCM
file, a generated tone, or macOS `say` TTS through `visualtalk.xav`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from imou_dhp2p import DHP2PTunnel, p2p_handshake
    from imou_visualtalk import VisualTalkClient
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from imou_dhp2p import DHP2PTunnel, p2p_handshake  # type: ignore[no-redef]
    from imou_visualtalk import VisualTalkClient  # type: ignore[no-redef]


DEFAULT_DEVICE_JSON = Path("standalone-app/out/assets/device.json")


def load_device(path: Path, index: int) -> dict:
    devices = json.loads(path.read_text())
    if not isinstance(devices, list) or not devices:
        raise ValueError(f"{path} does not contain a non-empty device list")
    try:
        return devices[index]
    except IndexError as exc:
        raise ValueError(f"device index {index} out of range; available: {len(devices)}") from exc


def tone_pcm(*, seconds: float, frequency: float, sample_rate: int, amplitude: float) -> bytes:
    sample_count = max(1, int(seconds * sample_rate))
    amp = max(0.0, min(amplitude, 1.0)) * 32767
    out = bytearray(sample_count * 2)
    for i in range(sample_count):
        sample = int(math.sin(2.0 * math.pi * frequency * i / sample_rate) * amp)
        struct.pack_into("<h", out, i * 2, sample)
    return bytes(out)


def ffmpeg_to_s16le(input_path: Path, *, sample_rate: int) -> bytes:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to convert non-PCM audio inputs")
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    return proc.stdout


def ffmpeg_file_to_aac_adts(input_path: Path, *, sample_rate: int) -> bytes:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to create AAC/ADTS audio")
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            "32k",
            "-f",
            "adts",
            "-",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    return proc.stdout


def ffmpeg_pcm_to_aac_adts(pcm: bytes, *, sample_rate: int) -> bytes:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to create AAC/ADTS audio")
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "s16le",
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-i",
            "-",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            "32k",
            "-f",
            "adts",
            "-",
        ],
        input=pcm,
        check=True,
        stdout=subprocess.PIPE,
    )
    return proc.stdout


def text_to_pcm(text: str, *, sample_rate: int) -> bytes:
    say = shutil.which("say")
    if not say:
        raise RuntimeError("--text currently requires macOS `say`; use --audio or --tone instead")
    with tempfile.TemporaryDirectory() as tmp:
        aiff = Path(tmp) / "tts.aiff"
        subprocess.run([say, "-o", str(aiff), text], check=True)
        return ffmpeg_to_s16le(aiff, sample_rate=sample_rate)


def read_audio(args: argparse.Namespace) -> bytes:
    if args.text:
        pcm = text_to_pcm(args.text, sample_rate=args.sample_rate)
        return ffmpeg_pcm_to_aac_adts(pcm, sample_rate=args.sample_rate) if args.codec in {"aac-adts", "aac-raw"} else pcm
    if args.audio:
        audio_path = Path(args.audio)
        if args.raw_s16le:
            pcm = audio_path.read_bytes()
            return ffmpeg_pcm_to_aac_adts(pcm, sample_rate=args.sample_rate) if args.codec in {"aac-adts", "aac-raw"} else pcm
        if args.codec in {"aac-adts", "aac-raw"}:
            return ffmpeg_file_to_aac_adts(audio_path, sample_rate=args.sample_rate)
        return ffmpeg_to_s16le(audio_path, sample_rate=args.sample_rate)
    pcm = tone_pcm(
        seconds=args.tone_seconds,
        frequency=args.tone_frequency,
        sample_rate=args.sample_rate,
        amplitude=args.tone_amplitude,
    )
    return ffmpeg_pcm_to_aac_adts(pcm, sample_rate=args.sample_rate) if args.codec in {"aac-adts", "aac-raw"} else pcm


async def send_once(args: argparse.Namespace) -> int:
    device = load_device(Path(args.device_json), args.device_index)
    serial = args.serial or device["Sn"]
    password = args.password or device["Pwd"]
    audio = read_audio(args)

    ptcp = await p2p_handshake(serial, relay_mode=True, dtype=args.type, debug=args.debug)
    tunnel = DHP2PTunnel(ptcp, args.remote_port, debug=args.debug)
    server_task = asyncio.create_task(tunnel.start(args.host, args.port))
    await asyncio.sleep(args.startup_delay)

    try:
        return await asyncio.to_thread(run_visualtalk, args, password, audio)
    finally:
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass


def run_visualtalk(args: argparse.Namespace, password: str, audio: bytes) -> int:
    client = VisualTalkClient(
        args.host,
        args.port,
        username=args.username,
        password=password,
        nonce=None,
        created=None,
        password_digest_override=None,
        lightweight_digest=None,
        timeout=args.timeout,
    )
    try:
        responses = client.start_talk(args)
        for index, response in enumerate(responses):
            print(f"Cseq {index}: {response.status_line} body={len(response.body)}")
            if args.dump_body and response.body:
                print(f"--- Cseq {index} body ---")
                print(response.body.decode("latin1", errors="replace"))
                print("--- end body ---")
            if response.code >= 400:
                return 1
        if args.open_only:
            return 0
        frame_count = client.send_audio(audio, args)
        print(f"sent {frame_count} interleaved DHAV frames")
    finally:
        client.close()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-json", default=str(DEFAULT_DEVICE_JSON))
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--serial", help="override serial from device json")
    parser.add_argument("--password", help="override camera password from device json")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18086)
    parser.add_argument("--remote-port", type=int, default=8086)
    parser.add_argument("--type", type=int, default=0, help="DHP2P auth type; type 0 is the proven relay route")
    parser.add_argument("--audio", help="audio file to convert with ffmpeg")
    parser.add_argument("--raw-s16le", action="store_true", help="treat --audio as mono signed 16-bit little-endian PCM")
    parser.add_argument("--text", help="macOS say TTS text to send")
    parser.add_argument("--tone-seconds", type=float, default=1.0)
    parser.add_argument("--tone-frequency", type=float, default=880.0)
    parser.add_argument("--tone-amplitude", type=float, default=0.25)
    parser.add_argument("--channel", type=int, default=1)
    parser.add_argument("--subtype", type=int, default=0)
    parser.add_argument("--encrypt", type=int, default=3)
    parser.add_argument("--track1", type=int, default=0)
    parser.add_argument("--track2", type=int, default=0)
    parser.add_argument("--talk-track", type=int, default=64)
    parser.add_argument("--media-track", type=int, default=5)
    parser.add_argument("--sdp")
    parser.add_argument("--open-only", action="store_true")
    parser.add_argument("--dump-body", action="store_true", help="print DHHTTP response bodies such as returned SDP")
    parser.add_argument("--codec", choices=("mulaw", "alaw", "aac-adts", "aac-raw", "copy"), default="aac-adts")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--frame-ms", type=int)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--startup-delay", type=float, default=0.2)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.frame_ms is None:
        args.frame_ms = 64 if args.codec in {"aac-adts", "aac-raw"} else 20
    return asyncio.run(send_once(args))


if __name__ == "__main__":
    raise SystemExit(main())
