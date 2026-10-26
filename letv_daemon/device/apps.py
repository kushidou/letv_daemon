"""用户应用清单缓存。

清单来自设备端 ``pm list packages -3``（第三方 / 用户安装的应用）。
按需求它**不参与轮询**：只在 ADB 连接成功时刷新一次，之后仅在网页
点击「刷新清单」时重新读取；卸载成功后就地移除，不再访问设备。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserApp:
    """一个用户应用；name 为配置提供的显示名，未知时为 None。"""

    package: str
    name: Optional[str] = None

    def as_dict(self) -> Dict[str, object]:
        return {"package": self.package, "name": self.name}


class UserAppCatalog:
    """线程安全的用户应用清单缓存。"""

    def __init__(
        self,
        adb: object,
        display_name: Callable[[str], Optional[str]],
        watch_interval: float = 2.0,
    ) -> None:
        self._adb = adb
        self._display_name = display_name
        self._watch_interval = max(0.1, float(watch_interval))

        self._lock = threading.Lock()
        self._apps: List[UserApp] = []
        self._loaded = False
        self._updated_at = 0.0
        self._error: Optional[str] = None

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- 查询 -----------------------------------------------------

    def snapshot(self) -> Dict[str, object]:
        """返回缓存快照，不访问设备。"""
        with self._lock:
            apps = [app.as_dict() for app in self._apps]
            loaded = self._loaded
            updated_at = self._updated_at
            error = self._error

        updated_iso = ""
        if updated_at:
            updated_iso = datetime.fromtimestamp(
                updated_at, tz=timezone.utc
            ).astimezone().isoformat(timespec="seconds")

        return {
            "loaded": loaded,
            "updated_at": updated_at,
            "updated_at_iso": updated_iso,
            "error": error,
            "apps": apps,
        }

    def contains(self, package: str) -> bool:
        with self._lock:
            return any(app.package == package for app in self._apps)

    # ---- 更新 -----------------------------------------------------

    def refresh(self) -> None:
        """立即从设备读取用户应用清单；失败时异常向上抛出。"""
        packages = self._adb.list_packages(third_party=True)
        apps = [
            UserApp(package=package, name=self._display_name(package))
            for package in packages
        ]
        with self._lock:
            self._apps = apps
            self._loaded = True
            self._updated_at = time.time()
            self._error = None
        log.info("用户应用清单已刷新: %d 个", len(apps))

    def mark_error(self, error: object) -> None:
        """记录最近一次刷新错误；保留已有缓存。"""
        with self._lock:
            self._error = str(error)
        log.warning("刷新用户应用清单失败: %s", error)

    def discard(self, package: str) -> None:
        """卸载成功后从缓存移除，不重新读取设备。"""
        with self._lock:
            self._apps = [
                app for app in self._apps if app.package != package
            ]
            self._updated_at = time.time()
            self._error = None

    # ---- 连接成功时自动刷新 ---------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._watch, name="letv-app-catalog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            # 可能正卡在设备 shell 上，不阻塞退出。
            thread.join(timeout=1.0)
        self._thread = None

    def _watch(self) -> None:
        """只在连接由断到通时刷新一次，不做周期性刷新。"""
        was_connected = False
        while not self._stop.is_set():
            connected = self._is_connected()
            if connected and not was_connected:
                try:
                    self.refresh()
                except Exception as exc:  # 设备错误或传输层异常
                    self.mark_error(exc)
            was_connected = connected
            self._stop.wait(self._watch_interval)

    def _is_connected(self) -> bool:
        try:
            return bool(self._adb.is_connected())
        except Exception as exc:
            log.debug("检查 ADB 连接失败: %s", exc)
            return False
