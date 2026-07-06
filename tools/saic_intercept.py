"""
SAIC iSmart mitmproxy intercept script
=======================================
Decrypts app-content-encrypted traffic (BOTH requests and responses) from
the SAIC iSmart gateways (all regions) and logs plain JSON both to the mitmweb
Event Log AND
to a persistent file on disk (/logs/saic_capture.log), so nothing is lost if
the mitmweb UI struggles under a large volume of traffic.

Decryption is self-contained — it uses only headers already present in each
message (APP-SEND-DATE and ORIGINAL-CONTENT-TYPE).  No auth token required.

Algorithm (from saic-python-client-ng crypto.py / crypto_utils.py):
  key = MD5( app_send_date + "1" + original_content_type )   # 32 hex chars -> 16 bytes
  iv  = MD5( app_send_date )                                  # 32 hex chars -> 16 bytes
  plaintext = AES-128-CBC-PKCS5-unpad( unhex(body), key, iv )

NOTE ON REQUEST DECRYPTION:
  Requests use a DIFFERENT key derivation from responses. Responses use a
  simple symmetric key: MD5(app_send_date + "1" + content_type). Requests mix
  in the request path, tenant id, and user (blade-auth) token as well:

    key = MD5( MD5(request_path + tenant_id + user_token + "app")
               + app_send_date + "1" + original_content_type )
    iv  = MD5(app_send_date)   # same both directions

  This was verified by decrypting real captured /vehicle/control commands. All
  the inputs are available on the request itself (path from the URL, tenant-id
  and blade-auth from headers), so no external state is needed — but the
  blade-auth token MUST be present on the request, or the key is wrong and it
  falls back to logging raw hex. This lets us capture the actual command
  parameters (e.g. climate mode / temperature values) the app sends.

Install dependencies inside the mitmproxy container once:
  pip install pycryptodome

Usage (already wired into compose command):
  mitmweb ... -s /scripts/intercept.py

File output:
  /logs/saic_capture.log — plain text, one entry per request/response,
  appended and flushed immediately so tail -f works during a live capture.
"""

from __future__ import annotations

import hashlib
import json
import logging
from binascii import unhexlify
from datetime import datetime, timezone
from pathlib import Path

from mitmproxy import ctx, http

# -- SAIC regional gateways ----------------------------------------------------
# The iSmart app talks to a different gateway per region. All known SAIC
# gateway hosts are listed here so this script works for any user regardless of
# where their account is registered. If your traffic isn't being captured, add
# your gateway host below (find it by watching mitmweb's flow list while the
# app refreshes) and open an issue so we can add it for everyone.
SAIC_HOSTS = {
    # Europe / Rest of World
    "gateway-mg-eu.soimt.com",
    "tap-eu.soimt.com",
    # Australia
    "gateway-mg-au.soimt.com",
    "tap-au.soimt.com",
    # China
    "tap-cn.soimt.com",
    # Brazil
    "gateway-mg-br.soimt.com",
    # Israel
    "gateway-mg-il.soimt.com",
    # Turkey
    "gateway-mg-tr.soimt.com",
    # India
    "gateway-mg-in.soimt.com",
}

# Base URIs are stripped down to "/" when computing the request_path that goes
# into the request key derivation (mirrors the saic client library's base_uri
# handling). Built automatically from SAIC_HOSTS so adding a host above is
# enough — no need to edit this too. The "/api.app/v1/" suffix is consistent
# across all known SAIC regions.
SAIC_BASE_URIS = tuple(
    f"https://{host}/api.app/v1/" for host in SAIC_HOSTS
)

SCRIPT_VERSION = "2026-07-02-bidirectional-allregions"  # bump when editing; shown in reload marker

log = logging.getLogger(__name__)

# -- file logging setup ---------------------------------------------------------
LOG_DIR = Path("/logs")
LOG_FILE = LOG_DIR / "saic_capture.log"

_file_handle = None


def _get_file_handle():
    """Lazily open the log file in line-buffered append mode."""
    global _file_handle
    if _file_handle is None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # buffering=1 => line-buffered, so every write() is flushed to disk
        # immediately without needing an explicit flush() call each time.
        _file_handle = open(LOG_FILE, "a", buffering=1, encoding="utf-8")
    return _file_handle


def _write_log(text: str) -> None:
    """Write a block of text to the capture file, with a timestamp, and flush."""
    fh = _get_file_handle()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"
    fh.write(f"[{ts}] {text}\n")
    fh.flush()


