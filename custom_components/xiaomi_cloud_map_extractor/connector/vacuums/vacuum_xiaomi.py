import base64
import json
import logging
from dataclasses import dataclass
from typing import Self, Any

from miio.exceptions import DeviceException
from miio.miot_device import MiotDevice
from vacuum_map_parser_base.map_data import ImageData, MapData, Point, Room
from vacuum_map_parser_xiaomi.aes_decryptor import gen_md5_key
from vacuum_map_parser_xiaomi.map_data_parser import XiaomiMapDataParser
from vacuum_map_parser_xiaomi.status_mapping import get_status_mapping

from .base.model import VacuumConfig, VacuumApi
from .base.vacuum_v2 import BaseXiaomiCloudVacuumV2
from ..utils.exceptions import FailedConnectionException

_LOGGER = logging.getLogger(__name__)
OFF_UPDATES = 3
XTL_MODEL_PREFIX = "xtl.vacuum."

@dataclass
class XiaomiVacuumPropertyMapping:
    """Dataclass containing mapping for map property"""

    # vacuum map service id
    siid: int = 10

    # current map property id in vacuum map service
    piid: int = 1

_NON_STANDARD_MAP_PROP = [
    (
        [
            "xiaomi.vacuum.b108gl",
        ],
        XiaomiVacuumPropertyMapping(siid=7),
    ),
    (
        [
            "xiaomi.vacuum.b108gp",
            "xiaomi.vacuum.ov32gl",
            "xiaomi.vacuum.ov43gl",
            "xiaomi.vacuum.ov51",
            "xiaomi.vacuum.ov81",
        ],
        XiaomiVacuumPropertyMapping(siid=9),
    ),
    (
        [
            "xiaomi.vacuum.b106bk",
            "xiaomi.vacuum.b106tr",
            "xiaomi.vacuum.b112",
            "xiaomi.vacuum.b112bk",
            "xiaomi.vacuum.b112gl",
            "xiaomi.vacuum.b112tr",
            "xiaomi.vacuum.c101",
            "xiaomi.vacuum.c101eu",
            "xiaomi.vacuum.c102",
            "xiaomi.vacuum.c104",
            "xiaomi.vacuum.e101gl",
        ],
        XiaomiVacuumPropertyMapping(piid=2),
    ),
]

