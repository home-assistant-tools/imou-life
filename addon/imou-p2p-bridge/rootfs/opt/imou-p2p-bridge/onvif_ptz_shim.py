#!/usr/bin/env python3
"""Minimal ONVIF device/media/PTZ shim that maps ONVIF PTZ -> Dahua RPC.

Lets Frigate (python-onvif-zeep) drive PTZ on an Imou/Dahua camera that has no
reachable ONVIF service (e.g. reached only over cloud P2P). It serves just enough
ONVIF to pass Frigate's init + PTZ capability detection, and translates
ContinuousMove/Stop/GotoPreset into Dahua `ptz.start/ptz.stop/GotoPreset` over the
DVRIP RPC, pointed at the camera's :37777 or a tunnel.

One instance per camera. Env:
  ONVIF_PORT, ONVIF_RTSP_URI, ONVIF_NAME, ONVIF_MODEL, ONVIF_SERIAL,
  PTZ_ENABLED, DVRIP_HOST, DVRIP_PORT, DVRIP_USER, DVRIP_PASS
Frigate config:
  cam:
    onvif:
      host: <nas-ip>
      port: <ONVIF_PORT>
      user: x
      password: x
"""
from __future__ import annotations

import os
import re
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dvrip

NS = (
    'xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:tt="http://www.onvif.org/ver10/schema" '
    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
    'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
    'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
)

# default ONVIF PTZ spaces (presence => Frigate enables pan/tilt + zoom)
SP_CPT = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"
SP_CZ = "http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace"
SP_RPT = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace"
SP_RZ = "http://www.onvif.org/ver10/tptz/ZoomSpaces/TranslationGenericSpace"
SP_AZ = "http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace"

