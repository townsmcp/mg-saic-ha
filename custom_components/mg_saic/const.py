# File: const.py

import logging
import voluptuous as vol
from homeassistant.helpers import config_validation as cv
from datetime import timedelta
from enum import Enum
from typing import Union

LOGGER = logging.getLogger(__package__)

DOMAIN = "mg_saic"

# API Base Urls
REGION_BASE_URIS = {
    "EU": "https://gateway-mg-eu.soimt.com/api.app/v1/",
    "China": "https://tap-cn.soimt.com/api.app/v1/",
    "Australia": "https://gateway-mg-au.soimt.com/api.app/v1/",
    "Brazil": "https://gateway-mg-br.soimt.com/api.app/v1/",
    "Israel": "https://gateway-mg-il.soimt.com/api.app/v1/",
    "Turkey": "https://gateway-mg-tr.soimt.com/api.app/v1/",
    "India": "https://gateway-mg-in.soimt.com/api.app/v1/",
    "Thailand": "https://gateway-mg-th.soimt.com/api.app/v1/",
    "Rest of World": "https://gateway-mg-eu.soimt.com/api.app/v1/",
}

# Region codes sent in the REGION request header (see saic-python-client-ng
# SaicApiConfiguration).  These follow the upstream mqtt-gateway convention.
REGION_API_CODES = {
    "EU": "eu",
    "China": "cn",
    "Australia": "au",
    "Brazil": "br",
    "Israel": "il",
    "Turkey": "tr",
    "India": "in",
    "Thailand": "th",
    "Rest of World": "eu",
}

# Default tenant ID (EU production value; part of the request signature).
DEFAULT_TENANT_ID = "459771"

# Scheduled charging mode: display label <-> saic-ismart-client-ng
# ScheduledChargingMode enum member name. Raw codes: 1 = until scheduled end
# time, 2 = disabled, 3 = until target SOC (bmsReserCtrlDspCmd readback uses
# the same codes; 0/None means no schedule reported).
SCHEDULED_CHARGING_MODE_LABELS = {
    "Disabled": "DISABLED",
    "Until Target SOC": "UNTIL_CONFIGURED_SOC",
    "Until Scheduled Time": "UNTIL_CONFIGURED_TIME",
}

# Config flow option for a user-supplied endpoint (base URI / region code /
# tenant ID), for markets that run on separate SAIC infrastructure.
REGION_CUSTOM = "Custom"

# List of regions for selection in the config flow
REGION_CHOICES = list(REGION_BASE_URIS.keys()) + [REGION_CUSTOM]

# Phone Login Country Codes
COUNTRY_CODES = [
    {"code": "+1", "country": "USA"},
    {"code": "+7", "country": "Russia"},
    {"code": "+20", "country": "Egypt"},
    {"code": "+27", "country": "South Africa"},
    {"code": "+30", "country": "Greece"},
    {"code": "+31", "country": "Netherlands"},
    {"code": "+32", "country": "Belgium"},
    {"code": "+33", "country": "France"},
    {"code": "+34", "country": "Spain"},
    {"code": "+36", "country": "Hungary"},
    {"code": "+39", "country": "Italy"},
    {"code": "+40", "country": "Romania"},
    {"code": "+41", "country": "Switzerland"},
    {"code": "+43", "country": "Austria"},
    {"code": "+44", "country": "United Kingdom"},
    {"code": "+45", "country": "Denmark"},
    {"code": "+46", "country": "Sweden"},
    {"code": "+47", "country": "Norway"},
    {"code": "+48", "country": "Poland"},
    {"code": "+49", "country": "Germany"},
    {"code": "+52", "country": "Mexico"},
    {"code": "+53", "country": "Cuba"},
    {"code": "+54", "country": "Argentina"},
    {"code": "+55", "country": "Brazil"},
    {"code": "+56", "country": "Chile"},
    {"code": "+57", "country": "Colombia"},
    {"code": "+58", "country": "Venezuela"},
    {"code": "+60", "country": "Malaysia"},
    {"code": "+61", "country": "Australia"},
    {"code": "+62", "country": "Indonesia"},
    {"code": "+63", "country": "Philippines"},
    {"code": "+64", "country": "New Zealand"},
    {"code": "+65", "country": "Singapore"},
    {"code": "+66", "country": "Thailand"},
    {"code": "+81", "country": "Japan"},
    {"code": "+82", "country": "South Korea"},
    {"code": "+86", "country": "China"},
    {"code": "+90", "country": "Turkey"},
    {"code": "+91", "country": "India"},
    {"code": "+351", "country": "Portugal"},
    {"code": "+355", "country": "Albania"},
    {"code": "+357", "country": "Cyprus"},
    {"code": "+358", "country": "Finland"},
    {"code": "+359", "country": "Bulgaria"},
    {"code": "+370", "country": "Lithuania"},
    {"code": "+371", "country": "Latvia"},
    {"code": "+372", "country": "Estonia"},
    {"code": "+373", "country": "Moldova"},
    {"code": "+385", "country": "Croatia"},
    {"code": "+386", "country": "Slovenia"},
    {"code": "+387", "country": "Bosnia and Herzegovina"},
    {"code": "+389", "country": "North Macedonia"},
    {"code": "+420", "country": "Czech Republic"},
    {"code": "+421", "country": "Slovakia"},
    {"code": "+381", "country": "Serbia"},
    {"code": "+382", "country": "Montenegro"},
    {"code": "+354", "country": "Iceland"},
    {"code": "+353", "country": "Ireland"},
    {"code": "+380", "country": "Ukraine"},
    {"code": "+596", "country": "Martinique"},
    {"code": "+852", "country": "Hong Kong"},
    {"code": "+966", "country": "Saudi Arabia"},
    {"code": "+971", "country": "United Arab Emirates"},
    {"code": "+972", "country": "Israel"},
]

# Conversion factors
PRESSURE_TO_BAR = 0.04
DATA_DECIMAL_CORRECTION = 0.1
DATA_DECIMAL_CORRECTION_SOC = 0.1
DATA_100_DECIMAL_CORRECTION = 0.01

# Conversion factors for charging data
CHARGING_CURRENT_FACTOR = 0.05
CHARGING_VOLTAGE_FACTOR = 0.25

