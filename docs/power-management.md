# Deep Sleep, Holiday Mode & Update Behaviour

Why the car sometimes goes quiet, and the options that control how the integration polls it.

← [Back to the main README](../README.md)

---

## Deep sleep & holiday mode
 
### Deep sleep (why the car sometimes goes quiet)
 
After a car has been idle for a long time (often around a day), its telematics module goes into a deep sleep to save the 12V battery. While asleep it can still answer *cached* status requests, but it cannot service live commands — these fail with the SAIC "can't reach the car" error (return code 4). This is normal vehicle behaviour, not an integration fault.
 
On **PHEVs** this matters more than on BEVs: a PHEV only recharges its 12V battery while the car is running (driving or, in some cases, charging the main battery), whereas a BEV tops the 12V up from the main traction battery as needed. So a PHEV left parked for a long time is more likely to drift into deep sleep, and — as owners have observed — once it's asleep, often only actually **driving** the car reliably wakes it again.
 
### Every poll wakes the car

Each poll asks the car itself for a live reading, so the car has to wake up to answer. You can see it on a monitor fitted to the 12V battery: on an MGS6, the 13 polls of one night (one every 30 minutes) lined up with 13 dips of 0.3–0.5 V, each lasting a few minutes, with nothing in between. An MG4 owner measured the same thing, hourly on a 60-minute interval (#407).

That is why the idle interval matters, and why a longer one is the first thing to try if your 12V battery runs low:

- **A long idle interval costs you no trips.** The check that spots the car being started only talks to SAIC's server, not to the car, and it runs every minute whatever your idle interval is.
- **What you give up** is anything that changes while the car is parked and off, such as plugging in without having driven first. That shows at the next poll.
- **To catch a charge without polling more often**, refresh from your charger's own status — see [Catching the start of a charge](#catching-the-start-of-a-charge).
- **Holiday Mode** (below) is for when the car is left for days.

**Reading SAIC's stored status** *(from 1.3.0-beta14)*. SAIC keeps a copy of the car's last status on its server, which the iSmart app reads when it opens. The `mg_saic.read_cached_status` action reads the same copy: it returns a cut-down status (lock, windows, tyre pressures, battery %, range, odometer), the time it was taken and whether SAIC considers the car online. It does not ask the car for anything, refresh the integration or change any entity. On an MGS6 it left the car asleep, and the car updated the copy by itself when plugged in and when a charge started. [Quiet charging](#quiet-charging) reads it overnight instead of polling the car. How to run it and what the results mean: [Reading SAIC's stored status](troubleshooting.md#reading-saics-stored-status-read_cached_status).

### Reachability sensor
 
The **Reachability** sensor surfaces this at a glance, so you can tell when data may be stale rather than wondering why things have gone quiet. *(From 1.4.0-beta13 it's in the device's **Diagnostic** section, with Data Freshness and Data Refresh. Same entity ID, name and states, so nothing that uses it needs changing.)* It has three states:
 
- **awake** — the car is powered on, or has reported activity recently
- **likely_asleep** — the car has reported no activity for longer than the *data staleness threshold* (default 12 hours, configurable); its data may be out of date
- **unreachable** — the car isn't answering: SAIC returned "can't reach the car" (return code 4) on two polls in a row, or a live command failed with it. It clears as soon as the car answers again.

**While the car is unreachable**, each scheduled poll makes a single attempt instead of retrying five times, and doesn't ask for charging data. A sleeping car can't answer, so the retries were only extra requests to SAIC (about 4 minutes of them an hour overnight). Refreshes you ask for, and the ones triggered by the car (e.g. a Vehicle Start message), still retry in full, so a car you've just woken — by unlocking it, for example — is picked up straight away.

The state is inferred from the **car's own reported activity**, not from how often the integration polls — so using holiday mode (below) does not make it read asleep incorrectly.
 
**Attributes** provide supporting evidence (none of which drives the state): `reported_battery_voltage` (see note), `hours_since_activity`, `last_command_unreachable`, `data_age_hours`, and `holiday_mode`.
 
> **Battery voltage note:** the reported aux-battery voltage is shown only as an attribute, never used to decide the state. The vehicle can mis-report its own aux voltage (one owner saw 11.7V reported in HA while a calibrated external monitor read 12.13V at the same moment), so it is surfaced as a rough early-warning hint, clearly labelled as possibly inaccurate.
 
### Data Freshness sensor
 
The **Data Freshness** sensor is a diagnostic entity that answers a different question from Reachability. Reachability describes the **car's** state (awake / asleep / unreachable); Data Freshness describes the **data's** state — how current the information from the most recent poll actually is. The two are separate on purpose: the car can be reachable while the poll still returns cached data. It has three states:
 
- **live** — the last poll returned a status whose timestamp advanced, i.e. genuinely fresh data straight from the car
- **cached** — the poll succeeded, but SAIC served the same, unchanged status (typical when the car is asleep and not reporting new data)
- **failed** — the last poll errored (for example a transient `return code 4`), or no status came back at all once its retries ran out
 
Like Reachability, it stays **always available** — including when polls are failing, since that's exactly when its `failed` state is most useful. It carries a single `last_update` attribute (when the current data was last refreshed). This is the reliable signal to gate automations on: for example, only fire a remote command when Data Freshness is `live` (or Reachability is `awake`), so you're not sending commands at a car that isn't listening.
 
### Charging Data Freshness sensor
 
*(EVs and PHEVs, in regions that provide charging data)*
 
The charging figures come from a **separate SAIC endpoint** from the rest of the car's data, and it fails on its own, sometimes for hours at a time (typically a `return code 4` or a timeout). While it's down, every charging sensor — Charging Status, Power, Current, Voltage, Duration, Mileage/Power Usage Since Last Charge and so on — **holds the last value it showed** rather than blanking. That's deliberate, but until now nothing told you those values were held. The Data Freshness sensor above can't: it only describes the vehicle-status poll, so it could read `live` while your charging figures were hours old.
 
**Charging Data Freshness** is a diagnostic entity covering the charging endpoint on its own. It has three states:
 
- **live** — the charging figures were refreshed on the most recent poll
- **stale** — the most recent charging fetch failed, so the charging sensors are showing values held from the last good one
- **no_data** — charging fetches have failed ever since Home Assistant started, so there's nothing to hold (the charging sensors show unknown)
 
It's **always available**, and its attributes give the detail:
 
| Attribute | Meaning |
|---|---|
| `last_success` | When the charging figures were last genuinely refreshed |
| `data_age_minutes` | How old the charging figures on screen are — 0 when `live`, growing while `stale` |
| `stale_since` | When the current outage started |
| `consecutive_failures` | Charging fetches failed in a row |
| `last_error` | Why the most recent fetch failed (e.g. `Timed out after 20s`, `return code: 4 …`) |
| `counter_reset_held` | `true` while the since-charge figures are being held over a reset the car made without a charge — see [below](troubleshooting.md#charging-figures-reset-to-0-without-a-charge) |
| `ignored_counter_reset_at` / `ignored_counter_resets` | When the last such reset was ignored, and how many have been |
| `mileage_since_charge_from_odometer` | `true` while SAIC is sending the odometer as Mileage Since Last Charge, and the figure shown is worked out instead — see [Mileage Since Last Charge shows the odometer](troubleshooting.md#mileage-since-last-charge-shows-the-odometer) |
 
Its companion, **Charging Data Last Updated**, is a timestamp of the same `last_success` moment, so a dashboard shows it natively as "12 minutes ago".
 
Use it to gate charging automations — for example, only act on Charging Power or Charging Status when Charging Data Freshness is `live`, so an automation never fires on a held value from before an outage.

**In the log** *(from 1.4.0-beta20)*: one INFO line when charging data goes, and one when it comes back with how long it was gone, so an outage can be matched up with the rest of the log. Nothing more is logged while it stays the same:

```
Charging data for VIN …6986: not available (return code: 4, message: The remote control instruction failed, please try again later.); charging sensors are holding the values from 06:00
Charging data for VIN …6986: back after 2 h 6 min
```

Charging data outages are usually on SAIC's side and hit many cars at once. The other sensors, including **Mileage** (the odometer), carry on from the car's status in the meantime.

#### Is the Charging Power or Current value current?

Charging Data Freshness covers the charging reply as a whole. The car can also send a good reply with one figure marked as "no value", and then **Charging Power** and **Charging Current** keep showing the last good figure while Charging Data Freshness still reads `live`. So both sensors carry two attributes of their own *(from 1.4.0-beta17, #442)*:

| Attribute | Meaning |
|---|---|
| `held` | `true` when the value shown was kept from an earlier reading: the figure was "no value" this time, or the charging reply didn't come back. `false` when it was read on the latest poll |
| `reading_time` | When the value shown was actually read from the car |

To act only on a fresh reading, check `held` is `false`, for example `{{ not state_attr('sensor.mg_mg4_charging_power', 'held') }}`. A reply kept from earlier, like the saved one shown at startup while the car isn't answering, counts as held too.

**The charging controls during an outage** *(from 1.4.0-beta5)*: the car can stop answering the charging request for hours while everything else works — and the iSmart app's charging page fails the same way (#419: 21 hours on an MG4). The controls stay usable instead of going unavailable:

- **Target SOC**, **Charging Current Limit**, **Scheduled Charging Mode**, **Start** and **End** show the last values the car gave. The car still accepts the commands, so you can still change them.
- **Charging** and **Battery Heating** stay usable but read unknown, because whether the car is charging (or heating) right now isn't known.

They only go unavailable if the car has never answered the charging request since Home Assistant started.

**The car's controls after a failed poll** *(from 1.4.0-beta10)*: a poll can fail when the car has just dropped off to sleep and SAIC can't wake it in time (an MGS6, eight minutes after parking). The **Lock**, **Climate**, **Climate Mode**, **Air Conditioning**, heated seat and steering wheel controls, and the defrost switches, keep the last state the car gave and stay usable. Data Freshness reads `failed` meanwhile, so you can still tell the values aren't current. A command still goes to the car as usual; if the car doesn't answer, it fails with an error as before. Once two polls in a row have failed and Reachability says `unreachable`, the controls go unavailable.

**When the car's status says it has stopped** *(from 1.4.0-beta9)*: the outage often starts at the end of a charge (#269: an HS PHEV from 04:07 until the next morning). The car's vehicle status still answers, and on the HS PHEV, MG4 and MGS6 it carries a charging flag. Once that flag has been seen during a charge on your car, and it says the car isn't charging:

- **Charging Power**, **Charging Current**, **Charging Voltage** and **Remaining Charging Time** read 0, instead of the charge's last figures.
- **Charging Status** reads `Not Charging` (whether the charge finished, stopped or the car was unplugged isn't known).
- The **Charging** switch reads off.

**Charging Duration** and the since-charge figures keep the last charge's values. If the flag says the car is still charging, everything holds its last values as before.

**Last Charge Energy after an outage** *(from 1.4.0-beta11)*: a charge can only be closed once the charging data comes back, which can be the next day, after a drive. The first reading whose status said the charge had stopped is kept, and if the battery has dropped or the car has moved by the time the charge is closed, the charge ends at that reading: its battery level and range, with the start and end times from the car's own charge record. Before, the battery level after the drive was used (#269: 91.8% instead of 100%).
 
> **Current isn't the same as right:** the state says whether the figures are *current*. If SAIC returns a successful response containing bad values, it reads `live`. The one known case — the car resetting its since-charge counters without a charge — is handled separately, and shows up in the `counter_reset_held` attributes above. See [Charging figures reset to 0 without a charge](troubleshooting.md#charging-figures-reset-to-0-without-a-charge).
 
### Holiday mode
 
**Holiday Mode** is a switch that slows the integration's idle polling right down while you're away, to reduce how often the telematics module is woken (and so reduce 12V drain, which is especially useful on PHEVs).
 
- It's a **switch** — on/off at a glance, and easy to use in automations (e.g. turn it on when you set your home alarm for a long trip).
- It **overrides** the idle polling interval at runtime (default every 12 hours, configurable) — it does **not** change your configured intervals, so turning it off returns you to exactly your previous settings, with nothing to remember or restore.
- It does **not** slow polling while the car is **charging** or **powered on** — if you've plugged in or are driving, those were deliberate actions and you still get normal updates.
- It **persists across restarts**, so a Home Assistant reboot while you're away won't silently resume fast polling. Home Assistant still performs one immediate poll on restart so your data is fresh, then resumes the holiday cadence.
- The `Next Update Time` / `Last Update Time` sensors reflect the holiday cadence automatically, so you can confirm it's active.
> Holiday mode reduces *Home Assistant's* share of the wake-ups. The car and the official iSmart app also poll it, so for very long storage a dedicated 12V maintenance charger is still the reliable safeguard against a flat battery.
 
### Quiet charging

*From 1.4.0-beta1. New: please say how it behaves on your car in #269.*

> **Renamed in 1.4.0-beta5.** In beta1 to beta4 this was *Quiet hours*, with a **Quiet Hours Live Polling** switch where **on** meant normal polling. It is now **Quiet Charging**, and **on means quiet**. The old entities are removed when you update, so an automation that used them needs pointing at the new ones, with on and off swapped.

**Quiet charging** stops the integration waking the car overnight. Between a start and an end time you choose, while the car is switched off, each scheduled poll reads SAIC's stored copy of the car's status instead of asking the car. That read does not contact the car (see [Reading SAIC's stored status](troubleshooting.md#reading-saics-stored-status-read_cached_status)), so on an MGS6 it leaves the 12V battery alone, and on an MG HS PHEV charging overnight it should spare you the lights coming on at every poll (#269).

**Turning it on:** tick **Quiet charging** under the integration's **Configure** menu. Three controls appear on the device, all starting with *Quiet Charging* so they sit together:

| Control | What it does |
|---|---|
| **Quiet Charging** | On: quiet. Off: polling as normal. The two times flip it, and you or an automation can flip it in between |
| **Quiet Charging Start** | When Quiet Charging turns on (default 22:00) |
| **Quiet Charging End** | When Quiet Charging turns off again (default 07:00) |

To stop using it altogether, untick **Quiet charging** in Configure: the three controls go and polling is normal all the time. The switch only overrides the times for now.

**Using your charger (recommended)** *(from 1.4.0-beta7)*: in Configure, **Quiet charging: charger status** lets you pick your charger's status sensor from the usual searchable list (sensors, binary sensors and input_boolean helpers; you can also type or paste an entity ID). A zappi **Plug status**, Ohme or Pod Point status sensor works as it is: it counts as charging when it reads `on` or `charging`, in any capitalisation. (On a zappi, use **Plug status**, not **Status**: Status shows the zappi's mode, such as Boosting, which can show before the car actually starts.) For anything else, pick a template binary sensor that is on while charging, or an input_boolean that an automation turns on and off.

The car never tells SAIC when it starts charging, and on an MG HS PHEV every live poll during a charge turns the lights on. With a charger picked:

- **The charger starts charging:** one live poll straight away. The car turns its lights on by itself at the start of a charge, so this poll adds no flash of its own.
- **The charger stops,** and is still stopped 2 minutes later: one live poll, after the car has stopped, so no flash, and an exact finish. A pause shorter than 2 minutes (a zappi's one-minute "Waiting for EV") costs nothing.
- **The predicted "has it finished?" check** is held back while the charger still says charging.
- **SAIC's stored status** is then only a fallback, used while the charger's entity is unavailable.
- A charger coming back from unavailable, or Home Assistant restarting, mid-charge doesn't count as a start, so it doesn't cost a poll.
- **The car was already full** *(from 1.4.0-beta9)*: if a live reading less than 2 hours old found the battery at its target (100% if it has none), the charger saying "charging" doesn't cost a poll, and nor does the stop after it. A zappi can show "Charging" for a minute after a charge has finished (#269: at 04:36, half an hour after an HS PHEV reached 100%).

**While Quiet Charging is on and the car is switched off:**

- Scheduled polls read the stored status at your normal idle interval (or the Holiday Mode one), never faster, even while charging.
- **When the stored status shows a charge may have started**, one live poll checks, so Home Assistant shows **Charging** overnight. The car updates the stored status by itself when a charge starts, so this needs no help from your charger. That is either:
  - its charging flag on (MGS6), or
  - *(from 1.4.0-beta3)* the stored status rewritten while the car is plugged in and below its target. An MG HS PHEV writes it the moment the charge starts, but before it is charging, so its flag is still off (#269). A car already at its target is left alone, so its routine reports every few hours cost nothing.
  - *(from 1.4.0-beta6)* the stored status rewritten with the car's lock state changed since the last live poll, for a car Home Assistant last saw unplugged. That's you getting home, or going out to the car, after the start time and plugging in: on an HS PHEV the copy written then says unlocked. Routine reports don't change the lock state.

  Each stored status buys one live poll at most: if the car turns out to be waiting for its slot, nothing more is asked until the stored status changes again.
- **When the charge should have finished**, one live poll checks. The time comes from the car's own "time remaining" figure where it gives one, otherwise from the battery level, your target and the charging power, plus 30 minutes, or a quarter of the time remaining if that's longer: the car's own estimate tends to run short near the end *(from 1.4.0-beta7; 15 minutes before)*. If the car has not finished (a smart tariff pausing it, for example), nothing more is asked until the stored status shows charging has started again.
- **At the end time**, Quiet Charging turns off, with one live poll to catch up.

**What you give up:** live battery % and charging power during the night. A charge seen by the live poll at its start is recorded as normal. From 1.4.0-beta3, one that starts and finishes without a live poll seeing it is still recorded at the end time, from the battery % before and after: see *Charges no poll saw* in [Trip & efficiency statistics](sensors.md#trip--efficiency-statistics).

**What still reaches the car:**
- A car switched on during the quiet time is polled as normal.
- **Update Vehicle Data** and any command you send.

**The checks after you switch the car off** *(from 1.4.0-beta15)*: switching the car off normally sets off a few extra live polls over the next 25 minutes, to spot you plugging in. While Quiet Charging is on they stop, including ones left over from switching off just before the start time. Quiet Charging is already watching the stored status for a plug-in, so nothing is missed. Up to 1.4.0-beta14 they carried on, and on an MG HS PHEV switched off at 18:50 with Quiet Charging from 19:00, two of them reached the car mid-charge at 19:06 and 19:16 (#269).

If **Update Vehicle Data** or a command finds the car charging, the "has it finished?" check is set up from it, just as for quiet charging's own poll *(from 1.4.0-beta4)*. So an automation that presses **Update Vehicle Data** when your charger starts charging gives you the charge-start poll straight away, and quiet charging takes it from there.

**Worth knowing:**
- The stored status updates itself at the start of a charge on an MGS6 and an MG HS PHEV. Other models are still to be confirmed; a model that doesn't simply won't show a charge until the end time.
- The live poll at the start of a charge is the one an HS PHEV owner may see as the lights coming on. The car also turns them on by itself when a charge starts or resumes, and quiet charging can't stop that.
- **Quiet Charging survives a Home Assistant restart** as you left it. If a start or end time passed while Home Assistant was down, that time's setting applies instead, so a restart can't leave it the wrong way round. With the default 22:00 to 07:00:

  | What happened | After the restart |
  |---|---|
  | Turned off by hand at 23:30, Home Assistant restarted at 01:00 | **Off**, as you left it |
  | On from 22:00, Home Assistant restarted at 03:00 | **On** |
  | On from 22:00, Home Assistant down until 08:00 (the 07:00 end was missed) | **Off** |
  | Turned off by hand at 23:30, Home Assistant down until 22:30 the next night (the 22:00 start was missed) | **On** |
- The Quiet Charging switch shows the start and end times, when the "has it finished?" check is due, and the last stored-status read, in its attributes.

### Catching the start of a charge

The integration notices a charge has started the next time it polls the car, so the first charging reading can arrive up to a whole polling interval in. It does **not** speed up polling because the car reports **Connecting**, **Plugged In**, **Charging Stopped** or **Scheduled Charging**: none of those says a charge is about to start. On a smart tariff a car can sit in **Connecting** for hours before the first burst of charging, and again between bursts through the night, and polling it more often then catches nothing.

If you want a charge picked up as it starts, let your charger say so. Trigger the **Update Vehicle Data** button (or the `mg_saic.update_vehicle_data` action) from an automation when your charger's own status changes to charging. That is one request, exactly when there is something new to see.

The figures the integration works out for a charge don't depend on catching its start: Last Charge Energy is measured from the reading before the charge, and the durations use the car's own start time. The measured energy figure on Last Charge Energy fills in the stretch before the first reading from that same start time.

> **1.3.0-beta12 only:** that version polled the car again one minute later, up to three times, each time it was first seen in **Connecting**. It was based on one DC charge where that state lasted two minutes. On cars that wait in that state those polls were wasted, so they were removed in 1.3.0-beta13 (#407).

### Related options
 
Under the integration's **Configure** menu:
 
- **Holiday mode idle interval (hours)** — how slowly to poll while holiday mode is on (default 12)
- **Quiet charging** — adds the Quiet Charging controls described above (default off)
- **Data staleness threshold (hours)** — how long without reported activity before the Reachability sensor reads `likely_asleep` (default 12)
