import logging
from pathlib import Path
import re
from typing import Self, Any

from homeassistant.components.camera import Camera, CameraEntityDescription, DOMAIN
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.exceptions import PlatformNotReady
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.issue_registry import async_delete_issue
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONTENT_TYPE, DOMAIN as INTEGRATION_DOMAIN
from .connector import XiaomiCloudMapExtractorConnector
from .connector.utils.exceptions import CaptchaRequiredException
from .coordinator import XiaomiCloudMapExtractorDataUpdateCoordinator
from .entity import XiaomiCloudMapExtractorEntity
from .types import XiaomiCloudMapExtractorConfigEntry
from .legacy import (
    LEGACY_ATTRIBUTE_CALIBRATION,
    LEGACY_ATTRIBUTE_CARPET_MAP,
    LEGACY_ATTRIBUTE_CHARGER,
    LEGACY_ATTRIBUTE_CLEANED_ROOMS,
    LEGACY_ATTRIBUTE_COUNTRY,
    LEGACY_ATTRIBUTE_GOTO,
    LEGACY_ATTRIBUTE_GOTO_PATH,
    LEGACY_ATTRIBUTE_GOTO_PREDICTED_PATH,
    LEGACY_ATTRIBUTE_IGNORED_OBSTACLES,
    LEGACY_ATTRIBUTE_IGNORED_OBSTACLES_WITH_PHOTO,
    LEGACY_ATTRIBUTE_IMAGE,
    LEGACY_ATTRIBUTE_IS_EMPTY,
    LEGACY_ATTRIBUTE_MAP_NAME,
    LEGACY_ATTRIBUTE_MAP_SAVED,
    LEGACY_ATTRIBUTE_MOP_PATH,
    LEGACY_ATTRIBUTE_NO_CARPET_AREAS,
    LEGACY_ATTRIBUTE_NO_GO_AREAS,
    LEGACY_ATTRIBUTE_NO_MOPPING_AREAS,
    LEGACY_ATTRIBUTE_OBSTACLES,
    LEGACY_ATTRIBUTE_OBSTACLES_WITH_PHOTO,
    LEGACY_ATTRIBUTE_PATH,
    LEGACY_ATTRIBUTE_ROOMS,
    LEGACY_ATTRIBUTE_ROOM_NUMBERS,
    LEGACY_ATTRIBUTE_VACUUM_POSITION,
    LEGACY_ATTRIBUTE_VACUUM_ROOM,
    LEGACY_ATTRIBUTE_VACUUM_ROOM_NAME,
    LEGACY_ATTRIBUTE_WALLS,
    LEGACY_ATTRIBUTE_ZONES,
    LEGACY_CONF_ATTRIBUTES,
    LEGACY_CONF_AUTO_UPDATE,
    LEGACY_CONF_COUNTRY,
    LEGACY_CONF_STORE_MAP_IMAGE,
    LEGACY_CONF_STORE_MAP_PATH,
    LEGACY_CONF_STORE_MAP_RAW,
    LEGACY_PLATFORM_SCHEMA,
    create_yaml_runtime_configuration,
)

_LOGGER = logging.getLogger(__name__)
KEY = "live_map"


PLATFORM_SCHEMA = LEGACY_PLATFORM_SCHEMA