# Per-vehicle-series profiles.
#
# The SAIC API exposes some values (notably AC temperature index scale and
# totalBatteryCapacity) inconsistently or unreliably across models, so these
# are tracked here per series rather than trusted from the API response.
# Series codes come from VinInfo.series (e.g. "EH32SP3", "MIS3E S") and are
# matched as a substring, consistent with the existing detection pattern.
#
# Fields:
#   min_temp / max_temp / temp_offset: AC temperature index mapping. See
#       MGSAICDataUpdateCoordinator.get_ac_temperature_idx for usage.
#   battery_capacity_kwh: known-good usable battery capacity in kWh, used to
#       override the API's totalBatteryCapacity field when it is known to be
#       inaccurate. None means "trust the API value" (no override).
#
# Sources: EH32 (MG4 Electric) values were already present in this codebase
# prior to this table's introduction. MIS3E (MGS6 EV) values confirmed
# against MG UK's official spec sheet and cross-referenced with EV Database,
# electrive.com, and Carwow (77 kWh gross / 74.3 kWh usable, same across
# single-motor Long Range and Dual Motor variants).
VEHICLE_PROFILES = {
    "ZP22": {  # MG3 Hybrid (HEV) — see #258
        "min_temp": 16,
        "max_temp": 30,
        "temp_offset": 2,
        "battery_capacity_kwh": None,
        # start_ac reports remoteClimateStatus=2 while running — whether it is
        # heating or cooling (the car can't distinguish, it just drives to the
        # requested temperature). Map 2 to the on-state so the mode read-back
        # isn't mis-decoded as fan_only, and clear fan_only (this car has none).
        "climate_status_cool": {2},
        "climate_status_fan_only": set(),
        # This car only honours the SIMPLE start_ac command (temperature only).
        # The full control_climate command (with the fan-speed byte) — which
        # both "Cool" and "Front Defrost" ride on — is silently ignored: the car
        # echoes remoteClimateStatus=3 (or stays 0 for defrost) but never
        # actions it. Confirmed from user HVAC tests + logs (#258). So route
        # Cool via start_ac, drop the fan slider, and don't offer Front Defrost.
        "cool_uses_start_ac": True,
        "has_front_defrost": False,
        # The car tracks only the driver window. passengerWindow comes back a
        # phantom (stuck =1) and there are no real rear-window sensors — the
        # iSmart app itself shows only the driver window (WINDOW bitmask 1000).
        "has_front_passenger_window": False,
        "has_rear_windows": False,
        # HEV (no plug) → no Target SOC. Also already gated by vehicle_type,
        # but set explicitly so the profile is self-describing.
        "supports_target_soc": False,
    },
    "EH32": {  # MG4 Electric
        "min_temp": 17,
        "max_temp": 33,
        "temp_offset": 3,
        "battery_capacity_kwh": None,
        # remoteClimateStatus decode, confirmed from decrypted iSmart traffic
        # and live telemetry (PR #173, kindel0): 2 = HEAT (PTC resistive heater
        # active), 3 = COOL (compressor active). 4 is assumed fan-only by
        # elimination (not independently confirmed). The MG4 heats with the
        # compressor OFF and the AUTO fan value — see the heat path in
        # climate.py (_set_hvac_fan_speed).
        "climate_status_cool": {3},
        "climate_status_heat": {2},
        "climate_status_fan_only": {4},
        # Fan speed values for cooling mode (1=low, 2=med, 3=high).
        # Byte values 4 and 5 are NOT higher fan speeds — on MG4-family cars
        # they trigger heating/front-defrost. Sending 5 as "High" put the car
        # into front defrost and made it report remoteClimateStatus=5 (which the
        # integration then read as defrost, not cooling). See #243. Keep the
        # slider strictly within the safe 1/2/3 range.
        "fan_speed_low": 1,
        "fan_speed_medium": 2,
        "fan_speed_high": 3,
        # Temperature index direction: False = forward (low temp -> low idx)
        "temp_idx_inverted": False,
        # Whether the car supports setting a Target SOC via the SAIC API.
        # True for most BEV/PHEV models; set False for models where the iSmart
        # app does not expose this control (prevents an always-Unknown entity).
        "supports_target_soc": True,
        # Whether the fuelRangeElec field in basicVehicleStatus is reliable for
        # this model.  When False the electric range sensor falls back to
        # bmsEstdElecRng from chrgMgmtData (estimated range after full charge)
        # instead of the per-second live value, which the API returns as -128.
        "reliable_fuel_range_elec": True,
    },
    "AH4EM": {  # MG4 EV URBAN (entry variant; series 'AH4EM L')
        # Confirmed by olflo (#243) through direct testing. Despite being an
        # MG4, this variant does NOT use the fan-speed scheme of the standard
        # MG4 (EH32). It uses mode_select: the API's "fan_speed" byte is a MODE
        # selector that the car echoes back verbatim as remoteClimateStatus.
        # Confirmed with the AC command on (value sent == remoteClimateStatus):
        #   1 -> fan only  (HVAC runs but does not cool)
        #   3 -> cooling   (confirmed: cabin cooled, climate tile stayed on)
        #   5 -> front defrost
        # Mode 3 is therefore used as the (only confirmed) cool mode.
        #
        # Mode 2's meaning on THIS car is NOT confirmed. It was only seen via the
        # fan-only path and reported as "on but not fan-only", which is
        # ambiguous — and on the sister MG4 (EH32) mode 2 is HEAT, not cool (see
        # PR #173, confirmed from decrypted traffic). So we deliberately do NOT
        # send 2 for cooling: doing so risks heating the cabin when the user
        # asked for cool. If mode 2 is later confirmed (auto-cool vs PTC heat),
        # add it here — and if it turns out to be heat, this car may gain a Heat
        # mode it can't get from the limited iSmart app.
        #
        # No heat mode is exposed yet (the app has none and mode 2/4 are
        # unconfirmed); climate_status_heat is left unset, which suppresses the
        # Heat HVAC mode (see climate.py). The iSmart app also has no
        # front-defrost button, so exposing the Defrost preset (mode 5, confirmed
        # working) gives the owner a control the app lacks.
        #
        # Temperature range/offset follow the MG4 (EH32); the car still honours a
        # target temperature under the cool mode. Not independently re-verified
        # for this variant — revisit if an owner reports the target temperature
        # landing wrong.
        "min_temp": 17,
        "max_temp": 33,
        "temp_offset": 3,
        "battery_capacity_kwh": None,
        "temp_idx_inverted": False,
        "supports_target_soc": True,
        "reliable_fuel_range_elec": True,
        # --- mode_select climate scheme ---
        "climate_control_scheme": "mode_select",
        "climate_mode_fan_only": 1,
        "climate_mode_cool": 3,       # only confirmed cool value on this car
        "climate_mode_defrost": 5,
        # No distinct climate_mode_max_cool: mode 3 is the only confirmed cool,
        # so plain Cool already uses the strongest cooling this car has. The
        # Max Cool preset is still offered via max_cool_forces_min_temp below —
        # it sends that same cool mode but pins the target temperature to the
        # profile minimum (17°C), mirroring the iSmart app's one-tap LOW-cool
        # button (temperature to lowest + fan max in a single action; #243).
        # No climate_mode_heat — unconfirmed, and this car has no heater, so a
        # matching Max Heat is deliberately not offered here.
        "max_cool_forces_min_temp": True,
        "climate_status_fan_only": {1},
        "climate_status_cool": {3},
        "climate_status_defrost": {5},
        # No climate_status_heat — leaving it unset suppresses the Heat mode.
    },
    "MIS3E": {  # MGS6 EV (Long Range and Dual Motor)
        "min_temp": 16,
        "max_temp": 30,
        "temp_offset": 2,  # retained for the fallback formula; index map takes priority
        "battery_capacity_kwh": 74.3,
        # Temperature index: NOT inverted. Confirmed by decrypting the iSmart
        # app's own climate commands (2026-07-04):
        #   16°C→1, 19°C→5, 22/23°C→9, 25°C→11, 28°C→14, 30°C→19
        # The earlier "idx=14 at 16°C, inverted" note was an incorrect inference
        # made before request decryption was available — the decrypted app
        # traffic supersedes it. The middle of the range (17–28°C) is a clean
        # idx = temp - 14; the two extremes are special values (16°C→1, 30°C→19),
        # so a direct lookup map is used rather than a linear formula.
        "temp_idx_inverted": False,
        "temp_index_map": {
            16: 1,
            17: 3,
            18: 4,
            19: 5,
            20: 6,
            21: 7,
            22: 8,
            23: 9,
            24: 10,
            25: 11,
            26: 12,
            27: 13,
            28: 14,
            29: 16,   # interpolated (not directly captured)
            30: 19,
        },
        "supports_target_soc": True,
        "reliable_fuel_range_elec": True,
        # The SAIC API incorrectly reports modelYear='2024' for the MGS6 EV.
        # The MGS6 launched globally to dealerships in November 2025 — there is
        # no 2024 model year variant.  Override to the correct value.
        "model_year_override": "2025",
        # --- mode_select climate scheme ---
        # The MGS6's iSmart app exposes only temperature + AC on/off (no fan
        # control at all — confirmed by the owner). Decrypted climate commands
        # (rvcReqType=6) show paramId 19 as a MODE selector, not a fan speed:
        #   2 = cool (auto fan, follows target temp)  — CONFIRMED
        #   5 = defrost / front windscreen            — CONFIRMED (front demist)
        #   0 = off                                   — CONFIRMED
        #
        # Front defrost detail (decrypted capture 2026-07-15): the app sends
        # rvcReqType=6 with paramId 19=5 AND paramId 20=8 — i.e. mode 5 bundled
        # with temperature index 8 = 22°C. It does NOT drop the temperature to
        # minimum; the A/C running during defrost is inherent to demisting
        # (dehumidification), not a cold setpoint. The car auto-cancels defrost
        # after ~10 minutes (the app warns of this) and remoteClimateStatus
        # returns to 0. The library's start_front_defrost() sends exactly the
        # same thing (fan_speed=5, temperature_idx=8), so the Front Defrost
        # SWITCH matches the app byte-for-byte.
        #   CAVEAT: the climate entity's "Defrost" PRESET instead sends the
        #   user's current target temperature (see _send_climate_command), so
        #   the two paths can differ. Both MG's app and the library hardcode
        #   22°C, which hints 22°C is the expected pairing — whether the car
        #   honours defrost at other temperatures is unconfirmed.
        #
        # Rear defrost is NOT part of this mode enum: it is a separate command
        # (rvcReqType=32, paramId 23=1) that sets rmtHtdRrWndSt and leaves
        # remoteClimateStatus untouched at 0, running concurrently with any
        # climate mode. It is therefore a standalone switch, never a preset.
        # Heat / max-cool / fan-only were NOT observed via the app on this model
        # (the app has no such controls), so those values are best-effort
        # inherited defaults and may not do anything on the MGS6.
        "climate_control_scheme": "mode_select",
        "climate_mode_cool": 2,        # CONFIRMED (decrypted app traffic)
        "climate_mode_defrost": 5,     # CONFIRMED (front windscreen button)
        "climate_mode_fan_only": 1,    # unconfirmed on MGS6 (no app control)
        "climate_mode_heat": 4,        # unconfirmed on MGS6 (no app control)
        "climate_mode_max_cool": 3,    # unconfirmed on MGS6 (no app control)
        "climate_status_cool": {2, 3},
        "climate_status_fan_only": {1},
        "climate_status_heat": {4},
        "climate_status_defrost": {5},
    },
    "MZS3E": {  # MGS5 EV (sister to the MGS6 / MIS3E) — see #277
        # Confirmed by owner @jeffreyguilmot (#277, series 'MZS3E S'): the MGS5
        # cools at remoteClimateStatus=2 (iSmart app shows AC on, cabin cools
        # rapidly) — but with no dedicated profile it fell through to
        # DEFAULT_VEHICLE_PROFILE, which maps 2 -> fan_only, so the climate
        # entity misreported "fan_only" while the car was actually cooling.
        # The MGS5 shares the MGS6's mode_select climate scheme, so the climate
        # config mirrors MIS3E. NOTE: battery_capacity_kwh and temp_index_map
        # are inherited from the MGS6 as best-effort and are NOT independently
        # confirmed for the MGS5 (variants differ) — the confirmed change here
        # is the climate_status mapping.
        "min_temp": 16,
        "max_temp": 30,
        "temp_offset": 2,
        "battery_capacity_kwh": None,  # MGS5 variants differ; unconfirmed
        "temp_idx_inverted": False,
        "temp_index_map": {
            16: 1, 17: 3, 18: 4, 19: 5, 20: 6, 21: 7, 22: 8, 23: 9, 24: 10,
            25: 11, 26: 12, 27: 13, 28: 14, 29: 16, 30: 19,
        },
        "supports_target_soc": True,
        "reliable_fuel_range_elec": True,
        # --- mode_select climate scheme (mirrors the MGS6) ---
        "climate_control_scheme": "mode_select",
        "climate_mode_cool": 2,
        "climate_mode_defrost": 5,
        "climate_mode_fan_only": 1,
        "climate_mode_heat": 4,
        "climate_mode_max_cool": 3,
        "climate_status_cool": {2, 3},   # CONFIRMED 2=cool on the MGS5 (#277)
        "climate_status_fan_only": {1},
        "climate_status_heat": {4},
        "climate_status_defrost": {5},
    },
    "EC32": {  # MG Cyberster (2-door BEV roadster/convertible)
        # The Cyberster has no rear doors or rear windows — see the
        # has_rear_doors/has_rear_windows override at the bottom of this
        # profile. Originally this was inferred automatically from the
        # DOOR/WINDOW bitmask in vehicleModelConfiguration, but that field
        # proved unreliable across other models (issue #203) so it's now an
        # explicit profile flag instead.
        #
        # fuelRangeElec: the log shows -128 (sentinel value) when parked, same
        # pattern as the HS PHEV.  Fall back to bmsEstdElecRng instead.
        #
        # Battery: API reports totalBatteryCapacity=725 → 72.5 kWh with ×0.1
        # factor.  MG spec quotes 77 kWh gross / ~72.5 kWh usable — plausible,
        # so no override needed.
        "min_temp": 16,
        "max_temp": 28,
        "temp_offset": 2,
        "battery_capacity_kwh": None,
        "climate_status_cool": {3},
        "climate_status_fan_only": {2},
        "fan_speed_low": 1,
        "fan_speed_medium": 3,
        "fan_speed_high": 5,
        "temp_idx_inverted": False,
        "supports_target_soc": True,
        "reliable_fuel_range_elec": False,
        "supports_charging_current_limit": True,
        # The Cyberster is a 2-door convertible with no rear doors or rear
        # glass windows. Previously this was inferred from the API's own
        # DOOR/WINDOW vehicleModelConfiguration bitmask, but that data proved
        # unreliable: MG4 and MGS5 (genuine 4-door/4-window cars) report
        # WINDOW='0000' identically to the Cyberster, which incorrectly
        # suppressed their rear window entities (issue #203). This is now an
        # explicit per-model override instead of trusting the API field.
        "has_rear_doors": False,
        "has_rear_windows": False,
    },
    "IS31P": {  # MG S9 PHEV (2025)
        # Series string from API: 'IS31P L'
        # Confirmed by eladrichi (issue #204), modelYear='2025', PHEV.
        #
        # IMPORTANT — this model uses the "mode_select" climate scheme, NOT a
        # fan speed slider. Extensive testing by eladrichi (issue #204) plus
        # independent analysis established that the API's "fan_speed" byte is
        # actually a climate MODE selector: the car echoes it back verbatim as
        # remoteClimateStatus, and each value selects a fixed operating mode,
        # NOT a linear fan intensity. The car manages its own fan speed.
        #
        # Confirmed value → behaviour map (value sent == remoteClimateStatus):
        #   1 → fan only, AC compressor off
        #   2 → AC cooling, AUTO fan, follows the HA target temperature, main
        #       vents. This is the sensible default "cool" mode.
        #   3 → AC cooling, fixed strong fan (~6/11), main vents, temp forced
        #       low. Exposed as the "Max Cool" preset (fast cool-down / boost).
        #   4 → heat: max temp, ~3/11 fan, leg vents. Exposed as HVAC "heat".
        #   5 → defrost: upper/windscreen vents. Exposed as "Defrost" preset.
        #
        # HA presents this as:
        #   hvac_modes:   off / fan_only / cool / heat
        #   preset_modes: Max Cool / Defrost
        #   (no fan-speed slider — it would misrepresent a mode selector)
        #
        # Temperature index confirmed correct (temp_idx_inverted=False):
        #   16°C → index 2, 22°C → index 8 (matches temp_offset=2 formula).
        "min_temp": 16,
        "max_temp": 28,
        "temp_offset": 2,
        "battery_capacity_kwh": None,
        "temp_idx_inverted": False,
        "supports_target_soc": True,
        "reliable_fuel_range_elec": True,
        "supports_charging_current_limit": True,
        # --- mode_select climate scheme ---
        "climate_control_scheme": "mode_select",
        "climate_mode_fan_only": 1,
        "climate_mode_cool": 2,
        "climate_mode_heat": 4,
        "climate_mode_max_cool": 3,
        "climate_mode_defrost": 5,
        # Reverse maps: remoteClimateStatus value → HA state.
        #   cool covers both the auto-cool (2) and Max-Cool boost (3) values,
        #   so a running boost still shows as "cool" in HA.
        "climate_status_cool": {2, 3},
        "climate_status_fan_only": {1},
        "climate_status_heat": {4},
        "climate_status_defrost": {5},
    },
    "AS33P": {  # MG HS PHEV (2025/2026 Super Hybrid)
        # Series string from API: 'AS33P S'
        # Battery capacity: API reports totalBatteryCapacity=725 (→ 72.5 kWh with
        # ×0.1 factor), which is incorrect by a factor of ~3.  The HS PHEV has a
        # 24.7 kWh usable PHEV battery; override here so the sensor shows correctly.
        # lastChargeEndingPower similarly reports 724 (÷10 = 72.4 kWh) — the profile
        # battery_capacity_kwh override covers totalBatteryCapacity; lastChargeEndingPower
        # is corrected via PHEV_BATTERY_CAPACITY_CORRECTION_FACTOR in the profile.
        "min_temp": 16,
        "max_temp": 28,
        "temp_offset": 2,
        "battery_capacity_kwh": 24.7,
        "climate_status_cool": {3},
        "climate_status_fan_only": {2},
        "fan_speed_low": 1,
        "fan_speed_medium": 3,
        "fan_speed_high": 5,
        "temp_idx_inverted": False,
        # iSmart app does not expose Target SOC control for the HS PHEV —
        # bmsOnBdChrgTrgtSOCDspCmd is always 0 (unmapped) so the slider would
        # permanently show Unknown.  Confirmed by Harry (issue #198, 2026 HS PHEV).
        # Suppress both the slider and the status sensor for this model.
        "supports_target_soc": False,
        # iSmart app does not expose Charging Current Limit for the HS PHEV —
        # attempting to set it returns a "Target SOC could not be found" error.
        # Suppress both the status sensor and the select control for this model.
        "supports_charging_current_limit": False,
        # The API returns fuelRangeElec=-128 (sentinel) for this model when the car
        # is parked — the live electric range field is not populated.  Fall back to
        # bmsEstdElecRng (estimated range after full charge) from chrgMgmtData.
        "reliable_fuel_range_elec": False,
        # Correction factor for energy-based fields that the API reports inflated
        # by approximately ×3 (totalBatteryCapacity, lastChargeEndingPower).
        # 24.7 kWh / 72.5 kWh (API) ≈ 0.3407; applying ×0.1 factor then ×(1/3)
        # is equivalent to using the raw value ÷ 30 rather than ÷ 10.
        # This is stored as a divisor multiplier applied on top of the standard
        # DATA_DECIMAL_CORRECTION — see SAICMGChargingSensor for usage.
        "charging_capacity_correction": 1 / 3,
    },
}

