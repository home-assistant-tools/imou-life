# Cloud API Surface

## Native Client Groups

`CloudClient.java` exposes native methods backed by `libCloudClient.so`.
Observed service/client groups include:

- `UAD`
- `CFS`
- `MTS`
- `VQS`
- `FAQ`
- user service

Native client path fragments:

```text
Src/client/CFSClient.cpp
Src/client/FAQClient.cpp
Src/client/MTSClient.cpp
Src/client/UADClient.cpp
Src/client/VQSClient.cpp
```

## Authentication Headers

Observed auth-related strings:

```text
Authorization: WSSE profile="UsernameToken"
Authorization:WSSE profile="UsernameToken"
X-WSSE: UsernameToken Username="%s", PasswordDigest="%s", Nonce="%s", Created="%s"
WWW-Authenticate: WSSE realm="huashi", profile="UsernameToken"
Authorization: Bearer %s
Authorization: Basic %s
Authorization: Digest %s
Authorization: %.*s4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s
```

The native library also exposes helpers for AES and key derivation:

```text
AesEncrypt
AesDecrypt
AesEncrypt256
AesDecrypt256
AESDecryptGCM256
AESEncryptGCM256
jniGetDerivationKeyByECCE
jniGetDerivationKeyByECE2
```

## P2P Discovery and Relay Endpoints

Observed endpoint strings:

```text
/online/p2psrv/
/online/relay
/online/stun
/p2p/stun/probe
/probe/p2psrv
```

## User and Device Endpoints

Representative strings from `libCloudClient.so`:

```text
/user/login?client-id=
/user/logout?client-id=%s
/user/device-list?client-id=%s&version=%s
/user/device/%s/info?client-id=%s
/user/%s/devices/port
/user/%s/devices/sort
/user/%s/device/%s/owner
/user/bind/device/%s?client-id=%s&version=%s
/user/unbind/device/%s?client-id=%s
/user/check/device/%s?client-id=%s&version=%s
/user/device/%s/share?client-id=%s
/user/device/%s/un-share?client-id=%s
/user/device/%s/check/encrypt-key?client-id=%s
/user/private-key
/user/public-key
```

Representative device endpoints:

```text
/device/%s/media/query
/device/%s/media/config
/device/%s/media/trans/encrypt/config?client-id=%s
/device/%s/capability-set?client-id=%s
/device/%s/ability/query?client-id=%s
/device/%s/ability/config?client-id=%s
/device/%s/time-info?client-id=%s
/device/%s/nameinfo/config?client-id=%s
/device/%s/device-restart?client-id=%s
/device/%s/wake-up?client-id=%s
/device/%s/storage/capacity?client-id=%s
/device/%s/storage/format?client-id=%s
```

## Records and Cloud Storage

Relevant media/record endpoints:

```text
/device/%s/record/list/%d?client-id=%s
/device/%s/record/bitmap/%d?client-id=%s
/device/%s/record/num/%d?client-id=%s
/file-stream/%s/%d/%s/video?client-id=%s
/file-stream/%s/%d/videos?client-id=%s
/file-stream/%s/%d/video-calender?client-id=%s
/file-picture/%s/%d/%s/picture?client-id=%s
/cloud-storage/%s/encrypt-key?client-id=%s
/cloud-storage/public-key?client-id=%s
```

## Control Endpoints

Representative control endpoints:

```text
/device/%s/ptz/move/config/%d?client-id=%s
/device/%s/ptz/location/query/%d?client-id=%s
/device/%s/ptz/location/config/%d?client-id=%s
/device/%s/wifi/current?client-id=%s
/device/%s/wifi/info?client-id=%s
/device/%s/wifi/open?client-id=%s
/device/%s/wifi/close?client-id=%s
/device/%s/wifi/operate?client-id=%s
/device/%s/alarm/plan/query?client-id=%s
/device/%s/alarm/range/query/%s?client-id=%s
/device/%s/alarm/sound/query?client-id=%s
```

## Runtime Data Still Needed

Static scanning gives endpoint formats, not the actual region hostnames,
headers, account tokens, or request bodies. The next useful capture is app
runtime logging while opening live view and talkback, with sensitive fields
redacted before committing any notes.

