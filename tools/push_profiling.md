# Profiling SAIC Push Notifications (per-region)

This note documents how to investigate whether a given SAIC regional backend
(EU, India, Thailand, etc.) delivers **push notifications** for vehicle events,
and how to capture the details needed to add push support to the integration for
that region. It is SAIC/iSmart-specific; it assumes familiarity with the `tools/`
mitmproxy setup already documented in this folder.

> This complements `saic_intercept.py` (which decrypts the app's HTTPS traffic to
> SAIC hosts). Push registration involves additional Google/Firebase hosts that the
> SAIC interceptor deliberately ignores.

---

## Background: how iSmart push works

The iSmart Android app receives vehicle events (confirmed for **Vehicle Start** and
**Alarm** on the EU backend) via **Firebase Cloud Messaging (FCM)** — not polling.
The app:

1. Registers with Google's push system as an Android device and obtains an FCM token.
2. Hands that token to the SAIC backend at login, in the `deviceId` field of the
   login/registration request.
3. Holds a persistent connection to `mtalk.google.com:5228` (Google's push channel).
4. SAIC's backend pushes an FCM message to the registered token when a relevant
   vehicle event occurs.

To add push to the integration for a region, we need that region's **Firebase project
constants** (embedded in the app) and confirmation that the region's backend actually
binds and pushes to a registered token.

---

## Step 1 — Extract the Firebase constants from the region's APK (static, no device)

These are Firebase *project* identifiers embedded in the public APK — identical for
every install in that region, not account secrets. No device or app execution needed,
so app anti-tamper is irrelevant here.

```bash
apktool d -f -s <ismart-region>.apk -o decoded
grep -iE "google_app_id|gcm_defaultSenderId|google_api_key|project_id" \
  decoded/res/values/strings.xml
```

Record:

| Field in strings.xml | Meaning |
| --- | --- |
| `project_id` | Firebase project (e.g. EU = `mg-ismart-eu`) — confirms which region's project |
| `gcm_defaultSenderId` | FCM sender ID |
| `google_app_id` | FCM app ID (`1:<sender>:android:<hash>`) |
| `google_api_key` | FCM API key (`AIza…`) |

> **Watch the `project_id`.** A market-specific APK repackage can still point at another
> region's Firebase project. Always confirm `project_id` matches the region you're
> profiling before trusting the constants.

Signing-cert SHA-1 (only sometimes needed for the Google `register3` call):

```bash
<sdk>/build-tools/<ver>/apksigner verify --print-certs <ismart-region>.apk | grep -i sha
```

---

## Step 2 — Confirm the backend pushes (live capture, needs a REAL device)

**Use a real Android device, not an emulator.** The iSmart app is protected with native
anti-tamper that self-destructs under emulation (a null-register `SIGSEGV` on the splash
screen). Real hardware has nothing to detect and runs normally. Any Android 8.0+ device
(app `min_sdk 26`) that can trust the mitmproxy CA works; a rooted device lets you install
the CA as a **system** cert, which Android 7+ requires for app traffic unless the app
opts into user certs.

Setup mirrors the standard `tools/` capture:

1. Install the mitmproxy CA cert (system cert if rooted).
2. Set the device WiFi proxy to the mitmproxy host/port.
3. Log in with a **secondary** iSmart account (never the primary — using the primary
   kicks the account's active session, same rule as the integration itself).

Then capture, watching two things:

**A. The SAIC `deviceId` field (the important one).** In the decrypted SAIC traffic
(`saic_capture.log`), find the login/registration request to the region's
`gateway-mg-*.soimt.com` and inspect the `deviceId` in the body. A real device sends a
**real FCM token** here. If the backend accepts and later pushes to it, the region
supports the mechanism.

**B. The Google/Firebase registration (context).** These hosts are *not* logged by
`saic_intercept.py` — watch them in the mitmproxy UI directly:
- `android.clients.google.com` — `/checkin`, `/c2dm/register3` (device gets FCM token)
- `firebaseinstallations.googleapis.com` — Firebase Installation
- `mtalk.google.com:5228` — persistent push channel. Won't decode (Google-proprietary
  binary over TLS); appears as a raw TCP flow that stays open. Its presence is the signal.

**C. A push payload.** Trigger a vehicle event known to push in that region (Vehicle
Start / Alarm on EU) while capturing, and record the FCM message contents so its event
type can be mapped to a coordinator refresh.

---

## Step 3 — What to do with the findings

Once a region's constants are known and the backend is confirmed to push:

1. Perform FCM registration in Python using that region's constants → real FCM token.
2. Inject the token into the SAIC login `deviceId` field (the underlying client sends a
   placeholder there by default — that's the hook).
3. Hold the `mtalk.google.com:5228` connection open with an FCM listener.
4. On a relevant push, request an immediate coordinator refresh and relax the idle poll
   interval.
5. Persist the GCM credentials so re-registration happens once.

## Known SAIC push/polling quirks (may differ by region)

- **Command completion** (lock/unlock) is not pushed — it's rapid re-polling with an
  `event-id` tracking number.
- **Charging start/end** are not surfaced to the app/message list on EU, so push won't
  cover charge timing there — an external charger sensor remains the right trigger.
- `/message/notificationList?deviceId=…` is polled constantly by the app, separate from
  the alarm/command message lists the integration reads — worth investigating per region.

## Per-region status

| Region | Firebase project | Push confirmed? | Notes |
| --- | --- | --- | --- |
| EU | `mg-ismart-eu` | Behavioural yes (Vehicle Start / Alarm on device); live token binding pending real-device capture | Constants extracted |
| India | TBD | India backend uses polling (different TAP protocol) — push may not apply | See India backend docs |
| Thailand | TBD | Not profiled | |

_These scripts only observe app traffic; they never modify the car, account, or
integration._
