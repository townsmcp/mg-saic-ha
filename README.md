[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/townsmcp/mg-saic-ha/blob/main/LICENSE)
![GitHub Release (latest SemVer including pre-releases)](https://img.shields.io/github/v/release/townsmcp/mg-saic-ha?include_prereleases)
![GitHub Downloads (all assets, latest release)](https://img.shields.io/github/downloads/townsmcp/mg-saic-ha/latest/total)
[![GitHub stars](https://img.shields.io/github/stars/townsmcp/mg-saic-ha?style=flat)](https://github.com/townsmcp/mg-saic-ha/stargazers)

[![hacs_badge](https://img.shields.io/badge/HACS-Default-green.svg)](https://github.com/hacs/default)
[![HACS Action](https://github.com/townsmcp/mg-saic-ha/actions/workflows/validate.yaml/badge.svg)](https://github.com/townsmcp/mg-saic-ha/actions/workflows/validate.yaml)
[![Hassfest](https://github.com/townsmcp/mg-saic-ha/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/townsmcp/mg-saic-ha/actions/workflows/hassfest.yaml)
[![Integration Usage](https://img.shields.io/badge/dynamic/json?color=41BDF5&logo=home-assistant&label=integration%20usage&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.mg_saic.total)](https://analytics.home-assistant.io/)

![Logo](/custom_components/mg_saic/brand/icon.png)


</br></br>
# MG/SAIC CUSTOM INTEGRATION

<a href="https://buymeacoffee.com/Townsmcp" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/default-orange.png" alt="Buy Me A Coffee" height="41" width="174"></a>
 
**Important Notes:** 
- **Using this integration causes the MG/SAIC mobile app to shut down if the same account is used, as per API requirements.**
- **To avoid issues, make sure to setup a Secondary Account on iSmart App.**

**Requirements:**
- Home Assistant 2025.2 or later.
- Confirmed compatible with Python 3.14, the runtime used by current Home Assistant core releases (2026.3+). No action needed on your part this is handled automatically by Home Assistant on supported installation methods.

## INSTALLATION
 
### HACS (Home Assistant Community Store)
 
1. Ensure that HACS is installed.
2. Go to HACS
3. Search for "MG SAIC" and download the repository.
4. Restart Home Assistant.
### Manual Installation
 
1. Download the latest release from the [MG SAIC Custom Integration GitHub repository](https://github.com/townsmcp/mg-saic-ha/releases).
2. Unzip the release and copy the `mg_saic` directory to `custom_components` in your Home Assistant configuration directory.
3. Restart Home Assistant.

### Checking for updates

Home Assistant's dashboard doesn't always show a pending MG SAIC update by name, even when one is waiting — it's easy to assume you're up to date when you're not. See [Where to find updates](docs/troubleshooting.md#where-to-find-updates) if you're not sure which version you're running.

## CONFIGURATION
 
To add the integration to your local Home Assistant, click here:
 
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=townsmcp&repository=mg-saic-ha&category=integration)
 
Install the integration, restart Home Assistant and then add the integration, either:
 
[<img src="https://github.com/user-attachments/assets/36459daa-a780-448a-82a5-19ee07ccd3f6">](https://my.home-assistant.io/redirect/config_flow_start?domain=mg_saic)
 
Or manually by:
 
1. Go to Configuration -> Integrations.
2. Click on the "+ Add Integration" button.
3. Search for "MG SAIC" and follow the instructions to set up the integration.
4. Select your type of account (email or phone), enter the details and select your region (EU, China, Australia, Brazil, Israel, Turkey, India, Thailand, Rest of World). If your country runs on separate SAIC infrastructure that is not covered by a built-in region, choose **Custom** and enter the API base URI, region code, and tenant ID for your market (known endpoints are collected in the [SAIC iSmart API community URI database](https://github.com/orgs/SAIC-iSmart-API/discussions/8)).
5. Once connected to the API, a list of available VINs associated with your account will be shown. Select the vehicle that you want to integrate and finish the process.
6. You will be asked which optional capabilities your vehicle has (heated seats, heated steering wheel, sunroof, window control, etc.). Tick the ones your car supports — this controls which entities are created. You can change these later via the integration's **Configure** (options) menu without re-adding the vehicle.
You may add additional vehicles by following the same steps as above.
 
### Multiple Vehicles
 
If you have more than one MG/SAIC vehicle, you can add each one as a separate integration entry. Vehicles on the **same SAIC account** are fully supported — the integration uses a single shared API session per account, so adding a second vehicle does not interfere with the first.
 
If your vehicles are on different SAIC accounts, add each account separately in the same way.
 
### Changing or updating your password
 
If you change your iSmart password (in the mobile app or on SAIC's website), the stored password stops working. You **no longer need to delete and re-add** the integration:
 
* **Automatic prompt:** the next time the integration finds the old password is rejected, Home Assistant raises a **"Reauthentication needed"** notification. Click it, enter your new password, and the vehicle reconnects — every other setting is kept.
* **Do it yourself at any time:** open the integration and choose **Reconfigure** from the entry's menu, then enter the new password.
 
**A note on password length.** The integration sends your password to SAIC exactly as entered (hashed, and never truncated on our side). However, SAIC's own servers limit password length when a password is *set*, so a very long password generated by a password manager can be accepted by the app but silently shortened on SAIC's side — after which it will never match what you type here. If a long password fails to log in, set a shorter one (around **16 characters or fewer**, with no trailing spaces) in the iSmart app and use that. The password field also trims accidental leading/trailing spaces from pasting.
 
 

## MG India Support (Beta)

MG India runs on a completely different backend to the rest of the world (a binary "TAP" protocol rather than the global REST API), maintained by [John Lazarus](https://github.com/john-lazarus). Vehicle status, lock/unlock, climate, windows, sunroof, heated seats and charging telemetry are supported.

Select **India** as your region during setup and follow the same configuration steps as above.

India support is in **beta** and actively looking for testers — see the [India tracking issue](https://github.com/townsmcp/mg-saic-ha/issues/221) and [Discussion #169](https://github.com/townsmcp/mg-saic-ha/discussions/169).

**Full detail — what's confirmed, what isn't, and PIN setup:** [docs/india.md](docs/india.md)


## Documentation

This README covers installation and initial setup. Everything else lives in its own page:

| Page | What's in it |
|---|---|
| [Sensors & Vehicle Profiles](docs/sensors.md) | Every sensor and binary sensor, trip/efficiency statistics, entity state reference, per-model vehicle profile notes, and the battery capacity override |
| [Controlling Your Car](docs/controls.md) | Climate control (both control schemes), windows, heated seats, and event-driven updates |
| [MG India Support](docs/india.md) | Setup, what's confirmed working, and current limitations for India-region vehicles |
| [A Better Route Planner (ABRP)](docs/abrp.md) | Connecting your car's live data to ABRP |
| [Deep Sleep, Holiday Mode & Update Behaviour](docs/power-management.md) | Why the car sometimes goes quiet, and the polling options that control it |
| [Troubleshooting & FAQ](docs/troubleshooting.md) | Common problems, enabling debug logging, and the diagnostic tools in `tools/` |


## Contributing
 
Contributions are welcome! If you have any suggestions or find any issues, please open an [issue](https://github.com/townsmcp/mg-saic-ha/issues) or a [pull request](https://github.com/townsmcp/mg-saic-ha/pulls).
 
## Credits
 
The global/EU backend runs on [`mg-saic-client`](https://github.com/townsmcp/saic-python-client-ng), our maintained fork of [saic-ismart-client-ng](https://github.com/SAIC-iSmart-API/saic-python-client-ng). Huge thanks to that original project and its developers/contributors, whose work this builds on. Included under the MIT License.
 
Special thanks to ad-ha for creating the original integration and for the hard work put into building and maintaining it in its previous stages. This repository continues that work.
 
India region support is built on the work of [John Lazarus](https://github.com/john-lazarus) ([john-lazarus](https://github.com/john-lazarus)), who reverse-engineered the MG India TAP protocol and created the [mg-ismart-india-ha](https://github.com/john-lazarus/mg-ismart-india-ha) client this integration uses. John maintains the India backend. Included under the MIT License.
 
## License
 
This project is licensed under the MIT License. See the LICENSE file for details.
 
## Disclaimer
THIS PROJECT IS NOT IN ANY WAY ASSOCIATED WITH OR RELATED TO THE SAIC MOTOR OR ANY OF ITS SUBSIDIARIES. The information here and online is for educational and resource purposes only and therefore the developers do not endorse or condone any inappropriate use of it, and take no legal responsibility for the functionality or security of your devices.