def load(loader):
    """Called once when the script is loaded — log a startup marker."""
    _write_log(
        f"{'=' * 70}\n"
        f"SAIC capture script loaded / reloaded — version {SCRIPT_VERSION}\n"
        f"(request+response decryption both enabled)\n"
        f"{'=' * 70}"
    )


def done():
    """Called on shutdown — close the file cleanly."""
    global _file_handle
    if _file_handle is not None:
        _write_log("SAIC capture script shutting down")
        _file_handle.close()
        _file_handle = None


# -- crypto helpers (mirrors saic_ismart_client_ng.crypto_utils) ---------------

def _md5_hex(content: str) -> str:
    """Return 32-char lowercase hex MD5 of a UTF-8 string."""
    digest = hashlib.md5(content.encode()).digest()  # noqa: S324
    # replicate the library's manual hex formatting
    result = ""
    for byte in digest:
        v = byte if byte >= 0 else byte + 0x100
        if v < 16:
            result += "0"
        result += format(v, "x")
    return result


def _decrypt_body(body_hex: str, app_send_date: str, original_content_type: str) -> str | None:
    """
    Decrypt a SAIC RESPONSE body.

    Returns the decrypted UTF-8 string, or None if decryption fails.
    """
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad

        key_source = app_send_date + "1" + original_content_type
        key_hex = _md5_hex(key_source)      # 32 hex chars = 16 bytes
        iv_hex = _md5_hex(app_send_date)    # 32 hex chars = 16 bytes

        key_bytes = unhexlify(key_hex)
        iv_bytes = unhexlify(iv_hex)
        cipher_bytes = unhexlify(body_hex.strip())

        cipher = AES.new(key_bytes, AES.MODE_CBC, iv_bytes)
        decrypted = unpad(cipher.decrypt(cipher_bytes), AES.block_size)
        return decrypted.decode("utf-8")

    except Exception as exc:
        ctx.log.warn(f"[SAIC] Response decryption failed: {exc}")
        return None


def _strip_base_uri(url: str) -> str:
    """Reduce a full request URL to the request_path used in the request key.

    Mirrors saic_ismart_client_ng: the base URI is replaced with "/", so
    e.g. ".../api.app/v1/vehicle/control" -> "/vehicle/control".
    Query strings are preserved (the library does not strip them).
    """
    for base in SAIC_BASE_URIS:
        if url.startswith(base):
            return "/" + url[len(base):]
    # Fallback for any host not in the explicit list: strip scheme+host and the
    # /api.app/v1/ prefix generically.
    import re
    return re.sub(r"^https?://[^/]+/api\.app/v1/", "/", url)


def _decrypt_request_body(
    body_hex: str,
    app_send_date: str,
    original_content_type: str,
    request_path: str,
    tenant_id: str,
    user_token: str,
) -> str | None:
    """
    Decrypt a SAIC REQUEST body.

    Requests use a DIFFERENT key derivation from responses (confirmed against
    saic_ismart_client_ng.net.crypto.decrypt_request): the key mixes in the
    request path, tenant id, user token, and the literal "app":

        key = MD5( MD5(request_path + tenant_id + user_token + "app")
                   + app_send_date + "1" + original_content_type )
        iv  = MD5(app_send_date)

    All of these are available from the request itself (path from the URL,
    tenant-id / blade-auth from headers), so no external state is needed —
    but the blade-auth token MUST be present and correct, or the key is wrong.

    Returns the decrypted UTF-8 string, or None if decryption fails.
    """
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad

        inner = _md5_hex(request_path + tenant_id + user_token + "app")
        key_hex = _md5_hex(inner + app_send_date + "1" + original_content_type)
        iv_hex = _md5_hex(app_send_date)

        key_bytes = unhexlify(key_hex)
        iv_bytes = unhexlify(iv_hex)
        cipher_bytes = unhexlify(body_hex.strip())

        cipher = AES.new(key_bytes, AES.MODE_CBC, iv_bytes)
        decrypted = unpad(cipher.decrypt(cipher_bytes), AES.block_size)
        return decrypted.decode("utf-8")

    except Exception as exc:
        ctx.log.warn(f"[SAIC] Request decryption failed: {exc}")
        return None


def _pretty_json(text: str) -> str:
    """Return pretty-printed JSON if parseable, otherwise return as-is."""
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except Exception:
        return text


# -- mitmproxy hooks -----------------------------------------------------------