# Fallback profile used when the vehicle's series does not match any entry
# in VEHICLE_PROFILES above (e.g. MG5, ZS EV, or any model not yet profiled).
# Values match the original integration behaviour so existing users are unaffected.
DEFAULT_VEHICLE_PROFILE = {
    "min_temp": 16,
    "max_temp": 28,
    "temp_offset": 2,
    "battery_capacity_kwh": None,
    "climate_status_cool": {3},
    "climate_status_fan_only": {2},
    # Fan byte values 4 and 5 are unsafe on the SAIC climate protocol — on
    # MG-family cars they trigger heating/front-defrost rather than a faster
    # fan. Selecting "High" previously sent 5, which put unprofiled MG4-family
    # cars (e.g. the MG4 EV URBAN, series AH4EM) into front defrost and made
    # them report remoteClimateStatus=5 — read by the integration as defrost,
    # not cooling. Keep the slider within the safe 1/2/3 range. See #243.
    "fan_speed_low": 1,
    "fan_speed_medium": 2,
    "fan_speed_high": 3,
    "temp_idx_inverted": False,
    # Default: assume Target SOC is supported (safe for BEV/PHEV unless known otherwise).
    "supports_target_soc": True,
    # Default: assume Charging Current Limit is supported (correct for most BEV/PHEV).
    "supports_charging_current_limit": True,
    # Default: assume the fuelRangeElec field is reliable (correct for most BEVs).
    "reliable_fuel_range_elec": True,
    # Default: no capacity correction needed (API value is correct for most models).
    "charging_capacity_correction": None,
    # Default: no model year override (API value is correct for most models).
    "model_year_override": None,
    # Default: assume the car has rear doors and rear windows (true for the
    # vast majority of models — 4-door cars). Only override to False for
    # models confirmed to genuinely lack them, e.g. EC32 (Cyberster).
    # See issue #203 for why this is a profile flag rather than read from
    # the API's own DOOR/WINDOW vehicleModelConfiguration bitmask.
    "has_rear_doors": True,
    "has_rear_windows": True,
    # Default climate control scheme: "fan_speed" — HA shows a Low/Med/High
    # fan slider. Only override to "mode_select" for models where the API's
    # fan_speed byte is really a mode selector (see IS31P). The mode_select
    # value keys (climate_mode_*, climate_status_heat/defrost) are only read
    # when the scheme is "mode_select", so they don't need to appear here.
    "climate_control_scheme": "fan_speed",
}