class XiaomiCloudVacuum(BaseXiaomiCloudVacuumV2):
    def __init__(self, vacuum_config: VacuumConfig):
        super().__init__(vacuum_config)
        self._token = vacuum_config.token
        self._host = vacuum_config.host

        self._miot_device = MiotDevice(self._host, self._token, timeout=2)

        self._xiaomi_map_data_parser = XiaomiMapDataParser(
            vacuum_config.palette,
            vacuum_config.sizes,
            vacuum_config.drawables,
            vacuum_config.image_config,
            vacuum_config.texts
        )

        self._status_mapping = get_status_mapping(self.model)
        self._off_counter = 0

        self._vacuum_map = next((mapping for models, mapping in _NON_STANDARD_MAP_PROP if self.model in models), XiaomiVacuumPropertyMapping())

    @property
    def should_update_map(self: Self) -> bool:
        if self.model.startswith(XTL_MODEL_PREFIX):
            return True

        try:
            status_value = self._miot_device.get_property_by(self._status_mapping.siid,
                                                             self._status_mapping.piid)[0]["value"]

            if status_value in self._status_mapping.idle_at:
                self._off_counter += 1
                _LOGGER.debug(
                    "Vacuum is not moving. Off counter: %d", self._off_counter)
                return self._off_counter <= OFF_UPDATES
            else:
                self._off_counter = 0
                return True
        except DeviceException as de:
            if "token" not in repr(de):
                return False
            raise FailedConnectionException(de)

    @staticmethod
    def vacuum_platform() -> VacuumApi:
        return VacuumApi.XIAOMI

    @property
    def map_archive_extension(self) -> str:
        return "zlib.enc"

    @property
    def map_data_parser(self) -> XiaomiMapDataParser:
        return self._xiaomi_map_data_parser
    
    async def get_map_name(self: Self) -> str:
        if self.model.startswith(XTL_MODEL_PREFIX):
            return await super().get_map_name()

        response = self._miot_device.get_property_by(self._vacuum_map.siid,
                                                     self._vacuum_map.piid)[0].get("value")

        if response is None:
            return super().get_map_name()

        if isinstance(response, int):
            return str(response)
        else:
            map_name = None
            try:
                map_name = json.loads(response).get("obj_name", None)
            except json.JSONDecodeError:
                if isinstance(response, str) and "/" in response:
                    map_name = response
            if map_name is None:
                return super().get_map_name()
            return map_name.split("/")[-1]

    async def get_map_url(self, map_name: str) -> str | None:
        return await self.get_fallback_map_url(map_name)

    def decode_and_parse(self, raw_map: bytes) -> MapData:
        try:
            payload = json.loads(raw_map)
            if isinstance(payload, dict) and "fields" in payload and "mapId" in payload:
                return self._decode_xtl_json_map(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

        # Try parsing as JSON first (old format), otherwise use raw data directly (new format)
        try:
            raw_map = base64.decodebytes(json.loads(raw_map)["data"].encode("latin1"))
        except (json.JSONDecodeError, KeyError, UnicodeDecodeError):
            # Data may not be JSON-wrapped
            pass
        
        raw_map = raw_map.hex()
        decoded_map = self.map_data_parser.unpack_map(
            raw_map,
            model=self.model.replace("xiaomi", "mi"),
            device_id=str(self._device_id),
        )
        return self.map_data_parser.parse(decoded_map)

    def _decode_xtl_json_map(self, payload: dict[str, Any]) -> MapData:
        fields = payload.get("fields") or []
        if len(fields) < 4:
            raise RuntimeError("XTL JSON map payload is missing required fields")

        map_field = self._json_from_b64(fields[0])
        position = self._json_from_text(fields[2]) if len(fields) > 2 else None
        rooms_meta = self._json_from_text(fields[3]) if len(fields) > 3 else []

        width = int(map_field["width"])
        height = int(map_field["height"])
        raw_map = self._lz4_block_decompress(
            base64.b64decode(map_field["map"]),
            int(map_field["lz4Len"]),
        )
        normalized_map = self.map_data_parser._normalize_json_map_pixels(raw_map)
        image, rooms_raw, cleaned_areas = self.map_data_parser._image_parser.parse(
            normalized_map,
            width,
            height,
        )
        if image is None:
            image = self.map_data_parser._image_generator.create_empty_map_image()

        y_offset = int(map_field.get("yMax", 0))
        x_origin = int(map_field.get("totalHeight", map_field.get("totalWidth", 0))) - int(map_field.get("xMin", 0)) - height + 1

        def transform(point: Point) -> Point:
            return Point(point.y - y_offset, point.x - x_origin, point.a)

        map_data = MapData(0, 1)
        map_data.image = ImageData(
            width * height,
            0,
            0,
            height,
            width,
            self._image_config,
            image,
            transform,
        )

        rooms_by_id = {
            int(room.get("room_id", room.get("id"))): room
            for room in rooms_meta
            if isinstance(room, dict) and room.get("room_id", room.get("id")) is not None
        }
        map_data.rooms = {}
        for room_number, room in rooms_raw.items():
            room_id = int(room_number) - 10 + 3
            meta = rooms_by_id.get(room_id, {})
            map_data.rooms[room_id] = Room(
                x_origin + room[1],
                y_offset + room[0],
                x_origin + room[3],
                y_offset + room[2],
                room_id,
                meta.get("name") or None,
                meta.get("centerX"),
                meta.get("centerY"),
            )
        map_data.cleaned_rooms = {int(room_number) - 10 + 3 for room_number in cleaned_areas}

        if isinstance(position, dict):
            map_data.vacuum_position = Point(
                position.get("x", 0),
                position.get("y", 0),
                self._xtl_angle(position.get("a", 0)),
            )

        charge_pos = map_field.get("chargePos")
        if charge_pos:
            charger = self._json_from_text(charge_pos)
            map_data.charger = Point(
                charger.get("x", 0),
                charger.get("y", 0),
                self._xtl_angle(charger.get("a", 0)),
            )

        if map_data.image is not None and not map_data.image.is_empty:
            self.map_data_parser._image_generator.draw_map(map_data)

        return map_data

    @staticmethod
    def _json_from_b64(value: str) -> dict[str, Any]:
        return json.loads(base64.b64decode(value).decode("utf-8"))

    @staticmethod
    def _json_from_text(value: str) -> Any:
        if not value:
            return None
        return json.loads(value)

    @staticmethod
    def _xtl_angle(value: Any) -> float:
        try:
            angle = float(value)
        except (TypeError, ValueError):
            return 0
        if abs(angle) > 360:
            angle /= 100
        return angle

    @staticmethod
    def _lz4_block_decompress(data: bytes, expected_size: int) -> bytes:
        output = bytearray()
        index = 0
        data_len = len(data)

        while index < data_len:
            token = data[index]
            index += 1

            literal_len = token >> 4
            if literal_len == 15:
                while index < data_len:
                    value = data[index]
                    index += 1
                    literal_len += value
                    if value != 255:
                        break

            output.extend(data[index:index + literal_len])
            index += literal_len
            if index >= data_len:
                break

            offset = data[index] | (data[index + 1] << 8)
            index += 2
            if offset == 0:
                raise RuntimeError("Invalid XTL LZ4 block: zero match offset")

            match_len = token & 0x0F
            if match_len == 15:
                while index < data_len:
                    value = data[index]
                    index += 1
                    match_len += value
                    if value != 255:
                        break
            match_len += 4

            start = len(output) - offset
            if start < 0:
                raise RuntimeError("Invalid XTL LZ4 block: match before output")
            for i in range(match_len):
                output.append(output[start + i])

        if len(output) != expected_size:
            raise RuntimeError(f"Invalid XTL LZ4 block size: {len(output)} != {expected_size}")
        return bytes(output)
    
    def additional_data(self: Self) -> dict[str, Any]:
        super_data = super().additional_data()
        enc_key = gen_md5_key(
            self.model.replace("xiaomi", "mi"),
            str(self._device_id),
        )

        return {**super_data, "enc_key": enc_key}
