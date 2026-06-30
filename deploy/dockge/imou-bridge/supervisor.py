#!/usr/bin/env python3
"""Imou P2P Bridge supervisor.

Per configured camera:
  - mode "p2p": run a DHP2P tunnel (serial -> 127.0.0.1:<tunnel_port> -> camera:554)
  - mode "lan": no tunnel; go2rtc sources the camera directly on the LAN

A bundled go2rtc consumes each source once and restreams RTSP/WebRTC/HLS to many
consumers (Home Assistant, Frigate) — this also works around DHP2P being
single-client. ONVIF shims expose each enabled camera as a local ONVIF endpoint.
"""

from __future__ import annotations

import json
import os
import re
import signal
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import dvrip

OPTIONS_PATH = Path(os.environ.get("IMOU_BRIDGE_OPTIONS", "/data/options.json"))
GO2RTC_CONFIG = Path(os.environ.get("IMOU_GO2RTC_CONFIG", "/data/go2rtc.yaml"))
GO2RTC_BIN = os.environ.get("IMOU_GO2RTC_BIN", "/opt/go2rtc")
STATUS_PATH = Path(os.environ.get("IMOU_BRIDGE_STATUS", "/data/status.json"))

REDACTIONS = (
    (re.compile(r'(PasswordDigest=")[^"]+'), r"\1<redacted>"),
    (re.compile(r"(<Token>)[^<]+"), r"\1<redacted>"),
    (re.compile(r"(/relay/start/)[^\s]+"), r"\1<redacted>"),
    (re.compile(r"(rtsp://[^:]+:)[^@]+(@)"), r"\1<redacted>\2"),
)


def log(message: str) -> None:
    print(message, flush=True)


def redact(line: str) -> str:
    for pattern, replacement in REDACTIONS:
        line = pattern.sub(replacement, line)
    return line


def display_cmd(cmd: list[str]) -> str:
    safe: list[str] = []
    hide_next = False
    for item in cmd:
        if hide_next:
            safe.append("<redacted>")
            hide_next = False
            continue
        safe.append(redact(item))
        if item == "--password":
            hide_next = True
    return " ".join(safe)


def slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower()).strip("_") or "camera"


def load_options() -> dict[str, Any]:
    if not OPTIONS_PATH.exists():
        raise SystemExit(f"Options file not found: {OPTIONS_PATH}")
    return json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))


@dataclass
class Camera:
    name: str
    display_name: str
    slug: str
    mode: str
    username: str
    password: str
    channel: int
    subtype: int
    serial: str = ""
    host: str = ""
    relay: bool = False
    ptz: bool = False
    talk: bool = True
    engine: str = ""
    warm: bool = True
    streams: list[dict[str, Any]] = field(default_factory=list)
    tunnel_port: int = 0  # assigned for p2p cameras (stream, ->554)
    ptz_port: int = 0     # assigned for p2p ptz cameras (DVRIP, ->37777)
    onvif_port: int = 0   # assigned for ptz cameras (ONVIF shim for Frigate)
    dlna_port: int = 0    # assigned for talk media renderer
    invalid_reason: str = ""

    def enabled_streams(self) -> list[dict[str, Any]]:
        streams = [s for s in self.streams if s.get("enabled", True)]
        if not streams:
            streams = [{"id": "ch1_sub0", "label": "Main", "channel": self.channel, "subtype": self.subtype}]
        return streams

    def realmonitor_path(self, stream: dict[str, Any] | None = None) -> str:
        stream = stream or self.enabled_streams()[0]
        channel = int(stream.get("channel", self.channel) or self.channel)
        subtype = int(stream.get("subtype", self.subtype) or self.subtype)
        return f"/cam/realmonitor?channel={channel}&subtype={subtype}"

    def source_url(self, stream: dict[str, Any] | None = None) -> str:
        user = quote(self.username, safe="")
        pw = quote(self.password, safe="")
        host = f"127.0.0.1:{self.tunnel_port}" if self.mode == "p2p" else f"{self.host}:554"
        return f"rtsp://{user}:{pw}@{host}{self.realmonitor_path(stream)}"

    def stream_slug(self, stream: dict[str, Any], *, primary: bool = False) -> str:
        if primary:
            return self.slug
        stream_id = str(stream.get("id") or f"ch{stream.get('channel', 1)}_sub{stream.get('subtype', 0)}")
        return f"{self.slug}_{slugify(stream_id)}"

    def onvif_streams(self, rtsp_port: int, advertised_host: str) -> list[dict[str, Any]]:
        result = []
        for index, stream in enumerate(self.enabled_streams()):
            primary = index == 0
            result.append({
                "token": f"profile{index}",
                "source_token": f"vsrc{int(stream.get('channel', 1) or 1)}",
                "encoder_token": f"venc{index}",
                "name": str(stream.get("label") or f"Channel {stream.get('channel', 1)} stream {stream.get('subtype', 0)}"),
                "channel": int(stream.get("channel", 1) or 1),
                "subtype": int(stream.get("subtype", 0) or 0),
                "uri": f"rtsp://{advertised_host}:{rtsp_port}/{self.stream_slug(stream, primary=primary)}",
            })
        return result


