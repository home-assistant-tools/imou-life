#!/usr/bin/env python3
"""MQTT bridge: HA discovery + state + PTZ/TTS commands over a local broker.

Publishes Home Assistant MQTT discovery for each camera (availability, RTSP URL,
and PTZ buttons for PanTilt cameras) and subscribes to command topics:
  imou/<slug>/ptz/set  -> Left|Right|Up|Down|ZoomTele|ZoomWide|Stop|Preset:<n>
  imou/<slug>/tts      -> text/URL (experimental; talk path)

PTZ uses the Dahua DVRIP RPC (dvrip.py): for LAN cameras directly on
<host>:37777, for P2P cameras via a dh-p2p tunnel the supervisor opened on
127.0.0.1:<ptz_port>. Video stays on go2rtc; this only carries control + status.
"""
from __future__ import annotations

import json
import threading
import time
from urllib import request

import dvrip

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None


def log(m: str) -> None:
    print(m, flush=True)


class PtzSession:
    """Lazy, self-healing DVRIP RPC session for one camera's PTZ."""

    def __init__(self, host: str, port: int, user: str, password: str):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.cli: dvrip.DvripClient | None = None
        self.obj = None
        self.lock = threading.Lock()

    def _ensure(self):
        if self.cli is not None:
            return
        c = dvrip.DvripClient(self.host, self.user, self.password, port=self.port, timeout=6)
        c.login()
        r = c.rpc("ptz.factory.instance", {"channel": 0})
        self.obj = r.get("result")
        self.cli = c

    def _reset(self):
        try:
            if self.cli:
                self.cli.close()
        except Exception:
            pass
        self.cli = None
        self.obj = None

    def move(self, code: str, speed: int = 4, run: bool = True):
        """Continuous start (run=True) / stop (run=False) for a direction code."""
        with self.lock:
            for _ in (1, 2):
                try:
                    self._ensure()
                    m = "ptz.start" if run else "ptz.stop"
                    self.cli.rpc(m, {"code": code, "arg1": 0, "arg2": int(speed), "arg3": 0},
                                 extra={"object": self.obj})
                    return True
                except Exception as e:
                    log(f"[ptz] {self.host}:{self.port} {m} {code} err: {e}")
                    self._reset()
            return False

    def stop_all(self):
        with self.lock:
            try:
                self._ensure()
                for c in ("Left", "Right", "Up", "Down", "ZoomTele", "ZoomWide"):
                    self.cli.rpc("ptz.stop", {"code": c, "arg1": 0, "arg2": 0, "arg3": 0},
                                 extra={"object": self.obj})
            except Exception as e:
                log(f"[ptz] stop_all err: {e}")
                self._reset()

    def command(self, code: str):
        with self.lock:
            for attempt in (1, 2):
                try:
                    self._ensure()
                    if code == "Stop":
                        # stop any motion (best-effort across common codes)
                        for c in ("Left", "Right", "Up", "Down", "ZoomTele", "ZoomWide"):
                            self.cli.rpc("ptz.stop", {"code": c, "arg1": 0, "arg2": 0, "arg3": 0},
                                         extra={"object": self.obj})
                        return True
                    if code.startswith("Preset:"):
                        n = int(code.split(":", 1)[1])
                        self.cli.rpc("ptz.start", {"code": "GotoPreset", "arg1": 0, "arg2": n, "arg3": 0},
                                     extra={"object": self.obj})
                        return True
                    p = {"code": code, "arg1": 0, "arg2": 4, "arg3": 0}
                    self.cli.rpc("ptz.start", p, extra={"object": self.obj})
                    time.sleep(0.6)  # brief nudge, then stop
                    self.cli.rpc("ptz.stop", p, extra={"object": self.obj})
                    return True
                except Exception as e:
                    log(f"[ptz] {self.host}:{self.port} {code} err: {e}")
                    self._reset()
            return False


