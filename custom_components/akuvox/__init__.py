"""Custom integration to integrate akuvox with Home Assistant.

For more details about this integration, please refer to
https://github.com/nimroddolev/akuvox
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from datetime import timedelta

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.util import dt as dt_util
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .config_flow import AkuvoxOptionsFlowHandler
from .api import AkuvoxApiClient
from .const import (
    DOMAIN,
    LOGGER
)
from .coordinator import AkuvoxDataUpdateCoordinator

PLATFORMS: list[Platform] = [
    Platform.CAMERA,
    Platform.BUTTON,
    Platform.SENSOR
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the integration's actions once (HA quality rule "action-setup"), not per config entry."""
    _async_register_services(hass)
    return True


# https://developers.home-assistant.io/docs/config_entries_index/#setting-up-an-entry
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up this integration using UI."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator = AkuvoxDataUpdateCoordinator(
        hass=hass,
        client=AkuvoxApiClient(
            session=async_get_clientsession(hass),
            hass=hass,
            entry=entry,
        ),
    )
    await async_update_configuration(hass=hass, entry=entry)

    # https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Door-log events (akuvox_door_update: calls, unlocks) poll from setup, not only after a reload.
    await coordinator.client.async_start_polling_personal_door_log()
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    await async_stop_polling(hass)
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry (options changed): the standard reload; setup restarts polling."""
    await hass.config_entries.async_reload(entry.entry_id)

# Polling

async def async_stop_polling(hass: HomeAssistant):
    """Stop polling the personal door log API."""
    api_client: AkuvoxApiClient = get_api_client(hass=hass) # type: ignore
    await api_client.async_stop_polling()

async def async_start_polling(hass: HomeAssistant):
    """Stop polling the personal door log API."""
    api_client: AkuvoxApiClient = get_api_client(hass=hass) # type: ignore
    await api_client.async_start_polling_personal_door_log()

def get_api_client(hass: HomeAssistant):
    """Akuvox API Client."""
    for _key, value in hass.data[DOMAIN].items():
        coordinator: AkuvoxDataUpdateCoordinator = value
        return coordinator.client

# Integration options

async def async_options(self, entry: ConfigEntry):
    """Present current configuration options for modification."""
    # Create an options flow handler and return it
    return AkuvoxOptionsFlowHandler(entry)

async def async_options_updated(self, entry: ConfigEntry):
    """Handle updated configuration options and update the entry."""
    # Handle the updated configuration options
    updated_options = entry.options

    # Print the updated options
    LOGGER.debug("Updated Options: %s", str(updated_options))

# Update

async def async_update_configuration(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update stored values from configuration."""
    try:
        if entry.options:
            updated_options: dict = entry.options.copy()

            # Wait for image URL?
            updated_options["wait_for_image_url"] = bool(updated_options.get("event_screenshot_options", "") == "wait")

            # Update API & data classes
            coordinator: AkuvoxDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
            client: AkuvoxApiClient = coordinator.client

            LOGGER.debug("Configured values:")
            for key, value in updated_options.items():
                #                           value=value)
                if value:
                    client.update_data(key, value)
                    if key in ["auth_token", "token", "refresh_token", "password_hash"]:
                        await client._data.async_set_stored_data_for_key(key, value)
                    str_value: str = str(value)
                    if key in ["auth_token", "token", "refresh_token", "password_hash"]:
                        length: int = len(str_value)
                        str_value = f"{str_value[0:3]}{'*'*int(length-6)}{str_value[int(length-3):length]}" # type: ignore
                    LOGGER.debug(" - %s = %s", key, str_value)
    except Exception as error:
        LOGGER.warning("Unable to update configuration: %s", str(error))


# Temporary keys (create / delete). SmartPlus has no push, so after a change the entry is reloaded:
# the key list is fetched again and the temp-key sensors are rebuilt.