# Base update intervals
# UPDATE_INTERVAL is the idle/parked background refresh — a safety net to keep
# data from going completely stale.  Now that the SAICMGAccountPoller triggers
# an immediate refresh on engine-start, shutdown, and charging events, this
# interval only matters when the car is genuinely sitting idle with nothing
# happening.  30 minutes is a good balance: fresh enough to be useful, infrequent
# enough not to drain the 12V battery or hit API rate limits.
# Users can still override this lower via the integration options if they prefer.
UPDATE_INTERVAL = timedelta(minutes=30)
UPDATE_INTERVAL_CHARGING = timedelta(minutes=5)
UPDATE_INTERVAL_DC_CHARGING = timedelta(minutes=5)
UPDATE_INTERVAL_POWERED = timedelta(minutes=15)

# Additional Update Intervals
UPDATE_INTERVAL_AFTER_SHUTDOWN = timedelta(minutes=2)
UPDATE_INTERVAL_GRACE_PERIOD = timedelta(minutes=10)

# When an update cycle fails outright (e.g. a transient "return code 4" that
# exhausts its retries), the interval-selection step never runs, so the
# coordinator would otherwise keep whatever interval the last *successful* cycle
# chose — which can be a multi-hour idle interval. If that failed poll happened
# to be the first of a charging session, the car's charge would go completely
# unpolled until the next idle wake-up (#238, MG HS PHEV). To avoid that, a
# failed cycle retries after this shorter interval instead — but only for a
# bounded number of consecutive failures, so a car that is genuinely away or
# asleep for a long time is not polled every few minutes indefinitely.
UPDATE_INTERVAL_AFTER_FAILURE = timedelta(minutes=5)
MAX_FAST_RETRIES_AFTER_FAILURE = 3

