"""电视媒体播放器实体：音量步进与静音。

设备只暴露遥控按键，没有绝对音量 / 静音状态回读：

* 音量只能整步增减，``volume_level`` 始终未知；
* 静音是切换键，实体内部维护一个乐观状态，HA 重启后重新变为未知。
"""

from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import (
    ACTION_MUTE,
    ACTION_VOLUME_DOWN,
    ACTION_VOLUME_UP,
)
from .coordinator import LetvRuntimeData
from .entity import LetvEntity
from .sources import available_sources


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: LetvRuntimeData = entry.runtime_data
    async_add_entities([LetvMediaPlayer(runtime)])


class LetvMediaPlayer(LetvEntity, MediaPlayerEntity):
    """音量与静音控制；电源由 switch 实体负责。"""

    _attr_name = None

    def __init__(self, runtime: LetvRuntimeData) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry_id}_media_player"
        self._muted: bool | None = None

        self._sources = available_sources(runtime.actions)
        self._attr_source = None
        self._attr_source_list = list(self._sources) or None

        features = MediaPlayerEntityFeature(0)
        if {
            ACTION_VOLUME_UP,
            ACTION_VOLUME_DOWN,
        } <= runtime.actions.keys():
            features |= MediaPlayerEntityFeature.VOLUME_STEP
        if ACTION_MUTE in runtime.actions:
            features |= MediaPlayerEntityFeature.VOLUME_MUTE
        if self._sources:
            features |= MediaPlayerEntityFeature.SELECT_SOURCE
        self._attr_supported_features = features

    @property
    def available(self) -> bool:
        return super().available and self.online

    @property
    def state(self) -> MediaPlayerState | None:
        data = self.state_data
        if data is None or data.screen_on is None:
            return None
        return MediaPlayerState.ON if data.screen_on else MediaPlayerState.OFF

    @property
    def is_volume_muted(self) -> bool | None:
        return self._muted

    async def async_volume_up(self) -> None:
        await self._async_require_action(ACTION_VOLUME_UP)
        await self.async_send_action(ACTION_VOLUME_UP)

    async def async_volume_down(self) -> None:
        await self._async_require_action(ACTION_VOLUME_DOWN)
        await self.async_send_action(ACTION_VOLUME_DOWN)

    async def async_mute_volume(self, mute: bool) -> None:
        await self._async_require_action(ACTION_MUTE)

        if mute:
            if self._muted is True:
                return
            await self.async_send_action(ACTION_MUTE)
            self._muted = True
        else:
            if self._muted is not True:
                # 状态未知时假定当前未静音，不盲发切换键。
                self._muted = False
                return
            await self.async_send_action(ACTION_MUTE)
            self._muted = False

        self.async_write_ha_state()

    async def async_select_source(self, source: str) -> None:
        action = self._sources.get(source)
        if action is None:
            raise HomeAssistantError(
                f"不受支持的信号源：{source}；"
                f"可选值：{', '.join(self._sources)}"
            )
        await self.async_send_action(action)
        self._attr_source = source
        self.async_write_ha_state()

    async def _async_require_action(self, action: str) -> None:
        if not self.online:
            raise HomeAssistantError("电视不在线，无法发送按键")
        if action not in self.runtime.actions:
            raise HomeAssistantError(
                f"守护服务未提供动作 {action}；请确认 config.json 中 "
                "allow_dangerous_actions=true"
            )
