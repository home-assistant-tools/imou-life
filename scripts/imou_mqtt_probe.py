#!/usr/bin/env python3
"""Probe Imou cloud MQTT realtime push events.

This is an intentionally dependency-free MQTT 3.1.1 client. It mirrors the
Imou Android push flow far enough to:

1. Sign in through PCS (`GetToken` + `Login`).
2. Call `client_v2/auth/get` with a stable terminal identifier.
3. Connect to the returned MQTT broker and subscribe to the app topics.

Keep this script as a research/probe tool. It does not store credentials.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import random
import socket
import ssl
import string
import struct
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request


DEFAULT_HOST = os.environ.get("IMOU_CLOUD_HOST", "app-sg1-v3.easy4ipcloud.com")
DEFAULT_DISCOVERY_HOST = os.environ.get("IMOU_DISCOVERY_HOST", "app-v3.easy4ipcloud.com")
DEFAULT_APIVER = os.environ.get("IMOU_APIVER", "191204")


def md5_hex(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


def b64_md5(value: str) -> str:
    return base64.b64encode(hashlib.md5(value.encode()).digest()).decode()


def rand_text(n: int = 32) -> str:
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def nonce(n: int = 32) -> str:
    return f"{int(time.time() * 1000)}{rand_text(n)}"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def account_password_key(password: str) -> str:
    # The Android app signs GetToken with md5(md5(password)).
    return md5_hex(md5_hex(password))


def session_key(token: str) -> str:
    # The post-login PCS session signs API calls with md5(token).
    return md5_hex(token)


def pcs_ok(resp: dict[str, Any]) -> bool:
    return resp.get("code") in (0, 10000)


def host_from_url(value: str | None, fallback: str = DEFAULT_HOST) -> str:
    if not value:
        return fallback
    parsed = parse.urlparse(value if "://" in value else "https://" + value)
    return parsed.netloc or parsed.path or fallback


def parse_broker_addr(value: str) -> tuple[str, int]:
    addr = value.strip()
    if "://" in addr:
        parsed = parse.urlparse(addr)
        host = parsed.hostname or parsed.path
        port = parsed.port
    else:
        host, sep, port_text = addr.rpartition(":")
        if not sep or not host:
            host, port_text = addr, ""
        port = int(port_text) if port_text.isdigit() else None
    return host, port or 8883


def client_ua_b64(user_id: str, terminal_id: str) -> str:
    if os.environ.get("IMOU_CLIENT_UA_B64"):
        return os.environ["IMOU_CLIENT_UA_B64"]
    if os.environ.get("IMOU_CLIENT_UA_JSON"):
        return base64.b64encode(os.environ["IMOU_CLIENT_UA_JSON"].encode()).decode()

    ua = OrderedDict(
        [
            ("country", os.environ.get("IMOU_COUNTRY", "VN")),
            ("userLabel", os.environ.get("IMOU_USER_LABEL", "1")),
            ("terminalBrand", os.environ.get("IMOU_TERMINAL_BRAND", "samsung")),
            ("project", os.environ.get("IMOU_PROJECT", "Base")),
            ("clientOS", os.environ.get("IMOU_CLIENT_OS", "Android")),
            ("language", os.environ.get("IMOU_LANGUAGE", "vi_VN")),
            ("terminalId", terminal_id),
            ("clientVersion", os.environ.get("IMOU_CLIENT_VERSION", "V10.0.6")),
            ("ttid", os.environ.get("IMOU_TTID", "")),
            ("terminalModel", os.environ.get("IMOU_TERMINAL_MODEL", "SM-G998B")),
            ("terminalName", os.environ.get("IMOU_TERMINAL_NAME", "samsung SM-G998B")),
            ("clientType", os.environ.get("IMOU_CLIENT_TYPE", "phone")),
            ("clientProtocolVersion", os.environ.get("IMOU_CLIENT_PROTOCOL_VERSION", "V9.7.2")),
            ("timezoneOffset", os.environ.get("IMOU_TIMEZONE_OFFSET", "25200")),
            ("clientOV", os.environ.get("IMOU_CLIENT_OV", "Android 15")),
            ("appid", os.environ.get("IMOU_APPID", "easy4ipbaseapp")),
            ("darkMode", os.environ.get("IMOU_DARK_MODE", "light")),
            ("userId", user_id),
        ]
    )
    if not ua["ttid"]:
        ua.pop("ttid")
    raw = json.dumps(ua, separators=(",", ":"), ensure_ascii=False)
    return base64.b64encode(raw.encode()).decode()


def pcs_headers(path: str, body: str, apiver: str, username: str, key: str, session_id: str | None = None) -> dict[str, str]:
    content_type = "application/json; charset=utf-8"
    content_md5 = b64_md5(body)
    date = utc_now()
    n = nonce()
    cua = os.environ.get("IMOU_CLIENT_UA_B64") or client_ua_b64(os.environ.get("IMOU_USER_ID", ""), os.environ.get("IMOU_TERMINAL_ID", "codex-mqtt-probe"))
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


def pcs_post(host: str, api: str, apiver: str, data: dict[str, Any], username: str, key: str, session_id: str | None = None) -> dict[str, Any]:
    path = "/pcs/v1/" + api
    body = json.dumps({"data": data}, separators=(",", ":"), ensure_ascii=False)
    req = request.Request(
        "https://" + host + path,
        data=body.encode(),
        headers=pcs_headers(path, body, apiver, username, key, session_id),
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode()
    except error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {api}: {raw}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON response from {api}: {raw}") from exc


def load_session(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if "token_data" in data or "login_data" in data:
        return data
    return {"token_data": data.get("token") or data.get("tokenData") or data, "login_data": data.get("login") or data.get("loginData") or {}}


def login(email: str, password: str, host: str, discovery_host: str, apiver: str) -> dict[str, Any]:
    token = pcs_post(
        discovery_host,
        "user.account.GetToken",
        apiver,
        {"areaCode": os.environ.get("IMOU_AREA_CODE", ""), "gpsInfo": {"latitude": 0.0, "longitude": 0.0}},
        "account\\" + email,
        account_password_key(password),
    )
    if not pcs_ok(token):
        raise RuntimeError(f"GetToken failed: {json.dumps(redact(token), ensure_ascii=False)}")
    token_data = token.get("data") or {}
    missing = [key for key in ("username", "token", "sessionId") if not token_data.get(key)]
    if missing:
        raise RuntimeError(f"GetToken returned no login token; missing {missing}, keys={sorted(token_data.keys())}")

    login_host = host_from_url(token_data.get("entryUrlV2") or token_data.get("entryUrl"), host)
    logged_in = pcs_post(
        login_host,
        "user.account.Login",
        apiver,
        {"timezoneOffset": int(os.environ.get("IMOU_LOGIN_TIMEZONE_OFFSET", "-420"))},
        "uuid\\" + token_data["username"],
        session_key(token_data["token"]),
        token_data["sessionId"],
    )
    if not pcs_ok(logged_in):
        raise RuntimeError(f"Login failed: {json.dumps(redact(logged_in), ensure_ascii=False)}")
    return {"token_data": token_data, "login_data": logged_in.get("data") or {}, "host": login_host}


def mqtt_identifier(email: str) -> str:
    env = os.environ.get("IMOU_MQTT_IDENTIFIER") or os.environ.get("IMOU_TERMINAL_ID")
    if env:
        return env
    digest = hashlib.sha256((email or os.uname().nodename).encode()).hexdigest()
    return digest[:16]


def get_mqtt_info(session: dict[str, Any], password: str, host: str, identifier: str, apiver: str) -> dict[str, Any]:
    token_data = session["token_data"]
    login_data = session.get("login_data") or {}
    mqtt_host = (
        host_from_url(login_data.get("iotEntryUrlV2") or login_data.get("iotEntryUrlV2Host") or login_data.get("entryUrlV2") or login_data.get("entryUrl"), host)
        if login_data
        else host
    )
    resp = pcs_post(
        mqtt_host,
        "client_v2/auth/get",
        apiver,
        {"identifier": identifier},
        "uuid\\" + token_data["username"],
        session_key(token_data["token"]),
        token_data["sessionId"],
    )
    if not pcs_ok(resp):
        raise RuntimeError(f"GetMQTTInfo failed: {json.dumps(redact(resp), ensure_ascii=False)}")
    data = resp.get("data") or {}
    if not data.get("clientId") or not data.get("username") or not data.get("mqttServer"):
        raise RuntimeError(f"GetMQTTInfo response is missing fields: {json.dumps(redact(resp), ensure_ascii=False)}")
    data["_host"] = mqtt_host
    data["_account_password_key"] = account_password_key(password)
    data["_plain_password"] = password
    data["_session_key"] = session_key(token_data["token"])
    return data


def mqtt_signing_key(mode: str, info: dict[str, Any]) -> str:
    if mode == "account-password-key":
        return info["_account_password_key"]
    if mode == "plain-password":
        return info["_plain_password"]
    if mode == "response-password":
        return str(info.get("password") or "")
    if mode == "response-salt":
        return str(info.get("salt") or "")
    if mode == "session-key":
        return info["_session_key"]
    return mode


def mqtt_password_json(uuid_username: str, ua: str, signing_key: str, connect_type: str = "main") -> str:
    date = utc_now()
    n = rand_text(32)
    canonical = (
        "x-pcs-client-ua:"
        + ua
        + "\n"
        + "x-pcs-date:"
        + date
        + "\n"
        + "x-pcs-nonce:"
        + n
        + "\n"
        + "x-pcs-username:"
        + uuid_username
        + "\n"
    )
    signature = base64.b64encode(hmac.new(signing_key.encode(), canonical.encode(), hashlib.sha256).digest()).decode()
    payload = OrderedDict(
        [
            ("x-pcs-username", uuid_username),
            ("x-pcs-nonce", n),
            ("x-pcs-date", date),
            ("x-pcs-client-ua", ua),
            ("x-pcs-signature", signature),
            ("x-pcs-conn-type", connect_type),
        ]
    )
    return json.dumps(payload, separators=(",", ":"))


def enc_str(value: str) -> bytes:
    data = value.encode()
    return struct.pack("!H", len(data)) + data


def enc_remaining_length(length: int) -> bytes:
    out = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length > 0:
            digit |= 0x80
        out.append(digit)
        if length == 0:
            return bytes(out)


def mqtt_packet(packet_type_flags: int, body: bytes) -> bytes:
    return bytes([packet_type_flags]) + enc_remaining_length(len(body)) + body


def read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("socket closed")
        buf.extend(chunk)
    return bytes(buf)


def read_packet(sock: socket.socket) -> tuple[int, bytes]:
    first = read_exact(sock, 1)[0]
    multiplier = 1
    remaining = 0
    while True:
        digit = read_exact(sock, 1)[0]
        remaining += (digit & 127) * multiplier
        if (digit & 128) == 0:
            break
        multiplier *= 128
        if multiplier > 128**4:
            raise RuntimeError("malformed MQTT remaining length")
    return first, read_exact(sock, remaining)


def mqtt_connect(sock: socket.socket, client_id: str, username: str, password: str, keepalive: int = 60) -> None:
    variable = enc_str("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", keepalive)
    payload = enc_str(client_id) + enc_str(username) + enc_str(password)
    sock.sendall(mqtt_packet(0x10, variable + payload))
    first, body = read_packet(sock)
    if first != 0x20 or len(body) != 2:
        raise RuntimeError(f"unexpected CONNACK packet: type=0x{first:02x} body={body.hex()}")
    if body[1] != 0:
        raise RuntimeError(f"MQTT CONNACK rejected with code {body[1]}")


def mqtt_subscribe(sock: socket.socket, topics: list[str], packet_id: int = 1) -> None:
    payload = bytearray(struct.pack("!H", packet_id))
    for topic in topics:
        payload.extend(enc_str(topic))
        payload.append(0)
    sock.sendall(mqtt_packet(0x82, bytes(payload)))
    first, body = read_packet(sock)
    if first != 0x90:
        raise RuntimeError(f"unexpected SUBACK packet: type=0x{first:02x} body={body.hex()}")
    codes = list(body[2:])
    if any(code == 0x80 for code in codes):
        raise RuntimeError(f"SUBACK failure: {codes}")


def parse_publish(first: int, body: bytes) -> tuple[str, bytes]:
    if not body or (first >> 4) != 3:
        raise RuntimeError("not a PUBLISH packet")
    topic_len = struct.unpack("!H", body[:2])[0]
    topic = body[2 : 2 + topic_len].decode(errors="replace")
    pos = 2 + topic_len
    qos = (first >> 1) & 0x03
    if qos:
        pos += 2
    return topic, body[pos:]


def format_payload(payload: bytes, raw: bool) -> str:
    text = payload.decode(errors="replace")
    if raw:
        return text
    try:
        return json.dumps(redact(json.loads(text)), ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        return text


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lower = str(key).lower()
            if any(marker in lower for marker in ("password", "token", "signature", "sessionid")):
                out[key] = "***"
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def run_probe(args: argparse.Namespace) -> int:
    email = args.email or os.environ.get("IMOU_USERNAME", "")
    password = args.password or os.environ.get("IMOU_PASSWORD", "")
    if args.session:
        session = load_session(Path(args.session))
        if not password:
            password = os.environ.get("IMOU_MQTT_SIGNING_PASSWORD", "")
    else:
        if not email or not password:
            print("Set IMOU_USERNAME and IMOU_PASSWORD, or pass --session plus IMOU_MQTT_SIGNING_PASSWORD.", file=sys.stderr)
            return 2
        session = login(email, password, args.host, args.discovery_host, args.apiver)

    token_data = session["token_data"]
    login_data = session.get("login_data") or {}
    user_id = str(login_data.get("userId") or token_data.get("username") or "")
    identifier = args.identifier or mqtt_identifier(email or user_id)
    info = get_mqtt_info(session, password, args.host, identifier, args.apiver)
    server = info["mqttServer"]
    ssl_addr = server.get("sslAddr") or server.get("sslHost") or server.get("tcpAddr")
    if not ssl_addr:
        raise RuntimeError(f"No MQTT server address in response: {json.dumps(redact(info), ensure_ascii=False)}")
    broker_host, broker_port = parse_broker_addr(str(ssl_addr))

    uuid_username = "uuid\\" + user_id if user_id and not user_id.startswith("uuid") else user_id
    ua = client_ua_b64(user_id, identifier)
    key = mqtt_signing_key(args.key_mode, info)
    if not key:
        raise RuntimeError(f"Empty MQTT signing key for mode {args.key_mode}")
    password_json = mqtt_password_json(uuid_username, ua, key, args.connect_type)
    topics = [topic.strip() for topic in args.topics.split(",") if topic.strip()]

    print(
        json.dumps(
            {
                "step": "mqtt_info",
                "pcs_host": info.get("_host"),
                "broker": f"{broker_host}:{broker_port}",
                "clientId": info.get("clientId"),
                "connectUsername": info.get("username"),
                "authUsername": uuid_username,
                "identifier": identifier,
                "topics": topics,
                "keyMode": args.key_mode,
            },
            ensure_ascii=False,
        )
    )

    raw_sock = socket.create_connection((broker_host, broker_port), timeout=args.timeout)
    ctx = ssl.create_default_context()
    if args.insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with ctx.wrap_socket(raw_sock, server_hostname=broker_host) as sock:
        sock.settimeout(args.timeout)
        mqtt_connect(sock, str(info["clientId"]), str(info["username"]), password_json)
        print(json.dumps({"step": "mqtt_connected"}, ensure_ascii=False))
        mqtt_subscribe(sock, topics)
        print(json.dumps({"step": "mqtt_subscribed", "topics": topics}, ensure_ascii=False))
        deadline = time.time() + args.listen_seconds
        while time.time() < deadline:
            try:
                first, body = read_packet(sock)
            except socket.timeout:
                sock.sendall(mqtt_packet(0xC0, b""))
                continue
            packet_type = first >> 4
            if packet_type == 3:
                topic, payload = parse_publish(first, body)
                print(json.dumps({"step": "publish", "topic": topic, "payload": format_payload(payload, args.raw)}, ensure_ascii=False))
            elif packet_type == 13:
                print(json.dumps({"step": "pingresp"}, ensure_ascii=False))
            else:
                print(json.dumps({"step": "packet", "type": packet_type, "bytes": len(body)}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default=os.environ.get("IMOU_USERNAME", ""))
    parser.add_argument("--password", default=os.environ.get("IMOU_PASSWORD", ""))
    parser.add_argument("--session", help="JSON containing token_data/login_data from a successful login")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--discovery-host", default=DEFAULT_DISCOVERY_HOST)
    parser.add_argument("--apiver", default=DEFAULT_APIVER)
    parser.add_argument("--identifier", default=os.environ.get("IMOU_MQTT_IDENTIFIER", ""))
    parser.add_argument("--topics", default=os.environ.get("IMOU_MQTT_TOPICS", "iot_response,android_iot_property,iot_request"))
    parser.add_argument("--listen-seconds", type=int, default=int(os.environ.get("IMOU_MQTT_LISTEN_SECONDS", "120")))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("IMOU_MQTT_TIMEOUT", "25")))
    parser.add_argument("--connect-type", choices=("main", "stream"), default=os.environ.get("IMOU_MQTT_CONNECT_TYPE", "main"))
    parser.add_argument(
        "--key-mode",
        default=os.environ.get("IMOU_MQTT_KEY_MODE", "account-password-key"),
        help="account-password-key, plain-password, response-password, response-salt, session-key, or a literal HMAC key",
    )
    parser.add_argument("--insecure", action="store_true", default=os.environ.get("IMOU_MQTT_INSECURE") == "1")
    parser.add_argument("--raw", action="store_true", help="print MQTT payloads without JSON redaction")
    try:
        return run_probe(parser.parse_args())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
