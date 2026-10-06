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

**Experimental: reading SAIC's stored status** *(from 1.3.0-beta14)*. SAIC keeps a copy of the car's last status on its server, which the iSmart app reads when it opens. The `mg_saic.read_cached_status` action reads the same copy: it returns a cut-down status (lock, windows, tyre pressures, battery %, range, odometer), the time it was taken and whether SAIC considers the car online. It does not ask the car for anything, refresh the integration or change any entity. It is there to measure whether that read leaves the car asleep and how old the stored status gets; nothing in the integration uses it yet.

### Reachability sensor
 
The **Reachability** sensor surfaces this at a glance, so you can tell when data may be stale rather than wondering why things have gone quiet. It has three states:
 
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
 
> **Current isn't the same as right:** the state says whether the figures are *current*. If SAIC returns a successful response containing bad values, it reads `live`. The one known case — the car resetting its since-charge counters without a charge — is handled separately, and shows up in the `counter_reset_held` attributes above. See [Charging figures reset to 0 without a charge](troubleshooting.md#charging-figures-reset-to-0-without-a-charge).
 
### Holiday mode
 
**Holiday Mode** is a switch that slows the integration's idle polling right down while you're away, to reduce how often the telematics module is woken (and so reduce 12V drain, which is especially useful on PHEVs).
 
- It's a **switch** — on/off at a glance, and easy to use in automations (e.g. turn it on when you set your home alarm for a long trip).
- It **overrides** the idle polling interval at runtime (default every 12 hours, configurable) — it does **not** change your configured intervals, so turning it off returns you to exactly your previous settings, with nothing to remember or restore.
- It does **not** slow polling while the car is **charging** or **powered on** — if you've plugged in or are driving, those were deliberate actions and you still get normal updates.
- It **persists across restarts**, so a Home Assistant reboot while you're away won't silently resume fast polling. Home Assistant still performs one immediate poll on restart so your data is fresh, then resumes the holiday cadence.
- The `Next Update Time` / `Last Update Time` sensors reflect the holiday cadence automatically, so you can confirm it's active.
> Holiday mode reduces *Home Assistant's* share of the wake-ups. The car and the official iSmart app also poll it, so for very long storage a dedicated 12V maintenance charger is still the reliable safeguard against a flat battery.
 
### Catching the start of a charge

The integration notices a charge has started the next time it polls the car, so the first charging reading can arrive up to a whole polling interval in. It does **not** speed up polling because the car reports **Connecting**, **Plugged In**, **Charging Stopped** or **Scheduled Charging**: none of those says a charge is about to start. On a smart tariff a car can sit in **Connecting** for hours before the first burst of charging, and again between bursts through the night, and polling it more often then catches nothing.

If you want a charge picked up as it starts, let your charger say so. Trigger the **Update Vehicle Data** button (or the `mg_saic.update_vehicle_data` action) from an automation when your charger's own status changes to charging. That is one request, exactly when there is something new to see.

The figures the integration works out for a charge don't depend on catching its start: Last Charge Energy is measured from the reading before the charge, and the durations use the car's own start time. The measured energy figure on Last Charge Energy fills in the stretch before the first reading from that same start time.

> **1.3.0-beta12 only:** that version polled the car again one minute later, up to three times, each time it was first seen in **Connecting**. It was based on one DC charge where that state lasted two minutes. On cars that wait in that state those polls were wasted, so they were removed in 1.3.0-beta13 (#407).

### Related options
 
Under the integration's **Configure** menu:
 
- **Holiday mode idle interval (hours)** — how slowly to poll while holiday mode is on (default 12)
- **Data staleness threshold (hours)** — how long without reported activity before the Reachability sensor reads `likely_asleep` (default 12)
