#!/usr/bin/env python3
"""Compare what the integration sends for a climate command with the iSmart app.

Three sub-commands:

  ours     Show the exact /vehicle/control body the integration's SAIC client
           builds for a climate command -- WITHOUT sending anything to the car
           (the library's send step is intercepted just before encryption).

             python tools/climate_wire.py ours --mode 4 --temp-idx 19 --ac-on 1

  decode   Decrypt the /vehicle/control requests (and their responses) in a
           mitmproxy capture of the iSmart app. Accepts a mitmproxy flow file
           (mitmdump -w capture.mitm) or a HAR export.

             python tools/climate_wire.py decode capture.mitm

  compare  Decode a capture and compare its climate command(s) with what the
           integration sends for the given parameters.

             python tools/climate_wire.py compare capture.mitm --mode 4 --temp-idx 19 --ac-on 1

Requires the SAIC client the integration pins (manifest.json: mg-saic-client)
and, for .mitm files, mitmproxy. Nothing here contacts SAIC or the car.

Output never includes the account token or the VIN hash. Still check with
tools/redact.py before sharing a capture itself -- it contains your token.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from dataclasses import asdict
from pathlib import Path

CONTROL_PATH = "/vehicle/control"

# saic_ismart_client_ng.api.vehicle.schema.RvcParamsId / RvcReqType
PARAM_NAMES = {
    17: "HEATED_SEAT_DRIVER",
    18: "HEATED_SEAT_PASSENGER",
    19: "FAN_SPEED (climate mode on mode_select cars)",
    20: "TEMPERATURE (index)",
    22: "AC_ON_OFF",
    23: "REMOTE_HEAT_REAR_WINDOW",
    255: "PARAMS_MAX (terminator)",
}
REQ_TYPE_NAMES = {"5": "HEATED_SEATS", "6": "CLIMATE", "32": "REMOTE_HEAT_REAR_WINDOW"}


# ── Decoding / presentation ─────────────────────────────────────────────────


def decode_params(body: dict) -> list[tuple[int, str, bytes]]:
    """[(paramId, name, raw bytes)] in the order sent."""
    out = []
    for p in body.get("rvcParams") or []:
        raw = base64.b64decode(p.get("paramValue") or "")
        out.append((p.get("paramId"), PARAM_NAMES.get(p.get("paramId"), "?"), raw))
    return out


def describe(body: dict, title: str) -> str:
    req_type = str(body.get("rvcReqType"))
    lines = [title, f"  rvcReqType = {req_type} ({REQ_TYPE_NAMES.get(req_type, '?')})"]
    for pid, name, raw in decode_params(body):
        value = int.from_bytes(raw, "big") if raw else None
        lines.append(f"  paramId {pid:>3} {name:<45} = {value}  (bytes {raw.hex() or '-'})")
    return "\n".join(lines)


def comparable(body: dict) -> tuple:
    """What must match: request type and every parameter, in order. The VIN
    is a per-account hash and deliberately left out."""
    return (
        str(body.get("rvcReqType")),
        tuple((pid, raw) for pid, _name, raw in decode_params(body)),
    )


# ── ours: build the body without sending ────────────────────────────────────


async def _build_ours(mode: int, temp_idx: int, ac_on: bool | None) -> dict:
    from saic_ismart_client_ng import SaicApi
    from saic_ismart_client_ng.model import SaicApiConfiguration

    api = SaicApi(SaicApiConfiguration(username="offline@example.com", password="x"))
    captured = {}

    async def _capture(body, vin):  # replaces the network send
        captured["body"] = asdict(body)
        return None

    api.send_vehicle_control_command = _capture
    await api.control_climate(
        "OFFLINE0000000000", fan_speed=mode, ac_on=ac_on, temperature_idx=temp_idx
    )
    return captured["body"]


def build_ours(mode: int, temp_idx: int, ac_on: bool | None) -> dict:
    return asyncio.run(_build_ours(mode, temp_idx, ac_on))


# ── decode: read a capture ──────────────────────────────────────────────────


def _flows_from_mitm(path: Path):
    from mitmproxy import io as mitm_io

    with path.open("rb") as fh:
        for flow in mitm_io.FlowReader(fh).stream():
            req = getattr(flow, "request", None)
            if req is None:
                continue
            resp = getattr(flow, "response", None)
            yield {
                "url": req.pretty_url,
                "method": req.method,
                "req_headers": dict(req.headers),
                "req_body": req.get_text(strict=False) or "",
                "resp_headers": dict(resp.headers) if resp else {},
                "resp_body": (resp.get_text(strict=False) or "") if resp else "",
            }


def _flows_from_har(path: Path):
    har = json.loads(path.read_text(encoding="utf-8"))
    for entry in har.get("log", {}).get("entries", []):
        req, resp = entry.get("request", {}), entry.get("response", {})
        yield {
            "url": req.get("url", ""),
            "method": req.get("method", ""),
            "req_headers": {h["name"]: h["value"] for h in req.get("headers", [])},
            "req_body": (req.get("postData") or {}).get("text", ""),
            "resp_headers": {h["name"]: h["value"] for h in resp.get("headers", [])},
            "resp_body": (resp.get("content") or {}).get("text", ""),
        }


def _ci(headers: dict) -> dict:
    """Case-insensitive header access, as httpx Headers provides."""
    from httpx import Headers

    return Headers(headers)


def _base_uri(url: str) -> str:
    # Everything before the API path, so request_path matches what the app
    # signed (e.g. https://gateway-mg-eu.soimt.com/api.app/v1/).
    marker = "/api.app/v1/"
    i = url.find(marker)
    return url[: i + len(marker)] if i >= 0 else url[: url.find("/", 8) + 1]


def decode_capture(path: Path) -> list[dict]:
    """Decrypted /vehicle/control exchanges, in capture order."""
    from saic_ismart_client_ng.net.crypto import decrypt_request, decrypt_response

    reader = _flows_from_har if path.suffix.lower() == ".har" else _flows_from_mitm
    results = []
    for f in reader(path):
        if CONTROL_PATH not in f["url"].split("?")[0] or f["method"] != "POST":
            continue
        headers = _ci(f["req_headers"])
        raw = decrypt_request(
            original_request_url=f["url"].split("?")[0],
            original_request_headers=headers,
            original_request_content=f["req_body"],
            base_uri=_base_uri(f["url"]),
        ).decode("utf-8", "replace")
        entry = {"url": f["url"], "request": None, "request_raw": raw, "response": None}
        try:
            entry["request"] = json.loads(raw)
        except ValueError:
            pass
        if f["resp_body"]:
            resp_raw, _ = decrypt_response(
                original_response_content=f["resp_body"],
                original_response_headers=_ci(f["resp_headers"]),
                original_response_charset="utf-8",
            )
            try:
                entry["response"] = json.loads(resp_raw)
            except ValueError:
                entry["response"] = resp_raw.decode("utf-8", "replace")
        results.append(entry)
    return results


def _redacted_response(resp):
    if isinstance(resp, dict):
        return {k: v for k, v in resp.items() if k not in ("data",) or not v} | (
            {"data": "<omitted>"} if resp.get("data") else {}
        )
    return resp


# ── CLI ─────────────────────────────────────────────────────────────────────


def _ours_args(p):
    p.add_argument("--mode", type=int, required=True, help="paramId 19 value (e.g. 4 = HIGH on MGS6)")
    p.add_argument("--temp-idx", type=int, required=True, help="paramId 20 value")
    p.add_argument("--ac-on", type=int, choices=(0, 1), default=1, help="paramId 22 value")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    _ours_args(sub.add_parser("ours", help="what the integration sends"))
    sub.add_parser("decode", help="decrypt a capture").add_argument("capture", type=Path)
    cmp_p = sub.add_parser("compare", help="capture vs integration")
    cmp_p.add_argument("capture", type=Path)
    _ours_args(cmp_p)
    args = parser.parse_args(argv)

    if args.cmd == "ours":
        print(describe(build_ours(args.mode, args.temp_idx, bool(args.ac_on)), "Integration sends:"))
        return 0

    exchanges = decode_capture(args.capture)
    if not exchanges:
        print(f"No POST {CONTROL_PATH} requests found in {args.capture}")
        return 2

    if args.cmd == "decode":
        for n, ex in enumerate(exchanges, 1):
            if ex["request"] is None:
                print(f"#{n} {ex['url']}\n  could not decrypt: {ex['request_raw'][:120]!r}")
                continue
            print(describe(ex["request"], f"#{n} App sent:"))
            print(f"  response: {json.dumps(_redacted_response(ex['response']))}\n")
        return 0

    ours = build_ours(args.mode, args.temp_idx, bool(args.ac_on))
    print(describe(ours, "Integration sends:"), "\n")
    climate = [ex for ex in exchanges if ex["request"] and str(ex["request"].get("rvcReqType")) == "6"]
    if not climate:
        print("The capture has no climate (rvcReqType 6) command.")
        return 2
    all_match = True
    for n, ex in enumerate(climate, 1):
        print(describe(ex["request"], f"App climate command #{n}:"))
        match = comparable(ex["request"]) == comparable(ours)
        all_match &= match
        print(f"  => {'MATCH' if match else 'DIFFERENT'}\n")
    return 0 if all_match else 1


if __name__ == "__main__":
    sys.exit(main())