async def async_setup_platform(
        hass: HomeAssistant,
        config: ConfigType,
        async_add_entities: AddEntitiesCallback,
        discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up a camera directly from YAML."""
    async_delete_issue(
        hass,
        INTEGRATION_DOMAIN,
        f"deprecated_yaml_{INTEGRATION_DOMAIN}",
    )

    def session_creator():
        return async_create_clientsession(hass)

    try:
        runtime = await create_yaml_runtime_configuration(
            hass,
            config,
            session_creator,
        )
        connector = XiaomiCloudMapExtractorConnector(
            session_creator,
            runtime.connector,
            runtime.cloud,
        )
        connector.set_auto_updating(config[LEGACY_CONF_AUTO_UPDATE])
        coordinator = XiaomiCloudMapExtractorDataUpdateCoordinator(hass, connector)
        if scan_interval := config.get(CONF_SCAN_INTERVAL):
            coordinator.update_interval = scan_interval
        await coordinator.async_refresh()
        if not coordinator.last_update_success:
            raise PlatformNotReady("Initial Xiaomi map update failed")
    except CaptchaRequiredException:
        raise PlatformNotReady(
            "Xiaomi Cloud requested a CAPTCHA. Add the device MAC to the YAML "
            "configuration so the stored UI session can be reused."
        ) from None
    except Exception as err:
        raise PlatformNotReady(
            f"Unable to initialize Xiaomi Cloud Map Extractor YAML camera: {err}"
        ) from err

    async_add_entities(
        [
            XiaomiCloudMapExtractorYamlCamera(
                coordinator,
                runtime.name,
                runtime.connector.model,
                config,
            )
        ]
    )


async def async_setup_entry(
        hass: HomeAssistant,
        config_entry: XiaomiCloudMapExtractorConfigEntry,
        async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = config_entry.runtime_data.coordinator
    async_add_entities([XiaomiCloudMapExtractorCamera(coordinator, config_entry)])


class XiaomiCloudMapExtractorCamera(XiaomiCloudMapExtractorEntity, Camera):

    def __init__(
            self: Self,
            coordinator: XiaomiCloudMapExtractorDataUpdateCoordinator,
            config_entry: XiaomiCloudMapExtractorConfigEntry
    ) -> None:
        XiaomiCloudMapExtractorEntity.__init__(self, coordinator, config_entry, DOMAIN, KEY)
        Camera.__init__(self)
        self.content_type = CONTENT_TYPE
        self.entity_description = CameraEntityDescription(
            key=KEY,
            translation_key=KEY,
            entity_registry_enabled_default=False,
            entity_registry_visible_default=False,
        )

    @property
    def frame_interval(self: Self) -> float:
        return 0.2

    def camera_image(self: Self, width: int | None = None, height: int | None = None) -> bytes | None:
        data = self._data()
        if data is None:
            return None
        return data.map_image

    @property
    def extra_state_attributes(self: Self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        if (map_data := self._map_data()) is not None:
            attrs["calibration_points"] = map_data.calibration()
            attrs["rooms"] = {k: v.as_dict() for k, v in (map_data.rooms or {}).items()}
        return attrs


class XiaomiCloudMapExtractorYamlCamera(
    CoordinatorEntity[XiaomiCloudMapExtractorDataUpdateCoordinator],
    Camera,
):
    """Camera configured and managed directly through configuration.yaml."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: XiaomiCloudMapExtractorDataUpdateCoordinator,
        name: str,
        model: str,
        config: ConfigType,
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._attr_name = name
        self.content_type = CONTENT_TYPE
        self._model = model
        self._country = config[LEGACY_CONF_COUNTRY]
        self._attributes = config[LEGACY_CONF_ATTRIBUTES]
        self._store_map_raw = config[LEGACY_CONF_STORE_MAP_RAW]
        self._store_map_image = config[LEGACY_CONF_STORE_MAP_IMAGE]
        self._store_map_path = config[LEGACY_CONF_STORE_MAP_PATH]
        self._map_saved: bool | None = None

    @property
    def frame_interval(self) -> float:
        return 0.2

    def camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        data = self.coordinator.data
        return data and data.map_image

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_store_map()

    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        if self.hass:
            self.hass.async_create_task(self._async_store_map())

    async def _async_store_map(self) -> None:
        if not self._store_map_raw and not self._store_map_image:
            return
        data = self.coordinator.data
        if data is None:
            return
        target = Path(self._store_map_path or self.hass.config.path())
        filename = re.sub(r"[^A-Za-z0-9_.-]", "_", self._model)

        def write_files() -> bool:
            target.mkdir(parents=True, exist_ok=True)
            if self._store_map_raw and data.map_data_raw:
                (target / f"map_data_{filename}.raw").write_bytes(data.map_data_raw)
            if self._store_map_image and data.map_image:
                (target / f"map_image_{filename}.png").write_bytes(data.map_image)
            return True

        try:
            self._map_saved = await self.hass.async_add_executor_job(write_files)
        except OSError:
            self._map_saved = False
            _LOGGER.exception("Unable to store Xiaomi map files in %s", target)

    @staticmethod
    def _as_dict(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            return {
                key: XiaomiCloudMapExtractorYamlCamera._as_dict(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, set, tuple)):
            return [
                XiaomiCloudMapExtractorYamlCamera._as_dict(item)
                for item in value
            ]
        if hasattr(value, "as_dict"):
            return value.as_dict()
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if data is None or data.map_data is None:
            return {}
        map_data = data.map_data
        rooms = {
            room_id: room.name
            for room_id, room in (map_data.rooms or {}).items()
            if room.name
        } or list((map_data.rooms or {}).keys())
        available = {
            LEGACY_ATTRIBUTE_CALIBRATION: map_data.calibration(),
            LEGACY_ATTRIBUTE_CARPET_MAP: map_data.carpet_map,
            LEGACY_ATTRIBUTE_CHARGER: map_data.charger,
            LEGACY_ATTRIBUTE_CLEANED_ROOMS: map_data.cleaned_rooms,
            LEGACY_ATTRIBUTE_COUNTRY: self._country,
            LEGACY_ATTRIBUTE_GOTO: map_data.goto,
            LEGACY_ATTRIBUTE_GOTO_PATH: map_data.goto_path,
            LEGACY_ATTRIBUTE_GOTO_PREDICTED_PATH: map_data.predicted_path,
            LEGACY_ATTRIBUTE_IGNORED_OBSTACLES: map_data.ignored_obstacles,
            LEGACY_ATTRIBUTE_IGNORED_OBSTACLES_WITH_PHOTO: map_data.ignored_obstacles_with_photo,
            LEGACY_ATTRIBUTE_IMAGE: map_data.image and map_data.image.as_dict(),
            LEGACY_ATTRIBUTE_IS_EMPTY: map_data.image is None or map_data.image.is_empty,
            LEGACY_ATTRIBUTE_MAP_NAME: map_data.map_name,
            LEGACY_ATTRIBUTE_MOP_PATH: map_data.mop_path,
            LEGACY_ATTRIBUTE_NO_CARPET_AREAS: map_data.no_carpet_areas,
            LEGACY_ATTRIBUTE_NO_GO_AREAS: map_data.no_go_areas,
            LEGACY_ATTRIBUTE_NO_MOPPING_AREAS: map_data.no_mopping_areas,
            LEGACY_ATTRIBUTE_OBSTACLES: map_data.obstacles,
            LEGACY_ATTRIBUTE_OBSTACLES_WITH_PHOTO: map_data.obstacles_with_photo,
            LEGACY_ATTRIBUTE_PATH: map_data.path,
            LEGACY_ATTRIBUTE_ROOM_NUMBERS: rooms,
            LEGACY_ATTRIBUTE_ROOMS: map_data.rooms,
            LEGACY_ATTRIBUTE_VACUUM_POSITION: map_data.vacuum_position,
            LEGACY_ATTRIBUTE_VACUUM_ROOM: map_data.vacuum_room,
            LEGACY_ATTRIBUTE_VACUUM_ROOM_NAME: map_data.vacuum_room_name,
            LEGACY_ATTRIBUTE_WALLS: map_data.walls,
            LEGACY_ATTRIBUTE_ZONES: map_data.zones,
        }
        attrs = {
            key: self._as_dict(value)
            for key, value in available.items()
            if key in self._attributes
        }
        if self._store_map_raw:
            attrs[LEGACY_ATTRIBUTE_MAP_SAVED] = self._map_saved
        if data.last_update_timestamp:
            attrs["last_update_timestamp"] = data.last_update_timestamp
        if data.last_successful_update_timestamp:
            attrs["last_successful_update_timestamp"] = data.last_successful_update_timestamp
        return attrs
