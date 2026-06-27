#!/usr/bin/env python3
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import imou_login


ROOT = Path(__file__).resolve().parents[1]
APPDATA = ROOT / "artifacts/imou_apk/appdata"


def mmkv_text() -> str:
    return (APPDATA / "files/mmkv/dh_data").read_bytes().decode("utf-8", errors="ignore")


def mmkv_value_after(marker_text: str, marker: str, start_at: int = 0) -> str:
    idx = marker_text.find(marker, start_at)
    if idx < 0:
        raise SystemExit(f"missing {marker}")
    tail = marker_text[idx + len(marker) :]
    if tail.startswith("BA"):
        tail = tail[2:]
    value = tail.split("\n", 1)[0]
    return re.sub(r"[^A-Za-z0-9+/=]", "", value)


def extract_json_after(text: str, marker: str) -> dict:
    idx = text.find(marker)
    if idx < 0:
        return {}
    start = text.find("{", idx)
    depth = 0
    in_str = False
    esc = False
    for pos in range(start, len(text)):
        ch = text[pos]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : pos + 1])
    return {}


def aes_key(username: str) -> bytes:
    seed = f"M223AACCWFFjswn@{username}KwSWFccdsss991w#".encode()
    key = bytearray(hashlib.sha1(seed).hexdigest()[:32].encode())
    for i in range(len(key)):
        for x in b"FECOI()*&<MNCXZPKL":
            key[i] ^= x
    return bytes(key)


def decrypt_cached_password(cipher_b64: str, username: str) -> str:
    proc = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-d",
            "-K",
            aes_key(username).hex(),
            "-iv",
            "00" * 16,
            "-nosalt",
        ],
        input=base64.b64decode(cipher_b64),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return proc.stdout.decode()


def build_client_ua(text: str, user: dict) -> str:
    ttid = re.search(r"DEVICE_TTID.?\s*([0-9a-f]{32})", text)
    push = re.search(r"MQTT_PUSH_ID.?\s*Base([A-Za-z0-9]+)", text)
    ua = OrderedDict(
        [
            ("country", user.get("country", "VN")),
            ("userLabel", str(user.get("labelTwo", 5))),
            ("terminalBrand", os.environ.get("IMOU_TERMINAL_BRAND", "samsung")),
            ("project", "Base"),
            ("clientOS", "Android"),
            ("language", os.environ.get("IMOU_LANGUAGE", "vi_VN")),
            ("terminalId", os.environ.get("IMOU_TERMINAL_ID", push.group(1) if push else "bafd56c41d8df3d1")),
            ("clientVersion", os.environ.get("IMOU_CLIENT_VERSION", "V10.0.6")),
            ("ttid", os.environ.get("IMOU_TTID", ttid.group(1) if ttid else "")),
            ("terminalModel", os.environ.get("IMOU_TERMINAL_MODEL", "SM-G998B")),
            ("terminalName", os.environ.get("IMOU_TERMINAL_NAME", "samsung SM-G998B")),
            ("clientType", "phone"),
            ("clientProtocolVersion", os.environ.get("IMOU_PROTOCOL_VERSION", "V9.7.2")),
            ("timezoneOffset", os.environ.get("IMOU_TIMEZONE_OFFSET", "25200")),
            ("clientOV", os.environ.get("IMOU_CLIENT_OV", "Android 15")),
            ("appid", "easy4ipbaseapp"),
            ("darkMode", os.environ.get("IMOU_DARK_MODE", "light")),
        ]
    )
    raw = json.dumps(ua, separators=(",", ":"), ensure_ascii=False)
    return base64.b64encode(raw.encode()).decode()


def summarize_devices(device_list: list[dict]) -> list[dict]:
    out = []
    for dev in device_list:
        channels = []
        for ch in dev.get("channelList") or []:
            channels.append(
                {
                    "channelId": ch.get("channelId"),
                    "channelName": ch.get("channelName"),
                    "familyId": ch.get("familyId"),
                    "familyName": ch.get("familyName"),
                    "status": ch.get("status"),
                    "cameraStatus": ch.get("cameraStatus"),
                    "productId": ch.get("productId"),
                }
            )
        out.append(
            {
                "deviceId": dev.get("deviceId"),
                "deviceName": dev.get("deviceName"),
                "productModel": dev.get("productModel"),
                "catalog": dev.get("catalog"),
                "status": dev.get("status"),
                "role": dev.get("role"),
                "channels": channels,
            }
        )
    return out


def main() -> int:
    text = mmkv_text()
    user = extract_json_after(text, "USER_DATA")
    username_match = re.search(r"USER_NAME_HELP..(token/[A-Za-z0-9]+)", text)
    if not username_match:
        raise SystemExit("missing USER_NAME_HELP")
    saved_username = username_match.group(1)
    cipher_b64 = mmkv_value_after(text, "USER_PSW_HELP", username_match.end())
    plain = decrypt_cached_password(cipher_b64, saved_username)
    key = hashlib.md5(plain.encode()).hexdigest()
    username = "uuid\\" + saved_username.replace("token/", "")
    imou_login.HOST = user.get("entryUrlV2", user.get("entryUrl", "https://app-sg1-v3.easy4ipcloud.com:443")).replace("https://", "").replace("http://", "")
    os.environ["IMOU_CLIENT_UA_B64"] = build_client_ua(text, user)

    families = imou_login.post("family.manager.UserFamilyGet", "191204", {"defaultFamilyNameRule": True}, username, key).get("data", {})
    devices = imou_login.post(
        "device.list.BasicList",
        "191204",
        {"familyId": "-1", "transferStr": "", "offset": 0, "limit": 128, "roomId": "-1"},
        username,
        key,
    ).get("data", {})
    result = {
        "userId": user.get("userId"),
        "families": families.get("families", []),
        "devices": summarize_devices(devices.get("deviceList", [])),
        "hasNextPage": devices.get("hasNextPage"),
        "transferStr": devices.get("transferStr"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
