"""ADB 连接、命令执行与遥控控制。

默认优先使用 adbutils；未安装时自动回退到系统 adb 命令行，
两种实现共用同一套 :class:`ADBClient` 接口。
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import threading
import time
from typing import Callable, List, Optional, Sequence

from ..validation import ensure_activity, ensure_package
from .keys import Key, resolve
from .net import parse_host_port, tcp_probe

log = logging.getLogger(__name__)

_COMPONENT_RE = re.compile(
    r"^\s*([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/([A-Za-z0-9_.$]+)\s*$"
)
_AM_COMPONENT_RE = re.compile(
    r"cmp=([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)"
)
_AM_ERROR_MARKERS = ("Error:", "Error type", "does not exist", "Permission Denial")
_CONNECTION_ERROR_MARKERS = (
    "device offline",
    "no devices",
    "device not found",
    "cannot connect",
    "device unauthorized",
    "connection refused",
)
# 用来区分“文件为空”“文件不存在”和 shell 输出异常，避免依赖 adb 退出码。
_FILE_OK_MARKER = "__LETV_DAEMON_FILE_OK__"
_FILE_MISSING_MARKER = "__LETV_DAEMON_FILE_MISSING__"

# 系统设置中保存的媒体音量，取值是百分比（0–100）。
VOLUME_SETTING = "volume_music_speaker"
VOLUME_MAX = 100
# 设备只认音量键（settings put 不会改变实时音量）；一次 shell 调用可携带
# 多个 keyCode，分批发送并在每批后重读，避免超时重试造成大幅过量。
VOLUME_KEY_BATCH = 25
VOLUME_CORRECTION_PASSES = 2

# 设备长时间离线时，相同连接错误避免每隔几秒刷一条 WARNING。
_CONNECT_LOG_INTERVAL = 60.0


class AdbError(RuntimeError):
    """ADB 相关错误基类。"""


class AdbConnectionError(AdbError):
    """无法连接设备，或连接已断开。"""


class AdbCommandError(AdbError):
    """命令已下发，但设备返回失败。"""

    def __init__(
        self,
        message: str,
        command: Optional[str] = None,
        output: Optional[str] = None,
        returncode: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.command = command
        self.output = output
        self.returncode = returncode


class AdbFileNotFoundError(AdbError):
    """设备上的目标文件不存在。"""


class AdbTimeoutError(AdbConnectionError):
    """命令在超时时间内没有返回；通常意味着 adbd 假死，需要重置连接。"""


class Transport:
    """ADB 传输层的最小接口。"""

    def connect(self) -> None:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def get_state(self) -> str:
        raise NotImplementedError

    def shell(self, command: str, timeout: float = 10.0) -> str:
        raise NotImplementedError


class SubprocessTransport(Transport):
    """直接调用系统 adb 可执行文件。"""

    def __init__(
        self,
        serial: str,
        adb_path: str = "adb",
        connect_timeout: float = 5.0,
    ) -> None:
        self.serial = serial
        self.adb_path = adb_path or "adb"
        self.connect_timeout = connect_timeout

    def _run(
        self, args: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self.adb_path, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AdbError(
                f"未找到 adb 可执行文件: {self.adb_path}，请安装 adb 或配置 device.adb_path"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            # 设备侧命令超时通常是 adbd 假死，按连接错误处理以便触发重置。
            raise AdbTimeoutError(
                f"adb 命令超时（{timeout:g}s）: {' '.join(args)}"
            ) from exc

    def connect(self) -> None:
        result = self._run(["connect", self.serial], self.connect_timeout)
        text = f"{result.stdout}\n{result.stderr}".strip()
        lowered = text.lower()
        if (
            result.returncode != 0
            or "cannot" in lowered
            or "failed" in lowered
            or "unable" in lowered
        ):
            raise AdbConnectionError(f"连接 {self.serial} 失败: {text}")
        log.debug("adb connect %s: %s", self.serial, text)

    def disconnect(self) -> None:
        self._run(["disconnect", self.serial], self.connect_timeout)

    def get_state(self) -> str:
        result = self._run(
            ["-s", self.serial, "get-state"], self.connect_timeout
        )
        if result.returncode != 0:
            raise AdbConnectionError(
                f"设备 {self.serial} 状态不可用: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        return result.stdout.strip()

    def shell(self, command: str, timeout: float = 10.0) -> str:
        result = self._run(["-s", self.serial, "shell", command], timeout)
        output = f"{result.stdout}\n{result.stderr}".strip()
        lowered = output.lower()
        if any(marker in lowered for marker in _CONNECTION_ERROR_MARKERS):
            raise AdbConnectionError(
                f"设备 {self.serial} 连接异常: {output}"
            )
        if result.returncode != 0:
            raise AdbCommandError(
                f"命令执行失败（退出码 {result.returncode}）: {command}",
                command=command,
                output=output,
                returncode=result.returncode,
            )
        return result.stdout


class AdbutilsTransport(Transport):
    """基于 adbutils 库的传输层。"""

    def __init__(self, serial: str, connect_timeout: float = 5.0) -> None:
        try:
            import adbutils  # 延迟导入：未安装时仍可使用 subprocess 回退
            from adbutils.errors import AdbTimeout
        except ImportError as exc:
            raise AdbError("未安装 adbutils，无法使用 adbutils 传输层") from exc

        self.serial = serial
        self.connect_timeout = connect_timeout
        self._adb = adbutils.adb
        self._timeout_errors = (AdbTimeout, TimeoutError)

    def connect(self) -> None:
        try:
            self._adb.connect(self.serial, timeout=self.connect_timeout)
            state = self.get_state()
        except AdbConnectionError:
            raise
        except Exception as exc:  # adbutils 的异常类型随版本变化
            raise AdbConnectionError(f"连接 {self.serial} 失败: {exc}") from exc
        if state != "device":
            raise AdbConnectionError(f"设备 {self.serial} 状态异常: {state}")

    def disconnect(self) -> None:
        try:
            self._adb.disconnect(self.serial)
        except Exception as exc:
            log.debug("断开 %s 失败: %s", self.serial, exc)

    def get_state(self) -> str:
        # get_state() 自身使用 600 s 的默认超时，adbd 假死时会把守护线程卡住，
        # 因此这里显式用 open_transport 带上连接超时。
        try:
            device = self._adb.device(self.serial)
            with device.open_transport(
                "get-state", timeout=self.connect_timeout
            ) as conn:
                return conn.read_string_block()
        except self._timeout_errors as exc:
            raise AdbTimeoutError(
                f"读取 {self.serial} 状态超时（{self.connect_timeout:g}s）"
            ) from exc
        except Exception as exc:
            raise AdbConnectionError(f"读取 {self.serial} 状态失败: {exc}") from exc

    def shell(self, command: str, timeout: float = 10.0) -> str:
        try:
            return self._adb.device(self.serial).shell(command, timeout=timeout)
        except self._timeout_errors as exc:
            raise AdbTimeoutError(
                f"adb 命令超时（{timeout:g}s）: {command}"
            ) from exc
        except Exception as exc:
            message = str(exc)
            if any(marker in message.lower() for marker in _CONNECTION_ERROR_MARKERS):
                raise AdbConnectionError(
                    f"设备 {self.serial} 连接异常: {message}"
                ) from exc
            raise AdbCommandError(
                f"命令执行失败: {command}: {message}",
                command=command,
                output=message,
            ) from exc


def make_transport(
    serial: str, kind: str = "auto", adb_path: str = "adb"
) -> Transport:
    """按配置创建传输层。auto 优先 adbutils，失败则回退到 adb 命令行。"""
    if kind == "subprocess":
        return SubprocessTransport(serial, adb_path=adb_path)
    if kind == "adbutils":
        return AdbutilsTransport(serial)
    if kind != "auto":
        raise ValueError(f"未知的传输层类型: {kind}")

    try:
        transport = AdbutilsTransport(serial)
        log.info("使用 adbutils 作为 ADB 传输层")
        return transport
    except AdbError as exc:
        log.warning("adbutils 不可用（%s），回退到 adb 命令行", exc)
        return SubprocessTransport(serial, adb_path=adb_path)


class ADBClient:
    """带串行化、自动重连与安全校验的 ADB 客户端。"""

    def __init__(
        self,
        serial: str,
        transport: Optional[Transport] = None,
        reconnect_interval: float = 5.0,
        command_timeout: float = 10.0,
        allow_dangerous: bool = False,
        adb_path: str = "adb",
        transport_kind: str = "auto",
        host_runner: Optional[
            Callable[[Sequence[str], float], subprocess.CompletedProcess]
        ] = None,
        probe: Optional[Callable[[str, int, float], bool]] = None,
    ) -> None:
        self.serial = serial
        self.transport = transport or make_transport(
            serial, kind=transport_kind, adb_path=adb_path
        )
        self.reconnect_interval = reconnect_interval
        self.command_timeout = command_timeout
        self.allow_dangerous = allow_dangerous
        self.adb_path = adb_path or "adb"
        # 连接前先用一次 TCP 探测做门控，离线时不创建 adb 子进程。
        self._probe = probe or tcp_probe
        self._tcp_timeout = 1.0
        self._connect_listeners: List[Callable[[], None]] = []
        self._last_connect_msg: Optional[str] = None
        self._last_connect_warn = 0.0

        self._lock = threading.RLock()
        # 音量「读-改-写」整体串行化：单条 ADB 命令的 _lock 无法防止
        # 两个 set_volume 调用交错读值。
        self._volume_lock = threading.Lock()
        self._host_lock = threading.Lock()
        self._host_runner = host_runner or self._run_host
        self._connected = False
        self._generation = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def generation(self) -> int:
        """连接代次；每次重新建立连接时递增，供看门狗重启 logcat。

        刻意不加锁：查询方可能正在等待设备 shell，不能因此被阻塞。
        """
        return self._generation

    # ---- 连接管理 -------------------------------------------------

    def add_connect_listener(self, callback: Callable[[], None]) -> None:
        """注册连接成功回调；用于唤醒状态采样线程。"""
        with self._lock:
            self._connect_listeners.append(callback)

    def _log_connect_failure(self, message: str) -> None:
        """相同错误只在首次和每 60 秒记 WARNING，其余降为 DEBUG。"""
        now = time.monotonic()
        with self._lock:
            should_warn = (
                message != self._last_connect_msg
                or now - self._last_connect_warn >= _CONNECT_LOG_INTERVAL
            )
            if should_warn:
                self._last_connect_msg = message
                self._last_connect_warn = now
        if should_warn:
            log.warning("%s", message)
        else:
            log.debug("%s", message)

    def connect(self) -> bool:
        """主动连接并校验状态；成功返回 True。"""
        target = parse_host_port(self.serial)
        if target is not None and not self._probe(*target, self._tcp_timeout):
            with self._lock:
                self._connected = False
            # 电视关机期间每次重连都会走到这里；状态变化由 StateMonitor
            # 统一记录，探测失败本身只留 DEBUG，避免日志无限增长。
            log.debug("TCP %s 不可达", self.serial)
            return False

        listeners: List[Callable[[], None]] = []
        with self._lock:
            was_connected = self._connected
            try:
                self.transport.connect()
                state = self.transport.get_state()
                self._connected = state == "device"
                if not self._connected:
                    self._log_connect_failure(
                        f"设备 {self.serial} 状态异常: {state}"
                    )
            except AdbError as exc:
                self._connected = False
                self._log_connect_failure(f"连接 {self.serial} 失败: {exc}")
            except Exception as exc:  # 传输层实现可能抛任意异常
                self._connected = False
                self._log_connect_failure(f"连接 {self.serial} 出现异常: {exc}")
            if self._connected:
                self._last_connect_msg = None
                if not was_connected:
                    self._generation += 1
                    listeners = list(self._connect_listeners)
            connected = self._connected

        for callback in listeners:
            try:
                callback()
            except Exception:
                log.exception("ADB 连接回调失败")
        return connected

    def is_connected(self) -> bool:
        """轻量健康检查；状态读取失败视为断开。"""
        with self._lock:
            if not self._connected:
                return False
            try:
                state = self.transport.get_state()
            except Exception as exc:
                log.debug("读取设备状态失败: %s", exc)
                self._connected = False
                return False
            self._connected = state == "device"
            return self._connected

    def start(self) -> None:
        """先尝试连接，再启动后台重连线程。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self.connect()
            self._thread = threading.Thread(
                target=self._reconnect_loop,
                name=f"adb-reconnect-{self.serial}",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            # 线程是 daemon，设备无响应时没必要陪它等完整超时。
            thread.join(timeout=1.0)
        self._thread = None

    def _hard_reset(self) -> None:
        """强制断开当前传输层，让下一次 connect() 真正重建连接。

        adbd 假死时 ``get-state`` 仍可能正常，只有耗时命令会超时；
        必须显式 disconnect，否则 ``adb connect`` 只会回“already connected”。
        """
        try:
            self.transport.disconnect()
        except Exception as exc:
            log.debug("断开 %s 失败: %s", self.serial, exc)
        self._connected = False
        self._generation += 1

    def _reconnect_loop(self) -> None:
        was_connected = self._connected
        while not self._stop.wait(self.reconnect_interval):
            if not self.is_connected():
                if was_connected:
                    log.info("与 %s 的连接已断开，尝试重连", self.serial)
                self.connect()
            was_connected = self._connected

    # ---- 命令执行 -------------------------------------------------

    def shell(self, command: str, timeout: Optional[float] = None) -> str:
        """执行设备端 shell 命令；断线时自动重连并重试一次。"""
        return self._execute(
            command, self.command_timeout if timeout is None else timeout
        )

    def _execute(self, command: str, timeout: float) -> str:
        last_error: Optional[Exception] = None
        with self._lock:
            for attempt in range(2):
                if not self._connected and not self.connect():
                    last_error = AdbConnectionError(
                        f"未连接到设备 {self.serial}"
                    )
                    continue
                try:
                    return self.transport.shell(command, timeout=timeout)
                except AdbTimeoutError as exc:
                    last_error = exc
                    log.warning(
                        "命令超时，重置 ADB 连接后重试（%d/2）: %s",
                        attempt + 1,
                        exc,
                    )
                    self._hard_reset()
                except AdbCommandError:
                    raise
                except AdbError as exc:
                    last_error = exc
                    self._connected = False
                    log.warning(
                        "命令失败，重连后重试（%d/2）: %s", attempt + 1, exc
                    )
        raise AdbConnectionError(
            f"命令执行失败，设备不可用: {last_error}"
        )

    # ---- 控制接口 -------------------------------------------------

    def send_key(self, action: str) -> Key:
        """发送一个遥控按键，返回实际使用的 Key。"""
        key = resolve(action, allow_dangerous=self.allow_dangerous)
        log.info("发送按键 %s（keyCode=%d）", key.action, key.keycode)
        self._execute(f"input keyevent {key.keycode}", self.command_timeout)
        return key

    def resolve_activity(self, package: str) -> str:
        """解析包名的启动 Activity，返回 ``package/activity``。"""
        package = ensure_package(package)
        for category in ("LEANBACK_LAUNCHER", "LAUNCHER"):
            command = (
                "cmd package resolve-activity --brief "
                "-a android.intent.action.MAIN "
                f"-c android.intent.category.{category} {package}"
            )
            try:
                output = self._execute(command, self.command_timeout)
            except AdbConnectionError:
                raise
            except AdbError:
                continue
            component = _parse_component(output)
            if component and component.split("/", 1)[0] == package:
                return component
        raise AdbCommandError(
            f"无法解析 {package} 的启动 Activity，请在配置中指定 activity",
            command=None,
        )

    def launch_app(
        self, package: str, activity: Optional[str] = None
    ) -> str:
        """启动 App，返回实际使用的组件名。

        未指定 activity 时用 intent 解析启动：Android 6 没有 ``cmd`` 命令，
        因此不依赖 ``cmd package resolve-activity``；LEANBACK 类别失败后
        再回退到普通 LAUNCHER 类别。
        """
        package = ensure_package(package)
        if activity:
            activity = ensure_activity(activity)
            component = f"{package}/{activity}"
            command = f"am start -n {component}"
            output = self._execute(command, self.command_timeout)
            if any(marker in output for marker in _AM_ERROR_MARKERS):
                raise AdbCommandError(
                    f"启动 {package} 失败: {output.strip()}",
                    command=command,
                    output=output,
                )
            log.info("已启动 %s", component)
            return component

        return self._launch_via_intent(package)

    def _launch_via_intent(self, package: str) -> str:
        """通过 MAIN + LEANBACK/LAUNCHER 类别启动，兼容 Android 6。"""
        last_output = ""
        # 普通应用用 LEANBACK/LAUNCHER；原桌面这类 HOME 应用只声明 HOME。
        for category in ("LEANBACK_LAUNCHER", "LAUNCHER", "HOME"):
            command = (
                "am start -a android.intent.action.MAIN "
                f"-c android.intent.category.{category} -p {package}"
            )
            try:
                output = self._execute(command, self.command_timeout)
            except AdbConnectionError:
                raise
            except AdbCommandError as exc:
                last_output = exc.output or str(exc)
                continue

            if any(marker in output for marker in _AM_ERROR_MARKERS):
                last_output = output
                continue

            component = _parse_am_component(output) or package
            log.info("已启动 %s（intent，组件 %s）", package, component)
            return component

        raise AdbCommandError(
            f"启动 {package} 失败: 未找到可用的启动 Activity",
            command=None,
            output=last_output,
        )

    def list_packages(self, third_party: bool = False) -> List[str]:
        """列出已安装包名。"""
        command = "pm list packages -3" if third_party else "pm list packages"
        output = self._execute(command, self.command_timeout)
        packages = []
        for line in output.splitlines():
            line = line.strip().lstrip("﻿")
            if line.startswith("package:"):
                name = line[len("package:") :].strip()
                if name:
                    packages.append(name)
        return sorted(set(packages))

    # ---- 查询接口 -------------------------------------------------

    def get_volume(self) -> int:
        """读取媒体音量百分比（0–100）。"""
        command = f"settings get system {VOLUME_SETTING}"
        output = self._execute(command, self.command_timeout)
        text = output.strip()
        try:
            volume = int(text)
        except ValueError:
            raise AdbCommandError(
                f"无法解析音量设置: {text or '设备无输出'}",
                command=command,
                output=output,
            ) from None
        return max(0, min(VOLUME_MAX, volume))

    def set_volume(self, target: int) -> int:
        """把媒体音量调整到目标百分比，返回调整后的实际音量。

        设备上 ``settings put`` 只改设置值、不会改变实时音量，只有音量键
        有效。因此按差值注入音量键码，每批之后重读，必要时再修正。
        """
        target = max(0, min(VOLUME_MAX, int(target)))
        key_up = resolve("volume_up").keycode
        key_down = resolve("volume_down").keycode

        with self._volume_lock:
            for _ in range(VOLUME_CORRECTION_PASSES):
                current = self.get_volume()
                delta = target - current
                if delta == 0:
                    break
                keycode = key_up if delta > 0 else key_down
                remaining = abs(delta)
                while remaining > 0:
                    steps = min(remaining, VOLUME_KEY_BATCH)
                    command = "input keyevent " + " ".join(
                        [str(keycode)] * steps
                    )
                    timeout = max(
                        self.command_timeout, 5.0 + steps * 0.5
                    )
                    log.info(
                        "调整音量 %d → %d：发送 %d 个 keyCode=%d",
                        current,
                        target,
                        steps,
                        keycode,
                    )
                    self._execute(command, timeout)
                    remaining -= steps
            return self.get_volume()

    def uninstall_app(
        self, package: str, timeout: float = 120.0
    ) -> str:
        """卸载用户应用，返回设备输出。

        仅接受合法包名；失败（例如系统应用、设备管理员）时抛
        :class:`AdbCommandError`，保留原始输出便于网页展示。
        """
        package = ensure_package(package)
        command = f"pm uninstall {package}"
        output = self._execute(command, timeout)
        text = output.strip()
        if not re.search(r"(?m)^Success\s*$", text):
            raise AdbCommandError(
                f"卸载 {package} 失败: {text or '设备无输出'}",
                command=command,
                output=output,
            )
        log.info("已卸载应用 %s", package)
        return text

    def read_text_file(
        self, remote_path: str, timeout: Optional[float] = None
    ) -> str:
        """读取设备上的文本文件。

        用标记行区分文件不存在与空文件；不依赖旧版 adb shell 的退出码。
        """
        if not remote_path.strip():
            raise AdbError("远程路径不能为空")
        quoted = shlex.quote(remote_path)
        command = (
            f"if [ -f {quoted} ]; then echo {_FILE_OK_MARKER}; cat {quoted}; "
            f"else echo {_FILE_MISSING_MARKER}; fi"
        )
        output = self._execute(
            command, self.command_timeout if timeout is None else timeout
        )
        if output.startswith(_FILE_MISSING_MARKER):
            raise AdbFileNotFoundError(f"设备文件不存在: {remote_path}")
        if output.startswith(_FILE_OK_MARKER):
            content = output[len(_FILE_OK_MARKER) :]
            if content.startswith("\r\n"):
                content = content[2:]
            elif content.startswith("\n"):
                content = content[1:]
            return content
        raise AdbCommandError(
            f"读取设备文件失败: {remote_path}",
            command=command,
            output=output,
        )

    # ---- 主机侧文件操作 -------------------------------------------

    def _run_host(
        self, args: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess:
        """在主机上执行 adb 命令；与设备 shell 分开，避免长时间阻塞状态轮询。"""
        try:
            return subprocess.run(
                [self.adb_path, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AdbError(
                f"未找到 adb 可执行文件: {self.adb_path}，"
                "请安装 adb 或配置 device.adb_path"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbCommandError(
                f"adb 命令超时（{timeout:g}s）: {' '.join(args)}"
            ) from exc
        except OSError as exc:
            raise AdbError(f"执行 adb 失败: {exc}") from exc

    def _host_command(
        self, args: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess:
        # 即使设备层用 adbutils，主机侧 adb 也要先知道这个设备。
        if not self.connect():
            raise AdbConnectionError(f"未连接到设备 {self.serial}")
        with self._host_lock:
            return self._host_runner(args, timeout)

    @staticmethod
    def _combined_output(result: subprocess.CompletedProcess) -> str:
        return f"{result.stdout or ''}\n{result.stderr or ''}".strip()

    def _check_transport_output(self, output: str) -> None:
        lowered = output.lower()
        if any(marker in lowered for marker in _CONNECTION_ERROR_MARKERS):
            self._connected = False
            raise AdbConnectionError(
                f"设备 {self.serial} 连接异常: {output or 'adb 无输出'}"
            )

    def install_apk(self, apk_path: str, timeout: float = 600.0) -> str:
        """用 ``adb install -r`` 安装 APK，返回 adb 的输出。

        安装失败时抛 :class:`AdbCommandError`，其中保留完整输出，
        便于直接展示在网页上。
        """
        path = os.path.abspath(os.path.expanduser(apk_path))
        if not os.path.isfile(path):
            raise AdbError(f"APK 文件不存在: {apk_path}")

        args = ["-s", self.serial, "install", "-r", path]
        result = self._host_command(args, timeout)
        output = self._combined_output(result)
        self._check_transport_output(output)
        lowered = output.lower()
        if (
            result.returncode != 0
            or "success" not in lowered
            or "failure" in lowered
        ):
            raise AdbCommandError(
                f"APK 安装失败: {output or 'adb 未返回输出'}",
                command=" ".join(args),
                output=output,
                returncode=result.returncode,
            )
        log.info("APK 安装成功: %s", path)
        return output

    def push_file(
        self, local_path: str, remote_path: str, timeout: float = 120.0
    ) -> str:
        """用 ``adb push`` 把主机文件传到设备，返回 adb 的输出。"""
        path = os.path.abspath(os.path.expanduser(local_path))
        if not os.path.isfile(path):
            raise AdbError(f"本地文件不存在: {local_path}")
        if not remote_path.strip():
            raise AdbError("远程路径不能为空")

        args = ["-s", self.serial, "push", path, remote_path]
        result = self._host_command(args, timeout)
        output = self._combined_output(result)
        self._check_transport_output(output)
        lowered = output.lower()
        if (
            result.returncode != 0
            or "error:" in lowered
            or "failed" in lowered
        ):
            raise AdbCommandError(
                f"文件传输失败: {output or 'adb 未返回输出'}",
                command=" ".join(args),
                output=output,
                returncode=result.returncode,
            )
        log.info("已推送文件: %s -> %s", path, remote_path)
        return output


def _parse_component(output: str) -> Optional[str]:
    """从 resolve-activity 输出中提取最后一个组件名。"""
    component = None
    for line in output.splitlines():
        match = _COMPONENT_RE.match(line)
        if match:
            component = f"{match.group(1)}/{match.group(2)}"
    return component


def _parse_am_component(output: str) -> Optional[str]:
    """从 am start 的输出中提取已解析的组件名（可能不存在）。"""
    match = _AM_COMPONENT_RE.search(output)
    return match.group(1) if match else None
