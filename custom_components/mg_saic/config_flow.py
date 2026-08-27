# File: config_flow.py

import logging
from contextlib import suppress

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .backends import REGION_INDIA, create_backend
from .backends.india import IndiaBackendNotReadyError, hash_india_pin
from .const import (
    ABRP_DOC_URL,
    CONF_ABRP_API_KEY,
    CONF_ABRP_USER_TOKEN,
    CONF_HOLIDAY_UPDATE_INTERVAL,
    CONF_STALE_DATA_THRESHOLD,
    CONF_BATTERY_CAPACITY_OVERRIDE,
    DEFAULT_HOLIDAY_UPDATE_INTERVAL_HOURS,
    DEFAULT_STALE_DATA_THRESHOLD_HOURS,
    AFTER_ACTION_UPDATE_INTERVAL_DELAY,
    CONF_HAS_BATTERY_HEATING,
    CONF_HAS_HEATED_SEATS,
    CONF_HAS_SUNROOF,
    COUNTRY_CODES,
    DEFAULT_AC_LONG_INTERVAL,
    DEFAULT_ALARM_LONG_INTERVAL,
    DEFAULT_BATTERY_HEATING_LONG_INTERVAL,
    DEFAULT_CHARGING_CURRENT_LONG_INTERVAL,
    DEFAULT_CHARGING_LONG_INTERVAL,
    DEFAULT_CHARGING_PORT_LOCK_LONG_INTERVAL,
    DEFAULT_FRONT_DEFROST_LONG_INTERVAL,
    DEFAULT_HEATED_SEATS_LONG_INTERVAL,
    DEFAULT_LOCK_UNLOCK_LONG_INTERVAL,
    DEFAULT_REAR_WINDOW_HEAT_LONG_INTERVAL,
    DEFAULT_SUNROOF_LONG_INTERVAL,
    DEFAULT_TAILGATE_LONG_INTERVAL,
    DEFAULT_TARGET_SOC_LONG_INTERVAL,
    DOMAIN,
    LOGGER,
    DEFAULT_TENANT_ID,
    REGION_API_CODES,
    REGION_BASE_URIS,
    REGION_CHOICES,
    REGION_CUSTOM,
    UPDATE_INTERVAL,
    UPDATE_INTERVAL_AFTER_SHUTDOWN,
    UPDATE_INTERVAL_CHARGING,
    UPDATE_INTERVAL_DC_CHARGING,
    UPDATE_INTERVAL_GRACE_PERIOD,
    UPDATE_INTERVAL_POWERED,
)
from .logic import build_vehicle_options
from saic_ismart_client_ng import SaicApi
from saic_ismart_client_ng.model import SaicApiConfiguration

# A masked (password-type) text input for the credential fields.  The import is
# wrapped so the integration still loads under the lightweight import-based test
# harness (tests/), which stubs homeassistant.helpers without a real selector
# module; there it falls back to a plain string field.
try:  # pragma: no cover - exercised at runtime, shimmed under tests
    from homeassistant.helpers.selector import (
        TextSelector,
        TextSelectorConfig,
        TextSelectorType,
    )

    PASSWORD_SELECTOR = TextSelector(
        TextSelectorConfig(type=TextSelectorType.PASSWORD)
    )
except Exception:  # noqa: BLE001 - any import failure means "no selector here"
    PASSWORD_SELECTOR = str


class NoVehiclesFoundError(Exception):
    """Login succeeded, but the account has no vehicles linked to it.

    Raised by the vehicle-fetch helpers so the config flow can surface a
    distinct, actionable "add a car in the iSMART app first" message instead
    of the generic "check your credentials" error — the credentials are, in
    fact, correct in this case (issue #294).
    """


@callback
def configured_vins(hass):
    """Return a set of configured MG SAIC VINs."""
    return set(
        entry.data.get("vin") for entry in hass.config_entries.async_entries(DOMAIN)
    )


class SAICMGConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MG SAIC integration."""

    VERSION = 1

    def __init__(self):
        self.login_type = None
        self.username = None
        self.password = None
        self.country_code = None
        self.region = None
        self.custom_base_uri = None
        self.custom_region_code = None
        self.custom_tenant_id = None
        self.india_pin_hash = None
        self.vin = None
        self.vehicles = []
        self._existing_entry = None
        self.vehicle_options = {}
        self.vehicle_label = None
        self.vehicle_type = None

        self.has_sunroof = False
        self.has_heated_seats = False
        self.has_rear_heated_seats = False
        self.has_battery_heating = False
        self.has_steering_wheel_heat = False
        self.has_window_control = False

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            self.login_type = user_input["login_type"]
            return await self.async_step_login_data()

        data_schema = vol.Schema(
            {
                vol.Required("login_type"): vol.In(["email", "phone"]),
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    async def async_step_login_data(self, user_input=None):
        errors = {}
        if user_input is not None:
            self.username = user_input["username"]
            # Strip stray whitespace: passwords are frequently pasted from a
            # password manager and a trailing space or newline otherwise causes
            # a silent, hard-to-diagnose login failure (issue #250).
            self.password = user_input["password"].strip()
            username_is_email = self.login_type == "email"

            self.region = user_input["region"]

            if not username_is_email:
                self.country_code = user_input["country_code"].replace("+", "")
                self.username = self.username.replace(" ", "").replace("+", "")

            if self.region == REGION_CUSTOM:
                return await self.async_step_custom_region()

            if self.region == REGION_INDIA:
                # MG India runs a completely different backend (TAP protocol)
                # and requires the iSmart command PIN — collect it before
                # attempting login.
                return await self.async_step_india_pin()

            try:
                await self.fetch_vehicle_data(username_is_email)
                return await self.async_step_select_vehicle()
            except NoVehiclesFoundError as e:
                errors["base"] = "no_vehicles"
                LOGGER.warning(
                    "Login succeeded but the account has no vehicles: %s", e
                )
            except Exception as e:
                errors["base"] = "auth"
                LOGGER.error(f"Failed to authenticate or fetch vehicle data: {e}")

        if self.login_type == "email":
            data_schema = vol.Schema(
                {
                    vol.Required("username"): str,
                    vol.Required("password"): PASSWORD_SELECTOR,
                    vol.Required("region"): vol.In(REGION_CHOICES),
                }
            )
        else:  # phone login
            country_options = {code["code"]: code["code"] for code in COUNTRY_CODES}
            data_schema = vol.Schema(
                {
                    vol.Required("country_code"): vol.In(country_options),
                    vol.Required("username"): str,
                    vol.Required("password"): PASSWORD_SELECTOR,
                    vol.Required("region"): vol.In(REGION_CHOICES),
                }
            )

        return self.async_show_form(
            step_id="login_data", data_schema=data_schema, errors=errors
        )

    async def async_step_custom_region(self, user_input=None):
        """Step for entering a custom SAIC API endpoint (base URI, region code, tenant ID).

        For markets running on separate SAIC infrastructure that do not have a
        built-in preset (see issue #217 for Thailand).
        """
        errors = {}
        if user_input is not None:
            self.custom_base_uri = user_input["base_uri"].strip()
            if not self.custom_base_uri.endswith("/"):
                self.custom_base_uri += "/"
            self.custom_region_code = user_input["region_code"].strip().lower()
            self.custom_tenant_id = user_input["tenant_id"].strip()

            username_is_email = self.login_type == "email"
            try:
                await self.fetch_vehicle_data(username_is_email)
                return await self.async_step_select_vehicle()
            except NoVehiclesFoundError as e:
                errors["base"] = "no_vehicles"
                LOGGER.warning(
                    "Login succeeded but the account has no vehicles: %s", e
                )
            except Exception as e:
                errors["base"] = "auth"
                LOGGER.error(f"Failed to authenticate or fetch vehicle data: {e}")

        data_schema = vol.Schema(
            {
                vol.Required(
                    "base_uri",
                    default=self.custom_base_uri
                    or "https://gateway-mg-eu.soimt.com/api.app/v1/",
                ): str,
                vol.Required(
                    "region_code", default=self.custom_region_code or "eu"
                ): str,
                vol.Required(
                    "tenant_id", default=self.custom_tenant_id or DEFAULT_TENANT_ID
                ): str,
            }
        )
        return self.async_show_form(
            step_id="custom_region", data_schema=data_schema, errors=errors
        )

    async def async_step_india_pin(self, user_input=None):
        """India-specific step: the 4-digit iSmart command PIN.

        MG India authorises remote commands with the same 4-digit PIN used in
        the iSmart India app.  Only a derived hash is stored in the config
        entry — never the raw PIN (see backends.india.hash_india_pin).
        """
        errors = {}
        if user_input is not None:
            try:
                self.india_pin_hash = hash_india_pin(user_input["pin"])
            except ValueError:
                errors["pin"] = "invalid_pin"

            if not errors:
                try:
                    await self.fetch_vehicle_data_india()
                    return await self.async_step_select_vehicle()
                except IndiaBackendNotReadyError:
                    return self.async_abort(reason="india_backend_not_ready")
                except NoVehiclesFoundError as e:
                    errors["base"] = "no_vehicles"
                    LOGGER.warning(
                        "Login succeeded but the account has no vehicles (India): %s",
                        e,
                    )
                except Exception as e:
                    errors["base"] = "auth"
                    LOGGER.error(
                        "Failed to authenticate or fetch vehicle data (India): %s",
                        e,
                    )

        data_schema = vol.Schema(
            {
                vol.Required("pin"): str,
            }
        )
        return self.async_show_form(
            step_id="india_pin", data_schema=data_schema, errors=errors
        )

    async def fetch_vehicle_data_india(self):
        """Authenticate against the MG India TAP backend and fetch vehicles.

        Uses the same backend factory as runtime setup so the config flow
        exercises exactly the code path the integration will use.

        Backend contract: get_vehicle_info() returns a list of objects with a
        `vin` attribute (matching the global client's vinList entries) or
        plain VIN strings — both are accepted here.
        """
        backend = create_backend(
            {
                "region": REGION_INDIA,
                "username": self.username,
                "password": self.password,
                "country_code": self.country_code,
                "india_pin_hash": self.india_pin_hash,
            }
        )
        try:
            await backend.login()
            vehicles = await backend.get_vehicle_info()
            if not vehicles:
                raise NoVehiclesFoundError(
                    "India vehicle list returned no vehicles"
                )
            self.vehicle_options = build_vehicle_options(vehicles)
            self.vehicles = list(self.vehicle_options)
            LOGGER.info("Fetched India vehicle data successfully.")
        finally:
            with suppress(Exception):
                await backend.close()

    async def async_step_select_vehicle(self, user_input=None):
        errors = {}
        if user_input is not None:
            selected_vin = user_input["vin"]
            self.vehicle_type = user_input["vehicle_type"]  # Store vehicle type

            self.vin = selected_vin
            self.vehicle_label = self.vehicle_options.get(selected_vin, selected_vin)
            return await self.async_step_vehicle_capabilities()

        # Filter out VINs that are already configured in HA so the user cannot
        # accidentally add the same car twice.
        already_configured = configured_vins(self.hass)
        available_vehicles = [v for v in self.vehicles if v not in already_configured]

        if not available_vehicles:
            # Every VIN on this account is already set up — nothing to add.
            return self.async_abort(reason="already_configured")

        available_vehicle_options = {
            vin: self.vehicle_options.get(vin, vin) for vin in available_vehicles
        }

        # Add vehicle_type selection with fallback for user confirmation
        data_schema = vol.Schema(
            {
                vol.Required("vin"): vol.In(available_vehicle_options),
                vol.Required("vehicle_type"): vol.In(["BEV", "PHEV", "HEV", "ICE"]),
            }
        )

        return self.async_show_form(
            step_id="select_vehicle", data_schema=data_schema, errors=errors
        )

    async def async_step_vehicle_capabilities(self, user_input=None):
        """Step for configuring vehicle capabilities."""
        errors = {}
        if user_input is not None:
            self.has_sunroof = user_input["has_sunroof"]
            self.has_heated_seats = user_input["has_heated_seats"]
            self.has_rear_heated_seats = user_input.get(
                "has_rear_heated_seats", False
            )
            self.has_battery_heating = user_input["has_battery_heating"]
            self.has_steering_wheel_heat = user_input["has_steering_wheel_heat"]
            self.has_window_control = user_input["has_window_control"]

            return self.async_create_entry(
                title=f"MG SAIC - {self.vehicle_label or self.vin}",
                data={
                    "username": self.username,
                    "password": self.password,
                    "country_code": self.country_code,
                    "region": self.region,
                    "custom_base_uri": self.custom_base_uri,
                    "region_code": self.custom_region_code,
                    "tenant_id": self.custom_tenant_id,
                    "india_pin_hash": self.india_pin_hash,
                    "vin": self.vin,
                    "login_type": self.login_type,
                    "vehicle_type": self.vehicle_type,
                    "has_sunroof": self.has_sunroof,
                    "has_heated_seats": self.has_heated_seats,
                    "has_rear_heated_seats": self.has_rear_heated_seats,
                    "has_battery_heating": self.has_battery_heating,
                    "has_steering_wheel_heat": self.has_steering_wheel_heat,
                    "has_window_control": self.has_window_control,
                },
            )

        data_schema = vol.Schema(
            {
                vol.Required("has_sunroof", default=self.has_sunroof): bool,
                vol.Required("has_heated_seats", default=self.has_heated_seats): bool,
                vol.Required(
                    "has_rear_heated_seats", default=self.has_rear_heated_seats
                ): bool,
                vol.Required(
                    "has_battery_heating", default=self.has_battery_heating
                ): bool,
                vol.Required(
                    "has_steering_wheel_heat", default=self.has_steering_wheel_heat
                ): bool,
                vol.Required(
                    "has_window_control", default=self.has_window_control
                ): bool,
            }
        )

        return self.async_show_form(
            step_id="vehicle_capabilities", data_schema=data_schema, errors=errors
        )

    # ── Re-authentication / reconfigure ─────────────────────────────────────
    #
    # Issue #250: when the iSmart password is changed, the stored one stops
    # working.  Previously the only fix was to delete and re-add the
    # integration.  async_setup_entry now raises ConfigEntryAuthFailed on a
    # credential failure, which drives Home Assistant into async_step_reauth
    # below; the user can also start async_step_reconfigure themselves at any
    # time from the integration's menu.  Both reuse every stored account
    # detail and ask only for the new password.

    async def async_step_reauth(self, entry_data=None):
        """Handle re-authentication triggered by a credential failure."""
        return await self._start_credential_update()

    async def async_step_reconfigure(self, user_input=None):
        """Handle a user-initiated password update from the entry menu."""
        return await self._start_credential_update()

    async def _start_credential_update(self):
        """Load the existing entry's account details before asking for a new
        password."""
        self._existing_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        data = self._existing_entry.data
        self.login_type = data.get("login_type")
        self.username = data.get("username")
        self.country_code = data.get("country_code")
        self.region = data.get("region")
        self.custom_base_uri = data.get("custom_base_uri")
        self.custom_region_code = data.get("region_code")
        self.custom_tenant_id = data.get("tenant_id")
        self.india_pin_hash = data.get("india_pin_hash")
        self.vin = data.get("vin")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Ask for the new password, validate it, and update the entry."""
        errors = {}
        if user_input is not None:
            # Same whitespace guard as the initial login (issue #250).
            self.password = user_input["password"].strip()
            try:
                username_is_email = self.login_type == "email"
                if self.region == REGION_INDIA:
                    await self.fetch_vehicle_data_india()
                else:
                    await self.fetch_vehicle_data(username_is_email)
            except NoVehiclesFoundError as e:  # noqa: BLE001 - surfaced as a form error
                errors["base"] = "no_vehicles"
                LOGGER.warning(
                    "Re-authentication succeeded but the account has no vehicles: %s",
                    e,
                )
            except Exception as e:  # noqa: BLE001 - surfaced as a form error
                errors["base"] = "auth"
                LOGGER.error("Re-authentication failed: %s", e)
            else:
                return self.async_update_reload_and_abort(
                    self._existing_entry,
                    data={**self._existing_entry.data, "password": self.password},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required("password"): PASSWORD_SELECTOR}
            ),
            description_placeholders={"username": self.username or ""},
            errors=errors,
        )

    async def fetch_vehicle_data(self, username_is_email):
        """Authenticate and fetch vehicle data."""

        # Get the base_url for the selected region (a custom endpoint overrides)
        base_uri = self.custom_base_uri or REGION_BASE_URIS.get(self.region)
        if not base_uri:
            raise ValueError(f"Base URL not defined for region: {self.region}")

        region_code = self.custom_region_code or REGION_API_CODES.get(
            self.region, "eu"
        )
        tenant_id = self.custom_tenant_id or DEFAULT_TENANT_ID

        config = SaicApiConfiguration(
            username=self.username,
            password=self.password,
            base_uri=base_uri,
            region=region_code,
            tenant_id=tenant_id,
            phone_country_code=self.country_code if not username_is_email else None,
            username_is_email=username_is_email,
        )

        LOGGER.debug(
            "Logging in with Username: %s, Country Code: %s, Email: %s, Region: %s "
            "(code: %s), Tenant: %s, Base URL: %s",
            self.username,
            self.country_code,
            username_is_email,
            self.region,
            region_code,
            tenant_id,
            base_uri,
        )

        # Initialize SaicApi in the executor to avoid blocking the event loop
        saic_api = await self.hass.async_add_executor_job(SaicApi, config)

        try:
            await saic_api.login()
            vehicle_list_resp = await saic_api.vehicle_list()
            LOGGER.debug("Vehicle list response: %s", vehicle_list_resp)

            if (
                not hasattr(vehicle_list_resp, "vinList")
                or not vehicle_list_resp.vinList
            ):
                raise NoVehiclesFoundError(
                    "Vehicle list API returned no vehicles"
                )

            # Now safely iterate over vinList
            self.vehicles = [car.vin for car in vehicle_list_resp.vinList]
            LOGGER.info("Fetched vehicle data successfully.")
        except Exception as e:
            LOGGER.error("Error fetching vehicle data: %s", e)
            raise

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return SAICMGOptionsFlowHandler(config_entry)


class SAICMGOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for MG SAIC integration."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.entry_id = config_entry.entry_id

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        errors = {}
        def _normalise_capacity(user_input, errors):
            """Blank clears the override; otherwise store a validated float."""
            raw = user_input.get(CONF_BATTERY_CAPACITY_OVERRIDE, "")
            if raw in (None, ""):
                user_input.pop(CONF_BATTERY_CAPACITY_OVERRIDE, None)
                return
            try:
                value = float(raw)
            except (TypeError, ValueError):
                errors[CONF_BATTERY_CAPACITY_OVERRIDE] = "capacity_invalid"
                return
            if not 1 <= value <= 250:
                errors[CONF_BATTERY_CAPACITY_OVERRIDE] = "capacity_out_of_range"
                return
            user_input[CONF_BATTERY_CAPACITY_OVERRIDE] = value

        if user_input is not None:
            errors = await self._validate_abrp(user_input)
            _normalise_capacity(user_input, errors)
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        # On first render use the saved options; on a validation error re-render
        # with the values the user just entered so nothing is lost.
        self.options = {**self.config_entry.options, **(user_input or {})}

        # Access options directly using self.options
        data_schema = vol.Schema(
            {
                # Vehicle Capabilities
                vol.Optional(
                    "has_sunroof",
                    default=self.options.get(
                        "has_sunroof",
                        self.config_entry.data.get("has_sunroof", False),
                    ),
                ): bool,
                vol.Optional(
                    "has_heated_seats",
                    default=self.options.get(
                        "has_heated_seats",
                        self.config_entry.data.get("has_heated_seats", False),
                    ),
                ): bool,
                vol.Optional(
                    "has_rear_heated_seats",
                    default=self.options.get(
                        "has_rear_heated_seats",
                        self.config_entry.data.get("has_rear_heated_seats", False),
                    ),
                ): bool,
                vol.Optional(
                    "has_battery_heating",
                    default=self.options.get(
                        "has_battery_heating",
                        self.config_entry.data.get("has_battery_heating", False),
                    ),
                ): bool,
                vol.Optional(
                    "has_steering_wheel_heat",
                    default=self.options.get(
                        "has_steering_wheel_heat",
                        self.config_entry.data.get("has_steering_wheel_heat", False),
                    ),
                ): bool,
                vol.Optional(
                    "has_window_control",
                    default=self.options.get(
                        "has_window_control",
                        self.config_entry.data.get("has_window_control", False),
                    ),
                ): bool,
                # Usable battery capacity override (kWh). Takes priority over our
                # per-model value and the API's reported capacity, and feeds the
                # Total Battery Capacity sensor and the efficiency/trip energy
                # calculations. A plain text field (so it serialises and can be
                # cleared); validated/normalised to a float on submit. Uses
                # suggested_value (not default) so blank clears the override.
                vol.Optional(
                    CONF_BATTERY_CAPACITY_OVERRIDE,
                    description={
                        "suggested_value": self.options.get(
                            CONF_BATTERY_CAPACITY_OVERRIDE, ""
                        )
                    },
                ): str,
                # A Better Route Planner (ABRP) live-data push. Both the user
                # token and the API key are user-supplied and required to enable
                # ABRP for this vehicle; clear both to disable it.
                #
                # NOTE: these use `suggested_value`, NOT `default`. With a
                # default, Home Assistant re-applies the old value when the field
                # is submitted empty, so the field can never be cleared —
                # meaning ABRP could not be turned off once set.
                vol.Optional(
                    CONF_ABRP_USER_TOKEN,
                    description={
                        "suggested_value": self.options.get(
                            CONF_ABRP_USER_TOKEN, ""
                        )
                    },
                ): str,
                vol.Optional(
                    CONF_ABRP_API_KEY,
                    description={
                        "suggested_value": self.options.get(CONF_ABRP_API_KEY, "")
                    },
                ): str,
                # Behaviour options
                vol.Optional(
                    "enable_shutdown_refresh_sequence",
                    default=self.options.get(
                        "enable_shutdown_refresh_sequence", True
                    ),
                ): bool,
                # Update Intervals in minutes
                vol.Optional(
                    "update_interval",
                    default=self.options.get(
                        "update_interval", self.get_minutes(UPDATE_INTERVAL)
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                # Holiday-mode idle interval (HOURS) and stale-data threshold
                # (HOURS). Holiday mode itself is toggled via its switch entity,
                # not here — these just tune its interval and the reachability
                # sensor's "likely asleep" threshold.
                vol.Optional(
                    CONF_HOLIDAY_UPDATE_INTERVAL,
                    default=self.options.get(
                        CONF_HOLIDAY_UPDATE_INTERVAL,
                        DEFAULT_HOLIDAY_UPDATE_INTERVAL_HOURS,
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    CONF_STALE_DATA_THRESHOLD,
                    default=self.options.get(
                        CONF_STALE_DATA_THRESHOLD,
                        DEFAULT_STALE_DATA_THRESHOLD_HOURS,
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "charging_update_interval",
                    default=self.options.get(
                        "charging_update_interval",
                        self.get_minutes(UPDATE_INTERVAL_CHARGING),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "dc_charging_update_interval",
                    default=self.options.get(
                        "dc_charging_update_interval",
                        self.get_minutes(UPDATE_INTERVAL_DC_CHARGING),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "powered_update_interval",
                    default=self.options.get(
                        "powered_update_interval",
                        self.get_minutes(UPDATE_INTERVAL_POWERED),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "after_shutdown_update_interval",
                    default=self.options.get(
                        "after_shutdown_update_interval",
                        self.get_minutes(UPDATE_INTERVAL_AFTER_SHUTDOWN),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "grace_period_update_interval",
                    default=self.options.get(
                        "grace_period_update_interval",
                        self.get_minutes(UPDATE_INTERVAL_GRACE_PERIOD),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                # After action delay in seconds
                vol.Optional(
                    "after_action_delay",
                    default=self.options.get(
                        "after_action_delay",
                        self.get_seconds(AFTER_ACTION_UPDATE_INTERVAL_DELAY),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                # Long-interval updates after actions in minutes
                vol.Optional(
                    "alarm_long_interval",
                    default=self.options.get(
                        "alarm_long_interval",
                        self.get_minutes(DEFAULT_ALARM_LONG_INTERVAL),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "ac_long_interval",
                    default=self.options.get(
                        "ac_long_interval", self.get_minutes(DEFAULT_AC_LONG_INTERVAL)
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "front_defrost_long_interval",
                    default=self.options.get(
                        "front_defrost_long_interval",
                        self.get_minutes(DEFAULT_FRONT_DEFROST_LONG_INTERVAL),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "rear_window_heat_long_interval",
                    default=self.options.get(
                        "rear_window_heat_long_interval",
                        self.get_minutes(DEFAULT_REAR_WINDOW_HEAT_LONG_INTERVAL),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "lock_unlock_long_interval",
                    default=self.options.get(
                        "lock_unlock_long_interval",
                        self.get_minutes(DEFAULT_LOCK_UNLOCK_LONG_INTERVAL),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "charging_port_lock_long_interval",
                    default=self.options.get(
                        "charging_port_lock_long_interval",
                        self.get_minutes(DEFAULT_CHARGING_PORT_LOCK_LONG_INTERVAL),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "heated_seats_long_interval",
                    default=self.options.get(
                        "heated_seats_long_interval",
                        self.get_minutes(DEFAULT_HEATED_SEATS_LONG_INTERVAL),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "battery_heating_long_interval",
                    default=self.options.get(
                        "battery_heating_long_interval",
                        self.get_minutes(DEFAULT_BATTERY_HEATING_LONG_INTERVAL),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "charging_long_interval",
                    default=self.options.get(
                        "charging_long_interval",
                        self.get_minutes(DEFAULT_CHARGING_LONG_INTERVAL),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "sunroof_long_interval",
                    default=self.options.get(
                        "sunroof_long_interval",
                        self.get_minutes(DEFAULT_SUNROOF_LONG_INTERVAL),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "tailgate_long_interval",
                    default=self.options.get(
                        "tailgate_long_interval",
                        self.get_minutes(DEFAULT_TAILGATE_LONG_INTERVAL),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "target_soc_long_interval",
                    default=self.options.get(
                        "target_soc_long_interval",
                        self.get_minutes(DEFAULT_TARGET_SOC_LONG_INTERVAL),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    "charging_current_long_interval",
                    default=self.options.get(
                        "charging_current_long_interval",
                        self.get_minutes(DEFAULT_CHARGING_CURRENT_LONG_INTERVAL),
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={"abrp_doc_url": ABRP_DOC_URL},
        )

    async def _validate_abrp(self, user_input):
        """Validate ABRP credentials when the token was set or changed.

        Returns an ``errors`` dict (empty when OK). Validation only runs when a
        token is present and either the token or the key differs from what is
        already stored, so saving unrelated options never triggers a network
        call.
        """
        errors = {}
        token = (user_input.get(CONF_ABRP_USER_TOKEN) or "").strip()
        api_key = (user_input.get(CONF_ABRP_API_KEY) or "").strip()

        # Normalise stored values back into user_input so we persist trimmed
        # strings regardless of the validation outcome.
        user_input[CONF_ABRP_USER_TOKEN] = token
        user_input[CONF_ABRP_API_KEY] = api_key

        if not token:
            return errors  # ABRP disabled — nothing to validate

        stored = self.config_entry.options
        stored_token = (stored.get(CONF_ABRP_USER_TOKEN) or "").strip()
        stored_key = (stored.get(CONF_ABRP_API_KEY) or "").strip()
        if token == stored_token and api_key == stored_key:
            return errors  # unchanged — assume still valid, skip network call

        # Both credentials are user-supplied; there is no shared default key.
        # A token without its API key means ABRP can't be enabled.
        if not api_key:
            errors["base"] = "abrp_no_api_key"
            return errors

        from .abrp import AbrpApi, AbrpAuthError, AbrpConnectionError

        session = async_get_clientsession(self.hass)
        try:
            await AbrpApi(session, api_key, token).async_validate()
        except AbrpAuthError:
            errors["base"] = "abrp_invalid_auth"
        except AbrpConnectionError:
            errors["base"] = "abrp_cannot_connect"
        except Exception:  # noqa: BLE001
            LOGGER.exception("Unexpected error validating ABRP credentials")
            errors["base"] = "abrp_unknown"
        return errors

    def get_minutes(self, interval):
        """Convert timedelta to minutes."""
        return int(interval.total_seconds() // 60)

    def get_seconds(self, interval):
        """Convert timedelta to seconds."""
        return int(interval.total_seconds())
