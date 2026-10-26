"""信号源选择实体。

切源走 UDP 9900，不依赖 ADB；因此即使电视的 ADB 暂时不可用，
只要守护服务可达且放行了 ``switch_*`` 动作，该实体仍可用。
设备无法回读当前信号源，选择成功后按乐观状态显示。
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import LetvRuntimeData
from .entity import LetvEntity
from .sources import available_sources


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: LetvRuntimeData = entry.runtime_data
    if available_sources(runtime.actions):
        async_add_entities([LetvInputSourceSelect(runtime)])


class LetvInputSourceSelect(LetvEntity, SelectEntity):
    """把 ``switch_*`` UDP 动作做成一个信号源下拉框。"""

    _attr_translation_key = "input_source"
    _attr_icon = "mdi:video-input-hdmi"

    def __init__(self, runtime: LetvRuntimeData) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry_id}_input_source"
        self._sources = available_sources(runtime.actions)
        self._attr_options = list(self._sources)
        self._attr_current_option = None

    @property
    def available(self) -> bool:
        # 切源不需要 ADB，只要守护服务可达即可。
        return super().available and bool(self._sources)

    async def async_select_option(self, option: str) -> None:
        action = self._sources.get(option)
        if action is None:
            raise HomeAssistantError(
                f"不受支持的信号源：{option}；"
                f"可选值：{', '.join(self._attr_options)}"
            )
        await self.async_send_action(action)
        self._attr_current_option = option
        self.async_write_ha_state()
