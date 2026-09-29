# File: status_schema.py
"""Vehicle status fields the SAIC client library doesn't know about.

The car's ``/vehicle/status`` response carries more than the library's
``BasicVehicleStatus`` dataclass declares. The library builds that dataclass
with dacite, which silently drops any key it has no field for, so those values
never reach the integration. Confirmed from an MGS6 (MIS3E) response,
2026-09-29::

    "secondRowLeftSeatHeatLevel": 0, "secondRowRightSeatHeatLevel": 0

The rear heated seat switches read those two fields, so until now they could
only ever show Off -- even while the rear seats were heating.

This module asks for the same status the library does (same endpoint, same
parameters, the library's own request/decrypt/retry path) but deserialises it
into a subclass that adds the missing fields. Everything the library already
reads is unchanged: the subclass only adds attributes. If the library's
internals ever change shape, ``fetch_vehicle_status`` falls back to the
library's own ``get_vehicle_status`` so the integration keeps working, just
without the extra fields.
"""

from dataclasses import dataclass
from typing import Any

from .const import LOGGER

# Keys present in the raw status response that the library's dataclass drops.
EXTRA_BASIC_STATUS_FIELDS = (
    "secondRowLeftSeatHeatLevel",
    "secondRowRightSeatHeatLevel",
)

_STATUS_PATH = "/vehicle/status"
_STATUS_REQ_TYPE = "2"  # same value the library sends

_extended_type: Any = None
_fallback_logged = False


def _build_extended_type():
    """Create the extended response dataclass (lazily, from the real library)."""
    from saic_ismart_client_ng.api.vehicle.schema import (  # noqa: PLC0415
        BasicVehicleStatus,
        VehicleStatusResp,
    )

    @dataclass
    class BasicVehicleStatusWithRearSeats(BasicVehicleStatus):
        secondRowLeftSeatHeatLevel: int | None = None
        secondRowRightSeatHeatLevel: int | None = None

    @dataclass
    class VehicleStatusRespWithRearSeats(VehicleStatusResp):
        basicVehicleStatus: BasicVehicleStatusWithRearSeats | None = None

    # This module deliberately does NOT use "from __future__ import
    # annotations": the two annotations above are real types, so dacite
    # doesn't have to resolve them by name (the classes are local to this
    # function). Inherited fields resolve in the library's own module.
    return VehicleStatusRespWithRearSeats


def extended_status_type():
    """The response type used for status fetches (built once, then cached)."""
    global _extended_type  # noqa: PLW0603
    if _extended_type is None:
        _extended_type = _build_extended_type()
    return _extended_type


async def fetch_vehicle_status(saic_api, vin: str):
    """Fetch vehicle status, keeping the fields the library would drop.

    Uses the library's own request machinery (encryption, event-id retries,
    error codes), so errors surface exactly as they do from the library's
    ``get_vehicle_status``. Only a missing or changed library internal (an
    ImportError/AttributeError/TypeError raised *before* the request is made)
    triggers the fallback.
    """
    global _fallback_logged  # noqa: PLW0603
    try:
        from saic_ismart_client_ng.crypto_utils import (  # noqa: PLC0415
            sha256_hex_digest,
        )

        out_type = extended_status_type()
        call = saic_api.execute_api_call_with_event_id
    except (ImportError, AttributeError, TypeError) as err:
        if not _fallback_logged:
            LOGGER.warning(
                "Extended vehicle status unavailable (%s); falling back to the "
                "library's status call. Rear heated seat levels will not be "
                "reported.",
                err,
            )
            _fallback_logged = True
        return await saic_api.get_vehicle_status(vin)

    return await call(
        "GET",
        _STATUS_PATH,
        params={"vin": sha256_hex_digest(vin), "vehStatusReqType": _STATUS_REQ_TYPE},
        out_type=out_type,
    )
