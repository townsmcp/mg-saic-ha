# 1.2.7-beta5

Two fixes that affect every car, and one new sensor. All three came out of [discussion #262](https://github.com/townsmcp/mg-saic-ha/discussions/262) — thanks to @HarryFlatter for the detail.

## Fixed: Efficiency Since Charge (SOC) never showed a value

This sensor has been stuck on `Unknown` since it was introduced — on **all** models, not just PHEVs, and regardless of how much you'd driven since charging.

Two separate faults were stacked on top of each other. The sensor was handing the wrong object to its odometer lookup, and that lookup's backup route was searching a part of the API response that doesn't contain an odometer at all. With no odometer reading, the sensor had nothing to calculate a distance from, so it never produced a figure.

Both are fixed. The underlying charge-detection was working correctly all along, so the sensor starts reporting as soon as you've driven after a charge.

## Fixed: Power Usage Since Last Charge still ~3× too high on MG HS PHEV

The correction for this was added in 1.2.6, but it was applied in a code path this particular sensor never takes — so in practice nothing changed and the sensor kept showing the inflated figure. It's now applied wherever the value is read.

On an HS PHEV, a reading like 20.20 kWh should have been around 6.9 kWh.

Two related sensors — `Efficiency Since Last Charge` and the `Last Trip` energy figures — were already correcting this properly and were never affected. If you have an HS PHEV and your energy figures looked inconsistent with each other, this is why.

## New: Last Charge Energy sensor *(BEV/PHEV)*

Tells you how much energy the last completed charge put **into** the battery. Useful when you've charged somewhere that isn't home and want to know what you actually took — the car reports charging power while it's happening, and how much you've used *since* charging, but nothing about the charge itself.

The API has no field for this, so the integration measures it across the charging session. Two independent figures are calculated and both appear in the sensor's attributes:

- **From battery percentage** — the rise in charge level against your car's usable capacity. This is the headline value, as it works on any car that reports a battery percentage. If your `Total Battery Capacity` looks wrong, set a [battery capacity override](https://github.com/townsmcp/mg-saic-ha#battery-capacity-override) and this will follow it.
- **From the car's own energy figures** — shown alongside for comparison, and omitted when it doesn't look trustworthy.

Also in the attributes: start and end battery percentage, percentage added, how long the charge took, average power, and the session's timestamps. A `mg_saic_charge_completed` event fires at the end of each charge carrying the same data, so you can notify or log on it.

**Worth knowing:** this is energy measured at the battery, so it will read lower than your wall meter, Zappi or similar — those also pay for charger and cable losses. Expect a gap of roughly 5–10%.

Some deliberate limits: charges that add less than 0.5% are ignored (that's the small rebound the pack reports after a drive rather than a real charge), and a session left open more than 48 hours is dropped rather than reported as a nonsense number. Crucially, a charging-data dropout is never mistaken for the end of a charge — on some cars the charging endpoint goes quiet the instant a session finishes, which would otherwise log a phantom charge every time.

## Upgrading

No action needed. The Last Charge Energy sensor appears after a restart and populates once it has seen a complete charge from start to finish — so it will read `Unknown` until your next charge finishes.
