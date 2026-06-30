# Cloud P2P Stream Path (Easy4IP / Dahua PTCP)

How the stream (and talk) reaches a camera over the **cloud P2P** path — the
route the Imou app actually uses for its devices, and the one a standalone bridge
uses to reach a camera from anywhere (not just the LAN). Derived from the
`dh-p2p` PoC (`artifacts/research/dh-p2p`, `main.py` + `helpers.py`) and verified
end-to-end against the real device.

## Verified result

Ran `dh-p2p <serial>` for the account camera "example camera" (`EXAMPLESERIAL01`). It
established a PTCP tunnel to the device's P2P address `<camera-public-ip>:19779`
(the same peer the app uses) and exposed `127.0.0.1:<port> → camera:554`. Over
that tunnel:

- **RTSP receive works**: HEVC video + AAC 16 kHz (`ffprobe` via the tunnel).
- **RTSP talk accepted**: `ANNOUNCE … RTSP/1.0 → 200 OK`, audio bytes streamed,
  no reset (two simultaneous tunnels — one receive, one talk — also worked).
- Credentials `admin/<pwd>` worked for example camera too (same account password).

So the cloud path needs **only the serial** to build the tunnel; the camera
password is used only for the RTSP digest auth (and for P2P device-auth when
`type>0`). Serial alone → tunnel → RTSP gates the actual media.

## Architecture (3 parties)

```
This bridge  <--1-->  Easy4IPCloud (rendezvous)  <--2-->  Camera/NVR
     \________________________ 3 (PTCP/UDP) ________________________/
```

1. Bridge asks Easy4IPCloud to locate the device by serial.
2. Cloud tells the device to prepare for an incoming P2P connection.
3. Bridge and device connect directly (UDP hole-punch + PTCP), or via a relay.

## Rendezvous signaling (UDP "DHGET/DHPOST" to `www.easy4ipcloud.com:8800`)

A custom HTTP-like protocol over UDP, authenticated with **WSSE** using the
**app's built-in P2P API credentials** (hardcoded, not the user account):

```
USERNAME = cba1b29e32cb17aa46b8ff9e73c7f40b
USERKEY  = 996103384cdf19179e19243e959bbf8b
X-WSSE: UsernameToken Username="<USERNAME>", PasswordDigest="<b64(sha1(nonce+date+"DHP2P:USERNAME:USERKEY"))>", Nonce="<n>", Created="<date>"
```

Sequence (each step returns XML, parsed for the next hop):

```
main(www.easy4ipcloud.com:8800):  /probe/p2psrv
                                  /online/p2psrv/{SN}      -> US: p2psrv host:port
p2psrv:                           /probe/device/{SN}
                                  /info/device/{SN}        -> device status + randsalt
main:                             /online/relay            -> relay host:port
device-channel(main):             /device/{SN}/p2p-channel  (body: Identify aid, LocalAddr; +DevAuth if type>0)
relay:                            /relay/agent             -> Token + Agent host:port
agent:                            /relay/start/{Token}     (body <Client>:0</Client>)
device-channel reply:             -> device PubAddr + LocalAddr (+Nonce, encrypted if type>0)
main:                             /device/{SN}/relay-channel  (body: agentAddr)
```

### Device auth (only for `type>0`, devices that require it)

```
key   = MD5("<user>:Login to <RANDSALT>:<pwd>").hex().upper()
enc   = AES-OFB(key=pbkdf2_hmac_sha256(key, str(nonce), 20000, 32), iv="2z52*lk9o6HRyJrf")   # encrypts LocalAddr
auth  = base64(hmac_sha256(key, f"{nonce}{curdate}{payload}"))   # DevAuth field
```

The user's cameras worked with **type 0** (no device auth needed for the P2P
channel) — the tunnel came up from the serial alone.

## NAT traversal + PTCP handshake

After rendezvous, a STUN-like UDP exchange on the device's `PubAddr` opens the
NAT (the `\xff\xfe…` packets in `main.py`, using bit-inverted `aid`/`eaddr`).
Then the **PTCP** (PhonyTCP) handshake on the same UDP local port:

- SYN: body `00 03 01 00`
- `0x17` → device returns a **sign** (body[12:])
- `0x19` auth (carries the sign) → `0x1a` server response → `0x1b` client ack
- `0x13` heartbeats keep it alive

### PTCP framing

24-byte header `PTCP | sent | recv | pid | lmid | rmid`, then a body whose first
byte is the type:

```
0x00 SYN        0x10 TCP-data (realm)   0x13 heartbeat
0x11 bind-port  0x12 conn status (CONN/DISC)   0x17/0x19/0x1a/0x1b auth
```

## Stream/talk tunnel (the actual media)

Each local TCP client connection becomes a **realm**:

```
bridge -> device:  0x11 bind-port  realm_id + port=554 (0x22A) + 127.0.0.1
device -> bridge:  0x12 (CONN)
both ways:         0x10 TCP-data  (PTCPPayload: len|0x10000000, realm, payload=raw TCP bytes)
close:             0x12 DISC
```

i.e. the realm is a transparent TCP pipe to the camera's **port 554**. Whatever
you write to the local listener (RTSP `DESCRIBE/SETUP/PLAY` to receive, or
`ANNOUNCE` + audio to talk) is carried verbatim as `0x10` TCP-data. **The stream
and the talk both ride the same port-554 realm** — no extra cloud step for talk.

## Implications

- A standalone "camera as TTS player" over the cloud = `dh-p2p <serial>` tunnel +
  the RTSP `ANNOUNCE` talk (`scripts/`/`talk_send.py`) pointed at the tunnel.
  This works from anywhere, not just the LAN, and matches the app's real path.
- Only the **serial** is needed to build the tunnel (type 0). RTSP digest auth
  (camera user/password) still gates receive and talk.
- The `dh-p2p` PoC is single-client and polling-based (unstable); a production
  bridge should reimplement PTCP with proper multiplexing (multiple realms,
  duplex, reconnect) — the protocol is fully specified above.
- Still open (same as LAN): whether the RTSP `ANNOUNCE` audio framing produces
  clean speaker output. `ANNOUNCE` returns 200 and bytes are accepted; echo
  correlated with the talk window but was not conclusive (camera mic noise-
  suppression kills steady tones). Needs a human listen or RTP-framing refinement
  (proper `SETUP`/`RECORD`, RTP timing/marker, or DHAV packaging).
