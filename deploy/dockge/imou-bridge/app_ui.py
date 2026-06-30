#!/usr/bin/env python3
"""Imou bridge configuration UI.

React SPA + Flask API for managing Imou cloud accounts, discovered cameras, and
bridge/ONVIF selection. Cloud passwords are stored locally only when the user
chooses to save/update them so account reloads can refresh camera metadata.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import random
import re
import string
import subprocess
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request

from flask import Flask, jsonify, request as flask_request, Response, send_from_directory


HOST = os.environ.get("IMOU_CLOUD_HOST", "app-sg1-v3.easy4ipcloud.com")
DISCOVERY_HOST = os.environ.get("IMOU_DISCOVERY_HOST", "app-v3.easy4ipcloud.com")
APIVER = os.environ.get("IMOU_APIVER", "191204")
GET_TOKEN_APIVER = os.environ.get("IMOU_GET_TOKEN_APIVER", APIVER)
GEETEST4_APIVER = os.environ.get("IMOU_GEETEST4_APIVER", "152485")
LOGIN_APIVER = os.environ.get("IMOU_LOGIN_APIVER", APIVER)
OPTIONS_PATH = Path(os.environ.get("IMOU_BRIDGE_OPTIONS", "/data/options.json"))
STATUS_PATH = Path(os.environ.get("IMOU_BRIDGE_STATUS", "/data/status.json"))
LISTEN_HOST = os.environ.get("IMOU_UI_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("IMOU_UI_PORT", os.environ.get("INGRESS_PORT", "8099")))
APP_DIR = Path(__file__).resolve().parent
GEELAB_ASSET_DIR = APP_DIR / "geelab_assets"

OPTIONS_LOCK = threading.Lock()
PENDING: dict[str, dict] = {}

app = Flask(__name__)


def slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower()).strip("_") or "camera"


def b64_md5(s: str) -> str:
    return base64.b64encode(hashlib.md5(s.encode()).digest()).decode()


def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def account_password_key(password: str) -> str:
    return md5_hex(md5_hex(password))


def session_key(token: str) -> str:
    return md5_hex(token)


def nonce(n: int = 32) -> str:
    suffix = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))
    return f"{int(time.time() * 1000)}{suffix}"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def client_ua() -> str:
    install_seed = os.environ.get(
        "IMOU_INSTALL_SEED",
        f"{HOST}:{os.uname().nodename}:{OPTIONS_PATH.parent}",
    )
    install_hash = hashlib.sha256(install_seed.encode()).hexdigest()
    ua = {
        "country": os.environ.get("IMOU_COUNTRY", "VN"),
        "userLabel": os.environ.get("IMOU_USER_LABEL", "1"),
        "terminalBrand": os.environ.get("IMOU_TERMINAL_BRAND", "samsung"),
        "project": os.environ.get("IMOU_PROJECT", "Base"),
        "clientOS": "Android",
        "language": os.environ.get("IMOU_LANGUAGE", "vi_VN"),
        "terminalId": os.environ.get("IMOU_TERMINAL_ID") or install_hash[:16],
        "clientVersion": os.environ.get("IMOU_CLIENT_VERSION", "V10.0.6"),
        "ttid": os.environ.get("IMOU_TTID") or install_hash[16:48],
        "terminalModel": os.environ.get("IMOU_TERMINAL_MODEL", "SM-G998B"),
        "terminalName": os.environ.get("IMOU_TERMINAL_NAME", "samsung SM-G998B"),
        "clientType": "phone",
        "clientProtocolVersion": os.environ.get("IMOU_CLIENT_PROTOCOL_VERSION", "V9.7.2"),
        "timezoneOffset": os.environ.get("IMOU_TIMEZONE_OFFSET", "25200"),
        "clientOV": os.environ.get("IMOU_CLIENT_OV", "Android 15"),
        "appid": os.environ.get("IMOU_APPID", "easy4ipbaseapp"),
        "darkMode": os.environ.get("IMOU_DARK_MODE", "light"),
    }
    return base64.b64encode(json.dumps(ua, separators=(",", ":")).encode()).decode()


def host_from_entry_url(entry_url: str | None) -> str:
    if not entry_url:
        return HOST
    parsed = parse.urlparse(entry_url if "://" in entry_url else "https://" + entry_url)
    return parsed.netloc or parsed.path or HOST


def pcs_ok(resp: dict) -> bool:
    return resp.get("code") in (0, 10000)


def is_captcha_required(resp: dict) -> bool:
    return resp.get("code") in (12110, 12112, 12114, 2033)


def log_cloud_response(stage: str, resp: dict) -> None:
    data = resp.get("data") if isinstance(resp, dict) else None
    data = data if isinstance(data, dict) else {}
    cap = data.get("captchaData") if isinstance(data.get("captchaData"), dict) else {}
    summary = {
        "code": resp.get("code") if isinstance(resp, dict) else None,
        "desc": resp.get("desc") if isinstance(resp, dict) else None,
        "msg": resp.get("msg") if isinstance(resp, dict) else None,
        "data_keys": sorted(data.keys()),
        "captcha_keys": sorted(cap.keys()),
        "captchaMode": data.get("captchaMode"),
        "has_captcha_id": bool(cap.get("captchaId")),
        "has_captcha_server": bool(cap.get("captchaServer")),
        "has_verify_token": bool(cap.get("verifyToken")),
    }
    print(f"[app-ui] {stage} cloud response {json.dumps(summary, separators=(',', ':'))}", flush=True)


def has_login_token(data: dict) -> bool:
    return all(data.get(k) for k in ("username", "token", "sessionId"))


def captcha_response_payload(token_data: dict, pending_id: str) -> dict:
    cap = token_data.get("captchaData") or {}
    payload = {
        "captcha_required": True,
        "pending_id": pending_id,
        "captcha_id": cap.get("captchaId", ""),
        "captcha_server": cap.get("captchaServer", ""),
    }
    if cap.get("image"):
        payload.update({"type": "image", "image": cap.get("image", "")})
    elif token_data.get("captchaMode") == "gt4" or cap.get("captchaServer") or cap.get("new_captcha"):
        payload.update({"type": "geetest4"})
    else:
        payload.update({"type": "unsupported", "raw_keys": list(token_data.keys())})
    return payload


def pcs_post(
    api: str,
    data: dict,
    username: str,
    key: str,
    session_id: str | None = None,
    apiver: str | None = None,
    host: str | None = None,
    content_type: str = "application/json",
) -> dict:
    api_version = apiver or APIVER
    path = "/pcs/v1/" + api
    body = json.dumps({"data": data}, separators=(",", ":"), ensure_ascii=False)
    content_md5 = b64_md5(body)
    date = utc_now()
    n = nonce()
    cua = client_ua()
    canonical = (
        f"POST\n{path}\n{content_md5}\n{content_type}\n"
        f"x-pcs-apiver:{api_version}\nx-pcs-client-ua:{cua}\nx-pcs-date:{date}\n"
        f"x-pcs-nonce:{n}\n"
    )
    if session_id:
        canonical += f"x-pcs-session-id:{session_id}\n"
    canonical += f"x-pcs-username:{username}\n"
    sig = base64.b64encode(hmac.new(key.encode(), canonical.encode(), hashlib.sha256).digest()).decode()
    headers = {
        "Content-Type": content_type,
        "User-Agent": os.environ.get("IMOU_USER_AGENT", "okhttp/4.9.2"),
        "Content-MD5": content_md5,
        "x-pcs-username": username,
        "x-pcs-apiver": api_version,
        "x-pcs-nonce": n,
        "x-pcs-date": date,
        "x-pcs-signature": sig,
        "x-pcs-client-ua": cua,
        "x-pcs-request-id": "",
    }
    if session_id:
        headers["x-pcs-session-id"] = session_id
    req_host = host or HOST
    req = request.Request("https://" + req_host + path, data=body.encode(), headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"code": exc.code, "msg": raw}


def get_token(email: str, password: str) -> dict:
    return pcs_post(
        "user.account.GetToken",
        {"gpsInfo": {"latitude": 0.0, "longitude": 0.0}},
        "account\\" + email,
        account_password_key(password),
        apiver=GET_TOKEN_APIVER,
        host=DISCOVERY_HOST,
        content_type="application/json; charset=utf-8",
    )


def verify_captcha(email: str, password: str, challenge: dict, code: str) -> dict:
    return pcs_post(
        "common.validcode.CheckImageValidCode",
        {
            "codeId": challenge["codeId"],
            "code": code,
            "usage": "Login",
            "captchaMetaData": "",
            "captchaId": challenge["captchaId"],
            "verifyToken": challenge["verifyToken"],
        },
        "account\\" + email,
        account_password_key(password),
        host=DISCOVERY_HOST,
        content_type="application/json; charset=utf-8",
    )


def verify_geetest4(email: str, password: str, challenge: dict, result: dict) -> dict:
    payload = {
        "account": email,
        "captchaId": result.get("captcha_id") or result.get("captchaId") or challenge["captchaId"],
        "captchaMetaData": "",
        "captchaOutput": result.get("captcha_output") or result.get("captchaOutput") or "",
        "genTime": str(result.get("gen_time") or result.get("genTime") or ""),
        "lotNumber": result.get("lot_number") or result.get("lotNumber") or "",
        "passToken": result.get("pass_token") or result.get("passToken") or "",
        "usage": challenge.get("usage", "Login"),
        "verifyToken": challenge.get("verifyToken", ""),
    }
    lca_content_type = "application/json; charset=utf-8"
    variants = [
        ("lca-discovery-191204", payload, "account\\" + email, APIVER, DISCOVERY_HOST, lca_content_type),
        ("lca-region-191204", payload, "account\\" + email, APIVER, HOST, lca_content_type),
        ("hsview-discovery-152485", payload, "account\\" + email, GEETEST4_APIVER, DISCOVERY_HOST, "application/json"),
        ("lca-account-prefixed-discovery-191204", {**payload, "account": "account\\" + email}, "account\\" + email, APIVER, DISCOVERY_HOST, lca_content_type),
    ]
    last = {}
    attempts = []
    for name, payload, username, apiver, host, content_type in variants:
        resp = pcs_post(
            "common.validcode.CheckGeeTest4",
            payload,
            username,
            account_password_key(password),
            apiver=apiver,
            host=host,
            content_type=content_type,
        )
        resp["_variant"] = name
        attempts.append({"variant": name, "code": resp.get("code"), "desc": resp.get("desc") or resp.get("msg")})
        if pcs_ok(resp):
            resp["_attempts"] = attempts
            return resp
        last = resp
    last["_attempts"] = attempts
    return last


def do_login(token_data: dict) -> dict:
    return pcs_post(
        "user.account.Login",
        {"timezoneOffset": int(os.environ.get("IMOU_LOGIN_TIMEZONE_OFFSET", "25200"))},
        "uuid\\" + token_data["username"],
        session_key(token_data["token"]),
        token_data["sessionId"],
        apiver=LOGIN_APIVER,
        host=host_from_entry_url(token_data.get("entryUrlV2") or token_data.get("entryUrl")),
        content_type="application/json; charset=utf-8",
    )


def list_devices(token_data: dict) -> dict:
    return pcs_post(
        "device.list.BasicList",
        {"familyId": "-1", "transferStr": "", "offset": 0, "limit": 128, "roomId": "-1"},
        "uuid\\" + token_data["username"],
        session_key(token_data["token"]),
        token_data["sessionId"],
        host=host_from_entry_url(token_data.get("entryUrlV2") or token_data.get("entryUrl")),
    )


def default_options() -> dict:
    return {
        "log_level": "info",
        "discovery_ui": True,
        "go2rtc": {"rtsp_port": 8554, "api_port": 1984, "webrtc_port": 8555},
        "bridge": {
            "engine": "python",
            "python_bridge": "/opt/imou-p2p-bridge/imou_dhp2p.py",
            "restart_seconds": 5,
            "base_port": 8600,
            "verbose": False,
            "warm_streams": True,
            "warm_all_streams": False,
            "warm_restart_seconds": 30,
        },
        "accounts": [],
        "cameras": [],
    }


def load_options() -> dict:
    if not OPTIONS_PATH.exists():
        return default_options()
    data = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    base = default_options()
    for key, value in base.items():
        data.setdefault(key, value)
    data.setdefault("accounts", [])
    data.setdefault("cameras", [])
    return data


def save_options(options: dict) -> None:
    OPTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OPTIONS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(options, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(OPTIONS_PATH)


def load_status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"cameras": {}}


def camera_config_status(cam: dict, runtime: dict) -> dict:
    is_lan = bool(cam.get("lan_detected") and cam.get("local_ip"))
    enabled = bool(cam.get("enabled"))
    relay = bool(cam.get("relay"))
    password = str(cam.get("password") or "")
    if enabled and not is_lan and not password:
        return {
            "state": "error",
            "message": "Camera password is required before enabling remote P2P/relay streaming.",
        }
    if relay and not password:
        return {
            "state": "error",
            "message": "Camera password is required before enabling relay.",
        }
    cameras = runtime.get("cameras") if isinstance(runtime, dict) else {}
    cameras = cameras if isinstance(cameras, dict) else {}
    for key in (cam.get("serial"), cam.get("bridge_name"), slugify(str(cam.get("bridge_name") or cam.get("device_name") or cam.get("serial") or ""))):
        if key and isinstance(cameras.get(key), dict):
            return cameras[key]
    if enabled:
        return {"state": "configured", "message": "Waiting for supervisor status."}
    return {"state": "disabled", "message": "Bridge is off."}


def public_options(options: dict) -> dict:
    out = deepcopy(options)
    runtime = load_status()
    for account in out.get("accounts", []):
        password = account.pop("password", "")
        account["has_password"] = bool(password)
        for cam in account.get("cameras", []):
            cam["bridge_status"] = camera_config_status(cam, runtime)
    for cam in out.get("cameras", []):
        cam["bridge_status"] = camera_config_status(cam, runtime)
    return out


def private_ip(value) -> str:
    if value is None:
        return ""
    try:
        ip = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return ""
    return str(ip) if ip.is_private else ""


def extract_local_ip(item: dict) -> str:
    preferred = (
        "localIp",
        "localIP",
        "local_ip",
        "ipAddress",
        "ip",
        "lanIp",
        "lanIP",
        "deviceIp",
        "deviceIP",
    )
    for key in preferred:
        found = private_ip(item.get(key))
        if found:
            return found
    for value in item.values():
        if isinstance(value, dict):
            found = extract_local_ip(value)
            if found:
                return found
    return ""


def normalize_device(item: dict) -> dict:
    serial = str(item.get("deviceId") or item.get("devSn") or item.get("serial") or "")
    device_name = str(item.get("deviceName") or item.get("name") or serial)
    channels = item.get("channelList") if isinstance(item.get("channelList"), list) else []
    first_channel = channels[0] if channels and isinstance(channels[0], dict) else {}
    local_ip = extract_local_ip(item)
    thumbnail_url = extract_thumbnail_url(item)
    return {
        "id": serial,
        "serial": serial,
        "device_name": device_name,
        "bridge_name": slugify(device_name),
        "model": str(item.get("productModel") or item.get("model") or ""),
        "role": str(item.get("role") or ""),
        "family_id": str(first_channel.get("familyId") or item.get("familyId") or ""),
        "family_name": str(first_channel.get("familyName") or item.get("familyName") or ""),
        "room_id": str(first_channel.get("roomId") or item.get("roomId") or ""),
        "room_name": str(first_channel.get("roomName") or item.get("roomName") or ""),
        "thumbnail_url": str(thumbnail_url),
        "local_ip": local_ip,
        "lan_detected": bool(local_ip),
        "enabled": False,
        "username": "admin",
        "password": "",
        "streams": infer_streams(item),
        "relay": False,
        "ptz": False,
        "talk": True,
        "engine": "",
        "warm": True,
    }


def extract_thumbnail_url(item: dict) -> str:
    """Find a camera thumbnail URL in the many shapes returned by Imou APIs."""
    keys = {
        "thumbnailurl", "thumbnail", "thumburl", "thumb", "imageurl", "image",
        "picurl", "pictureurl", "photourl", "logourl", "coverurl", "snapshoturl",
        "snapurl", "devicepicurl", "deviceimageurl", "devicepic", "deviceimage",
        "channelpicurl", "channelimageurl",
    }

    def usable(value) -> str:
        if not isinstance(value, str):
            return ""
        value = value.strip()
        if not value:
            return ""
        lower = value.lower()
        if lower.startswith(("http://", "https://", "data:image/")):
            return value
        if value.startswith("//"):
            return "https:" + value
        return ""

    def walk(value) -> str:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).replace("_", "").lower() in keys:
                    found = usable(child)
                    if found:
                        return found
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return ""

    return walk(item)


def infer_streams(item: dict) -> list[dict]:
    """Infer camera channel/subtype profiles from cloud metadata.

    Imou returns different keys across camera models. Prefer explicit channel
    counts when present; otherwise expose the common Dahua/Imou main+sub pair.
    """
    def int_value(*keys: str, default: int = 0) -> int:
        for key in keys:
            value = item.get(key)
            try:
                if value not in (None, ""):
                    return max(0, int(value))
            except (TypeError, ValueError):
                continue
        return default

    channels = int_value("channelNum", "channelCount", "channels", "videoChannelNum", "videoChannelCount", default=1)
    channels = max(1, min(channels, 64))
    raw_subtypes = item.get("subtypes") or item.get("streamTypes") or item.get("streamTypeList")
    subtypes: list[int] = []
    if isinstance(raw_subtypes, list):
        for value in raw_subtypes:
            try:
                subtypes.append(int(value))
            except (TypeError, ValueError):
                pass
    if not subtypes:
        subtypes = [0, 1]
    subtypes = sorted({s for s in subtypes if 0 <= s <= 3})
    labels = {0: "Main", 1: "Sub", 2: "Third", 3: "Fourth"}
    streams = []
    for channel in range(1, channels + 1):
        for subtype in subtypes:
            streams.append({
                "id": f"ch{channel}_sub{subtype}",
                "label": f"Ch {channel} {labels.get(subtype, f'Sub {subtype}')}",
                "channel": channel,
                "subtype": subtype,
                "enabled": True,
            })
    return streams


def build_account(email: str, label: str, login: dict, devices: dict, password: str = "", account_id: str | None = None) -> dict:
    login_data = login.get("data") or {}
    dev_data = devices.get("data") or {}
    items = dev_data.get("deviceList") or []
    return {
        "id": account_id or uuid.uuid4().hex[:12],
        "email": email,
        "label": label or login_data.get("nickname") or email,
        "nickname": login_data.get("nickname", ""),
        "password": password,
        "added_at": utc_now(),
        "cameras": [normalize_device(item) for item in items if item.get("deviceId")],
    }


def merge_account_refresh(existing: dict, refreshed: dict) -> dict:
    """Merge a fresh cloud camera list while preserving local bridge settings."""
    keep = ("enabled", "bridge_name", "username", "password", "relay", "ptz", "talk", "engine", "warm")
    existing_by_serial = {cam.get("serial"): cam for cam in existing.get("cameras", []) if cam.get("serial")}
    merged_cameras = []
    for cam in refreshed.get("cameras", []):
        old = existing_by_serial.get(cam.get("serial"))
        if old:
            for key in keep:
                if key in old:
                    cam[key] = old[key]
        merged_cameras.append(cam)
    refreshed["added_at"] = existing.get("added_at") or refreshed.get("added_at")
    return refreshed | {"cameras": merged_cameras}


def login_and_list(email: str, password: str) -> tuple[dict, dict, dict]:
    token = get_token(email, password)
    if is_captcha_required(token):
        raise ValueError("Imou requested captcha; use Add account login flow first, then try reload again")
    if not pcs_ok(token):
        raise ValueError(str(token.get("msg") or token))
    token_data = token.get("data") or {}
    if not has_login_token(token_data):
        raise ValueError(f"GetToken returned success without login token: {sorted(token_data.keys())}")
    login = do_login(token_data)
    if not pcs_ok(login):
        raise ValueError(str(login.get("msg") or login))
    devices = list_devices(token_data)
    if not pcs_ok(devices):
        raise ValueError(str(devices.get("msg") or devices))
    return token_data, login, devices


def sync_managed_cameras(options: dict) -> None:
    unmanaged = [cam for cam in options.get("cameras", []) if not cam.get("managed_by_account")]
    managed = []
    used_slugs: set[str] = {slugify(str(cam.get("name", ""))) for cam in unmanaged}
    for account in options.get("accounts", []):
        for cam in account.get("cameras", []):
            if not cam.get("enabled"):
                continue
            base_slug = slugify(str(cam.get("bridge_name") or cam.get("device_name") or cam.get("serial")))
            streams = ensure_camera_streams(cam)
            primary = next((s for s in streams if s.get("enabled", True)), streams[0])
            slug = base_slug
            n = 2
            while slug in used_slugs:
                slug = f"{base_slug}_{n}"
                n += 1
            used_slugs.add(slug)
            managed.append(
                {
                    "name": slug,
                    "display_name": cam.get("bridge_name") or cam.get("device_name") or slug,
                    "mode": "lan" if cam.get("lan_detected") and cam.get("local_ip") else "p2p",
                    "serial": cam.get("serial", ""),
                    "host": cam.get("local_ip", ""),
                    "username": cam.get("username", "admin"),
                    "password": cam.get("password", ""),
                    "channel": int(primary.get("channel", 1) or 1),
                    "subtype": int(primary.get("subtype", 0) or 0),
                    "streams": streams,
                    "relay": False if cam.get("lan_detected") else bool(cam.get("relay", False)),
                    "ptz": True,
                    "talk": True,
                    "engine": str(cam.get("engine", "") or ""),
                    "warm": bool(cam.get("warm", True)),
                    "managed_by_account": account.get("id"),
                    "source_device_name": cam.get("device_name", ""),
                    "source_family_name": cam.get("family_name", ""),
                }
            )
    options["cameras"] = unmanaged + managed


def ensure_camera_streams(cam: dict) -> list[dict]:
    streams = cam.get("streams")
    if not isinstance(streams, list) or not streams:
        streams = [
            {"id": "ch1_sub0", "label": "Ch 1 Main", "channel": int(cam.get("channel", 1) or 1), "subtype": int(cam.get("subtype", 0) or 0), "enabled": True},
            {"id": "ch1_sub1", "label": "Ch 1 Sub", "channel": int(cam.get("channel", 1) or 1), "subtype": 1, "enabled": True},
        ]
        cam["streams"] = streams
    normalized = []
    seen = set()
    for stream in streams:
        try:
            channel = int(stream.get("channel", 1) or 1)
            subtype = int(stream.get("subtype", 0) or 0)
        except (TypeError, ValueError):
            continue
        stream_id = str(stream.get("id") or f"ch{channel}_sub{subtype}")
        if stream_id in seen:
            continue
        seen.add(stream_id)
        normalized.append({
            "id": stream_id,
            "label": str(stream.get("label") or f"Ch {channel} Sub {subtype}"),
            "channel": channel,
            "subtype": subtype,
            "enabled": bool(stream.get("enabled", True)),
        })
    cam["streams"] = normalized
    return normalized


def migrate_stream_profiles(options: dict) -> bool:
    """Backfill stream metadata and force bridge-enabled features."""
    changed = False
    for account in options.get("accounts", []):
        for cam in account.get("cameras", []):
            before = json.dumps(cam.get("streams"), sort_keys=True, ensure_ascii=False)
            streams = ensure_camera_streams(cam)
            after = json.dumps(streams, sort_keys=True, ensure_ascii=False)
            if before != after:
                changed = True
            if cam.get("enabled") and (cam.get("ptz") is not True or cam.get("talk") is not True):
                cam["ptz"] = True
                cam["talk"] = True
                changed = True
    for cam in options.get("cameras", []):
        before = json.dumps(cam.get("streams"), sort_keys=True, ensure_ascii=False)
        streams = ensure_camera_streams(cam)
        after = json.dumps(streams, sort_keys=True, ensure_ascii=False)
        if before != after:
            changed = True
        if cam.get("enabled", True) and (cam.get("ptz") is not True or cam.get("talk") is not True):
            cam["ptz"] = True
            cam["talk"] = True
            changed = True
    if changed:
        sync_managed_cameras(options)
    return changed


def current_managed_slug(options: dict, account_id: str, serial: str) -> str:
    for cam in options.get("cameras", []):
        if cam.get("managed_by_account") == account_id and cam.get("serial") == serial:
            return str(cam.get("name") or "")
    return ""


def find_talk_camera(options: dict, serial: str) -> dict | None:
    """Find a camera with credentials for one-shot talk/TTS calls."""
    for cam in options.get("cameras", []):
        if cam.get("serial") == serial:
            return cam
    for account in options.get("accounts", []):
        for cam in account.get("cameras", []):
            if cam.get("serial") == serial:
                return cam
    return None


def google_tts_url(text: str, lang: str) -> str:
    query = parse.urlencode({
        "ie": "UTF-8",
        "client": "tw-ob",
        "tl": lang,
        "q": text,
    })
    return f"https://translate.google.com/translate_tts?{query}"


def download_tts(text: str, lang: str, path: Path) -> None:
    req = request.Request(
        google_tts_url(text, lang),
        headers={"User-Agent": "Mozilla/5.0 ImouBridge/1.0"},
    )
    with request.urlopen(req, timeout=30) as resp:
        path.write_bytes(resp.read())


def run_camera_tts(cam: dict, text: str, lang: str) -> dict:
    password = str(cam.get("password") or "")
    if not password:
        raise ValueError("Camera password is required for talk/TTS")
    username = str(cam.get("username") or "admin")
    serial = str(cam.get("serial") or "")
    if not serial:
        raise ValueError("Camera serial is missing")
    channel = int(cam.get("channel", 1) or 1)
    subtype = int(cam.get("subtype", 0) or 0)
    codec = str(cam.get("talk_output_codec") or cam.get("talk_codec") or "aac-adts")
    sample_rate = int(cam.get("talk_sample_rate", 16000) or 16000)
    with tempfile.TemporaryDirectory(prefix="imou-tts-") as tmp:
        audio_path = Path(tmp) / "tts.mp3"
        download_tts(text, lang, audio_path)
        cmd = [
            "python3",
            str(APP_DIR / "imou_pure_talk.py"),
            "--serial",
            serial,
            "--username",
            username,
            "--password",
            password,
            "--audio",
            str(audio_path),
            "--channel",
            str(channel),
            "--subtype",
            str(subtype),
            "--codec",
            codec,
            "--sample-rate",
            str(sample_rate),
        ]
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
    output = proc.stdout or ""
    sent_frames = None
    m = re.search(r"sent\s+(\d+)\s+interleaved", output)
    if m:
        sent_frames = int(m.group(1))
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "sent_frames": sent_frames,
        "output": output[-2000:],
    }


def error_response(message: str, status: int = 400, **extra):
    payload = {"error": message}
    payload.update(extra)
    return jsonify(payload), status


@app.get("/")
def index():
    return Response(
        INDEX_HTML,
        mimetype="text/html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/geelab/gl4-index.html")
def geelab_index():
    html = (GEELAB_ASSET_DIR / "gl4-index.html").read_text(encoding="utf-8")
    bridge = """
    <script>
      window.JSInterface = {
        gt4Notify: function(raw) {
          var message = raw;
          try { message = JSON.parse(raw); } catch (e) {}
          window.parent.postMessage({source: "imou-geelab", message: message}, window.location.origin);
        }
      };
    </script>
    """
    return Response(html.replace("<script>", bridge + "\n    <script>", 1), mimetype="text/html")


@app.get("/geelab/<path:name>")
def geelab_asset(name: str):
    return send_from_directory(GEELAB_ASSET_DIR, name)


@app.get("/api/state")
def api_state():
    with OPTIONS_LOCK:
        options = load_options()
        if migrate_stream_profiles(options):
            save_options(options)
        return jsonify(public_options(options))


@app.post("/api/accounts/start")
def api_account_start():
    data = flask_request.get_json(force=True, silent=True) or {}
    email = str(data.get("email", "")).strip()
    password = str(data.get("password", ""))
    label = str(data.get("label", "")).strip()
    if not email or not password:
        return error_response("Email and password are required")
    token = get_token(email, password)
    if not pcs_ok(token):
        log_cloud_response("GetToken/start", token)
    code = token.get("code")
    if is_captcha_required(token):
        token_data = token.get("data") or {}
        cap = token_data.get("captchaData") or {}
        pending_id = uuid.uuid4().hex
        PENDING[pending_id] = {
            "email": email,
            "password": password,
            "label": label,
            "captcha": {
                k: cap.get(k)
                for k in (
                    "codeId",
                    "captchaId",
                    "captchaServer",
                    "captchaMetaData",
                    "usage",
                    "verifyToken",
                    "gt",
                    "challenge",
                    "gtServerStatus",
                )
            },
            "expires": time.time() + 300,
        }
        if token_data.get("captchaMetaData"):
            PENDING[pending_id]["captcha"]["captchaMetaData"] = token_data.get("captchaMetaData")
        return jsonify(captcha_response_payload(token_data, pending_id))
    token_data = token.get("data") or {}
    if not pcs_ok(token):
        return error_response(str(token.get("msg") or token), status=401, code=code)
    if has_login_token(token_data):
        return finish_login(email, password, label, token_data)
    if token_data.get("captchaData"):
        pending_id = uuid.uuid4().hex
        cap = token_data.get("captchaData") or {}
        PENDING[pending_id] = {
            "email": email,
            "password": password,
            "label": label,
            "captcha": {
                k: cap.get(k)
                for k in (
                    "codeId",
                    "captchaId",
                    "captchaServer",
                    "captchaMetaData",
                    "usage",
                    "verifyToken",
                    "gt",
                    "challenge",
                    "gtServerStatus",
                )
            },
            "expires": time.time() + 300,
        }
        if token_data.get("captchaMetaData"):
            PENDING[pending_id]["captcha"]["captchaMetaData"] = token_data.get("captchaMetaData")
        return jsonify(captcha_response_payload(token_data, pending_id))
    return error_response(f"GetToken returned success without login token: {sorted(token_data.keys())}", status=502, code=code)


@app.post("/api/accounts/captcha")
def api_account_captcha():
    data = flask_request.get_json(force=True, silent=True) or {}
    pending_id = str(data.get("pending_id", ""))
    code = str(data.get("code", "")).strip()
    pending = PENDING.get(pending_id)
    if not pending or pending.get("expires", 0) < time.time():
        return error_response("Verification session expired", status=410)
    if not code:
        return error_response("Verification code is required")
    verified = verify_captcha(pending["email"], pending["password"], pending["captcha"], code)
    if not pcs_ok(verified):
        return error_response(str(verified.get("msg") or verified), status=401, code=verified.get("code"))
    token = get_token(pending["email"], pending["password"])
    if not pcs_ok(token):
        log_cloud_response("GetToken/after-image-captcha", token)
    token_data = token.get("data") or {}
    if not pcs_ok(token):
        return error_response(str(token.get("msg") or token), status=401, code=token.get("code"))
    if not has_login_token(token_data):
        return error_response(f"GetToken returned success without login token: {sorted(token_data.keys())}", status=502, code=token.get("code"))
    PENDING.pop(pending_id, None)
    return finish_login(pending["email"], pending["password"], pending["label"], token_data)


@app.post("/api/accounts/geetest4")
def api_account_geetest4():
    data = flask_request.get_json(force=True, silent=True) or {}
    pending_id = str(data.get("pending_id", ""))
    result = data.get("result") or {}
    pending = PENDING.get(pending_id)
    if not pending or pending.get("expires", 0) < time.time():
        return error_response("Verification session expired", status=410)
    challenge = pending["captcha"]
    if not challenge.get("captchaId"):
        return error_response("Geetest captchaId is missing from the server challenge", status=422)
    required = ("lot_number", "captcha_output", "pass_token", "gen_time")
    if not all(result.get(k) for k in required):
        return error_response("Geetest validation payload is incomplete", status=400)
    verified = verify_geetest4(pending["email"], pending["password"], challenge, result)
    if not pcs_ok(verified):
        debug = {
            "captcha_id_match": (result.get("captcha_id") or result.get("captchaId") or challenge.get("captchaId")) == challenge.get("captchaId"),
            "has_challenge_meta": bool(challenge.get("captchaMetaData")),
            "has_verify_token": bool(challenge.get("verifyToken")),
            "sent_challenge_meta": bool(challenge.get("captchaMetaData")),
            "verify_variant": verified.get("_variant"),
            "verify_attempts": verified.get("_attempts", []),
            "result_keys": sorted(result.keys()),
            "result_lengths": {k: len(str(result.get(k) or "")) for k in ("lot_number", "captcha_output", "pass_token", "gen_time", "captcha_id")},
        }
        print(f"[app-ui] geetest verify failed code={verified.get('code')} desc={verified.get('desc') or verified.get('msg')} debug={json.dumps(debug, separators=(',', ':'))}", flush=True)
        return error_response(str(verified.get("msg") or verified), status=401, code=verified.get("code"), details=debug)
    token = get_token(pending["email"], pending["password"])
    if not pcs_ok(token):
        log_cloud_response("GetToken/after-geetest4", token)
    token_data = token.get("data") or {}
    if is_captcha_required(token):
        cap = token_data.get("captchaData") or {}
        new_pending_id = uuid.uuid4().hex
        PENDING[new_pending_id] = {
            "email": pending["email"],
            "password": pending["password"],
            "label": pending["label"],
            "captcha": {
                k: cap.get(k)
                for k in (
                    "codeId",
                    "captchaId",
                    "captchaServer",
                    "captchaMetaData",
                    "usage",
                    "verifyToken",
                    "gt",
                    "challenge",
                    "gtServerStatus",
                )
            },
            "expires": time.time() + 300,
        }
        if token_data.get("captchaMetaData"):
            PENDING[new_pending_id]["captcha"]["captchaMetaData"] = token_data.get("captchaMetaData")
        PENDING.pop(pending_id, None)
        return jsonify(captcha_response_payload(token_data, new_pending_id))
    if not pcs_ok(token):
        return error_response(str(token.get("msg") or token), status=401, code=token.get("code"))
    if not has_login_token(token_data):
        return error_response(f"GetToken returned success without login token: {sorted(token_data.keys())}", status=502, code=token.get("code"))
    PENDING.pop(pending_id, None)
    return finish_login(pending["email"], pending["password"], pending["label"], token_data)


def finish_login(email: str, password: str, label: str, token_data: dict):
    login = do_login(token_data)
    if not pcs_ok(login):
        return error_response(str(login.get("msg") or login), status=401, code=login.get("code"))
    devices = list_devices(token_data)
    if not pcs_ok(devices):
        return error_response(str(devices.get("msg") or devices), status=502, code=devices.get("code"))
    account = build_account(email, label, login, devices, password=password)
    with OPTIONS_LOCK:
        options = load_options()
        options.setdefault("accounts", [])
        options["accounts"].append(account)
        sync_managed_cameras(options)
        save_options(options)
    public_account = deepcopy(account)
    public_account["has_password"] = bool(public_account.pop("password", ""))
    return jsonify({"account": public_account})


@app.delete("/api/accounts/<account_id>")
def api_delete_account(account_id: str):
    with OPTIONS_LOCK:
        options = load_options()
        before = len(options.get("accounts", []))
        options["accounts"] = [a for a in options.get("accounts", []) if a.get("id") != account_id]
        if len(options["accounts"]) == before:
            return error_response("Account not found", status=404)
        sync_managed_cameras(options)
        save_options(options)
        return jsonify(public_options(options))


@app.post("/api/accounts/<account_id>/reload")
def api_reload_account(account_id: str):
    with OPTIONS_LOCK:
        options = load_options()
        account = next((a for a in options.get("accounts", []) if a.get("id") == account_id), None)
        if not account:
            return error_response("Account not found", status=404)
        email = str(account.get("email") or "").strip()
        password = str(account.get("password") or "").strip()
        label = str(account.get("label") or email)
    if not password:
        return error_response("Update this account password before reloading from Imou", status=409)
    try:
        _token, login, devices = login_and_list(email, password)
    except ValueError as exc:
        return error_response(str(exc), status=401)
    refreshed = build_account(email, label, login, devices, password=password, account_id=account_id)
    with OPTIONS_LOCK:
        options = load_options()
        for idx, existing in enumerate(options.get("accounts", [])):
            if existing.get("id") == account_id:
                options["accounts"][idx] = merge_account_refresh(existing, refreshed)
                sync_managed_cameras(options)
                save_options(options)
                return jsonify(public_options(options))
    return error_response("Account not found", status=404)


@app.post("/api/accounts/<account_id>/password")
def api_update_account_password(account_id: str):
    data = flask_request.get_json(force=True, silent=True) or {}
    password = str(data.get("password") or "").strip()
    if not password:
        return error_response("Password is required")
    with OPTIONS_LOCK:
        options = load_options()
        account = next((a for a in options.get("accounts", []) if a.get("id") == account_id), None)
        if not account:
            return error_response("Account not found", status=404)
        email = str(account.get("email") or "").strip()
        label = str(account.get("label") or email)
    try:
        _token, login, devices = login_and_list(email, password)
    except ValueError as exc:
        return error_response(str(exc), status=401)
    refreshed = build_account(email, label, login, devices, password=password, account_id=account_id)
    with OPTIONS_LOCK:
        options = load_options()
        for idx, existing in enumerate(options.get("accounts", [])):
            if existing.get("id") == account_id:
                options["accounts"][idx] = merge_account_refresh(existing, refreshed)
                sync_managed_cameras(options)
                save_options(options)
                return jsonify(public_options(options))
    return error_response("Account not found", status=404)


@app.patch("/api/accounts/<account_id>/cameras/<serial>")
def api_update_camera(account_id: str, serial: str):
    data = flask_request.get_json(force=True, silent=True) or {}
    allowed = {"enabled", "bridge_name", "username", "password", "channel", "subtype", "streams", "relay", "ptz", "talk", "engine", "warm"}
    with OPTIONS_LOCK:
        options = load_options()
        old_slug = current_managed_slug(options, account_id, serial)
        for account in options.get("accounts", []):
            if account.get("id") != account_id:
                continue
            for cam in account.get("cameras", []):
                if cam.get("serial") != serial:
                    continue
                next_cam = deepcopy(cam)
                for key, value in data.items():
                    if key in allowed:
                        next_cam[key] = value
                is_lan = bool(next_cam.get("lan_detected") and next_cam.get("local_ip"))
                password = str(next_cam.get("password") or "").strip()
                if bool(next_cam.get("relay")) and not password:
                    return error_response("Camera password is required before enabling relay")
                if bool(next_cam.get("enabled")) and not is_lan and not password:
                    return error_response("Camera password is required before enabling remote P2P bridge")
                for key, value in data.items():
                    if key in allowed:
                        cam[key] = value
                if bool(cam.get("enabled")):
                    cam["ptz"] = True
                    cam["talk"] = True
                sync_managed_cameras(options)
                save_options(options)
                return jsonify(public_options(options))
        return error_response("Camera not found", status=404)


@app.post("/api/cameras/<serial>/tts")
def api_camera_tts(serial: str):
    data = flask_request.get_json(force=True, silent=True) or {}
    text = str(data.get("text") or data.get("message") or "").strip()
    lang = str(data.get("lang") or data.get("language") or "vi").strip().lower()
    if not text:
        return error_response("Text is required")
    if len(text) > 300:
        return error_response("Text is too long; keep each TTS request under 300 characters")
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", lang):
        return error_response("Invalid language code")
    with OPTIONS_LOCK:
        options = load_options()
        cam = find_talk_camera(options, serial)
        if not cam:
            return error_response("Camera not found", status=404)
        cam = deepcopy(cam)
    try:
        result = run_camera_tts(cam, text, lang)
    except subprocess.TimeoutExpired:
        return error_response("TTS talk timed out", status=504)
    except Exception as exc:
        return error_response(str(exc), status=500)
    if not result["ok"]:
        return error_response("TTS talk failed", status=502, details=result)
    return jsonify(result)


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Imou Bridge</title>
  <script>
    (function () {
      try {
        var saved = localStorage.getItem("imou_ui_theme");
        var systemDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
        var mode = saved === "dark" || saved === "night" ? "night" : (saved === "light" || saved === "day" ? "day" : "auto");
        document.documentElement.dataset.themeMode = mode;
        document.documentElement.dataset.theme = mode === "auto" ? (systemDark ? "dark" : "light") : (mode === "night" ? "dark" : "light");
      } catch (e) {
        document.documentElement.dataset.theme = "light";
      }
    })();
  </script>
  <script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
  <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
  <script crossorigin src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fb;
      --panel: #ffffff;
      --ink: #172033;
      --muted: #667085;
      --line: #d8dfeb;
      --brand: #0f766e;
      --brand-2: #155eef;
      --danger: #b42318;
      --ok: #067647;
      --shadow: 0 10px 28px rgba(22, 32, 51, .08);
      --control: #ffffff;
      --header: #0b1220;
      --header-ink: #ffffff;
      --header-muted: #b8c2d6;
      --mark-bg: #14b8a6;
      --mark-ink: #06201d;
      --hover-line: #aab6ca;
      --menu-shadow: 0 14px 38px rgba(22, 32, 51, .16);
      --dirty: #fffdf7;
      --thumb: #edf3f8;
      --serial: #475467;
      --badge-bg: #eff6ff;
      --badge-line: #b7d7ff;
      --badge-ink: #155eef;
      --empty-bg: #fbfcff;
      --error-bg: #fff1f0;
      --error-line: #fecdca;
      --switch-off: #c9d2e3;
      --knob: #ffffff;
      --backdrop: rgba(12, 18, 32, .48);
      --brand-contrast: #ffffff;
    }
    :root[data-theme="dark"] {
      color-scheme: dark;
      --bg: #101413;
      --panel: #171c1b;
      --ink: #edf4f1;
      --muted: #9aa8a4;
      --line: #303a37;
      --brand: #2dd4bf;
      --brand-2: #9ab3ff;
      --danger: #ff9a90;
      --ok: #74d99f;
      --shadow: 0 12px 30px rgba(0, 0, 0, .34);
      --control: #1e2423;
      --header: #0c1110;
      --header-ink: #f1f7f4;
      --header-muted: #a8b7b2;
      --mark-bg: #2dd4bf;
      --mark-ink: #071311;
      --hover-line: #50625d;
      --menu-shadow: 0 18px 44px rgba(0, 0, 0, .44);
      --dirty: #252316;
      --thumb: #202827;
      --serial: #b8c4c0;
      --badge-bg: #162f2b;
      --badge-line: #276d62;
      --badge-ink: #7dd3c7;
      --empty-bg: #151918;
      --error-bg: #2a1716;
      --error-line: #7f2f2a;
      --switch-off: #4a5552;
      --knob: #f3f7f5;
      --backdrop: rgba(0, 0, 0, .58);
      --brand-contrast: #071311;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font: 14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); }
    button, input { font: inherit; }
    .shell { min-height: 100vh; display: grid; grid-template-rows: auto 1fr; }
    header { background: var(--header); color: var(--header-ink); padding: 18px 28px; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .mark { width: 38px; height: 38px; display: grid; place-items: center; border-radius: 8px; background: var(--mark-bg); color: var(--mark-ink); font-weight: 800; }
    h1 { font-size: 18px; margin: 0; font-weight: 700; }
    .subtitle { margin-top: 2px; color: var(--header-muted); font-size: 13px; }
    .header-actions { display: flex; align-items: center; gap: 10px; }
    .theme-toggle { display: inline-grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 3px; min-height: 38px; min-width: 226px; padding: 3px; border: 1px solid rgba(255,255,255,.18); border-radius: 7px; background: rgba(255,255,255,.08); color: var(--header-muted); }
    .theme-option { border: 0; border-radius: 5px; padding: 6px 8px; min-width: 0; background: transparent; color: inherit; cursor: pointer; font-weight: 700; font-size: 12px; display: inline-flex; align-items: center; justify-content: center; gap: 5px; }
    .theme-option .line-icon { width: 14px; height: 14px; color: currentColor; }
    .theme-option.active { background: var(--panel); color: var(--ink); box-shadow: 0 1px 5px rgba(0,0,0,.16); }
    main { width: min(1180px, calc(100vw - 32px)); margin: 20px auto 40px; display: grid; gap: 14px; align-content: start; align-items: start; grid-auto-rows: max-content; }
    .toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); align-self: start; }
    .panel-head { padding: 14px 18px; border-bottom: 1px solid var(--line); display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .panel-title { margin: 0; font-size: 16px; }
    .panel-body { padding: 14px 18px; }
    .btn { border: 1px solid var(--line); background: var(--control); color: var(--ink); border-radius: 7px; padding: 9px 12px; cursor: pointer; display: inline-flex; align-items: center; gap: 8px; min-height: 38px; }
    .btn:hover { border-color: var(--hover-line); }
    .btn.primary { background: var(--brand); border-color: var(--brand); color: var(--brand-contrast); }
    .btn.danger { color: var(--danger); border-color: #f1b8b2; }
    .btn.icon { width: 38px; padding: 0; justify-content: center; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .field { display: grid; gap: 6px; }
    label { color: var(--muted); font-size: 12px; font-weight: 600; }
    input { border: 1px solid var(--line); border-radius: 7px; padding: 9px 10px; outline: none; min-width: 0; background: var(--control); color: var(--ink); }
    input:focus { border-color: var(--brand-2); box-shadow: 0 0 0 3px rgba(21, 94, 239, .12); }
    .accounts { display: grid; gap: 14px; }
    .account-card { overflow: hidden; }
    .account-card.collapsed .panel-head { border-bottom: 0; }
    .account-meta { display: flex; align-items: center; gap: 12px; min-width: 0; border: 0; background: transparent; color: inherit; padding: 0; text-align: left; cursor: pointer; flex: 1; }
    .account-meta:hover .name { color: var(--brand); }
    .account-meta:focus-visible { outline: 3px solid rgba(21, 94, 239, .18); outline-offset: 4px; border-radius: 8px; }
    .account-copy { min-width: 0; display: grid; gap: 2px; }
    .account-lines { display: grid; gap: 2px; margin-top: 2px; }
    .account-subline { display: flex; align-items: center; gap: 5px; min-width: 0; color: var(--muted); font-size: 13px; }
    .account-subline .line-icon { width: 13px; height: 13px; flex: 0 0 13px; }
    .account-subline span { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .avatar { width: 36px; height: 36px; border-radius: 8px; background: #e6f7f4; color: #0f766e; display: grid; place-items: center; font-weight: 800; }
    .chevron { width: 18px; height: 18px; display: grid; place-items: center; color: var(--muted); transition: transform .18s ease; flex: 0 0 auto; }
    .chevron.collapsed { transform: rotate(-90deg); }
    .account-actions { position: relative; flex: 0 0 auto; }
    .menu-button { color: var(--muted); font-size: 20px; line-height: 1; }
    .account-menu { position: absolute; right: 0; top: calc(100% + 6px); min-width: 142px; padding: 6px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--menu-shadow); z-index: 4; }
    .menu-item { width: 100%; border: 0; background: transparent; border-radius: 6px; padding: 9px 10px; text-align: left; cursor: pointer; color: var(--ink); display: flex; align-items: center; gap: 8px; }
    .menu-item .line-icon { width: 15px; height: 15px; }
    .menu-item:hover { background: var(--bg); }
    .menu-item.danger { color: var(--danger); }
    .name { font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .muted { color: var(--muted); font-size: 13px; }
    .camera-collapse { overflow: hidden; transition: max-height .26s ease, opacity .18s ease; }
    .camera-collapse.open { max-height: 3200px; opacity: 1; }
    .camera-collapse.collapsed { max-height: 0; opacity: 0; }
    .camera-box { margin: 0 18px 18px; border-top: 1px solid var(--line); }
    .camera-row { display: grid; grid-template-columns: 56px minmax(0, 1fr) 62px; gap: 12px; align-items: center; padding: 12px; border-top: 1px solid var(--line); }
    .camera-row.compact, .camera-row.folded { padding-block: 10px; }
    .camera-row:first-child { border-top: 0; }
    .camera-row.dirty { background: var(--dirty); }
    .camera-thumb { width: 56px; height: 42px; border-radius: 7px; background: var(--thumb); border: 1px solid var(--line); overflow: hidden; display: grid; place-items: center; color: var(--muted); font-weight: 800; }
    .camera-thumb > * { grid-area: 1 / 1; }
    .camera-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .thumb-icon { width: 24px; height: 24px; color: var(--muted); }
    .camera-toggles { grid-column: 2 / -1; display: grid; gap: 8px; width: min(240px, 100%); padding-top: 8px; }
    .camera-extra { grid-column: 2 / -1; display: grid; grid-template-columns: minmax(180px, 1fr) minmax(180px, 1fr); gap: 10px; align-items: end; padding-top: 6px; }
    .integration-panel { grid-column: 2 / -1; display: grid; gap: 10px; margin-top: 10px; }
    .integration-grid { display: grid; grid-template-columns: 1fr; gap: 10px; }
    .copy-field { display: grid; grid-template-columns: minmax(150px, 210px) minmax(0, 1fr) auto; gap: 8px; align-items: center; min-width: 0; }
    .row-label { display: inline-flex; align-items: center; gap: 6px; min-width: 0; color: var(--muted); font-size: 12px; font-weight: 600; }
    .row-label span:last-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .line-icon { width: 15px; height: 15px; flex: 0 0 15px; color: var(--muted); }
    .copy-value { min-width: 0; overflow-wrap: anywhere; word-break: break-word; white-space: normal; border: 1px solid var(--line); border-radius: 7px; padding: 9px 10px; background: var(--panel); color: var(--ink); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 1.45; }
    .yaml-field { align-items: start; }
    .yaml-preview { margin: 0; min-width: 0; overflow-x: auto; white-space: pre-wrap; border: 1px solid var(--line); border-radius: 7px; padding: 12px; background: var(--panel); color: var(--ink); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 1.45; }
    .yaml-key { color: var(--brand-2); font-weight: 700; }
    .yaml-string { color: var(--ok); }
    .yaml-number, .yaml-bool { color: var(--badge-ink); }
    .yaml-punc { color: var(--muted); }
    .yaml-field .yaml-preview { grid-column: 1 / -1; grid-row: 2; }
    .yaml-field .copy-btn { grid-column: 3; grid-row: 1; justify-self: end; }
    .copy-btn { border: 1px solid var(--line); border-radius: 7px; background: var(--panel); color: var(--ink); cursor: pointer; min-width: 54px; min-height: 30px; padding: 0 8px; font-size: 12px; font-weight: 700; }
    .copy-btn:hover { border-color: var(--hover-line); }
    .camera-actions { grid-column: 1 / -1; display: flex; align-items: center; justify-content: flex-end; gap: 8px; padding-top: 2px; }
    .camera-enable { display: grid; gap: 6px; justify-items: start; }
    .camera-enable label:first-child, .mini-toggle .row-label { font-size: 11px; }
    .mini-toggle { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-width: 0; }
    .camera-main { min-width: 0; }
    .camera-title { display: flex; align-items: baseline; gap: 5px; min-width: 0; }
    .camera-title .name { min-width: 0; }
    .camera-title-icon { color: var(--muted); transform: translateY(2px); }
    .camera-row.enabled .camera-thumb, .camera-row.enabled .camera-main { cursor: pointer; }
    .camera-row.enabled .camera-main:hover .name { color: var(--brand); }
    .camera-model-line { display: flex; align-items: center; gap: 5px; color: var(--muted); font-size: 12px; font-weight: 600; min-width: 0; margin-top: 3px; }
    .camera-model-line .line-icon, .camera-meta-line .line-icon { width: 13px; height: 13px; flex: 0 0 13px; }
    .camera-model { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .camera-meta-line { display: flex; align-items: center; gap: 5px; min-width: 0; margin-top: 2px; }
    .camera-home-line { display: flex; align-items: center; gap: 5px; color: var(--muted); font-size: 12px; font-weight: 600; min-width: 0; margin-top: 3px; }
    .camera-home-line .line-icon { width: 13px; height: 13px; }
    .home-name { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .camera-stream-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 3px; }
    .channel-pill { display: inline-flex; align-items: center; gap: 4px; color: var(--muted); font-size: 11px; white-space: nowrap; }
    .channel-pill .line-icon { width: 13px; height: 13px; }
    .status-pill { display: inline-flex; align-items: center; min-height: 20px; border-radius: 999px; padding: 2px 7px; font-size: 11px; font-weight: 700; border: 1px solid var(--line); color: var(--muted); background: var(--empty-bg); }
    .status-pill.ready, .status-box.ready { color: var(--ok); border-color: rgba(6, 118, 71, .28); background: rgba(6, 118, 71, .08); }
    .status-pill.starting, .status-pill.configured, .status-box.starting, .status-box.configured { color: var(--badge-ink); border-color: var(--badge-line); background: var(--badge-bg); }
    .status-pill.restarting, .status-box.restarting { color: #b54708; border-color: #fedf89; background: #fffaeb; }
    .status-pill.error, .status-box.error { color: var(--danger); border-color: var(--error-line); background: var(--error-bg); }
    .status-box { display: grid; gap: 3px; border: 1px solid var(--line); border-radius: 7px; padding: 10px 12px; color: var(--muted); background: var(--empty-bg); }
    .status-box strong { font-size: 12px; text-transform: uppercase; letter-spacing: .02em; }
    .status-box span { font-size: 13px; line-height: 1.35; }
    .serial { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: var(--serial); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .badge { display: inline-flex; align-items: center; border: 1px solid var(--badge-line); background: var(--badge-bg); color: var(--badge-ink); padding: 2px 7px; border-radius: 999px; font-size: 12px; width: fit-content; }
    .switch { position: relative; width: 38px; height: 22px; display: inline-block; }
    .camera-enable > .switch { width: 42px; height: 24px; }
    .switch input { opacity: 0; width: 0; height: 0; }
    .slider { position: absolute; cursor: pointer; inset: 0; background: var(--switch-off); border-radius: 999px; transition: .18s; }
    .slider:before { position: absolute; content: ""; width: 16px; height: 16px; left: 3px; top: 3px; background: var(--knob); border-radius: 50%; transition: .18s; box-shadow: 0 1px 3px rgba(0,0,0,.22); }
    .camera-enable > .switch .slider:before { width: 18px; height: 18px; }
    .switch input:checked + .slider { background: var(--brand); }
    .switch input:checked + .slider:before { transform: translateX(16px); }
    .camera-enable > .switch input:checked + .slider:before { transform: translateX(18px); }
    .switch input:disabled + .slider { cursor: not-allowed; opacity: .55; }
    .empty { padding: 28px; color: var(--muted); text-align: center; border: 1px dashed var(--line); border-radius: 8px; background: var(--empty-bg); }
    .modal-backdrop { position: fixed; inset: 0; background: var(--backdrop); display: grid; place-items: center; padding: 16px; z-index: 10; }
    .modal { width: min(560px, 100%); background: var(--panel); border-radius: 8px; box-shadow: 0 24px 80px rgba(0,0,0,.25); overflow: hidden; }
    .modal.camera-modal { width: min(920px, calc(100vw - 24px)); max-height: min(92vh, 920px); display: grid; grid-template-rows: auto minmax(0, 1fr) auto; }
    .modal.camera-modal .modal-body { overflow: auto; align-content: start; }
    .camera-detail-summary { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .camera-detail-summary .camera-thumb { flex: 0 0 auto; }
    .camera-detail-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .modal-head { padding: 16px 18px; border-bottom: 1px solid var(--line); display: flex; align-items: center; justify-content: space-between; }
    .modal-body { padding: 18px; display: grid; gap: 14px; }
    .modal-actions { padding: 14px 18px; border-top: 1px solid var(--line); display: flex; justify-content: flex-end; gap: 10px; }
    .error { color: var(--danger); background: var(--error-bg); border: 1px solid var(--error-line); padding: 10px 12px; border-radius: 7px; }
    .ok { color: var(--ok); }
    .challenge-box { display: grid; gap: 12px; padding: 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--empty-bg); }
    .challenge-target { min-height: 48px; }
    .challenge-target .geetest_box_wrap, .challenge-target .geetest_bind_box { max-width: 100%; }
    .geelab-frame { width: 100%; height: 390px; border: 0; background: var(--panel); border-radius: 8px; }
    @media (max-width: 760px) {
      header { padding: 14px 16px; align-items: stretch; flex-direction: column; }
      .header-actions { width: 100%; display: grid; grid-template-columns: minmax(0, 1fr) auto; }
      .theme-toggle { min-width: 0; width: 100%; }
      header .btn { width: 100%; justify-content: center; }
      main { width: min(100vw - 18px, 1180px); margin: 10px auto 24px; gap: 12px; }
      .toolbar, .panel-head { align-items: stretch; flex-direction: column; }
      .panel-head { padding: 12px; }
      .panel-body { padding: 12px; }
      .account-meta { width: 100%; align-items: center; }
      .account-copy { padding-right: 54px; }
      .account-subline { align-items: flex-start; }
      .account-subline .line-icon { margin-top: 2px; }
      .account-subline span { white-space: normal; overflow: visible; text-overflow: clip; line-height: 1.25; }
      .account-actions { align-self: flex-end; margin-top: -48px; }
      .account-card { overflow: visible; }
      .camera-box { margin: 0 14px 14px; border-top: 1px solid var(--line); }
      .grid { grid-template-columns: 1fr; }
      .camera-row, .camera-row.compact, .camera-row.folded { grid-template-columns: 52px minmax(0, 1fr) 58px; align-items: start; gap: 10px; padding: 14px 0; border-top: 1px solid var(--line); }
      .camera-row:first-child { border-top: 0; }
      .camera-row > .camera-enable { grid-column: 3; grid-row: 1; justify-self: end; }
      .camera-thumb { width: 52px; height: 40px; }
      .camera-title { align-items: flex-start; row-gap: 2px; }
      .camera-title .name, .camera-model, .home-name { white-space: normal; overflow: visible; text-overflow: clip; line-height: 1.25; }
      .camera-title .name { flex: 1 1 calc(100% - 24px); min-width: 0; }
      .camera-title-icon { margin-top: 2px; }
      .camera-meta-line { flex-wrap: wrap; gap: 4px 8px; }
      .camera-model-line, .camera-home-line { align-items: flex-start; }
      .camera-model-line .line-icon, .camera-home-line .line-icon { margin-top: 1px; }
      .serial { white-space: normal; overflow-wrap: anywhere; }
      .badge { max-width: 100%; white-space: normal; }
      .camera-toggles { width: 100%; border-top: 1px solid var(--line); padding-top: 12px; margin-top: 2px; }
      .camera-extra { grid-template-columns: 1fr 1fr; }
      .camera-extra, .integration-panel { padding-top: 10px; margin-top: 2px; border-top: 1px solid var(--line); }
      .integration-grid { grid-template-columns: 1fr; }
      .copy-field { grid-template-columns: minmax(0, 1fr) auto; }
      .copy-field > .row-label { grid-column: 1; }
      .copy-field:not(.yaml-field) .copy-btn { grid-column: 2; grid-row: 1; justify-self: end; }
      .copy-value { grid-column: 1 / -1; grid-row: 2; }
      .yaml-field .copy-btn { grid-column: 2; }
      .camera-actions { justify-content: stretch; padding-top: 0; }
      .camera-actions .btn { flex: 1; justify-content: center; }
      .camera-detail-grid { grid-template-columns: 1fr; }
      .modal.camera-modal { width: min(100vw - 18px, 920px); max-height: 94vh; }
    }
    @media (max-width: 430px) {
      .camera-extra { grid-template-columns: 1fr; }
      .camera-row, .camera-row.compact, .camera-row.folded { grid-template-columns: 48px minmax(0, 1fr) 56px; }
      .camera-thumb { width: 48px; height: 38px; }
      .subtitle { font-size: 12px; }
    }
  </style>
</head>
<body>
<div id="root"></div>
<script type="text/babel">
const {useEffect, useMemo, useRef, useState} = React;

function randomHex(length) {
  const alphabet = "0123456789abcdef";
  let out = "";
  const cryptoObj = window.crypto || window.msCrypto;
  if (cryptoObj?.getRandomValues) {
    const bytes = new Uint8Array(length);
    cryptoObj.getRandomValues(bytes);
    for (let i = 0; i < length; i++) out += alphabet[bytes[i] & 15];
    return out;
  }
  for (let i = 0; i < length; i++) out += alphabet[Math.floor(Math.random() * 16)];
  return out;
}

function uuid4() {
  return `${randomHex(8)}-${randomHex(4)}-4${randomHex(3)}-${(8 + Math.floor(Math.random() * 4)).toString(16)}${randomHex(3)}-${randomHex(12)}`;
}

function showGeelabSlider() {
  const frame = document.getElementById("geelab4-frame");
  frame?.contentWindow?.jsBridge?.callback?.("showBox");
}

function api(path, opts = {}) {
  return fetch(path, {
    ...opts,
    headers: {"Content-Type": "application/json", ...(opts.headers || {})},
  }).then(async r => {
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const details = data.details ? `\n${JSON.stringify(data.details)}` : "";
      throw new Error((data.error || `HTTP ${r.status}`) + details);
    }
    return data;
  });
}

function slugifyUi(value) {
  return String(value || "").trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "") || "camera";
}

function enabledStreams(cam) {
  const streams = Array.isArray(cam.streams) && cam.streams.length ? cam.streams : [{id: "ch1_sub0", channel: 1, subtype: 0, enabled: true}];
  return streams.filter(s => s.enabled !== false);
}

function enabledChannelCount(cam) {
  return new Set(enabledStreams(cam).map(s => Number(s.channel || 1))).size || 1;
}

function bridgeStatus(cam, draft, isLan) {
  if (!!draft.enabled && !isLan && !String(draft.password || "").trim()) {
    return {state: "error", message: "Camera password is required before enabling remote P2P/relay streaming."};
  }
  if (!!draft.relay && !String(draft.password || "").trim()) {
    return {state: "error", message: "Camera password is required before enabling relay."};
  }
  return cam.bridge_status || {state: draft.enabled ? "configured" : "disabled", message: draft.enabled ? "Waiting for supervisor status." : "Bridge is off."};
}

function statusLabel(status) {
  const state = status?.state || "unknown";
  if (state === "ready") return "stream ready";
  if (state === "starting") return "starting";
  if (state === "restarting") return "reconnecting";
  if (state === "error") return "error";
  if (state === "disabled") return "off";
  return state;
}

function streamSlug(baseSlug, stream, primary) {
  if (primary) return baseSlug;
  const id = stream?.id || `ch${stream?.channel || 1}_sub${stream?.subtype || 0}`;
  return `${baseSlug}_${slugifyUi(id)}`;
}

function streamLabel(stream, index) {
  return stream?.label || `Ch ${stream?.channel || 1} Sub ${stream?.subtype ?? index}`;
}

function hostForLinks() {
  return window.location.hostname || "localhost";
}

function httpUrl(port, path) {
  const protocol = window.location.protocol || "http:";
  return `${protocol}//${hostForLinks()}:${port}${path}`;
}

function onvifPortForCamera(config, cam, draft) {
  const base = Number(config?.onvif_base_port || 8700);
  const rootCameras = Array.isArray(config?.cameras) ? config.cameras : [];
  let index = rootCameras.findIndex(item => item?.serial && item.serial === cam.serial);
  if (index < 0) index = rootCameras.length;
  return base + Math.max(0, index);
}

function cameraLinks(config, cam, draft) {
  const go2rtc = config?.go2rtc || {};
  const rtspPort = Number(go2rtc.rtsp_port || 8554);
  const apiPort = Number(go2rtc.api_port || 1984);
  const uiPort = Number(config?.discovery_port || window.location.port || 8099);
  const onvifPort = onvifPortForCamera(config, cam, draft);
  const onvif = httpUrl(onvifPort, "/onvif/device_service");
  const baseSlug = slugifyUi(draft.bridge_name || cam.bridge_name || cam.device_name || cam.serial);
  const streams = enabledStreams(cam);
  const streamLinks = (streams.length ? streams : [{id: "ch1_sub0", label: "Ch 1 Main", channel: 1, subtype: 0}]).map((stream, index) => {
    const slug = streamSlug(baseSlug, stream, index === 0);
    return {
      label: streamLabel(stream, index),
      slug,
      channel: Number(stream?.channel || 1),
      subtype: Number(stream?.subtype || 0),
      rtsp: `rtsp://${hostForLinks()}:${rtspPort}/${slug}`,
      webrtc: apiPort ? httpUrl(apiPort, `/stream.html?src=${encodeURIComponent(slug)}&mode=webrtc`) : "",
    };
  });
  const talk = httpUrl(uiPort, `/api/cameras/${encodeURIComponent(cam.serial)}/tts`);
  const frigateLines = [
    "cameras:",
  ];
  streamLinks.forEach((item) => {
    frigateLines.push(
      `  ${item.slug}:`,
      "    ffmpeg:",
      "      inputs:",
      `        - path: ${item.rtsp}`,
      "          roles:",
      "            - detect",
      "            - record",
      "    onvif:",
      `      host: ${hostForLinks()}`,
      `      port: ${onvifPort}`,
      `      user: ${draft.username || "admin"}`,
      `      password: ${draft.password || "x"}`,
      "    live:",
      `      stream_name: ${item.slug}`,
    );
  });
  return {onvif, onvifPort, streams: streamLinks, talk, frigate: frigateLines.join("\n")};
}

async function copyText(value) {
  const text = String(value || "");
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const node = document.createElement("textarea");
  node.value = text;
  node.style.position = "fixed";
  node.style.left = "-9999px";
  document.body.appendChild(node);
  node.focus();
  node.select();
  document.execCommand("copy");
  document.body.removeChild(node);
}

function normalizeThemeMode(value) {
  if (value === "dark" || value === "night") return "night";
  if (value === "light" || value === "day") return "day";
  return "auto";
}

function applyThemeMode(value) {
  const mode = normalizeThemeMode(value);
  const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const effective = mode === "auto" ? (media?.matches ? "dark" : "light") : (mode === "night" ? "dark" : "light");
  document.documentElement.dataset.themeMode = mode;
  document.documentElement.dataset.theme = effective;
  return mode;
}

function App() {
  const [state, setState] = useState(null);
  const [modal, setModal] = useState(false);
  const [passwordAccount, setPasswordAccount] = useState(null);
  const [err, setErr] = useState("");
  const [saving, setSaving] = useState(false);
  const [themeMode, setThemeModeState] = useState(() => normalizeThemeMode(document.documentElement.dataset.themeMode || localStorage.getItem("imou_ui_theme")));

  const load = () => api("/api/state").then(setState).catch(e => setErr(e.message));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
    if (!media) return;
    const onChange = () => {
      const mode = normalizeThemeMode(localStorage.getItem("imou_ui_theme") || document.documentElement.dataset.themeMode);
      if (mode === "auto") applyThemeMode("auto");
    };
    if (media.addEventListener) media.addEventListener("change", onChange);
    else media.addListener(onChange);
    return () => {
      if (media.removeEventListener) media.removeEventListener("change", onChange);
      else media.removeListener(onChange);
    };
  }, []);
  function setTheme(next) {
    const mode = applyThemeMode(next);
    localStorage.setItem("imou_ui_theme", mode);
    setThemeModeState(mode);
  }
  const accounts = state?.accounts || [];
  const enabledCount = accounts.reduce((n, a) => n + (a.cameras || []).filter(c => c.enabled).length, 0);

  async function removeAccount(id) {
    setState(await api(`/api/accounts/${id}`, {method: "DELETE"}));
  }

  async function reloadAccount(id) {
    setErr("");
    setSaving(true);
    try {
      setState(await api(`/api/accounts/${id}/reload`, {method: "POST"}));
    } catch (e) {
      setErr(e.message);
    } finally {
      setSaving(false);
    }
  }

  async function updateAccountPassword(account, password) {
    setErr("");
    const next = await api(`/api/accounts/${account.id}/password`, {
      method: "POST",
      body: JSON.stringify({password}),
    });
    setState(next);
  }

  async function updateCamera(accountId, serial, patch) {
    const next = await api(`/api/accounts/${accountId}/cameras/${encodeURIComponent(serial)}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    setState(next);
  }

  return <div className="shell">
    <header>
      <div className="brand">
        <div className="mark">IM</div>
        <div>
          <h1>Imou Bridge</h1>
          <div className="subtitle">Manage accounts, cameras, ONVIF, and P2P streams</div>
        </div>
      </div>
      <div className="header-actions">
        <ThemeToggle theme={themeMode} onChange={setTheme} />
        <button className="btn primary" onClick={() => setModal(true)}>+ Add account</button>
      </div>
    </header>
    <main>
      {err && <div className="error">{err}</div>}
      <div className="toolbar">
        <div>
          <h2 className="panel-title">Imou Accounts</h2>
          <div className="muted">{accounts.length} account(s), {enabledCount} camera(s) enabled for bridge/ONVIF</div>
        </div>
      </div>

      <section className="accounts">
        {!state && <div className="empty">Loading configuration...</div>}
        {state && accounts.length === 0 && <div className="empty">No accounts yet. Click “Add account” to sign in to Imou and fetch the camera list.</div>}
        {accounts.map(account => <AccountCard key={account.id} config={state} account={account} busy={saving} onReload={reloadAccount} onUpdatePassword={() => setPasswordAccount(account)} onDelete={removeAccount} onUpdateCamera={updateCamera} />)}
      </section>
    </main>
    {modal && <AddAccountModal onClose={() => setModal(false)} onAdded={() => { setModal(false); load(); }} />}
    {passwordAccount && <UpdatePasswordModal account={passwordAccount} onClose={() => setPasswordAccount(null)} onSaved={async password => { await updateAccountPassword(passwordAccount, password); setPasswordAccount(null); }} />}
  </div>;
}

function ThemeToggle({theme, onChange}) {
  const items = [
    {mode: "day", icon: "sun", label: "Day"},
    {mode: "night", icon: "moon", label: "Night"},
    {mode: "auto", icon: "auto", label: "Auto"},
  ];
  return <div className="theme-toggle" role="group" aria-label="UI theme">
    {items.map(item => <button key={item.mode} className={`theme-option ${theme === item.mode ? "active" : ""}`} onClick={() => onChange(item.mode)}>
      <LineIcon name={item.icon} />
      <span>{item.label}</span>
    </button>)}
  </div>;
}

function Field({label, value, onBlur, type = "text", placeholder = ""}) {
  const [v, setV] = useState(value ?? "");
  useEffect(() => setV(value ?? ""), [value]);
  return <div className="field">
    <label>{label}</label>
    <input type={type} value={v} placeholder={placeholder} onChange={e => setV(e.target.value)} onBlur={() => onBlur?.(v)} />
  </div>;
}

function EditField({label, value, onChange, type = "text", placeholder = ""}) {
  return <div className="field">
    <label>{label}</label>
    <input type={type} value={value ?? ""} placeholder={placeholder} onChange={e => onChange?.(e.target.value)} />
  </div>;
}

function AccountCard({config, account, busy = false, onReload, onUpdatePassword, onDelete, onUpdateCamera}) {
  const [collapsed, setCollapsed] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef(null);
  const cams = account.cameras || [];
  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = event => {
      if (menuRef.current && !menuRef.current.contains(event.target)) setMenuOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [menuOpen]);
  function remove() {
    setMenuOpen(false);
    if (!confirm(`Remove ${account.label || account.email} from the bridge? Cameras enabled from this account will also be removed from the generated config.`)) return;
    onDelete(account.id);
  }
  function reload() {
    setMenuOpen(false);
    onReload(account.id);
  }
  function updatePassword() {
    setMenuOpen(false);
    onUpdatePassword(account);
  }
  return <article className={`panel account-card ${collapsed ? "collapsed" : ""}`}>
    <div className="panel-head">
      <button className="account-meta" onClick={() => setCollapsed(!collapsed)} aria-expanded={!collapsed} title={collapsed ? "Show cameras" : "Hide cameras"}>
        <span className={`chevron ${collapsed ? "collapsed" : ""}`}>v</span>
        <div className="avatar">{(account.label || account.email || "?").slice(0,2).toUpperCase()}</div>
        <div className="account-copy">
          <div className="name">{account.label || account.email}</div>
          <div className="account-lines">
            <div className="account-subline"><LineIcon name="mail" /><span>{account.email}</span></div>
            <div className="account-subline"><LineIcon name="camera" /><span>{cams.length} camera</span></div>
          </div>
        </div>
      </button>
      <div className="account-actions" ref={menuRef}>
        <button className="btn icon menu-button" title="Account actions" onClick={() => setMenuOpen(!menuOpen)}>⋯</button>
        {menuOpen && <div className="account-menu">
          <button className="menu-item" onClick={reload} disabled={busy}><LineIcon name="refresh" />{busy ? "Reloading..." : "Reload"}</button>
          <button className="menu-item" onClick={updatePassword}><LineIcon name="key" />Update password</button>
          <button className="menu-item danger" onClick={remove}><LineIcon name="trash" />Delete</button>
        </div>}
      </div>
    </div>
    <div className={`camera-collapse ${collapsed ? "collapsed" : "open"}`}>
      <div className="camera-box">
        {cams.length === 0 && <div className="empty">This account did not return any cameras.</div>}
        {cams.map(cam => <CameraRow key={cam.serial} config={config} account={account} cam={cam} onUpdate={onUpdateCamera} />)}
      </div>
    </div>
  </article>;
}

function LineIcon({name, className = ""}) {
  const common = {viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: "2", strokeLinecap: "round", strokeLinejoin: "round", className: `line-icon ${className}`};
  const icons = {
    camera: <><path d="M14.5 4l1.4 2H20a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4.1l1.4-2h5z" /><circle cx="12" cy="13" r="3.5" /></>,
    cctv: <><path d="M4 8l9-4 2 5-9 4-2-5z" /><path d="M13 4l6 2-2 5-2-2" /><path d="M7 13v4" /><path d="M5 21h8" /><path d="M9 17l4 4" /></>,
    model: <><rect x="4" y="6" width="16" height="12" rx="2" /><path d="M8 10h8" /><path d="M8 14h5" /></>,
    serial: <><path d="M5 8h14" /><path d="M5 16h14" /><path d="M8 4l-2 16" /><path d="M16 4l-2 16" /></>,
    home: <><path d="M3 11l9-8 9 8" /><path d="M5 10v10h14V10" /><path d="M9 20v-6h6v6" /></>,
    sun: <><circle cx="12" cy="12" r="4" /><path d="M12 2v2" /><path d="M12 20v2" /><path d="M4.9 4.9l1.4 1.4" /><path d="M17.7 17.7l1.4 1.4" /><path d="M2 12h2" /><path d="M20 12h2" /><path d="M4.9 19.1l1.4-1.4" /><path d="M17.7 6.3l1.4-1.4" /></>,
    moon: <path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.5 6.5 0 0 0 9.8 9.8z" />,
    auto: <><path d="M21 12a9 9 0 0 1-15.4 6.4" /><path d="M3 12A9 9 0 0 1 18.4 5.6" /><path d="M18 2v4h-4" /><path d="M6 22v-4h4" /></>,
    mail: <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="M3 7l9 6 9-6" /></>,
    refresh: <><path d="M21 12a9 9 0 0 1-15.4 6.4" /><path d="M3 12A9 9 0 0 1 18.4 5.6" /><path d="M18 2v4h-4" /><path d="M6 22v-4h4" /></>,
    key: <><circle cx="7.5" cy="14.5" r="3.5" /><path d="M10 12l9-9" /><path d="M15 6l3 3" /><path d="M13 8l3 3" /></>,
    trash: <><path d="M3 6h18" /><path d="M8 6V4h8v2" /><path d="M6 6l1 15h10l1-15" /><path d="M10 11v6" /><path d="M14 11v6" /></>,
    layers: <><path d="M12 2l9 5-9 5-9-5 9-5z" /><path d="M3 12l9 5 9-5" /><path d="M3 17l9 5 9-5" /></>,
    channel: <><rect x="4" y="5" width="6" height="6" rx="1" /><rect x="14" y="5" width="6" height="6" rx="1" /><rect x="4" y="15" width="6" height="6" rx="1" /><rect x="14" y="15" width="6" height="6" rx="1" /></>,
    stream: <><path d="M4 7h10" /><path d="M4 12h16" /><path d="M4 17h12" /><path d="M18 7l2 2-2 2" /></>,
    chevronDown: <path d="M6 9l6 6 6-6" />,
    chevronRight: <path d="M9 6l6 6-6 6" />,
    onvif: <><circle cx="12" cy="12" r="8" /><path d="M2 12h20" /><path d="M12 4a12 12 0 0 1 0 16" /><path d="M12 4a12 12 0 0 0 0 16" /></>,
    relay: <><path d="M4 12a8 8 0 0 1 8-8" /><path d="M4 12a8 8 0 0 0 8 8" /><path d="M13 7l5 5-5 5" /></>,
    move: <><path d="M12 2v20" /><path d="M2 12h20" /><path d="M5 9l-3 3 3 3" /><path d="M19 9l3 3-3 3" /><path d="M9 5l3-3 3 3" /><path d="M9 19l3 3 3-3" /></>,
    mic: <><path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z" /><path d="M19 10v2a7 7 0 0 1-14 0v-2" /><path d="M12 19v3" /></>,
    link: <><path d="M10 13a5 5 0 0 0 7.1 0l2-2a5 5 0 0 0-7.1-7.1l-1.1 1.1" /><path d="M14 11a5 5 0 0 0-7.1 0l-2 2a5 5 0 0 0 7.1 7.1l1.1-1.1" /></>,
    radio: <><path d="M4.9 19.1a10 10 0 0 1 0-14.2" /><path d="M8.5 15.5a5 5 0 0 1 0-7" /><circle cx="12" cy="12" r="1.5" /><path d="M15.5 8.5a5 5 0 0 1 0 7" /><path d="M19.1 4.9a10 10 0 0 1 0 14.2" /></>,
    volume: <><path d="M11 5L6 9H3v6h3l5 4V5z" /><path d="M15.5 8.5a5 5 0 0 1 0 7" /></>,
    code: <><path d="M16 18l6-6-6-6" /><path d="M8 6l-6 6 6 6" /></>,
  };
  return <svg {...common}>{icons[name] || icons.link}</svg>;
}

function RowLabel({icon, children}) {
  return <div className="row-label"><LineIcon name={icon} /><span>{children}</span></div>;
}

function cameraDraft(cam) {
  const enabled = !!cam.enabled;
  return {
    enabled,
    bridge_name: cam.bridge_name || "",
    username: cam.username || "admin",
    password: cam.password || "",
    relay: !!cam.relay,
    ptz: enabled,
    talk: enabled,
  };
}

function CameraRow({config, account, cam, onUpdate}) {
  const saved = useMemo(() => cameraDraft(cam), [cam.serial, cam.bridge_name, cam.enabled, cam.username, cam.password, cam.relay]);
  const [draft, setDraft] = useState(saved);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  useEffect(() => setDraft(saved), [saved]);
  const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
  const isLan = !!(cam.lan_detected && cam.local_ip);
  const set = (k, v) => setDraft({...draft, [k]: v});
  const setEnabled = (enabled) => {
    setDraft({...draft, enabled, ptz: !!enabled, talk: !!enabled});
    if (enabled) setDetailsOpen(true);
  };
  async function save() {
    setSaving(true);
    try {
      await onUpdate(account.id, cam.serial, {
        enabled: !!draft.enabled,
        bridge_name: draft.bridge_name,
        username: draft.username || "admin",
        password: draft.password,
        relay: !!draft.relay,
        ptz: !!draft.enabled,
        talk: !!draft.enabled,
      });
    } finally {
      setSaving(false);
    }
  }
  function cancel() {
    setDraft({...saved});
  }
  const homeName = cam.family_name || cam.home_name || "";
  const links = cameraLinks(config, cam, draft);
  const streamCount = enabledStreams(cam).length || 1;
  const channelCount = enabledChannelCount(cam);
  const status = bridgeStatus(cam, draft, isLan);
  const showDetails = !!draft.enabled && detailsOpen;
  const toggleDetails = () => {
    if (draft.enabled) setDetailsOpen(true);
  };
  return <div className={`camera-row ${draft.enabled ? "enabled" : ""} ${dirty ? "dirty" : ""} compact`}>
    <div className="camera-thumb" onClick={toggleDetails} title={draft.enabled ? (detailsOpen ? "Collapse camera" : "Expand camera") : ""}>
      <LineIcon name="cctv" className="thumb-icon" />
      {cam.thumbnail_url && <img src={cam.thumbnail_url} alt="" loading="lazy" referrerPolicy="no-referrer" onError={e => { e.currentTarget.remove(); }} />}
    </div>
    <div className="camera-main" onClick={toggleDetails} title={draft.enabled ? (detailsOpen ? "Collapse camera" : "Expand camera") : ""}>
      <div className="camera-title">
        <LineIcon name="camera" className="camera-title-icon" />
        <div className="name">{cam.device_name || cam.serial}</div>
      </div>
      {cam.model && <div className="camera-model-line"><LineIcon name="model" /><span className="camera-model">{cam.model}</span></div>}
      {homeName && <div className="camera-home-line"><LineIcon name="home" /><span className="home-name">{homeName}</span></div>}
      <div className="camera-meta-line">
        <LineIcon name="serial" />
        <div className="serial">{cam.serial}</div>
      </div>
      <div className="camera-stream-line">
        <span className="channel-pill"><LineIcon name="channel" />{channelCount} channel</span>
        <span className="channel-pill"><LineIcon name="stream" />{streamCount} stream</span>
        <span className={`status-pill ${status.state || "unknown"}`} title={status.message || ""}>{statusLabel(status)}</span>
      </div>
    </div>
    <div className="camera-enable">
      <label>Bridge</label>
      <label className="switch" title="Expose this camera through go2rtc and ONVIF">
        <input type="checkbox" checked={!!draft.enabled} onChange={e => setEnabled(e.target.checked)} />
        <span className="slider"></span>
      </label>
    </div>
    {dirty && <div className="camera-actions">
      <button className="btn cancel-camera" onClick={cancel} disabled={!dirty || saving}>Cancel</button>
      <button className="btn primary save-camera" onClick={save} disabled={!dirty || saving}>{saving ? "Saving..." : "Save"}</button>
    </div>}
    {showDetails && <CameraDetailModal
      config={config}
      cam={cam}
      draft={draft}
      setDraft={setDraft}
      dirty={dirty}
      saving={saving}
      onSave={save}
      onCancel={cancel}
      onClose={() => setDetailsOpen(false)}
    />}
  </div>;
}

function CameraDetailModal({config, cam, draft, setDraft, dirty, saving, onSave, onCancel, onClose}) {
  const set = (k, v) => setDraft({...draft, [k]: v});
  const isLan = !!(cam.lan_detected && cam.local_ip);
  const links = cameraLinks(config, cam, draft);
  const homeName = cam.family_name || cam.home_name || "";
  const status = bridgeStatus(cam, draft, isLan);
  const relayBlocked = !isLan && !String(draft.password || "").trim();
  return <div className="modal-backdrop" onClick={onClose}>
    <div className="modal camera-modal" onClick={e => e.stopPropagation()}>
      <div className="modal-head">
        <div className="camera-detail-summary">
          <div className="camera-thumb">
            <LineIcon name="cctv" className="thumb-icon" />
            {cam.thumbnail_url && <img src={cam.thumbnail_url} alt="" loading="lazy" referrerPolicy="no-referrer" onError={e => { e.currentTarget.remove(); }} />}
          </div>
          <div className="camera-main">
            <div className="camera-title">
              <LineIcon name="camera" className="camera-title-icon" />
              <div className="name">{cam.device_name || cam.serial}</div>
            </div>
            {cam.model && <div className="camera-model-line"><LineIcon name="model" /><span className="camera-model">{cam.model}</span></div>}
            {homeName && <div className="camera-home-line"><LineIcon name="home" /><span className="home-name">{homeName}</span></div>}
            <div className="camera-meta-line"><LineIcon name="serial" /><div className="serial">{cam.serial}</div></div>
          </div>
        </div>
        <button className="btn icon" onClick={onClose}>×</button>
      </div>
      <div className="modal-body">
        <div className="camera-detail-grid">
          <EditField label="Bridge name" value={draft.bridge_name} onChange={v => set("bridge_name", v)} />
          <EditField label="Camera user" value={draft.username} onChange={v => set("username", v)} />
          <EditField label="Camera password" type="text" value={draft.password} onChange={v => set("password", v)} />
        </div>
        <div className={`status-box ${status.state || "unknown"}`}>
          <strong>{statusLabel(status)}</strong>
          <span>{status.message || ""}</span>
        </div>
        {!isLan && <div className="camera-toggles">
          <MiniSwitch icon="relay" label="Relay" checked={!!draft.relay} disabled={relayBlocked} title={relayBlocked ? "Enter camera password before enabling relay" : ""} onChange={v => set("relay", v)} />
        </div>}
        <IntegrationLinks links={links} />
      </div>
      <div className="modal-actions">
        <button className="btn" onClick={dirty ? () => { onCancel(); onClose(); } : onClose} disabled={saving}>{dirty ? "Cancel changes" : "Close"}</button>
        {dirty && <button className="btn primary" onClick={onSave} disabled={saving}>{saving ? "Saving..." : "Save"}</button>}
      </div>
    </div>
  </div>;
}

function IntegrationLinks({links}) {
  return <div className="integration-panel">
    <div className="integration-grid">
      <CopyField icon="onvif" label="ONVIF" value={links.onvif} />
      {links.streams.map(stream => <React.Fragment key={stream.slug}>
        <CopyField icon="link" label={`RTSP · ${stream.label}`} value={stream.rtsp} />
        {stream.webrtc && <CopyField icon="radio" label={`WebRTC · ${stream.label}`} value={stream.webrtc} />}
      </React.Fragment>)}
      {links.talk && <CopyField icon="volume" label="Talk" value={links.talk} />}
      <CopyField icon="code" label="Frigate sample" value={links.frigate} wide block />
    </div>
  </div>;
}

function CopyField({icon = "link", label, value, wide = false, block = false}) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    await copyText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  }
  return <div className={`copy-field ${wide ? "wide" : ""} ${block ? "yaml-field" : ""}`}>
    <RowLabel icon={icon}>{label}</RowLabel>
    {block
      ? <pre className="yaml-preview"><code>{highlightYaml(value)}</code></pre>
      : <div className="copy-value" title={value}>{value}</div>}
    <button className="copy-btn" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
  </div>;
}

function highlightYaml(value) {
  return String(value || "").split("\n").map((line, index, lines) => {
    const match = line.match(/^(\s*-?\s*)([A-Za-z0-9_]+)(:)(.*)$/);
    const children = [];
    if (match) {
      children.push(<span key="prefix" className="yaml-punc">{match[1]}</span>);
      children.push(<span key="key" className="yaml-key">{match[2]}</span>);
      children.push(<span key="colon" className="yaml-punc">{match[3]}</span>);
      const rest = match[4];
      const trimmed = rest.trim();
      const cls = /^(true|false|null)$/i.test(trimmed) ? "yaml-bool" : (/^-?\d+(\.\d+)?$/.test(trimmed) ? "yaml-number" : (trimmed ? "yaml-string" : ""));
      children.push(<span key="rest" className={cls}>{rest}</span>);
    } else {
      children.push(<span key="line">{line}</span>);
    }
    if (index < lines.length - 1) children.push("\n");
    return <React.Fragment key={index}>{children}</React.Fragment>;
  });
}

function MiniSwitch({icon = "link", label, checked, onChange, disabled = false, title = ""}) {
  return <div className="mini-toggle">
    <RowLabel icon={icon}>{label}</RowLabel>
    <label className="switch" title={title}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={e => onChange(e.target.checked)} />
      <span className="slider"></span>
    </label>
  </div>;
}

function UpdatePasswordModal({account, onClose, onSaved}) {
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  async function save() {
    setLoading(true);
    setErr("");
    try {
      await onSaved(password);
    } catch (e) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  }
  return <div className="modal-backdrop">
    <div className="modal">
      <div className="modal-head">
        <h2 className="panel-title">Update Imou Password</h2>
        <button className="btn icon" onClick={onClose}>×</button>
      </div>
      <div className="modal-body">
        {err && <div className="error">{err}</div>}
        <div className="muted">{account.email}</div>
        <div className="field">
          <label>Password</label>
          <input type="text" value={password} onChange={e => setPassword(e.target.value)} />
        </div>
      </div>
      <div className="modal-actions">
        <button className="btn" onClick={onClose} disabled={loading}>Cancel</button>
        <button className="btn primary" onClick={save} disabled={loading || !password}>{loading ? "Saving..." : "Save"}</button>
      </div>
    </div>
  </div>;
}

function AddAccountModal({onClose, onAdded}) {
  const [form, setForm] = useState({
    label: "",
    email: "",
    password: ""
  });
  const [captcha, setCaptcha] = useState(null);
  const [captchaCode, setCaptchaCode] = useState("");
  const [geelabUrl, setGeelabUrl] = useState("");
  const [challengeStatus, setChallengeStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const verifyingRef = useRef(false);
  const set = (k, v) => setForm({...form, [k]: v});

  useEffect(() => {
    if (!captcha || captcha.type !== "geetest4") return;
    let canceled = false;
    setErr("");
    verifyingRef.current = false;
    setChallengeStatus("Loading slider verification...");
    try {
      if (!captcha.captcha_id) throw new Error("Imou did not return a Geetest captchaId");
      const fp = /^[0-9a-f]{64}$/.test(localStorage.getItem("imou_gt_fp") || "")
        ? localStorage.getItem("imou_gt_fp")
        : randomHex(64);
      localStorage.setItem("imou_gt_fp", fp);
      const fpTs = localStorage.getItem("imou_gt_ts") || String(Date.now());
      localStorage.setItem("imou_gt_ts", fpTs);
      const args = {
        displayArea: "center",
        protocol: "https://",
        loading: "./gl4-loading.gif",
        captchaId: captcha.captcha_id,
        challenge: uuid4(),
        debug: false,
        language: "vi-vn",
        apiServers: [captcha.captcha_server || "cap-global.geelabapi.com"],
        timeout: 10000,
        clientVersion: "1.8.11",
        clientType: "android",
        mask: {outside: false},
        displayMode: 1,
        mi: {
          geeid: {bd: "$unknown", d: "$unknown", e: "$unknown", fp, ts: fpTs, ver: "1.0.0", client_type: "android"},
          packageName: "com.mm.android.smartlifeiot",
          displayName: "Imou%20Life",
          appVer: "10.0.6",
          build: "500527",
          clientVersion: "1.8.11",
        },
      };
      setGeelabUrl(`/geelab/gl4-index.html?data=${encodeURIComponent(JSON.stringify(args))}`);
    } catch (e) {
      setChallengeStatus("");
      setErr(e.message);
      return;
    }
    const onMessage = async event => {
      if (canceled || event.origin !== window.location.origin) return;
      const envelope = event.data || {};
      if (envelope.source !== "imou-geelab") return;
      const message = envelope.message || {};
      if (message.type === "ready") {
        setChallengeStatus("Slider verification is ready.");
        setTimeout(showGeelabSlider, 100);
        return;
      }
      if (message.type === "error" || message.type === "fail") {
        setChallengeStatus("");
        setErr(message.data?.msg || message.data?.message || JSON.stringify(message.data || message));
        return;
      }
      if (message.type !== "result" || verifyingRef.current) return;
      const result = message.data || {};
      verifyingRef.current = true;
      setLoading(true);
      setErr("");
      setChallengeStatus("Verifying slider result...");
      try {
        const res = await api("/api/accounts/geetest4", {
          method: "POST",
          body: JSON.stringify({pending_id: captcha.pending_id, result, source: "apk-gl4"}),
        });
        if (res.captcha_required) {
          setCaptchaCode("");
          setGeelabUrl("");
          setChallengeStatus("");
          setCaptcha(res);
          return;
        }
        onAdded();
      } catch (e) {
        verifyingRef.current = false;
        setErr(e.message);
      } finally {
        setLoading(false);
      }
    };
    window.addEventListener("message", onMessage);
    return () => {
      canceled = true;
      window.removeEventListener("message", onMessage);
      setGeelabUrl("");
    };
  }, [captcha]);

  async function submit() {
    setErr(""); setLoading(true);
    try {
      if (captcha) {
        await api("/api/accounts/captcha", {method: "POST", body: JSON.stringify({pending_id: captcha.pending_id, code: captchaCode})});
        onAdded();
        return;
      }
      const res = await api("/api/accounts/start", {method: "POST", body: JSON.stringify(form)});
      if (res.captcha_required) {
        setCaptchaCode("");
        setChallengeStatus("");
        setCaptcha(res);
      } else {
        onAdded();
      }
    } catch (e) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  }

  return <div className="modal-backdrop">
    <div className="modal">
      <div className="modal-head">
        <h2 className="panel-title">Add Imou Account</h2>
        <button className="btn icon" onClick={onClose}>×</button>
      </div>
      <div className="modal-body">
        {err && <div className="error">{err}</div>}
        {!captcha && <>
          <div className="grid">
            <Field label="Display name" value={form.label} onBlur={v => set("label", v)} placeholder="Farm / City house" />
            <Field label="Email" value={form.email} onBlur={v => set("email", v)} placeholder="you@example.com" />
          </div>
          <div className="field">
            <label>Imou password</label>
            <input type="text" value={form.password} onChange={e => set("password", e.target.value)} onKeyDown={e => { if (e.key === "Enter") submit(); }} />
          </div>
          <div className="muted">The account password is used only to sign in and fetch the camera list. It is not written to the config file.</div>
        </>}
        {captcha?.type === "image" && <>
          <div className="muted">Imou returned an image verification challenge for this API flow. Enter the code shown in the image to finish adding the account.</div>
          <img alt="Verification challenge" src={`data:image/jpeg;base64,${captcha.image}`} style={{maxWidth: "260px", border: "1px solid var(--line)", borderRadius: 8}} />
          <Field label="Verification code" value={captchaCode} onBlur={setCaptchaCode} />
        </>}
        {captcha?.type === "geetest4" && <div className="challenge-box">
          <div className="muted">Imou returned a slider verification challenge. Complete it below to continue the login.</div>
          {geelabUrl && <iframe id="geelab4-frame" className="geelab-frame" src={geelabUrl} onLoad={() => setTimeout(showGeelabSlider, 250)}></iframe>}
          {challengeStatus && <div className="muted">{challengeStatus}</div>}
          {!captcha.captcha_id && <div className="error">The server did not return a Geetest captchaId for this challenge.</div>}
        </div>}
        {captcha?.type === "unsupported" && <div className="error">
          Imou returned a verification challenge without captcha payload. Try again in a moment, or sign in once on the official app to refresh the trusted session.
        </div>}
      </div>
      <div className="modal-actions">
        <button className="btn" onClick={onClose}>Cancel</button>
        {(!captcha || captcha?.type === "image") && <button className="btn primary" onClick={submit} disabled={loading}>{loading ? "Working..." : (captcha ? "Verify" : "Add account")}</button>}
      </div>
    </div>
  </div>;
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
</script>
</body>
</html>
"""


def main() -> None:
    print(f"Imou bridge UI at http://{LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, threaded=True)


if __name__ == "__main__":
    main()