def normalize_streams(raw: dict[str, Any]) -> list[dict[str, Any]]:
    streams = raw.get("streams")
    if not isinstance(streams, list) or not streams:
        streams = [
            {"id": f"ch{int(raw.get('channel', 1) or 1)}_sub{int(raw.get('subtype', 0) or 0)}",
             "label": "Main", "channel": int(raw.get("channel", 1) or 1), "subtype": int(raw.get("subtype", 0) or 0), "enabled": True}
        ]
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in streams:
        if not isinstance(item, dict):
            continue
        try:
            channel = max(1, int(item.get("channel", 1) or 1))
            subtype = max(0, int(item.get("subtype", 0) or 0))
        except (TypeError, ValueError):
            continue
        stream_id = str(item.get("id") or f"ch{channel}_sub{subtype}")
        if stream_id in seen:
            continue
        seen.add(stream_id)
        normalized.append({
            "id": stream_id,
            "label": str(item.get("label") or f"Channel {channel} stream {subtype}"),
            "channel": channel,
            "subtype": subtype,
            "enabled": bool(item.get("enabled", True)),
        })
    return normalized or [{"id": "ch1_sub0", "label": "Main", "channel": 1, "subtype": 0, "enabled": True}]


def parse_cameras(options: dict[str, Any], base_port: int) -> list[Camera]:
    cams: list[Camera] = []
    next_port = base_port
    for index, raw in enumerate(options.get("cameras", [])):
        name = str(raw.get("name", "")).strip()
        mode = str(raw.get("mode", "p2p")).strip().lower()
        if not name:
            raise SystemExit(f"Camera #{index + 1} is missing 'name'")
        if mode not in ("p2p", "lan"):
            raise SystemExit(f"Camera '{name}': mode must be 'p2p' or 'lan'")
        if mode == "p2p" and not raw.get("serial"):
            raise SystemExit(f"Camera '{name}': p2p mode needs 'serial'")
        if mode == "lan" and not raw.get("host"):
            raise SystemExit(f"Camera '{name}': lan mode needs 'host'")
        cam = Camera(
            name=name,
            display_name=str(raw.get("display_name") or name),
            slug=slugify(name),
            mode=mode,
            username=str(raw.get("username", "admin")),
            password=str(raw.get("password", "")),
            channel=int(raw.get("channel", 1)),
            subtype=int(raw.get("subtype", 0)),
            serial=str(raw.get("serial", "")),
            host=str(raw.get("host", "")),
            relay=bool(raw.get("relay", False)),
            ptz=bool(raw.get("ptz", True)),
            talk=bool(raw.get("talk", True)),
            engine=str(raw.get("engine", "")).strip().lower(),
            warm=bool(raw.get("warm", True)),
            streams=normalize_streams(raw),
        )
        if mode == "p2p" and not cam.password:
            cam.invalid_reason = "Camera password is required for remote P2P/relay streaming"
        if mode == "p2p":
            cam.tunnel_port = next_port
            next_port += 1
            if cam.ptz:
                cam.ptz_port = next_port  # second tunnel -> 37777 for DVRIP/PTZ
                next_port += 1
        cams.append(cam)
    return cams


class RuntimeStatus:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {"cameras": {}}

    def set_camera(self, cam: Camera, state: str, message: str = "") -> None:
        payload = {
            "state": state,
            "message": message,
            "updated_at": int(time.time()),
            "name": cam.name,
            "slug": cam.slug,
            "serial": cam.serial,
        }
        with self.lock:
            for key in {cam.name, cam.slug, cam.serial}:
                if key:
                    self.data.setdefault("cameras", {})[key] = payload
            self._write_locked()

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)


