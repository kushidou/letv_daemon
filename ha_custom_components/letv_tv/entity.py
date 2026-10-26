"""letv_tv 实体的公共基类。"""

from __future__ import annotations

from typing import Optional

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import (
    LetvApiError,
    LetvConnectionError,
    LetvError,
    LetvForbiddenError,
    LetvState,
)
from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import LetvTvCoordinator, LetvTvRuntimeData


class LetvTvEntity(CoordinatorEntity[LetvTvCoordinator]):
    """集中处理设备信息、运行时数据与错误转换。"""

    _attr_has_entity_name = True

    def __init__(self, runtime: LetvTvRuntimeData) -> None:
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
    def state_data(self) -> Optional[LetvState]:
        data = self.coordinator.data
        return data.state if data is not None else None

    @property
    def online(self) -> bool:
        data = self.state_data
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
