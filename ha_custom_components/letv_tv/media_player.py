"""HomeKit 电视媒体播放器实体。

输入源完全由守护进程 ``config.json`` 的 ``inputs`` 决定（名称 / 动作 /
回读目标）。选择 App 时调用 ``/api/launch``，选择按键类输入源时发送对应
动作；当前输入源通过前台包名/组件名反向映射，从而在手动操作电视后仍能
保持同步。

音量使用守护进程的绝对音量接口；HomeKit 的 Volume 滑块需要
:mod:`homekit_compat` 修正标度后才声明 ``VOLUME_SET``。

关机后电视会从 ADB 上消失（``screen_on`` 变成未知）。实体不会显示为
“未知”，而是保留最近一次已知状态与输入源/音量，并在确认关机后标记为
``off``。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import voluptuous as vol

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.components.media_player.const import (
    ATTR_INPUT_SOURCE,
    ATTR_INPUT_SOURCE_LIST,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import (
    ACTION_MUTE,
    ACTION_POWER,
    ACTION_POWER_OFF,
    ACTION_VOLUME_DOWN,
    ACTION_VOLUME_UP,
    LetvError,
    LetvState,
)
from .const import (
    ATTR_ENTITY_ID,
    ATTR_KEY_NAME,
    HOMEKIT_TV_REMOTE_KEY_PRESSED,
    LAUNCH_SETTLE_SECONDS,
    VOLUME_MAX,
)
from .coordinator import LetvTvRuntimeData
from .entity import LetvTvEntity
from .homekit_compat import install_homekit_volume_fix
from .sources import (
    REMOTE_KEY_ACTIONS,
    SourceTracker,
    next_power_state,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_VOLUME = "set_volume"

# 只有 HomeKit 兼容补丁安装成功时才声明 VOLUME_SET；否则保留原来的
# 相对音量 + 静音，避免 Home App 出现无法写入的绝对音量滑块。
_HOMEKIT_VOLUME_FIXED = install_homekit_volume_fix()
_SUPPORTED_FEATURES = (
    MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
)
if _HOMEKIT_VOLUME_FIXED:
    _SUPPORTED_FEATURES |= MediaPlayerEntityFeature.VOLUME_SET

_LOGGER.debug(
    "letv_tv 支持的 feature=0x%X（HomeKit 绝对音量补丁=%s）",
    _SUPPORTED_FEATURES,
    _HOMEKIT_VOLUME_FIXED,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: LetvTvRuntimeData = entry.runtime_data
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_VOLUME,
        {
            vol.Required("volume"): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=VOLUME_MAX)
            )
        },
        "async_set_absolute_volume",
    )
    async_add_entities([LetvTvMediaPlayer(runtime)])


class LetvTvMediaPlayer(LetvTvEntity, MediaPlayerEntity):
    """把 App 作为输入源的 HomeKit 电视。"""

    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_volume_step = 0.01
    _attr_supported_features = _SUPPORTED_FEATURES

    def __init__(self, runtime: LetvTvRuntimeData) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry_id}_media_player"
        self._attr_source_list = list(runtime.source_list)

        self._tracker = SourceTracker(runtime.source_targets.values())
        # 关机后设备从 ADB 消失，screen_on 变成 None；这里保留最近一次
        # 已知状态，避免 HA/HomeKit 显示为 unknown。
        self._last_media_state: Optional[MediaPlayerState] = None
        self._offline_polls = 0

        snapshot = runtime.coordinator.data
        if snapshot is not None:
            self._tracker.observe(
                snapshot.state.foreground_package,
                snapshot.state.foreground_component,
            )
            self._update_power_state(snapshot.state)

        self._muted: Optional[bool] = None
        self._volume_override: Optional[float] = None
        self._pending_volume: Optional[int] = None
        self._volume_task: Optional[asyncio.Task] = None

    # ---- 状态 -----------------------------------------------------

    @property
    def state(self) -> Optional[MediaPlayerState]:
        return self._last_media_state

    def _update_power_state(self, data: LetvState) -> None:
        """把一次设备读数折算成电源状态（详见 ``next_power_state``）。"""
        state, polls = next_power_state(
            last_state=(
                self._last_media_state.value
                if self._last_media_state is not None
                else None
            ),
            online=data.online,
            screen_on=data.screen_on,
            offline_polls=self._offline_polls,
        )
        self._offline_polls = polls
        self._last_media_state = (
            MediaPlayerState(state) if state is not None else None
        )

    @property
    def source(self) -> Optional[str]:
        return self._tracker.current

    @property
    def volume_level(self) -> Optional[float]:
        if self._volume_override is not None:
            return self._volume_override
        snapshot = self.coordinator.data
        if snapshot is None or snapshot.volume is None:
            return None
        return snapshot.volume.ratio

    @property
    def is_volume_muted(self) -> Optional[bool]:
        return self._muted

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self.state_data
        snapshot = self.coordinator.data
        volume = snapshot.volume if snapshot is not None else None
        attributes: dict[str, object] = {
            "daemon_online": bool(data is not None and data.online),
            "foreground_app_name": (
                data.foreground_app_name if data is not None else None
            ),
            "foreground_package": (
                data.foreground_package if data is not None else None
            ),
            "volume_max": volume.max if volume is not None else None,
        }
        # MediaPlayerEntity.state_attributes 在状态为 off 时会直接返回空字典，
        # 连 source_list 都不输出；而 HomeKit 只在启动时按该属性构建
        # InputSource。这里始终补齐，避免电视关机时重启 HA 后源列表消失。
        attributes[ATTR_INPUT_SOURCE_LIST] = list(self._attr_source_list)
        if (source := self.source) is not None:
            attributes[ATTR_INPUT_SOURCE] = source
        return attributes

    # ---- 输入源 ---------------------------------------------------

    async def async_select_source(self, source: str) -> None:
        target = self.runtime.source_targets.get(source)
        if target is None:
            raise HomeAssistantError(
                f"不受支持的输入源：{source}；"
                f"可选值：{', '.join(self.runtime.source_list)}"
            )

        try:
            if target.kind == "key":
                if target.action is None:
                    raise HomeAssistantError(
                        f"输入源 {source} 没有对应动作"
                    )
                await self.async_send_action(target.action)
            else:
                try:
                    await self.runtime.client.async_launch(
                        target.package or "", target.activity
                    )
                except LetvError as err:
                    raise HomeAssistantError(
                        f"启动 {source} 失败：{err}"
                    ) from err
        except HomeAssistantError:
            # 选择失败时不写乐观状态；重写一次实体状态，让 HomeKit 回弹。
            self.async_write_ha_state()
            raise

        self._tracker.select(source)
        self.async_write_ha_state()

        if target.kind == "app":
            self.hass.async_create_task(self._async_refresh_after_launch())

    async def _async_refresh_after_launch(self) -> None:
        # 给系统一点时间把新 Activity 推到前台，避免旧读数覆盖乐观源。
        await asyncio.sleep(LAUNCH_SETTLE_SECONDS)
        await self.runtime.coordinator.async_force_refresh()

    # ---- 电源 -----------------------------------------------------

    async def async_turn_on(self) -> None:
        """开机由独立硬件完成，这里刻意不发送命令。"""
        _LOGGER.debug(
            "忽略开机请求：%s 的开机由外部硬件实现",
            self.runtime.name,
        )

    async def async_turn_off(self) -> None:
        data = self.state_data
        # UDP 的 power 是待机/唤醒切换，屏幕已确定关闭时不要再发。
        if data is not None and data.screen_on is False:
            return
        if ACTION_POWER_OFF in self.runtime.actions:
            await self.async_send_action(ACTION_POWER_OFF)
        elif data is not None and data.screen_on is True:
            if ACTION_POWER not in self.runtime.actions:
                raise HomeAssistantError(
                    "守护服务未提供关机动作；请确认 config.json 中 "
                    "allow_dangerous_actions=true"
                )
            await self.async_send_action(ACTION_POWER)
        else:
            raise HomeAssistantError(
                "守护服务未提供关机动作，且无法确认当前屏幕状态"
            )

        # 不做乐观标记：电源状态始终以设备读数为准。电视真正关机后
        # ADB 掉线，实体按掉线计数判定为 off。
        self._offline_polls = 0
        await self.runtime.coordinator.async_force_refresh()

    # ---- 音量 -----------------------------------------------------

    async def async_set_volume_level(self, volume: float) -> None:
        """设置绝对音量（0–1），快速连续调用会合并到最新目标。"""
        level = max(0.0, min(1.0, float(volume)))
        self._volume_override = level
        self._pending_volume = round(level * VOLUME_MAX)
        self.async_write_ha_state()
        self._ensure_volume_worker()

    async def async_set_absolute_volume(self, volume: int) -> None:
        """自定义服务入口：音量使用设备百分比（0–100）。"""
        await self.async_set_volume_level(int(volume) / VOLUME_MAX)

    def _ensure_volume_worker(self) -> None:
        if self._volume_task is None or self._volume_task.done():
            self._volume_task = self.hass.async_create_task(
                self._async_volume_worker()
            )

    async def _async_volume_worker(self) -> None:
        try:
            while self._pending_volume is not None:
                target = self._pending_volume
                self._pending_volume = None
                try:
                    actual = await self.runtime.client.async_set_volume(
                        target
                    )
                except LetvError as err:
                    _LOGGER.warning("设置音量失败：%s", err)
                    if self._pending_volume is None:
                        self._volume_override = None
                    self.async_write_ha_state()
                    continue
                self._volume_override = actual / VOLUME_MAX
                self.async_write_ha_state()
        finally:
            self._volume_task = None
            if self._pending_volume is not None:
                self._ensure_volume_worker()

    async def async_volume_up(self) -> None:
        level = self.volume_level
        if level is None:
            await self._async_send_volume_key(ACTION_VOLUME_UP)
            return
        await self.async_set_volume_level(
            min(1.0, level + self.volume_step)
        )

    async def async_volume_down(self) -> None:
        level = self.volume_level
        if level is None:
            await self._async_send_volume_key(ACTION_VOLUME_DOWN)
            return
        await self.async_set_volume_level(
            max(0.0, level - self.volume_step)
        )

    async def _async_send_volume_key(self, action: str) -> None:
        if action not in self.runtime.actions:
            raise HomeAssistantError(
                f"守护服务未提供动作 {action}"
            )
        await self.async_send_action(action)

    async def async_mute_volume(self, mute: bool) -> None:
        if self._muted == mute:
            return

        if self._muted is None and not mute:
            # 状态未知时假定当前未静音，不盲发切换键。
            self._muted = False
            self.async_write_ha_state()
            return

        if ACTION_MUTE not in self.runtime.actions:
            raise HomeAssistantError("守护服务未提供静音动作")

        await self.async_send_action(ACTION_MUTE)
        self._muted = mute
        self.async_write_ha_state()

    # ---- 播放控制 -------------------------------------------------

    async def async_media_play_pause(self) -> None:
        await self.async_send_action("media_play_pause")

    async def async_media_play(self) -> None:
        await self.async_send_action("media_play")

    async def async_media_pause(self) -> None:
        await self.async_send_action("media_pause")

    # ---- HomeKit 遥控器按键 ---------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen(
                HOMEKIT_TV_REMOTE_KEY_PRESSED,
                self._handle_homekit_remote_key,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """卸载实体时停止音量任务，避免继续写已移除实体的状态。"""
        self._pending_volume = None
        if self._volume_task is not None:
            self._volume_task.cancel()
            self._volume_task = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_homekit_remote_key(self, event: Event) -> None:
        if event.data.get(ATTR_ENTITY_ID) != self.entity_id:
            return
        key_name = event.data.get(ATTR_KEY_NAME)
        action = REMOTE_KEY_ACTIONS.get(key_name)
        if action is None:
            _LOGGER.debug("未映射的 HomeKit 按键：%s", key_name)
            return
        self.hass.async_create_task(self._async_send_remote_key(action))

    async def _async_send_remote_key(self, action: str) -> None:
        try:
            await self.runtime.client.async_send_key(action)
        except LetvError as err:
            # 事件总线上的按键无法向 HomeKit 回报错误，只能记日志。
            _LOGGER.warning("发送遥控按键 %s 失败：%s", action, err)

    # ---- 协调器更新 -----------------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        snapshot = self.coordinator.data
        if snapshot is not None:
            state = snapshot.state
            self._update_power_state(state)
            self._tracker.observe(
                state.foreground_package,
                state.foreground_component,
            )
            if (
                snapshot.volume_live
                and self._pending_volume is None
                and self._volume_task is None
            ):
                self._volume_override = None
        super()._handle_coordinator_update()
