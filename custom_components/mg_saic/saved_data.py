"""The car's last status and charging data, saved across restarts.

After a restart the integration knows nothing about the car until the car
answers. James's MGS6, 15 Oct 2026: Home Assistant restarted at 12:43 while
the car wasn't answering (the iSmart app said "Vehicle not responding" too),
and the lock, climate, doors, windows, battery level, range, mileage and the
rest stayed unavailable until it did, because the last values only lived in
memory.

So the last status and charging replies the car gave are kept in the
integration's storage, and shown at startup when the car doesn't answer.
Data Freshness still says the figures aren't current; the next poll that
gets an answer replaces them.

The replies are the SAIC client's dataclasses; they are stored as plain
dicts with their class, and rebuilt the way the client builds them from the
API's JSON (dacite).
"""

from __future__ import annotations

import dataclasses
import importlib
from typing import Any


def encode_vehicle_data(reply: Any) -> dict[str, Any] | None:
    """A storable form of one reply, or None if it isn't a dataclass."""
    if reply is None or not dataclasses.is_dataclass(reply) or isinstance(reply, type):
        return None
    cls = type(reply)
    return {
        "type": f"{cls.__module__}:{cls.__qualname__}",
        "data": dataclasses.asdict(reply),
    }


def decode_vehicle_data(saved: dict[str, Any] | None) -> Any:
    """Rebuild a reply saved by encode_vehicle_data, or None if it can't be."""
    if not isinstance(saved, dict):
        return None
    type_name = saved.get("type")
    data = saved.get("data")
    if not isinstance(type_name, str) or ":" not in type_name or not isinstance(data, dict):
        return None
    module_name, _, qualname = type_name.partition(":")
    try:
        import dacite

        target: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            target = getattr(target, part)
        if not dataclasses.is_dataclass(target):
            return None
        return dacite.from_dict(target, data)
    except Exception:  # noqa: BLE001 - a bad save must never break setup
        return None
