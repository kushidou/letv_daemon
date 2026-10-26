"""电视状态采集：在线、前台应用、屏幕开关。

``dumpsys window`` 单次约 1.3 s，因此只做低频轮询并缓存结果。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Tuple

from .adb import AdbCommandError, AdbConnectionError
from .net import tcp_probe

log = logging.getLogger(__name__)

# 一次 shell 往返同时取前台窗口与电源状态，减少设备端开销。
STATE_COMMAND = (
    "echo __FOCUS__; "
    "dumpsys window windows | grep mCurrentFocus; "
    "echo __POWER__; "
    "dumpsys power | grep -E 'mWakefulness|mScreenOn|Display Power'; "
    "true"
)

_FOCUS_RE = re.compile(
    r"mCurrentFocus=(?!null).*?\s"
    r"(?P<package>[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/"
    r"(?P<activity>[A-Za-z0-9_.$]+)"
)
_FOCUSED_APP_RE = re.compile(
    r"mFocusedApp=.*?\s"
    r"(?P<package>[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/"
    r"(?P<activity>[A-Za-z0-9_.$]+)"
)
_SCREEN_ON_RE = re.compile(r"mScreenOn=(true|false)", re.IGNORECASE)
_WAKEFULNESS_RE = re.compile(r"mWakefulness=(\w+)", re.IGNORECASE)
_DISPLAY_STATE_RE = re.compile(r"Display Power: state=(\w+)", re.IGNORECASE)
_AWAKE_STATES = {"awake", "dreaming"}
_DISPLAY_ON_STATES = {"on", "on_suspend", "doze", "doze_suspend"}


@dataclass
class DeviceState:
    """某一时刻的设备状态快照。"""

    online: bool = False
    foreground_package: Optional[str] = None
    foreground_activity: Optional[str] = None
    screen_on: Optional[bool] = None
    updated_at: float = 0.0
    error: Optional[str] = None

    @property
    def foreground_component(self) -> Optional[str]:
        if self.foreground_package and self.foreground_activity:
            return f"{self.foreground_package}/{self.foreground_activity}"
        return None

    @property
    def screen(self) -> str:
        if self.screen_on is None:
            return "unknown"
        return "on" if self.screen_on else "off"

    def as_dict(self) -> Dict[str, object]:
        updated = ""
        if self.updated_at:
            updated = datetime.fromtimestamp(
                self.updated_at, tz=timezone.utc
            ).astimezone().isoformat(timespec="seconds")
        return {
            "online": self.online,
            "foreground_package": self.foreground_package,
            "foreground_activity": self.foreground_activity,
            "foreground_component": self.foreground_component,
            "screen_on": self.screen_on,
            "screen": self.screen,
            "updated_at": self.updated_at,
            "updated_at_iso": updated,
            "error": self.error,
        }


def parse_focus(output: str) -> Tuple[Optional[str], Optional[str]]:
    """解析 mCurrentFocus（必要时回退到 mFocusedApp）。"""
    for line in output.splitlines():
        if "mCurrentFocus=" not in line:
            continue
        match = _FOCUS_RE.search(line)
        if match:
            return match.group("package"), match.group("activity")
        if "mCurrentFocus=null" in line:
            return None, None

    match = _FOCUSED_APP_RE.search(output)
    if match:
        return match.group("package"), match.group("activity")
    return None, None


def parse_power(output: str) -> Optional[bool]:
    """解析屏幕是否点亮；无法判断时返回 None。"""
    match = _SCREEN_ON_RE.search(output)
    if match:
        return match.group(1).lower() == "true"

    match = _WAKEFULNESS_RE.search(output)
    if match:
        return match.group(1).lower() in _AWAKE_STATES

    match = _DISPLAY_STATE_RE.search(output)
    if match:
        return match.group(1).lower() in _DISPLAY_ON_STATES
    return None


class StateMonitor:
    """后台低频采集设备状态，并对外提供缓存快照。"""

    def __init__(
        self,
        adb: object,
        host: str,
        port: int,
        interval: float = 5.0,
        tcp_timeout: float = 1.0,
        probe: Optional[Callable[[str, int, float], bool]] = None,
        offline_after_sec: float = 60.0,
        offline_poll_interval: float = 30.0,
        offline_confirm_polls: int = 3,
    ) -> None:
        self._adb = adb
        self._host = host
        self._port = port
        self._interval = max(2.0, float(interval))
        self._tcp_timeout = tcp_timeout
        self._probe = probe or tcp_probe
        # 离线自适应退避：连续离线超过 offline_after_sec 后放慢采样。
        self._offline_after = max(0.0, float(offline_after_sec))
        self._offline_interval = max(0.0, float(offline_poll_interval))
        # 连续失败多少次才把"关机/离线"作为事件写入日志。
        self._offline_confirm = max(1, int(offline_confirm_polls))
        self._offline_polls = 0
        self._offline_reported = False
        self._offline_since: Optional[float] = None
        self._backing_off = False

        self._lock = threading.Lock()
        # 串行化完整采样，避免看门狗/强制刷新与采样线程并发执行两份 dumpsys。
        self._sample_lock = threading.Lock()
        self._state = DeviceState()
        self._stop = threading.Event()
        # 连接恢复时由 ADBClient 回调，立即唤醒采样线程。
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def interval(self) -> float:
        return self._interval

    def sample(self) -> DeviceState:
        """同步采集一次状态。"""
        now = time.time()

        if not self._probe(self._host, self._port, self._tcp_timeout):
            return DeviceState(
                online=False,
                updated_at=now,
                error=f"TCP {self._host}:{self._port} 不可达",
            )

        try:
            connected = bool(self._adb.is_connected())
        except Exception as exc:  # 自定义传输层可能抛任意异常
            log.debug("ADB 状态检查失败: %s", exc)
            connected = False

        if not connected:
            return DeviceState(
                online=False, updated_at=now, error="ADB 未连接"
            )

        try:
            output = self._adb.shell(STATE_COMMAND)
        except AdbConnectionError as exc:
            return DeviceState(online=False, updated_at=now, error=str(exc))
        except AdbCommandError as exc:
            return DeviceState(
                online=True, updated_at=now, error=f"状态采集失败: {exc}"
            )
        except Exception as exc:
            return DeviceState(
                online=True, updated_at=now, error=f"状态采集异常: {exc}"
            )

        package, activity = parse_focus(output)
        return DeviceState(
            online=True,
            foreground_package=package,
            foreground_activity=activity,
            screen_on=parse_power(output),
            updated_at=now,
        )

    def refresh(self) -> DeviceState:
        """采集并更新缓存，返回最新快照。"""
        with self._sample_lock:
            state = self.sample()
            with self._lock:
                self._state = state
            self._record_online(state.online)
            return state

    def get(self) -> DeviceState:
        """返回缓存快照，不触发设备访问。"""
        with self._lock:
            return self._state

    def wake(self) -> None:
        """让采样线程立即醒来；用于 ADB 重连成功后的快速恢复。"""
        self._wake.set()

    def _record_online(self, online: bool) -> None:
        """只在状态变化时记日志，探测失败本身不再逐条输出。"""
        if online:
            if self._offline_reported:
                duration = 0.0
                if self._offline_since is not None:
                    duration = time.monotonic() - self._offline_since
                log.info("电视已开机/恢复在线（离线约 %.0f 秒）", duration)
            elif self._backing_off:
                log.info("设备已恢复，状态轮询恢复 %.1f 秒", self._interval)
            self._offline_since = None
            self._backing_off = False
            self._offline_polls = 0
            self._offline_reported = False
            return

        if self._offline_since is None:
            self._offline_since = time.monotonic()
        self._offline_polls += 1
        if not self._offline_reported and self._offline_polls >= self._offline_confirm:
            self._offline_reported = True
            log.info(
                "电视已关机/离线：连续 %d 次探测不可达",
                self._offline_polls,
            )

    def _next_delay(self) -> float:
        """按当前离线时长决定下一次采样间隔。"""
        if self._offline_since is None or self._offline_interval <= 0:
            return self._interval
        elapsed = time.monotonic() - self._offline_since
        if elapsed < self._offline_after:
            return self._interval
        delay = max(self._interval, self._offline_interval)
        if not self._backing_off:
            self._backing_off = True
            log.info(
                "设备离线超过 %.0f 秒，状态轮询降频至 %.1f 秒",
                self._offline_after,
                delay,
            )
        return delay

    def start(self) -> None:
        """启动后台轮询线程。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._run, name="letv-state-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # 唤醒可能正睡在退避间隔里的采样线程。
        self._wake.set()
        thread = self._thread
        if thread is not None:
            # 采集线程是 daemon；设备无响应时不要为它拖慢退出。
            thread.join(timeout=1.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                log.exception("状态采集线程异常")
            self._wake.wait(self._next_delay())
            self._wake.clear()