def tunnel_failure_status(code: int | None, recent: list[str]) -> tuple[str, str]:
    text = "\n".join(recent)
    if "username/password are required" in text:
        return "error", "Camera password is required for remote P2P/relay streaming"
    if re.search(r"password|auth|login|credential|unauthori[sz]ed", text, re.IGNORECASE):
        return "error", "Camera authentication failed; check the camera username/password"
    if "DH response 404" in text or "404: Not Found" in text:
        return "error", "P2P cloud cannot locate this device right now (404)"
    if "TimeoutError" in text or "timed out" in text.lower():
        return "error", "P2P tunnel timed out; check camera password, relay route, or camera network"
    message = recent[-1] if recent else f"Tunnel exited with code {code}"
    return "restarting", message


def probe_ptz(cam: Camera, host: str, port: int) -> bool:
    """Confirm PTZ support with the camera's DVRIP RPC before exposing controls."""
    last_error = None
    for attempt in range(1, 11):
        cli = None
        try:
            cli = dvrip.DvripClient(host, cam.username, cam.password, port=port, timeout=4)
            cli.login()
            result = cli.rpc("ptz.factory.instance", {"channel": max(0, cam.channel - 1)})
            obj = result.get("result")
            if obj is not None and obj is not False:
                log(f"[ptz:{cam.name}] supported via {host}:{port} object={obj}")
                return True
            last_error = result
        except Exception as exc:
            last_error = exc
        finally:
            try:
                if cli:
                    cli.close()
            except Exception:
                pass
        time.sleep(1)
    log(f"[ptz:{cam.name}] disabled; ptz.factory.instance failed via {host}:{port}: {last_error}")
    return False


def write_go2rtc_config(cameras: list[Camera], go2rtc: dict[str, Any], log_level: str) -> None:
    rtsp_port = int(go2rtc.get("rtsp_port", 8554))
    api_port = int(go2rtc.get("api_port", 1984))
    webrtc_port = int(go2rtc.get("webrtc_port", 8555))
    lines = [
        "log:",
        f"  level: {log_level}",
        "api:",
        f'  listen: ":{api_port}"',
        "rtsp:",
        f'  listen: ":{rtsp_port}"',
        "webrtc:",
        "  listen: \":%d\"" % webrtc_port,
        "streams:",
    ]
    for cam in cameras:
        # main source + an ffmpeg-restream fallback for finicky decoders
        for index, stream in enumerate(cam.enabled_streams()):
            stream_name = cam.stream_slug(stream, primary=index == 0)
            lines.append(f"  {stream_name}:")
            lines.append(f"    - {cam.source_url(stream)}")
    GO2RTC_CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"[go2rtc] wrote config with {len(cameras)} stream(s) -> {GO2RTC_CONFIG}")


def rtsp_describe(host: str, port: int, stream: str, timeout: float = 8.0) -> tuple[bool, str]:
    request = (
        f"DESCRIBE rtsp://{host}:{port}/{stream} RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "Accept: application/sdp\r\n"
        "User-Agent: imou-bridge-supervisor\r\n"
        "\r\n"
    ).encode("ascii")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\r\n\r\n" in b"".join(chunks):
                    break
    except OSError as exc:
        return False, f"RTSP probe failed: {exc}"
    response = b"".join(chunks).decode("utf-8", "replace")
    first = response.splitlines()[0] if response else "empty RTSP response"
    if re.search(r"RTSP/\d\.\d\s+200\b", first):
        return True, f"RTSP stream ready: {stream}"
    return False, first


def start_stream_watchdog(stop: threading.Event, cameras: list[Camera], rtsp_port: int,
                          status: RuntimeStatus, interval: int) -> threading.Thread | None:
    if not cameras:
        return None

    def worker() -> None:
        stop.wait(8)
        while not stop.is_set():
            for cam in cameras:
                ok, message = rtsp_describe("127.0.0.1", rtsp_port, cam.slug)
                if ok:
                    status.set_camera(cam, "ready", message)
                elif "404" in message:
                    status.set_camera(cam, "error", "RTSP stream is not available yet; retrying automatically")
                else:
                    status.set_camera(cam, "restarting", f"{message}; retrying automatically")
                if stop.wait(1):
                    return
            stop.wait(interval)

    thread = threading.Thread(target=worker, name="stream-watchdog", daemon=True)
    thread.start()
    return thread


