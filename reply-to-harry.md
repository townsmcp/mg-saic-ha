@HarryFlatter — all three of these turned out to be real, and two of them are worse than you thought (they affect every car, not just yours). Fixed in #330, going out as **1.2.7-beta5**.

## 1. Efficiency Since Charge (SOC) — not a PHEV thing

You said it looked "absent for PHEV". It isn't — the entity is created for BEV and PHEV alike, so you do have it. It just never produces a value **on any car**. I checked mine: charged Friday night, 61 km driven since, and it's been sat on `Unknown` the entire time too.

Two bugs stacked on top of each other:

- The sensor was passing the wrong object into its odometer and SOC lookups. SOC got away with it because it has a fallback to the charging data; the odometer didn't.
- That odometer fallback was then looking for `mileage` in the wrong part of the response. I checked the client library schema to be sure: `mileage`, `mileageOfDay` and `mileageSinceLastCharge` all live in `rvsChargeStatus`, not where we were looking. So the fallback could never have worked.

No odometer, no distance, no value — every time. The charge-detection behind it was working perfectly all along, which is why it was so unobvious. Both fixed, and it'll populate as soon as you've driven after a charge.

## 2. Power Usage Since Last Charge — you were right, and right about why

Your instinct that this smelled like the earlier 3× bug was correct, though the mechanism was different this time. The correction **does** exist and it's the right number — it was just added in a place this particular sensor never reaches. So it was dead code and the sensor carried on showing the raw figure.

Your numbers back it out exactly: 20.20 kWh corrected is **~6.9 kWh**, which fits 24 miles against a 53-of-75 mile range readout. Now applied wherever the value is read.

Worth knowing which sensors were affected: `Efficiency Since Last Charge` and the `Last Trip` energy figures were already correcting this properly and were always right. Only the standalone Power Usage sensor was wrong — so if those looked inconsistent with each other on your dashboard, that's the explanation.

On `lastChargeEndingPower=725` — good spot, and yes, that's the same 3× inflation (725 → 72.5 kWh → ~24.7 kWh real). To answer your question directly: **there is no `lastChargeStartingPower`.** I went through the full field list for both charging blocks; nothing of the sort exists. Which brings us to:

## 3. Last Charge Energy — new sensor, your suggestion

Since there's no starting figure to subtract, the only way to get this is to measure it across the charging session, so that's what it now does. It gives you two independent numbers, both in the attributes:

- **From battery percentage** — SOC rise × usable capacity. This is the headline value since it works on every car.
- **From the car's own energy figures** — `lastChargeEndingPower` minus `Power Usage Since Last Charge`, which is effectively the energy sitting in the pack. Shown alongside for comparison, and dropped when it doesn't look trustworthy.

Plus start/end/added percentage, duration, average power, and timestamps. There's also a `mg_saic_charge_completed` event firing at the end of each charge with the same data, which should suit your dashboard.

One caveat for your granny-charging use case: **this is energy at the battery, not at the wall.** Your Zappi will always read higher, because it's also paying for charger and cable losses — typically 5–10%, more on a slow granny charge in the cold. So it'll get you close for settling up with your friend, but it's a floor, not a meter reading.

Your car's charging dropout got specifically designed around, incidentally. Because your charging endpoint goes quiet the instant a session completes, a naive implementation would log a phantom charge every single time that happened. Session tracking now only acts on polls where the charging data actually came back.

## On the capacity — you got there before I could ask

I was going to ask you to check whether you had an override set, because 24.70 didn't match the profile. You've answered it: override deleted, now on 23.2. That's the right call and it makes your Last Charge Energy figures correct from the start.

One correction though, because it matters for trusting the number: **SAIC hasn't started reporting 23.2.** The car still reports the same inflated 725 it always has. The 23.2 is a figure *we* hold in the vehicle profile for the AS33P — the real usable capacity, as opposed to the 24.7 nominal pack size from the brochure. So nothing changed at SAIC's end; you've just switched from the brochure number to the usable one, which is the more honest basis for energy maths (you can't actually use all 24.7).

## And your arithmetic checks out

    27.9% of 23.2 kWh = 6.47 kWh
    20.20 kWh / 3     = 6.73 kWh

Close enough — yes, and usefully so. Those are two genuinely independent routes to the same number (battery percentage against known capacity, versus the car's own energy counter with the correction applied) landing within about 4% of each other. That's the first real-world confirmation I've had that the correction factor is right, so thank you — it's exactly the cross-check I couldn't do on my own car, since mine doesn't have the inflation.

It's also mildly interesting that the SOC route reads slightly *lower*. With one data point I'm not going to start tuning the factor on the strength of it, but if you fancy jotting down both figures over your next few charges, that would tell us whether the 4% is a consistent bias worth correcting or just noise. No obligation.

## On the version — sorry, that one's my fault

Good catch, and the confusion is entirely mine. Here's what happened: when I released beta4 on the 27th I tagged and published the release, but didn't bump the version inside the integration's manifest, which was still reading beta3. So the release you installed says beta4 (that's what HACS shows you) while the code inside thinks it's beta3.

I then wrote the PR against that manifest and bumped what I thought was beta3 → beta4 — straight into a version number that already existed and that you were already running. So no, beta4 hasn't been patched; my PR was about to create a second, different beta4, which would have been thoroughly confusing for everyone.

**Fixed — these three changes will land as 1.2.7-beta5.** I'll post here when it's up.