# After action immediate and refresh intervals
AFTER_ACTION_UPDATE_INTERVAL_DELAY = timedelta(seconds=15)

# Default additional long-interval updates after actions
DEFAULT_ALARM_LONG_INTERVAL = timedelta(minutes=5)
DEFAULT_AC_LONG_INTERVAL = timedelta(minutes=15)
DEFAULT_FRONT_DEFROST_LONG_INTERVAL = timedelta(minutes=15)
DEFAULT_REAR_WINDOW_HEAT_LONG_INTERVAL = timedelta(minutes=15)
DEFAULT_LOCK_UNLOCK_LONG_INTERVAL = timedelta(minutes=5)
DEFAULT_CHARGING_PORT_LOCK_LONG_INTERVAL = timedelta(minutes=5)
DEFAULT_HEATED_SEATS_LONG_INTERVAL = timedelta(minutes=15)
DEFAULT_BATTERY_HEATING_LONG_INTERVAL = timedelta(minutes=15)
DEFAULT_CHARGING_LONG_INTERVAL = timedelta(minutes=5)
DEFAULT_SUNROOF_LONG_INTERVAL = timedelta(minutes=5)
DEFAULT_TAILGATE_LONG_INTERVAL = timedelta(minutes=5)
DEFAULT_TARGET_SOC_LONG_INTERVAL = timedelta(minutes=5)
DEFAULT_CHARGING_CURRENT_LONG_INTERVAL = timedelta(minutes=5)

