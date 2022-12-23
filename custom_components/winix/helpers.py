"""The Winix Air Purifier component."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging

from homeassistant.core import HomeAssistant
import requests

from custom_components.winix.device_wrapper import MyWinixDeviceStub
from winix import WinixAccount, auth

from .const import WINIX_DOMAIN

_LOGGER = logging.getLogger(__name__)
DEFAULT_POST_TIMEOUT = 5


class Helpers:
    """Utility helper class."""

    @staticmethod
    def send_notification(
        hass: HomeAssistant, notification_id: str, title: str, message: str
    ) -> None:
        """Display a persistent notification."""
        hass.async_create_task(
            hass.services.async_call(
                domain="persistent_notification",
                service="create",
                service_data={
                    "title": title,
                    "message": message,
                    "notification_id": f"{WINIX_DOMAIN}.{notification_id}",
                },
            )
        )

    @staticmethod
    async def async_login(
        hass: HomeAssistant, username: str, password: str
    ) -> auth.WinixAuthResponse:
        """Log in."""

        def _login(username: str, password: str) -> auth.WinixAuthResponse:
            """Log in."""

            _LOGGER.debug("Attempting login")

            try:
                response = auth.login(username, password)
            except Exception as err:  # pylint: disable=broad-except
                raise WinixException.from_aws_exception(err) from err

            access_token = response.access_token
            account = WinixAccount(access_token)

            # The next 2 operations can raise generic or botocore exceptions
            try:
                account.register_user(username)
                account.check_access_token()
            except Exception as err:  # pylint: disable=broad-except
                raise WinixException.from_winix_exception(err) from err

            expires_at = (datetime.now() + timedelta(seconds=3600)).timestamp()
            _LOGGER.debug("Login successful, token expires %d", expires_at)
            return response

        return await hass.async_add_executor_job(_login, username, password)

    @staticmethod
    async def async_refresh_auth(
        hass: HomeAssistant, response: auth.WinixAuthResponse
    ) -> auth.WinixAuthResponse:
        """
        Refresh authentication.

        Raises WinixException.
        """

        def _refresh(response: auth.WinixAuthResponse) -> auth.WinixAuthResponse:
            _LOGGER.debug("Attempting re-authentication")

            try:
                reponse = auth.refresh(
                    user_id=response.user_id, refresh_token=response.refresh_token
                )
            except Exception as err:  # pylint: disable=broad-except
                raise WinixException.from_aws_exception(err) from err

            account = WinixAccount(response.access_token)
            _LOGGER.debug("Attempting access token check")

            try:
                account.check_access_token()
            except Exception as err:  # pylint: disable=broad-except
                raise WinixException.from_winix_exception(err) from err

            _LOGGER.debug("Re-authentication successful")
            return reponse

        return await hass.async_add_executor_job(_refresh, response)

    @staticmethod
    async def async_get_device_stubs(hass: HomeAssistant, access_token: str):
        """
        Get device list.

        Raises WinixException.
        """

        # Modified from https://github.com/hfern/winix to support additional attributes.
        def get_device_info_list(access_token: str):
            # pylint: disable=line-too-long

            # com.google.gson.k kVar = new com.google.gson.k();
            # kVar.p("accessToken", deviceMainActivity2.f2938o);
            # kVar.p("uuid", Common.w(deviceMainActivity2.f2934k));
            # new com.winix.smartiot.util.o0(deviceMainActivity2.f2934k, "https://us.mobile.winix-iot.com/getDeviceInfoList", kVar).a(new TypeToken<g4.v>() {
            #  // from class: com.winix.smartiot.activity.DeviceMainActivity.9
            # }, new com.winix.smartiot.activity.d(deviceMainActivity2, 4));

            resp = requests.post(
                "https://us.mobile.winix-iot.com/getDeviceInfoList",
                json={
                    "accessToken": access_token,
                    "uuid": WinixAccount(access_token).get_uuid(),
                },
                timeout=DEFAULT_POST_TIMEOUT,
            )

            if resp.status_code != 200:
                err_data = resp.json()
                result_code = err_data.get("resultCode")
                result_message = err_data.get("resultMessage")

                if result_code and result_message:
                    raise Exception(
                        f"Error while performing RPC get_device_info_list ({result_code}): {result_message}"
                    )

                raise Exception(
                    f"Error while performing RPC get_device_info_list ({err_data.result_code}): {resp.text}"
                )

            return [
                MyWinixDeviceStub(
                    id=d["deviceId"],
                    mac=d["mac"],
                    alias=d["deviceAlias"],
                    location_code=d["deviceLocCode"],
                    filter_replace_date=d["filterReplaceDate"],
                    model=d["modelName"],
                    sw_version=d["mcuVer"],
                )
                for d in resp.json()["deviceInfoList"]
            ]

        try:
            _LOGGER.debug("Obtaining device list")
            return await hass.async_add_executor_job(get_device_info_list, access_token)
        except Exception as err:
            raise WinixException.from_winix_exception(err) from err


class WinixException(Exception):
    """Wiinx related operation exception."""

    def __init__(self, values: dict):
        """Create instance of WinixException."""

        super().__init__(values["message"])

        self.result_code: str = values.get("result_code", "")
        """Error code."""
        self.result_message: str = values.get("result_message", "")
        """Error code message."""

    @staticmethod
    def from_winix_exception(err: Exception):
        """Build exception for Winix library operation."""
        return WinixException(WinixException.parse_winix_exception(err))

    @staticmethod
    def from_aws_exception(err: Exception):
        """Build exception for AWS operation."""
        return WinixException(WinixException.parse_aws_exception(err))

    @staticmethod
    def parse_winix_exception(err: Exception):
        """Parse Winix library exception message."""

        message = str(err)
        if message.find(":") == -1:
            return {"message": message}

        pcs = message.partition(":")
        if pcs[0].rfind("(") == -1:
            return {"message": message}

        pcs2 = pcs[0].rpartition("(")
        return {
            "message": message,
            "result_code": pcs2[2].rstrip(")"),
            "result_message": pcs[2],
        }

    @staticmethod
    def parse_aws_exception(err: Exception):
        """Parse AWS operation exception."""
        message = str(err)

        # https://stackoverflow.com/questions/60703127/how-to-catch-botocore-errorfactory-usernotfoundexception
        try:
            response = err.response
            if response:
                return {
                    "message": message,
                    "result_code": response.get("Error", {}).get("Code"),
                }

            return None
        except AttributeError:
            return {"message": message}