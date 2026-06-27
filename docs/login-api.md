# Imou Cloud Login API (PCS / SaaS)

The user-account API the app uses for login + device management. Confirmed from
live MITM captures of the patched app (`app-sg1-v3.easy4ipcloud.com`). Distinct
from the **P2P** WSSE creds in `cloud-p2p-stream.md` (those are app-global; this
is the user's account).

## Transport & signing

- `POST https://app-<region>-v3.easy4ipcloud.com/pcs/v1/<method>`
  (region e.g. `sg1`; base `app-v3.easy4ipcloud.com` redirects per account).
- Body: `{"data": {...}}` JSON.
- Headers: `x-pcs-username`, `x-pcs-apiver` (`191204`), `x-pcs-nonce`,
  `x-pcs-date` (UTC), `x-pcs-client-ua` (base64 JSON device descriptor),
  `Content-MD5` (base64 md5 of body), optionally `x-pcs-session-id`, and
  `x-pcs-signature`.
- **Signature** = `base64(HMAC-SHA256(key, canonical))` where canonical is:
  ```
  POST\n<path>\n<Content-MD5>\napplication/json; charset=utf-8\n
  x-pcs-apiver:<v>\nx-pcs-client-ua:<ua>\nx-pcs-date:<d>\nx-pcs-nonce:<n>\n
  [x-pcs-session-id:<s>\n]x-pcs-username:<u>\n
  ```
  The `key` changes per phase (see below).

## Login flow

1. **`user.account.GetToken`** — `username = account\<email>`, `key = md5(password)`,
   body `{"areaCode":"","gpsInfo":{"latitude":0,"longitude":0}}`.
   - Success (`code:0`) → `data: {token, sessionId, username:"uuid\\<id>"}`.
   - `code:12110` → captcha required; response carries
     `data.captchaData.{image(b64), codeId, captchaId, verifyToken}`.
2. **(captcha)** `common.validcode.CheckImageValidCode` — same auth as GetToken,
   body `{codeId, code, usage:"Login", captchaMetaData:"", captchaId, verifyToken}`;
   then retry GetToken.
3. **`user.account.Login`** — `username = token/uuid\\<id>`, `key = token`,
   `x-pcs-session-id = sessionId`, body `{"timezoneOffset":-420}`.
   - Returns `data: {userId/uuid, email, nickname, mqttAk, mqttToken,
     iotEntryUrlV2(+Host), regImsAddr, role, ...}`.

After login, normal calls use `username = uuid\\<id>`, `key = token`,
`x-pcs-session-id = sessionId`.

## Device list (serials)

**`device.list.BasicList`** — body
`{"familyId":"-1","transferStr":"","offset":0,"limit":128,"roomId":"-1"}` →
`data.deviceList[]` with:

- `deviceId` — the **serial** (what the P2P bridge needs, e.g. `C67C…`)
- `deviceName` — display name ("Cam sân")
- `productModel` (`IPC-PS70F-10M0`), `catalog`, `role`, `channelList`,
  `streamEntryAddrV3` (nginx device proxy), `streamEntryAddrV4` (iot mqtt).

Related: `device.list.DeviceBasicInfoQueryV2`, `device.list.DetailInfoQuery`.

## Helpers

- `scripts/imou_login.py` — headless login (env `IMOU_USERNAME`/`IMOU_PASSWORD`),
  saves the captcha image when `code:12110` for manual solving.
- `scripts/imou_login_site.py` — **local web helper**: `python3 scripts/imou_login_site.py`
  → open `http://127.0.0.1:8099`, enter email/password, solve the captcha in the
  browser; it logs in, calls `device.list.BasicList`, lists the serials, and
  prints a ready-to-paste add-on `cameras:` block. Nothing is written to disk.
