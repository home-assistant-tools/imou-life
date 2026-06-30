#!/usr/bin/env python3
"""Local web helper to log in to the Imou cloud and list devices.

Solves the hard parts interactively in the browser (the user types the captcha),
then calls the PCS login API and `device.list.BasicList`, and prints the camera
serials plus a ready-to-paste add-on `cameras:` block.

Run:
    python3 scripts/imou_login_site.py        # serves http://127.0.0.1:8099
Open the URL, enter the Imou account email + password, solve the captcha if
asked. Nothing is stored on disk; tokens live only in memory for the session.

PCS request signing (x-pcs-signature = HMAC-SHA256 over the canonical headers)
mirrors scripts/imou_login.py — see docs/cloud-api-surface.md.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import random
import string
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, parse, request

HOST = "app-sg1-v3.easy4ipcloud.com"
APIVER = "191204"
# 0.0.0.0 so Home Assistant ingress can reach it inside the add-on container.
LISTEN = (os.environ.get("IMOU_SITE_HOST", "127.0.0.1"), int(os.environ.get("IMOU_SITE_PORT", "8099")))

# in-memory state (single local user)
STATE: dict = {}


def b64_md5(s: str) -> str:
    return base64.b64encode(hashlib.md5(s.encode()).digest()).decode()


def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def nonce(n: int = 32) -> str:
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def client_ua() -> str:
    ua = {
        "clientType": "phone", "clientVersion": "V10.0.6", "clientOV": "Android 15",
        "clientOS": "Android", "terminalModel": "SM-G998B", "terminalId": "imou-login-site",
        "appid": "Easy4ip", "project": "Easy4ip", "language": "vi_VN",
        "clientProtocolVersion": "V7.1.1", "timezoneOffset": -420,
        "terminalBrand": "samsung", "country": "VN", "darkMode": False,
    }
    return base64.b64encode(json.dumps(ua, separators=(",", ":")).encode()).decode()


def post(api: str, data: dict, username: str, key: str, session_id: str | None = None) -> dict:
    path = "/pcs/v1/" + api
    body = json.dumps({"data": data}, separators=(",", ":"), ensure_ascii=False)
    content_md5 = b64_md5(body)
    date = utc_now()
    n = nonce()
    cua = client_ua()
    canonical = (f"POST\n{path}\n{content_md5}\napplication/json; charset=utf-8\n"
                 f"x-pcs-apiver:{APIVER}\nx-pcs-client-ua:{cua}\nx-pcs-date:{date}\n"
                 f"x-pcs-nonce:{n}\n")
    if session_id:
        canonical += f"x-pcs-session-id:{session_id}\n"
    canonical += f"x-pcs-username:{username}\n"
    sig = base64.b64encode(hmac.new(key.encode(), canonical.encode(), hashlib.sha256).digest()).decode()
    headers = {
        "Content-Type": "application/json; charset=utf-8", "Content-MD5": content_md5,
        "x-pcs-username": username, "x-pcs-apiver": APIVER, "x-pcs-nonce": n,
        "x-pcs-date": date, "x-pcs-signature": sig, "x-pcs-client-ua": cua,
        "x-pcs-request-id": "",
    }
    if session_id:
        headers["x-pcs-session-id"] = session_id
    req = request.Request("https://" + HOST + path, data=body.encode(), headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except error.HTTPError as exc:
        return json.loads(exc.read().decode(errors="replace"))


# ------------------------------- API steps --------------------------------
def get_token(email: str, password: str) -> dict:
    return post("user.account.GetToken", {"areaCode": "", "gpsInfo": {"latitude": 0.0, "longitude": 0.0}},
                "account\\" + email, md5_hex(password))


def verify_captcha(email: str, password: str, ch: dict, code: str) -> dict:
    return post("common.validcode.CheckImageValidCode",
                {"codeId": ch["codeId"], "code": code, "usage": "Login",
                 "captchaMetaData": "", "captchaId": ch["captchaId"], "verifyToken": ch["verifyToken"]},
                "account\\" + email, md5_hex(password))


def do_login(token_data: dict) -> dict:
    return post("user.account.Login", {"timezoneOffset": -420},
                "token/" + token_data["username"], token_data["token"], token_data["sessionId"])


def list_devices(token_data: dict) -> dict:
    return post("device.list.BasicList",
                {"familyId": "-1", "transferStr": "", "offset": 0, "limit": 128, "roomId": "-1"},
                token_data["username"], token_data["token"], token_data["sessionId"])


# ------------------------------- HTML views -------------------------------
PAGE = """<!doctype html><meta charset=utf-8><title>Imou login helper</title>
<style>body{{font-family:system-ui;max-width:780px;margin:40px auto;padding:0 16px}}
input,button{{font-size:16px;padding:8px;margin:4px 0}} input{{width:100%%;box-sizing:border-box}}
table{{border-collapse:collapse;width:100%%;margin-top:12px}} td,th{{border:1px solid #ccc;padding:6px;text-align:left;font-size:14px}}
pre{{background:#f4f4f4;padding:12px;overflow:auto}} .err{{color:#b00}} img{{border:1px solid #888}}</style>
<h2>Imou login helper</h2>{body}"""

FORM = """<form method=post action=login>
<p>Sign in to your Imou account. This runs locally and does not write account credentials to disk.</p>
<label>Email<input name=email type=email required value="{email}"></label>
<label>Password<input name=password type=password required></label>
<button>Sign in</button></form>{err}"""

CAPTCHA = """<form method=post action=captcha>
<p>Enter the verification code shown in the image:</p>
<img src="data:image/jpeg;base64,{img}"><br>
<label>Code<input name=code required autofocus></label>
<button>Verify</button></form>{err}"""


def render_devices(login: dict, devs: dict) -> str:
    d = (devs.get("data") or {})
    items = d.get("deviceList") or []
    rows = ["<tr><th>Name</th><th>Serial (deviceId)</th><th>Model</th><th>Role</th></tr>"]
    cams = []
    for it in items:
        did = it.get("deviceId", "")
        name = it.get("deviceName", "")
        rows.append("<tr><td>{}</td><td><code>{}</code></td><td>{}</td><td>{}</td></tr>".format(
            html.escape(name), html.escape(did), html.escape(it.get("productModel", "")), html.escape(it.get("role", ""))))
        slug = "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_") or "cam"
        cams.append(f"""  - name: {slug}
    mode: p2p
    serial: {did}
    username: admin
    password: ""
    channel: 1
    subtype: 0""")
    ld = login.get("data") or {}
    info = f"<p>Login OK — <b>{html.escape(ld.get('nickname',''))}</b> ({html.escape(ld.get('email',''))})</p>"
    yaml = "cameras:\n" + "\n".join(cams) if cams else "(no devices)"
    return (info + "<table>" + "".join(rows) + "</table>"
            + "<h3>Paste into the add-on config and fill each camera password:</h3><pre>" + html.escape(yaml) + "</pre>")


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, code: int = 200):
        out = PAGE.format(body=body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _form(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return {k: v[0] for k, v in parse.parse_qs(self.rfile.read(n).decode()).items()}

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        self._send(FORM.format(email="", err=""))

    def do_POST(self):
        if self.path == "/login":
            f = self._form()
            email, pw = f.get("email", ""), f.get("password", "")
            STATE["email"], STATE["password"] = email, pw
            r = get_token(email, pw)
            code = r.get("code")
            if code == 0:
                STATE["token_data"] = r["data"]
                return self._finish()
            if code == 12110:  # captcha required
                cap = ((r.get("data") or {}).get("captchaData") or {})
                STATE["captcha"] = {k: cap.get(k) for k in ("codeId", "captchaId", "verifyToken")}
                return self._send(CAPTCHA.format(img=cap.get("image", ""), err=""))
            return self._send(FORM.format(email=html.escape(email),
                              err=f'<p class=err>Error {code}: {html.escape(str(r.get("msg") or r))}</p>'))
        if self.path == "/captcha":
            f = self._form()
            v = verify_captcha(STATE["email"], STATE["password"], STATE["captcha"], f.get("code", ""))
            if v.get("code") != 0:
                return self._send(CAPTCHA.format(img="", err=f'<p class=err>Verification failed: {html.escape(str(v.get("msg")))}</p>'))
            r = get_token(STATE["email"], STATE["password"])
            if r.get("code") != 0:
                return self._send(FORM.format(email=html.escape(STATE["email"]),
                                  err=f'<p class=err>GetToken error {r.get("code")}</p>'))
            STATE["token_data"] = r["data"]
            return self._finish()
        self._send(FORM.format(email="", err=""), 404)

    def _finish(self):
        td = STATE["token_data"]
        login = do_login(td)
        if login.get("code") != 0:
            return self._send(FORM.format(email="", err=f'<p class=err>Login error {login.get("code")}: {html.escape(str(login.get("msg")))}</p>'))
        devs = list_devices(td)
        self._send(render_devices(login, devs))


def main():
    srv = ThreadingHTTPServer(LISTEN, Handler)
    print(f"Imou login helper at http://{LISTEN[0]}:{LISTEN[1]}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
