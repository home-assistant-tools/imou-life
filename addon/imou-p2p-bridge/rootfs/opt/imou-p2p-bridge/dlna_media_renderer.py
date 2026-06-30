#!/usr/bin/env python3
"""Tiny per-camera DLNA MediaRenderer facade for Home Assistant.

Each enabled Imou camera with talk support is advertised as a UPnP/DLNA
MediaRenderer. Home Assistant can discover it as a media_player and send a media
URL; this service downloads the media, lets imou_pure_talk.py convert it with
ffmpeg, and sends it through the camera's visualtalk path.
"""

from __future__ import annotations

import html
import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request
from urllib.parse import urlparse
import re


SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVT_SERVICE = "urn:schemas-upnp-org:service:AVTransport:1"
RC_SERVICE = "urn:schemas-upnp-org:service:RenderingControl:1"
CM_SERVICE = "urn:schemas-upnp-org:service:ConnectionManager:1"
APP_DIR = Path(__file__).resolve().parent


def log(message: str) -> None:
    print(f"[dlna] {message}", flush=True)


def xml_escape(value: str) -> str:
    return html.escape(value or "", quote=True)


def soap_envelope(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body>{body}</s:Body></s:Envelope>"
    ).encode()


def soap_fault(message: str) -> bytes:
    return soap_envelope(
        "<s:Fault><faultcode>s:Client</faultcode>"
        f"<faultstring>{xml_escape(message)}</faultstring></s:Fault>"
    )


def find_tag(body: str, tag: str) -> str:
    m = re.search(rf"<(?:\w+:)?{tag}\b[^>]*>(.*?)</(?:\w+:)?{tag}>", body, re.S)
    return html.unescape(m.group(1).strip()) if m else ""


def action_name(headers, body: str) -> str:
    soap_action = headers.get("SOAPACTION", "").strip('"')
    if "#" in soap_action:
        return soap_action.rsplit("#", 1)[1]
    m = re.search(r"<(?:\w+:)?Body[^>]*>\s*<(?:(\w+):)?(\w+)", body)
    return m.group(2) if m else ""


@dataclass
class Renderer:
    name: str
    slug: str
    serial: str
    username: str
    password: str
    channel: int = 1
    subtype: int = 0
    port: int = 0
    host: str = "127.0.0.1"
    uuid: str = ""
    current_uri: str = ""
    state: str = "STOPPED"
    last_error: str = ""
    proc: subprocess.Popen | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def udn(self) -> str:
        return f"uuid:{self.uuid}"

    @property
    def location(self) -> str:
        return f"http://{self.host}:{self.port}/description.xml"

    @property
    def friendly_name(self) -> str:
        return f"Imou {self.name}"

    def play_uri(self, uri: str | None = None) -> None:
        with self.lock:
            if uri:
                self.current_uri = uri
            if not self.current_uri:
                raise ValueError("No media URI set")
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
            self.state = "TRANSITIONING"
            self.last_error = ""
            threading.Thread(target=self._play_worker, daemon=True).start()

    def stop(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
            self.proc = None
            self.state = "STOPPED"

    def _play_worker(self) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix=f"imou-dlna-{self.slug}-") as tmp:
                media_path = Path(tmp) / "media"
                download_media(self.current_uri, media_path)
                cmd = [
                    "python3",
                    str(APP_DIR / "imou_pure_talk.py"),
                    "--serial",
                    self.serial,
                    "--username",
                    self.username,
                    "--password",
                    self.password,
                    "--audio",
                    str(media_path),
                    "--channel",
                    str(self.channel),
                    "--subtype",
                    str(self.subtype),
                    "--codec",
                    "aac-adts",
                ]
                log(f"{self.slug}: play {self.current_uri}")
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                with self.lock:
                    self.proc = proc
                    self.state = "PLAYING"
                out, _ = proc.communicate(timeout=180)
                if proc.returncode:
                    raise RuntimeError((out or "").strip() or f"talk exited {proc.returncode}")
                with self.lock:
                    self.state = "STOPPED"
                    self.proc = None
        except Exception as exc:
            with self.lock:
                self.state = "STOPPED"
                self.last_error = str(exc)
                self.proc = None
            log(f"{self.slug}: play failed: {exc}")


