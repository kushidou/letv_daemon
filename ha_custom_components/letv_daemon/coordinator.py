"""数据协调器：轮询 letv_daemon 的设备状态。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import (
    LetvAction,
    LetvApp,
    LetvAuthError,
    LetvClient,
    LetvError,
    LetvState,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class LetvRuntimeData:
    """配置条目运行期共享数据。"""

    entry_id: str
    name: str
    client: LetvClient
    coordinator: "LetvCoordinator"
    actions: dict[str, LetvAction]
    apps: list[LetvApp]


class LetvCoordinator(DataUpdateCoordinator[LetvState]):
    """在 HA 侧缓存守护服务返回的状态快照。"""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: LetvClient,
        interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"letv_daemon {client.host}",
            update_interval=timedelta(seconds=interval),
            config_entry=entry,
        )
        self.client = client

    async def _async_update_data(self) -> LetvState:
        try:
            return await self.client.async_get_state()
        except LetvAuthError as err:
            raise ConfigEntryAuthFailed(
                f"letv_daemon 拒绝访问令牌: {err}"
            ) from err
        except LetvError as err:
            raise UpdateFailed(f"获取电视状态失败: {err}") from err

    async def async_force_refresh(self) -> None:
        """动作执行后立即向设备采集一次状态（失败只记日志）。

        用于电源切换、启动 App 等会改变前台/屏幕状态的动作；
        普通按键不调用，避免频繁触发约 1.3 s 的 dumpsys 采集。
        """
        try:
            state = await self.client.async_get_state(refresh=True)
        except LetvAuthError as err:
            _LOGGER.warning("强制刷新时访问令牌被拒绝: %s", err)
            return
        except LetvError as err:
            _LOGGER.debug("强制刷新失败: %s", err)
            return
        self.async_set_updated_data(state)
