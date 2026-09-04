# Controlling Your Car

Climate, windows, heated seats, and how the integration reacts to events from the car in real time.

← [Back to the main README](../README.md)

---

## Climate Control
 
The MG SAIC integration exposes a climate entity for remote control of the vehicle's air conditioning. Because SAIC limits remote commands to **3 per cycle between starting the car with a key**, the integration is designed to use commands as efficiently as possible.
 
### Two control schemes
 
Not all MG models expose climate control the same way, so the integration uses one of the following schemes depending on your vehicle:
 
- **Fan-speed models (most cars):** a Low / Medium / High fan slider plus `Cool` / `Fan Only` / `Off` HVAC modes (and `Heat` on models with a confirmed heater, e.g. the MG4 Electric). This is the default and covers the standard MG4, Cyberster, and any model not specifically profiled.
- **Mode-select models (e.g. MGS5 EV, MGS6 EV, MG S9 PHEV, MG4 EV URBAN):** on some cars the SAIC API's "fan speed" value is not a fan speed at all — it is a fixed climate *mode* selector, and the car chooses its own fan speed. On these models a Low/Med/High slider is misleading, so instead the integration exposes HVAC modes and presets that map to the car's actual modes (see below). The correct scheme is selected automatically based on your vehicle. Available modes and presets vary by model — a car is only offered `Heat` or a `Defrost` preset if it actually supports them (the MG4 EV URBAN, for example, has no heat mode).
- **No-remote-fan models (e.g. MG HS PHEV / Super Hybrid):** this car has no remote fan control — the app's AC page has no fan slider and always runs the fan on AUTO — so the integration hides the fan slider and offers `Cool` / `Fan Only` / `Off` (temperature range 16–30 °C). Here `Fan Only` triggers the car's separate **AC Airflow** cabin-ventilation mode (fresh-air blower, no cooling), matching the app's dedicated AC Airflow button. Like the app, this requires the **AC to be off first**: if you select `Fan Only` while the AC is running, the integration doesn't send the command (which the car would reject anyway) — it leaves the AC on and raises a notification telling you to turn the AC off first, so none of your limited remote commands are wasted.
- **Simple-AC models (e.g. MG3 Hybrid):** a few cars only act on the basic AC command and ignore everything else, so they get a stripped-back `Cool` / `Heat` / `Off` climate entity (Cool = coldest, Heat = warmest) with no fan slider (see below).
 
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
 
### Voice and automation control

Home Assistant's voice assistants, the MCP bridge, and some automations can only set a limited range of entity types — in particular, they **cannot set a climate entity's mode**, so they can't turn the AC on from the climate widget alone. To make remote AC fully controllable that way, the integration also exposes the same climate state as plain, assistant-friendly entities that stay in sync with the climate entity (change one, the others follow):

- **Air Conditioning** *(switch)* — turns the AC on/off. This is the one an assistant can toggle by voice. Turning it **on runs the AC at whatever temperature is currently set** — it does not force a hot/cold extreme.
- **Climate Target Temperature** *(number)* — the setpoint as a plain number. Display-only, exactly like the climate slider: changing it never sends a command; it rides along with the next AC command.
- **Climate Mode** *(select)* — `Off` / `Cool` / `Fan Only` (and `Heat` where supported). Not shown on simple-AC models that only have `Cool`.
- **Climate Mode** *(sensor)* — a detailed read-back of the mode the car is actually running (`off` / `cool` / `fan_only` / `heat` / `defrost`), so an assistant or automation can confirm the result — more than the simple on/off HVAC Status. It also reports **On (under car control)** when the car's climate is running under **local** control (i.e. you're driving with it on from the dashboard) rather than from a remote command — see below.

All of these mirror the climate entity, so you can mix and match: set the temperature with the number, turn it on with the switch, and read the result from the sensor. The 3-command limit still applies — only turning the AC on/off or changing mode sends a command.