HOST = os.environ.get("ADVERTISED_HOST", "127.0.0.1")
ONVIF_PORT = int(os.environ.get("ONVIF_PORT", "8700"))
RTSP_URI = os.environ.get("ONVIF_RTSP_URI", f"rtsp://{HOST}:8654/remote_camera")
CAMERA_NAME = os.environ.get("ONVIF_NAME", "Imou Camera")
CAMERA_MODEL = os.environ.get("ONVIF_MODEL", "P2P Camera")
CAMERA_SERIAL = os.environ.get("ONVIF_SERIAL", "shim")
PTZ_ENABLED = os.environ.get("PTZ_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


def load_streams() -> list[dict]:
    raw = os.environ.get("ONVIF_STREAMS_JSON", "")
    streams: list[dict] = []
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                streams = [item for item in data if isinstance(item, dict)]
        except json.JSONDecodeError as exc:
            print(f"[onvif-shim] invalid ONVIF_STREAMS_JSON: {exc}", flush=True)
    if not streams:
        streams = [{"token": "profile0", "source_token": "vsrc1", "encoder_token": "venc0",
                    "name": CAMERA_NAME, "channel": 1, "subtype": 0, "uri": RTSP_URI}]
    normalized = []
    for index, item in enumerate(streams):
        channel = int(item.get("channel", 1) or 1)
        subtype = int(item.get("subtype", 0) or 0)
        normalized.append({
            "token": str(item.get("token") or f"profile{index}"),
            "source_token": str(item.get("source_token") or f"vsrc{channel}"),
            "encoder_token": str(item.get("encoder_token") or f"venc{index}"),
            "name": str(item.get("name") or f"{CAMERA_NAME} ch{channel} sub{subtype}"),
            "channel": channel,
            "subtype": subtype,
            "uri": str(item.get("uri") or RTSP_URI),
        })
    return normalized


STREAMS = load_streams()


def xml_escape(value: object) -> str:
    return (str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def profile_for(stream: dict, include_ptz: bool) -> str:
    ptz_cfg = PTZCFG if include_ptz else ""
    name = xml_escape(stream["name"])
    token = xml_escape(stream["token"])
    source_token = xml_escape(stream["source_token"])
    encoder_token = xml_escape(stream["encoder_token"])
    return (f"<trt:Profiles token=\"{token}\" fixed=\"true\">"
            f"<tt:Name>{name}</tt:Name>"
            f"<tt:VideoSourceConfiguration token=\"vsc_{source_token}\"><tt:Name>vsc_{source_token}</tt:Name><tt:UseCount>1</tt:UseCount>"
            f"<tt:SourceToken>{source_token}</tt:SourceToken><tt:Bounds x=\"0\" y=\"0\" width=\"1920\" height=\"1080\"/></tt:VideoSourceConfiguration>"
            f"<tt:VideoEncoderConfiguration token=\"{encoder_token}\"><tt:Name>{encoder_token}</tt:Name><tt:UseCount>1</tt:UseCount>"
            f"<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>"
            f"<tt:Quality>4</tt:Quality><tt:RateControl><tt:FrameRateLimit>20</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>2048</tt:BitrateLimit></tt:RateControl>"
            f"<tt:H264><tt:GovLength>30</tt:GovLength><tt:H264Profile>Main</tt:H264Profile></tt:H264></tt:VideoEncoderConfiguration>"
            f"{ptz_cfg}"
            f"</trt:Profiles>")


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
        with self.lock:
            for _ in (1, 2):
                try:
                    self._ensure()
                    method = "ptz.start" if run else "ptz.stop"
                    self.cli.rpc(method, {"code": code, "arg1": 0, "arg2": int(speed), "arg3": 0},
                                 extra={"object": self.obj})
                    return True
                except Exception as exc:
                    print(f"[onvif-ptz] {self.host}:{self.port} {code} err: {exc}", flush=True)
                    self._reset()
            return False

    def stop_all(self):
        with self.lock:
            try:
                self._ensure()
                for code in ("Left", "Right", "Up", "Down", "ZoomTele", "ZoomWide"):
                    self.cli.rpc("ptz.stop", {"code": code, "arg1": 0, "arg2": 0, "arg3": 0},
                                 extra={"object": self.obj})
            except Exception as exc:
                print(f"[onvif-ptz] stop_all err: {exc}", flush=True)
                self._reset()

PTZCFG = f"""<tt:PTZConfiguration token="ptz0">
 <tt:Name>ptz0</tt:Name><tt:UseCount>1</tt:UseCount>
 <tt:NodeToken>node0</tt:NodeToken>
 <tt:DefaultAbsolutePantTiltPositionSpace>{SP_AZ}</tt:DefaultAbsolutePantTiltPositionSpace>
 <tt:DefaultAbsoluteZoomPositionSpace>{SP_AZ}</tt:DefaultAbsoluteZoomPositionSpace>
 <tt:DefaultRelativePanTiltTranslationSpace>{SP_RPT}</tt:DefaultRelativePanTiltTranslationSpace>
 <tt:DefaultRelativeZoomTranslationSpace>{SP_RZ}</tt:DefaultRelativeZoomTranslationSpace>
 <tt:DefaultContinuousPanTiltVelocitySpace>{SP_CPT}</tt:DefaultContinuousPanTiltVelocitySpace>
 <tt:DefaultContinuousZoomVelocitySpace>{SP_CZ}</tt:DefaultContinuousZoomVelocitySpace>
 <tt:DefaultPTZSpeed><tt:PanTilt x="0.5" y="0.5" xmlns:tt="http://www.onvif.org/ver10/schema"/><tt:Zoom x="0.5" xmlns:tt="http://www.onvif.org/ver10/schema"/></tt:DefaultPTZSpeed>
 <tt:DefaultPTZTimeout>PT5S</tt:DefaultPTZTimeout>
</tt:PTZConfiguration>"""

SPACES = f"""<tt:Spaces>
 <tt:ContinuousPanTiltVelocitySpace><tt:URI>{SP_CPT}</tt:URI>
  <tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange>
  <tt:YRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:YRange></tt:ContinuousPanTiltVelocitySpace>
 <tt:ContinuousZoomVelocitySpace><tt:URI>{SP_CZ}</tt:URI>
  <tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange></tt:ContinuousZoomVelocitySpace>
 <tt:RelativePanTiltTranslationSpace><tt:URI>{SP_RPT}</tt:URI>
  <tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange>
  <tt:YRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:YRange></tt:RelativePanTiltTranslationSpace>
 <tt:RelativeZoomTranslationSpace><tt:URI>{SP_RZ}</tt:URI>
  <tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange></tt:RelativeZoomTranslationSpace>
 <tt:AbsoluteZoomPositionSpace><tt:URI>{SP_AZ}</tt:URI>
  <tt:XRange><tt:Min>0</tt:Min><tt:Max>1</tt:Max></tt:XRange></tt:AbsoluteZoomPositionSpace>
</tt:Spaces>"""


def envelope(body: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope {NS}><s:Body>{body}</s:Body></s:Envelope>')


def base() -> str:
    return f"http://{HOST}:{ONVIF_PORT}"


class Shim(BaseHTTPRequestHandler):
    ptz: PtzSession | None = None  # set in main

    def log_message(self, *a):
        pass

    def _send(self, xml: str, code: int = 200):
        out = xml.encode()
        self.send_response(code)
        self.send_header("Content-Type", 'application/soap+xml; charset=utf-8')
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self._send(envelope("<tds:GetSystemDateAndTimeResponse/>"))

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        # action = local name of the first child element of <Body>
        m = re.search(r"<(?:\w+:)?Body[^>]*>\s*<(?:(\w+):)?(\w+)", body)
        action = m.group(2) if m else ""
        print(f"[onvif-shim] {self.path} -> {action}", flush=True)
        try:
            self._send(envelope(self.handle_action(action, body)))
        except Exception as e:
            self._send(envelope(
                f'<s:Fault><s:Reason><s:Text>{e}</s:Text></s:Reason></s:Fault>'), 500)

    # ------------------------------------------------------------------
    def handle_action(self, action: str, body: str) -> str:
        b = base()
        if action == "GetSystemDateAndTime":
            return "<tds:GetSystemDateAndTimeResponse/>"
        if action == "GetDeviceInformation":
            return ("<tds:GetDeviceInformationResponse>"
                    f"<tds:Manufacturer>Imou</tds:Manufacturer><tds:Model>{CAMERA_MODEL}</tds:Model>"
                    "<tds:FirmwareVersion>1.0</tds:FirmwareVersion>"
                    f"<tds:SerialNumber>{CAMERA_SERIAL}</tds:SerialNumber>"
                    "<tds:HardwareId>shim</tds:HardwareId></tds:GetDeviceInformationResponse>")
        if action == "GetServices" or action == "GetCapabilities":
            media = f"{b}/onvif/media_service"
            ptz = f"{b}/onvif/ptz_service"
            dev = f"{b}/onvif/device_service"
            if action == "GetServices":
                ptz_service = (
                    f"<tds:Service><tds:Namespace>http://www.onvif.org/ver20/ptz/wsdl</tds:Namespace><tds:XAddr>{ptz}</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tds:Version></tds:Service>"
                    if PTZ_ENABLED else ""
                )
                return (f"<tds:GetServicesResponse>"
                        f"<tds:Service><tds:Namespace>http://www.onvif.org/ver10/device/wsdl</tds:Namespace><tds:XAddr>{dev}</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tds:Version></tds:Service>"
                        f"<tds:Service><tds:Namespace>http://www.onvif.org/ver10/media/wsdl</tds:Namespace><tds:XAddr>{media}</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tds:Version></tds:Service>"
                        f"{ptz_service}"
                        f"</tds:GetServicesResponse>")
            ptz_cap = f"<tt:PTZ><tt:XAddr>{ptz}</tt:XAddr></tt:PTZ>" if PTZ_ENABLED else ""
            return (f"<tds:GetCapabilitiesResponse><tds:Capabilities>"
                    f"<tt:Device><tt:XAddr>{dev}</tt:XAddr></tt:Device>"
                    f"<tt:Media><tt:XAddr>{media}</tt:XAddr></tt:Media>"
                    f"{ptz_cap}"
                    f"</tds:Capabilities></tds:GetCapabilitiesResponse>")
        if action == "GetScopes":
            return "<tds:GetScopesResponse/>"
        if action == "GetVideoSources":
            seen = set()
            sources = []
            for stream in STREAMS:
                token = stream["source_token"]
                if token in seen:
                    continue
                seen.add(token)
                sources.append(
                    f"<trt:VideoSources token=\"{xml_escape(token)}\">"
                    "<tt:Framerate>20</tt:Framerate><tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>"
                    "</trt:VideoSources>"
                )
            return f"<trt:GetVideoSourcesResponse>{''.join(sources)}</trt:GetVideoSourcesResponse>"
        if action == "GetProfiles":
            profiles = "".join(profile_for(stream, PTZ_ENABLED) for stream in STREAMS)
            return f"<trt:GetProfilesResponse>{profiles}</trt:GetProfilesResponse>"
        if action == "GetStreamUri":
            token_match = re.search(r"<(?:\w+:)?ProfileToken>\s*([^<\s]+)", body)
            requested = token_match.group(1) if token_match else STREAMS[0]["token"]
            stream = next((s for s in STREAMS if s["token"] == requested), STREAMS[0])
            return (f"<trt:GetStreamUriResponse><trt:MediaUri>"
                    f"<tt:Uri>{xml_escape(stream['uri'])}</tt:Uri>"
                    f"<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
                    f"<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT0S</tt:Timeout>"
                    f"</trt:MediaUri></trt:GetStreamUriResponse>")
        if action == "GetConfigurations":
            return f"<tptz:GetConfigurationsResponse>{PTZCFG}</tptz:GetConfigurationsResponse>".replace("tt:PTZConfiguration", "tptz:PTZConfiguration") if False else f"<tptz:GetConfigurationsResponse>{PTZCFG}</tptz:GetConfigurationsResponse>"
        if action == "GetConfigurationOptions":
            return (f"<tptz:GetConfigurationOptionsResponse><tptz:PTZConfigurationOptions>"
                    f"{SPACES}"
                    f"<tt:PTZTimeout><tt:Min>PT1S</tt:Min><tt:Max>PT10S</tt:Max></tt:PTZTimeout>"
                    f"</tptz:PTZConfigurationOptions></tptz:GetConfigurationOptionsResponse>")
        if action == "GetNodes":
            return ("<tptz:GetNodesResponse><tptz:PTZNode token=\"node0\">"
                    "<tt:Name>node0</tt:Name>" + SPACES +
                    "<tt:MaximumNumberOfPresets>8</tt:MaximumNumberOfPresets>"
                    "<tt:HomeSupported>false</tt:HomeSupported></tptz:PTZNode></tptz:GetNodesResponse>")
        if action == "GetStatus":
            return ("<tptz:GetStatusResponse><tptz:PTZStatus>"
                    "<tt:Position><tt:PanTilt x=\"0\" y=\"0\"/><tt:Zoom x=\"0\"/></tt:Position>"
                    "<tt:MoveStatus><tt:PanTilt>IDLE</tt:PanTilt><tt:Zoom>IDLE</tt:Zoom></tt:MoveStatus>"
                    "<tt:UtcTime>1970-01-01T00:00:00Z</tt:UtcTime>"
                    "</tptz:PTZStatus></tptz:GetStatusResponse>")
        if action == "GetPresets":
            return "<tptz:GetPresetsResponse/>"
        if action == "ContinuousMove":
            if not self.ptz:
                return "<tptz:ContinuousMoveResponse/>"
            self._do_move(body)
            return "<tptz:ContinuousMoveResponse/>"
        if action == "Stop":
            if self.ptz:
                self.ptz.stop_all()
            return "<tptz:StopResponse/>"
        if action == "RelativeMove":
            if not self.ptz:
                return "<tptz:RelativeMoveResponse/>"
            self._do_move(body, relative=True)
            return "<tptz:RelativeMoveResponse/>"
        if action == "AbsoluteMove":
            return "<tptz:AbsoluteMoveResponse/>"
        if action == "GotoPreset":
            pm = re.search(r"PresetToken>\s*(\d+)", body)
            if pm and self.ptz:
                self.ptz.move("GotoPreset", speed=int(pm.group(1)), run=True)
            return "<tptz:GotoPresetResponse/>"
        if action == "SetPreset":
            return "<tptz:SetPresetResponse><tptz:PresetToken>1</tptz:PresetToken></tptz:SetPresetResponse>"
        return f"<{action}Response/>"

    def _do_move(self, body: str, relative: bool = False):
        def num(tag):
            m = re.search(rf'<(?:\w+:)?{tag}[^>]*\bx="(-?[\d.]+)"(?:[^>]*\by="(-?[\d.]+)")?', body)
            if not m:
                return 0.0, 0.0
            return float(m.group(1)), float(m.group(2)) if m.group(2) else 0.0
        x, y = num("PanTilt")
        zx, _ = num("Zoom")
        speed = max(1, min(8, int(round(max(abs(x), abs(y), abs(zx)) * 8)) or 4))
        # dominant axis -> Dahua code; for relative, do a brief nudge
        code = None
        if abs(x) >= abs(y) and abs(x) >= abs(zx) and x != 0:
            code = "Right" if x > 0 else "Left"
        elif abs(y) >= abs(zx) and y != 0:
            code = "Up" if y > 0 else "Down"
        elif zx != 0:
            code = "ZoomTele" if zx > 0 else "ZoomWide"
        if not code:
            if self.ptz:
                self.ptz.stop_all()
            return
        if not self.ptz:
            return
        if relative:
            self.ptz.move(code, speed, run=True)
            import time
            time.sleep(0.5)
            self.ptz.move(code, speed, run=False)
        else:
            self.ptz.move(code, speed, run=True)


def main():
    if PTZ_ENABLED:
        Shim.ptz = PtzSession(
            os.environ.get("DVRIP_HOST", "127.0.0.1"),
            int(os.environ.get("DVRIP_PORT", "37777")),
            os.environ.get("DVRIP_USER", "admin"),
            os.environ.get("DVRIP_PASS", ""),
        )
    srv = ThreadingHTTPServer(("0.0.0.0", ONVIF_PORT), Shim)
    ptz_target = f" -> DVRIP {os.environ.get('DVRIP_HOST')}:{os.environ.get('DVRIP_PORT')}" if PTZ_ENABLED else ""
    print(f"[onvif-shim] ONVIF on :{ONVIF_PORT} stream={RTSP_URI}{ptz_target}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
