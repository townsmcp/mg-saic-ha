"""Tyre pressure readings, shared by the four tyre sensors and Tyre Pressure Low.

The car's tyre sensors only transmit while the wheels turn, so the car reports
the pressures from its last drive whenever it is off. Some cars go further: an
MG Marvel R (EP21) sends exactly 55 (2.20 bar) on all four tyres shortly after
it is switched off, and its real last-drive values again once it is switched
on (#504, @stfvrg). That 55 is filler, not a reading.

TyreReadings keeps the last real reading of each tyre, when it was read and
whether the car was on, so that:

- filler (set per model as ``tyre_pressure_no_reading_raw``), -128 and polls
  without a status keep the last real value instead of showing a false low;
- the values survive a Home Assistant restart (the tyre sensors restore them);
- Tyre Pressure Low works from the same values.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .const import PRESSURE_TO_BAR

# The four tyres, as the car names them. Left/right, not driver/passenger, so
# no RHD swap is needed.
TYRE_FIELDS = (
    ("Front Left", "frontLeftTyrePressure"),
    ("Front Right", "frontRightTyrePressure"),
    ("Rear Left", "rearLeftTyrePressure"),
    ("Rear Right", "rearRightTyrePressure"),
)
FIELD_TO_TYRE = {field: name for name, field in TYRE_FIELDS}

# powerMode 2 / 3: the car is on (the same test trips use). 0 / 1: off.
CAR_ON_POWER_MODES = (2, 3)

# The ``reading`` attribute: whether the value was read with the car on, or
# with it off, when the car reports what it measured on its last drive.
READING_CAR_ON = "car_on"
READING_LAST_DRIVE = "last_drive"


def _raw(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def tyre_pressures_bar(basic, no_reading_raw=None):
    """The four tyre pressures in bar, by name, or None if the car sent none.

    -128 and missing readings are left out. A 0 counts as a reading (a flat
    tyre) unless all four are 0, which is the car sending nothing useful. If
    the model has a filler value (``no_reading_raw``) and all four tyres read
    exactly that, it is no reading either; a single tyre at that value still
    counts, so a real low tyre is never ignored.
    """
    if basic is None:
        return None
    raw = {}
    for name, field in TYRE_FIELDS:
        value = _raw(getattr(basic, field, None))
        if value is not None:
            raw[name] = value
    if not raw or all(v == 0 for v in raw.values()):
        return None
    if (
        no_reading_raw is not None
        and len(raw) == len(TYRE_FIELDS)
        and all(v == no_reading_raw for v in raw.values())
    ):
        return None
    return {name: round(v * PRESSURE_TO_BAR, 2) for name, v in raw.items()}


def _status_time(status):
    seconds = getattr(status, "statusTime", None)
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0:
        return datetime.fromtimestamp(seconds, timezone.utc)
    return datetime.now(timezone.utc)


class TyreReadings:
    """The last real reading of each tyre, with where it came from."""

    def __init__(self):
        self._tyres: dict[str, dict] = {}
        self._last_status = None

    def update(self, status, no_reading_raw=None):
        """Take this poll's status. Each status is only read once."""
        if status is None or status is self._last_status:
            if status is None:
                self._mark_held(self._tyres)
            return
        self._last_status = status
        basic = getattr(status, "basicVehicleStatus", None)
        current = tyre_pressures_bar(basic, no_reading_raw)
        if current is None:
            self._mark_held(self._tyres)
            return
        reading = (
            READING_CAR_ON
            if getattr(basic, "powerMode", None) in CAR_ON_POWER_MODES
            else READING_LAST_DRIVE
        )
        when = _status_time(status)
        for name, bar in current.items():
            self._tyres[name] = {
                "bar": bar,
                "reading": reading,
                "reading_time": when,
                "held": False,
            }
        self._mark_held(n for n in self._tyres if n not in current)

    def _mark_held(self, names):
        for name in list(names):
            self._tyres[name]["held"] = True

    def restore(self, name, bar, reading=None, reading_time=None):
        """Put back a tyre's value from before a restart, if nothing newer."""
        if name in self._tyres or bar is None:
            return
        self._tyres[name] = {
            "bar": bar,
            "reading": reading,
            "reading_time": reading_time,
            "held": True,
        }

    def get(self, name):
        return self._tyres.get(name)

    def pressures(self):
        """{tyre: bar} for every tyre with a value, or None if none has one."""
        if not self._tyres:
            return None
        return {name: t["bar"] for name, t in self._tyres.items()}


def tyre_readings(coordinator):
    """The coordinator's TyreReadings, brought up to date with its data."""
    store = getattr(coordinator, "_tyre_readings", None)
    if store is None:
        store = TyreReadings()
        setattr(coordinator, "_tyre_readings", store)
    data = getattr(coordinator, "data", None) or {}
    store.update(
        data.get("status"), getattr(coordinator, "tyre_pressure_no_reading_raw", None)
    )
    return store
