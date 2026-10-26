"""快捷启动按钮：每个配置的 App 一个实体。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import (
    ACTION_POWER,
    ACTION_POWER_OFF,
    LetvApiError,
    LetvApp,
    LetvConnectionError,
    LetvError,
)
from .coordinator import LetvRuntimeData
from .entity import LetvEntity, async_turn_off

_LOGGER = logging.getLogger(__name__)

# 启动后等待前台窗口切换，再强制采集一次状态。
LAUNCH_SETTLE_SECONDS = 0.8


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: LetvRuntimeData = entry.runtime_data
    entities: list[ButtonEntity] = []

    # 关机按钮优先走 UDP，电视待机 / ADB 掉线时也能用。
    if (
        ACTION_POWER_OFF in runtime.actions
        or ACTION_POWER in runtime.actions
    ):
        entities.append(LetvPowerOffButton(runtime))

    entities.extend(LetvAppButton(runtime, app) for app in runtime.apps)
    async_add_entities(entities)


class LetvPowerOffButton(LetvEntity, ButtonEntity):
    """关机按钮：不受 ``online``（ADB）状态限制。

    守护服务放行 ``power_off`` 时通过 UDP 直接关机；只有 ADB
    ``power`` 时则要求电视在线，由基类辅助函数处理。
    """

    _attr_translation_key = "power_off"
    _attr_icon = "mdi:power-off"

    def __init__(self, runtime: LetvRuntimeData) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry_id}_power_off"

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if ACTION_POWER_OFF in self.runtime.actions:
            return True
        return self.online and ACTION_POWER in self.runtime.actions

    async def async_press(self) -> None:
        await async_turn_off(self)


class LetvAppButton(LetvEntity, ButtonEntity):
    """一键打开 config.json ``apps`` 中的一个应用。"""

    _attr_icon = "mdi:application"

    def __init__(self, runtime: LetvRuntimeData, app: LetvApp) -> None:
        super().__init__(runtime)
        self._app = app
        self._attr_name = app.name
        self._attr_unique_id = (
            f"{runtime.entry_id}_launch_{app.package}"
        )

    @property
    def available(self) -> bool:
        return super().available and self.online

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "package": self._app.package,
            "activity": self._app.activity,
        }

    async def async_press(self) -> None:
        if not self.online:
            raise HomeAssistantError("电视不在线，无法启动应用")
        try:
            await self.runtime.client.async_launch(
                self._app.package, self._app.activity
            )
        except LetvApiError as err:
            raise HomeAssistantError(
                f"启动 {self._app.name} 失败：{err}"
            ) from err
        except LetvConnectionError as err:
            raise HomeAssistantError(
                f"无法连接 letv_daemon：{err}"
            ) from err
        except LetvError as err:
            raise HomeAssistantError(
                f"启动 {self._app.name} 失败：{err}"
            ) from err

        await asyncio.sleep(LAUNCH_SETTLE_SECONDS)
        await self.coordinator.async_force_refresh()
