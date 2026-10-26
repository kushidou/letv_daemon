"""遥控器实体：发送键位动作。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from homeassistant.components.remote import RemoteEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ACTION_POWER, ACTION_POWER_OFF
from .coordinator import LetvRuntimeData
from .entity import (
    LetvEntity,
    async_turn_off as power_off,
    async_turn_on as power_on,
)

_LOGGER = logging.getLogger(__name__)

MAX_REPEATS = 20


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: LetvRuntimeData = entry.runtime_data
    async_add_entities([LetvRemote(runtime)])


class LetvRemote(LetvEntity, RemoteEntity):
    """把 ``remote.send_command`` 映射到守护服务的动作名。"""

    _attr_translation_key = "remote"
    _attr_icon = "mdi:remote"

    def __init__(self, runtime: LetvRuntimeData) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry_id}_remote"

    @property
    def available(self) -> bool:
        return super().available and self.online

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

    async def async_send_command(
        self, command: Iterable[str], **kwargs
    ) -> None:
        commands = [
            str(item).strip()
            for item in command
            if str(item).strip()
        ]
        if not commands:
            raise HomeAssistantError("command 不能为空")

        repeats = kwargs.get("num_repeats", 1)
        try:
            repeats = int(repeats)
        except (TypeError, ValueError):
            repeats = 1
        if repeats < 1:
            repeats = 1
        if repeats > MAX_REPEATS:
            _LOGGER.warning(
                "num_repeats=%s 过大，已限制为 %s", repeats, MAX_REPEATS
            )
            repeats = MAX_REPEATS

        delay = kwargs.get("delay_secs")
        try:
            delay = float(delay) if delay is not None else 0.0
        except (TypeError, ValueError):
            delay = 0.0
        delay = max(0.0, min(delay, 5.0))

        for _ in range(repeats):
            for action in commands:
                await self.async_send_action(action)
                if delay:
                    await asyncio.sleep(delay)

        if any(
            action in (ACTION_POWER, ACTION_POWER_OFF)
            for action in commands
        ):
            await self.coordinator.async_force_refresh()
