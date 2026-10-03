# File: log_redaction.py
"""Keep personal details out of the logs -- always on, in one place.

People are asked to attach debug logs to public GitHub issues. Those logs
carried the account's email address or phone number, the car's full VIN and
its GPS position, and -- at the SAIC library's debug level -- the login reply
with the account's access and refresh tokens (anyone holding those can use
the account until they expire).

A single logging filter, attached to the integration's logger and to the SAIC
libraries' loggers, rewrites every line before it reaches any handler. So the
log file, the "Enable debug logging" download and anything else reading the
log all get the masked text.

What is masked:

* login tokens and passwords                  -> ***
* the account's email / phone, any email      -> ***@*** / ***
* account identifiers in SAIC's login reply   -> ***
* VINs                                        -> last 4 characters (…9373)
* latitude / longitude                        -> ***

The last 4 characters of a VIN are kept so a log with two cars in it can
still be followed.

The filter only runs on lines that are actually being written (after the
level check), so it costs nothing while debug logging is off. It never drops
a line and never raises: if masking fails the line is logged as it was.

Not covered: lines written by Home Assistant's own loggers (which can quote
an entity's unique ID, and that contains the VIN), and the traceback shown in
the Logs panel of the UI. The docs still ask people to check a log before
sharing it.

No Home Assistant imports: used by __init__.py and backends/, and trivially
testable.
"""

from __future__ import annotations

import logging
import re

MASK = "***"
MASKED_EMAIL = "***@***"

# The SAIC client libraries, whose debug lines carry SAIC's raw replies.
LIBRARY_LOGGERS = ("saic_ismart_client_ng", "mg_ismart_india_client")

# Keys whose value is masked wherever "key: value" / key=value appears, in
# JSON ("access_token":"abc"), a dataclass repr (access_token='abc'), a dict
# repr ('access_token': 'abc') or a URL query (token=abc).
_SECRET_KEYS = (
    "access_token",
    "refresh_token",
    "accessToken",
    "refreshToken",
    "id_token",
    "user_token",
    "token",
    "api_key",
    "jti",
    "password",
    "authorization",
    "blade-auth",
)
_IDENTITY_KEYS = (
    "user_id",
    "userId",
    "user_name",
    "userName",
    "username",
    "account",
    "oauth_id",
    "avatar",
    "mobile",
    "phone",
    "email",
)
_LOCATION_KEYS = ("latitude", "longitude")

_KEYED_VALUE = re.compile(
    r"""(?<![A-Za-z0-9_])            # not the tail of a longer key
        (?P<key>["']?(?:%s)["']?     # the key, quoted or not
            \s*[:=]\s*)
        (?P<value>
            "(?:[^"\\]|\\.)*"        # JSON / double-quoted string
          | '(?:[^'\\]|\\.)*'        # single-quoted string
          | [^\s,;&)}\]'"]+          # bare value: number, word
        )"""
    % "|".join(
        re.escape(key) for key in _SECRET_KEYS + _IDENTITY_KEYS + _LOCATION_KEYS
    ),
    re.IGNORECASE | re.VERBOSE,
)

# Values that say "nothing here" are worth keeping: "access_token=None" is a
# useful thing to see in a log and gives nothing away.
_EMPTY_VALUES = {"none", "null", '""', "''", "true", "false"}

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

# Anything VIN-shaped: 17 characters from the VIN alphabet (no I, O or Q),
# with at least one letter and one digit, not part of a longer word. Catches
# cars on the account that have no config entry of their own.
_VIN = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])(?=[A-HJ-NPR-Z0-9]*[0-9])"
    r"[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9])"
)

# Shortest account name worth replacing wherever it appears. Anything shorter
# could match ordinary text.
_MIN_KNOWN_LENGTH = 6


def mask_vin(vin) -> str:
    """A VIN with only its last 4 characters visible."""
    if not vin or not isinstance(vin, str) or len(vin) < 4:
        return "****"
    return f"…{vin[-4:]}"


