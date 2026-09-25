"""MGS6 (MIS3E) climate: HIGH matches the iSmart app byte for byte.

Decrypted iSmart capture, 2026-09-24 20:26:48 UTC+1, MGS6 EV, HIGH pressed in
the app (temperature dial untouched):

    POST /vehicle/control  rvcReqType 6
      paramId 19 (mode)          [2]    temperature-following mode
      paramId 20 (temperature)   [19]   index 19 = 30 °C, the maximum
      paramId 22 (AC flag)       [0]    OFF -- although the app showed "AC on"
      paramId 255 (end marker)   [0]

and the car reported remoteClimateStatus 2 for the whole session. HA's HIGH
was sending mode 4 (fixed max heat) with the AC flag on -- a different command
(the car reported status 4). These tests pin HA's HIGH to the capture, using
the car's REAL profile (coordinator._apply_vehicle_profile) and REAL
temperature table, and -- where the pinned SAIC client library is installed --
the real library building the request body.

Also covers the display and error-handling fixes from the same day: status 4
showing as OFF, the preset being wiped to NONE by the car's stale status, a
failed preset leaving the card at 30 °C, and code 8 always being reported as
"start the car with the key".
"""

import asyncio
import base64
import json
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_climate_presets_and_local_control import CLIMATE, _Base

COORD_MOD = sys.modules["mg_saic.coordinator"]
COORD_CLS = COORD_MOD.SAICMGDataUpdateCoordinator
API_MOD = sys.modules["mg_saic.api"]
LOGIC_MOD = sys.modules["mg_saic.logic"]

# The captures, as the app sent them (paramId -> raw bytes).
APP_HIGH_2026_09_24 = {19: bytes([2]), 20: bytes([19]), 22: bytes([0]), 255: bytes([0])}
# 2026-09-25 08:24:19 UTC+1, LOW pressed in the app: the same shape as HIGH,
# at the minimum temperature (index 1 = 16 °C). Car reported status 2 for the
# whole session; the app displayed "LOW" and "AC on".
APP_LOW_2026_09_25 = {19: bytes([2]), 20: bytes([1]), 22: bytes([0]), 255: bytes([0])}

# The SAIC client the integration pins (manifest: mg-saic-client). The shared
# test harness replaces it with a stub in THIS process, so the real library
# runs in a clean subprocess -- which the stubs can't reach.
HAVE_SAIC_LIB = (
    subprocess.run(
        [sys.executable, "-c", "import saic_ismart_client_ng"], capture_output=True, timeout=60
    ).returncode
    == 0
)

_BUILD_BODY = """
import asyncio, json, sys
from dataclasses import asdict
from saic_ismart_client_ng import SaicApi
from saic_ismart_client_ng.model import SaicApiConfiguration
kw = json.loads(sys.argv[1])
api = SaicApi(SaicApiConfiguration(username="offline@example.com", password="x"))
out = {}
async def capture(body, vin):  # replaces the network send: nothing leaves
    out["body"] = asdict(body)
api.send_vehicle_control_command = capture
asyncio.run(api.control_climate("OFFLINE0000000000", **kw))
print(json.dumps(out["body"]))
"""


def _real_library_body(**kwargs):
    """The /vehicle/control body the real pinned library builds (not sent)."""
    result = subprocess.run(
        [sys.executable, "-c", _BUILD_BODY, json.dumps(kwargs)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(result.stdout)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _with_profile(coordinator, series):
    """Give a fixture coordinator a car's REAL profile and temperature table."""
    coordinator.vehicle_series = series
    coordinator.battery_capacity_override = None
    COORD_CLS._apply_vehicle_profile(coordinator)
    coordinator.get_ac_temperature_idx = lambda temp: COORD_CLS.get_ac_temperature_idx(
        coordinator, temp
    )
    return coordinator


class _MGS6(_Base):
    def _mgs6(self, *, status=0):
        entity = self._entity(status=status)
        _with_profile(entity.coordinator, "MIS3E S")
        entity.coordinator.requested_target_temp = 22.0
        return entity


class HighMatchesTheAppTests(_MGS6):
    def test_high_sends_the_apps_command(self):
        entity = self._mgs6()
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_HIGH))
        entity._client.start_climate.assert_called_once()
        kwargs = entity._client.start_climate.call_args.kwargs
        self.assertEqual(
            (kwargs["fan_speed"], kwargs["temperature_idx"], kwargs["ac_on"]),
            (2, 19, False),
            "MGS6 HIGH must be mode 2 at the maximum temperature with AC off",
        )

    @unittest.skipUnless(HAVE_SAIC_LIB, "pinned SAIC client library not installed")
    def test_high_request_body_matches_the_capture(self):
        """End to end: our HIGH, through the real SAIC library, produces the
        same request as the app -- apart from the library's 4-byte end marker
        (the app sends 1 byte; the car accepts both, and it's library-level,
        not something the integration controls)."""
        entity = self._mgs6()
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_HIGH))
        kwargs = entity._client.start_climate.call_args.kwargs

        body = _real_library_body(
            fan_speed=kwargs["fan_speed"],
            ac_on=kwargs["ac_on"],
            temperature_idx=kwargs["temperature_idx"],
        )
        self.assertEqual(str(body["rvcReqType"]), "6")
        ours = {p["paramId"]: base64.b64decode(p["paramValue"]) for p in body["rvcParams"]}
        self.assertEqual(sorted(ours), sorted(APP_HIGH_2026_09_24))
        for param_id in (19, 20, 22):
            self.assertEqual(ours[param_id], APP_HIGH_2026_09_24[param_id], f"paramId {param_id}")
        self.assertEqual(ours[255], bytes(4), "library's end marker changed -- recheck vs app")

    def test_other_mode_select_cars_keep_their_max_heat_high(self):
        """Only a car with a captured app HIGH changes; the Marvel R keeps its
        confirmed max-heat byte."""
        entity = self._entity()
        _with_profile(entity.coordinator, "EP21")
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_HIGH))
        kwargs = entity._client.start_climate.call_args.kwargs
        self.assertEqual(kwargs["fan_speed"], 4)
        self.assertTrue(kwargs["ac_on"])