def warm_stream_entries(cameras: list[Camera], *, all_streams: bool) -> list[tuple[Camera, str]]:
    entries: list[tuple[Camera, str]] = []
    for cam in cameras:
        if not cam.warm:
            continue
        streams = cam.enabled_streams()
        selected = streams if all_streams else streams[:1]
        for index, stream in enumerate(selected):
            entries.append((cam, cam.stream_slug(stream, primary=index == 0)))
    return entries


# ----------------------------- process workers -----------------------------
class Proc(threading.Thread):
    """Generic restart-on-exit process runner with line logging."""

    def __init__(self, name: str, cmd: list[str], *, stop: threading.Event,
                 restart_seconds: int, verbose: bool, on_line=None, on_start=None,
                 on_exit=None, env: dict | None = None) -> None:
        super().__init__(daemon=True)
        self.name = name
        self.cmd = cmd
        self.stop = stop
        self.restart_seconds = max(1, restart_seconds)
        self.verbose = verbose
        self.on_line = on_line
        self.on_start = on_start
        self.on_exit = on_exit
        self.env = env
        self.proc: subprocess.Popen[str] | None = None

    def terminate(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def run(self) -> None:
        run_env = {**os.environ, **self.env} if self.env else None
        while not self.stop.is_set():
            recent: list[str] = []
            log(f"[{self.name}] starting: {display_cmd(self.cmd)}")
            if self.on_start:
                self.on_start()
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, bufsize=1,
                                         env=run_env)
            assert self.proc.stdout is not None
            for raw in self.proc.stdout:
                line = redact(raw.rstrip("\n"))
                recent.append(line)
                recent = recent[-8:]
                if self.on_line:
                    self.on_line(line)
                if self.verbose:
                    log(f"[{self.name}] {line}")
                if self.stop.is_set():
                    break
            code = self.proc.poll()
            if code is None:
                self.terminate()
                try:
                    code = self.proc.wait(timeout=10)
                except Exception:
                    code = -1
            if self.stop.is_set():
                break
            if code and not self.verbose and recent:
                for line in recent:
                    log(f"[{self.name}] {line}")
            if self.on_exit:
                self.on_exit(code, recent)
            log(f"[{self.name}] exited ({code}); restart in {self.restart_seconds}s")
            self.stop.wait(self.restart_seconds)


