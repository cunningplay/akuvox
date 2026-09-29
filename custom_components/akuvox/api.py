"""Akuvox API Client."""
from __future__ import annotations

import asyncio
import socket
import json
import time
from typing import Any

from homeassistant.core import HomeAssistant

import aiohttp
import async_timeout
import requests

from .data import AkuvoxData
from .door_poll import DoorLogPoller

from .const import (
    LOGGER,
    REST_SERVER_ADDR,
    REST_SERVER_PORT,
    API_SEND_SMS,
    SMS_LOGIN_API_VERSION,
    API_SMS_LOGIN,
    API_SERVERS_LIST,
    API_LOGIN,
    REST_SERVER_API_VERSION,
    API_REST_SERVER_DATA,
    USERCONF_API_VERSION,
    API_USERCONF,
    OPENDOOR_API_VERSION,
    API_OPENDOOR,
    API_APP_HOST,
    API_GET_PERSONAL_TEMP_KEY_LIST,
    API_ADD_PERSONAL_TEMP_KEY,
    API_DEL_PERSONAL_TEMP_KEY,
    API_GET_PERSONAL_DOOR_LOG,
    API_REFRESH_TOKEN,
    LAST_TOKEN_REFRESH_KEY,
    TOKEN_REFRESH_INTERVAL_DAYS,
)


class AkuvoxApiClientError(Exception):
    """Exception to indicate a general API error."""


class AkuvoxApiClientCommunicationError(AkuvoxApiClientError):
    """Exception to indicate a communication error."""


class AkuvoxApiClientAuthenticationError(AkuvoxApiClientError):
    """Exception to indicate an authentication error."""

