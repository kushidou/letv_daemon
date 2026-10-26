"""Home Assistant HomeKit 绝对音量的兼容层。

HA 2025.8.x 的 ``TelevisionMediaPlayer`` 有两个问题：

1. HomeKit 的 Volume 特征是 0–100，但 ``set_volume()`` 把它原样传给
   ``media_player.volume_set``（schema 只接受 0–1），写入必然失败；
2. ``async_update_state()`` 不会把实体的 ``volume_level`` 回写到 Volume
   特征，Home App 无法显示设备当前音量。

这里在运行时把 HomeKit 的 ``TelevisionMediaPlayer`` 类型替换为修正后的
子类。安装失败（例如 HomeKit 未安装或 HA 内部结构变化）时返回 False，
调用方据此不声明 ``VOLUME_SET``，避免暴露一个不可用的滑块。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

HOMEKIT_VOLUME_FIXED = False
_INSTALLED = False


def percent_to_level(value: Any) -> Optional[float]:
    """HomeKit 的 0–100 百分比 → HA 的 0–1 音量。"""
    try:
        percent = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, percent / 100.0))


def level_to_percent(value: Any) -> Optional[int]:
    """HA 的 0–1 音量 → HomeKit 的 0–100 百分比。"""
    try:
        level = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(max(0.0, min(1.0, level)) * 100))


def install_homekit_volume_fix() -> bool:
    """安装兼容补丁；幂等，返回补丁是否可用。"""
    global HOMEKIT_VOLUME_FIXED, _INSTALLED
    if _INSTALLED:
        return HOMEKIT_VOLUME_FIXED

    _INSTALLED = True
    try:
        from homeassistant.components.homekit import (
            type_media_players as hk,
        )
        from homeassistant.components.homekit.accessories import TYPES
    except Exception as err:  # pragma: no cover - 取决于 HA 安装
        _LOGGER.debug("HomeKit 不可用，跳过绝对音量兼容补丁: %s", err)
        return False

    base = getattr(hk, "TelevisionMediaPlayer", None)
    if base is None:  # pragma: no cover - 防御 HA 内部改名
        _LOGGER.debug("HomeKit 没有 TelevisionMediaPlayer，跳过音量补丁")
        return False

    class FixedTelevisionMediaPlayer(base):  # type: ignore[misc, valid-type]
        """修正 HomeKit 音量标度并回写当前音量。"""

        def set_volume(self, value: Any) -> None:
            level = percent_to_level(value)
            if level is None:
                return
            _LOGGER.debug("%s: Set volume to %s", self.entity_id, level)
            params = {
                hk.ATTR_ENTITY_ID: self.entity_id,
                hk.ATTR_MEDIA_VOLUME_LEVEL: level,
            }
            self.async_call_service(
                hk.MEDIA_PLAYER_DOMAIN, hk.SERVICE_VOLUME_SET, params
            )

        def async_update_state(self, new_state: Any) -> None:
            super().async_update_state(new_state)
            char_volume = getattr(self, "char_volume", None)
            if char_volume is None:
                return
            percent = level_to_percent(
                new_state.attributes.get(hk.ATTR_MEDIA_VOLUME_LEVEL)
            )
            if percent is not None:
                char_volume.set_value(percent)

    TYPES["TelevisionMediaPlayer"] = FixedTelevisionMediaPlayer
    HOMEKIT_VOLUME_FIXED = True
    _LOGGER.info("已安装 HomeKit 绝对音量兼容补丁")
    return True
