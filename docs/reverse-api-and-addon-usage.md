# Imou Bridge Reverse API and Home Assistant Add-on Guide

This document describes the reverse-engineered Imou account API, the local P2P
bridge architecture, and the Home Assistant add-on workflow.

## Project Scope

Imou Bridge is a local add-on style application for cameras you own or are
authorized to operate. It does not vendor APK files, proprietary native
libraries, decompiled source, account tokens, or captured private traffic. The
repository contains only interoperability code and research notes.

The app has three main jobs:

1. Sign in to an Imou account and import the camera list.
2. Expose selected cameras locally through go2rtc RTSP/WebRTC and one ONVIF
   endpoint per enabled camera.
3. Expose a talk/TTS path through DLNA media renderers and REST helpers for
   cameras whose visualtalk profile has been validated.

## Reverse-Engineered Cloud API

The mobile app talks to the PCS/SaaS API under regional hosts such as:

```text
https://app-<region>-v3.easy4ipcloud.com/pcs/v1/<method>
```

The account API is separate from the camera media P2P protocol. Account calls
return user identity, session data, camera metadata, family/home names, channels,
stream profiles, model information, and sometimes thumbnail URLs.

### Request Shape

Every PCS call sends JSON shaped like:

```json
{"data": {"methodSpecific": "payload"}}
```

Important headers:

| Header | Purpose |
| --- | --- |
| `x-pcs-username` | Login phase identity, usually `account\<email>` or `uuid\<user-id>` |
| `x-pcs-apiver` | API version observed from the app, for example `191204` |
| `x-pcs-nonce` | Per-request random value |
| `x-pcs-date` | UTC request timestamp |
| `x-pcs-client-ua` | Base64 JSON mobile client descriptor |
| `x-pcs-session-id` | Session id after `GetToken` |
| `Content-MD5` | Base64 MD5 of the request body |
| `x-pcs-signature` | HMAC-SHA256 signature over the canonical request |

The signature key changes by phase:

| Phase | Username header | Signing key |
| --- | --- | --- |
| `GetToken` | `account\<email>` | double-MD5 password material |
| `Login` | `uuid\<id>` after token conversion | MD5 of the temporary token |
| Normal calls | `uuid\<id>` | MD5 of the session token |

### Login Flow

The current implementation follows this sequence:

1. `user.account.GetToken`
   - Authenticates the account credential.
   - May return only `failNum` when the server accepts the request but does not
     issue a token.
   - May return a verification challenge.
2. Verification, if required
   - Image captcha path: `common.validcode.CheckImageValidCode`.
   - Geetest v4 slider path: the UI hosts local Geetest assets, displays the
     slider inside the modal, and sends the result back to the backend.
3. `user.account.Login`
   - Exchanges the temporary token/session id for normal account session data.
4. `device.list.BasicList`
   - Imports devices and channel metadata.

The server risk engine is sensitive to the mobile client descriptor and terminal
fingerprint. A fresh container or unknown fingerprint may receive a slider even
when the official mobile app logs in without one.

### Device Metadata

`device.list.BasicList` is the primary import call. The bridge normalizes these
fields:

| Normalized field | Common source keys |
| --- | --- |
| `serial` | `deviceId`, `devSn`, `serial` |
| `device_name` | `deviceName`, `name` |
| `model` | `productModel`, `model` |
| `family_name` | `channelList[].familyName`, `familyName` |
| `room_name` | `channelList[].roomName`, `roomName` |
| `thumbnail_url` | `thumbnail`, `thumbUrl`, `imageUrl`, `snapshotUrl`, nested image fields |
| `streams` | `channelList`, stream/profile metadata, inferred channel/subtype pairs |
| `local_ip` | private IP fields when returned by the API |

If the API does not return a thumbnail URL, the UI shows a CCTV icon fallback.

## Local Bridge Architecture

For each enabled camera the supervisor generates:

| Component | Purpose |
| --- | --- |
| DHP2P tunnel | For remote cameras, maps `127.0.0.1:<port>` to the camera RTSP/DVRIP port |
| go2rtc stream | Consumes the camera source once and restreams RTSP/WebRTC/HLS locally |
| ONVIF shim | Exposes `http://<host>:8700+/onvif/device_service` per enabled camera |
| DLNA renderer | Exposes a Home Assistant-discoverable media player for talk/TTS |
| REST TTS endpoint | `POST /api/cameras/<serial>/tts` for direct TTS injection |

Remote cameras still depend on Imou cloud relay/control access to establish the
P2P path. Once the local RTSP restream is up, Frigate and Home Assistant consume
ordinary local URLs and do not need to know the Imou protocol.

## Home Assistant Add-on Installation

This repository is structured as a Home Assistant add-on repository:

```text
addon/
  repository.yaml
  imou-p2p-bridge/
    config.yaml
    build.yaml
    Dockerfile
    rootfs/
```

Install from GitHub:

1. Open **Settings -> Add-ons -> Add-on Store**.
2. Open the top-right menu and choose **Repositories**.
3. Add:

   ```text
   https://github.com/home-assistant-tools/imou-life
   ```

4. Refresh the store and install **Imou P2P Bridge**.
5. Start the add-on.
6. Open **Web UI**.

The add-on uses host networking because RTSP, WebRTC, ONVIF discovery, DLNA
SSDP, and per-camera ports need direct LAN visibility.

## UI Workflow

1. Click **Add account**.
2. Enter a display name, Imou email, and Imou password.
3. Complete captcha/slider if the server requests verification.
4. Imported cameras appear under the account. They are not enabled by default.
5. Turn on **Bridge** for the cameras you want to expose.
6. Click an enabled camera row to open the detail modal.
7. Fill the camera device password and adjust:
   - bridge name,
   - relay mode for remote P2P cameras.
   PTZ probing and talk/TTS endpoints are enabled automatically for bridged
   cameras; unsupported PTZ cameras are filtered out by the runtime probe.
8. Copy generated RTSP/WebRTC/ONVIF/Talk URLs or the Frigate YAML sample.

Account actions:

| Action | Behavior |
| --- | --- |
| Reload | Re-login and refresh device metadata when a password is available |
| Update password | Stores the account password locally for future reloads |
| Delete | Removes the account and imported cameras |

## Frigate Example

The UI generates a complete per-camera sample. A minimal shape is:

```yaml
cameras:
  yard:
    ffmpeg:
      inputs:
        - path: rtsp://<addon-host>:8554/yard
          roles:
            - detect
            - record
    onvif:
      host: <addon-host>
      port: 8700
      user: admin
      password: <camera-password>
    live:
      stream_name: yard
```

If the camera has multiple channels or profiles, the bridge exposes every enabled
profile as its own go2rtc stream and ONVIF media profile.

## Talk/TTS Notes

The talk path uses Imou `visualtalk.xav` with DHAV audio framing. Different
camera models may accept different output codecs, so keep talk settings
per-camera. Tested paths include AAC/ADTS for some models and G.711 variants for
others.

Home Assistant can discover the DLNA media renderer endpoints as
`media_player` entities. There is no generic Home Assistant standard for an
add-on to auto-register an arbitrary TTS provider without a custom integration,
so the add-on also exposes a REST endpoint for automations or future HACS
integration work.

## Security Expectations

- Do not commit real account emails, passwords, tokens, serial lists, or private
  captures.
- Account passwords are used for login and local refresh only.
- Camera passwords are required by RTSP/talk and are stored only in the local
  add-on config.
- The public repository should contain documentation, source, and examples with
  placeholder values only.
