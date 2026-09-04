# MG India Support

← [Back to the main README](../README.md)

---

## MG India Support (Beta)
 
MG India runs on a completely different backend to the rest of the world (a binary "TAP" protocol rather than the global REST API). As of v1.1.4-beta2 this integration supports MG India vehicles natively, powered by the [mg-ismart-india-client](https://pypi.org/project/mg-ismart-india-client/) library created and maintained by [John Lazarus](https://github.com/john-lazarus), who reverse-engineered the protocol.
 
### Setting up an India vehicle
 
Follow the normal configuration steps above and select **India** as your region. You will then be asked for your **4-digit iSmart PIN** — the same PIN you use to authorise commands in the iSmart India app. MG India requires this PIN for all remote commands. Only a secure one-way hash of the PIN is stored by the integration; the PIN itself is never saved. When choosing your vehicle you will see its model name with a shortened VIN (e.g. “MG Comet EV (…0001)”) rather than only the raw VIN.
 
### What works for India (confirmed on a real vehicle)
 
- Vehicle status: doors, windows, boot, bonnet, lock state, climate state, interior/exterior temperature, range, odometer, tyre pressures, 12V battery voltage
- State of Charge for BEVs, reported by the ordinary vehicle-status payload
- Door lock / unlock (with automatic verification — MG India sometimes applies a command without confirming it, and the integration re-checks the vehicle state)
- Climate control on / off
- Windows open / close
- Sunroof open / close (if equipped)
- Front heated seats (if equipped)
- Tailgate release
- Find My Car
 
### Not available for India
 
- **Charging data and control** — charging status/control, scheduled charging, battery heating, target SOC, charging current, and total battery capacity entities are not created for India vehicles. The BEV State of Charge above comes from vehicle status; MG India's platform still does not expose the separate charging endpoint (it is not present in the iSmart India app either).
- Window **ventilate** (crack open) — not yet confirmed safe on the India protocol; the open/close buttons work.
- Event-driven updates — India vehicles use regular polling only.
 
India support is in **beta** and actively looking for testers — see the [India tracking issue](https://github.com/townsmcp/mg-saic-ha/issues/221) and [Discussion #169](https://github.com/townsmcp/mg-saic-ha/discussions/169).
 
 
 
