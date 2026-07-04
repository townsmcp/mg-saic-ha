[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/townsmcp/mg-saic-ha/blob/main/LICENSE)
![GitHub Release (latest SemVer including pre-releases)](https://img.shields.io/github/v/release/townsmcp/mg-saic-ha?include_prereleases)
![GitHub Downloads (all assets, latest release)](https://img.shields.io/github/downloads/townsmcp/mg-saic-ha/latest/total)
[![GitHub stars](https://img.shields.io/github/stars/townsmcp/mg-saic-ha?style=flat)](https://github.com/townsmcp/mg-saic-ha/stargazers)

[![hacs_badge](https://img.shields.io/badge/HACS-Default-green.svg)](https://github.com/hacs/default)
[![HACS Action](https://github.com/townsmcp/mg-saic-ha/actions/workflows/validate.yaml/badge.svg)](https://github.com/townsmcp/mg-saic-ha/actions/workflows/validate.yaml)
[![Hassfest](https://github.com/townsmcp/mg-saic-ha/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/townsmcp/mg-saic-ha/actions/workflows/hassfest.yaml)
[![Integration Usage](https://img.shields.io/badge/dynamic/json?color=41BDF5&logo=home-assistant&label=integration%20usage&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.mg_saic.total)](https://analytics.home-assistant.io/)

![Logo](brand/icon.png)


</br></br>
# MG/SAIC CUSTOM INTEGRATION

<a href="https://buymeacoffee.com/Townsmcp" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/default-orange.png" alt="Buy Me A Coffee" height="41" width="174"></a>
 
**Important Notes:** 
- **Using this integration causes the MG/SAIC mobile app to shut down if the same account is used, as per API requirements.**
- **To avoid issues, make sure to setup a Secondary Account on iSmart App.**

**Requirements:**
- Home Assistant 2024.06 or later.
- Confirmed compatible with Python 3.14, the runtime used by current Home Assistant core releases (2026.3+). No action needed on your part this is handled automatically by Home Assistant on supported installation methods.
## INSTALLATION
 
### HACS (Home Assistant Community Store)
 
1. Ensure that HACS is installed.
2. Go to HACS
3. Search for "MG SAIC" and download the repository.
4. Restart Home Assistant.
### Manual Installation
 
1. Download the latest release from the [MG SAIC Custom Integration GitHub repository](https://github.com/townsmcp/mg-saic-ha/releases).
2. Unzip the release and copy the `mg_saic` directory to `custom_components` in your Home Assistant configuration directory.
3. Restart Home Assistant.
## CONFIGURATION
 
To add the integration to your local Home Assistant, click here:
 
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=townsmcp&repository=mg-saic-ha&category=integration)
 
Install the integration, restart Home Assistant and then add the integration, either:
 
[<img src="https://github.com/user-attachments/assets/36459daa-a780-448a-82a5-19ee07ccd3f6">](https://my.home-assistant.io/redirect/config_flow_start?domain=mg_saic)
 
Or manually by:
 
1. Go to Configuration -> Integrations.
2. Click on the "+ Add Integration" button.
3. Search for "MG SAIC" and follow the instructions to set up the integration.
4. Select your type of account (email or phone), enter the details and select your region (EU, China, Australia, Brazil, Israel, Turkey, India, Thailand, Rest of World). If your country runs on separate SAIC infrastructure that is not covered by a built-in region, choose **Custom** and enter the API base URI, region code, and tenant ID for your market (known endpoints are collected in the [SAIC iSmart API community URI database](https://github.com/orgs/SAIC-iSmart-API/discussions/8)).
5. Once connected to the API, a list of available VINs associated with your account will be shown. Select the vehicle that you want to integrate and finish the process.
You may add additional vehicles by following the same steps as above.
 
### Multiple Vehicles
 
If you have more than one MG/SAIC vehicle, you can add each one as a separate integration entry. Vehicles on the **same SAIC account** are fully supported — the integration uses a single shared API session per account, so adding a second vehicle does not interfere with the first.
 
If your vehicles are on different SAIC accounts, add each account separately in the same way.
 
 
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
- State of Charge (SOC)
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
- Estimated Range After Charging
- Target SOC *(read-only mirror of the Target SOC slider — shown only on models where the iSmart app supports it)*
- Charging Duration
- Remaining Charging Time
- Added Electric Range
- Power Usage Since Last Charge
- Mileage Since Last Charge
- Total Battery Capacity *(kWh; corrected for models where the API reports an inaccurate value)*
- Battery Heating Status *(if equipped)*
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
  Use this in automations to get notified when a command does not go through.
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
- Heated Seat Front Left / Heated Seat Front Right *(if equipped; two independent switches, one per seat)*
- Sunroof *(if equipped)*
- Charging Port Lock *(⚠️ "on" means locked — see Entity States Reference)*
### BUTTONS
- Trigger Alarm
- Update Vehicle Data
- Open Boot *(momentary — releases the boot/tailgate latch; the SAIC API only supports remote opening, not closing, hence a button rather than a lock/cover)*
### LOCK
- Lock entity for door lock/unlock
  *(There is no separate lock entity for the boot/tailgate — use the "Open Boot" button instead, since the API only supports releasing the latch remotely, not locking it again.)*
### CLIMATE
- AC Control Climate entity
  * Temperature
  * Fan Speed *(most models)* **or** HVAC mode + Preset *(mode-select models, e.g. MG S9 PHEV — see [Climate Control](#climate-control))*
  * HVAC mode (Cool / Fan Only / Off, plus Heat on mode-select models)
### SLIDERS
- Target SOC *(shown only on models where the iSmart app supports it)*
### SELECT
- Charging Current Limit
- Heated Seat Front Left Level / Heated Seat Front Right Level *(if equipped)*
### TIME
- Battery Heating Schedule Time *(if equipped — the daily start time for scheduled battery heating, shown in your Home Assistant timezone. Changing it while the schedule is enabled pushes the new time to the vehicle immediately; otherwise it is held locally until the Battery Heating Schedule switch is turned on)*
**Note: Actions (Services) can be accessed and activated from the Actions menu under Developer Tools.**
![image](https://github.com/user-attachments/assets/14be0d41-ae65-4738-8bc0-5b0f743c290f)
 
 
## Climate Control
 
The MG SAIC integration exposes a climate entity for remote control of the vehicle's air conditioning. Because SAIC limits remote commands to **3 per cycle between starting the car with a key**, the integration is designed to use commands as efficiently as possible.
 
### Two control schemes
 
Not all MG models expose climate control the same way, so the integration uses one of two schemes depending on your vehicle:
 
- **Fan-speed models (most cars):** a Low / Medium / High fan slider plus `Cool` / `Fan Only` / `Off` HVAC modes. This is the default and covers the MG4, MGS5, Cyberster, HS PHEV, and any model not specifically profiled.
- **Mode-select models (e.g. MG S9 PHEV):** on some cars the SAIC API's "fan speed" value is not a fan speed at all — it is a fixed climate *mode* selector, and the car chooses its own fan speed. On these models a Low/Med/High slider is misleading, so instead the integration exposes HVAC modes and presets that map to the car's actual modes (see below). The correct scheme is selected automatically based on your vehicle.
 
### How commands are used
 
The SAIC API counts each instruction sent to the car as one command. To avoid wasting your allowance, only explicit HVAC mode or preset changes send a command. Adjusting fan speed or temperature on their own does not.
 
**Uses a command:**
- Turning the AC on (HVAC mode set to `Cool`, `Fan Only`, or `Heat`)
- Turning the AC off (HVAC mode set to `Off`)
- Switching between HVAC modes
- Selecting a preset (`Max Cool` / `Defrost`) on mode-select models
**Does NOT use a command:**
- Changing fan speed (`Low`, `Medium`, `High`) on fan-speed models
- Changing target temperature
### Recommended usage
 
Set your preferred temperature (and fan speed, on fan-speed models) **first**, then turn the AC on. The command sent to the car will include whatever settings you have already applied in HA. A complete remote pre-conditioning session uses exactly **2 commands** — one to turn on, one to turn off — leaving one spare for a lock or unlock action.
 
If you want to change settings while the AC is already running, update the values in HA first, then turn the AC off and back on. This applies your new settings using 2 commands.
 
### Fan-speed models
 
**Fan speeds**
 
| HA setting | Behaviour |
|---|---|
| Low | Gentle airflow |
| Medium | Default when turning on |
| High | Maximum normal fan speed |
 
> **Note:** Fan speed values used internally vary by vehicle model. The integration automatically selects the correct values for your car based on its series. The Front Defrost command uses a separate API speed value and is never accidentally triggered by fan speed changes.
 
**HVAC modes**
 
| Mode | Behaviour |
|---|---|
| `Cool` | Runs the compressor with your chosen temperature and fan speed |
| `Fan Only` | Runs the fan without the compressor (blowing only) |
| `Off` | Stops all climate activity |
 
### Mode-select models (e.g. MG S9 PHEV)
 
On these models there is **no fan-speed slider** — the car manages its own fan. Control is via HVAC modes and presets instead:
 
| Mode / Preset | Behaviour |
|---|---|
| HVAC `Cool` | AC on, automatic fan, follows your target temperature |
| HVAC `Heat` | Heating |
| HVAC `Fan Only` | Fan without the compressor |
| HVAC `Off` | Stops all climate activity |
| Preset `Max Cool` | Strong fixed-fan fast cool-down |
| Preset `Defrost` | Windscreen / upper-vent defrost |
 
> **⚠️ Note for MG S9 PHEV owners:** from **1.1.2** this model uses the mode-select scheme. The previous Low/Med/High fan control has been replaced by the HVAC modes and presets above. If you have automations or scripts that called `climate.set_fan_mode` on your S9 PHEV, update them to use `climate.set_hvac_mode` (`cool` / `heat` / `fan_only`) or `climate.set_preset_mode` (`Max Cool` / `Defrost`) instead.
 
 
## Event-Driven Updates
 
The integration polls the SAIC alarm message queue once per minute per account and automatically triggers an immediate data refresh when it detects:
 
- **Engine start** — data refreshes as soon as the car is driven away
- **Vehicle shutdown** — data refreshes after the car is turned off
- **Charging plug-in** — data refreshes when charging begins
This means you can set a long polling interval (e.g. 30 minutes or more) for idle/parked state and still get near-real-time updates when the car is active.
 
> **Multiple vehicles on one account:** The integration uses a single API session and a single message poll loop per SAIC account, regardless of how many vehicles are registered under it. This prevents session conflicts and duplicate API calls.
 
 
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
 
 
## Vehicle Profiles
 
The integration includes built-in profiles for specific MG/SAIC models that correct known inaccuracies in the API data:
 
| Series | Model | Notes |
|---|---|---|
| `EH32` | MG4 Electric | Temperature range and fan speed values confirmed |
| `MIS3E` | MGS6 EV (Long Range / Dual Motor) | Battery capacity 74.3 kWh; inverted temperature index; model year override (API reports 2024, corrected to 2025) |
| `EC32` | MG Cyberster | 2-door BEV roadster; no rear doors/windows; unreliable live electric range field (falls back to estimated range) |
| `IS31P` | MG S9 PHEV (2025) | Climate status/fan speed mappings confirmed by physical testing |
| `AS33P` | MG HS PHEV (Super Hybrid 2025/2026) | Battery capacity 24.7 kWh; Target SOC and Charging Current Limit not supported by iSmart; electric range uses live SOC-tracking field; energy values corrected for ~3x API over-reporting |
 
Models not listed above use safe default values and should work normally. If you notice incorrect sensor readings for your model, please open an issue with your vehicle's debug logs.
 
 
## 💡 Troubleshooting & FAQ
 
* **"Invalid Credentials" or Connection Timeouts:** Ensure you are choosing the correct region matching your mobile app setup.
* **"The account is not registered" (code 1000036):** Your account exists on a different regional SAIC backend than the one selected. Pick the region matching the country where the account was created — for markets without a built-in preset, use the **Custom** region option to enter your market's endpoint details.
* **Entities showing as 'Unavailable':** The integration respects API rate limits to prevent account lockouts. If an entity is temporarily unavailable, wait for the next scheduled update or use the `button.update_vehicle_data` entity to force a refresh.
* **My App keeps logging me out:** As noted above, ensure your Home Assistant integration uses a **Secondary Account**, not your primary mobile application credentials.
* **Target SOC entity is missing:** Some vehicle models (e.g. MG HS PHEV) do not support remote Target SOC setting via the iSmart API. The entity is intentionally not created for these models.
* **Electric Range shows an unexpected value:** For some PHEV models the live electric range field is not populated by the API. The integration falls back to the estimated-range-after-full-charge figure from the charging management data.
* **Two cars on the same account:** Fully supported. Both vehicles share a single API session so neither interferes with the other.
* **Instant Power sensor shows a stale value after HA restart:** Home Assistant restores entity states from its database on startup. The value will update to `0 kW` on the first successful poll (usually within 30 seconds) if the car is not driving.
* **"Lock Status" binary sensor shows on/off, not Locked/Unlocked:** This is expected HA behaviour for the `lock` device class — see the [Entity States Reference](#entity-states-reference) above for exactly what `on` and `off` mean for every status/control entity in this integration.
## How to enable logging
 
* Add the following lines to `configuration.yaml` (or your sub `logger.yaml` file if you have broken down `configuraiton.yaml` into smaller files)
```
  logger:
  default: warning
  
  logs:
    custom_components.mg_saic: debug
```
* Restart Home Assistant
* Go to System -> Logs
* Search for `mg_saic`
* Click the 3 vertical dots
* Choose `Show full logs`
## Contributing
 
Contributions are welcome! If you have any suggestions or find any issues, please open an [issue](https://github.com/townsmcp/mg-saic-ha/issues) or a [pull request](https://github.com/townsmcp/mg-saic-ha/pulls).
 
## Credits
 
This integration was made possible thanks to the [saic-ismart-client-ng](https://github.com/SAIC-iSmart-API/saic-python-client-ng) repository and its developers/contributors.
 
Special thanks to ad-ha for creating the original integration and for the hard work put into building and maintaining it in its previous stages. This repository continues that work.
 
## License
 
This project is licensed under the MIT License. See the LICENSE file for details.

## Disclaimer
THIS PROJECT IS NOT IN ANY WAY ASSOCIATED WITH OR RELATED TO THE SAIC MOTOR OR ANY OF ITS SUBSIDIARIES. The information here and online is for educational and resource purposes only and therefore the developers do not endorse or condone any inappropriate use of it, and take no legal responsibility for the functionality or security of your devices.