class LowMatchesTheAppTests(_MGS6):
    def test_low_sends_the_apps_command(self):
        entity = self._mgs6()
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        kwargs = entity._client.start_climate.call_args.kwargs
        self.assertEqual(
            (kwargs["fan_speed"], kwargs["temperature_idx"], kwargs["ac_on"]),
            (2, 1, False),
            "MGS6 LOW must be mode 2 at the minimum temperature with AC off",
        )
        self.assertEqual(entity.coordinator.requested_target_temp, 16)

    @unittest.skipUnless(HAVE_SAIC_LIB, "pinned SAIC client library not installed")
    def test_low_request_body_matches_the_capture(self):
        entity = self._mgs6()
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        kwargs = entity._client.start_climate.call_args.kwargs
        body = _real_library_body(
            fan_speed=kwargs["fan_speed"],
            ac_on=kwargs["ac_on"],
            temperature_idx=kwargs["temperature_idx"],
        )
        self.assertEqual(str(body["rvcReqType"]), "6")
        ours = {p["paramId"]: base64.b64decode(p["paramValue"]) for p in body["rvcParams"]}
        self.assertEqual(sorted(ours), sorted(APP_LOW_2026_09_25))
        for param_id in (19, 20, 22):
            self.assertEqual(ours[param_id], APP_LOW_2026_09_25[param_id], f"paramId {param_id}")

    def test_other_mode_select_cars_keep_their_max_cool_low(self):
        entity = self._entity()
        _with_profile(entity.coordinator, "EP21")
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        self.assertEqual(entity._client.start_climate.call_args.kwargs["fan_speed"], 3)


def _activity_coordinator(requested):
    """A real coordinator's _detect_activity, with just what it reads."""
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "TESTVIN"
    c.is_charging = False
    c.enable_shutdown_refresh_sequence = False
    c._shutdown_refresh_task = None
    c.requested_hvac_mode = requested
    return c


def _climate_status(c, value):
    c._detect_activity(SimpleNamespace(
        lockStatus=1, powerMode=0, driverDoor=0, passengerDoor=0, rearLeftDoor=0,
        rearRightDoor=0, bootStatus=0, bonnetStatus=0, remoteClimateStatus=value,
        rmtHtdRrWndSt=0, engineStatus=0))


class SessionEndForgetsTheRequestTests(unittest.TestCase):
    def test_session_ending_clears_it(self):
        c = _activity_coordinator("heat")
        _climate_status(c, 2)
        _climate_status(c, 0)
        self.assertEqual(c.requested_hvac_mode, "off")

    def test_the_status_lag_after_a_command_does_not(self):
        """Right after HA sends a command the car still reports 0."""
        c = _activity_coordinator("cool")
        _climate_status(c, 0)
        _climate_status(c, 0)
        self.assertEqual(c.requested_hvac_mode, "cool")

    def test_a_session_starting_does_not(self):
        c = _activity_coordinator("cool")
        _climate_status(c, 0)
        _climate_status(c, 2)
        self.assertEqual(c.requested_hvac_mode, "cool")