# Configuration Options
CONF_HAS_SUNROOF = "has_sunroof"
CONF_HAS_HEATED_SEATS = "has_heated_seats"
CONF_HAS_REAR_HEATED_SEATS = "has_rear_heated_seats"
CONF_HAS_BATTERY_HEATING = "has_battery_heating"
CONF_HAS_STEERING_WHEEL_HEAT = "has_steering_wheel_heat"
CONF_HAS_WINDOW_CONTROL = "has_window_control"

# Window control (rvcReqType=3) WINDOW_OPEN_CLOSE (paramId 13) values.
# Confirmed by decrypting iSmart app traffic on the MGS6 EV (MIS3E) and
# cross-checking the resulting window status in the response. Command values:
#   0 = close all four door windows
#   1 = ventilate (crack all four open a few cm — the app's "Ventilation")
#   2 = fully open all four door windows
# (Verified against a timestamped capture where the ventilate button was pressed
#  and the outgoing command carried paramId 13 = 1.)
#
# NOTE: the car's window *status* fields (driverWindow, passengerWindow,
# rearLeftWindow, rearRightWindow) are binary (0=closed, 1=open) and do NOT
# distinguish ventilated from fully open — both report as open (1). Confirmed
# across multiple captures: no third window-position value was ever observed.
#
# These commands act on all four door windows together; the car does not accept
# single-window control via this API. The sunroof (paramId 8) is always left
# untouched (0).
# Other models are unconfirmed; the same values are used on the assumption the
# command set is shared, and users can report back if their car differs.
WINDOW_ACTION_CLOSE = 0
WINDOW_ACTION_VENTILATE = 1
WINDOW_ACTION_OPEN = 2

# Vehicle window status field names (basicVehicleStatus), each 0=closed / 1=open.
WINDOW_STATUS_FIELDS = (
    "driverWindow",
    "passengerWindow",
    "rearLeftWindow",
    "rearRightWindow",
)

# remoteClimateStatus is a "climate mode" enum. Under the mode_select scheme the
# value SENT in paramId 19 is echoed back verbatim in this field, so the mode
# values and the status values are the same number. Values confirmed from
# decrypted MGS6 (MIS3E) captures:
#   0 = off
#   2 = cool / A/C running (remote session; car off)
#   5 = front defrost (confirmed: app sends rvcReqType=6 paramId 19=5 with
#       paramId 20=8, i.e. a bundled 22°C; status then reads 5, and the car
#       auto-cancels it after ~10 minutes)
#   6 = climate running under LOCAL (in-car) control — see below
#
# On value 6 — the control SOURCE, not a separate mode. CONFIRMED by test.
# Across all captures every remoteClimateStatus=2 observation had the car OFF
# (engineStatus=0, powerMode=0), i.e. a genuine remote session, while the =6
# observation had the car ON and being driven (engineStatus=1, powerMode=2,
# 12V at 13.9V with the DC-DC running, new journey ID, key seen, alarm
# disarmed). So 6 means "the climate is on, but the driver is operating it
# locally in the car", not a remote command.
#
# The decisive control test (2026-07-17): car powered ON (engineStatus=1,
# powerMode=2, 12V=13.9V) with the climate switched OFF reads
# remoteClimateStatus = 0 — NOT 6. This rules out the alternative reading that
# 6 is merely a "car is on / local-control mode" flag that would appear
# regardless of the HVAC. 6 therefore requires the climate to actually be
# running, under local control.
#   (An earlier comment here attributed 6 to "a sunroof session". That was
#   wrong: the capture in question contained no control commands at all, and
#   this vehicle has no sunroof (S35 Sunroof itemValue='0'). What it actually
#   recorded was the owner getting in and driving away with the climate on.)
#
# IMPORTANT — ventilation is NOT reliably represented here. An earlier
# assumption that remoteClimateStatus=2 covered ventilation was DISPROVEN by a
# live log: ventilating from cold (no A/C running) left remoteClimateStatus=0
# while the windows opened. The =2 seen in an earlier test was the A/C, which
# had been started before ventilating. Combined with the fact that the window
# status cannot distinguish "ventilated" from "fully open", there is no
# reliable status field for "is ventilating". The Ventilation binary sensor
# therefore uses OPTIMISTIC state tracked in the coordinator (see
# coordinator.ventilation_active), reflecting the last ventilate command sent
# from Home Assistant. Ventilation triggered from the iSmart app is not
# reflected — a known, documented gap.
# Front defrost always runs at a fixed 22°C. Both MG's iSmart app and the
# saic-ismart-client-ng library hardcode this: the app sends rvcReqType=6 with
# paramId 19=5 (defrost) AND paramId 20=8 (temperature index 8 = 22°C), and the
# library's start_front_defrost() does the same (fan_speed=5, temperature_idx=8).
# The integration therefore forces 22°C for front defrost on BOTH paths — the
# Front Defrost switch and the climate "Defrost" preset — rather than sending
# whatever target temperature the user happens to have on the slider. This keeps
# the two paths byte-identical to each other and to the app, and avoids sending
# the car a defrost/temperature pairing that has never been observed in the
# wild (it is unconfirmed whether the car honours defrost at other temps).
FRONT_DEFROST_TEMP_C = 22


