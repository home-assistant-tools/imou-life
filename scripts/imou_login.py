#!/usr/bin/env python3
import base64
import hashlib
import hmac
import json
import os
import random
import string
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error


HOST = os.environ.get("IMOU_HOST", "app-v3.easy4ipcloud.com")
USERNAME = os.environ.get("IMOU_USERNAME", "")
PASSWORD = os.environ.get("IMOU_PASSWORD", "")
CAPTCHA_CODE = os.environ.get("IMOU_CAPTCHA_CODE", "")
CAPTCHA_REUSE = os.environ.get("IMOU_CAPTCHA_REUSE", "") == "1"
CAPTCHA_DIR = Path(os.environ.get("IMOU_CAPTCHA_DIR", "artifacts/imou_apk/mitm-work"))


def b64_md5(data: str) -> str:
    return base64.b64encode(hashlib.md5(data.encode()).digest()).decode()


def md5_hex(data: str) -> str:
    return hashlib.md5(data.encode()).hexdigest()


def nonce(n: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def client_ua() -> str:
    raw_b64 = os.environ.get("IMOU_CLIENT_UA_B64", "")
    if raw_b64:
        return raw_b64
    raw_json = os.environ.get("IMOU_CLIENT_UA_JSON", "")
    if raw_json:
        return base64.b64encode(raw_json.encode()).decode()
    ua = {
        "clientType": os.environ.get("IMOU_CLIENT_TYPE", "phone"),
        "clientVersion": os.environ.get("IMOU_CLIENT_VERSION", "V10.0.6"),
        "clientOV": os.environ.get("IMOU_CLIENT_OV", "Android 15"),
        "clientOS": os.environ.get("IMOU_CLIENT_OS", "Android"),
        "terminalModel": os.environ.get("IMOU_TERMINAL_MODEL", "SM-G998B"),
        "terminalId": os.environ.get("IMOU_TERMINAL_ID", "codex-script"),
        "appid": os.environ.get("IMOU_APPID", "Easy4ip"),
        "project": os.environ.get("IMOU_PROJECT", "Easy4ip"),
        "language": os.environ.get("IMOU_LANGUAGE", "vi_VN"),
        "clientProtocolVersion": os.environ.get("IMOU_PROTOCOL_VERSION", "V7.1.1"),
        "timezoneOffset": int(os.environ.get("IMOU_TIMEZONE_OFFSET", "-420")),
    }
    terminal_brand = os.environ.get("IMOU_TERMINAL_BRAND", "samsung")
    if terminal_brand:
        ua["terminalBrand"] = terminal_brand
    ttid = os.environ.get("IMOU_TTID", "")
    if ttid:
        ua["ttid"] = ttid
    terminal_name = os.environ.get("IMOU_TERMINAL_NAME", "")
    if terminal_name:
        ua["terminalName"] = terminal_name
    country = os.environ.get("IMOU_COUNTRY", "VN")
    if country:
        ua["country"] = country
    user_label = os.environ.get("IMOU_USER_LABEL", "")
    if user_label:
        ua["userLabel"] = user_label
    ua["darkMode"] = os.environ.get("IMOU_DARK_MODE", "false").lower() == "true"
    raw = json.dumps(ua, separators=(",", ":"), ensure_ascii=False)
    return base64.b64encode(raw.encode()).decode()


def saas_headers(path: str, body: str, apiver: str, username: str, key: str, session_id: str | None = None) -> dict[str, str]:
    content_type = "application/json; charset=utf-8"
    content_md5 = b64_md5(body)
    date = utc_now()
    n = nonce()
    cua = client_ua()
    canonical = (
        "POST\n"
        + path
        + "\n"
        + content_md5
        + "\n"
        + content_type
        + "\n"
        + "x-pcs-apiver:"
        + apiver
        + "\n"
        + "x-pcs-client-ua:"
        + cua
        + "\n"
        + "x-pcs-date:"
        + date
        + "\n"
        + "x-pcs-nonce:"
        + n
        + "\n"
    )
    if session_id:
        canonical += "x-pcs-session-id:" + session_id + "\n"
    canonical += "x-pcs-username:" + username + "\n"
    signature = base64.b64encode(hmac.new(key.encode(), canonical.encode(), hashlib.sha256).digest()).decode()
    headers = {
        "Content-Type": content_type,
        "Content-MD5": content_md5,
        "x-pcs-username": username,
        "x-pcs-apiver": apiver,
        "x-pcs-nonce": n,
        "x-pcs-date": date,
        "x-pcs-signature": signature,
        "x-pcs-client-ua": cua,
        "x-pcs-request-id": "",
    }
    if session_id:
        headers["x-pcs-session-id"] = session_id
    return headers


def post(api: str, apiver: str, data: dict, username: str, key: str, session_id: str | None = None) -> dict:
    path = "/pcs/v1/" + api
    body = json.dumps({"data": data}, separators=(",", ":"), ensure_ascii=False)
    req = request.Request(
        "https://" + HOST + path,
        data=body.encode(),
        headers=saas_headers(path, body, apiver, username, key, session_id),
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode()
    except error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {raw}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit(raw)


def captcha_paths() -> tuple[Path, Path]:
    return CAPTCHA_DIR / "imou-captcha.jpg", CAPTCHA_DIR / "imou-captcha.json"


def verify_image_captcha(challenge: dict) -> bool:
    verify_resp = post(
        "common.validcode.CheckImageValidCode",
        os.environ.get("IMOU_APIVER", "191204"),
        {
            "codeId": challenge["codeId"],
            "code": CAPTCHA_CODE,
            "usage": "Login",
            "captchaMetaData": "",
            "captchaId": challenge["captchaId"],
            "verifyToken": challenge["verifyToken"],
        },
        "account\\" + USERNAME,
        md5_hex(PASSWORD),
    )
    print(json.dumps({"step": "CaptchaVerify", "code": verify_resp.get("code"), "msg": verify_resp.get("msg"), "data": verify_resp.get("data")}, ensure_ascii=False))
    return verify_resp.get("code") == 0


def load_saved_captcha() -> dict | None:
    _, meta_path = captcha_paths()
    if not meta_path.exists():
        return None
    return json.loads(meta_path.read_text())


def save_image_captcha(resp: dict) -> bool:
    data = resp.get("data") or {}
    captcha = data.get("captchaData") or {}
    image = captcha.get("image")
    code_id = captcha.get("codeId")
    captcha_id = captcha.get("captchaId")
    verify_token = captcha.get("verifyToken")
    if not (image and code_id and captcha_id and verify_token):
        return False

    CAPTCHA_DIR.mkdir(parents=True, exist_ok=True)
    image_path, meta_path = captcha_paths()
    image_path.write_bytes(base64.b64decode(image))
    meta_path.write_text(json.dumps({"codeId": code_id, "captchaId": captcha_id, "verifyToken": verify_token}, ensure_ascii=False))
    print(json.dumps({"step": "Captcha", "image": str(image_path), "meta": str(meta_path), "codeId": code_id}, ensure_ascii=False))
    print("Set IMOU_CAPTCHA_CODE from the saved image and run again with IMOU_CAPTCHA_REUSE=1 within captcha expiry.", file=sys.stderr)
    return True

def maybe_verify_saved_captcha() -> bool:
    if not (CAPTCHA_CODE and CAPTCHA_REUSE):
        return False
    challenge = load_saved_captcha()
    if not challenge:
        print("No saved captcha metadata found.", file=sys.stderr)
        return False
    return verify_image_captcha(challenge)


def main() -> int:
    if not USERNAME or not PASSWORD:
        print("Set IMOU_USERNAME and IMOU_PASSWORD.", file=sys.stderr)
        return 2

    account_user = "account\\" + USERNAME
    if CAPTCHA_REUSE and not maybe_verify_saved_captcha():
        return 1

    token_resp = post(
        "user.account.GetToken",
        os.environ.get("IMOU_APIVER", "191204"),
        {"areaCode": os.environ.get("IMOU_AREA_CODE", ""), "gpsInfo": {"latitude": 0.0, "longitude": 0.0}},
        account_user,
        md5_hex(PASSWORD),
    )
    print(json.dumps({"step": "GetToken", "code": token_resp.get("code"), "msg": token_resp.get("msg")}, ensure_ascii=False))
    if token_resp.get("code") != 0:
        if token_resp.get("code") == 12110:
            save_image_captcha(token_resp)
            return 1
        else:
            print(json.dumps(token_resp, ensure_ascii=False, indent=2))
            return 1

    data = token_resp["data"]
    login_resp = post(
        "user.account.Login",
        os.environ.get("IMOU_APIVER", "191204"),
        {"timezoneOffset": -420},
        "token/" + data["username"],
        data["token"],
        data["sessionId"],
    )
    out = {
        "step": "Login",
        "code": login_resp.get("code"),
        "msg": login_resp.get("msg"),
        "sessionId": data.get("sessionId"),
        "username": data.get("username"),
    }
    login_data = login_resp.get("data") or {}
    for key in ("userId", "email", "phone", "nickname", "entryUrl", "entryUrlV2", "iotEntryUrlV2Host"):
        if key in login_data:
            out[key] = login_data[key]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if login_resp.get("code") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