SERVICE_CREATE_TEMP_KEY = "create_temp_key"
SERVICE_DELETE_TEMP_KEY = "delete_temp_key"

CREATE_TEMP_KEY_SCHEMA = vol.Schema({
    vol.Required("description"): cv.string,
    vol.Required("valid_until"): cv.datetime,
    vol.Optional("valid_from"): cv.datetime,
    vol.Optional("allowed_times", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
    # Door station names as shown in SmartPlus (e.g. "ENTRADA MAR DE BARENTS"); all doors when omitted.
    vol.Optional("doors"): vol.All(cv.ensure_list, [cv.string]),
})
DELETE_TEMP_KEY_SCHEMA = vol.Schema({vol.Required("key_id"): cv.string})


async def _async_store_fresh_keys(client: AkuvoxApiClient) -> bool:
    """Fetch the key list again and save it (the sensor platform builds its entities from storage).

    A failed fetch keeps the stored list, so a transient error never wipes the key sensors.
    """
    if not await client.async_retrieve_temp_keys_data():
        return False
    await client._data.async_set_stored_data_for_key("door_keys_data", client._data.door_keys_data)
    return True


def _loaded_entry(hass: HomeAssistant) -> ConfigEntry:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if entry.entry_id in hass.data.get(DOMAIN, {}):
            return entry
    raise ServiceValidationError("Akuvox SmartPlus is not set up")


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_CREATE_TEMP_KEY):
        return

    async def _create(call: ServiceCall) -> ServiceResponse:
        entry = _loaded_entry(hass)
        client: AkuvoxApiClient = hass.data[DOMAIN][entry.entry_id].client
        relays = client._data.door_relay_data
        wanted = [d.strip().casefold() for d in call.data.get("doors", [])]
        chosen = [r for r in relays if not wanted or str(r.get("name", "")).strip().casefold() in wanted]
        if not chosen:
            raise ServiceValidationError(f"No SmartPlus door matches {call.data.get('doors')}")
        start = dt_util.as_local(call.data.get("valid_from") or dt_util.now())
        end = dt_util.as_local(call.data["valid_until"])
        if end <= start:
            raise ServiceValidationError("valid_until must be after valid_from")
        # Snapshot the key ids first, so only the key this call created is returned (names can repeat).
        before = {str(k.get("key_id")) for k in client._data.door_keys_data}
        ok = await client.async_add_temp_key(call.data["description"],
                                            [(r["mac"], r["relay_id"]) for r in chosen],
                                            start, end, call.data["allowed_times"])
        if not ok:
            raise HomeAssistantError(f"SmartPlus did not confirm the new key: {client._last_api_error}")
        fresh = await _async_store_fresh_keys(client)
        new = [k for k in client._data.door_keys_data if str(k.get("key_id")) not in before]
        await hass.config_entries.async_reload(entry.entry_id)
        if not fresh or not new:
            # Created, but the list didn't show it yet: say so rather than return someone else's key.
            return {"created": True}
        k = max(new, key=lambda k: int(k.get("key_id") or 0))
        return {"created": True, "key_id": k.get("key_id"), "key_code": k.get("key_code"),
                "qr_code_url": k.get("qr_code_url"), "end_time": k.get("end_time")}

    async def _delete(call: ServiceCall) -> None:
        entry = _loaded_entry(hass)
        client: AkuvoxApiClient = hass.data[DOMAIN][entry.entry_id].client
        if not await client.async_delete_temp_key(call.data["key_id"]):
            raise HomeAssistantError(f"SmartPlus did not confirm deleting the key: {client._last_api_error}")
        await _async_store_fresh_keys(client)
        await hass.config_entries.async_reload(entry.entry_id)

    hass.services.async_register(DOMAIN, SERVICE_CREATE_TEMP_KEY, _create, schema=CREATE_TEMP_KEY_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_DELETE_TEMP_KEY, _delete, schema=DELETE_TEMP_KEY_SCHEMA)
