"""Wheel Tyre Monitor Status follows the car's own per-tyre warnings (#497).

@SteveMSJ: the car's wheelTyreMonitorStatus stayed 0 through all three of
these, while the car and iSmart warned about the tyre. The warning was in
alertDataSum instead, one slot per tyre, anticlockwise from the front left:

    MGS5, puncture, rear right 0.88 bar        -> slot 4 = 2
    MG4, rear left let down to 2.24 bar        -> slot 3 = 2   (20 Nov 15:00)
    MG4, front left let down to 1.88 bar       -> slot 2 = 2   (20 Nov 16:00)

Front right isn't tested yet: slot 1 or 5, both read as it, unconfirmed.
"""

import unittest
from types import SimpleNamespace as NS

from test_data_refresh_sensor import BINARY


def _alerts(**slots):
    alerts = [0] * 64
    for slot, value in slots.items():
        alerts[int(slot[1:])] = value
    return alerts


def _entity(flag=0, alerts=None, *, status=True):
    basic = NS(wheelTyreMonitorStatus=flag)
    st = NS(basicVehicleStatus=basic)
    if alerts is not None:
        st.extendedVehicleStatus = NS(alertDataSum=alerts)
    coordinator = NS(
        data={"status": st if status else None},
        vin_info=NS(vin="VIN1", brandName="MG", modelName="MG4 Electric"),
        last_update_success=True,
    )
    entity = BINARY.SAICMGWheelTyreMonitorBinarySensor.__new__(
        BINARY.SAICMGWheelTyreMonitorBinarySensor
    )
    entity.coordinator = coordinator
    entity._data_type = "status"
    entity._field = "wheelTyreMonitorStatus"
    entity._last_valid_state = None
    entity._last_attrs = None
    return entity


class StevesTestsTests(unittest.TestCase):
    def test_mg4_rear_left(self):
        e = _entity(alerts=_alerts(s3=2))
        self.assertTrue(e.is_on)
        self.assertEqual(e.extra_state_attributes["warning_tyres"], ["Rear Left"])
        self.assertEqual(e.extra_state_attributes["car_tyre_monitor_flag"], 0)

    def test_mg4_front_left(self):
        e = _entity(alerts=_alerts(s2=2))
        self.assertTrue(e.is_on)
        self.assertEqual(e.extra_state_attributes["warning_tyres"], ["Front Left"])

    def test_mgs5_rear_right(self):
        e = _entity(alerts=_alerts(s4=2))
        self.assertTrue(e.is_on)
        self.assertEqual(e.extra_state_attributes["warning_tyres"], ["Rear Right"])

    def test_all_fine(self):
        e = _entity(alerts=_alerts())
        self.assertFalse(e.is_on)
        self.assertEqual(e.extra_state_attributes["warning_tyres"], [])


class FrontRightTests(unittest.TestCase):
    def test_slot_1_or_5_is_front_right_unconfirmed(self):
        for slot in ("s1", "s5"):
            with self.subTest(slot=slot):
                e = _entity(alerts=_alerts(**{slot: 2}))
                self.assertTrue(e.is_on)
                self.assertEqual(
                    e.extra_state_attributes["warning_tyres"], ["Front Right (unconfirmed)"]
                )

    def test_listed_once_if_both_set(self):
        e = _entity(alerts=_alerts(s1=2, s5=2))
        self.assertEqual(e.extra_state_attributes["warning_tyres"], ["Front Right (unconfirmed)"])


class OtherTests(unittest.TestCase):
    def test_any_non_zero_value_counts(self):
        self.assertTrue(_entity(alerts=_alerts(s3=7)).is_on)

    def test_slots_outside_the_tyres_dont_count(self):
        e = _entity(alerts=_alerts(s0=2, s9=3))
        self.assertFalse(e.is_on)
        self.assertEqual(e.extra_state_attributes["car_alert_slots"], {0: 2, 9: 3})

    def test_cars_own_flag_still_counts(self):
        e = _entity(flag=1, alerts=_alerts())
        self.assertTrue(e.is_on)

    def test_no_alert_list_falls_back_to_the_flag(self):
        self.assertFalse(_entity(flag=0).is_on)
        self.assertTrue(_entity(flag=1).is_on)

    def test_held_through_a_poll_without_status(self):
        e = _entity(alerts=_alerts(s3=2))
        self.assertTrue(e.is_on)
        e.coordinator.data["status"] = None
        self.assertTrue(e.is_on)
        self.assertEqual(e.extra_state_attributes["warning_tyres"], ["Rear Left"])

    def test_same_unique_id_as_before(self):
        # Built with the same arguments as the old generic sensor, so the
        # entity and its history carry on.
        source = BINARY.__file__
        text = open(source).read()
        self.assertIn('SAICMGWheelTyreMonitorBinarySensor(\n                coordinator,\n                entry,\n                "Wheel Tyre Monitor Status",\n                "wheelTyreMonitorStatus",', text)
        self.assertTrue(issubclass(BINARY.SAICMGWheelTyreMonitorBinarySensor, BINARY.SAICMGBinarySensor))


if __name__ == "__main__":
    unittest.main()
