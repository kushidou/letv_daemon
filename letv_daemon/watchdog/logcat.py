"""通过系统 adb 持续读取 logcat，识别进入桌面的事件。

Android 6 上 ``ActivityManager`` 的 HOME 启动日志形如::

    I/ActivityManager( 2267): START u0 {act=android.intent.action.MAIN
        cat=[android.intent.category.HOME] flg=0x10200000
        pkg=com.stv.appdaemon cmp=com.stv.appdaemon/.ui.DaemonActivity}

一次 HOME 按键会连续产生多条（appdaemon → ExternalJumpActivity → Launcher），
因此只负责产出事件，去抖由 :class:`~.watchdog.Watchdog` 完成。
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from typing import Callable, Iterator, Optional, Sequence

log = logging.getLogger(__name__)

HOME_EVENT_RE = re.compile(
    r"START u0 \{.*android\.intent\.category\.HOME"
)

# 只订阅 ActivityManager 的 info 日志，减少无关输出与带宽。
# -T 0：不重放缓冲区，只跟随新日志；否则启动时会命中旧的 HOME 事件。
LOGCAT_ARGS: Sequence[str] = (
    "logcat",
    "-T",
    "0",
    "-v",
    "brief",
    "ActivityManager:I",
    "*:S",
)


def is_home_event(line: str) -> bool:
    """判断一行 logcat 是否是 ``category.HOME`` 的 activity 启动。"""
    return bool(HOME_EVENT_RE.search(line))


class LogcatStream:
    """阻塞式读取 logcat 行，进程退出或连接断开后自动重建。

    ``adb`` 客户端进程独立于 :class:`~letv_daemon.device.adb.ADBClient`
    的命令连接；看门狗的设备操作仍统一走 ADBClient 的锁。
    """

    def __init__(
        self,
        adb_path: str,
        serial: str,
        stop_event: threading.Event,
        reconnect_interval: float = 5.0,
        command: Sequence[str] = LOGCAT_ARGS,
        ready: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._adb_path = adb_path or "adb"
        self._serial = serial
        self._stop = stop_event
        self._reconnect_interval = max(0.5, float(reconnect_interval))
        self._command = tuple(command)
        # 设备离线时不创建 logcat 子进程；恢复后由下一轮循环拉起。
        self._ready = ready
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None

    def lines(self) -> Iterator[str]:
        """持续产出 logcat 行，直到 ``stop_event`` 被设置。"""
        args = [self._adb_path, "-s", self._serial, *self._command]

        while not self._stop.is_set():
            if self._ready is not None and not self._ready():
                self._stop.wait(self._reconnect_interval)
                continue

            try:
                proc = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except (OSError, ValueError) as exc:
                log.error("无法启动 logcat（%s），稍后重试", exc)
                self._stop.wait(self._reconnect_interval)
                continue

            with self._lock:
                self._proc = proc

            # stop() 可能恰好发生在 Popen 与赋值之间，这里补一次检查。
            if self._stop.is_set():
                self._terminate(proc)
                with self._lock:
                    if self._proc is proc:
                        self._proc = None
                break

            log.debug("logcat 已连接: %s", self._serial)
            try:
                if proc.stdout is not None:
                    for line in proc.stdout:
                        if self._stop.is_set():
                            break
                        yield line.rstrip("\r\n")
            except (OSError, ValueError) as exc:
                log.warning("logcat 读取中断: %s", exc)
            finally:
                self._terminate(proc)
                with self._lock:
                    if self._proc is proc:
                        self._proc = None

            if self._stop.is_set():
                break
            log.warning(
                "logcat 已断开，%.1f 秒后重连", self._reconnect_interval
            )
            self._stop.wait(self._reconnect_interval)

    def close(self) -> None:
        """终止当前 adb logcat 进程，让阻塞读取尽快返回。"""
        with self._lock:
            proc = self._proc
        if proc is not None:
            self._terminate(proc)

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
