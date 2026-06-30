import logging
from typing import Self, Any

from homeassistant.components.camera import Camera, CameraEntityDescription, DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONTENT_TYPE, CONF_ATTRIBUTES, CONF_STORE_MAP_RAW
from .coordinator import XiaomiCloudMapExtractorDataUpdateCoordinator
from .entity import XiaomiCloudMapExtractorEntity
from .types import XiaomiCloudMapExtractorConfigEntry
from .legacy import handle_old_config, LEGACY_PLATFORM_SCHEMA

_LOGGER = logging.getLogger(__name__)
KEY = "live_map"


PLATFORM_SCHEMA = LEGACY_PLATFORM_SCHEMA


async def async_setup_platform(
        hass: HomeAssistant,
        config: ConfigType,
        async_add_entities: AddEntitiesCallback,
        discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """YAML init: import via config flow."""
    handle_old_config(hass, config)


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
        self._configured_attributes = config_entry.options.get(CONF_ATTRIBUTES, [])
        self._store_map_raw = config_entry.options.get(CONF_STORE_MAP_RAW, False)

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
        data = self._data()
        if data is None or data.map_data is None:
            return attrs
        map_data = data.map_data
        map_values = data.as_dict()["map_data"] or {}
        rooms = {
            room_id: room.name
            for room_id, room in (map_data.rooms or {}).items()
            if room_id is not None
        }
        if not any(rooms.values()):
            rooms = list((map_data.rooms or {}).keys())
        values = {
            **map_values,
            "calibration_points": map_values.get("calibration"),
            "country": self._server,
            "goto_predicted_path": map_values.get("predicted_path"),
            "is_empty": map_data.image is None or map_data.image.is_empty,
            "room_numbers": rooms,
        }
        attrs.update(
            {
                attribute: values.get(attribute)
                for attribute in self._configured_attributes
                if attribute in values
            }
        )
        if self._store_map_raw:
            attrs["map_saved"] = self.coordinator.map_saved
        return attrs
