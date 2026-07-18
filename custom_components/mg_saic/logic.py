"""Pure logic helpers used by the integration.

These helpers deliberately avoid Home Assistant imports so they can be tested
with the standard library only.
"""

from datetime import timedelta


def normalize_sunroof_action(action):
    """Normalize a sunroof action to `(should_open, action_name)`."""
    if isinstance(action, bool):
        return action, "open" if action else "close"

    action_name = str(action).lower()
    if action_name not in {"open", "close"}:
        raise ValueError(
            f"Invalid sunroof action '{action}'. Expected 'open' or 'close'."
        )

    return action_name == "open", action_name


def build_vehicle_options(vehicles):
    """Return VIN option values mapped to privacy-safe display labels."""
    options = {}
    for vehicle in vehicles:
        vin = str(getattr(vehicle, "vin", vehicle))
        model_name = getattr(vehicle, "modelName", None) or getattr(
            vehicle, "series", None
        )
        label = f"{model_name} (…{vin[-5:]})" if model_name else vin
        options[vin] = label
    return options


def select_update_interval(
    *,
    is_powered_on,
    is_charging,
    is_dc_charging=False,
    idle_duration,
    activity_duration,
    default_update_interval,
    powered_update_interval,
    charging_update_interval,
    dc_charging_update_interval=None,
    grace_period_update_interval,
    after_shutdown_update_interval,
):
    """Return the interval that should be used for the current state.

    Priority order (highest to lowest):
    1. Powered on — always use powered interval
    2. DC charging — use dc_charging_update_interval (typically shorter than AC)
    3. AC charging — use charging_update_interval
    4. Grace period — recent activity but not powered/charging
    5. After shutdown window
    6. Default idle interval
    """
    if is_powered_on:
        return powered_update_interval

    if is_dc_charging and dc_charging_update_interval is not None:
        return dc_charging_update_interval

    if is_charging:
        return charging_update_interval

    if (
        activity_duration <= grace_period_update_interval
        or idle_duration <= grace_period_update_interval
    ):
        return grace_period_update_interval

    if idle_duration <= after_shutdown_update_interval:
        return after_shutdown_update_interval

    if not isinstance(default_update_interval, timedelta):
        raise TypeError("default_update_interval must be a timedelta")

    return default_update_interval
