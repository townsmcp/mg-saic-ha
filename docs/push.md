# Real-Time Push (instant car-start and charging alerts)

How the integration hears from SAIC the moment something happens, instead of asking every minute.

← [Back to the main README](../README.md)

---

## What it does

SAIC sends a push notification to the iSmart app when the car starts and when charging stops. From 2.0.0-beta01 the integration registers its own push channel with Google's Firebase Cloud Messaging (FCM) and listens for the same notifications.

- **Car start** (message type 323): the car is refreshed within a few seconds of being started, and the powered-on polling begins straight away.
- **Charging stopped** (message type 328): "Finish Charging … remaining power is NN%". SAIC sends this whenever charging stops, not only at full. That includes the end of a cheap-rate slot, so it is not proof the battery is full.

While push is working, the message queue is only checked every **30 minutes** as a safety net. That check is the reason for the 12V battery drain people saw with 60-second polling, so push is the main saving. If push drops, the integration goes back to every minute until it recovers.

Push is available on the global/EU backend. It is not available for MG India.

## Which cars send which pushes

What we know so far, from owners testing the 2.0.0 betas:

| Car | Car start push | Charging stopped push |
|---|---|---|
| MGS6 | Working | Working |
| MGS5 | Working | Working (thanks @SteveMSJ) |
| MG4 | Working (thanks @Heiko-Schneider) | Not working (thanks @SteveMSJ) |
| HS PHEV | Working | Not working (thanks @HarryFlatter) |

The car start push comes on a full start. On an MG4, switching the ignition on while V2L is running doesn't send it, so Power Mode can stay Off during V2L until the next update. On normal drives it arrived within seconds, and HA showed the car on 10–30 seconds after getting in.

The car-start push is the main feature; treat charging stopped as a bonus where your car supports it. Whether a car sends it is up to SAIC, not the integration, and other models may not get it either.

**Got a car that isn't listed, or a different result?** Please tell us in [Push notifications – known compatibility (discussion #506)](https://github.com/townsmcp/mg-saic-ha/discussions/506): your model, year and trim, whether **Push Status** reaches `push`, and whether you get a **Last Start Push** and a **Last Charging Stopped Push**. The table there is kept up to date.

## Turning it on

- **New installs:** on by default.
- **Existing cars:** off until you choose. A Repair item ("Turn on real-time push") appears; **Submit** turns it on. You can also tick **Push notifications** under **Configure**.

When it is first turned on, the integration re-arms the car's alarm switches (fault and car-start) once: off, a 10-second pause, then on. On an MGS6 this may be what made SAIC start sending the charging-stopped message to the integration; that is not proven. On an MG4 it made no difference.

## Entities

| Entity | What it shows |
|---|---|
| **Push Status** | `off`, `connecting`, `push` (working; queue checked every 30 min) or `polling` (not dependable; queue checked every minute). The `reason` attribute says why it is not `push`. |
| **Last Push Received** | Time of the latest push of any kind, with its message type and title. |
| **Last Start Push** | Time of the latest car-start push. |
| **Last Charging Stopped Push** *(called Last Charging Finished Push in 2.0.0-beta01)* | Time of the latest charging-stopped push. The `battery_percent` attribute is the level SAIC reported. |

Both "Last … Push" sensors keep their value across restarts.

## Automations

Every push fires the event `mg_saic_push`, so you can react without waiting for a refresh. Open **Developer tools → Events**, listen to `mg_saic_push` and start the car to see the contents.

## Holiday Mode

In **Fully Quiet** holiday mode a push still fires the event but no refresh is made.

## How it keeps itself healthy

- The registration is replaced roughly every 18 hours. A registration several days old stopped receiving the car-start alarm while still looking connected.
- If the message queue shows a start that no push announced (and the connection had been up for 35 minutes or more), push is treated as unhealthy for 3 hours, polling returns to every minute and the registration is renewed.
- A push more than 10 minutes old is recorded but not acted on. SAIC keeps undelivered pushes for 28 days and replays them when the connection returns.
- After 3 failed logins in a row with the push registration, a new one is made.

## One account, one push channel

SAIC keeps one session per account, and the most recent login owns the push channel. Use a separate SAIC account for Home Assistant, shared with the car as a secondary account, so the iSmart app on your phone is not disturbed.

## Requirements

`mg-saic-client` 0.10.1 or later and `protobuf`. Both are installed automatically.

## Troubleshooting

- **Push Status stays on `polling`:** check the `reason` attribute. `registration_failed` and `login_with_push_token_failed` usually clear on their own after 30 minutes; if not, turn the option off and on.
- **No charging-stopped push:** only the MGS5 and MGS6 have been seen sending it. An MG4 and an HS PHEV have been observed not to, and other models may not be able to be enabled for it. This is down to the car and SAIC, not the integration. See [which cars send which pushes](#which-cars-send-which-pushes), and please report your model in [discussion #506](https://github.com/townsmcp/mg-saic-ha/discussions/506).
