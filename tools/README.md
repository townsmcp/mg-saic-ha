# `tools/` — diagnostic & research scripts

These are optional helper scripts for **investigating and improving the integration** — they are not part of the Home Assistant component itself and are never loaded by it. They're here so owners can help us map how their specific car behaves, and so the process is documented and repeatable.

> ⚠️ **These scripts only *observe* traffic from the official iSmart app. They do not modify your car, your account, or the integration.**

## Contents

| File | Purpose |
|------|---------|
| `saic_intercept.py` | A [mitmproxy](https://mitmproxy.org/) addon that decrypts the iSmart app's encrypted traffic locally (both requests and responses) and logs it as readable JSON. Used to see exactly what commands the app sends and what the car reports back. |
| `redact.py` | A helper to strip your login token and other sensitive headers out of a captured log **before** you share it. Always run this before posting any capture. |

## Who these are for

Any MG/SAIC owner who wants to help us profile their model's climate control (or investigate other API behaviour). You don't need to be a developer — the full walkthrough is in the discussion linked below.

## How to use them

The complete step-by-step guide (Docker setup, routing your phone, running the tests, and reporting results) lives in the pinned discussion:

➡️ **[Climate control mapping — help us support every model](../../discussions)** *(replace with the actual discussion link once posted)*

### Quick reference

1. Run mitmproxy in Docker with `saic_intercept.py` mounted at `/scripts/saic_intercept.py`.
2. Inside the container once: `pip install pycryptodome`, then restart it.
3. Route your phone through the proxy and trust the mitmproxy CA cert (http://mitm.it).
4. Confirm the log header shows the script version and "request+response decryption both enabled".
5. Use the app; watch `/logs/saic_capture.log`.
6. **Before sharing any log**, run it through `redact.py`:
   ```bash
   python3 redact.py saic_capture.log > saic_capture_redacted.log
   ```
   and double-check no `blade-auth` value remains.

## 🔒 Security note — please read

A live capture contains your **iSmart login token**, which can be used to send commands to your car and is valid for a long time. Treat any raw capture as a password.

- **Never** paste or upload a raw, un-redacted capture anywhere.
- Always run `redact.py` first, and verify the token is gone.
- If you believe a token has been exposed, log out and back in to the iSmart app to invalidate it.

## Regions

`saic_intercept.py` includes the known SAIC gateway hosts for all regions (EU, Australia, China, Brazil, Israel, Turkey, India). If your traffic isn't captured, your region's gateway may be missing — find it in mitmweb's flow view (the `*.soimt.com` host the app talks to) and open an issue so we can add it for everyone.

## Compatibility note

`saic_intercept.py` mirrors the request/response encryption scheme used by [`saic-python-client-ng`](https://github.com/SAIC-iSmart-API/saic-python-client-ng). If SAIC changes their crypto, these scripts (and the integration) may need updating together.
