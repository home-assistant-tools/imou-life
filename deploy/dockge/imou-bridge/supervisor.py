#!/usr/bin/env python3
"""Imou P2P Bridge supervisor.

Per configured camera:
  - mode "p2p": run a dh-p2p tunnel (serial -> 127.0.0.1:<tunnel_port> -> camera:554)
  - mode "lan": no tunnel; go2rtc sources the camera directly on the LAN

A bundled go2rtc consumes each source once and restreams RTSP/WebRTC/HLS to many
consumers (Home Assistant, Frigate) — this also works around dh-p2p being
single-client. Optional MQTT discovery exposes the go2rtc RTSP URL per camera.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    from paho.mqtt import publish as mqtt_publish
except Exception:  # pragma: no cover - optional in local dev
    mqtt_publish = None

OPTIONS_PATH = Path(os.environ.get("IMOU_BRIDGE_OPTIONS", "/data/options.json"))
GO2RTC_CONFIG = Path(os.environ.get("IMOU_GO2RTC_CONFIG", "/data/go2rtc.yaml"))
GO2RTC_BIN = os.environ.get("IMOU_GO2RTC_BIN", "/opt/go2rtc")

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


def slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower()).strip("_") or "camera"


def load_options() -> dict[str, Any]:
    if not OPTIONS_PATH.exists():
        raise SystemExit(f"Options file not found: {OPTIONS_PATH}")
    return json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))


@dataclass
class Camera:
    name: str
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
    tunnel_port: int = 0  # assigned for p2p cameras (stream, ->554)
    ptz_port: int = 0     # assigned for p2p ptz cameras (DVRIP, ->37777)
    onvif_port: int = 0   # assigned for ptz cameras (ONVIF shim for Frigate)

    def realmonitor_path(self) -> str:
        return f"/cam/realmonitor?channel={self.channel}&subtype={self.subtype}"

    def source_url(self) -> str:
        user = quote(self.username, safe="")
        pw = quote(self.password, safe="")
        host = f"127.0.0.1:{self.tunnel_port}" if self.mode == "p2p" else f"{self.host}:554"
        return f"rtsp://{user}:{pw}@{host}{self.realmonitor_path()}"


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
            slug=slugify(name),
            mode=mode,
            username=str(raw.get("username", "admin")),
            password=str(raw.get("password", "")),
            channel=int(raw.get("channel", 1)),
            subtype=int(raw.get("subtype", 0)),
            serial=str(raw.get("serial", "")),
            host=str(raw.get("host", "")),
            relay=bool(raw.get("relay", False)),
            ptz=bool(raw.get("ptz", False)),
        )
        if mode == "p2p":
            cam.tunnel_port = next_port
            next_port += 1
            if cam.ptz:
                cam.ptz_port = next_port  # second tunnel -> 37777 for DVRIP/PTZ
                next_port += 1
        cams.append(cam)
    return cams


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
        lines.append(f"  {cam.slug}:")
        lines.append(f"    - {cam.source_url()}")
    GO2RTC_CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"[go2rtc] wrote config with {len(cameras)} stream(s) -> {GO2RTC_CONFIG}")


# ----------------------------- MQTT discovery ------------------------------
@dataclass
class MqttConfig:
    enabled: bool
    host: str
    port: int
    username: str
    password: str
    topic_prefix: str
    discovery: bool
    discovery_prefix: str

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> "MqttConfig":
        m = options.get("mqtt", {})
        return cls(
            enabled=bool(m.get("enabled", False)),
            host=str(m.get("host", "core-mosquitto")),
            port=int(m.get("port", 1883)),
            username=str(m.get("username", "")),
            password=str(m.get("password", "")),
            topic_prefix=str(m.get("topic_prefix", "imou_p2p")).strip("/"),
            discovery=bool(m.get("discovery", True)),
            discovery_prefix=str(m.get("discovery_prefix", "homeassistant")).strip("/"),
        )


class Mqtt:
    def __init__(self, config: MqttConfig) -> None:
        self.config = config
        if config.enabled and mqtt_publish is None:
            log("[mqtt] paho-mqtt unavailable; MQTT disabled")
            self.config.enabled = False

    def publish(self, topic: str, payload: str, *, retain: bool = False) -> None:
        if not self.config.enabled:
            return
        auth = {"username": self.config.username, "password": self.config.password} if self.config.username else None
        try:
            mqtt_publish.single(topic, payload=payload, hostname=self.config.host,
                                port=self.config.port, auth=auth, retain=retain)
        except Exception as exc:
            log(f"[mqtt] publish failed {topic}: {exc}")

    def state_topic(self, slug: str) -> str:
        return f"{self.config.topic_prefix}/{slug}/state"

    def announce(self, cam: Camera, rtsp_port: int) -> None:
        if not (self.config.enabled and self.config.discovery):
            return
        # Use the host running the add-on; HA substitutes its own IP for the
        # generic camera. We publish the restream path; users prefix the host.
        device = {"identifiers": [f"imou_p2p_{cam.slug}"], "name": f"Imou {cam.name}",
                  "manufacturer": "Imou/Dahua"}
        base = self.config.discovery_prefix
        url_sensor = {
            "name": f"{cam.name} RTSP URL",
            "unique_id": f"imou_p2p_{cam.slug}_url",
            "state_topic": f"{self.config.topic_prefix}/{cam.slug}/rtsp_url",
            "icon": "mdi:cctv", "device": device,
        }
        conn = {
            "name": f"{cam.name} bridge",
            "unique_id": f"imou_p2p_{cam.slug}_conn",
            "state_topic": self.state_topic(cam.slug),
            "payload_on": "online", "payload_off": "offline",
            "device_class": "connectivity", "device": device,
        }
        self.publish(f"{base}/sensor/imou_p2p_{cam.slug}_url/config",
                     json.dumps(url_sensor, separators=(",", ":")), retain=True)
        self.publish(f"{base}/binary_sensor/imou_p2p_{cam.slug}_conn/config",
                     json.dumps(conn, separators=(",", ":")), retain=True)
        # restream path (host filled in by the user); go2rtc serves /<slug>
        self.publish(f"{self.config.topic_prefix}/{cam.slug}/rtsp_url",
                     f"rtsp://<HA_OR_ADDON_HOST>:{rtsp_port}/{cam.slug}", retain=True)


# ----------------------------- process workers -----------------------------
class Proc(threading.Thread):
    """Generic restart-on-exit process runner with line logging."""

    def __init__(self, name: str, cmd: list[str], *, stop: threading.Event,
                 restart_seconds: int, verbose: bool, on_line=None, env: dict | None = None) -> None:
        super().__init__(daemon=True)
        self.name = name
        self.cmd = cmd
        self.stop = stop
        self.restart_seconds = max(1, restart_seconds)
        self.verbose = verbose
        self.on_line = on_line
        self.env = env
        self.proc: subprocess.Popen[str] | None = None

    def terminate(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def run(self) -> None:
        run_env = {**os.environ, **self.env} if self.env else None
        while not self.stop.is_set():
            log(f"[{self.name}] starting: {' '.join(redact(c) for c in self.cmd)}")
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, bufsize=1,
                                         env=run_env)
            assert self.proc.stdout is not None
            for raw in self.proc.stdout:
                line = redact(raw.rstrip("\n"))
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
    binary = str(bridge.get("binary", "/opt/dh-p2p/dh-p2p"))

    cameras = parse_cameras(options, base_port)
    if not cameras:
        raise SystemExit("Configure at least one camera")
    write_go2rtc_config(cameras, go2rtc_opts, log_level)

    mqtt_cfg = options.get("mqtt", {})
    stop = threading.Event()
    workers: list[Proc] = []

    def handle_signal(_s, _f) -> None:
        stop.set()
        for w in workers:
            w.terminate()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # dh-p2p tunnels for p2p cameras
    for cam in cameras:
        if cam.mode != "p2p":
            continue
        cmd = [binary, "-p", f"127.0.0.1:{cam.tunnel_port}:554"]
        if cam.relay:
            cmd.append("--relay")
        cmd.append(cam.serial)

        def make_handler(c: Camera):
            def handler(line: str) -> None:
                if "Ready to connect!" in line:
                    log(f"[{c.name}] tunnel ready on 127.0.0.1:{c.tunnel_port}")
            return handler

        w = Proc(f"tunnel:{cam.name}", cmd, stop=stop, restart_seconds=restart_seconds,
                 verbose=verbose, on_line=make_handler(cam))
        w.start()
        workers.append(w)

    # extra dh-p2p tunnels to :37777 for PTZ (DVRIP) on p2p PTZ cameras
    for cam in cameras:
        if cam.mode == "p2p" and cam.ptz and cam.ptz_port:
            cmd = [binary, "-p", f"127.0.0.1:{cam.ptz_port}:37777"]
            if cam.relay:
                cmd.append("--relay")
            cmd.append(cam.serial)
            w = Proc(f"ptz-tunnel:{cam.name}", cmd, stop=stop,
                     restart_seconds=restart_seconds, verbose=verbose)
            w.start()
            workers.append(w)

    # go2rtc (consumes sources, restreams)
    go2rtc = Proc("go2rtc", [GO2RTC_BIN, "-config", str(GO2RTC_CONFIG)],
                  stop=stop, restart_seconds=restart_seconds, verbose=True)
    go2rtc.start()
    workers.append(go2rtc)

    # device-discovery web UI (Home Assistant ingress -> "Open Web UI")
    if bool(options.get("discovery_ui", True)):
        site_port = int(os.environ.get("INGRESS_PORT") or options.get("discovery_port") or 8099)
        site = Proc("discovery-ui",
                    ["python3", "/opt/imou-p2p-bridge/login_site.py"],
                    stop=stop, restart_seconds=restart_seconds, verbose=True,
                    env={"IMOU_SITE_HOST": "0.0.0.0", "IMOU_SITE_PORT": str(site_port)})
        site.start()
        workers.append(site)
        log(f"[discovery-ui] login/serial helper on ingress port {site_port}")

    for cam in cameras:
        log(f"[{cam.name}] restream: rtsp://<addon-host>:{rtsp_port}/{cam.slug}")

    adv = str(options.get("advertised_host", "<nas>"))

    # PTZ DVRIP endpoints (LAN direct :37777 or p2p tunnel) for MQTT + ONVIF shim
    ptz_eps = {}
    for c in cameras:
        if not c.ptz:
            continue
        if c.mode == "lan":
            ptz_eps[c.slug] = (c.host, 37777, c.username, c.password)
        elif c.ptz_port:
            ptz_eps[c.slug] = ("127.0.0.1", c.ptz_port, c.username, c.password)

    # ONVIF PTZ shim per PTZ camera -> Frigate can drive PTZ via onvif
    onvif_base = int(options.get("onvif_base_port", 8700))
    for i, c in enumerate(cameras):
        if not c.ptz or c.slug not in ptz_eps:
            continue
        h, p, u, pw = ptz_eps[c.slug]
        oport = onvif_base + i
        c.onvif_port = oport
        w = Proc(f"onvif:{c.name}", ["python3", "/opt/imou-p2p-bridge/onvif_ptz_shim.py"],
                 stop=stop, restart_seconds=restart_seconds, verbose=verbose,
                 env={"ONVIF_PORT": str(oport), "ADVERTISED_HOST": adv,
                      "DVRIP_HOST": h, "DVRIP_PORT": str(p), "DVRIP_USER": u, "DVRIP_PASS": pw})
        w.start()
        workers.append(w)
        log(f"[onvif:{c.name}] ONVIF PTZ shim on :{oport} (Frigate onvif host:{adv} port:{oport})")

    # local MQTT: HA discovery + state + PTZ/TTS commands
    if bool(mqtt_cfg.get("enabled", False)):
        try:
            from mqtt_bridge import MqttBridge
        except Exception as exc:
            log(f"[mqtt] cannot import bridge: {exc}")
            MqttBridge = None
        if MqttBridge:
            cam_dicts = [{"slug": c.slug, "name": c.name, "mode": c.mode, "ptz": c.ptz} for c in cameras]
            api_port = int(go2rtc_opts.get("api_port", 1984))
            snap_iv = int(options.get("snapshot_interval", 5))
            bridge_th = MqttBridge(mqtt_cfg, cam_dicts, rtsp_port, stop, ptz_eps,
                                   advertised_host=adv, api_port=api_port,
                                   snapshot_interval=snap_iv)
            bridge_th.start()
            log(f"[mqtt] bridge started -> {mqtt_cfg.get('host')}:{mqtt_cfg.get('port', 1883)} "
                f"(ptz cams: {list(ptz_eps)})")

    while not stop.is_set():
        if not any(w.is_alive() for w in workers):
            return 1
        stop.wait(2)
    for w in workers:
        w.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