class AppStartedSessionDisplayTests(_MGS6):
    def test_this_morning_replayed(self):
        """HA HIGH last night (request 'heat'), its session ended, then the
        app's LOW at 08:24 (status 2, cooling 22 -> 18 °C). HA showed Heat
        throughout; it must now say Heat/Cool -- running, direction unknown."""
        c = _activity_coordinator("heat")
        _climate_status(c, 2)   # last night's HIGH session
        _climate_status(c, 0)   # ended
        _climate_status(c, 2)   # app LOW this morning

        entity = self._mgs6(status=2)
        entity.coordinator.requested_hvac_mode = c.requested_hvac_mode
        self.assertEqual(entity.hvac_mode, CLIMATE.HVACMode.HEAT_COOL)
        # ...and the Climate Mode sensor, on a real coordinator with the
        # MGS6 profile, says the same.
        sensor_side = COORD_CLS.__new__(COORD_CLS)
        sensor_side.vehicle_series = "MIS3E S"
        sensor_side.battery_capacity_override = None
        COORD_CLS._apply_vehicle_profile(sensor_side)
        sensor_side.data = {"status": SimpleNamespace(
            basicVehicleStatus=SimpleNamespace(remoteClimateStatus=2))}
        sensor_side.requested_hvac_mode = c.requested_hvac_mode
        self.assertEqual(sensor_side.climate_mode_from_status(), "heat_cool")

    def test_a_session_ha_started_keeps_its_direction(self):
        entity = self._mgs6(status=2)
        entity.coordinator.requested_hvac_mode = "cool"
        self.assertEqual(entity.hvac_mode, CLIMATE.HVACMode.COOL)
        entity.coordinator.requested_hvac_mode = "heat"
        self.assertEqual(entity.hvac_mode, CLIMATE.HVACMode.HEAT)


class DisplayTests(_MGS6):
    def test_status_4_shows_heat_not_off(self):
        """07:08:55 -> 07:24: the car in mode 4, HA showing OFF."""
        entity = self._mgs6(status=4)
        self.assertEqual(entity.hvac_mode, CLIMATE.HVACMode.HEAT)

    def test_preset_survives_the_cars_stale_off_after_sending(self):
        """07:08:47 and 20:46:45: a successful HIGH wiped to NONE because the
        car still reported the pre-command status 0."""
        entity = self._mgs6(status=0)
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_HIGH))
        self.assertEqual(entity.preset_mode, CLIMATE.PRESET_HIGH)

    def test_preset_clears_once_the_grace_window_has_passed(self):
        entity = self._mgs6(status=0)
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_HIGH))
        entity._last_command_ts = time.monotonic() - CLIMATE.COMMAND_SYNC_GRACE_SECONDS - 1
        self.assertEqual(entity.preset_mode, CLIMATE.PRESET_NONE)


class FailedPresetTests(_MGS6):
    def test_rejected_high_puts_the_target_back(self):
        """07:07:26: a rejected HIGH left the card at 30 °C, climate off."""
        entity = self._mgs6()
        entity._client.start_climate = AsyncMock(
            side_effect=API_MOD.CommandsLimitReachedException("return code: 8, message: x")
        )
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_HIGH))
        self.assertEqual(entity.coordinator.requested_target_temp, 22.0)
        self.assertIsNone(entity.coordinator.pre_preset_target_temp)

    def test_a_later_failure_does_not_undo_a_command_the_car_accepted(self):
        entity = self._mgs6()
        entity.coordinator.schedule_action_refresh = AsyncMock(side_effect=RuntimeError("x"))
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_HIGH))
        self.assertEqual(entity.coordinator.requested_target_temp, 30)


class Code8MessageTests(unittest.TestCase):
    def test_saic_message_is_extracted(self):
        self.assertEqual(
            API_MOD.saic_message(Exception("return code: 8, message: Operation too frequent")),
            "Operation too frequent",
        )
        self.assertIsNone(API_MOD.saic_message(Exception("boom")))

    def test_advice_follows_what_saic_said(self):
        advice = LOGIC_MOD.command_rejection_advice
        self.assertIn("key", advice("Remote control limit reached"))
        self.assertIn("Wait a minute", advice("Operation too frequent"))
        for unknown in (None, "", "Something else entirely"):
            self.assertNotIn("key", advice(unknown), unknown)

    def test_notification_quotes_saic_and_never_invents_a_key_start(self):
        c = COORD_CLS.__new__(COORD_CLS)
        c.vin_info = SimpleNamespace(brandName="MG", modelName="MGS6 EV")
        c.client = SimpleNamespace(last_rejection_message="Something else entirely")
        c.hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock()))
        c._command_error_event_entity = None
        _run(c.notify_command_limit_reached("VIN1"))
        payload = c.hass.services.async_call.call_args.args[2]
        self.assertIn("Something else entirely", payload["message"])
        self.assertNotIn("key", payload["message"].lower())
        self.assertNotIn("limit reached", payload["title"].lower())


if __name__ == "__main__":
    unittest.main()
