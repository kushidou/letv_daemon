"""数据协调器：轮询守护进程的设备状态与媒体音量。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import (
    LetvAction,
    LetvAuthError,
    LetvClient,
    LetvError,
    LetvInput,
    LetvState,
    LetvVolume,
)
from .sources import SourceTarget

_LOGGER = logging.getLogger(__name__)


@dataclass
class LetvTvSnapshot:
    """一次轮询得到的电视状态 + 音量。"""

    state: LetvState
    volume: Optional[LetvVolume] = None
    volume_live: bool = False


@dataclass
class LetvTvRuntimeData:
    """配置条目运行期共享数据。"""

    entry_id: str
    name: str
    client: LetvClient
    coordinator: "LetvTvCoordinator"
    actions: dict[str, LetvAction]
    inputs: list[LetvInput]
    source_targets: dict[str, SourceTarget]
    source_list: list[str]


class LetvTvCoordinator(DataUpdateCoordinator[LetvTvSnapshot]):
    """在 HA 侧缓存设备状态与绝对音量。"""

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
            name=f"letv_tv {client.host}",
            update_interval=timedelta(seconds=interval),
            config_entry=entry,
        )
        self.client = client

    async def _async_update_data(self) -> LetvTvSnapshot:
        try:
            state = await self.client.async_get_state()
        except LetvAuthError as err:
            raise ConfigEntryAuthFailed(
                f"letv_daemon 拒绝访问令牌: {err}"
            ) from err
        except LetvError as err:
            raise UpdateFailed(f"获取电视状态失败: {err}") from err

        volume, live = await self._async_fetch_volume()
        return LetvTvSnapshot(
            state=state,
            volume=volume,
            volume_live=live,
        )

    async def _async_fetch_volume(
        self,
    ) -> tuple[Optional[LetvVolume], bool]:
        """读取音量；失败时保留上一次的值，不让电视整体不可用。"""
        try:
            return await self.client.async_get_volume(), True
        except LetvAuthError:
            raise
        except LetvError as err:
            _LOGGER.debug("读取音量失败，保留上一次的值: %s", err)
            return self._previous_volume(), False

    def _previous_volume(self) -> Optional[LetvVolume]:
        data = self.data
        return data.volume if data is not None else None

    async def async_force_refresh(self) -> None:
        """动作执行后立即向设备采集一次状态（失败只记日志）。"""
        try:
            state = await self.client.async_get_state(refresh=True)
        except LetvAuthError as err:
            _LOGGER.warning("强制刷新时访问令牌被拒绝: %s", err)
            return
        except LetvError as err:
            _LOGGER.debug("强制刷新失败: %s", err)
            return

        try:
            volume, live = await self._async_fetch_volume()
        except LetvAuthError as err:
            # 强制刷新是动作后的尽力而为；令牌问题交给下一次正常轮询
            # 触发重新认证，不要把异常抛进后台任务。
            _LOGGER.warning("强制刷新音量时访问令牌被拒绝: %s", err)
            volume, live = self._previous_volume(), False
        self.async_set_updated_data(
            LetvTvSnapshot(
                state=state,
                volume=volume,
                volume_live=live,
            )
        )
