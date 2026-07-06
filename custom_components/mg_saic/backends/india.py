# File: backends/india.py
#
# India (TAP protocol) backend for the MG/SAIC Home Assistant integration.
# Based on mg-ismart-india-ha by John Lazarus (github.com/john-lazarus)
# Copyright (c) 2026 John Lazarus. Licensed under the MIT License.
#
# STATUS: SCAFFOLD — this module defines the backend shape, capability set,
# and PIN handling agreed in issue #169.  The TAP protocol client itself is
# being contributed by John Lazarus and will replace the NotImplementedError
# bodies below.  Until then, attempting to set up an India account fails
# with a clear message rather than the misleading "account is not
# registered" error from the global gateway.

import hashlib

from ..const import LOGGER
from . import INDIA_FEATURES, Feature


class IndiaBackendNotReadyError(Exception):
    """Raised while the India TAP client is not yet integrated."""


def hash_india_pin(pin: str) -> str:
    """Return the MG India command-PIN hash for a 4-digit PIN.

    MG India remote commands are authorised with an uppercase MD5 hex digest
    of the PIN with "00" appended:  md5(f"{pin}00").hexdigest().upper()

    Confirmed against the iSmart India app by John Lazarus.  Only this hash
    is stored in the config entry — never the raw PIN.
    """
    pin = str(pin).strip()
    if len(pin) != 4 or not pin.isdigit():
        raise ValueError("MG India PIN must be exactly 4 digits")
    return hashlib.md5(f"{pin}00".encode()).hexdigest().upper()


class IndiaBackend:
    """Backend for MG India vehicles (TAP binary protocol).

    Conforms to the SAICMGAPIClient method surface for every feature in
    INDIA_FEATURES.  Charging-related methods are intentionally absent —
    MG India's platform has no charging data or control, and
    backend_supports() gates all charging entities/services off before they
    would ever be called.

    Behavioural notes carried over from the standalone India integration:
      * Commands require the PIN hash (see hash_india_pin).
      * Door lock/unlock has a fallback: when MG India returns no terminal
        result for the command, the backend re-reads vehicle status to
        confirm the outcome.  This lives entirely inside this backend so
        the coordinator stays backend-agnostic.
    """

    supported_features = INDIA_FEATURES

    def __init__(self, username, password, vin=None, pin_hash=None, country_code=None):
        self.username = username
        self.password = password
        self.vin = vin
        self.pin_hash = pin_hash
        self.country_code = country_code
        self.region_name = "India"

    def _not_ready(self, what: str):
        raise IndiaBackendNotReadyError(
            f"India backend: {what} is not available yet — the India TAP "
            "client is being integrated (see "
            "https://github.com/townsmcp/mg-saic-ha/issues/169)."
        )

    # ── Session ──────────────────────────────────────────────────────────────

    async def login(self):
        """Authenticate with the MG India TAP gateway."""
        LOGGER.error(
            "MG India support is in development and not functional in this "
            "release — follow issue #169 for progress."
        )
        self._not_ready("login")

    async def close(self):
        """Close the client session (nothing to close in the scaffold)."""
        return None

    # ── Data retrieval (Feature.STATUS) ──────────────────────────────────────

    async def get_vehicle_info(self):
        self._not_ready("vehicle list")

    async def get_vehicle_status(self, vin: str | None = None):
        self._not_ready("vehicle status")

    # ── Controls (PIN-authorised; capability-gated) ──────────────────────────

    async def lock_vehicle(self, vin):
        self._not_ready("door lock")

    async def unlock_vehicle(self, vin):
        self._not_ready("door unlock")

    async def open_tailgate(self, vin):
        self._not_ready("tailgate")

    async def control_windows(self, vin, action):
        self._not_ready("window control")

    async def control_sunroof(self, vin, action):
        self._not_ready("sunroof control")

    async def start_ac(self, vin, temperature_idx=None):
        self._not_ready("climate control")

    async def start_climate(self, vin, temperature_idx, fan_speed, ac_on):
        self._not_ready("climate control")

    async def stop_ac(self, vin):
        self._not_ready("climate control")

    async def control_heated_seat(self, vin, seat, level):
        self._not_ready("heated seat control")

    async def control_heated_seats(self, vin, left_side_level=0, right_side_level=0):
        self._not_ready("heated seat control")

    async def trigger_alarm(self, vin, with_horn=True, with_lights=True, should_stop=False):
        self._not_ready("find my car")
