# Sensors & Vehicle Profiles Reference

Every entity the integration creates, what it means, and how battery capacity is worked out. Start here if you're trying to understand a specific sensor's value or attributes.

← [Back to the main README](../README.md)

---

## SENSORS AVAILABLE
 
The MG/SAIC Custom Integration provides the following sensors, binary sensors, and controls. Not all entities are available on every vehicle — availability depends on vehicle type (BEV, PHEV, HEV, ICE) and optional equipment.
 
> **Looking for what "on"/"off" or a particular sensor value actually means?** See the [Entity States Reference](#entity-states-reference) section below — it lists every possible state for every status and control entity.
 
### SENSORS
 
#### General
- Brand
- Model
- Model Year
- VIN *(displayed masked, e.g. `LS**********46986`; the full VIN is available as the `vin_full` attribute for use in automations/services)*
- Mileage
- Interior Temperature
- Exterior Temperature
- Ancillary Battery Voltage *(12V battery)*
- Speed
- Power Mode
- Last Key Seen *(raw key fob identifier; shown as Unknown when key is not present)*
- Last Powered On
- Last Powered Off
- Last Vehicle Activity
- Last Update Time
- Next Update Time
#### Tyre Pressure
- Tyre Pressure Front Left
- Tyre Pressure Front Right
- Tyre Pressure Rear Left
- Tyre Pressure Rear Right
#### Electric / Hybrid
- State of Charge (SOC) *(BEV/PHEV; also HEV on self-charging hybrids with no charge port, e.g. MG3 Hybrid+ — see [Vehicle Profiles](#vehicle-profiles))*
- Electric Range
- Instant Power *(kW draw/regen while driving; negative = traction, positive = regen/charge)*
- Fuel Level *(PHEV/HEV/ICE only)*
- Fuel Range *(PHEV/HEV/ICE only)*
#### Climate
- Front Left Heated Seat Level *(if equipped)*
- Front Right Heated Seat Level *(if equipped)*
- Steering Wheel Heat *(if equipped)*
  *(Note: the AC/HVAC running state itself is a **binary sensor**, not a sensor — see "HVAC Status" below.)*
#### Charging Data *(BEV/PHEV)*
- Charging Status *(Unplugged / Charging (AC) / Charging (DC) / V2X Discharging / …)*
- Charging Voltage
- Charging Current
- Charging Current Limit
- Charging Power
- Estimated Range After Charging *(the range expected when the current charge completes — see [Trip & efficiency statistics](#trip--efficiency-statistics))*
- Target SOC *(read-only mirror of the Target SOC slider — shown only on models where the iSmart app supports it)*
- Charging Duration
- Remaining Charging Time
- Added Electric Range *(the range the last charge added, where the car reports it — see [Trip & efficiency statistics](#trip--efficiency-statistics))*
- Power Usage Since Last Charge
- Mileage Since Last Charge
- Efficiency Since Last Charge *(BEV/PHEV; km/kWh, derived from the two sensors above — see [Trip & efficiency statistics](#trip--efficiency-statistics))*
- Efficiency Since Charge (SOC) *(BEV/PHEV; km/kWh, an SOC/odometer-only alternative independent of the counters above — see [Trip & efficiency statistics](#trip--efficiency-statistics))*
- Last Charge Range Added *(BEV/PHEV; electric range the last completed charge put back — shown in your Home Assistant unit system, so miles if that's what you use)*
- Last Charge Energy *(BEV/PHEV; kWh put **into** the battery by the last completed charge — see [Trip & efficiency statistics](#trip--efficiency-statistics))*
- Last Trip Distance *(distance driven on the last completed drive)*
- Last Trip Efficiency *(BEV/PHEV; switchable km/kWh · mi/kWh · kWh/100km, full breakdown in attributes)*
- Last Trip Fuel Economy *(ICE/HEV/PHEV; L/100km, with the full breakdown in its attributes)*
- Total Battery Capacity *(kWh; corrected for models where the API reports an inaccurate value, and can be overridden per vehicle — see [Battery capacity override](#battery-capacity-override))*
- Battery Heating Status *(if equipped)*
- Reachability *(is the car awake / likely asleep / unreachable — see [Deep sleep & holiday mode](power-management.md#deep-sleep--holiday-mode))*
- Data Freshness *(diagnostic: whether the last poll returned `live`, `cached` or `failed` data — see [Data Freshness sensor](power-management.md#data-freshness-sensor))*
### Trip & efficiency statistics

The integration derives per-trip and per-charge efficiency from data it already collects — the odometer, state of charge, and (for combustion models) fuel level — so no extra setup is needed.

**Efficiency Since Last Charge** *(BEV/PHEV)* comes straight from the car's own `Mileage Since Last Charge` and `Power Usage Since Last Charge` figures, so it's available immediately and needs no trip tracking.

**Efficiency Since Charge (SOC)** *(BEV/PHEV)* is an alternative to the sensor above, computed entirely from the odometer and battery percentage — it never touches the `Mileage Since Last Charge` / `Power Usage Since Last Charge` fields at all. It exists because those fields are unreliable on some cars (they can reset spuriously without an actual charge — see below) and permanently unpopulated (`Unknown`) on others; this sensor works either way, and lets you compare the two where both are available. Its "since charge" point is where the car was last seen to gain charge, which may not always be a full charge to 100%. That's taken from the car's own charging state where it reports one, and otherwise inferred: a battery percentage rise while the odometer is unchanged means energy came from outside the car. A rise after the car has moved is regen, not a charge, so it doesn't reset the measurement — without that distinction, arriving somewhere downhill on a net regen gain would look identical to plugging in and would silently drop the leg you'd just driven from the figures.

**Last Charge Energy** *(BEV/PHEV)* reports how much energy the last completed charge put **into** the battery. The API has no field for this — it reports charging power live, and `Power Usage Since Last Charge` (energy taken back *out* afterwards), but there is no "starting power" to subtract from `lastChargeEndingPower` — so the session is measured across its start and end. Two independent figures are produced, and both appear in the attributes:

- `energy_added_kWh_soc` — the rise in battery percentage × the usable capacity. This is the headline value, because it works on any car that reports SOC and has a known capacity (see [Battery capacity override](#battery-capacity-override) if yours is wrong).
- `energy_added_kWh_counter` — the change in the car's own pack-energy figure (`lastChargeEndingPower` minus `Power Usage Since Last Charge`). Independent of the capacity figure, but it relies on the car refreshing `lastChargeEndingPower` promptly when the charge ends, so it's omitted when it doesn't look plausible.

Also in the attributes: `range_added_km` (with `range_start_km` / `range_end_km`), `soc_start_pct`, `soc_end_pct`, `soc_added_pct`, `duration_s`, `average_power_kW`, `method` (which figure was used), and the session's start/end timestamps. A `mg_saic_charge_completed` event fires when a charge finishes, carrying the same data, so you can log or notify on it.

The same figure is also published as its own **Last Charge Range Added** sensor. Prefer that one for dashboards: sensor states are converted to your Home Assistant unit system (so miles on an imperial setup), whereas attribute values never are — the `*_km` attributes below are always kilometres regardless of your settings.

**Added Electric Range** shows the electric range a charge added, in kilometres as the car reports it, taken straight from the car rather than calculated. Support varies by model and there is nothing the integration can do about that: an MG IM5 reports it and keeps the figure between charges, while an MGS6 and an MG HS PHEV report `0` throughout a charge with everything else reporting healthily.

Where the car doesn't populate it the sensor reads unknown rather than `0`, so an absent field no longer looks like a working sensor reporting nothing. If yours shows a value, it is the car's own figure; if you want a number that works regardless of model, use **Last Charge Range Added** instead, which is measured across the charging session.

**Estimated Range After Charging** shows the range you'll have when the current charge finishes. The car usually works this out itself, but some models never do: on an MG HS PHEV the field stays at `0` throughout a charge, as does its discharging equivalent, so that car appears not to compute range estimates at all.

Where the car gives no usable figure, the integration projects one instead, from the electric range and battery percentage the car does report, scaled to whatever the charge is heading for — the target SOC where one is set, or 100% where there isn't. It needs no battery capacity, so a [capacity override](#battery-capacity-override) has no effect on it. Checked against a car reporting both: 257 km at 51.7% with an 80% target projects to 398 km, where the car itself said 410.

A `source` attribute says which you're looking at: `reported` when it's the car's own figure, `estimated` when it's ours, or `stale` on the rare poll where neither is available and the last good figure is being held. The car's figure is always preferred when it's live; the moment the car stops providing one — including flagging its existing figure as no longer valid — the sensor switches to the projection rather than continuing to display whatever the car last said. The projection is skipped below about 12% battery, where a percentage point of noise swings the result too far to be useful, and the sensor reads unknown rather than guessing. On a car with no target SOC the estimate assumes a charge to full, so it will read optimistically if you unplug early.


`range_added_km` is the electric range the charge added, measured across the session. Note this is *not* the same as the **Added Electric Range** sensor, which exposes the API's own `chrgngAddedElecRng` — a live counter that runs during a session and resets when it ends, and which on the cars observed so far stays at 0 throughout. The range delta here is derived from the electric range reading at each boundary instead.

Note the energy figure is measured **at the battery**, so it will read lower than a wall meter or smart charger, which also pay for charger and cable losses. A charge that delivers less than 0.5% is ignored (that's the small percentage rebound the pack reports after a drive, not a charge), and a session left open more than 48 hours is abandoned rather than reported. A charging-data dropout is never mistaken for the end of a charge — on some cars the charging endpoint goes quiet the moment a session completes.

**Last Trip** sensors are populated when a drive ends (the car powers off). Distance and electric energy come from the car's own cumulative counters (`Mileage Since Last Charge` / `Power Usage Since Last Charge`), diffed between one trip and the next — so they match the car's own measurements and don't depend on exactly when the trip was detected. (For non-charging models, distance falls back to the odometer.) A charge between trips is handled automatically (the counters reset). A trip is one power-on to power-off, so a journey with a stop in the middle counts as two trips.

Because the counters aren't always trustworthy (see below), `Last Trip Distance` and `Last Trip Efficiency` also expose the counter-only and odometer/SOC-only figures **independently**, as attributes, alongside the primary (counter-preferred) value — so you can compare them directly for any trip: `distance_km_counter` / `distance_mi_counter` and `distance_km_odometer` / `distance_mi_odometer` on Last Trip Distance; `energy_kWh_counter` / `efficiency_km_per_kWh_counter` / `efficiency_mi_per_kWh_counter` / `consumption_kWh_per_100km_counter` / `consumption_kWh_per_100mi_counter` and the equivalent `_soc` set on Last Trip Efficiency. The counter figures are shown raw/unfiltered, even on a trip where the primary figure discarded them (see `counter_reset_detected` below) — seeing what the counter actually reported is itself useful.

On some cars, the since-charge counters occasionally reset on their own without an actual charge. If that happens mid-trip, the primary trip figure falls back to the odometer for distance and to the battery-percentage change for energy, and carries a `counter_reset_detected` attribute so it's visible when this happened.

If a drive is never seen live — the car wasn't polled while it was powered (a short trip that fell between polls, or a missed vehicle-start message) — the trip is reconstructed afterwards from the odometer movement once the car is next seen parked. These reconstructed trips carry `retrospective: true` and `timing: approximate` attributes, because the exact start/end times aren't known and several short hops in the same gap may be merged into one. If a trip ever gets stuck "open" (its power-off was missed), it's force-closed automatically so it doesn't block new trips.

The `Last Trip Efficiency` (BEV/PHEV) and `Last Trip Fuel Economy` (ICE/HEV/PHEV) sensors carry the full breakdown of the last drive in their **attributes**: distance, SOC used, energy in kWh, fuel used, duration, and both metric and mi/kWh figures. A `mg_saic_trip_completed` event also fires for each completed trip (with the same fields), so automations and the logbook can keep a full history without any single sensor holding a list.

Notes and limitations:
- **Units are switchable per entity.** The efficiency sensors use Home Assistant's `energy_distance` device class (HA 2025.2+), so you can switch each one between **km/kWh, mi/kWh and kWh/100km** in its settings — the same way Mileage switches between km and miles. On older HA they stay in km/kWh. Fuel economy is reported in **L/100km**; since HA has no fuel-consumption unit conversion, **UK and US mpg are provided in that sensor's attributes**.
- SOC and fuel level are whole-number percentages, so figures for very short trips are coarse.
- Trip *duration* is measured to the poll that detects shutdown, so treat it as approximate.
- Fuel figures in litres / L per 100 km need a per-model tank size; until one is set for a given model, the fuel sensor reports **fuel % used** but not litres, L/100km or mpg.
- If the car is charged or refuelled while parked mid-trip, that trip's electric/fuel figure is omitted and flagged (`charged_during_park` / `refuelled_during_park`) rather than reported wrongly.
- When a value can't be computed yet, the efficiency sensors read **Unknown** rather than Unavailable — e.g. `Efficiency Since Last Charge` while charging or right after a charge (0 km driven since), or `Last Trip Efficiency` for a trip where a charge spanned the drive. The sensor's attributes still show the breakdown so you can see why.

### BINARY SENSORS
 
#### Doors
- Door Front Left / Door Front Right *(named "Driver"/"Passenger" logically, but labelled by physical side — automatically swapped for RHD vs LHD vehicles)*
- Door Rear Left / Door Rear Right *(not present on 2-door models, e.g. MG Cyberster)*
- Bonnet Status
- Boot Status
#### Windows
- Window Front Left / Window Front Right
- Window Rear Left / Window Rear Right *(not present on convertibles with no rear glass, e.g. MG Cyberster)*
- Sunroof Status *(if equipped)*
- Ventilation *(reflects ventilation started from Home Assistant — see [Window Control](controls.md#window-control))*
#### Lights
- Dipped Beam Status
- Main Beam Status
- Side Light Status
#### Other
- Engine Status
- HVAC Status *(the AC/climate running state — see states table for what "on" actually covers)*
- Lock Status *(⚠️ reports on/off, not Locked/Unlocked — see Entity States Reference)*
- Wheel Tyre Monitor Status *(a "problem" sensor — on means a TPMS/tyre fault is reported, not that everything is fine)*
- Charging Gun State *(BEV/PHEV only)*
### EVENTS
 
- **Command Errors** — a single event entity with two possible event types:
  - `command_error` — fired when a remote command (lock, AC, charge, etc.) fails or is rejected by the vehicle.
  - `command_limit_reached` — fired specifically when the vehicle's remote-command allowance has been used up.
  Use this in automations to get notified when a command does not go through. Alongside the original `source` and `error` attributes (still present), each event now also carries readable ones: `action` (what was attempted, e.g. "Setting HVAC mode"), `reason` (a plain-English explanation, e.g. "The car couldn't be reached…"), and `code` (the SAIC return code where applicable, e.g. `4` or `8`).
### DEVICE TRACKER
- Latitude
- Longitude
- Elevation (Altitude)
- HDOP
- Satellites
- Heading *(numeric, `raw_heading` attribute)*
- Heading *(cardinal direction, e.g. N/NE/E/SE/S/SW/W/NW, `heading` attribute)*
### SWITCHES
- Charging Start/Stop
- Battery Heating *(if equipped)*
- Battery Heating Schedule *(if equipped — enables/disables the daily timed battery heating; set the time with the Battery Heating Schedule Time entity)*
- Front Defrost
- Rear Window Defrost
- Heated Seats *(if equipped)* — four independent switches: Front Left, Front Right, Rear Left, Rear Right. Front seat switches apply the level chosen in that seat's Level select (defaulting to Low if the select is Off); rear seats are on/off. See [Heated Seats](controls.md#heated-seats).
- Heated Steering Wheel *(if equipped — enable "Has Steering Wheel Heat" in options)*
- Sunroof *(if equipped — currently non-functional on tested models; see note below)*
- Charging Port Lock *(⚠️ "on" means locked — see Entity States Reference)*
- Holiday Mode *(slows polling to reduce wake-ups / 12V drain while the car is left for long periods — see [Deep sleep & holiday mode](power-management.md#deep-sleep--holiday-mode))*
> **Sunroof note:** the sunroof switch and status are retained but are currently non-functional on tested models (e.g. MGS6 EV), where the SAIC API always reports the sunroof as closed regardless of its real position and no working control command has been identified. The option is off by default. It may be revisited if MG adds sunroof support to the iSmart app.
### BUTTONS
- Trigger Alarm
- Update Vehicle Data
- Open Boot *(momentary — releases the boot/tailgate latch; the SAIC API only supports remote opening, not closing, hence a button rather than a lock/cover)*
- Ventilate Windows / Open Windows / Close Windows *(if "Has Window Control" is enabled in options)* — act on all four door windows together. "Ventilate" cracks them open a few centimetres (mirroring the iSmart app's Ventilation feature); "Open" fully opens; "Close" closes. See [Window Control](controls.md#window-control).
### LOCK
- Lock entity for door lock/unlock
  *(There is no separate lock entity for the boot/tailgate — use the "Open Boot" button instead, since the API only supports releasing the latch remotely, not locking it again.)*
### CLIMATE
- AC Control Climate entity
  * Temperature
  * Fan Speed *(most models)* **or** HVAC mode + Preset *(mode-select models, e.g. MG S9 PHEV — see [Climate Control](controls.md#climate-control))*
  * HVAC mode (Cool / Fan Only / Off, plus Heat on models with a heater — the MG4 Electric, and mode-select models that support it)
### SLIDERS
- Target SOC *(shown only on models where the iSmart app supports it)*
### SELECT
- Charging Current Limit
- Heated Seat Front Left Level / Heated Seat Front Right Level *(if equipped)*
- Scheduled Charging Mode *(BEV/PHEV — Disabled / Until Target SOC / Until Scheduled Time. Selecting a mode sends one command applying the mode together with the Scheduled Charging Start/End times)*
### TIME
- Scheduled Charging Start / Scheduled Charging End *(BEV/PHEV — the charging window, shown as in the iSmart app. Changing these does **not** send a command; the window is applied when you change the Scheduled Charging Mode select, so adjusting both times costs a single command)*
- Battery Heating Schedule Time *(if equipped — the daily start time for scheduled battery heating, shown in your Home Assistant timezone. Changing it while the schedule is enabled pushes the new time to the vehicle immediately; otherwise it is held locally until the Battery Heating Schedule switch is turned on)*
**Note: Actions (Services) can be accessed and activated from the Actions menu under Developer Tools.**
![image](https://github.com/user-attachments/assets/14be0d41-ae65-4738-8bc0-5b0f743c290f)
 
 

---

## 📋 Entity States Reference
 
This section lists every possible state for every status and control entity, so you know exactly what to expect when coding dashboards or automations. Home Assistant binary sensors always report the underlying state as **`on`/`off`** — never as descriptive text like "Locked"/"Unlocked" or "Open"/"Closed" — the description below tells you what `on` and `off` actually *mean* for each one. The friendly text ("Open", "Locked", etc.) is only shown in the Lovelace UI because of the entity's device class; the state itself, e.g. as read via `states('binary_sensor...')` in a template, is always `on` or `off`.
 
### Binary sensors
 
| Entity | Device class | `on` means | `off` means |
|---|---|---|---|
| Bonnet Status | door | Open | Closed |
| Boot Status | door | Open | Closed |
| Door Front Left / Front Right | door | Open | Closed |
| Door Rear Left / Rear Right | door | Open | Closed |
| Window Front Left / Front Right | window | Open | Closed |
| Window Rear Left / Rear Right | window | Open | Closed |
| Sunroof Status | window | Open | Closed |
| Dipped Beam Status | light | Light on | Light off |
| Main Beam Status | light | Light on | Light off |
| Side Light Status | light | Light on | Light off |
| Engine Status | power | Engine running | Engine not running |
| HVAC Status | running | Climate control active (cooling, fan-only, defrost, or heat) | Climate control fully off |
| **Lock Status** | lock | **Unlocked** | **Locked** |
| Wheel Tyre Monitor Status | problem | Fault/problem reported (e.g. low pressure or TPMS fault) | No fault reported |
| Charging Gun State | plug | Charging gun/cable plugged in | Unplugged |
 
> **This is the entity from the issue report:** `binary_sensor` device class **`lock`** is the one HA device class where `on` does *not* mean "active/true" in the usual sense — by HA convention, `on` = unlocked (the "open" state) and `off` = locked. It is easy to assume `on` = Locked, but it's the opposite.
 
### Lock entity
 
| Entity | States |
|---|---|
| Lock | `locked` / `unlocked` (standard HA lock entity — reported as plain text, not on/off) |
 
### Switches
 
| Entity | `on` means | `off` means |
|---|---|---|
| Charging | Actively charging (AC or DC), or V2X discharging in progress | Not charging (includes "Scheduled Charging" status — the switch only reflects active current flow) |
| Battery Heating | Battery heating active | Battery heating inactive |
| Battery Heating Schedule | A daily timed battery heating schedule is enabled on the vehicle | No schedule enabled |
| Front Defrost | Front defrost running | Front defrost off |
| Rear Window Defrost | Rear window heater on | Rear window heater off |
| Heated Seat Front Left / Front Right | Seat heat level 1 or above (Low/Medium/High) | Seat heat level 0 (Off) |
| Sunroof | Sunroof open | Sunroof closed |
| **Charging Port Lock** | **Charging port locked** | **Charging port unlocked** |
 
> Note that for **Charging Port Lock**, `on` = locked — the opposite convention to the `lock`-device-class binary sensor above. This is because it's a `switch` entity (where `on` simply reflects "the lock control is engaged"), not a `binary_sensor` with a `lock` device class.
 
### Enumerated sensors (text states)
 
| Entity | Possible states |
|---|---|
| Power Mode | `Off`, `Accessory`, `On`, `Start` |
| Charging Status | `Unplugged`, `Charging (AC)`, `Charging Finished`, `Charging`, `Fault Charging`, `Connecting`, `Unrecognized Connection`, `Plugged In`, `Charging Stopped`, `Scheduled Charging`, `Charging (DC)`, `Super Offboard Charging`, `V2X Discharging` |
| Battery Heating Status | `Off`, `On`, `Error` |
| Front Left/Right Heated Seat Level | `Off`, `Low`, `Medium`, `High` |
| Steering Wheel Heat | `Off`, `On` |
| Reachability | `awake`, `likely_asleep`, `unreachable` |
| Charging Current Limit *(sensor)* | `0A (Ignore)`, `6A`, `8A`, `16A`, `Max` |
| Target SOC *(sensor)* | `40`, `50`, `60`, `70`, `80`, `90`, `100` (%) |
 
> The API reports two separate raw codes (`3` and `12`) that both map to the plain `Charging` text for the Charging Status sensor. If you need to tell them apart in an automation, use the numeric `bmsChrgSts` value via the debug log rather than the sensor state.
 
### Select entities (settable, same options as their read-only sensor counterparts)
 
| Entity | Options |
|---|---|
| Charging Current Limit | `0A (Ignore)`, `6A`, `8A`, `16A`, `Max` |
| Heated Seat Front Left/Right Level | `Off`, `Low`, `Medium`, `High` |
 
### Climate entity
 
| Attribute | Possible values |
|---|---|
| HVAC mode | `Cool`, `Fan Only`, `Off` |
| Fan mode | `Low`, `Medium`, `High` |
 
### Event entity
 
| Event type | Fired when | Event data |
|---|---|---|
| `command_error` | Any remote command fails or is rejected | `source` (which command), `error` (the error message) |
| `command_limit_reached` | The vehicle's remote command allowance is used up | `source`, `message` |
 
 

---

## Vehicle Profiles
 
The integration includes built-in profiles for specific MG/SAIC models that correct known inaccuracies in the API data:
 
| Series | Model | Notes |
|---|---|---|
| `EH32` | MG4 Electric | Temperature range and fan speed values confirmed; PTC resistive **Heat** mode supported (#173) |
| `AH4EM` | MG4 EV URBAN | Mode-select climate scheme (owner-confirmed, #243); this variant has no heat mode — see [Climate Control](controls.md#climate-control) |
| `MIS3E` | MGS6 EV (Long Range / Dual Motor) | Battery capacity 74.3 kWh; inverted temperature index; model year override (API reports 2024, corrected to 2025) |
| `MZS3E` | MGS5 EV | Mode-select climate scheme mirroring the MGS6 (status code 2 = cool, #277); battery capacity 62.1 kWh usable (64 kWh gross pack EU169A64S, #301); temperature index inherited from the MGS6 as best-effort |
| `EC32` | MG Cyberster | 2-door BEV roadster; no rear doors/windows; unreliable live electric range field (falls back to estimated range) |
| `IS31P` | MG S9 PHEV (2025) | Climate status/fan speed mappings confirmed by physical testing |
| `AS33P` | MG HS PHEV (Super Hybrid 2025/2026) | Battery capacity 24.7 kWh; Target SOC and Charging Current Limit not supported by iSmart; electric range uses live SOC-tracking field; energy values corrected for ~3x API over-reporting |
| `S12L` | IM6 (IM by MG Motor) | Battery capacity 96.5 kWh usable (100 kWh nominal NMC) — replaces the API's bogus `totalBatteryCapacity=725` (→ 72.5 kWh) (#53). In the UK/EU the IM6 is sold on the 100 kWh pack only, so this covers every variant; a 75 kWh LFP Premium exists in some other markets and would need 73.5 kWh and a split if it reports the same series |
| `P12L` | IM5 (IM by MG Motor) | Mode-select climate scheme mirroring the MGS6 (status code 2 = cool, confirmed, #326) — fixes the car showing as "Fan only" while genuinely cooling. Fan-only/heat/defrost/max-cool values are still unconfirmed best-effort, pending a debug log with the AC confirmed on. Battery capacity set to **96.5 kWh usable** for the confirmed Long Range/Performance pack (100 kWh nominal NCM, #326), replacing the API's bogus `totalBatteryCapacity=725` (→ 72.5 kWh). ⚠️ The IM5 **Standard Range** (75 kWh LFP, 73.5 kWh usable) reports the same series code and will read too high — set a [battery capacity override](#battery-capacity-override) to 73.5 and please comment on #326 so the variants can be split |
| `ZP22 EU` | MG3 Hybrid+ | Self-charging full hybrid (1.83 kWh HV battery, no charge port); reports as vehicle type HEV. State of Charge is now populated from `basicVehicleStatus.extendedData1`, since this vehicle type has no charging-endpoint data to read (#318) |
 
Models not listed above use safe default values and should work normally. If you notice incorrect sensor readings for your model, please open an issue with your vehicle's debug logs.
 
 

---

### Battery capacity override

Some MG models share one series code across several battery sizes (the MG4, for example, ships with 51, 64, and 77 kWh packs), and the API's reported capacity is unreliable on a few cars — so the value we use isn't always right for your exact variant.

The **Usable battery capacity override (kWh)** option (under **Configure**) lets you set your car's usable capacity yourself. When set, it takes priority over both our built-in per-model value and the API-reported value, and it becomes the figure used everywhere capacity matters: the **Total Battery Capacity** sensor and the electric energy/efficiency calculations (including Last Trip figures on models that fall back to a battery-percentage estimate). Enter the **usable** capacity for your variant; leave it blank to go back to the automatic value. Saving the option takes effect immediately — no restart or reload needed.

The Total Battery Capacity sensor carries a `capacity_source` attribute (`user_override`, `profile`, or `api`) so you can see — and template off — exactly where the displayed figure came from. The same resolved figure feeds every energy calculation derived from capacity, so the displayed pack size and the sensors derived from it can't disagree.

Where a car reports a capacity that can't be trusted, none is used: the `totalBatteryCapacity=725` placeholder (→ 72.5 kWh) is rejected outright, as is anything outside 5–200 kWh. On such a car with no profile figure and no override, Total Battery Capacity reads blank and `capacity_source` is absent, rather than showing a number the car invented and deriving charge and efficiency figures from it. Setting a [battery capacity override](#battery-capacity-override) is the fix if you know your real capacity.
