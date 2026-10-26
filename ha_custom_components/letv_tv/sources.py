"""输入源与 HomeKit 遥控按键的纯逻辑。

本模块只依赖标准库，不 import ``homeassistant``，因此可以脱离 HA 单测。

输入源的事实来源是守护进程 ``config.json`` 的 ``inputs`` 数组，HTTP 层
通过 ``/api/inputs`` 暴露。旧版守护进程没有该接口时，集成会退回
``/api/apps``（HDMI 1 + 应用列表）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

_LOGGER = logging.getLogger(__name__)

# 旧版守护进程（无 /api/inputs）的后备默认值。
HDMI_SOURCE = "HDMI 1"
HDMI_ACTION = "switch_hdmi_0"

# 电视关机后会从网络消失；连续多次采集不到才判定为关机，避免短暂抖动。
OFFLINE_POLLS_BEFORE_OFF = 3

# HomeKit 的 key_name -> letv_daemon 动作名。
REMOTE_KEY_ACTIONS: dict[str, str] = {
    "arrow_up": "up",
    "arrow_down": "down",
    "arrow_left": "left",
    "arrow_right": "right",
    "select": "ok",
    "back": "return",
    "exit": "home",
    "information": "info",
    "play_pause": "media_play_pause",
    "next_track": "media_next",
    "previous_track": "media_previous",
    "rewind": "media_rewind",
    "fast_forward": "media_fast_forward",
}


@dataclass(frozen=True)
class SourceTarget:
    """一个输入源及其对应的守护进程操作与状态回读目标。"""

    name: str
    kind: str  # "app" | "key"
    package: Optional[str] = None
    activity: Optional[str] = None
    action: Optional[str] = None
    targets: tuple[str, ...] = ()

    def matches(self, package: Optional[str], component: Optional[str]) -> bool:
        """前台包名/组件是否属于该输入源。

        target 不含 ``/`` 时按包名匹配；含 ``/`` 时按完整组件名匹配。
        """
        for target in self.targets:
            if "/" in target:
                if component and component == target:
                    return True
            elif package and package == target:
                return True
        return False


def build_source_targets(
    inputs: Iterable[object],
) -> dict[str, SourceTarget]:
    """构造 ``显示名 -> SourceTarget``，保持配置顺序。

    ``inputs`` 可以是 :class:`LetvInput` 或带同名属性的对象。重名时保留
    第一个（HomeKit 要求源名称唯一）。
    """
    targets: dict[str, SourceTarget] = {}
    for item in inputs:
        name = getattr(item, "name", None)
        kind = getattr(item, "kind", None)
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if not isinstance(kind, str) or kind not in ("app", "key"):
            _LOGGER.warning("忽略未知类型的输入源 %s（%s）", name, kind)
            continue
        if name in targets:
            _LOGGER.warning(
                "忽略重复的输入源 %s；HomeKit 要求源名称唯一",
                name,
            )
            continue

        raw_targets = getattr(item, "targets", ()) or ()
        if isinstance(raw_targets, str):
            raw_targets = [raw_targets]
        normalized_targets = tuple(
            value.strip()
            for value in raw_targets
            if isinstance(value, str) and value.strip()
        )

        if kind == "app":
            package = getattr(item, "package", None)
            if not isinstance(package, str) or not package.strip():
                _LOGGER.warning("输入源 %s 缺少 package，已忽略", name)
                continue
            activity = getattr(item, "activity", None)
            targets[name] = SourceTarget(
                name=name,
                kind="app",
                package=package.strip(),
                activity=(
                    activity.strip()
                    if isinstance(activity, str) and activity.strip()
                    else None
                ),
                targets=normalized_targets or (package.strip(),),
            )
            continue

        action = getattr(item, "key", None)
        if not isinstance(action, str) or not action.strip():
            _LOGGER.warning("输入源 %s 缺少 key，已忽略", name)
            continue
        targets[name] = SourceTarget(
            name=name,
            kind="key",
            action=action.strip(),
            targets=normalized_targets,
        )
    return targets


def source_list(targets: dict[str, SourceTarget]) -> list[str]:
    return list(targets)


def next_power_state(
    *,
    last_state: Optional[str],
    online: bool,
    screen_on: Optional[bool],
    offline_polls: int,
    offline_polls_before_off: int = OFFLINE_POLLS_BEFORE_OFF,
) -> tuple[Optional[str], int]:
    """根据一次设备读数推进电源状态。

    屏幕状态直接以设备读数为准，不做乐观覆盖。电视关机后 ADB 端口
    关闭，守护进程只能报告 ``online=False`` 且 ``screen_on=None``；
    此时保留上一次状态，连续多次采样不到才判定为 ``off``。

    返回 ``(state, offline_polls)``。本函数不依赖 Home Assistant，
    便于单测。
    """
    if screen_on is not None:
        return ("on" if screen_on else "off"), 0

    if not online:
        polls = offline_polls + 1
        if polls >= offline_polls_before_off:
            return "off", polls
        return last_state, polls

    # 守护进程可达但屏幕状态采集失败：保持上一次已知状态。
    return last_state, offline_polls


class SourceTracker:
    """把前台包名映射为当前输入源，并维护选中后的乐观状态。

    没有物理回读的源（如 HDMI）也能被手动选择；如果电视继续报告选择前
    的应用，就保持乐观值；一旦出现目标源或另一个已知源，就恢复跟随
    真实前台。前台为未知包名（系统界面等）时保持当前源。
    """

    def __init__(
        self,
        sources: Iterable[SourceTarget],
        observed_source: Optional[str] = None,
    ) -> None:
        self._sources = {
            source.name: source for source in sources if source.name
        }
        self._current: Optional[str] = (
            observed_source if observed_source in self._sources else None
        )
        self._observed_source: Optional[str] = self._current
        self._override: Optional[tuple[str, Optional[str]]] = None

    @property
    def current(self) -> Optional[str]:
        return self._current

    def match(
        self,
        package: Optional[str],
        component: Optional[str] = None,
    ) -> Optional[str]:
        """返回前台包名对应的输入源名称；没有匹配时返回 None。"""
        for name, source in self._sources.items():
            if source.matches(package, component):
                return name
        return None

    def select(self, source: str) -> None:
        """记录用户选择；用最近一次真实前台作为陈旧读数的基准。"""
        self._current = source
        self._override = (source, self._observed_source)

    def observe(
        self,
        package: Optional[str],
        component: Optional[str] = None,
    ) -> Optional[str]:
        """喂入一次前台读数，返回当前应显示的输入源。"""
        observed = self.match(package, component)
        if observed is None:
            return self._current

        self._observed_source = observed

        if self._override is None:
            self._current = observed
            return self._current

        target, baseline = self._override
        if observed == target:
            # 目标源已到前台，乐观状态完成使命。
            self._override = None
            self._current = observed
        elif observed != baseline:
            # 用户从别处切到了另一个已知源。
            self._override = None
            self._current = observed
        # observed == baseline：切换前的陈旧读数，继续保持乐观值。
        return self._current
