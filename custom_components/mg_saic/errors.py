# File: errors.py
"""What went wrong, read from a SAIC error -- in one place.

SAIC replies to a failed request with a numeric return code and a message.
Until mg-saic-client 0.9.6 the library only kept them glued together as text
("return code: 8, message: Vehicle not locked."), so the integration searched
that text in a dozen places. If SAIC reworded a message, or the text's format
changed, those searches would quietly stop matching.

mg-saic-client 0.9.6 keeps the code and the message as separate values on the
error (``return_code``, ``saic_message``). Everything here reads those first.
The text is only searched for errors that didn't come from the library with
those values -- the integration's own wrapped errors, the India backend's, a
timeout -- so behaviour for those is unchanged.

No Home Assistant imports: this module is used by api.py, coordinator.py,
event.py and message_poller.py, and is trivially testable.
"""

from __future__ import annotations

import re

RETURN_CODE_UNREACHABLE = 4  # SAIC can't reach the car (asleep, no signal)
RETURN_CODE_REJECTED = 8  # request refused: not locked, too many commands, ...
RETURN_CODES_LOGGED_OUT = (401, 403)

_CODE_IN_TEXT = re.compile(r"return code[:=]?\s*(-?\d+)", re.IGNORECASE)
_SESSION_PHRASES = ("invalid session", "token expired", "not logged in")
_MAX_CAUSE_DEPTH = 5


def _library_error(error):
    """The SAIC library error behind ``error`` (itself or an explicit cause).

    Recognised by carrying both ``return_code`` and ``saic_message``. Follows
    ``raise X from e`` chains, so the integration's own exception types
    (CommandsLimitReachedException, VehicleNotLockedException) still lead back
    to SAIC's reply.
    """
    depth = 0
    while isinstance(error, BaseException) and depth < _MAX_CAUSE_DEPTH:
        if hasattr(error, "return_code") and hasattr(error, "saic_message"):
            return error
        error = error.__cause__
        depth += 1
    return None


def return_code_of(error) -> int | None:
    """SAIC's return code for ``error`` (an exception or error text), or None."""
    library_error = _library_error(error)
    if library_error is not None:
        code = library_error.return_code
        return int(code) if code is not None else None
    match = _CODE_IN_TEXT.search(str(error))
    return int(match.group(1)) if match else None


def saic_message_of(error) -> str | None:
    """SAIC's own words for ``error``, without the code."""
    library_error = _library_error(error)
    if library_error is not None:
        return (library_error.saic_message or "").strip() or None
    text = str(error)
    marker = "message:"
    i = text.find(marker)
    return text[i + len(marker):].strip() or None if i >= 0 else None


def _message_lower(error) -> str:
    return (saic_message_of(error) or str(error)).lower()


def is_vehicle_unreachable(error) -> bool:
    """SAIC couldn't reach the car (return code 4)."""
    return return_code_of(error) == RETURN_CODE_UNREACHABLE


def is_vehicle_not_locked(error) -> bool:
    """Refused because the vehicle isn't locked.

    SAIC uses the same code (8) for every refusal, so its message is the only
    thing that tells this one apart (#374).
    """
    return "vehicle not locked" in _message_lower(error)


def is_request_rejected(error) -> bool:
    """SAIC refused the request (return code 8), for any reason."""
    return (
        return_code_of(error) == RETURN_CODE_REJECTED
        or "too frequent" in _message_lower(error)
    )


def is_session_expired(error) -> bool:
    """The login is no longer valid and the client must log in again."""
    library_error = _library_error(error)
    if library_error is not None and getattr(library_error, "is_logged_out", False):
        return True
    if return_code_of(error) in RETURN_CODES_LOGGED_OUT:
        return True
    text = str(error).lower()
    if any(phrase in text for phrase in _SESSION_PHRASES):
        return True
    # Errors without the library's values: the old check was a bare "401"
    # anywhere in the text. Kept for those only -- on a library error the code
    # above is authoritative, and a bare search could match an unrelated
    # number (an event id containing 401, say).
    return library_error is None and "401" in text