# Holiday mode: a runtime override that slows idle polling right down to reduce
# wake-ups (and, on PHEVs, 12V parasitic drain) while the car is left for long
# periods — without the user having to edit and later restore their configured
# intervals. It is a switch (on/off), overrides the configured intervals at
# runtime only, and is deliberately NOT written back to the config options.
DEFAULT_HOLIDAY_UPDATE_INTERVAL_HOURS = 12
CONF_HOLIDAY_UPDATE_INTERVAL = "holiday_update_interval"

# Deep-sleep / reachability sensor.
# The API has no "asleep" field, so the state is inferred:
#   awake         — car powered on, or a recent successful live contact
#   likely_asleep — car idle beyond the staleness threshold (data may be stale)
#   unreachable   — a live command OR a status poll recently failed with return
#                   code 4 (the car itself confirming it can't be reached);
#                   cleared again when the car answers with a fresh statusTime
# The idle basis is the vehicle's OWN reported activity (last_vehicle_activity /
# powerMode), NOT the time since we last polled — so slowing polling (e.g.
# holiday mode) does not falsely flip the sensor to asleep.
#
# Battery voltage is DELIBERATELY NOT used to drive the state. Field evidence
# (issue #235) shows the vehicle mis-reports its own aux voltage — HA showed
# 11.7V while a calibrated external monitor read 12.13V at the same moment —
# so a fixed voltage threshold would act on an unreliable number. Instead the
# reported voltage is exposed as a sensor ATTRIBUTE (early-warning evidence,
# labelled as vehicle-reported and possibly inaccurate) alongside inactivity
# hours, last command result, and data age.
VEHICLE_REACHABILITY_AWAKE = "awake"
VEHICLE_REACHABILITY_LIKELY_ASLEEP = "likely_asleep"
VEHICLE_REACHABILITY_UNREACHABLE = "unreachable"

# Data Freshness sensor (#238): how current the data from the last poll was.
# A separate axis from reachability — the car can be "awake" while the poll
# still returned "cached" data. Values stay lowercase snake_case so
# automations/templates match on them; translations provide display labels.
DATA_FRESHNESS_LIVE = "live"
DATA_FRESHNESS_CACHED = "cached"
DATA_FRESHNESS_FAILED = "failed"
# Hours of vehicle inactivity after which data is treated as possibly stale.
# Configurable; default sits comfortably inside the observed ~1-day sleep onset.
DEFAULT_STALE_DATA_THRESHOLD_HOURS = 12
CONF_STALE_DATA_THRESHOLD = "stale_data_threshold_hours"
# Remote-command return code that means "can't reach the car right now".
SAIC_RETURN_CODE_UNREACHABLE = 4

# --- A Better Route Planner (ABRP) integration -----------------------------
# Pushes this vehicle's telemetry (SoC, range, position, charging state, ...)
# to ABRP's live-data API so it can plan routes without an OBD dongle.
# Iternio uses two credentials, and the USER supplies BOTH:
#   * an API KEY that identifies the application sending data — the user
#     obtains their own from the Iternio developer portal (see ABRP_DOC_URL);
#   * a per-vehicle USER TOKEN that the user generates in the ABRP app
#     (Settings -> the car -> Live Data -> "Generic"/MQTT source).
# Both are pasted in the options flow and stored per VIN (each config entry is a
# single vehicle). ABRP is enabled for a vehicle only when BOTH are provided;
# leaving either blank keeps ABRP disabled for that vehicle. The integration
# ships no default/shared API key.
ABRP_BASE_URL = "https://api.iternio.com/1"
ABRP_ME_URL = f"{ABRP_BASE_URL}/oauth/me"
ABRP_SEND_URL = f"{ABRP_BASE_URL}/tlm/send"
# Where users obtain their token / API key / read about the API (shown in the UI).
ABRP_DOC_URL = "https://www.iternio.com/api"

CONF_ABRP_USER_TOKEN = "abrp_user_token"
CONF_ABRP_API_KEY = "abrp_api_key"

REMOTE_CLIMATE_STATUS_OFF = 0
REMOTE_CLIMATE_STATUS_ACTIVE = 2  # reports A/C / HVAC (NOT a ventilation flag)
REMOTE_CLIMATE_STATUS_DEFROST = 5  # front defrost (mode value echoed back)
REMOTE_CLIMATE_STATUS_LOCAL = 6  # climate RUNNING under local (in-car) control

# Heated seat control (rvcReqType=5, HEATED_SEATS). Each seat is addressed by
# its own paramId and sent independently (confirmed via decrypted MGS6 traffic).
# Front seats: 0=off, 1=low, 2=medium, 3=high.
# Rear seats:  on/off in the app, but the app sends level 3 for "on", 0 for off.
HEATED_SEATS_REQ_TYPE_VALUE = "5"
HEATED_SEAT_PARAM_IDS = {
    "front_left": 17,
    "front_right": 18,
    "rear_left": 25,
    "rear_right": 26,
}
REAR_SEAT_ON_LEVEL = 3  # value the app sends for rear-seat "on"

# Heated steering wheel — NOT exposed by the saic client library. Captured from
# decrypted MGS6 traffic: rvcReqType=8, paramId 24, value 1=on / 0=off.
STEERING_WHEEL_HEAT_REQ_TYPE_VALUE = "8"
STEERING_WHEEL_HEAT_PARAM_ID = 24

# Generic response tresholds
GENERIC_RESPONSE_SOC_THRESHOLD = 1000
GENERIC_RESPONSE_STATUS_THRESHOLD = 0
GENERIC_RESPONSE_TEMPERATURE = -40
GENERIC_RESPONSE_EXTREME_TEMPERATURE = -128

# Sanity bounds for the API's statusTime field. A response whose timestamp
# falls outside these bounds relative to "now" is treated as untrustworthy
# and discarded (see SAICMGDataUpdateCoordinator._is_status_timestamp_valid).
STATUS_TIMESTAMP_FUTURE_TOLERANCE = timedelta(minutes=5)
STATUS_TIMESTAMP_MAX_AGE = timedelta(hours=24)

# Retry configuration
RETRY_LIMIT = 5
RETRY_BACKOFF_FACTOR = 15

