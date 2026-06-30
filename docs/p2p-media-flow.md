# P2P and Media Flow

## Server Configuration

The app reads server entries from its global configuration. Relevant domain
types include:

- `p2p`
- `p2p-v2`
- `p2p-ipv4only`
- `p2p-ios-alarm`
- `p2p-android-alarm`
- `pss`

The P2P initializer prefers `p2p-v2`, falls back to `p2p`, resolves the hostname
to IPv4/IPv6, then calls the native login SDK.

Observed Java call path:

```text
Ac/a.java
  -> LCSDK_Login.getInstance().initP2PSeverAfterSDKEx(...)
  -> com.iotcom.commonsdk.login.LoginManager.jniInitP2PServerAfterSDKEx(...)
  -> libCommonSDK.so
```

## Local Port Flow

The media layer builds a request like:

```json
{"Sn":"<device-sn>","Type":1,"Port":0,"User":"","Pwd":"","Pid":"<pid>"}
```

Then it calls:

```text
LoginManager.getInstance().getP2PPort(json, stateArray, timeoutMs, outArray)
```

The returned value is a local port. The app then constructs a local media URL.

## Live Stream URLs

For RTSP live view:

```text
rtsp://127.0.0.1:<port>/cam/realmonitor?channel=<channel>&subtype=<stream>&encrypt=<mode>&proto=Private3
```

For HTTP/XAV live view:

```text
127.0.0.1:<port>/live/realmonitor.xav?channel=<channel>&subtype=<stream>&audioType=1&proto=Private3
```

For visual talk:

```text
127.0.0.1:<port>/live/visualtalk.xav?channel=<channel>&subtype=<stream>&audioType=1
```

For playback:

```text
rtsp://127.0.0.1:<port>/cam/playback?channel=<channel>&subtype=<stream>&starttime=<start>&endtime=<end>
127.0.0.1:<port>/vod/playback.xav?channel=<channel>&subtype=<stream>&starttime=<start>&endtime=<end>
```

## Link Types

The app distinguishes stream paths with:

```text
P2P_LOCAL
P2P_P2P
P2P_RELAY
P2P_NULL
MTS
MTS_QUIC
```

Observed code maps `LCSDK_Login.getInstance().getP2PLinkType(did, pid)`:

- `0`: local
- `1`: direct P2P
- `2`: relay
- `-1` or unknown: relay/null fallback

## Native P2P Clues

Important strings from native libraries:

```text
DHProxyRegP2PTraversalInfoHandler
regP2PTraversalInfoHandler
regP2PTraversalInfoHandlerEx
regP2PICEStrLogReportHandler
direct channel Stun Start
relay channel StunFail
relay channel GetRelayStart
p2p ice success
p2p ice fail
p2p,udprelay
p2p-channel
p2p mapped port OK
p2p mapped port NG
link switch to p2p
p2pchannel success
devp2pver
p2pid
a_p2p_peer_ip
a_p2p_local_ip
```

Important native source path fragments embedded in strings:

```text
Src/P2PSDK/P2PClient.cpp
Src/P2PSDK/Kdf.cpp
Src/PTCP/PhonyTcpReactor.cpp
Src/Proxy/ProxyP2PClient.cpp
Src/Proxy/P2PMessageParser.cpp
Src/StunClient/StunClientImp.cpp
Src/NatEventDriver.cpp
Src/LinkThrough/DeviceInfoMgr.cpp
```

## Working Hypothesis

The app uses Imou/Lechange cloud configuration plus native P2P traversal to
build a local TCP-like stream endpoint. Depending on network conditions, the
native layer chooses local LAN, direct UDP P2P, or relay. The app player only
needs to read the local URL.

