#!/usr/bin/env python3
"""
Redact sensitive auth tokens from SAIC mitmproxy capture logs before sharing.

Usage:
    python3 redact.py saic_capture.txt > saic_capture_redacted.txt
"""
import re
import sys

if len(sys.argv) != 2:
    print("Usage: python3 redact.py <capture_file>", file=sys.stderr)
    sys.exit(1)

with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as f:
    text = f.read()

# blade-auth JWT — the live session token. Keep header name, redact value.
text = re.sub(r"'blade-auth':\s*'[^']*'", "'blade-auth': '[REDACTED]'", text)

# cookie header — contains GA tracking IDs, low sensitivity but redact anyway.
text = re.sub(r"'cookie':\s*'[^']*'", "'cookie': '[REDACTED]'", text)

# app-verification-string — per-request HMAC, not reusable, but redact for good measure.
text = re.sub(
    r"'app-verification-string':\s*'[^']*'",
    "'app-verification-string': '[REDACTED]'",
    text,
)

sys.stdout.write(text)
