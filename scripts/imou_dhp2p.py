#!/usr/bin/env python3
"""Pure-Python Dahua/Imou DHP2P + PTCP TCP tunnel.

This is a Python port of the local `artifacts/research/dh-p2p` PoC plus the
async multi-realm shape from the Rust version. It exposes a local TCP listener
whose accepted connections are bound to a remote device TCP port, usually
554/RTSP or 8086/DHHTTP visualtalk.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import random
import re
import socket
import struct
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

try:
    from imou_wsse import (
        DEFAULT_RAND_SALT,
        build_device_auth_fields,
        decrypt_local_addr,
        device_login_key,
        dhp2p_wsse_header,
        encrypt_local_addr,
    )
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from imou_wsse import (  # type: ignore[no-redef]
        DEFAULT_RAND_SALT,
        build_device_auth_fields,
        decrypt_local_addr,
        device_login_key,
        dhp2p_wsse_header,
        encrypt_local_addr,
    )


MAIN_SERVER = "www.easy4ipcloud.com"
MAIN_PORT = 8800

# Public DHP2P rendezvous application credentials recovered in the upstream PoC.
DHP2P_USERNAME = "cba1b29e32cb17aa46b8ff9e73c7f40b"
DHP2P_USERKEY = "996103384cdf19179e19243e959bbf8b"

PTCP_SYNC = b"\x00\x03\x01\x00"
PTCP_HEARTBEAT = b"\x13\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
PTCP_AUTH_17 = b"\x17\x00\x00\x00" + b"\x00" * 8
PTCP_AUTH_1B = b"\x1b\x00\x00\x00" + b"\x00" * 8
STUN_TIMEOUT = 5.0


def redact(value: str) -> str:
    value = value.replace(DHP2P_USERNAME, "<dhp2p-user>").replace(DHP2P_USERKEY, "<dhp2p-key>")
    value = re.sub(r'(PasswordDigest=")[^"]+', r"\1<redacted>", value)
    value = re.sub(r"(/relay/start/)[^\s]+", r"\1<redacted>", value)
    value = re.sub(r"(<Token>)[^<]+", r"\1<redacted>", value)
    return value


def random_bytes(length: int) -> bytes:
    return os.urandom(length)


def invert(data: bytes) -> bytes:
    return bytes(0xFF - b for b in data)


def ip_port_to_inverted_bytes(addr: str) -> bytes:
    host, port_text = addr.rsplit(":", 1)
    raw = int(port_text).to_bytes(2, "big") + ipaddress.IPv4Address(host).packed
    return invert(raw)


def flatten_xml(xml_text: str) -> dict[str, str]:
    if not xml_text.strip():
        return {}
    out: dict[str, str] = {}
    stack: list[str] = []
    token_re = re.compile(r"<(/?)([A-Za-z0-9_:-]+)(?:\s[^>]*)?>|([^<]+)")
    for match in token_re.finditer(xml_text):
        closing, tag, text = match.groups()
        if tag:
            if closing:
                if stack and stack[-1] == tag:
                    stack.pop()
                continue
            full = match.group(0)
            if full.endswith("/>"):
                continue
            stack.append(tag)
        elif text and stack:
            value = text.strip()
            if value:
                out["/".join(stack)] = value
                out[stack[-1]] = value
    return out


@dataclass(frozen=True)
class DHResponse:
    version: str
    code: int
    status: str
    headers: dict[str, str]
    body: dict[str, str]

    @classmethod
    def parse(cls, data: bytes) -> "DHResponse":
        text = data.decode(errors="replace")
        head, _, body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        version, code_text, status = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ": " in line:
                key, value = line.split(": ", 1)
                headers[key] = value
        return cls(version, int(code_text), status, headers, flatten_xml(body))


class AsyncUdpPeer:
    def __init__(self, debug: bool = False) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.setblocking(False)
        self.debug = debug
        self.remote: tuple[str, int] | None = None

    @property
    def local_port(self) -> int:
        return self.sock.getsockname()[1]

    async def connect(self, host: str, port: int) -> None:
        self.remote = (host, port)
        self.sock.connect(self.remote)

    async def send(self, data: bytes) -> None:
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(self.sock, data)

    async def recv(self, size: int = 65535, timeout: float | None = None) -> bytes:
        loop = asyncio.get_running_loop()
        coro = loop.sock_recv(self.sock, size)
        if timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout)

    async def dh_request(
        self,
        path: str,
        *,
        body: str = "",
        cseq: int,
        auth: bool = True,
        username: str = DHP2P_USERNAME,
        userkey: str = DHP2P_USERKEY,
    ) -> None:
        method = "DHPOST" if body else "DHGET"
        lines = [f"{method} {path} HTTP/1.1", f"CSeq: {cseq}"]
        if auth:
            lines.append('Authorization: WSSE profile="UsernameToken"')
            lines.append(f"X-WSSE: {dhp2p_wsse_header(username, userkey)}")
        if body:
            lines.append("Content-Type: ")
            lines.append(f"Content-Length: {len(body)}")
        request = "\r\n".join(lines) + "\r\n\r\n" + body
        if self.debug:
            print(f">>> {self.remote} {path}")
            print(redact(request))
        await self.send(request.encode())

    async def dh_read(self, *, allow_error: bool = False, timeout: float | None = None) -> DHResponse:
        data = await self.recv(timeout=timeout)
        response = DHResponse.parse(data)
        if self.debug:
            print(f"<<< {self.remote} {response.code} {response.status}")
            print(response.body)
        if not allow_error and response.code >= 300:
            raise RuntimeError(f"DH response {response.code}: {response.status}")
        return response

    def close(self) -> None:
        self.sock.close()


class BodyKind(str, Enum):
    SYNC = "sync"
    COMMAND = "command"
    PAYLOAD = "payload"
    BIND = "bind"
    STATUS = "status"
    HEARTBEAT = "heartbeat"
    EMPTY = "empty"


@dataclass(frozen=True)
class PTCPPayload:
    realm: int
    data: bytes

    def serialize(self) -> bytes:
        return struct.pack("!LLL", 0x10000000 | len(self.data), self.realm, 0) + self.data

    @classmethod
    def parse(cls, data: bytes) -> "PTCPPayload":
        if len(data) < 12 or data[0] != 0x10:
            raise ValueError("invalid PTCP payload")
        header, realm, padding = struct.unpack("!LLL", data[:12])
        length = header & 0xFFFF
        payload = data[12:]
        if padding != 0 or len(payload) != length:
            raise ValueError("invalid PTCP payload length/padding")
        return cls(realm, payload)


@dataclass(frozen=True)
class PTCPBody:
    kind: BodyKind
    data: bytes = b""
    realm: int = 0
    port: int = 0
    status: str = ""

    @classmethod
    def empty(cls) -> "PTCPBody":
        return cls(BodyKind.EMPTY)

    @classmethod
    def sync(cls) -> "PTCPBody":
        return cls(BodyKind.SYNC)

    @classmethod
    def command(cls, data: bytes) -> "PTCPBody":
        return cls(BodyKind.COMMAND, data=data)

    @classmethod
    def heartbeat(cls) -> "PTCPBody":
        return cls(BodyKind.HEARTBEAT)

    @classmethod
    def bind(cls, realm: int, port: int) -> "PTCPBody":
        return cls(BodyKind.BIND, realm=realm, port=port)

    @classmethod
    def status_body(cls, realm: int, status: str) -> "PTCPBody":
        return cls(BodyKind.STATUS, realm=realm, status=status)

    @classmethod
    def payload(cls, realm: int, data: bytes) -> "PTCPBody":
        return cls(BodyKind.PAYLOAD, realm=realm, data=data)

    @property
    def wire_len(self) -> int:
        if self.kind is BodyKind.SYNC:
            return 4
        if self.kind is BodyKind.PAYLOAD:
            return len(self.data) + 12
        if self.kind is BodyKind.BIND:
            return 20
        if self.kind is BodyKind.STATUS:
            return len(self.status) + 12
        if self.kind is BodyKind.HEARTBEAT:
            return 12
        if self.kind is BodyKind.COMMAND:
            return len(self.data)
        return 0

    def serialize(self) -> bytes:
        if self.kind is BodyKind.SYNC:
            return PTCP_SYNC
        if self.kind is BodyKind.COMMAND:
            return self.data
        if self.kind is BodyKind.PAYLOAD:
            return PTCPPayload(self.realm, self.data).serialize()
        if self.kind is BodyKind.BIND:
            return (
                b"\x11\x00\x00\x00"
                + self.realm.to_bytes(4, "big")
                + b"\x00\x00\x00\x00"
                + self.port.to_bytes(4, "big")
                + b"\x7f\x00\x00\x01"
            )
        if self.kind is BodyKind.STATUS:
            return b"\x12\x00\x00\x00" + self.realm.to_bytes(4, "big") + b"\x00\x00\x00\x00" + self.status.encode()
        if self.kind is BodyKind.HEARTBEAT:
            return PTCP_HEARTBEAT
        return b""

    @classmethod
    def parse(cls, data: bytes) -> "PTCPBody":
        if not data:
            return cls.empty()
        if len(data) < 4:
            raise ValueError("invalid PTCP body")
        tag = data[0]
        if tag == 0x00:
            return cls.sync()
        if tag == 0x10:
            payload = PTCPPayload.parse(data)
            return cls.payload(payload.realm, payload.data)
        if tag == 0x11:
            return cls.bind(int.from_bytes(data[4:8], "big"), int.from_bytes(data[12:16], "big"))
        if tag == 0x12:
            return cls.status_body(int.from_bytes(data[4:8], "big"), data[12:].decode(errors="replace"))
        if tag == 0x13:
            return cls.heartbeat()
        return cls.command(data)


@dataclass(frozen=True)
class PTCPPacket:
    sent: int
    recv: int
    pid: int
    lmid: int
    rmid: int
    body: PTCPBody

    def serialize(self) -> bytes:
        return b"PTCP" + struct.pack("!LLLLL", self.sent, self.recv, self.pid, self.lmid, self.rmid) + self.body.serialize()

    @classmethod
    def parse(cls, data: bytes) -> "PTCPPacket":
        if len(data) < 24 or data[:4] != b"PTCP":
            raise ValueError("invalid PTCP packet")
        sent, recv, pid, lmid, rmid = struct.unpack("!LLLLL", data[4:24])
        return cls(sent, recv, pid, lmid, rmid, PTCPBody.parse(data[24:]))


class PTCPSession:
    def __init__(self) -> None:
        self.sent = 0
        self.recv = 0
        self.count = 0
        self.ident = 0
        self.rmid = 0

    def make_packet(self, body: PTCPBody) -> PTCPPacket:
        pid = 0x0002FFFF if body.kind is BodyKind.SYNC else 0x0000FFFF - self.count
        packet = PTCPPacket(self.sent, self.recv, pid, self.ident, self.rmid, body)
        self.sent += body.wire_len
        self.ident += 1
        if body.kind not in {BodyKind.SYNC, BodyKind.EMPTY}:
            self.count += 1
        return packet

    def observe(self, packet: PTCPPacket) -> PTCPPacket:
        self.recv += packet.body.wire_len
        self.rmid = packet.lmid
        return packet


class PTCPPeer:
    def __init__(self, udp: AsyncUdpPeer, session: PTCPSession, *, debug: bool = False) -> None:
        self.udp = udp
        self.session = session
        self.debug = debug
        self.lock = asyncio.Lock()

    async def send_body(self, body: PTCPBody) -> None:
        async with self.lock:
            packet = self.session.make_packet(body)
            if self.debug:
                print(f"PTCP >>> {format_ptcp_packet(packet)}")
            await self.udp.send(packet.serialize())

    async def read_packet(self, *, timeout: float | None = None) -> PTCPPacket:
        packet = PTCPPacket.parse(await self.udp.recv(timeout=timeout))
        async with self.lock:
            packet = self.session.observe(packet)
        if self.debug:
            print(f"PTCP <<< {format_ptcp_packet(packet)}")
        return packet

    async def ack_if_needed(self, packet: PTCPPacket) -> None:
        if packet.body.kind is not BodyKind.EMPTY:
            await self.send_body(PTCPBody.empty())

    async def drain_control(self, *, timeout: float = 0.35) -> None:
        """Consume early relay control packets before opening client realms."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                packet = await self.read_packet(timeout=remaining)
            except asyncio.TimeoutError:
                return
            await self.ack_if_needed(packet)


