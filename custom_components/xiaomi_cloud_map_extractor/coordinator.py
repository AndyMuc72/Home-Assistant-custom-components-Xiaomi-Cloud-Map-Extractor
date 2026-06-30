import logging
from datetime import timedelta
from pathlib import Path
from typing import Self

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .connector import XiaomiCloudMapExtractorConnector
from .connector.model import XiaomiCloudMapExtractorData
from .connector.utils.exceptions import (
    XiaomiCloudMapExtractorException,
    FailedLoginException,
    TwoFactorAuthRequiredException,
    InvalidDeviceTokenException,
    InvalidCredentialsException,
    CaptchaRequiredException,
    DeviceNotFoundException,
)
from .const import DOMAIN, DEFAULT_UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class XiaomiCloudMapExtractorDataUpdateCoordinator(DataUpdateCoordinator[XiaomiCloudMapExtractorData]):

    def __init__(
            self: Self,
            hass: HomeAssistant,
            connector: XiaomiCloudMapExtractorConnector,
            update_interval_seconds: float | None = None,
            store_map_raw: bool = False,
            store_map_image: bool = False,
            store_map_path: str = "",
            model: str = "vacuum",
    ) -> None:
        self.connector = connector
        self._store_map_raw = store_map_raw
        self._store_map_image = store_map_image
        self._store_map_path = store_map_path
        self._model = model.replace("/", "_").replace("\\", "_")
        self.map_saved = False
        update_interval = (
            timedelta(seconds=update_interval_seconds)
            if update_interval_seconds is not None
            else DEFAULT_UPDATE_INTERVAL
        )
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval,
                         update_method=self.update_data)

    async def update_data(self: Self) -> XiaomiCloudMapExtractorData:
        try:
            data = await self.connector.get_data()
            if self._store_map_raw or self._store_map_image:
                self.map_saved = await self.hass.async_add_executor_job(
                    self._store_map_data, data
                )
            return data
        except (
                FailedLoginException,
                InvalidCredentialsException,
                InvalidDeviceTokenException,
                TwoFactorAuthRequiredException,
                CaptchaRequiredException,
                DeviceNotFoundException,
        ) as err:
            _LOGGER.error(err)
            _LOGGER.debug("Triggering reauth flow...")
            raise ConfigEntryAuthFailed(err) from err
        except XiaomiCloudMapExtractorException as err:
            _LOGGER.error(err)
            raise UpdateFailed(err) from err

    async def force_update_data(self) -> None:
        self.connector.force_refresh()
        await self.async_request_refresh()

    async def set_auto_updating(self, updating: bool) -> None:
        self.connector.set_auto_updating(updating)

    def is_auto_updating(self) -> bool:
        return self.connector.is_auto_updating()

    def _store_map_data(self, data: XiaomiCloudMapExtractorData) -> bool:
        if not self._store_map_path:
            _LOGGER.warning("Map storage is enabled, but store_map_path is empty")
            return False
        try:
            target = Path(self._store_map_path)
            target.mkdir(parents=True, exist_ok=True)
            if self._store_map_raw and data.map_data_raw is not None:
                extension = "json" if data.map_data_raw.lstrip().startswith(b"{") else "bin"
                (target / f"map_data_{self._model}.{extension}").write_bytes(data.map_data_raw)
            if self._store_map_image and data.map_image is not None:
                (target / f"map_image_{self._model}.png").write_bytes(data.map_image)
            return True
        except OSError:
            _LOGGER.warning("Unable to store map files in %s", self._store_map_path, exc_info=True)
            return False