def download_media(uri: str, path: Path) -> None:
    parsed = urlparse(uri)
    if parsed.scheme in {"http", "https"}:
        req = request.Request(uri, headers={"User-Agent": "ImouBridge-DLNA/1.0"})
        with request.urlopen(req, timeout=30) as resp:
            path.write_bytes(resp.read())
        return
    if parsed.scheme == "file":
        src = Path(parsed.path)
        path.write_bytes(src.read_bytes())
        return
    raise ValueError(f"Unsupported media URI scheme: {parsed.scheme or 'empty'}")


def description_xml(r: Renderer) -> bytes:
    return f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>{DEVICE_TYPE}</deviceType>
    <friendlyName>{xml_escape(r.friendly_name)}</friendlyName>
    <manufacturer>Imou Bridge</manufacturer>
    <manufacturerURL>https://github.com/</manufacturerURL>
    <modelDescription>Imou camera talk renderer</modelDescription>
    <modelName>Imou Talk Renderer</modelName>
    <modelNumber>1</modelNumber>
    <serialNumber>{xml_escape(r.serial)}</serialNumber>
    <UDN>{r.udn}</UDN>
    <serviceList>
      <service>
        <serviceType>{AVT_SERVICE}</serviceType>
        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
        <SCPDURL>/avtransport.xml</SCPDURL>
        <controlURL>/upnp/control/AVTransport</controlURL>
        <eventSubURL>/upnp/event/AVTransport</eventSubURL>
      </service>
      <service>
        <serviceType>{RC_SERVICE}</serviceType>
        <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
        <SCPDURL>/renderingcontrol.xml</SCPDURL>
        <controlURL>/upnp/control/RenderingControl</controlURL>
        <eventSubURL>/upnp/event/RenderingControl</eventSubURL>
      </service>
      <service>
        <serviceType>{CM_SERVICE}</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/connectionmanager.xml</SCPDURL>
        <controlURL>/upnp/control/ConnectionManager</controlURL>
        <eventSubURL>/upnp/event/ConnectionManager</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>""".encode()


AVT_SCPD = b"""<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>SetAVTransportURI</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>CurrentURI</name><direction>in</direction><relatedStateVariable>AVTransportURI</relatedStateVariable></argument>
      <argument><name>CurrentURIMetaData</name><direction>in</direction><relatedStateVariable>AVTransportURIMetaData</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>SetNextAVTransportURI</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>NextURI</name><direction>in</direction><relatedStateVariable>AVTransportURI</relatedStateVariable></argument>
      <argument><name>NextURIMetaData</name><direction>in</direction><relatedStateVariable>AVTransportURIMetaData</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>Play</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>Speed</name><direction>in</direction><relatedStateVariable>TransportPlaySpeed</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>Stop</name><argumentList><argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument></argumentList></action>
    <action><name>Pause</name><argumentList><argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument></argumentList></action>
    <action><name>GetTransportInfo</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>CurrentTransportState</name><direction>out</direction><relatedStateVariable>TransportState</relatedStateVariable></argument>
      <argument><name>CurrentTransportStatus</name><direction>out</direction><relatedStateVariable>TransportStatus</relatedStateVariable></argument>
      <argument><name>CurrentSpeed</name><direction>out</direction><relatedStateVariable>TransportPlaySpeed</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetMediaInfo</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>NrTracks</name><direction>out</direction><relatedStateVariable>NumberOfTracks</relatedStateVariable></argument>
      <argument><name>MediaDuration</name><direction>out</direction><relatedStateVariable>CurrentMediaDuration</relatedStateVariable></argument>
      <argument><name>CurrentURI</name><direction>out</direction><relatedStateVariable>AVTransportURI</relatedStateVariable></argument>
      <argument><name>CurrentURIMetaData</name><direction>out</direction><relatedStateVariable>AVTransportURIMetaData</relatedStateVariable></argument>
      <argument><name>NextURI</name><direction>out</direction><relatedStateVariable>AVTransportURI</relatedStateVariable></argument>
      <argument><name>NextURIMetaData</name><direction>out</direction><relatedStateVariable>AVTransportURIMetaData</relatedStateVariable></argument>
      <argument><name>PlayMedium</name><direction>out</direction><relatedStateVariable>PlaybackStorageMedium</relatedStateVariable></argument>
      <argument><name>RecordMedium</name><direction>out</direction><relatedStateVariable>RecordStorageMedium</relatedStateVariable></argument>
      <argument><name>WriteStatus</name><direction>out</direction><relatedStateVariable>RecordMediumWriteStatus</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetPositionInfo</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>Track</name><direction>out</direction><relatedStateVariable>CurrentTrack</relatedStateVariable></argument>
      <argument><name>TrackDuration</name><direction>out</direction><relatedStateVariable>CurrentTrackDuration</relatedStateVariable></argument>
      <argument><name>TrackMetaData</name><direction>out</direction><relatedStateVariable>CurrentTrackMetaData</relatedStateVariable></argument>
      <argument><name>TrackURI</name><direction>out</direction><relatedStateVariable>CurrentTrackURI</relatedStateVariable></argument>
      <argument><name>RelTime</name><direction>out</direction><relatedStateVariable>RelativeTimePosition</relatedStateVariable></argument>
      <argument><name>AbsTime</name><direction>out</direction><relatedStateVariable>AbsoluteTimePosition</relatedStateVariable></argument>
      <argument><name>RelCount</name><direction>out</direction><relatedStateVariable>RelativeCounterPosition</relatedStateVariable></argument>
      <argument><name>AbsCount</name><direction>out</direction><relatedStateVariable>AbsoluteCounterPosition</relatedStateVariable></argument>
    </argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_InstanceID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>TransportState</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>TransportStatus</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>TransportPlaySpeed</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>AVTransportURI</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>AVTransportURIMetaData</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>NumberOfTracks</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentMediaDuration</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>PlaybackStorageMedium</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>RecordStorageMedium</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>RecordMediumWriteStatus</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTrack</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTrackDuration</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTrackMetaData</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTrackURI</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>RelativeTimePosition</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>AbsoluteTimePosition</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>RelativeCounterPosition</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>AbsoluteCounterPosition</name><dataType>i4</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""