def format_ptcp_packet(packet: PTCPPacket) -> str:
    body = packet.body
    extra = ""
    if body.kind in {BodyKind.BIND, BodyKind.STATUS, BodyKind.PAYLOAD}:
        extra += f" realm={body.realm:08x}"
    if body.kind is BodyKind.BIND:
        extra += f" port={body.port}"
    if body.kind is BodyKind.STATUS:
        extra += f" status={body.status}"
    if body.kind is BodyKind.PAYLOAD:
        extra += f" data={len(body.data)}"
    return (
        f"sent={packet.sent} recv={packet.recv} pid=0x{packet.pid:08x} "
        f"lmid=0x{packet.lmid:08x} rmid=0x{packet.rmid:08x} "
        f"{body.kind.value} len={body.wire_len}{extra}"
    )


class CSeq:
    def __init__(self) -> None:
        self.value = 0

    def next(self) -> int:
        self.value += 1
        return self.value


def body_value(response: DHResponse, key: str) -> str:
    for candidate in (key, f"body/{key}", f"body/{key.strip('/')}"):
        if candidate in response.body:
            return response.body[candidate]
    raise KeyError(f"{key} missing in DH response body: {response.body}")


async def request(peer: AsyncUdpPeer, path: str, cseq: CSeq, *, body: str = "", allow_error: bool = False) -> DHResponse:
    await peer.dh_request(path, body=body, cseq=cseq.next())
    return await peer.dh_read(allow_error=allow_error)