class MqttBridge(threading.Thread):
    def __init__(self, mqtt_cfg: dict, cameras: list, rtsp_port: int, stop: threading.Event,
                 ptz_endpoints: dict, tts_cb=None, advertised_host: str = "<nas>",
                 api_port: int = 1984, snapshot_interval: int = 5):
        super().__init__(daemon=True)
        self.cfg = mqtt_cfg
        self.cameras = cameras           # list of dicts: {slug,name,mode,ptz,...}
        self.rtsp_port = rtsp_port
        self.host_ip = advertised_host
        self.api_port = api_port
        self.snapshot_interval = snapshot_interval
        self.stop = stop
        self.tts_cb = tts_cb
        self.disc = str(mqtt_cfg.get("discovery_prefix", "homeassistant")).strip("/")
        self.base = str(mqtt_cfg.get("topic_prefix", "imou")).strip("/")
        # ptz_endpoints: slug -> (host, port) for DVRIP
        self.ptz = {slug: PtzSession(h, p, u, pw) for slug, (h, p, u, pw) in ptz_endpoints.items()}
        self.client = None

    def t(self, slug: str, suffix: str) -> str:
        return f"{self.base}/{slug}/{suffix}"

    def run(self):
        if mqtt is None:
            log("[mqtt] paho-mqtt missing; bridge disabled")
            return
        c = mqtt.Client()
        if self.cfg.get("username"):
            c.username_pw_set(self.cfg["username"], self.cfg.get("password", ""))
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        # availability LWT
        c.will_set(f"{self.base}/bridge/state", "offline", retain=True)
        self.client = c
        host = self.cfg.get("host", "127.0.0.1")
        port = int(self.cfg.get("port", 1883))
        while not self.stop.is_set():
            try:
                c.connect(host, port, keepalive=30)
                break
            except Exception as e:
                log(f"[mqtt] connect {host}:{port} failed: {e}; retry in 5s")
                self.stop.wait(5)
        c.loop_start()
        c.publish(f"{self.base}/bridge/state", "online", retain=True)
        snap = threading.Thread(target=self._snapshot_loop, daemon=True)
        snap.start()
        while not self.stop.is_set():
            self.stop.wait(1)
        c.loop_stop()

    def _snapshot_loop(self):
        """Periodically fetch a JPEG from go2rtc and publish it (MQTT camera)."""
        while not self.stop.is_set():
            for cam in self.cameras:
                slug = cam["slug"]
                url = f"http://127.0.0.1:{self.api_port}/api/frame.jpeg?src={slug}"
                try:
                    with request.urlopen(url, timeout=12) as r:
                        img = r.read()
                    if img and self.client:
                        self.client.publish(self.t(slug, "snapshot"), img, retain=True)
                except Exception as e:
                    log(f"[snap] {slug}: {e}")
                if self.stop.is_set():
                    return
            self.stop.wait(max(1, self.snapshot_interval))

    def _on_connect(self, client, _u, _f, rc):
        log(f"[mqtt] connected rc={rc}")
        for cam in self.cameras:
            self._announce(client, cam)
            client.subscribe(self.t(cam["slug"], "ptz/set"))
            client.subscribe(self.t(cam["slug"], "tts"))
            client.publish(self.t(cam["slug"], "state"), "online", retain=True)

    def _announce(self, client, cam):
        slug, name = cam["slug"], cam["name"]
        dev = {"identifiers": [f"imou_{slug}"], "name": f"Imou {name}", "manufacturer": "Imou/Dahua"}
        avail = [{"topic": self.t(slug, "state")}]

        def cfg(component, oid, payload):
            payload = {**payload, "device": dev, "availability": avail,
                       "payload_available": "online", "payload_not_available": "offline"}
            client.publish(f"{self.disc}/{component}/imou_{oid}/config",
                           json.dumps(payload, separators=(",", ":")), retain=True)

        cfg("sensor", f"{slug}_url", {
            "name": f"{name} RTSP URL", "unique_id": f"imou_{slug}_url",
            "state_topic": self.t(slug, "rtsp_url"), "icon": "mdi:cctv"})
        client.publish(self.t(slug, "rtsp_url"),
                       f"rtsp://{self.host_ip}:{self.rtsp_port}/{slug}", retain=True)

        # MQTT camera = refreshing JPEG snapshot (viewable in HA via MQTT).
        # For smooth live video use the RTSP URL above (Generic Camera / go2rtc).
        cfg("camera", f"{slug}_cam", {
            "name": name, "unique_id": f"imou_{slug}_cam",
            "topic": self.t(slug, "snapshot")})

        if cam.get("ptz"):
            buttons = [("left", "Left", "mdi:arrow-left"), ("right", "Right", "mdi:arrow-right"),
                       ("up", "Up", "mdi:arrow-up"), ("down", "Down", "mdi:arrow-down"),
                       ("zoom_in", "ZoomTele", "mdi:magnify-plus"),
                       ("zoom_out", "ZoomWide", "mdi:magnify-minus"),
                       ("stop", "Stop", "mdi:stop")]
            for key, code, icon in buttons:
                cfg("button", f"{slug}_ptz_{key}", {
                    "name": f"{name} PTZ {key.replace('_', ' ')}",
                    "unique_id": f"imou_{slug}_ptz_{key}",
                    "command_topic": self.t(slug, "ptz/set"),
                    "payload_press": code, "icon": icon})

    def _on_message(self, _c, _u, msg):
        try:
            payload = msg.payload.decode("utf-8", "replace").strip()
        except Exception:
            return
        for cam in self.cameras:
            slug = cam["slug"]
            if msg.topic == self.t(slug, "ptz/set"):
                sess = self.ptz.get(slug)
                if sess:
                    log(f"[mqtt] ptz {slug} <- {payload}")
                    sess.command(payload)
                return
            if msg.topic == self.t(slug, "tts") and self.tts_cb:
                log(f"[mqtt] tts {slug} <- {payload[:40]}")
                self.tts_cb(cam, payload)
                return