class AkuvoxApiClient:
    """Sample API Client."""

    _data: AkuvoxData = None # type: ignore
    hass: HomeAssistant
    door_log_poller: DoorLogPoller

    def __init__(
        self,
        session: aiohttp.ClientSession,
        hass: HomeAssistant,
        entry,
    ) -> None:
        """Akuvox API Client."""
        self._session = session
        self.hass = hass
        self._last_api_error: dict[str, Any] | None = None
        self._next_refresh_retry_at = 0
        if entry:
            LOGGER.debug("▶️ Initializing AkuvoxData from API client init")
            self._data = AkuvoxData(
                entry=entry,
                hass=hass) # type: ignore

    async def async_init_api(self) -> bool:
        """Initialize API configuration data."""
        stored_token = await self._data.async_get_stored_data_for_key("token")
        if stored_token and not self._data.token:
            self._data.token = stored_token

        stored_auth_token = await self._data.async_get_stored_data_for_key("auth_token")
        if stored_auth_token and not self._data.auth_token:
            self._data.auth_token = stored_auth_token

        stored_refresh_token = await self._data.async_get_stored_data_for_key("refresh_token")
        if stored_refresh_token and not self._data.refresh_token:
            self._data.refresh_token = stored_refresh_token

        stored_auth_mode = await self._data.async_get_stored_data_for_key("auth_mode")
        if stored_auth_mode and not self._data.auth_mode:
            self._data.auth_mode = stored_auth_mode

        stored_login_user = await self._data.async_get_stored_data_for_key("login_user")
        if stored_login_user and not self._data.login_user:
            self._data.login_user = stored_login_user

        stored_password_hash = await self._data.async_get_stored_data_for_key("password_hash")
        if stored_password_hash and not self._data.password_hash:
            self._data.password_hash = stored_password_hash

        if (
            self._data.auth_mode == "family_member"
            and self._data.login_user
            and self._data.password_hash
            and (not self._data.token or not self._data.host)
        ):
            if await self.async_family_member_login(
                hass=self.hass,
                login_user=self._data.login_user,
                password_hash=self._data.password_hash,
                subdomain=self._data.subdomain,
            ) is False:
                return False

        if self._data.refresh_token:
            await self.async_check_and_refresh_tokens(reason="startup")

        if self._data.host is None or len(self._data.host) == 0:
            self._data.host = "...request in process"
            if await self.async_fetch_rest_server() is False:
                return False

        return True

    async def async_start_polling(self):
        """Start polling the personal door log API."""
        self.door_log_poller: DoorLogPoller = DoorLogPoller(
            hass=self.hass,
            poll_function=self.async_retrieve_personal_door_log)
        await self.door_log_poller.async_start()

    async def async_stop_polling(self):
        """Stop polling the personal door log API (the task, and the optional poller)."""
        task = getattr(self, "_door_log_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._door_log_task = None
        poller = getattr(self, "door_log_poller", None)
        if poller is not None:
            await poller.async_stop()

    def init_api_with_data(self,
                           hass: HomeAssistant,
                           host=None,
                           subdomain=None,
                           auth_token=None,
                           token=None,
                           refresh_token=None,
                           auth_mode=None,
                           login_user=None,
                           password_hash=None,
                           phone_number=None,
                           country_code=None):
        """"Initialize values from saved data/options."""
        if not self._data:
            LOGGER.debug("▶️ Initializing AkuvoxData from API client init_api_with_data")
            self._data = AkuvoxData(
                entry=None, # type: ignore
                hass=hass,
                host=host, # type: ignore
                subdomain=subdomain, # type: ignore
                auth_token=auth_token, # type: ignore
                token=token, # type: ignore
                refresh_token=refresh_token, # type: ignore
                auth_mode=auth_mode, # type: ignore
                login_user=login_user, # type: ignore
                password_hash=password_hash, # type: ignore
                phone_number=phone_number, # type: ignore
                country_code=country_code) # type: ignore
        else:
            self._data.host = host if host is not None else self._data.host
            self._data.subdomain = subdomain if subdomain is not None else self._data.subdomain
            self._data.auth_token = auth_token if auth_token is not None else self._data.auth_token
            self._data.token = token if token is not None else self._data.token
            self._data.refresh_token = refresh_token if refresh_token is not None else self._data.refresh_token
            self._data.auth_mode = auth_mode if auth_mode is not None else self._data.auth_mode
            self._data.login_user = login_user if login_user is not None else self._data.login_user
            self._data.password_hash = password_hash if password_hash is not None else self._data.password_hash
            self._data.phone_number = phone_number if phone_number is not None else self._data.phone_number
        self.hass = self.hass if self.hass else hass

    ####################
    # API Call Methods #
    ####################

    async def async_fetch_rest_server(self):
        """Retrieve the Akuvox REST server addresses and data."""
        LOGGER.debug("📡 Fetching REST server data...")
        json_data = await self._async_api_wrapper(
            method="get",
            url=f"https://{REST_SERVER_ADDR}:{REST_SERVER_PORT}/{API_REST_SERVER_DATA}",
            data=None,
            headers={
                'api-version': REST_SERVER_API_VERSION
            }
        )
        if json_data is not None:
            LOGGER.debug("✅ REST server data received successfully")
            if self._data.parse_rest_server_response(json_data): # type: ignore
                return True
            LOGGER.error("❌ Unable to parse Akuvox server rest API data.")
        else:
            LOGGER.error("❌ Unable to fetch Akuvox server rest API data.")
        return False

    async def async_send_sms(self, hass:HomeAssistant, country_code, phone_number, subdomain):
        """Request SMS code to user's device."""
        self.init_api_with_data(
            hass=hass,
            subdomain=subdomain,
            country_code=country_code,
            phone_number=phone_number)
        if await self.async_fetch_rest_server():
            url = f"https://{self._data.host}/{API_SEND_SMS}".replace(".subdomain", f".{subdomain}")
            LOGGER.debug("url = %s", url)
            headers = {
                "Host": self._data.host,
                "Content-Type": "application/x-www-form-urlencoded",
                "X-AUTH-TOKEN": "",
                "Connection": "keep-alive",
                "Accept": "*/*",
                "User-Agent": "VBell/6.61.2 (iPhone; iOS 16.6; Scale/3.00)",
                "Accept-Language": "en-AU;q=1, he-AU;q=0.9, ru-RU;q=0.8",
                "x-cloud-lang": "en"
            }
            data = {
                "AreaCode": country_code,
                "MobileNumber": phone_number,
                "Type": 0
            }
            LOGGER.debug("📡 Requesting SMS code from subdomain %s...", subdomain)
            response = await self._async_api_wrapper(
                method="post",
                url=url,
                headers=headers,
                data=data,
            )
            if response is not None:
                if response["result"] == 0: # type: ignore
                    LOGGER.debug("✅ SMS code request successful")
                    return True

            LOGGER.error("❌ SMS code request unsuccessful. Request URL: %s", url)
        else:
            LOGGER.error("❌ Unable to initialize API. Did you login again from your device? Try logging in/adding tokens again.")
        return False

    async def async_make_servers_list_request(self,
                                              hass: HomeAssistant,
                                              auth_token: str,
                                              token: str,
                                              country_code,
                                              phone_number: str,
                                              subdomain: str = "") -> bool:
        """Request server list data."""
        self.init_api_with_data(
            hass=hass,
            subdomain=subdomain,
            auth_token=auth_token,
            token=token,
            refresh_token=self._data.refresh_token,
            country_code=country_code,
            phone_number=phone_number)
        if (self._data.host is None or len(self._data.host) == 0) and await self.async_fetch_rest_server() is False:
            return False


        url = f"https://{REST_SERVER_ADDR}:{REST_SERVER_PORT}/{API_SERVERS_LIST}"
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "x-auth-token": token,
            "api-version": "6.6",
            "x-cloud-lang": "en",
            "user-agent": "VBell/6.61.2 (iPhone; iOS 16.6; Scale/3.00)",
            "accept-language": "en-AU;q=1, he-AU;q=0.9, ru-RU;q=0.8"
        }
        obfuscated_number = str(self.get_obfuscated_phone_number(phone_number))
        data = json.dumps({
            "auth_token": auth_token,
            "passwd": auth_token,
            "token": token,
            "user": obfuscated_number,
        })
        LOGGER.debug("📡 Requesting server list...")
        json_data = await self._async_api_wrapper(
            method="post",
            url=url,
            headers=headers,
            data=data,
        )
        if json_data is not None:
            LOGGER.debug("✅ Server list retrieved successfully")
            self._data.parse_sms_login_response(json_data) # type: ignore
            await self.async_store_tokens(update_last_refresh=False)
            return True

        LOGGER.error("❌ Unable to retrieve server list. Try sigining in again / check that your tokens are valid.")
        return False

    async def async_family_member_login(
        self,
        hass: HomeAssistant,
        login_user: str,
        password_hash: str,
        subdomain: str,
    ) -> bool:
        """Sign in using the family-member email/login-passwd flow."""
        self.init_api_with_data(
            hass=hass,
            subdomain=subdomain,
            auth_mode="family_member",
            login_user=login_user,
            password_hash=password_hash,
        )
        url = (
            f"https://gate.{subdomain}.akuvox.com:{REST_SERVER_PORT}/"
            f"{API_LOGIN}?user={login_user}&passwd={password_hash}&id_code=(null)"
        )
        headers = {
            "Host": f"gate.{subdomain}.akuvox.com:{REST_SERVER_PORT}",
            "accept": "*/*",
            "x-auth-token": "",
            "api-version": "7.33",
            "user-agent": "VBell/7.40.3 (iPhone; iOS 26.4.2; Scale/3.00)",
            "priority": "u=3, i",
            "accept-language": "en-US;q=1",
        }
        json_data = await self._async_api_wrapper(
            method="get",
            url=url,
            headers=headers,
            data={},
        )
        if json_data is None:
            LOGGER.error("❌ Family-member login failed.")
            return False

        self._data.parse_sms_login_response(json_data)  # type: ignore
        if "rest_server_https" in json_data:
            self._data.host = json_data["rest_server_https"]

        await self.async_store_tokens(update_last_refresh=False)
        return True

    async def async_sms_sign_in(self, phone_number, country_code, sms_code) -> bool:
        """Sign user in with their phone number and SMS code."""

        login_data = await self.async_validate_sms_code(phone_number, country_code, sms_code)
        if login_data is not None:
            self._data.parse_sms_login_response(login_data) # type: ignore
            await self.async_store_tokens(update_last_refresh=False)

            # Retrieve connected device data
            await self.async_retrieve_device_data()
            await self.async_retrieve_temp_keys_data()

            return True

        return False

    async def async_validate_sms_code(self, phone_number, country_code, sms_code):
        """Validate the SMS code received by the user."""
        LOGGER.debug("📡 Logging in user with phone number and SMS code...")

        obfuscated_number = self.get_obfuscated_phone_number(phone_number)
        params = f"phone={obfuscated_number}&code={sms_code}&area_code={country_code}"
        url = f"https://{REST_SERVER_ADDR}:{REST_SERVER_PORT}/{API_SMS_LOGIN}?{params}"
        data = {}
        headers = {
            'api-version': SMS_LOGIN_API_VERSION,
            'User-Agent': 'VBell/6.61.2 (iPhone; iOS 16.6; Scale/3.00)'
        }
        response = await self._async_api_wrapper(method="get", url=url, headers=headers, data=data)

        if response is not None:
            LOGGER.debug("✅ Login successful")
            return response

        LOGGER.error("❌ Unable to log in with SMS code.")
        return None

    async def async_retrieve_user_data(self) -> bool:
        """Retrieve user devices and temp keys data."""
        if self._data.auth_mode == "family_member":
            # The camera stream host (rtsp_ip) is only provided by the /login
            # response. A freshly initialized client (on setup/reload) starts
            # without it, and the family-member path otherwise skips login, so
            # log in again before building camera URLs — otherwise they end up
            # with an empty host (rtsp://…@:554/<mac>).
            if (
                not self._data.rtsp_ip
                and self._data.login_user
                and self._data.password_hash
            ):
                if await self.async_family_member_login(
                    hass=self.hass,
                    login_user=self._data.login_user,
                    password_hash=self._data.password_hash,
                    subdomain=self._data.subdomain,
                ) is False:
                    return False
            if await self.async_retrieve_device_data():
                await self.async_retrieve_temp_keys_data()
                return True
            return False
        if await self.async_make_servers_list_request(
            hass=self.hass,
            auth_token=self._data.auth_token,
            token=self._data.token,
            country_code="",
            phone_number=self._data.phone_number,
            subdomain=self._data.subdomain):
            await self.async_retrieve_device_data()
            await self.async_retrieve_temp_keys_data()
            return True
        return False

    async def async_retrieve_device_data(self) -> bool:
        """Request and parse the user's device data."""
        user_conf_data = await self.async_user_conf()
        if user_conf_data is not None:
            self._data.parse_userconf_data(user_conf_data) # type: ignore
            return True
        return False

    async def async_retrieve_user_data_with_tokens(self, auth_token, token) -> bool:
        """Retrieve user devices and temp keys data with an alternate token string."""
        self._data.auth_token = auth_token
        self._data.token = token
        return await self.async_retrieve_user_data()

    async def async_refresh_token(self, reason: str = "scheduled refresh") -> bool:
        """Refresh the current Akuvox token pair using the stored refresh token."""
        if not self._data.refresh_token:
            LOGGER.warning("Akuvox token refresh skipped: no refresh_token is available.")
            return False

        url = f"https://gate.{self._data.subdomain}.akuvox.com:{REST_SERVER_PORT}/{API_REFRESH_TOKEN}"
        headers = {
            "x-auth-token": self._data.token,
            "content-type": "application/json",
            "accept": "application/json",
            "accept-language": "en-US,en;q=0.9",
            "api-version": "6.8",
            "user-agent": "VBell/7.20.5 (iPhone; iOS 26.1; Scale/2.00)",
        }
        data = json.dumps({
            "refresh_token": self._data.refresh_token,
        })

        LOGGER.info("Refreshing Akuvox credentials (%s).", reason)
        json_data = await self._async_api_wrapper(
            method="post",
            url=url,
            headers=headers,
            data=data,
        )
        if json_data is None:
            if self._last_api_error:
                if self._data.auth_mode == "family_member" and self._data.login_user and self._data.password_hash:
                    LOGGER.warning("Akuvox refresh failed for family-member session; attempting re-login.")
                    if await self.async_family_member_login(
                        hass=self.hass,
                        login_user=self._data.login_user,
                        password_hash=self._data.password_hash,
                        subdomain=self._data.subdomain,
                    ):
                        return await self.async_refresh_token(reason="post re-login refresh")
                LOGGER.error("Akuvox token refresh failed with API error: %s", self._last_api_error)
            else:
                LOGGER.error("Akuvox token refresh failed: empty response.")
            return False

        payload = json_data.get("datas", json_data) if isinstance(json_data, dict) else {}
        new_token = payload.get("token")
        new_refresh_token = payload.get("refresh_token")
        if not new_token or not new_refresh_token:
            LOGGER.error("Akuvox token refresh failed: response did not include a new token pair.")
            return False

        self._data.token = new_token
        self._data.refresh_token = new_refresh_token
        await self.async_store_tokens(update_last_refresh=True)
        self._next_refresh_retry_at = 0
        LOGGER.info("Akuvox credentials refreshed successfully.")
        LOGGER.debug(
            "Akuvox token rotation complete. token=%s refresh_token=%s",
            self._mask_token(new_token),
            self._mask_token(new_refresh_token),
        )
        return True

    async def async_user_conf(self):
        """Request the user's configuration data."""
        LOGGER.debug("📡 Retrieving list of user's devices...")
        url = f"https://{self._data.host}/{API_USERCONF}?token={self._data.token}"
        data = {}
        headers = {
            "Host": self._data.host,
            "X-AUTH-TOKEN": self._data.token,
            "Connection": "keep-alive",
            "api-version": USERCONF_API_VERSION,
            "Accept": "*/*",
            "User-Agent": "VBell/6.61.2 (iPhone; iOS 16.6; Scale/3.00)",
            "Accept-Language": "en-AU;q=1, he-AU;q=0.9, ru-RU;q=0.8",
            "x-cloud-lang": "en"
        }
        json_data = await self._async_api_wrapper(method="get", url=url, headers=headers, data=data)

        if json_data is not None:
            LOGGER.debug("✅ User's device list retrieved successfully")
            return json_data

        LOGGER.error("❌ Unable to retrieve user's device list.")
        return None

    def make_opendoor_request(self, name: str, host: str, token: str, data: str):
        """Request the user's configuration data."""
        LOGGER.debug("📡 Sending request to open door '%s'...", name)
        LOGGER.debug("Request data = %s", str(data))
        url = f"https://{host}/{API_OPENDOOR}?token={token}"
        headers = {
            "Host": host,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-AUTH-TOKEN": token,
            "api-version": OPENDOOR_API_VERSION,
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Accept": "*/*",
            "User-Agent": "VBell/6.61.2 (iPhone; iOS 16.6; Scale/3.00)",
            "Accept-Language": "en-AU;q=1, he-AU;q=0.9, ru-RU;q=0.8",
            "Content-Length": "24",
            "x-cloud-lang": "en",
        }
        response = self.post_request(url=url, headers=headers, data=data)
        json_data = self.process_response(response, url)
        if json_data is not None:
            LOGGER.debug("✅ Door open request sent successfully.")
            return json_data

        LOGGER.error("❌ Request to open door failed.")
        return None

    async def async_retrieve_temp_keys_data(self) -> bool:
        """Request and parse the user's temporary keys (keeps the old list when the request fails)."""
        self._last_api_error = None
        json_data = await self.async_get_temp_key_list()
        if json_data is not None and not self._last_api_error:
            self._data.parse_temp_keys_data(json_data)
            return True
        return False

    async def async_get_temp_key_list(self):
        """Request the user's configuration data."""
        LOGGER.debug("📡 Retrieving list of user's temporary keys...")
        host = self.get_activities_host()
        subdomain = self._data.subdomain # await self._data.async_get_stored_data_for_key("subdomain")
        url = f"https://{host}/{API_GET_PERSONAL_TEMP_KEY_LIST}"
        data = {}
        headers = {
            "x-cloud-version": "6.4",
            "accept": "application/json, text/plain, */*",
            "sec-fetch-site": "same-origin",
            "accept-language": "en-AU,en;q=0.9",
            "sec-fetch-mode": "cors",
            "x-cloud-lang": "en",
            "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) SmartPlus/6.2",
            "referer": f"https://{subdomain}.akuvox.com/smartplus/TmpKey.html?TOKEN={self._data.token}&USERTYPE=20&VERSION=6.6",
            "x-auth-token": self._data.token,
            "sec-fetch-dest": "empty"
        }

        json_data = await self._async_api_wrapper(method="get", url=url, headers=headers, data=data)

        if json_data is not None:
            LOGGER.debug("✅ User's temporary keys list retrieved successfully")
            return json_data

        LOGGER.error("❌ Unable to retrieve user's temporary key list.")
        return None

    def _temp_key_headers(self) -> dict:
        """Headers for temp-key writes (the set verified against ucloud on 2026-09-29)."""
        return {
            "x-cloud-version": "6.4",
            "x-cloud-lang": "en",
            "accept": "application/json, text/plain, */*",
            "content-type": "application/x-www-form-urlencoded",
            "x-auth-token": self._data.token,
        }

    async def _async_temp_key_post(self, path: str, data: dict) -> bool:
        """POST a temp-key form with the current stored token; on an identity error refresh once and retry.

        The in-memory token can lag the stored one (refreshes rotate it), and SmartPlus answers
        "Invalid identity information! Please login again." (code 2) to a stale token.
        """
        for attempt in (1, 2):
            stored = await self._data.async_get_stored_data_for_key("token")
            if stored:
                self._data.token = stored
            url = f"https://{self.get_activities_host()}/{path}"
            self._last_api_error = None
            try:
                result = await self._async_api_wrapper(method="post", url=url, headers=self._temp_key_headers(), data=data,
                                                       retry_other_app_type=False)
            except (AkuvoxApiClientError, AkuvoxApiClientCommunicationError) as err:
                # A timed-out POST may still have reached SmartPlus: never resend it automatically.
                self._last_api_error = {"exception": str(err)}
                return False
            # Success only on an explicit {"code": 0}: HTTP errors, rate limits and unknown bodies are failures.
            if result is not None and not self._last_api_error:
                return True
            if attempt == 1 and self.has_token_error():
                LOGGER.warning("🔁 SmartPlus rejected the token for %s; refreshing and retrying once", path)
                await self.async_refresh_token(reason="temp key request")
                continue
            return False
        return False

    async def async_add_temp_key(self, description: str, relays: list[tuple[str, str]],
                                 start, end, allowed_times: int) -> bool:
        """Create a one-time ("never repeat") temporary key for the given (MAC, relay) doors.

        Mirrors the SmartPlus web view's submit(): v3 endpoints take form fields, nested values
        flattened as MAC[i][MAC] / MAC[i][Relay] (0-based relays joined with ';').
        """
        by_mac: dict[str, list[str]] = {}
        for mac, relay in relays:
            by_mac.setdefault(str(mac).replace(":", "").upper(), []).append(str(relay))
        data = {
            "Description": description,
            "IDNumber": "",
            "SchedulerType": "3",
            "DateFlag": "",
            "StartDay": start.strftime("%Y-%m-%d"),
            "StopDay": end.strftime("%Y-%m-%d"),
            "StartTime": start.strftime("%H:%M:%S"),
            "StopTime": end.strftime("%H:%M:%S"),
            "AllowedTimes": str(int(allowed_times)),
        }
        for i, (mac, rel) in enumerate(by_mac.items()):
            data[f"MAC[{i}][MAC]"] = mac
            data[f"MAC[{i}][Relay]"] = ";".join(sorted(set(rel)))
            data[f"MAC[{i}][SecurityRelay]"] = ""
        if self._data.app_type != "single":
            data["IsFollowMyAccess"] = "1"
        if not await self._async_temp_key_post(API_ADD_PERSONAL_TEMP_KEY, data):
            LOGGER.error("❌ SmartPlus rejected the new temporary key: %s", self._last_api_error)
            return False
        LOGGER.debug("✅ Temporary key '%s' created", description)
        return True

    async def async_delete_temp_key(self, key_id: str) -> bool:
        """Delete a temporary key by its ID (the key_id attribute of its sensor)."""
        if not await self._async_temp_key_post(API_DEL_PERSONAL_TEMP_KEY, {"ID": str(key_id)}):
            LOGGER.error("❌ SmartPlus rejected deleting temporary key %s: %s", key_id, self._last_api_error)
            return False
        LOGGER.debug("✅ Temporary key %s deleted", key_id)
        return True

    async def async_start_polling_personal_door_log(self):
        """Poll the server contineously for the latest personal door log."""
        # Make sure only 1 instance of the door log polling is running
        task = getattr(self, "_door_log_task", None)
        if task is not None and not task.done():
            return
        self._door_log_task = self.hass.async_create_background_task(
            self.async_retrieve_personal_door_log(), "akuvox door log polling")

    async def async_retrieve_personal_door_log(self) -> bool:
        """Request and parse the user's door log every 2 seconds."""
        while True:
            # Get the latest pesonal door log; a network error must not end the polling task.
            try:
                json_data = await self.async_get_personal_door_log()
            except (AkuvoxApiClientError, AkuvoxApiClientCommunicationError) as err:
                LOGGER.debug("Door log poll failed: %s", err)
                json_data = None
            if json_data is not None:
                self._rate_limited = False
                new_door_log = await self._data.async_parse_personal_door_log(json_data)
                if new_door_log is not None:
                    # Fire HA event
                    LOGGER.debug("🚪 New door open event occurred. Firing akuvox_door_update event")
                    event_name = "akuvox_door_update"
                    self.hass.bus.async_fire(event_name, new_door_log)
            # SmartPlus answers {"error_msg": "Requesting too frequently"} (HTTP 429) to 2-12 s polls. Calls arrive over SIP (ha-sip), so this is only a fallback: poll every 60 s and
            # back off 5 min after a rate-limit answer (process_response logs it as an unknown response).
            await asyncio.sleep(300 if getattr(self, "_rate_limited", False) else 60)

    async def async_get_personal_door_log(self):
        """Request the user's personal door log data."""
        if self._data.refresh_token:
            await self.async_check_and_refresh_tokens(reason="door log poll")

        # LOGGER.debug("📡 Retrieving list of user's personal door log...")
        host = self.get_activities_host()
        url = f"https://{host}/{API_GET_PERSONAL_DOOR_LOG}"
        data = {}
        headers = {
            "x-cloud-version": "6.4",
            "accept": "application/json, text/plain, */*",
            "sec-fetch-site": "same-origin",
            "accept-language": "en-AU,en;q=0.9",
            "sec-fetch-mode": "cors",
            "x-cloud-lang": "en",
            "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) SmartPlus/6.2",
            "referer": f"https://{self._data.subdomain}.akuvox.com/smartplus/Activities.html?TOKEN={self._data.token}",
            "x-auth-token": self._data.token,
            "sec-fetch-dest": "empty"
        }

        json_data: list = await self._async_api_wrapper(method="get",
                                                        url=url,
                                                        headers=headers,
                                                        data=data) # type: ignore

        # Response empty, try changing app type "single" <--> "community"
        if json_data is not None and len(json_data) == 0:
            self.switch_activities_host()
            host = self.get_activities_host()
            url = f"https://{host}/{API_GET_PERSONAL_DOOR_LOG}"
            json_data = await self._async_api_wrapper(method="get",
                                                      url=url,
                                                      headers=headers,
                                                      data=data) # type: ignore

        if json_data is None and self.has_token_error():
            LOGGER.warning("Akuvox door log rejected the current token; refreshing and retrying once.")
            if await self.async_refresh_token(reason="door log rejection"):
                headers["referer"] = f"https://{self._data.subdomain}.akuvox.com/smartplus/Activities.html?TOKEN={self._data.token}"
                headers["x-auth-token"] = self._data.token
                json_data = await self._async_api_wrapper(method="get",
                                                          url=url,
                                                          headers=headers,
                                                          data=data) # type: ignore

        if json_data is not None and len(json_data) > 0:
            return json_data

        LOGGER.debug("Unable to retrieve the personal door log (rate limit, or no access for this account)")
        return None

    ###################
    # Request Methods #
    ###################

    async def _async_api_wrapper(
        self,
        method: str,
        url: str,
        data,
        headers: dict | None = None,
        retry_other_app_type: bool = True,
    ):
        """Get information from the API."""
        try:
            async with async_timeout.timeout(10):
                func = self.post_request if method == "post" else self.get_request
                subdomain = self._data.subdomain
                url = url.replace("subdomain.", f"{subdomain}.")
                if not url.endswith(API_GET_PERSONAL_DOOR_LOG):
                    LOGGER.debug("⏳ Sending request to %s", url)
                response = await self.hass.async_add_executor_job(func, url, headers, data, 10)
                return self.process_response(response, url)

        except asyncio.TimeoutError as exception:
            if not retry_other_app_type:
                raise AkuvoxApiClientCommunicationError(f"Timeout (not retried): {exception}") from exception
            # Fix for accounts which use the "single" endpoint instead of "community"
            app_type_1 = "community"
            app_type_2 = "single"
            if f"app/{app_type_1}/" in url:
                LOGGER.warning("Request 'app/%s' API %s request timed out: %s - Retry using '%s'",
                               app_type_1,
                               method,
                               url,
                               app_type_2)
                self._data.app_type = app_type_2
                url = url.replace("app/"+app_type_1+"/", "app/"+app_type_2+"/")
                return await self._async_api_wrapper(method, url, data, headers)
            if f"app/{app_type_2}/" in url:
                LOGGER.error("Timeout occured for 'app/%s' API %s request: %s",
                             app_type_2,
                             method,
                             url)
                self._data.app_type = app_type_1
            raise AkuvoxApiClientCommunicationError(
                f"Timeout error fetching information: {exception}",
            ) from exception
        except (aiohttp.ClientError, socket.gaierror) as exception:
            raise AkuvoxApiClientCommunicationError(
                f"Error fetching information: {exception}",
            ) from exception
        except Exception as exception:  # pylint: disable=broad-except
            raise AkuvoxApiClientError(
                f"Something really wrong happened! {exception}. URL = {url}"
            ) from exception
        return None

    def process_response(self, response, url):
        """Process response and return dict with data."""
        if response.status_code == 429:
            if url.endswith(API_GET_PERSONAL_DOOR_LOG) and not getattr(self, "_rate_limited", False):
                LOGGER.debug("SmartPlus rate limit (HTTP 429) on %s; backing off", url)
            if url.endswith(API_GET_PERSONAL_DOOR_LOG):
                self._rate_limited = True
            self._last_api_error = {"http_status": 429}
            return None
        if response.status_code == 200:
            # Assuming the response is valid JSON, parse it
            try:
                json_data = response.json()

                # Standard requests
                if "result" in json_data:
                    if json_data["result"] == 0:
                        self._last_api_error = None
                        if "datas" in json_data:
                            return json_data["datas"]
                        return json_data
                    self._last_api_error = json_data
                    LOGGER.warning("Akuvox API rejected %s: %s", url, json_data)
                    return None

                # Temp key requests
                if "code" in json_data:
                    if json_data["code"] == 0:
                        self._last_api_error = None
                        if "data" in json_data:
                            return json_data["data"]
                        return json_data
                    self._last_api_error = json_data
                    LOGGER.warning("Akuvox API rejected %s: %s", url, json_data)
                    return []

                if "err_code" in json_data:
                    if str(json_data["err_code"]) == "0":
                        self._last_api_error = None
                        return json_data
                    self._last_api_error = json_data
                    LOGGER.warning("Akuvox refresh API rejected %s: %s", url, json_data)
                    return None

                if "too frequently" in str(json_data.get("error_msg", "")).lower():
                    if url.endswith(API_GET_PERSONAL_DOOR_LOG):
                        self._rate_limited = True
                    self._last_api_error = json_data
                    return None
                LOGGER.warning("🤨 Response: %s", str(json_data))
                self._last_api_error = json_data
            except Exception as error:
                LOGGER.error("❌ Error occurred when parsing JSON: %s\nRequest: %s",
                             error,
                             url)
        else:
            self._last_api_error = {"http_status": response.status_code}
            LOGGER.debug("❌ Error: HTTP status code = %s for request to %s",
                         response.status_code,
                         url)
        return None

    async def async_make_get_request(self, url, headers, data=None):
        """Make an HTTP get request."""
        return await self.async_make_request("get", url, headers, data)

    async def async_make_post_request(self, url, headers, data=None):
        """Make an HTTP post request."""
        return await self.async_make_request("post", url, headers, data)

    async def async_make_request(self, request_type, url, headers, data=None):
        """Make an HTTP request."""
        func = self._session.post if request_type == "post" else self._session.get

        response = await func(url=url, headers=headers, data=data)
        if response is not None:
            if response.status == 200:
                # Assuming the response is valid JSON, parse it
                try:
                    json_data = response.json()
                    return json_data
                except Exception as error:
                    LOGGER.warning(
                        "❌ Error occurred when parsing JSON: %s", error)
            else:
                LOGGER.debug("❌ Error: HTTP status code %s",
                             response.status)
                return None

    def post_request(self, url, headers, data="", timeout=10):
        """Make a synchronous post request."""
        response: requests.Response = requests.post(url,
                                                    headers=headers,
                                                    data=data,
                                                    timeout=timeout)
        return response

    def get_request(self, url, headers, data, timeout=10):
        """Make a synchronous post request."""
        response: requests.Response = requests.get(url,
                                                   headers=headers,
                                                   data=data,
                                                   timeout=timeout)
        return response

    ###########
    # Getters #
    ###########

    def get_title(self) -> str:
        """Title of Akuvox account."""
        return self._data.project_name

    def get_devices_json(self) -> dict:
        """Device data dictionary."""
        return self._data.get_device_data()

    def get_obfuscated_phone_number(self, phone_number):
        """Obfuscate the user's phone number for API requests."""
        if (phone_number is None or len(phone_number) == 0):
            LOGGER.error("No phone number provided for obfuscation")
        # Mask phone number
        try:
            num_str = str(phone_number)
        except Exception as error:
            LOGGER.error("Unable to get obfuscated phone number from %s: %s",
                         str(phone_number),
                         str(error))
            return False
        transformed_str = ""
        # Iterate through each digit in the input number
        for digit_char in num_str:
            digit = int(digit_char)
            # Add 3 to the digit and take the result modulo 10
            transformed_digit = (digit + 3) % 10
            transformed_str += str(transformed_digit)
        return int(transformed_str)

    def get_activities_host(self):
        """Get the host address string for activities API requests."""
        if self._data.app_type == "single":
            return API_APP_HOST + "single"
        return API_APP_HOST + "community"

    def switch_activities_host(self):
        """Switch the activities host from single <--> community."""
        if self._data.app_type == "single":
            LOGGER.debug("Switching API address from 'single' to 'community'")
            self._data.app_type = "community"
        else:
            self._data.app_type = "single"
            LOGGER.debug("Switching API address from 'community' to 'single'")

    def update_data(self, key, value):
        """Update the data model."""
        self._data.subdomain = value if key == "subdomain" else self._data.subdomain
        self._data.auth_mode = value if key == "auth_mode" else self._data.auth_mode
        self._data.login_user = value if key == "login_user" else self._data.login_user
        self._data.password_hash = value if key == "password_hash" else self._data.password_hash
        self._data.auth_token = value if key == "auth_token" else self._data.auth_token
        self._data.token = value if key == "token" else self._data.token
        self._data.refresh_token = value if key == "refresh_token" else self._data.refresh_token
        self._data.wait_for_image_url = value if key == "wait_for_image_url" else self._data.wait_for_image_url

    async def async_store_tokens(self, update_last_refresh: bool) -> None:
        """Persist the active Akuvox tokens to Home Assistant storage."""
        await self._data.async_set_stored_data_for_key("auth_mode", self._data.auth_mode)
        await self._data.async_set_stored_data_for_key("login_user", self._data.login_user)
        await self._data.async_set_stored_data_for_key("password_hash", self._data.password_hash)
        await self._data.async_set_stored_data_for_key("auth_token", self._data.auth_token)
        await self._data.async_set_stored_data_for_key("token", self._data.token)
        await self._data.async_set_stored_data_for_key("refresh_token", self._data.refresh_token)
        if update_last_refresh:
            await self._data.async_set_stored_data_for_key(LAST_TOKEN_REFRESH_KEY, int(time.time()))

    def has_token_error(self) -> bool:
        """Return True when the most recent API error looks like an auth/token failure."""
        if not self._last_api_error:
            return False

        error_text = json.dumps(self._last_api_error).lower()
        return any(
            token_error in error_text
            for token_error in (
                "token invalid",
                "token expired",
                "invalid identity",
                "unauthorized",
                "\"code\": 2",
                "\"err_code\": 2",
            )
        )

    async def async_check_and_refresh_tokens(self, reason: str) -> bool:
        """Refresh the Akuvox token pair before expiry."""
        if not self._data.refresh_token:
            return True

        last_refresh = await self._data.async_get_stored_data_for_key(LAST_TOKEN_REFRESH_KEY)
        now = int(time.time())
        refresh_interval = TOKEN_REFRESH_INTERVAL_DAYS * 24 * 60 * 60

        needs_refresh = True
        if last_refresh is not None:
            try:
                needs_refresh = now - int(last_refresh) >= refresh_interval
            except (TypeError, ValueError):
                needs_refresh = True

        if not needs_refresh:
            return True

        if now < self._next_refresh_retry_at:
            return False

        refreshed = await self.async_refresh_token(reason=reason)
        if not refreshed:
            self._next_refresh_retry_at = now + 3600
        return refreshed

    def _mask_token(self, value: str) -> str:
        """Return a redacted token string for logs."""
        if len(value) <= 8:
            return "***"
        return f"{value[:4]}...{value[-4:]}"
