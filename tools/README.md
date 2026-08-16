# `tools/` — diagnostic & research notes

These are optional helpers and notes for **investigating and improving the integration** — they are not part of the Home Assistant component itself and are never loaded by it. They're here so owners can help us map how their specific car behaves, and so the process is documented and repeatable.

> ⚠️ **This process only *observes* traffic from the official iSmart app. It does not modify your car, your account, or the integration.**

## Contents

| File | Purpose |
|------|---------|
| `redact.py` | A helper to strip your login token and other sensitive headers out of a captured log **before** you share it. Always run this before posting any capture. |

The test procedures further down document exactly which actions to perform in the app so that a capture is useful for profiling a model.

## Who these are for

Any MG/SAIC owner who wants to help us profile their model's climate control (or investigate other API behaviour). You don't need to be a developer — the full walkthrough is in the discussion linked below.

## How to use them

The complete step-by-step guide (proxy setup, routing your phone, running the tests, and reporting results) lives in the pinned discussion:

➡️ **[Climate control mapping — help us support every model](../../discussions/208)**

### Quick reference

1. Run [mitmproxy](https://mitmproxy.org/) (e.g. in Docker) to capture the app's traffic.
2. Route your phone through the proxy and trust the mitmproxy CA cert (http://mitm.it).
3. Work through the test actions below, noting the time of each step.
4. Save the captured traffic to a log file (e.g. `saic_capture.log`).
5. **Before sharing any log**, run it through `redact.py`:
   ```bash
   python3 redact.py saic_capture.log > saic_capture_redacted.log
   ```
   and double-check no `blade-auth` value remains.
6. Post the **redacted** capture and your timing notes in the discussion.

### Climate tests to record times

## CLIMATE — temperature & on/off
 
| # | Action in app | Time | Notes |
|---|--------------|------|-------|
| 1 | AC **On**, temp slider full **Low** (coldest) | | |
| 2 | (refresh after ~30s) | | |
| 3 | Slide temp to **middle** (note the °C shown) | | note the temp: ____°C |
| 4 | (refresh after ~30s) | | |
| 5 | Slide temp to **High** (warmest), AC still on | | |
| 6 | (refresh after ~30s) | | |
| 7 | **AC Off** | | |
| 8 | (refresh after ~30s) | | |
 
## WINDSCREENS (defrost/demist)
 
| # | Action in app | Time | Notes |
|---|--------------|------|-------|
| 9  | **Front Windscreen** button | | on or toggle? |
| 10 | (refresh after ~30s) | | |
| 11 | **Rear Windscreen** button | | on or toggle? |
| 12 | (refresh after ~30s) | | |
 
## HEATED STEERING WHEEL
 
| # | Action in app | Time | Notes |
|---|--------------|------|-------|
| 13 | Steering wheel heating **On** | | (the one missed last time) |
| 14 | Steering wheel heating **Off** | | |
 
## HEATED SEATS — front (Off/Low/Medium/High)
 
Do the front-LEFT seat through all levels so we map the 4-step scale:
 
| # | Action in app | Time | Notes |
|---|--------------|------|-------|
| 15 | Front-left seat → **Low** | | |
| 16 | Front-left seat → **Medium** | | |
| 17 | Front-left seat → **High** | | |
| 18 | Front-left seat → **Off** | | |
| 19 | Front-**right** seat → **High** (confirm which param = right) | | |
| 20 | Front-right → **Off** | | |
 
## HEATED SEATS — rear (Off/On)
 
| # | Action in app | Time | Notes |
|---|--------------|------|-------|
| 21 | Rear-left seat → **On** | | |
| 22 | Rear-right seat → **On** | | |
| 23 | Both rear → **Off** | | |

## 🔒 Security note — please read

A live capture contains your **iSmart login token**, which can be used to send commands to your car and is valid for a long time. Treat any raw capture as a password.

- **Never** paste or upload a raw, un-redacted capture anywhere.
- Always run `redact.py` first, and verify the token is gone.
- If you believe a token has been exposed, log out and back in to the iSmart app to invalidate it.

## Regions

The iSmart app talks to a regional SAIC gateway — a `*.soimt.com` host that differs by region (EU, Australia, China, Brazil, Israel, Turkey, India). If your traffic isn't being captured, find the gateway host your app is talking to in mitmweb's flow view and mention it in the discussion so we can help.