class _Known:
    """Exact values to replace wherever they appear: this installation's own
    VINs (also inside unique IDs, where nothing marks them as a VIN) and its
    account names (a phone number looks like any other number)."""

    def __init__(self) -> None:
        self.replacements: dict[str, str] = {}
        self.pattern: re.Pattern | None = None

    def add(self, value, replacement: str) -> None:
        if not isinstance(value, str):
            return
        value = value.strip()
        if len(value) < _MIN_KNOWN_LENGTH:
            return
        key = value.lower()
        if self.replacements.get(key) == replacement:
            return
        replacements = dict(self.replacements)
        replacements[key] = replacement
        # Longest first, so one value that contains another wins.
        pattern = re.compile(
            "|".join(
                re.escape(v) for v in sorted(replacements, key=len, reverse=True)
            ),
            re.IGNORECASE,
        )
        # Swapped in whole: a log call on another thread sees either the old
        # pair or the new one, never a mix.
        self.replacements, self.pattern = replacements, pattern

    def apply(self, text: str) -> str:
        pattern, replacements = self.pattern, self.replacements
        if pattern is None:
            return text
        return pattern.sub(
            lambda m: replacements.get(m.group(0).lower(), MASK), text
        )


_KNOWN = _Known()


def register_vin(vin) -> None:
    """Mask this VIN wherever it appears, e.g. inside a unique ID."""
    _KNOWN.add(vin, mask_vin(vin) if isinstance(vin, str) else MASK)


def register_account(username) -> None:
    """Mask this account name (email or phone number) wherever it appears."""
    if isinstance(username, str) and "@" in username:
        _KNOWN.add(username, MASKED_EMAIL)
    else:
        _KNOWN.add(username, MASK)


def _mask_keyed_value(match: re.Match) -> str:
    value = match.group("value")
    if value.lower() in _EMPTY_VALUES:
        return match.group(0)
    quote = value[0] if value[0] in "\"'" else ""
    return f"{match.group('key')}{quote}{MASK}{quote}"


def redact(text: str) -> str:
    """``text`` with personal details masked."""
    if not text:
        return text
    text = _KNOWN.apply(text)
    text = _KEYED_VALUE.sub(_mask_keyed_value, text)
    text = _EMAIL.sub(MASKED_EMAIL, text)
    return _VIN.sub(lambda m: mask_vin(m.group(0)), text)


class RedactingFilter(logging.Filter):
    """Masks personal details in every record that passes through."""

    _traceback_formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001
                # The arguments don't fit the format string. mg-saic-client
                # has one such debug line ("...event_id to the newly obtained
                # value %d" with a text event id), and logging answers each
                # one by printing "--- Logging error ---" and a 30-line call
                # stack instead of the line -- on every retry of every
                # request while debug logging is on. Log what was passed
                # instead.
                message = f"{record.msg} {record.args!r}"
                record.msg = message
                record.args = None
            clean = redact(message)
            if clean != message:
                record.msg = clean
                record.args = None
            # A traceback is rendered once and cached on the record, so
            # rendering it here (masked) is what every handler then writes.
            if record.exc_info and not record.exc_text:
                record.exc_text = self._traceback_formatter.formatException(
                    record.exc_info
                )
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
        except Exception:  # noqa: BLE001 - logging must never fail
            pass
        return True


_FILTER = RedactingFilter()


def install_log_redaction(*logger_names: str) -> None:
    """Attach the filter to these loggers and every logger beneath them.

    A filter on a logger only sees records logged on that logger itself, not
    on its children, so each existing child is given the filter too. Safe to
    call repeatedly: it is called again after setup so loggers created by
    modules imported later are picked up.
    """
    names = (
        tuple(n for n in logger_names if isinstance(n, str) and n) + LIBRARY_LOGGERS
    )
    loggers = {name: logging.getLogger(name) for name in names}
    for name, logger in list(logging.root.manager.loggerDict.items()):
        if isinstance(logger, logging.Logger) and any(
            name.startswith(f"{prefix}.") for prefix in names
        ):
            loggers[name] = logger
    for logger in loggers.values():
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
