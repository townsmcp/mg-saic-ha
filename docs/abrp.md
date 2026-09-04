# A Better Route Planner (ABRP) Integration

← [Back to the main README](../README.md)

---

## A Better Route Planner (ABRP)

The integration can push each vehicle's live telemetry — state of charge, estimated range, charging state, outside temperature, odometer and (when the car reports a GPS fix) position — to [A Better Route Planner](https://abetterrouteplanner.com/). This lets ABRP plan and adjust routes using your car's real SoC **without an OBD dongle**, the same way the SAIC MQTT gateway does.

### What to expect

Telemetry is sent only when the car returns genuinely fresh data on a poll, so ABRP is fed real readings rather than cached ones. Because the data comes from SAIC's telematics (not a direct OBD link), updates arrive at your polling cadence — great for SoC, range and parked location, but not second-by-second position while driving. This is the same limitation any SAIC-based ABRP feed has.

### Setup

ABRP is configured per vehicle, in that vehicle's integration options — **Settings → Devices & Services → MG SAIC → (your car) → Configure (cog)**. You need **two** credentials, and you obtain **both** yourself:

1. **Create your ABRP API (telemetry) key.** Go to the [ABRP telemetry API keys page](https://abetterrouteplanner.com/home/app/api-keys/telemetry) and sign in with your ABRP account. (This page is linked from the **Telemetry API** section of <https://www.iternio.com/api>.) The integration does **not** ship a shared key, so this step is required.

   ![ABRP API keys page](images/abrp-api-keys.png)

   Click **Create key**, give it a name so you can recognise it later — for example `MG SAIC HA` — then click **Create key**.

   ![Create an ABRP API key](images/abrp-create-key.png)

   Copy the key it generates and keep it somewhere safe; you'll paste it into the integration. (You can create up to five keys.)

2. **Get your ABRP user token.** This is a **separate** credential from the API key above — a per-vehicle token ABRP uses to accept your data. Get it from the ABRP app (make sure the vehicle you want is selected):

   - Tap the **☰ menu** at the top right of the ABRP home screen to open **Settings**.
   - Under **Connect live data**, tap **Connect**.

     ![ABRP Connect live data](images/abrp-connect-live-data.png)

   - Under **Available methods → Generic**, tap **Connect**.

     ![ABRP Generic live data method](images/abrp-generic-connect.png)

   - Tap **Copy Token** and keep the token safe — this is your **ABRP user token**.

     ![ABRP Generic token](images/abrp-generic-token.png)

   (Per [ABRP's docs](https://www.iternio.com/api), user tokens can also be obtained via their OAuth flow, but this integration uses this manually-pasted token.)

3. Paste your key into **ABRP API key**, your token into **ABRP user token**, and save. **Both are required** — telemetry starts flowing on the next successful refresh once the pair is validated.

To **disable** ABRP for a vehicle, clear the fields and save.

Both credentials are validated against ABRP when you save them, so an incorrect key or token is flagged immediately rather than failing silently later.

### Multiple cars / adding a car later

Each vehicle is a separate config entry, so the ABRP token is stored per VIN — set a different token for each car in its own options. If you add another vehicle later (**Add** a new MG SAIC entry), just open that new car's options and paste its token. You can change or remove a token at any time via the same Configure screen.

 
