"""桌面守护：HOME 事件 + 去抖 + 兜底轮询 + PAUSE 开关。

不修改电视任何持久状态，只在原桌面出现后主动把自建桌面拉回前台。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

from ..config import WatchdogConfig
from .logcat import LogcatStream, is_home_event

log = logging.getLogger(__name__)

SourceFactory = Callable[[threading.Event], "LogcatSource"]


class LogcatSource:
    """看门狗需要的最小流接口，便于测试替换。"""

    def lines(self):
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def _isoformat(timestamp: float) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(
        timestamp, tz=timezone.utc
    ).astimezone().isoformat(timespec="seconds")


class Watchdog:
    """监听 HOME 并拉回自建桌面。

    - logcat 主通道：命中 ``category.HOME`` 后去抖再拉起；
    - 兜底轮询：每 ``poll_sec`` 秒检查前台，仍是原桌面就拉起（0 表示关闭）；
    - ``PAUSE`` 文件：存在时暂停一切拉起动作，删除后自动恢复。
    """

    def __init__(
        self,
        adb: object,
        config: WatchdogConfig,
        monitor: object,
        pause_path: Path,
        *,
        serial: str = "",
        adb_path: str = "adb",
        reconnect_interval: float = 5.0,
        source_factory: Optional[SourceFactory] = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._adb = adb
        self._config = config
        self._monitor = monitor
        self._pause_path = Path(pause_path)
        self._clock = clock
        self._monotonic = monotonic
        self._target_package, self._target_activity = config.target.split(
            "/", 1
        )
        self._source_factory: SourceFactory = (
            source_factory or self._default_source_factory
        )
        self._serial = serial
        self._adb_path = adb_path or "adb"
        self._reconnect_interval = reconnect_interval

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._running = False
        self._started_at = 0.0
        self._last_trigger_mono = 0.0
        self._last_trigger_at = 0.0
        self._triggers = 0
        self._last_error: Optional[str] = None
        self._triggering = False

        self._source: Optional[LogcatSource] = None
        self._reader: Optional[threading.Thread] = None
        self._supervisor: Optional[threading.Thread] = None
        self._adb_generation = self._current_adb_generation()
        self._last_state_error: Optional[str] = None

    # ---- 生命周期 -------------------------------------------------

    def start(self) -> None:
        """启动 logcat 监听与兜底轮询；重复调用无副作用。"""
        with self._lock:
            if self._running:
                return
            self._stop.clear()
            self._running = True
            self._started_at = self._clock()
            self._last_trigger_mono = 0.0
            self._triggering = False
            self._adb_generation = self._current_adb_generation()
            self._last_state_error = None

            source = self._source_factory(self._stop)
            reader = threading.Thread(
                target=self._read_loop,
                args=(source,),
                name="letv-watchdog-logcat",
                daemon=True,
            )
            supervisor = threading.Thread(
                target=self._supervise_loop,
                name="letv-watchdog-poll",
                daemon=True,
            )
            self._source = source
            self._reader = reader
            self._supervisor = supervisor
            reader.start()
            supervisor.start()

        log.info("桌面守护已启动: %s", self._config.target)
        if self._is_paused():
            log.info("桌面守护处于暂停状态: %s", self._pause_path)

    def stop(self) -> None:
        """停止监听线程；可在任意线程调用。"""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stop.set()
            source = self._source
            reader = self._reader
            supervisor = self._supervisor
            self._source = None
            self._reader = None
            self._supervisor = None

        if source is not None:
            source.close()
        for thread in (reader, supervisor):
            if thread is not None:
                # 线程是 daemon，设备无响应时无需等待完整的 shell 超时。
                thread.join(timeout=1.0)
        log.info("桌面守护已停止")

    # ---- 线程主体 -------------------------------------------------

    def _current_adb_generation(self) -> int:
        try:
            return int(getattr(self._adb, "generation", 0))
        except Exception:
            return 0

    def _restart_logcat_if_reconnected(self) -> None:
        """ADB 重建连接后，旧 logcat 管道可能已经假死，主动换一条。"""
        generation = self._current_adb_generation()
        if generation == self._adb_generation:
            return
        self._adb_generation = generation
        with self._lock:
            source = self._source
        if source is not None:
            log.info("ADB 连接已重建，重启 logcat 监听")
            source.close()

    def _default_source_factory(
        self, stop_event: threading.Event
    ) -> LogcatSource:
        return LogcatStream(
            self._adb_path,
            self._serial,
            stop_event,
            reconnect_interval=self._reconnect_interval,
            ready=getattr(self._adb, "is_connected", None),
        )

    def _read_loop(self, source: LogcatSource) -> None:
        try:
            for line in source.lines():
                if self._stop.is_set():
                    break
                try:
                    self.handle_log_line(line)
                except Exception:
                    log.exception("处理 logcat 事件失败")
        except Exception:
            log.exception("logcat 监听线程异常")

    def _supervise_loop(self) -> None:
        poll_sec = max(0.0, float(self._config.poll_sec))
        was_paused = self._is_paused()

        if not was_paused and poll_sec > 0:
            try:
                self.poll_once()
            except Exception:
                log.exception("桌面守护启动检查失败")

        last_poll = self._monotonic()
        while not self._stop.wait(0.25):
            self._restart_logcat_if_reconnected()
            paused = self._is_paused()
            if paused != was_paused:
                if paused:
                    log.info("桌面守护已暂停（%s）", self._pause_path)
                else:
                    log.info("桌面守护已恢复")
                    with self._lock:
                        self._last_trigger_mono = 0.0
                was_paused = paused
            if paused:
                continue

            if poll_sec <= 0:
                continue
            now = self._monotonic()
            if now - last_poll < poll_sec:
                continue
            last_poll = now
            try:
                self.poll_once()
            except Exception:
                log.exception("桌面守护轮询失败")

    # ---- 事件处理 -------------------------------------------------

    def handle_log_line(self, line: str) -> bool:
        """处理一行 logcat；命中 HOME 事件并成功拉起时返回 True。"""
        if not is_home_event(line):
            return False
        if self._is_target_event(line):
            log.debug("忽略自建桌面自身的 HOME 日志")
            return False
        return self._trigger("logcat")

    def poll_once(self) -> bool:
        """检查一次前台应用；仍停留在原桌面则拉回。"""
        if self._is_paused() or self._stop.is_set():
            return False

        try:
            # 复用采样线程的缓存；仅启动早期尚未采样时主动采一次。
            state = self._monitor.get()
            if not state.updated_at:
                state = self._monitor.refresh()
        except Exception as exc:
            log.warning("兜底轮询失败: %s", exc)
            with self._lock:
                self._last_error = f"状态采集失败: {exc}"
            return False

        if state.error:
            # 只在错误变化时告警，避免设备长时间不可用时刷屏。
            if state.error != self._last_state_error:
                self._last_state_error = state.error
                log.warning("桌面守护状态采集异常: %s", state.error)
            with self._lock:
                self._last_error = f"状态采集失败: {state.error}"
            return False
        self._last_state_error = None

        if not state.online:
            return False
        if state.foreground_package != self._config.stock_package:
            return False
        return self._trigger("poll")

    def _trigger(self, reason: str) -> bool:
        now_mono = self._monotonic()
        with self._lock:
            if self._stop.is_set() or self._triggering:
                return False
            if self._is_paused():
                return False

            debounce = max(0.0, float(self._config.debounce_sec))
            if (
                debounce
                and self._last_trigger_mono
                and now_mono - self._last_trigger_mono < debounce
            ):
                log.debug("桌面守护去抖，忽略 %s 事件", reason)
                return False

            self._last_trigger_mono = now_mono
            self._triggering = True

        try:
            component = self._adb.launch_app(
                self._target_package, self._target_activity
            )
        except Exception as exc:
            with self._lock:
                self._triggering = False
                self._last_error = str(exc)
            log.warning("桌面守护拉起失败（%s）: %s", reason, exc)
            return False

        with self._lock:
            self._triggering = False
            self._triggers += 1
            self._last_trigger_at = self._clock()
            self._last_error = None
        log.info("桌面守护已拉起 %s（来源: %s）", component, reason)
        return True

    def _is_target_event(self, line: str) -> bool:
        return (
            f"cmp={self._config.target}" in line
            or f"pkg={self._target_package}" in line
        )

    # ---- PAUSE 开关 -----------------------------------------------

    def pause(self) -> None:
        """创建 PAUSE 文件；看门狗线程会在一个轮询周期内停止拉起。"""
        parent = self._pause_path.parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
        self._pause_path.write_text(
            "paused by letv_daemon\n", encoding="utf-8"
        )

    def resume(self) -> None:
        """删除 PAUSE 文件并立即恢复自动拉起。"""
        try:
            self._pause_path.unlink()
        except FileNotFoundError:
            pass
        with self._lock:
            self._last_trigger_mono = 0.0

    def _is_paused(self) -> bool:
        try:
            return self._pause_path.exists()
        except OSError as exc:
            log.debug("检查 PAUSE 文件失败: %s", exc)
            return False

    # ---- 状态查询 -------------------------------------------------

    def status(self) -> Dict[str, object]:
        paused = self._is_paused()
        with self._lock:
            return {
                "enabled": self._config.enabled,
                "running": self._running,
                "paused": paused,
                "target": self._config.target,
                "stock_package": self._config.stock_package,
                "debounce_sec": self._config.debounce_sec,
                "poll_sec": self._config.poll_sec,
                "pause_file": str(self._pause_path),
                "triggers": self._triggers,
                "last_trigger_at": self._last_trigger_at,
                "last_trigger_iso": _isoformat(self._last_trigger_at),
                "last_error": self._last_error,
                "started_at": self._started_at,
                "started_at_iso": _isoformat(self._started_at),
            }
