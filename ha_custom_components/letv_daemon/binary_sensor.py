"""电视在线状态二进制传感器。"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import LetvRuntimeData
from .entity import LetvEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: LetvRuntimeData = entry.runtime_data
    async_add_entities([LetvOnlineBinarySensor(runtime)])


class LetvOnlineBinarySensor(LetvEntity, BinarySensorEntity):
    """守护服务是否仍能连上电视。"""

    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, runtime: LetvRuntimeData) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry_id}_online"

    @property
    def is_on(self) -> bool:
        data = self.state_data
        return bool(data is not None and data.online)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.state_data
        if data is None:
            return {}
        return {
            "updated_at": data.updated_at_iso,
            "error": data.error,
        }
