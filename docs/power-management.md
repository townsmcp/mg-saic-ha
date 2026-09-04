# Deep Sleep, Holiday Mode & Update Behaviour

Why the car sometimes goes quiet, and the options that control how the integration polls it.

← [Back to the main README](../README.md)

---

## Deep sleep & holiday mode
 
### Deep sleep (why the car sometimes goes quiet)
 
After a car has been idle for a long time (often around a day), its telematics module goes into a deep sleep to save the 12V battery. While asleep it can still answer *cached* status requests, but it cannot service live commands — these fail with the SAIC "can't reach the car" error (return code 4). This is normal vehicle behaviour, not an integration fault.
 
On **PHEVs** this matters more than on BEVs: a PHEV only recharges its 12V battery while the car is running (driving or, in some cases, charging the main battery), whereas a BEV tops the 12V up from the main traction battery as needed. So a PHEV left parked for a long time is more likely to drift into deep sleep, and — as owners have observed — once it's asleep, often only actually **driving** the car reliably wakes it again.
 
### Reachability sensor
 
The **Reachability** sensor surfaces this at a glance, so you can tell when data may be stale rather than wondering why things have gone quiet. It has three states:
 
- **awake** — the car is powered on, or has reported activity recently
- **likely_asleep** — the car has reported no activity for longer than the *data staleness threshold* (default 12 hours, configurable); its data may be out of date
- **unreachable** — a live command recently failed with return code 4 (the car itself confirming it can't be reached)
The state is inferred from the **car's own reported activity**, not from how often the integration polls — so using holiday mode (below) does not make it read asleep incorrectly.
 
**Attributes** provide supporting evidence (none of which drives the state): `reported_battery_voltage` (see note), `hours_since_activity`, `last_command_unreachable`, `data_age_hours`, and `holiday_mode`.
 
> **Battery voltage note:** the reported aux-battery voltage is shown only as an attribute, never used to decide the state. The vehicle can mis-report its own aux voltage (one owner saw 11.7V reported in HA while a calibrated external monitor read 12.13V at the same moment), so it is surfaced as a rough early-warning hint, clearly labelled as possibly inaccurate.
 
### Data Freshness sensor
 
The **Data Freshness** sensor is a diagnostic entity that answers a different question from Reachability. Reachability describes the **car's** state (awake / asleep / unreachable); Data Freshness describes the **data's** state — how current the information from the most recent poll actually is. The two are separate on purpose: the car can be reachable while the poll still returns cached data. It has three states:
 
- **live** — the last poll returned a status whose timestamp advanced, i.e. genuinely fresh data straight from the car
- **cached** — the poll succeeded, but SAIC served the same, unchanged status (typical when the car is asleep and not reporting new data)
- **failed** — the last poll errored (for example a transient `return code 4`)
 
Like Reachability, it stays **always available** — including when polls are failing, since that's exactly when its `failed` state is most useful. It carries a single `last_update` attribute (when the current data was last refreshed). This is the reliable signal to gate automations on: for example, only fire a remote command when Data Freshness is `live` (or Reachability is `awake`), so you're not sending commands at a car that isn't listening.
 
### Holiday mode
 
**Holiday Mode** is a switch that slows the integration's idle polling right down while you're away, to reduce how often the telematics module is woken (and so reduce 12V drain, which is especially useful on PHEVs).
 
- It's a **switch** — on/off at a glance, and easy to use in automations (e.g. turn it on when you set your home alarm for a long trip).
- It **overrides** the idle polling interval at runtime (default every 12 hours, configurable) — it does **not** change your configured intervals, so turning it off returns you to exactly your previous settings, with nothing to remember or restore.
- It does **not** slow polling while the car is **charging** or **powered on** — if you've plugged in or are driving, those were deliberate actions and you still get normal updates.
- It **persists across restarts**, so a Home Assistant reboot while you're away won't silently resume fast polling. Home Assistant still performs one immediate poll on restart so your data is fresh, then resumes the holiday cadence.
- The `Next Update Time` / `Last Update Time` sensors reflect the holiday cadence automatically, so you can confirm it's active.
> Holiday mode reduces *Home Assistant's* share of the wake-ups. The car and the official iSmart app also poll it, so for very long storage a dedicated 12V maintenance charger is still the reliable safeguard against a flat battery.
 
### Related options
 
Under the integration's **Configure** menu:
 
- **Holiday mode idle interval (hours)** — how slowly to poll while holiday mode is on (default 12)
- **Data staleness threshold (hours)** — how long without reported activity before the Reachability sensor reads `likely_asleep` (default 12)