def main() -> int:
    options = load_options()
    log_level = str(options.get("log_level", "info"))
    bridge = options.get("bridge", {})
    go2rtc_opts = options.get("go2rtc", {})
    rtsp_port = int(go2rtc_opts.get("rtsp_port", 8554))
    base_port = int(bridge.get("base_port", 8600))
    restart_seconds = int(bridge.get("restart_seconds", 5))
    verbose = bool(bridge.get("verbose", False))
    engine = str(bridge.get("engine", "python")).strip().lower()
    binary = str(bridge.get("binary", "/opt/dh-p2p/dh-p2p"))
    python_bridge = str(bridge.get("python_bridge", "/opt/imou-p2p-bridge/imou_dhp2p.py"))
    warm_enabled = bool(bridge.get("warm_streams", True))
    warm_all_streams = bool(bridge.get("warm_all_streams", False))
    warm_restart_seconds = max(restart_seconds, int(bridge.get("warm_restart_seconds", 30)))
    ffmpeg_bin = str(bridge.get("ffmpeg", os.environ.get("IMOU_FFMPEG_BIN", "ffmpeg")))

    cameras = parse_cameras(options, base_port)
    status = RuntimeStatus(STATUS_PATH)
    for cam in cameras:
        if cam.invalid_reason:
            status.set_camera(cam, "error", cam.invalid_reason)
    active_cameras = [cam for cam in cameras if not cam.invalid_reason]
    if not active_cameras:
        log("[supervisor] no enabled cameras; starting UI with an empty go2rtc config")
    write_go2rtc_config(active_cameras, go2rtc_opts, log_level)

    stop = threading.Event()
    workers: list[Proc] = []

    def handle_signal(_s, _f) -> None:
        stop.set()
        for w in workers:
            w.terminate()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    def p2p_tunnel_cmd(cam: Camera, local_port: int, remote_port: int) -> tuple[list[str], dict[str, str] | None]:
        selected_engine = cam.engine or engine
        if selected_engine == "rust":
            cmd = [binary, "-p", f"127.0.0.1:{local_port}:{remote_port}"]
            if cam.relay:
                cmd.append("--relay")
            if cam.username and cam.password:
                cmd.extend(["--dtype", "1", "--username", cam.username])
            cmd.append(cam.serial)
            return cmd, {"IMOU_DHP2P_PASSWORD": cam.password} if cam.password else None
        cmd = [
            "python3",
            python_bridge,
            cam.serial,
            "--bind",
            f"127.0.0.1:{local_port}",
            "--remote-port",
            str(remote_port),
            "--type",
            "1",
            "--username",
            cam.username,
        ]
        if cam.relay:
            cmd.append("--relay")
        return cmd, {"IMOU_DHP2P_PASSWORD": cam.password}

    # DHP2P tunnels for p2p cameras
    tunnel_failures: dict[str, int] = {}
    for cam in active_cameras:
        if cam.mode != "p2p":
            continue
        cmd, env = p2p_tunnel_cmd(cam, cam.tunnel_port, 554)

        def make_handler(c: Camera):
            def handler(line: str) -> None:
                if "Ready to connect!" in line or "Pure Python DHP2P tunnel listening" in line:
                    tunnel_failures[c.slug] = 0
                    status.set_camera(c, "ready", f"Tunnel ready on 127.0.0.1:{c.tunnel_port}")
                    log(f"[{c.name}] tunnel ready on 127.0.0.1:{c.tunnel_port}")
            return handler

        def make_start_handler(c: Camera):
            def handler() -> None:
                failures = tunnel_failures.get(c.slug, 0)
                if failures:
                    status.set_camera(c, "restarting", f"Retrying P2P tunnel after {failures} failure(s)")
                else:
                    status.set_camera(c, "starting", "Starting P2P tunnel")
            return handler

        def make_exit_handler(c: Camera):
            def handler(code: int | None, recent: list[str]) -> None:
                tunnel_failures[c.slug] = tunnel_failures.get(c.slug, 0) + 1
                state, message = tunnel_failure_status(code, recent)
                if state == "error":
                    message = f"{message}; retrying automatically"
                status.set_camera(c, state, message)
            return handler

        w = Proc(f"tunnel:{cam.name}", cmd, stop=stop, restart_seconds=restart_seconds,
                 verbose=verbose, on_line=make_handler(cam),
                 on_start=make_start_handler(cam),
                 on_exit=make_exit_handler(cam), env=env)
        w.start()
        workers.append(w)

    # extra DHP2P tunnels to :37777 for PTZ (DVRIP) on p2p PTZ cameras
    for cam in active_cameras:
        if cam.mode == "p2p" and cam.ptz and cam.ptz_port:
            cmd, env = p2p_tunnel_cmd(cam, cam.ptz_port, 37777)
            w = Proc(f"ptz-tunnel:{cam.name}", cmd, stop=stop,
                     restart_seconds=restart_seconds, verbose=verbose, env=env)
            w.start()
            workers.append(w)

    # go2rtc (consumes sources, restreams)
    go2rtc = Proc("go2rtc", [GO2RTC_BIN, "-config", str(GO2RTC_CONFIG)],
                  stop=stop, restart_seconds=restart_seconds, verbose=True)
    go2rtc.start()
    workers.append(go2rtc)
    stream_watchdog = start_stream_watchdog(stop, active_cameras, rtsp_port, status, restart_seconds)

    if warm_enabled:
        resolved_ffmpeg = shutil.which(ffmpeg_bin) or (ffmpeg_bin if Path(ffmpeg_bin).exists() else "")
        if not resolved_ffmpeg:
            log(f"[warm] ffmpeg not found ({ffmpeg_bin}); stream warming disabled")
        else:
            for cam, stream_name in warm_stream_entries(active_cameras, all_streams=warm_all_streams):
                url = f"rtsp://127.0.0.1:{rtsp_port}/{stream_name}"
                cmd = [
                    resolved_ffmpeg,
                    "-hide_banner",
                    "-nostdin",
                    "-rtsp_transport",
                    "tcp",
                    "-i",
                    url,
                    "-map",
                    "0",
                    "-c",
                    "copy",
                    "-f",
                    "null",
                    "-",
                ]

                def make_warm_line_handler(c: Camera, name: str):
                    ready = {"seen": False}

                    def handler(line: str) -> None:
                        if not ready["seen"] and re.search(r"Stream mapping:|Output #0", line):
                            ready["seen"] = True
                            status.set_camera(c, "ready", f"RTSP stream warm: {name}")
                    return handler

                w = Proc(f"warm:{stream_name}", cmd, stop=stop,
                         restart_seconds=warm_restart_seconds, verbose=verbose,
                         on_line=make_warm_line_handler(cam, stream_name))
                w.start()
                workers.append(w)
                log(f"[warm:{stream_name}] keeping rtsp://127.0.0.1:{rtsp_port}/{stream_name} active")

    # account/camera management web UI (Home Assistant ingress -> "Open Web UI")
    if bool(options.get("discovery_ui", True)):
        site_port = int(os.environ.get("INGRESS_PORT") or options.get("discovery_port") or 8099)
        site = Proc("app-ui",
                    ["python3", "/opt/imou-p2p-bridge/app_ui.py"],
                    stop=stop, restart_seconds=restart_seconds, verbose=True,
                    env={"IMOU_UI_HOST": "0.0.0.0", "IMOU_UI_PORT": str(site_port)})
        site.start()
        workers.append(site)
        log(f"[app-ui] account/camera manager on ingress port {site_port}")

    for cam in active_cameras:
        log(f"[{cam.name}] restream: rtsp://<addon-host>:{rtsp_port}/{cam.slug}")

    adv = str(options.get("advertised_host", "<nas>"))
    dlna_base = int(options.get("dlna_base_port", 8800))

    renderers = []
    for i, c in enumerate(active_cameras):
        if not c.talk:
            continue
        c.dlna_port = dlna_base + i
        renderers.append({
            "name": c.display_name,
            "slug": c.slug,
            "serial": c.serial,
            "username": c.username,
            "password": c.password,
            "channel": c.channel,
            "subtype": c.subtype,
            "port": c.dlna_port,
        })
    if renderers:
        w = Proc("dlna-media",
                 ["python3", "/opt/imou-p2p-bridge/dlna_media_renderer.py"],
                 stop=stop, restart_seconds=restart_seconds, verbose=verbose,
                 env={"ADVERTISED_HOST": adv,
                      "IMOU_RENDERERS_JSON": json.dumps(renderers, separators=(",", ":"))})
        w.start()
        workers.append(w)
        for r in renderers:
            log(f"[dlna:{r['slug']}] MediaRenderer on :{r['port']} name=Imou {r['name']}")

    # PTZ DVRIP endpoints (LAN direct :37777 or p2p tunnel) for ONVIF shim
    ptz_eps = {}
    for c in active_cameras:
        if not c.ptz:
            continue
        if c.mode == "lan":
            endpoint = (c.host, 37777, c.username, c.password)
        elif c.ptz_port:
            endpoint = ("127.0.0.1", c.ptz_port, c.username, c.password)
        else:
            endpoint = None
        if not endpoint:
            c.ptz = False
            continue
        if probe_ptz(c, endpoint[0], endpoint[1]):
            ptz_eps[c.slug] = endpoint
        else:
            c.ptz = False

    # ONVIF PTZ shim per PTZ camera -> Frigate can drive PTZ via onvif
    onvif_base = int(options.get("onvif_base_port", 8700))
    for i, c in enumerate(active_cameras):
        has_ptz = c.ptz and c.slug in ptz_eps
        h, p, u, pw = ptz_eps[c.slug] if has_ptz else ("", 0, c.username, c.password)
        oport = onvif_base + i
        c.onvif_port = oport
        w = Proc(f"onvif:{c.name}", ["python3", "/opt/imou-p2p-bridge/onvif_ptz_shim.py"],
                 stop=stop, restart_seconds=restart_seconds, verbose=verbose,
                 env={"ONVIF_PORT": str(oport), "ADVERTISED_HOST": adv,
                      "ONVIF_RTSP_URI": f"rtsp://{adv}:{rtsp_port}/{c.slug}",
                      "ONVIF_STREAMS_JSON": json.dumps(c.onvif_streams(rtsp_port, adv), separators=(",", ":")),
                      "ONVIF_NAME": c.display_name, "ONVIF_MODEL": "Imou Bridge", "ONVIF_SERIAL": c.serial or c.slug,
                      "PTZ_ENABLED": "true" if has_ptz else "false",
                      "DVRIP_HOST": h, "DVRIP_PORT": str(p), "DVRIP_USER": u, "DVRIP_PASS": pw})
        w.start()
        workers.append(w)
        log(f"[onvif:{c.name}] ONVIF shim on :{oport} stream=rtsp://{adv}:{rtsp_port}/{c.slug} ptz={has_ptz}")

    while not stop.is_set():
        if not any(w.is_alive() for w in workers):
            return 1
        stop.wait(2)
    for w in workers:
        w.join(timeout=10)
    if stream_watchdog:
        stream_watchdog.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
