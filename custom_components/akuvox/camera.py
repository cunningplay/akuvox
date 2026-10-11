"""Camera platform for akuvox."""

import sys

from collections.abc import Callable, Awaitable

from homeassistant.helpers import storage
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.const import ATTR_IDENTIFIERS, CONF_NAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.components.generic.camera import GenericCamera
from homeassistant.components.generic.const import (
    CONF_CONTENT_TYPE,
    CONF_FRAMERATE,
    CONF_LIMIT_REFETCH_TO_URL_CHANGE,
    CONF_STREAM_SOURCE,
    SECTION_ADVANCED,
)
from homeassistant.components.stream import CONF_RTSP_TRANSPORT

from .const import DOMAIN, LOGGER, NAME, VERSION, DATA_STORAGE_KEY


async def async_setup_entry(hass: HomeAssistant,
                            _entry,
                            async_add_devices: Callable[[list], Awaitable[None]]):
    """Set up the camera platform."""
    store = storage.Store(hass, 1, DATA_STORAGE_KEY)
    device_data = await store.async_load()

    if not device_data:
        LOGGER.error("No device data found")
        return

    cameras_data = device_data.get("camera_data")
    if not cameras_data:
        LOGGER.error("No camera data found in device data")
        return

    entities = []
    for camera_data in cameras_data:
        name = str(camera_data["name"]).strip()
        rtsp_url = str(camera_data["video_url"]).strip()
        entities.append(AkuvoxCameraEntity(
            hass=hass,
            name=name,
            rtsp_url=rtsp_url
        ))

    if async_add_devices is None:
        LOGGER.error("async_add_devices is None")
        return

    async_add_devices(entities)
    return True

class AkuvoxCameraEntity(GenericCamera):
    """Akuvox camera class."""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        rtsp_url: str) -> None:
        """Initialize the Akuvox camera class."""
        LOGGER.debug("Adding Akuvox camera '%s'", name)

        super().__init__(
            hass=hass,
            device_info={
                ATTR_IDENTIFIERS: {(DOMAIN, name)},
                CONF_NAME: name,
                CONF_STREAM_SOURCE: rtsp_url,
                CONF_CONTENT_TYPE: "",
                SECTION_ADVANCED: {
                    CONF_LIMIT_REFETCH_TO_URL_CHANGE: True,
                    CONF_FRAMERATE: 5,
                    CONF_VERIFY_SSL: False,
                    CONF_RTSP_TRANSPORT: "udp",
                },
            },
            identifier=name,
            title=name,
        )

        self._name = name
        self._rtsp_url = rtsp_url
        self._attr_unique_id = name
        self._attr_name = name

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, name)},
            name=name,
            model=VERSION,
            manufacturer=NAME,
        )


    async def stream_source(self) -> str | None:
        """Return the RTSP URL; hand go2rtc the ffmpeg form, like HA does for generic cameras.

        SmartPlus cloud relays serve RTSP over UDP only. go2rtc's native RTSP client is TCP-only and
        fails ("wrong transport"); its ffmpeg source with the "rtsp/udp" input pulls over UDP. So when
        go2rtc is the caller, return "ffmpeg:<url>#input=rtsp/udp" (HA's own HLS stream keeps the plain
        URL, as a fallback).
        """
        src = await super().stream_source()
        frame = sys._getframe(1)
        for _ in range(4):
            if frame is None:
                break
            if str(frame.f_globals.get("__name__", "")).startswith("homeassistant.components.go2rtc"):
                # go2rtc's "rtsp" ffmpeg input forces TCP ("Nonmatching transport"); "rtsp/udp" forces UDP.
                return f"ffmpeg:{src}#input=rtsp/udp" if src and not src.startswith("ffmpeg:") else src
            frame = frame.f_back
        return src
