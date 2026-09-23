# Troubleshooting & FAQ

Common problems, how to turn on debug logging, and the diagnostic tools shipped with the integration.

← [Back to the main README](../README.md)

---

## 💡 Troubleshooting & FAQ
 
* **"Invalid Credentials" or Connection Timeouts:** Ensure you are choosing the correct region matching your mobile app setup.
* **I changed my password and the integration stopped working:** You no longer need to delete and re-add it — Home Assistant will prompt you to re-enter the new password, or you can trigger it yourself via **Reconfigure**. See [Changing or updating your password](../README.md#changing-or-updating-your-password). If a long, password-manager-generated password won't log in, SAIC may have truncated it when it was set; use around 16 characters or fewer.
* **"The account is not registered" (code 1000036):** Your account exists on a different regional SAIC backend than the one selected. Pick the region matching the country where the account was created — for markets without a built-in preset, use the **Custom** region option to enter your market's endpoint details.
* **Entities showing as 'Unavailable':** The integration respects API rate limits to prevent account lockouts. If an entity is temporarily unavailable, wait for the next scheduled update or use the `button.update_vehicle_data` entity to force a refresh.
* **My App keeps logging me out:** As noted above, ensure your Home Assistant integration uses a **Secondary Account**, not your primary mobile application credentials.
* **Target SOC entity is missing:** Some vehicle models (e.g. MG HS PHEV) do not support remote Target SOC setting via the iSmart API. The entity is intentionally not created for these models.
* **Electric Range shows an unexpected value:** For some PHEV models the live electric range field is not populated by the API. The integration falls back to the estimated-range-after-full-charge figure from the charging management data.
* **Two cars on the same account:** Fully supported. Both vehicles share a single API session so neither interferes with the other.
* **Instant Power sensor shows a stale value after HA restart:** Home Assistant restores entity states from its database on startup. The value will update to `0 kW` on the first successful poll (usually within 30 seconds) if the car is not driving.
* **"Lock Status" binary sensor shows on/off, not Locked/Unlocked:** This is expected HA behaviour for the `lock` device class — see the [Entity States Reference](sensors.md#entity-states-reference) above for exactly what `on` and `off` mean for every status/control entity in this integration.
* **"MG SAIC: Vehicle Not Locked" notification:** A remote command (e.g. starting climate) was rejected because the car isn't locked. Lock it with the key fob or the iSmart app and send the command again — no physical key start is needed. This is a separate condition from **"MG SAIC: Remote Command Limit Reached"**: SAIC uses the same underlying error code for both, but only the command-limit one requires starting the vehicle with the physical key to reset (#374).
* **Charging figures look out of date, or didn't change during an outage:** SAIC's charging endpoint fails independently of everything else, and while it's down the charging sensors hold their last values on purpose. Check the **Charging Data Freshness** sensor — `stale` means the figures are held, and its `last_success` / `data_age_minutes` attributes say from when. See [Charging Data Freshness sensor](power-management.md#charging-data-freshness-sensor).
* **Charging figures reset to 0 without a charge:** see [below](#charging-figures-reset-to-0-without-a-charge).
* **I can't find the update, or don't realise there is one:** See [Where to find updates](#where-to-find-updates) below — the dashboard summary card doesn't always show every pending update by name.

---

## Charging figures reset to 0 without a charge

**Mileage Since Last Charge** and **Power Usage Since Last Charge** can drop to 0 — and **Efficiency Since Last Charge** go with them — even though the car hasn't been charged. This comes from the car itself, not from Home Assistant: SAIC returns a normal, successful response in which the car's own since-charge counters have been reset, as if a charge had just finished.

A debug log from an MG HS PHEV (#262) caught it happening. After a SAIC outage lasting about two hours (`return code 6`, then `return code 4`), the first successful response showed:

- Mileage and Power Usage Since Last Charge reset to 0
- `lastChargeEndingPower` reset to the battery's current energy
- a charge-end timestamp stamped *during* the outage

— while the battery percentage, charging status, plug state and odometer were all **unchanged**. No charge took place; the car's telematics recorded one when it came back.

Because the response is genuine, retention doesn't apply and **Charging Data Freshness** correctly reads `live`. If you track efficiency, **Efficiency Since Charge (SOC)** is unaffected: it's worked out from the battery percentage and odometer and never reads these counters — see [Trip & efficiency statistics](sensors.md#trip--efficiency-statistics).

## Where to find updates

If you've updated the integration through HACS but people are telling you they're still on an old version, it's usually not that the update isn't there — it's that they haven't seen it.

Home Assistant's dashboard shows a summary card like this one, and it only lists a couple of names even when there are more updates waiting:

![Home Assistant dashboard update summary, showing 5 updates but only two named](images/updates-dashboard-summary.png)

Five updates are available here, but only two are named on the card. MG SAIC could easily be one of the three not shown, and there's nothing on this card to tell you either way.

**Tap the arrow to see the full list.** Go to **Settings → System → Updates**, or tap through from the summary card above, and every pending update is listed — usually grouped by source, with everything installed through HACS (including this integration) under a **HACS** heading with its own **Update all**:

![Full Home Assistant Updates page, showing MG SAIC listed under the HACS group](images/updates-full-list.png)

If you're not seeing a version you expect on your own dashboard, check here before assuming the update hasn't reached you.

---

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

---

## Diagnostic Tools (`tools/`)
 
The [`tools/`](tools/) folder contains optional helper scripts for **researching how a specific car model behaves** — they are not part of the integration and are never loaded by Home Assistant. They let owners capture what the official iSmart app sends and receives, so we can map new features (like climate modes, heated seats, and window control) accurately per model.
 
| File | Purpose |
|------|---------|
| `redact.py` | Strips your login token and sensitive headers from a capture **before** you share it — always run this first. |
 
These scripts only *observe* app traffic; they do not modify your car, account, or the integration. See [`tools/README.md`](tools/README.md) for the full walkthrough. If you'd like to help profile your model, contributions of captured (redacted) data are very welcome.
 
 