# Maximum seconds to wait for the very first API fetch during HA startup.
# If the SAIC server is unreachable and we exceed this, we raise
# ConfigEntryNotReady so HA can finish booting and retry in the background
# rather than blocking startup for up to RETRY_LIMIT × RETRY_BACKOFF_FACTOR
# seconds (75 s) before failing.
#
# Set to 30 s (raised from 15 s). SAIC's API response times have increased over
# time, and the previous 15 s ceiling frequently cancelled requests that would
# have succeeded a few seconds later when SAIC was slow-but-alive (issue #216).
# Because the integration is declared iot_class=cloud_polling and raises
# ConfigEntryNotReady on timeout, waiting 30 s here only delays THIS entry
# becoming ready — it does NOT block Home Assistant's overall startup or any
# other integration. The only cost of the higher value is a slightly longer
# give-up time per retry when SAIC is fully down, which happens in the
# background and is never user-visible.
# HA retries automatically with exponential backoff once ConfigEntryNotReady
# is raised.
STARTUP_API_TIMEOUT = 30

# During initial setup only, cap the charging-info fetch to this many seconds.
# Charging is the SAIC endpoint most prone to being slow/degraded (issue #216),
# and it is NOT required for the integration to load — info + status are enough
# (option 1). If charging doesn't return within this inner cap at startup, we
# abandon just that fetch and let setup complete, leaving charging sensors to
# populate on the next scheduled refresh. This keeps one slow endpoint from
# eating the whole STARTUP_API_TIMEOUT budget.
STARTUP_CHARGING_TIMEOUT = 12

# On routine (non-startup) refreshes, cap the charging-info fetch too. The
# charging endpoint can fail for long stretches independently of everything
# else (SAIC-side, return code 4). Without a cap it runs the full retry ladder
# (RETRY_LIMIT x RETRY_BACKOFF_FACTOR) and then aborts the whole cycle, holding
# up / failing the entire refresh — including a user's manual refresh
# (reported by @HarryFlatter, #262). Charging is non-essential (status is the
# core payload), so we bound it and proceed without it on failure.
RUNTIME_CHARGING_TIMEOUT = 20

# Transient temperature-spike guard (#277). Some status refreshes — especially
# a wake-from-idle or the refresh right after a climate command — briefly report
# bad temperature values across fields (e.g. exterior 33°C -> 45°C, interior
# jumping the wrong way while cooling). If a reading jumps more than
# TEMP_SPIKE_MAX_JUMP_C from the last accepted value AND the last accepted value
# was within TEMP_SPIKE_GUARD_WINDOW_S seconds (i.e. rapid polling, not a normal
# long idle gap), we skip that SINGLE reading and retain the last value. The
# next reading is always accepted, so a genuinely fast change is only delayed
# one poll and never permanently hidden. Deliberately generous: air temperature
# can't plausibly move this much this fast, even with the A/C running flat out.
TEMP_SPIKE_MAX_JUMP_C = 10
TEMP_SPIKE_GUARD_WINDOW_S = 300

# basicVehicleStatus.mileage is a 16-bit field, so once the odometer passes
# 6553.5 km it saturates at the uint16 maximum (65535) and stays there (#280).
# Treat that exact value as invalid so the mileage sensor falls back to the
# wider ChrgMgmtData.mileage field, which holds the true odometer.
MILEAGE_UINT16_SATURATION = 65535

# Charging status codes indicating that the vehicle is actively using the
# charging/discharging system.  Used by the coordinator to select the
# charging update interval and keep the session alive.
# 13 = V2X_DISCHARGING — included so V2X export sessions get the same
# frequent refresh cadence as AC/DC charging sessions.
CHARGING_STATUS_CODES = {1, 3, 10, 12, 13}

# Charging Current Limit options
CHARGING_CURRENT_OPTIONS = ["0A (Ignore)", "6A", "8A", "16A", "Max"]

# Platforms
PLATFORMS = [
    "binary_sensor",
    "button",
    "climate",
    "device_tracker",
    "event",
    "lock",
    "number",
    "select",
    "sensor",
    "switch",
    "time",
]


# Battery SOC
class BatterySoc(Enum):
    """Enum for Battery SOC identification"""

    SOC_40 = 1
    SOC_50 = 2
    SOC_60 = 3
    SOC_70 = 4
    SOC_80 = 5
    SOC_90 = 6
    SOC_100 = 7


# Charge Current Limit
class ChargeCurrentLimitOption(Enum):
    C_IGNORE = 0
    C_6A = 1
    C_8A = 2
    C_16A = 3
    C_MAX = 4

    @staticmethod
    def to_code(limit: Union[str, "ChargeCurrentLimitOption"]):
        LOGGER.debug(f"Converting limit: {limit} (type: {type(limit)}) to code")
        if isinstance(limit, ChargeCurrentLimitOption):
            return limit
        if isinstance(limit, str):
            limit_upper = limit.upper()
            match limit_upper:
                case "6A":
                    return ChargeCurrentLimitOption.C_6A
                case "8A":
                    return ChargeCurrentLimitOption.C_8A
                case "16A":
                    return ChargeCurrentLimitOption.C_16A
                case "MAX":
                    return ChargeCurrentLimitOption.C_MAX
                case "0A (IGNORE)":
                    return ChargeCurrentLimitOption.C_IGNORE
                case "0A":
                    return ChargeCurrentLimitOption.C_IGNORE
                case _:
                    LOGGER.error(f"Unknown charge current limit: {limit}")
                    raise ValueError(f"Unknown charge current limit: {limit}")
        LOGGER.error(f"Invalid type for limit: {type(limit)}")
        raise TypeError(f"Invalid type for limit: {type(limit)}")

    @property
    def limit(self) -> str:
        match self:
            case ChargeCurrentLimitOption.C_6A:
                return "6A"
            case ChargeCurrentLimitOption.C_8A:
                return "8A"
            case ChargeCurrentLimitOption.C_16A:
                return "16A"
            case ChargeCurrentLimitOption.C_MAX:
                return "Max"
            case ChargeCurrentLimitOption.C_IGNORE:
                return "0A (Ignore)"
            case _:
                raise ValueError(f"Unknown charge current limit code: {self}")


# Windows List
class VehicleWindowId(Enum):
    """Enum for identifying vehicle windows."""

    DRIVER = "driver"
    WINDOW_2 = "window_2"
    WINDOW_3 = "window_3"
    WINDOW_4 = "window_4"
    SUNROOF = "sunroof"
