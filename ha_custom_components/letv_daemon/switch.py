"""电源开关实体。"""

from __future__ import annotations

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ACTION_POWER_OFF
from .coordinator import LetvRuntimeData
from .entity import (
    LetvEntity,
    async_turn_off as power_off,
    async_turn_on as power_on,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: LetvRuntimeData = entry.runtime_data
    async_add_entities([LetvPowerSwitch(runtime)])


class LetvPowerSwitch(LetvEntity, SwitchEntity):
    """电视电源。

    ``on`` 表示屏幕点亮；**开机方向是空操作**——电视待机会断网，且
    ``power`` 是切换键，无法安全地用作开机，冷启动改由外部硬件实现。
    关机走 UDP，只要守护服务放行了 ``power_off``，实体仍然可用。
    """

    _attr_translation_key = "power"
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:television"

    def __init__(self, runtime: LetvRuntimeData) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry_id}_power"

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self.online or ACTION_POWER_OFF in self.runtime.actions

    @property
    def is_on(self) -> bool | None:
        data = self.state_data
        if data is None:
            return None
        return data.screen_on

    async def async_turn_on(self, **kwargs) -> None:
        await power_on(self)

    async def async_turn_off(self, **kwargs) -> None:
        await power_off(self)
