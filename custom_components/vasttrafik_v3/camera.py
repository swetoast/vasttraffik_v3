"""Camera platform — still images from commuter parking that has cameras.

Images are fetched only when something asks for one; nothing is polled.
"""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import VtjpAdapter
from .const import DOMAIN
from .sensor import device_info_for_parking

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    entities = [
        VasttrafikParkingCamera(store["api"], area, lot, camera_id, number, entry.entry_id)
        for area in (store["parking"].data or {}).values()
        for lot in area["lots"]
        for number, camera_id in enumerate(lot["cameras"], start=1)
    ]
    if entities:
        async_add_entities(entities)


class VasttrafikParkingCamera(Camera):
    _attr_has_entity_name = True
    _attr_translation_key = "parking_camera"
    _attr_attribution     = "Data provided by Västtrafik"

    def __init__(
        self, api: VtjpAdapter, area: dict, lot: dict, camera_id: int, number: int, entry_id: str
    ) -> None:
        super().__init__()
        self._api = api
        self._lot_id = lot["id"]
        self._camera_id = camera_id
        label = f"{lot['name']} {number}" if len(area["lots"]) > 1 and lot.get("name") else str(number)
        self._attr_translation_placeholders = {"label": label}
        self._attr_unique_id   = f"{entry_id}_parkcam_{lot['id']}_{camera_id}"
        self._attr_device_info = device_info_for_parking(entry_id, area)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        try:
            image = await self.hass.async_add_executor_job(
                self._api.parking_image, self._lot_id, self._camera_id
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Parking image failed for %s/%s: %s", self._lot_id, self._camera_id, exc)
            return None
        if image is None:
            return None
        self.content_type = image[1].split(";")[0].strip() or "image/jpeg"
        return image[0]
