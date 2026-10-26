"""所有实体的公共基类与动作辅助函数。"""

from __future__ import annotations

import logging

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import (
    ACTION_POWER,
    ACTION_POWER_OFF,
    LetvApiError,
    LetvConnectionError,
    LetvError,
    LetvForbiddenError,
    LetvState,
)
from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import LetvCoordinator, LetvRuntimeData

_LOGGER = logging.getLogger(__name__)


class LetvEntity(CoordinatorEntity[LetvCoordinator]):
    """把设备信息、运行时数据与错误转换集中处理。"""

    _attr_has_entity_name = True

    def __init__(self, runtime: LetvRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self.runtime = runtime
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.entry_id)},
            name=runtime.name,
            manufacturer=MANUFACTURER,
            model=MODEL,
            configuration_url=runtime.client.base_url,
        )

    @property
    def state_data(self) -> LetvState | None:
        return self.coordinator.data

    @property
    def online(self) -> bool:
        data = self.coordinator.data
        return bool(data is not None and data.online)

    async def async_send_action(self, action: str) -> None:
        """发送一个动作，并把守护服务的错误翻译成 HA 错误。"""
        try:
            await self.runtime.client.async_send_key(action)
        except LetvForbiddenError as err:
            raise HomeAssistantError(
                f"动作 {action} 被守护服务拒绝：{err}"
            ) from err
        except LetvApiError as err:
            raise HomeAssistantError(
                f"发送动作 {action} 失败：{err}"
            ) from err
        except LetvConnectionError as err:
            raise HomeAssistantError(
                f"无法连接 letv_daemon：{err}"
            ) from err
        except LetvError as err:
            raise HomeAssistantError(
                f"发送动作 {action} 失败：{err}"
            ) from err


async def async_turn_on(entity: LetvEntity) -> None:
    """开机 / 唤醒（当前空操作）。

    电视待机会断开 Wi-Fi，ADB 与 UDP 都无法冷启动；而 ``power`` 又是
    待机/唤醒切换键，在屏幕状态未知时发送反而可能把已经开机的电视关掉。
    开机将由独立硬件（WoL / 红外 / 继电器等）实现，这里刻意不发送任何
    命令。
    """
    _LOGGER.debug(
        "忽略开机请求：%s 的开机由外部硬件实现",
        entity.runtime.name,
    )


async def async_turn_off(entity: LetvEntity) -> None:
    """关机。

    优先使用 UDP 的 ``power_off``（固件实际执行的是 ``power`` 待机切换）；
    服务端未提供时退化为 ADB ``power``（进入待机）。危险动作由服务端统一
    放行。由于 UDP ``power`` 是切换语义，屏幕已知关闭时直接返回，避免把
    电视重新点亮。
    """
    data = entity.state_data
    if data is not None and data.screen_on is False:
        return
    if ACTION_POWER_OFF in entity.runtime.actions:
        await entity.async_send_action(ACTION_POWER_OFF)
    elif data is not None and data.screen_on is True:
        if ACTION_POWER not in entity.runtime.actions:
            raise HomeAssistantError(
                "守护服务未提供关机动作；请确认 config.json 中 "
                "allow_dangerous_actions=true"
            )
        await entity.async_send_action(ACTION_POWER)
    else:
        raise HomeAssistantError(
            "守护服务未提供关机动作，且无法确认当前屏幕状态"
        )
    await entity.coordinator.async_force_refresh()