async def p2p_handshake(
    serial: str,
    *,
    relay_mode: bool = False,
    dtype: int = 0,
    username: str | None = None,
    password: str | None = None,
    rand_salt: str = DEFAULT_RAND_SALT,
    debug: bool = False,
) -> PTCPPeer:
    if dtype > 0 and (not username or not password):
        raise ValueError("username/password are required for dtype > 0")

    cseq = CSeq()
    main = AsyncUdpPeer(debug=debug)
    await main.connect(MAIN_SERVER, MAIN_PORT)

    await request(main, "/probe/p2psrv", cseq)
    p2psrv_response = await request(main, f"/online/p2psrv/{serial}", cseq)
    p2psrv_host, p2psrv_port_text = body_value(p2psrv_response, "US").split(":")
    relay_response = await request(main, "/online/relay", cseq)
    relay_host, relay_port_text = body_value(relay_response, "Address").split(":")

    p2psrv = AsyncUdpPeer(debug=debug)
    await p2psrv.connect(p2psrv_host, int(p2psrv_port_text))
    await request(p2psrv, f"/probe/device/{serial}", cseq)
    info = await request(p2psrv, f"/info/device/{serial}", cseq, allow_error=True)
    if "RandSalt" in info.body:
        rand_salt = info.body["RandSalt"]
    p2psrv.close()

    device = main
    identify = random_bytes(8)
    local_addr = f"127.0.0.1:{device.local_port}"
    auth_xml = ""
    if dtype > 0:
        assert username is not None and password is not None
        nonce = random.randrange(2**31)
        key = device_login_key(username, password, rand_salt)
        encrypted_local = encrypt_local_addr(key, nonce, local_addr)
        auth_xml = build_device_auth_fields(
            username,
            password,
            nonce=nonce,
            rand_salt=rand_salt,
            payload=encrypted_local,
        ).to_xml_fields()
        local_xml = f"<IpEncrptV2>true</IpEncrptV2><LocalAddr>{encrypted_local}</LocalAddr>"
    else:
        local_xml = f"<IpEncrpt>true</IpEncrpt><LocalAddr>{local_addr}</LocalAddr>"

    identify_text = " ".join(f"{b:x}" for b in identify)
    channel_body = f"<body>{auth_xml}<Identify>{identify_text}</Identify>{local_xml}<version>5.0.0</version></body>"
    await device.dh_request(f"/device/{serial}/p2p-channel", body=channel_body, cseq=cseq.next())
    await asyncio.sleep(0.2)

    relay = AsyncUdpPeer(debug=debug)
    await relay.connect(relay_host, int(relay_port_text))
    agent_response = await request(relay, "/relay/agent", cseq)
    token = body_value(agent_response, "Token")
    agent_addr = body_value(agent_response, "Agent")
    agent_host, agent_port_text = agent_addr.split(":")

    await relay.connect(agent_host, int(agent_port_text))
    await request(relay, f"/relay/start/{token}", cseq, body="<body><Client>:0</Client></body>")
    await asyncio.sleep(0.2)

    device_response = await device.dh_read(allow_error=True, timeout=12)
    if device_response.code == 100:
        device_response = await device.dh_read(allow_error=True, timeout=12)
    if device_response.code >= 400:
        raise RuntimeError(f"device p2p-channel failed: {device_response.code} {device_response.status}")

    device_pub_addr = body_value(device_response, "PubAddr")
    device_local_addr = body_value(device_response, "LocalAddr")
    if dtype > 0:
        assert username is not None and password is not None
        key = device_login_key(username, password, rand_salt)
        device_nonce = body_value(device_response, "Nonce")
        device_local_addr = decrypt_local_addr(key, device_nonce, device_local_addr)

    relay_channel_body = f"<body><agentAddr>{agent_addr}</agentAddr></body>"
    if dtype > 0:
        assert username is not None and password is not None
        relay_channel_body = (
            "<body>"
            + build_device_auth_fields(username, password, rand_salt=rand_salt).to_xml_fields()
            + f"<agentAddr>{agent_addr}</agentAddr></body>"
        )

    relay_channel_path = f"/device/{serial}/relay-channel"
    relay_agent_port = int(agent_port_text)
    for attempt in range(3):
        await relay.connect(MAIN_SERVER, MAIN_PORT)
        await relay.dh_request(relay_channel_path, body=relay_channel_body, cseq=cseq.next())
        await relay.connect(agent_host, relay_agent_port)
        try:
            await relay.dh_read(timeout=8)
            break
        except asyncio.TimeoutError:
            if attempt == 2:
                raise
            await asyncio.sleep(0.5)

    session = PTCPSession()
    relay_ptcp = PTCPPeer(relay, session, debug=debug)
    await relay_ptcp.send_body(PTCPBody.sync())
    sync_packet = await relay_ptcp.read_packet()
    if sync_packet.body.kind is not BodyKind.SYNC:
        raise RuntimeError("relay PTCP sync failed")
    if relay_mode:
        device.close()
        return relay_ptcp

    await relay_ptcp.send_body(PTCPBody.command(PTCP_AUTH_17))
    sign_packet = await relay_ptcp.read_packet()
    while sign_packet.body.kind is BodyKind.EMPTY:
        sign_packet = await relay_ptcp.read_packet()
    if sign_packet.body.kind is not BodyKind.COMMAND:
        raise RuntimeError("relay sign packet was not a command")
    sign = sign_packet.body.data[12:]

    await device.connect(*_split_addr(device_pub_addr))
    await _direct_hole_punch(device, identify, device_pub_addr, device_local_addr, debug=debug)

    direct_session = PTCPSession()
    direct_ptcp = PTCPPeer(device, direct_session, debug=debug)
    await direct_ptcp.send_body(PTCPBody.sync())
    sync_packet = await direct_ptcp.read_packet()
    if sync_packet.body.kind is not BodyKind.SYNC:
        raise RuntimeError("direct PTCP sync failed")

    await direct_ptcp.send_body(PTCPBody.command(b"\x19\x00\x00\x00" + b"\x00" * 8 + sign))
    auth_packet = await direct_ptcp.read_packet()
    while auth_packet.body.kind is BodyKind.EMPTY:
        auth_packet = await direct_ptcp.read_packet()
    if auth_packet.body.kind is not BodyKind.COMMAND or not auth_packet.body.data.startswith(b"\x1a"):
        raise RuntimeError("direct PTCP auth failed")

    await direct_ptcp.send_body(PTCPBody.command(PTCP_AUTH_1B))
    ack_packet = await direct_ptcp.read_packet()
    if ack_packet.body.kind is not BodyKind.EMPTY:
        raise RuntimeError("direct PTCP final ack failed")

    relay.close()
    return direct_ptcp


