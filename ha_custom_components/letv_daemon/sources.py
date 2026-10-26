"""UDP 信号源动作与显示名映射。

本模块不依赖 ``homeassistant``，可独立测试。动作名来自
``letv_daemon/device/keys.py`` 的 UDP 动作表。
"""

from __future__ import annotations

from typing import Iterable

# (显示名, 动作名)；顺序即 HA 下拉框中的顺序。
SOURCE_CHOICES: tuple[tuple[str, str], ...] = (
    ("TV", "switch_tv"),
    ("HDMI 1", "switch_hdmi_0"),
    ("HDMI 2", "switch_hdmi_1"),
    ("HDMI 3", "switch_hdmi_2"),
    ("HDMI 4", "switch_hdmi_3"),
    ("AV", "switch_cvbs"),
    ("VGA", "switch_vga"),
)


def available_sources(actions: Iterable[str]) -> dict[str, str]:
    """按守护服务当前放行的动作，返回 ``显示名 -> 动作名``。"""
    allowed = set(actions)
    return {
        label: action
        for label, action in SOURCE_CHOICES
        if action in allowed
    }
