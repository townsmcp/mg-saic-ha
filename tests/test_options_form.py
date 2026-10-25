"""The options form saves when opened and submitted unchanged (#269).

The battery capacity and fuel tank overrides are saved as numbers but sit in
text fields. Offered back as numbers, the untouched fields were submitted as
numbers and the form rejected them ("expected str"), so once an override was
set, Configure could not be saved again until both were retyped.
"""

import asyncio
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

CF = sys.modules["mg_saic.config_flow"]
CAPACITY = CF.CONF_BATTERY_CAPACITY_OVERRIDE
TANK = CF.CONF_FUEL_TANK_OVERRIDE


class _Field(str):
    """Stands in for vol.Optional and keeps the field's description."""

    def __new__(cls, key, **kw):
        field = super().__new__(cls, key)
        field.description = kw.get("description") or {}
        return field


def _suggested(saved):
    """The suggested values the form offers for the two overrides."""
    flow = CF.SAICMGOptionsFlowHandler.__new__(CF.SAICMGOptionsFlowHandler)
    flow.config_entry = NS(options=saved, data={})
    flow.async_show_form = lambda **kw: kw
    with patch.object(CF.vol, "Optional", _Field):
        form = asyncio.run(flow.async_step_init(None))
    fields = {str(f): f for f in form["data_schema"]}
    return {
        key: fields[key].description.get("suggested_value")
        for key in (CAPACITY, TANK)
    }


class OverrideAsTextTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(CF.override_as_text(64.0), "64")
        self.assertEqual(CF.override_as_text(64.5), "64.5")
        self.assertEqual(CF.override_as_text(37), "37")

    def test_nothing_saved(self):
        self.assertEqual(CF.override_as_text(None), "")
        self.assertEqual(CF.override_as_text(""), "")

    def test_text_left_alone(self):
        self.assertEqual(CF.override_as_text("51.4"), "51.4")


class OptionsFormOffersTextTests(unittest.TestCase):
    def test_saved_overrides_are_offered_as_text(self):
        offered = _suggested({CAPACITY: 64.0, TANK: 37.0})
        self.assertEqual(offered, {CAPACITY: "64", TANK: "37"})
        for value in offered.values():
            self.assertIsInstance(value, str)

    def test_no_overrides_offer_blank(self):
        self.assertEqual(_suggested({}), {CAPACITY: "", TANK: ""})

    def test_offered_text_saves_back_as_the_same_number(self):
        offered = _suggested({CAPACITY: 64.5, TANK: 37.0})
        flow = CF.SAICMGOptionsFlowHandler.__new__(CF.SAICMGOptionsFlowHandler)
        flow.config_entry = NS(options={CAPACITY: 64.5, TANK: 37.0}, data={})
        flow._validate_abrp = AsyncMock(return_value={})
        flow.async_create_entry = lambda title, data: {"data": data}
        data = asyncio.run(flow.async_step_init(dict(offered)))["data"]
        self.assertEqual(data[CAPACITY], 64.5)
        self.assertEqual(data[TANK], 37.0)


if __name__ == "__main__":
    unittest.main()