def _split_addr(addr: str) -> tuple[str, int]:
    host, port_text = addr.rsplit(":", 1)
    return host, int(port_text)


async def _direct_hole_punch(device: AsyncUdpPeer, identify: bytes, device_pub_addr: str, device_local_addr: str, *, debug: bool) -> None:
    cid = invert(identify)
    cookie = random_bytes(4)
    trans_id = random_bytes(12)
    first = b"\xff\xfe\xff\xe7" + cookie + trans_id + b"\x7f\xd5\xff\xf7" + cid + b"\xff\xfb\xff\xf7\xff\xfe" + ip_port_to_inverted_bytes(device_pub_addr)
    if debug:
        print(f"STUN >>> {first.hex()}")
    await device.send(first)
    response = await device.recv(timeout=STUN_TIMEOUT)
    if debug:
        print(f"STUN <<< {response.hex()}")
    reply_trans_id = response[8:20]
    second = b"\xfe\xfe\xff\xe7" + cookie + reply_trans_id + b"\x7f\xd6\xff\xf7" + cid + b"\xff\xfb\xff\xf7\xff\xfe" + ip_port_to_inverted_bytes(device_local_addr)
    if debug:
        print(f"STUN >>> {second.hex()}")
    await device.send(second)
    for _ in range(5):
        try:
            data = await device.recv(timeout=1.0)
        except asyncio.TimeoutError:
            break
        if debug:
            print(f"STUN <<< {data.hex()}")