CM_SCPD = b"""<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetProtocolInfo</name><argumentList>
      <argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
      <argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
    </argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="yes"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""

RC_SCPD = b"""<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetVolume</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
      <argument><name>CurrentVolume</name><direction>out</direction><relatedStateVariable>Volume</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>SetVolume</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
      <argument><name>DesiredVolume</name><direction>in</direction><relatedStateVariable>Volume</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetMute</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
      <argument><name>CurrentMute</name><direction>out</direction><relatedStateVariable>Mute</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>SetMute</name><argumentList>
      <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
      <argument><name>DesiredMute</name><direction>in</direction><relatedStateVariable>Mute</relatedStateVariable></argument>
    </argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_InstanceID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Channel</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>Volume</name><dataType>ui2</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>Mute</name><dataType>boolean</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""


class RendererHandler(BaseHTTPRequestHandler):
    renderer: Renderer

    def log_message(self, *args):
        pass

    def _send(self, body: bytes, content_type: str = "text/xml; charset=utf-8", code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/description.xml":
            self._send(description_xml(self.renderer))
        elif self.path == "/avtransport.xml":
            self._send(AVT_SCPD)
        elif self.path == "/connectionmanager.xml":
            self._send(CM_SCPD)
        elif self.path == "/renderingcontrol.xml":
            self._send(RC_SCPD)
        else:
            self._send(b"Imou DLNA renderer\n", "text/plain")

    def do_SUBSCRIBE(self):
        sid = f"uuid:{uuid.uuid4()}"
        self.send_response(200)
        self.send_header("SID", sid)
        self.send_header("TIMEOUT", "Second-1800")
        self.end_headers()

    def do_UNSUBSCRIBE(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        action = action_name(self.headers, body)
        try:
            response = self.handle_action(action, body)
            self._send(soap_envelope(response))
        except Exception as exc:
            self._send(soap_fault(str(exc)), code=500)

    def handle_action(self, action: str, body: str) -> str:
        r = self.renderer
        if action == "SetAVTransportURI":
            r.current_uri = find_tag(body, "CurrentURI")
            return '<u:SetAVTransportURIResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"/>'
        if action == "SetNextAVTransportURI":
            return '<u:SetNextAVTransportURIResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"/>'
        if action == "Play":
            r.play_uri()
            return '<u:PlayResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"/>'
        if action == "Stop":
            r.stop()
            return '<u:StopResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"/>'
        if action == "Pause":
            r.stop()
            return '<u:PauseResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"/>'
        if action == "GetTransportInfo":
            return (f'<u:GetTransportInfoResponse xmlns:u="{AVT_SERVICE}">'
                    f"<CurrentTransportState>{r.state}</CurrentTransportState>"
                    "<CurrentTransportStatus>OK</CurrentTransportStatus>"
                    "<CurrentSpeed>1</CurrentSpeed></u:GetTransportInfoResponse>")
        if action == "GetMediaInfo":
            uri = xml_escape(r.current_uri)
            return (f'<u:GetMediaInfoResponse xmlns:u="{AVT_SERVICE}"><NrTracks>1</NrTracks>'
                    f"<MediaDuration>00:00:00</MediaDuration><CurrentURI>{uri}</CurrentURI>"
                    "<CurrentURIMetaData/><NextURI/><NextURIMetaData/>"
                    "<PlayMedium>NETWORK</PlayMedium><RecordMedium>NOT_IMPLEMENTED</RecordMedium>"
                    "<WriteStatus>NOT_IMPLEMENTED</WriteStatus></u:GetMediaInfoResponse>")
        if action == "GetPositionInfo":
            uri = xml_escape(r.current_uri)
            return (f'<u:GetPositionInfoResponse xmlns:u="{AVT_SERVICE}"><Track>1</Track>'
                    "<TrackDuration>00:00:00</TrackDuration><TrackMetaData/>"
                    f"<TrackURI>{uri}</TrackURI><RelTime>00:00:00</RelTime>"
                    "<AbsTime>00:00:00</AbsTime><RelCount>0</RelCount><AbsCount>0</AbsCount>"
                    "</u:GetPositionInfoResponse>")
        if action == "GetProtocolInfo":
            info = ",".join([
                "http-get:*:audio/mpeg:*",
                "http-get:*:audio/wav:*",
                "http-get:*:audio/x-wav:*",
                "http-get:*:audio/aac:*",
                "http-get:*:audio/mp4:*",
                "http-get:*:audio/ogg:*",
                "http-get:*:audio/flac:*",
            ])
            return (f'<u:GetProtocolInfoResponse xmlns:u="{CM_SERVICE}">'
                    f"<Source/><Sink>{xml_escape(info)}</Sink></u:GetProtocolInfoResponse>")
        if action == "GetVolume":
            return f'<u:GetVolumeResponse xmlns:u="{RC_SERVICE}"><CurrentVolume>100</CurrentVolume></u:GetVolumeResponse>'
        if action == "SetVolume":
            return f'<u:SetVolumeResponse xmlns:u="{RC_SERVICE}"/>'
        if action == "GetMute":
            return f'<u:GetMuteResponse xmlns:u="{RC_SERVICE}"><CurrentMute>0</CurrentMute></u:GetMuteResponse>'
        if action == "SetMute":
            return f'<u:SetMuteResponse xmlns:u="{RC_SERVICE}"/>'
        return f"<u:{action}Response xmlns:u=\"{AVT_SERVICE}\"/>"


def make_handler(renderer: Renderer):
    class Handler(RendererHandler):
        pass

    Handler.renderer = renderer
    return Handler


def ssdp_packet(renderer: Renderer, st: str, nts: str = "ssdp:alive") -> bytes:
    usn = renderer.udn if st == renderer.udn else f"{renderer.udn}::{st}"
    return (
        "NOTIFY * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        "CACHE-CONTROL: max-age=1800\r\n"
        f"LOCATION: {renderer.location}\r\n"
        "SERVER: Linux/3.14 UPnP/1.0 ImouBridge/1.0\r\n"
        f"NT: {st}\r\n"
        f"NTS: {nts}\r\n"
        f"USN: {usn}\r\n\r\n"
    ).encode()


def search_response(renderer: Renderer, st: str) -> bytes:
    usn = renderer.udn if st == renderer.udn else f"{renderer.udn}::{st}"
    return (
        "HTTP/1.1 200 OK\r\n"
        "CACHE-CONTROL: max-age=1800\r\n"
        "EXT:\r\n"
        f"LOCATION: {renderer.location}\r\n"
        "SERVER: Linux/3.14 UPnP/1.0 ImouBridge/1.0\r\n"
        f"ST: {st}\r\n"
        f"USN: {usn}\r\n\r\n"
    ).encode()


def ssdp_loop(renderers: list[Renderer], stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.bind(("", SSDP_PORT))
    mreq = struct.pack("4sl", socket.inet_aton(SSDP_ADDR), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(1.0)

    def notify_all() -> None:
        for renderer in renderers:
            for st in ("upnp:rootdevice", renderer.udn, DEVICE_TYPE):
                sock.sendto(ssdp_packet(renderer, st), (SSDP_ADDR, SSDP_PORT))

    notify_all()
    last_notify = time.monotonic()
    while not stop.is_set():
        if time.monotonic() - last_notify > 60:
            notify_all()
            last_notify = time.monotonic()
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        text = data.decode("latin1", "ignore").upper()
        if "M-SEARCH" not in text or "SSDP:DISCOVER" not in text:
            continue
        st = "*"
        for line in data.decode("latin1", "ignore").splitlines():
            if line.upper().startswith("ST:"):
                st = line.split(":", 1)[1].strip()
                break
        for renderer in renderers:
            if st in ("ssdp:all", "*"):
                targets = ("upnp:rootdevice", renderer.udn, DEVICE_TYPE)
            elif st in ("upnp:rootdevice", renderer.udn, DEVICE_TYPE):
                targets = (st,)
            else:
                continue
            for target in targets:
                sock.sendto(search_response(renderer, target), addr)


def load_renderers() -> list[Renderer]:
    raw = json.loads(os.environ.get("IMOU_RENDERERS_JSON", "[]"))
    host = os.environ.get("ADVERTISED_HOST", "127.0.0.1")
    renderers: list[Renderer] = []
    for item in raw:
        serial = str(item.get("serial") or "")
        if not serial:
            log(f"skip {item.get('name')}: no serial for talk")
            continue
        slug = str(item.get("slug") or item.get("name") or serial)
        stable = uuid.uuid5(uuid.NAMESPACE_DNS, f"imou-bridge:{serial}:{slug}")
        renderers.append(Renderer(
            name=str(item.get("name") or slug),
            slug=slug,
            serial=serial,
            username=str(item.get("username") or "admin"),
            password=str(item.get("password") or ""),
            channel=int(item.get("channel") or 1),
            subtype=int(item.get("subtype") or 0),
            port=int(item.get("port") or 0),
            host=host,
            uuid=str(stable),
        ))
    return renderers


def main() -> int:
    renderers = load_renderers()
    if not renderers:
        log("no talk-enabled renderers")
        return 0
    stop = threading.Event()
    servers: list[ThreadingHTTPServer] = []
    for renderer in renderers:
        srv = ThreadingHTTPServer(("0.0.0.0", renderer.port), make_handler(renderer))
        servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log(f"{renderer.slug}: MediaRenderer at {renderer.location}")
    threading.Thread(target=ssdp_loop, args=(renderers, stop), daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
        for srv in servers:
            srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
