"""当前前台应用传感器。"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
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
    async_add_entities([LetvCurrentAppSensor(runtime)])


class LetvCurrentAppSensor(LetvEntity, SensorEntity):
    """显示当前前台 App；离线时不可用。"""

    _attr_translation_key = "current_app"
    _attr_icon = "mdi:television-classic"

    def __init__(self, runtime: LetvRuntimeData) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry_id}_current_app"

    @property
    def available(self) -> bool:
        return super().available and self.online

    @property
    def native_value(self) -> str | None:
        data = self.state_data
        if data is None or not data.online:
            return None
        return data.foreground_app_name or data.foreground_package

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.state_data
        if data is None:
            return {}
        return {
            "online": data.online,
            "foreground_package": data.foreground_package,
            "foreground_activity": data.foreground_activity,
            "foreground_component": data.foreground_component,
            "screen": data.screen,
            "updated_at": data.updated_at_iso,
            "error": data.error,
        }