def request(flow: http.HTTPFlow) -> None:
    if flow.request.host not in SAIC_HOSTS:
        return

    header_block = (
        f"\n{'-' * 70}\n"
        f">> REQUEST  {flow.request.method}  {flow.request.pretty_url}\n"
        f"   Headers: {dict(flow.request.headers)}"
    )
    ctx.log.info(header_block)
    _write_log(header_block)

    body = flow.request.get_text(strict=False)
    if not body:
        return

    # Requests are also encrypted — decrypt them so we can see what was sent.
    app_send_date = flow.request.headers.get("app-send-date") or \
        flow.request.headers.get("APP-SEND-DATE")
    original_ct = flow.request.headers.get("original-content-type") or \
        flow.request.headers.get("ORIGINAL-CONTENT-TYPE")
    encrypted = flow.request.headers.get("app-content-encrypted") or \
        flow.request.headers.get("APP-CONTENT-ENCRYPTED")

    if encrypted == "1" and app_send_date and original_ct:
        # Requests use a different key derivation from responses — it mixes in
        # the request path, tenant id, and blade-auth token. Reconstruct those
        # from the request itself and decrypt. This reveals the actual command
        # parameters (fan_speed / temperature bytes, etc).
        tenant_id = flow.request.headers.get("tenant-id") or \
            flow.request.headers.get("TENANT-ID") or ""
        user_token = flow.request.headers.get("blade-auth") or \
            flow.request.headers.get("BLADE-AUTH") or ""
        request_path = _strip_base_uri(flow.request.pretty_url)

        plaintext = _decrypt_request_body(
            body, app_send_date, original_ct, request_path, tenant_id, user_token
        )

        if plaintext is not None:
            pretty = _pretty_json(plaintext)
            line = f"   Body (DECRYPTED REQUEST):\n{pretty}"
            ctx.log.info(line)
            _write_log(line)
        else:
            # Decryption failed — most likely the blade-auth token wasn't
            # present/forwarded on this request, or the path mapping differs.
            # Preserve the full raw hex so it can be decrypted offline later.
            line = f"   Body (request decrypt failed, raw hex, {len(body)} chars): {body[:120]}…"
            ctx.log.info(line)
            _write_log(line)
            _write_log(f"   Body (encrypted hex, FULL): {body}")
    else:
        pretty = _pretty_json(body)
        line = f"   Body: {pretty}"
        ctx.log.info(line)
        _write_log(line)


def response(flow: http.HTTPFlow) -> None:
    if flow.request.host not in SAIC_HOSTS:
        return

    status = flow.response.status_code
    url = flow.request.pretty_url
    header_block = f"\n{'-' * 70}\n<< RESPONSE {status}  {url}"
    ctx.log.info(header_block)
    _write_log(header_block)

    headers_line = f"   Headers: {dict(flow.response.headers)}"
    ctx.log.info(headers_line)
    _write_log(headers_line)

    body = flow.response.get_text(strict=False)
    if not body:
        ctx.log.info("   Body: (empty)")
        _write_log("   Body: (empty)")
        return

    encrypted = (
        flow.response.headers.get("app-content-encrypted") or
        flow.response.headers.get("APP-CONTENT-ENCRYPTED")
    )

    if encrypted != "1":
        # Plain JSON — just pretty print it
        pretty = _pretty_json(body)
        line = f"   Body (plain):\n{pretty}"
        ctx.log.info(line)
        _write_log(line)
        return

    # -- decrypt ----------------------------------------------------------------
    app_send_date = (
        flow.response.headers.get("app-send-date") or
        flow.response.headers.get("APP-SEND-DATE")
    )
    original_ct = (
        flow.response.headers.get("original-content-type") or
        flow.response.headers.get("ORIGINAL-CONTENT-TYPE")
    )

    if not app_send_date or not original_ct:
        warn = (
            f"[SAIC] Encrypted response but missing APP-SEND-DATE or "
            f"ORIGINAL-CONTENT-TYPE headers — cannot decrypt.\n"
            f"   Raw body: {body[:200]}"
        )
        ctx.log.warn(warn)
        _write_log(warn)
        return

    plaintext = _decrypt_body(body, app_send_date, original_ct)  # response key

    if plaintext is None:
        warn = f"   Body (decryption failed, raw hex): {body[:200]}"
        ctx.log.warn(warn)
        _write_log(warn)
        return

    pretty = _pretty_json(plaintext)
    line = f"   Body (DECRYPTED):\n{pretty}"
    ctx.log.info(line)
    _write_log(line)
    # Encrypted body is forwarded to the app untouched — do not rewrite the
    # flow, or the iSmart app will receive plaintext it doesn't expect and
    # fail to render vehicle data.  Decrypted output is now in BOTH the
    # mitmweb Event Log panel AND /logs/saic_capture.log on disk.