> **Setting a specific temperature:** the AC on/off switch (and the climate power button) run the AC at the **currently set temperature**. The `Cool` and `Heat` *modes* carry their own temperature — on simple-AC models like the MG3, `Cool` goes to the coldest setting and `Heat` to the warmest. So if you want a temperature **between** the extremes, **set the temperature first, then turn the AC on with the switch** — that pushes your chosen value. Using the `Cool`/`Heat` mode instead will move to the min/max end.

> **Climate under local control (while driving):** if you're driving with the climate on from the car's own dashboard, the car reports it as running under *local* control. The remote controls — the climate entity, the A/C switch and the Climate Mode select — represent what Home Assistant is *commanding*, so they stay **Off** in this case (there's no active remote command). Only the **Climate Mode sensor** reflects the physical reality, showing **On (under car control)**. This is also why the car's own dashboard temperature isn't shown while driving: SAIC doesn't report the car's live setpoint, so Home Assistant only ever shows the temperature *you've* set.

### Front defrost behaviour
 
Front defrost (the standalone **Front Defrost switch**, and the **Defrost preset** on mode-select models) mirrors the iSmart app exactly, based on decrypted app traffic and a live control test:
 
- **Always runs at 22°C**, regardless of the temperature set on the climate slider — this matches what the app sends. Your own temperature setting is **not** changed by starting defrost, and defrost auto-cancels after roughly 10 minutes, so your preference is intact for the next AC session.
- **Cannot start while the AC is already running.** The vehicle rejects this (the iSmart app blocks it too, asking you to turn AC Auto off first). Rather than waste one of your limited daily remote commands on a request the car would ignore, the integration does not send it — you get a **persistent notification** explaining the AC must be turned off first, plus a command-error event in the Logbook.
 
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
| `Heat` | Runs the resistive (PTC) heater — **shown only on models with a heater, e.g. the MG4 Electric.** Heats to the top of the temperature range (this is how the car's own app drives it) |
| `Fan Only` | Runs the fan without the compressor (blowing only) |
| `Off` | Stops all climate activity |

> **Note:** `Heat` appears only on models where resistive heating has been confirmed. On the MG4 Electric it engages the PTC heater; because of how the car works, heating always runs at the top of the temperature range rather than a chosen setpoint.
 
### Simple-AC models (MG3 Hybrid)

Some cars only accept the simplest remote-AC command and silently ignore the fuller one that carries a fan speed. The **MG3 Hybrid** is the first such model: it has a single command that just drives the cabin to a target temperature, heating or cooling as needed — there's no separate fan speed or mode. The integration therefore shows a stripped-back climate entity with two one-tap ends of the range:

| Mode | Behaviour |
|---|---|
| `Cool` | Air conditioning at the **coldest** setting (drops the setpoint to the minimum) |
| `Heat` | Air conditioning at the **warmest** setting (raises the setpoint to the maximum) |
| `Off` | Turns the air conditioning off |

Both `Cool` and `Heat` use the same underlying command — the only difference is the temperature they aim for — and each moves the temperature slider to the matching end automatically. There is **no fan-speed slider, no `Fan Only` mode, and no Front Defrost** on this model, because the car doesn't act on the commands those need. The MG3 also only reports its **driver window**, so only that one window sensor is created.

### Mode-select models (e.g. MG S9 PHEV, MG4 EV URBAN)
 
On these models there is **no fan-speed slider** — the car manages its own fan. Control is via HVAC modes and presets instead:
 
| Mode / Preset | Behaviour |
|---|---|
| HVAC `Cool` | AC on, automatic fan, follows your target temperature |
| HVAC `Heat` | Heating |
| HVAC `Fan Only` | Fan without the compressor |
| HVAC `Off` | Stops all climate activity |
| Preset `Max Cool` | Fast cool-down using the strongest cooling the car has; on models like the MG4 EV URBAN it also drops the temperature to the lowest setting in a single tap |
| Preset `Defrost` | Windscreen / upper-vent defrost |
 
> **Note:** not every mode-select car offers all of these. `Heat` and the `Defrost` preset are only shown on models that actually support them. The **MG4 EV URBAN**, for example, has no heat mode, so it shows only `Cool` / `Fan Only` / `Off` plus the `Max Cool` and `Defrost` presets — the Defrost preset gives URBAN owners a front-defrost control the iSmart app itself doesn't provide.
 
> **⚠️ Note for MG S9 PHEV owners:** from **1.1.2** this model uses the mode-select scheme. The previous Low/Med/High fan control has been replaced by the HVAC modes and presets above. If you have automations or scripts that called `climate.set_fan_mode` on your S9 PHEV, update them to use `climate.set_hvac_mode` (`cool` / `heat` / `fan_only`) or `climate.set_preset_mode` (`Max Cool` / `Defrost`) instead.
 
 

---

## Window Control
 
If your vehicle supports it, enable **Has Window Control** in the integration options to add three window buttons:
 
| Button | Action |
|---|---|
| Ventilate Windows | Cracks all four door windows open a few centimetres (mirrors the iSmart app's "Ventilation") |
| Open Windows | Fully opens all four door windows |
| Close Windows | Closes all four door windows |
 
Notes:
- The commands act on **all four door windows together** — the SAIC API does not support controlling a single window remotely.
- The window status sensors are open/closed only; the car does not report "ventilated" as distinct from "fully open", so a ventilated window shows as open.
- These commands are confirmed on the MGS6 EV. On other models the command is assumed to be the same — if it behaves differently on your car, please open an issue so we can add a per-model mapping.
### Ventilation binary sensor
 
A **Ventilation** binary sensor indicates whether the car is currently ventilating. The vehicle exposes no reliable ventilation status field (the remote-climate status reports the A/C, not ventilation, and window status can't distinguish "ventilated" from "fully open"), so this sensor uses **optimistic tracking of commands sent from Home Assistant**:
 
- It turns **on** when you press **Ventilate Windows**.
- It turns **off** when you press **Open Windows** or **Close Windows**, or once the windows report closed again (ventilation ended). A short guard keeps it on during the brief delay between pressing ventilate and the car actioning it.
> **Known limitation:** if you start ventilation from the **iSmart app** rather than Home Assistant, this sensor cannot detect it and will stay off (your window sensors will still correctly show the windows open). When ventilation is controlled through Home Assistant, the sensor is accurate.

---

## Heated Seats
 
When **Has Heated Seats** is enabled, the integration exposes:
 
- **Front Left / Front Right:** a Level select (Off / Low / Medium / High) **plus** an on/off switch.
- **Rear Left / Rear Right:** an on/off switch only.
**How front seats work:** the Level select only stores your chosen level — it does **not** send a command by itself. The level is applied when you turn that seat's switch on. If the switch is turned on while the select still says "Off", it defaults to **Low**. This mirrors the climate entity's "set the value, then activate" pattern and avoids spending a remote command every time you nudge the dropdown.
 
Each seat is sent as its own independent command, so changing one seat never disturbs another.
 
> **Note:** rear-seat heat status may not reliably report back from the car — on tested models the SAIC API does not always reflect the rear seats as "on" after a command, even though the command is sent. The switch still works; only the status read-back is affected.
 
 

---

## Event-Driven Updates
 
The integration polls the SAIC alarm message queue once per minute per account and automatically triggers an immediate data refresh when it detects:
 
- **Engine start** — data refreshes as soon as the car is driven away
- **Vehicle shutdown** — data refreshes after the car is turned off
- **Charging plug-in** — data refreshes when charging begins
This means you can set a long polling interval (e.g. 30 minutes or more) for idle/parked state and still get near-real-time updates when the car is active.
 
> **Multiple vehicles on one account:** The integration uses a single API session and a single message poll loop per SAIC account, regardless of how many vehicles are registered under it. This prevents session conflicts and duplicate API calls.
 
 