class DHP2PTunnel:
    def __init__(self, ptcp: PTCPPeer, remote_port: int, *, debug: bool = False) -> None:
        self.ptcp = ptcp
        self.remote_port = remote_port
        self.debug = debug
        self.queues: dict[int, asyncio.Queue[bytes | None]] = {}
        self.connected: dict[int, asyncio.Future[bool]] = {}

    async def start(self, host: str, port: int) -> None:
        server = await asyncio.start_server(self._accept_client, host, port)
        reader = asyncio.create_task(self._ptcp_reader())
        heartbeat = asyncio.create_task(self._heartbeat())
        sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        print(f"Pure Python DHP2P tunnel listening on {sockets}; remote port {self.remote_port}")
        try:
            async with server:
                await server.serve_forever()
        finally:
            reader.cancel()
            heartbeat.cancel()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self.ptcp.send_body(PTCPBody.heartbeat())

    async def _accept_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        realm = random.randrange(0, 2**32)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=128)
        connected = asyncio.get_running_loop().create_future()
        first_data = asyncio.create_task(reader.read(4096))
        self.queues[realm] = queue
        self.connected[realm] = connected
        client_to_ptcp: asyncio.Task[None] | None = None
        ptcp_to_client: asyncio.Task[None] | None = None
        did_bind = False
        try:
            await self.ptcp.send_body(PTCPBody.bind(realm, self.remote_port))
            did_bind = True
            try:
                await asyncio.wait_for(asyncio.shield(connected), timeout=10)
            except asyncio.TimeoutError:
                if self.debug:
                    print(f"PTCP bind timeout realm={realm:08x}")
                return
            if first_data.done() and not first_data.result():
                return
            if not first_data.done():
                try:
                    await asyncio.wait_for(asyncio.shield(first_data), timeout=0.05)
                except asyncio.TimeoutError:
                    pass
            if first_data.done() and first_data.result():
                await self.ptcp.send_body(PTCPBody.payload(realm, first_data.result()))
                client_to_ptcp = asyncio.create_task(self._client_to_ptcp(realm, reader))
            else:
                client_to_ptcp = asyncio.create_task(self._client_to_ptcp_with_first(realm, reader, first_data))
            ptcp_to_client = asyncio.create_task(self._ptcp_to_client(realm, queue, writer))
            await asyncio.wait({client_to_ptcp, ptcp_to_client}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            first_data.cancel()
            if client_to_ptcp:
                client_to_ptcp.cancel()
            if ptcp_to_client:
                ptcp_to_client.cancel()
            if did_bind:
                try:
                    await self.ptcp.send_body(PTCPBody.status_body(realm, "DISC"))
                except Exception as exc:
                    if self.debug:
                        print(f"PTCP cleanup failed for realm {realm:08x}: {exc}")
            self.queues.pop(realm, None)
            self.connected.pop(realm, None)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                pass

    async def _client_to_ptcp_with_first(
        self,
        realm: int,
        reader: asyncio.StreamReader,
        first_data: asyncio.Task[bytes],
    ) -> None:
        data = await first_data
        if not data:
            return
        await self.ptcp.send_body(PTCPBody.payload(realm, data))
        await self._client_to_ptcp(realm, reader)

    async def _client_to_ptcp(self, realm: int, reader: asyncio.StreamReader) -> None:
        while True:
            data = await reader.read(4096)
            if not data:
                return
            await self.ptcp.send_body(PTCPBody.payload(realm, data))

    async def _ptcp_to_client(self, realm: int, queue: asyncio.Queue[bytes | None], writer: asyncio.StreamWriter) -> None:
        while True:
            data = await queue.get()
            if data is None:
                return
            writer.write(data)
            await writer.drain()

    async def _ptcp_reader(self) -> None:
        while True:
            packet = await self.ptcp.read_packet()
            if packet.body.kind is BodyKind.EMPTY:
                continue
            await self.ptcp.send_body(PTCPBody.empty())
            body = packet.body
            if body.kind is BodyKind.HEARTBEAT:
                continue
            if body.kind is BodyKind.STATUS:
                if self.debug:
                    print(f"PTCP status realm={body.realm:08x} {body.status}")
                if body.status == "CONN" and body.realm in self.connected and not self.connected[body.realm].done():
                    self.connected[body.realm].set_result(True)
                elif body.status == "DISC" and body.realm in self.queues:
                    await self.queues[body.realm].put(None)
            elif body.kind is BodyKind.PAYLOAD and body.realm in self.queues:
                await self.queues[body.realm].put(body.data)


def parse_bind(value: str) -> tuple[str, int]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("bind must be host:port")
    host, port_text = value.rsplit(":", 1)
    return host, int(port_text)


async def amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("serial", help="camera/NVR serial")
    parser.add_argument("--bind", type=parse_bind, default=("127.0.0.1", 1554))
    parser.add_argument("--remote-port", type=int, default=554)
    parser.add_argument("--relay", action="store_true", help="force relay mode")
    parser.add_argument("--type", type=int, default=0, help="device p2p auth type, default 0")
    parser.add_argument("--username", help="camera username for type > 0")
    parser.add_argument("--password", help="camera password for type > 0")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    bind_host, bind_port = args.bind
    ptcp = await p2p_handshake(
        args.serial,
        relay_mode=args.relay,
        dtype=args.type,
        username=args.username,
        password=args.password,
        debug=args.debug,
    )
    await DHP2PTunnel(ptcp, args.remote_port, debug=args.debug).start(bind_host, bind_port)
    return 0


def self_test() -> None:
    payload = PTCPPayload(0x12345678, b"hello").serialize()
    assert payload == bytes.fromhex("10000005123456780000000068656c6c6f")
    assert PTCPPayload.parse(payload).data == b"hello"
    bind = PTCPBody.bind(0x01020304, 554).serialize()
    assert bind == bytes.fromhex("1100000001020304000000000000022a7f000001")
    packet = PTCPSession().make_packet(PTCPBody.sync()).serialize()
    parsed = PTCPPacket.parse(packet)
    assert parsed.body.kind is BodyKind.SYNC
    assert parsed.pid == 0x0002FFFF
    response = DHResponse.parse(b"HTTP/1.1 200 OK\r\nCSeq: 1\r\n\r\n<body><US>127.0.0.1:1</US></body>")
    assert body_value(response, "US") == "127.0.0.1:1"
    assert ip_port_to_inverted_bytes("1.2.3.4:554").hex() == "fdd5fefdfcfb"
    print("self-test ok")


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
