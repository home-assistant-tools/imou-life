#!/usr/bin/env python3
"""Pure-Python helpers for Imou/Dahua DHAV talk audio frames.

This replaces the small `LCDHAVAudioPacker` / G.711 part of libCommonSDK for
standalone bridge experiments. It does not open the P2P session; it only builds
audio payloads that can be written into an already-enabled DHHTTP talk stream.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from collections.abc import Iterable


SAMPLE_RATE_CODES = {
    8000: 2,
    11025: 3,
    16000: 4,
    20000: 5,
    22050: 6,
    32000: 7,
    44100: 8,
    48000: 9,
}

DHAV_AUDIO_INFO = b"\x83\x01\x1a"
DHAV_OVERHEAD = 36
DEFAULT_TALK_TRACK_ID = 5


def sample_rate_code(sample_rate: int) -> int:
    try:
        return SAMPLE_RATE_CODES[sample_rate]
    except KeyError as exc:
        supported = ", ".join(str(x) for x in sorted(SAMPLE_RATE_CODES))
        raise ValueError(f"unsupported DHAV sample rate {sample_rate}; supported: {supported}") from exc


def dhav_checksum(header: bytes | bytearray) -> int:
    """Checksum used by LCDHAVAudioPacker.

    The captured frame in docs has checksum 0x0e for the first 23 bytes:
    magic through the constant byte at offset 0x16, excluding the checksum byte.
    """

    if len(header) < 0x17:
        raise ValueError("DHAV header must include bytes 0x00..0x16")
    return sum(header[:0x17]) & 0xFF


def pack_dhav_audio(
    payload: bytes,
    *,
    seq: int = 0,
    sample_rate: int = 16000,
    timestamp: int | None = None,
    tick: int | None = None,
    frame_type: int = 0xF0,
) -> bytes:
    """Pack one audio payload as a DHAV audio frame.

    `payload` is codec-specific bytes, typically G.711u/G.711a or AAC depending
    on the talk track negotiated by the camera. The codec itself is not encoded
    in the DHAV header.
    """

    if timestamp is None:
        timestamp = int(time.time())
    if tick is None:
        tick = int(time.monotonic() * 1000)

    total_len = len(payload) + DHAV_OVERHEAD
    header = bytearray(28)
    struct.pack_into("<4sB3xII", header, 0, b"DHAV", frame_type & 0xFF, seq & 0xFFFFFFFF, total_len)
    struct.pack_into("<I", header, 0x10, timestamp & 0xFFFFFFFF)
    struct.pack_into("<H", header, 0x14, tick & 0xFFFF)
    header[0x16] = 0x04
    header[0x17] = dhav_checksum(header)
    header[0x18:0x1B] = DHAV_AUDIO_INFO
    header[0x1B] = sample_rate_code(sample_rate)

    return bytes(header) + payload + b"dhav" + struct.pack("<I", total_len)


def pack_dhhttp_interleaved(frame: bytes, *, track_id: int = DEFAULT_TALK_TRACK_ID) -> bytes:
    """Wrap a DHAV frame for DHHTTP interleaved media.

    Imou visual talk uses trackID=5 for talk-send, so the interleave channel is
    `2 * trackID` (0x0a). Length is a 4-byte big-endian value in this DHHTTP
    variant.
    """

    channel = track_id * 2
    if not 0 <= channel <= 0xFF:
        raise ValueError("track_id produces an invalid one-byte interleave channel")
    return b"$" + bytes([channel]) + struct.pack(">I", len(frame)) + frame


def linear16_to_mulaw_sample(sample: int) -> int:
    """Convert one signed 16-bit PCM sample to ITU G.711 mu-law."""

    bias = 0x84
    clip = 32635
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    if sample > clip:
        sample = clip
    sample += bias

    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        mask >>= 1
        exponent -= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def linear16_to_alaw_sample(sample: int) -> int:
    """Convert one signed 16-bit PCM sample to ITU G.711 A-law."""

    if sample >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        sample = -sample - 8
    if sample < 0:
        sample = 0
    if sample > 0x7FFF:
        sample = 0x7FFF

    if sample < 256:
        compressed = sample >> 4
    else:
        exponent = 7
        exp_mask = 0x4000
        while exponent > 0 and not (sample & exp_mask):
            exp_mask >>= 1
            exponent -= 1
        compressed = (exponent << 4) | ((sample >> (exponent + 3)) & 0x0F)
    return compressed ^ mask


def pcm_s16le_to_g711(pcm: bytes, codec: str) -> bytes:
    if len(pcm) % 2:
        raise ValueError("PCM input length must be even for signed 16-bit LE")
    if codec == "mulaw":
        convert = linear16_to_mulaw_sample
    elif codec == "alaw":
        convert = linear16_to_alaw_sample
    else:
        raise ValueError("codec must be mulaw or alaw")

    out = bytearray(len(pcm) // 2)
    for i in range(0, len(pcm), 2):
        sample = struct.unpack_from("<h", pcm, i)[0]
        out[i // 2] = convert(sample)
    return bytes(out)


def chunks(data: bytes, size: int) -> Iterable[bytes]:
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]


def adts_frames(data: bytes, *, strip_header: bool = False) -> Iterable[bytes]:
    offset = 0
    while offset < len(data):
        if offset + 7 > len(data) or data[offset] != 0xFF or (data[offset + 1] & 0xF0) != 0xF0:
            raise ValueError(f"invalid ADTS sync word at offset {offset}")
        protection_absent = data[offset + 1] & 0x01
        header_len = 7 if protection_absent else 9
        frame_len = ((data[offset + 3] & 0x03) << 11) | (data[offset + 4] << 3) | ((data[offset + 5] & 0xE0) >> 5)
        if frame_len < header_len or offset + frame_len > len(data):
            raise ValueError(f"invalid ADTS frame length {frame_len} at offset {offset}")
        frame = data[offset : offset + frame_len]
        yield frame[header_len:] if strip_header else frame
        offset += frame_len


def build_frames(
    data: bytes,
    *,
    codec: str,
    sample_rate: int,
    frame_ms: int,
    interleaved: bool,
    track_id: int,
) -> Iterable[bytes]:
    samples_per_frame = sample_rate * frame_ms // 1000
    if samples_per_frame <= 0:
        raise ValueError("frame duration is too small")

    if codec in {"mulaw", "alaw"}:
        pcm_bytes_per_frame = samples_per_frame * 2
        payload_iter = (
            pcm_s16le_to_g711(frame.ljust(pcm_bytes_per_frame, b"\x00"), codec)
            for frame in chunks(data, pcm_bytes_per_frame)
        )
    elif codec == "aac-adts":
        payload_iter = adts_frames(data)
    elif codec == "aac-raw":
        payload_iter = adts_frames(data, strip_header=True)
    elif codec == "copy":
        payload_iter = chunks(data, samples_per_frame)
    else:
        raise ValueError("codec must be mulaw, alaw, aac-adts, aac-raw, or copy")

    seq = 0
    tick = int(time.monotonic() * 1000)
    timestamp = int(time.time())
    for payload in payload_iter:
        frame = pack_dhav_audio(payload, seq=seq, sample_rate=sample_rate, timestamp=timestamp, tick=tick)
        if interleaved:
            frame = pack_dhhttp_interleaved(frame, track_id=track_id)
        yield frame
        seq += 1
        tick += frame_ms


def self_test() -> None:
    captured_header = bytes.fromhex(
        "44484156f0000000e6070000b0010000b969b769aa6d040e83011a04"
    )
    assert dhav_checksum(captured_header) == 0x0E
    assert sample_rate_code(16000) == 4
    assert linear16_to_mulaw_sample(0) == 0xFF
    assert linear16_to_alaw_sample(0) == 0xD5

    frame = pack_dhav_audio(b"\xFF" * 320, seq=1, sample_rate=16000, timestamp=0x11223344, tick=0x5566)
    assert frame[:4] == b"DHAV"
    assert frame[0x1B] == 4
    assert frame[-8:-4] == b"dhav"
    assert struct.unpack_from("<I", frame, 0x0C)[0] == 356
    assert struct.unpack_from("<I", frame, len(frame) - 4)[0] == 356

    wrapped = pack_dhhttp_interleaved(frame)
    assert wrapped[:2] == b"$\x0a"
    assert struct.unpack_from(">I", wrapped, 2)[0] == len(frame)
    adts = bytes.fromhex("fff15040035ffc") + b"\x00" * 19
    assert list(adts_frames(adts)) == [adts]
    assert list(adts_frames(adts, strip_header=True)) == [b"\x00" * 19]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", help="input audio bytes; stdin if omitted")
    parser.add_argument("--codec", choices=("mulaw", "alaw", "aac-adts", "aac-raw", "copy"), default="mulaw")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--frame-ms", type=int, default=20)
    parser.add_argument("--interleaved", action="store_true", help="wrap frames as $ + channel + len + DHAV")
    parser.add_argument("--track-id", type=int, default=DEFAULT_TALK_TRACK_ID)
    parser.add_argument("--out", help="output file; stdout if omitted")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        print("self-test ok", file=sys.stderr)
        return 0

    data = sys.stdin.buffer.read() if not args.input or args.input == "-" else open(args.input, "rb").read()
    out = sys.stdout.buffer if not args.out else open(args.out, "wb")
    try:
        for frame in build_frames(
            data,
            codec=args.codec,
            sample_rate=args.sample_rate,
            frame_ms=args.frame_ms,
            interleaved=args.interleaved,
            track_id=args.track_id,
        ):
            out.write(frame)
    finally:
        if out is not sys.stdout.buffer:
            out.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
