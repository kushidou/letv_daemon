"""配置文件加载与校验。

所有电视参数都在 ``config.json`` 中调整，代码不硬编码设备地址。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .device.keys import UnknownActionError, normalize_action
from .validation import ensure_activity, ensure_package

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.json"
DEFAULT_DEVICE_HOST = "192.168.0.100"
DEFAULT_UDP_PORT = 9900
DEFAULT_WATCHDOG_TARGET = (
    "com.peter.tvhome.letv/com.gathin.tvhome.MainActivity"
)
# 未配置 inputs 时，沿用旧行为：HDMI 1 + apps 中的应用。
DEFAULT_HDMI_INPUT = "HDMI 1"
DEFAULT_HDMI_ACTION = "switch_hdmi_0"

_KNOWN_TOP_LEVEL = {
    "device",
    "udp",
    "web",
    "watchdog",
    "apps",
    "app_names",
    "inputs",
    "allow_dangerous_actions",
}
_KNOWN_DEVICE = {
    "host",
    "adb_port",
    "reconnect_interval",
    "command_timeout",
    "poll_interval",
    "offline_after_sec",
    "offline_poll_interval",
    "offline_confirm_polls",
    "adb_path",
    "transport",
}
_KNOWN_UDP = {"enabled", "host", "port", "timeout"}
_KNOWN_WEB = {"host", "port", "token"}
_KNOWN_WATCHDOG = {
    "enabled",
    "target",
    "stock_package",
    "debounce_sec",
    "poll_sec",
    "pause_file",
}


class ConfigError(ValueError):
    """配置文件缺失、格式错误或字段非法。"""


@dataclass
class DeviceConfig:
    host: str = DEFAULT_DEVICE_HOST
    adb_port: int = 5555
    reconnect_interval: float = 5.0
    command_timeout: float = 10.0
    poll_interval: float = 5.0
    # 连续离线超过 offline_after_sec 后，采样间隔放慢到 offline_poll_interval。
    offline_after_sec: float = 60.0
    offline_poll_interval: float = 30.0
    # 连续多少次探测失败后记录一次"关机/离线"事件。
    offline_confirm_polls: int = 3
    adb_path: str = "adb"
    transport: str = "auto"  # auto | adbutils | subprocess

    @property
    def serial(self) -> str:
        return f"{self.host}:{self.adb_port}"


@dataclass
class UdpConfig:
    enabled: bool = True
    host: str = DEFAULT_DEVICE_HOST
    port: int = DEFAULT_UDP_PORT
    timeout: float = 2.0


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8770
    token: str = ""


@dataclass
class WatchdogConfig:
    enabled: bool = True
    target: str = DEFAULT_WATCHDOG_TARGET
    stock_package: str = "com.stv.deskplatform"
    debounce_sec: float = 1.5
    poll_sec: float = 5.0
    pause_file: str = "PAUSE"


@dataclass
class AppConfig:
    package: str
    name: str
    activity: Optional[str] = None


@dataclass(frozen=True)
class InputConfig:
    """HomeKit / HA 的一个输入源。

    ``kind`` 为 ``app`` 时用 ``package``/``activity`` 启动应用；为 ``key``
    时发送 ``key`` 指定的动作。``targets`` 是前台包名（或
    ``package/activity`` 组件名），用于把电视上的手动操作映射回输入源。
    """

    name: str
    kind: str  # app | key
    package: Optional[str] = None
    activity: Optional[str] = None
    key: Optional[str] = None
    targets: tuple[str, ...] = ()


@dataclass
class Config:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    udp: UdpConfig = field(default_factory=UdpConfig)
    web: WebConfig = field(default_factory=WebConfig)
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    apps: List[AppConfig] = field(default_factory=list)
    # 应用管理页面的中文显示名：包名 → 名称；apps 中已有的名称作为回退。
    app_names: Dict[str, str] = field(default_factory=dict)
    inputs: List[InputConfig] = field(default_factory=list)
    allow_dangerous_actions: bool = False
    path: Optional[str] = None

    def find_app(self, package: Optional[str]) -> Optional[AppConfig]:
        if not package:
            return None
        for app in self.apps:
            if app.package == package:
                return app
        return None

    def display_name(self, package: str) -> Optional[str]:
        """返回应用显示名：优先 app_names，其次 apps[].name。"""
        name = self.app_names.get(package)
        if name:
            return name
        app = self.find_app(package)
        if app is not None and app.name != app.package:
            return app.name
        return None

    def protected_packages(self) -> set:
        """不应被网页卸载的包：桌面守护目标与原桌面。"""
        target = self.watchdog.target.split("/", 1)[0]
        return {target, self.watchdog.stock_package}


def _ensure_mapping(value: object, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} 必须是对象")
    return value


def _section(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = data.get(key, {})
    if value is None:
        return {}
    return _ensure_mapping(value, key)


def _warn_unknown(section: Dict[str, Any], allowed: set, path: str) -> None:
    for key in section:
        if key not in allowed:
            log.warning("配置文件 %s 包含未知字段: %s", path, key)


def _string(section: Dict[str, Any], key: str, default: str, path: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{path}.{key} 必须是字符串")
    return value


def _required_string(section: Dict[str, Any], key: str, path: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}.{key} 必须是非空字符串")
    return value.strip()


def _integer(
    section: Dict[str, Any],
    key: str,
    default: int,
    path: str,
    minimum: int,
    maximum: int,
) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}.{key} 必须是整数")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{path}.{key} 必须在 {minimum}..{maximum} 之间")
    return value


def _number(
    section: Dict[str, Any],
    key: str,
    default: float,
    path: str,
    minimum: float,
) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path}.{key} 必须是数字")
    value = float(value)
    if value < minimum:
        raise ConfigError(f"{path}.{key} 必须 ≥ {minimum}")
    return value


def _boolean(section: Dict[str, Any], key: str, default: bool, path: str) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{path}.{key} 必须是布尔值 true/false")
    return value


def _build_device(section: Dict[str, Any]) -> DeviceConfig:
    _warn_unknown(section, _KNOWN_DEVICE, "device")
    transport = _string(section, "transport", "auto", "device")
    if transport not in ("auto", "adbutils", "subprocess"):
        raise ConfigError("device.transport 必须是 auto / adbutils / subprocess")
    return DeviceConfig(
        host=_required_string(section, "host", "device")
        if "host" in section
        else DEFAULT_DEVICE_HOST,
        adb_port=_integer(section, "adb_port", 5555, "device", 1, 65535),
        reconnect_interval=_number(section, "reconnect_interval", 5.0, "device", 0.5),
        command_timeout=_number(section, "command_timeout", 10.0, "device", 1.0),
        poll_interval=_number(section, "poll_interval", 5.0, "device", 2.0),
        offline_after_sec=_number(
            section, "offline_after_sec", 60.0, "device", 0.0
        ),
        offline_poll_interval=_number(
            section, "offline_poll_interval", 30.0, "device", 0.0
        ),
        offline_confirm_polls=_integer(
            section, "offline_confirm_polls", 3, "device", 1, 1000
        ),
        adb_path=_string(section, "adb_path", "adb", "device"),
        transport=transport,
    )


def _build_udp(section: Dict[str, Any], device: DeviceConfig) -> UdpConfig:
    _warn_unknown(section, _KNOWN_UDP, "udp")
    host = section.get("host", device.host)
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("udp.host 必须是非空字符串")
    return UdpConfig(
        enabled=_boolean(section, "enabled", True, "udp"),
        host=host.strip(),
        port=_integer(
            section, "port", DEFAULT_UDP_PORT, "udp", 1, 65535
        ),
        timeout=_number(section, "timeout", 2.0, "udp", 0.1),
    )


def _build_web(section: Dict[str, Any]) -> WebConfig:
    _warn_unknown(section, _KNOWN_WEB, "web")
    return WebConfig(
        host=_string(section, "host", "0.0.0.0", "web"),
        port=_integer(section, "port", 8770, "web", 0, 65535),
        token=_string(section, "token", "", "web"),
    )


def _build_watchdog(section: Dict[str, Any]) -> WatchdogConfig:
    _warn_unknown(section, _KNOWN_WATCHDOG, "watchdog")
    pause_file = _string(section, "pause_file", "PAUSE", "watchdog")
    if not pause_file:
        raise ConfigError("watchdog.pause_file 不能为空")

    target = _string(
        section, "target", DEFAULT_WATCHDOG_TARGET, "watchdog"
    ).strip()
    if "/" not in target:
        raise ConfigError("watchdog.target 必须是 package/activity")
    package, activity = target.split("/", 1)
    try:
        package = ensure_package(package.strip())
        activity = ensure_activity(activity.strip())
    except ValueError as exc:
        raise ConfigError(f"watchdog.target: {exc}") from exc

    stock_package = _string(
        section, "stock_package", "com.stv.deskplatform", "watchdog"
    ).strip()
    try:
        stock_package = ensure_package(stock_package)
    except ValueError as exc:
        raise ConfigError(f"watchdog.stock_package: {exc}") from exc

    return WatchdogConfig(
        enabled=_boolean(section, "enabled", True, "watchdog"),
        target=f"{package}/{activity}",
        stock_package=stock_package,
        debounce_sec=_number(section, "debounce_sec", 1.5, "watchdog", 0.0),
        poll_sec=_number(section, "poll_sec", 5.0, "watchdog", 0.0),
        pause_file=pause_file,
    )


def _build_apps(raw: object) -> List[AppConfig]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("apps 必须是数组")

    apps: List[AppConfig] = []
    seen = set()
    for index, item in enumerate(raw):
        path = f"apps[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{path} 必须是对象")

        try:
            package = ensure_package(item.get("package"))
        except ValueError as exc:
            raise ConfigError(f"{path}.package: {exc}") from exc

        name = item.get("name", package)
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{path}.name 必须是非空字符串")
        name = name.strip()

        activity_raw = item.get("activity")
        activity: Optional[str] = None
        if activity_raw is not None:
            if not isinstance(activity_raw, str):
                raise ConfigError(f"{path}.activity 必须是字符串")
            activity_raw = activity_raw.strip()
            if activity_raw:
                try:
                    activity = ensure_activity(activity_raw)
                except ValueError as exc:
                    raise ConfigError(f"{path}.activity: {exc}") from exc

        if package in seen:
            log.warning("apps 中包名重复，已忽略: %s", package)
            continue
        seen.add(package)
        apps.append(AppConfig(package=package, name=name, activity=activity))
    return apps


_KNOWN_INPUT = {
    "name",
    "action",
    "package",
    "activity",
    "key",
    "target",
}


def _build_input_targets(raw: object, path: str) -> tuple:
    """解析 target：字符串或字符串数组（包名 / package/activity）。"""
    if raw is None:
        return ()
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = raw
    else:
        raise ConfigError(f"{path}.target 必须是字符串或字符串数组")

    targets = []
    for index, item in enumerate(values):
        item_path = f"{path}.target[{index}]"
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{item_path} 必须是非空字符串")
        value = item.strip()
        package, sep, activity = value.partition("/")
        try:
            package = ensure_package(package)
        except ValueError as exc:
            raise ConfigError(f"{item_path}: {exc}") from exc
        if sep:
            if not activity:
                raise ConfigError(f"{item_path} 缺少 Activity")
            try:
                activity = ensure_activity(activity)
            except ValueError as exc:
                raise ConfigError(f"{item_path}: {exc}") from exc
            value = f"{package}/{activity}"
        else:
            value = package
        if value not in targets:
            targets.append(value)
    return tuple(targets)


def _build_inputs(raw: object, apps: List[AppConfig]) -> List[InputConfig]:
    """构建输入源列表。

    未配置 ``inputs`` 时保持旧行为：``HDMI 1`` + ``apps`` 中的应用。
    配置了 ``inputs`` 后，它就是这个列表的**完整**定义（顺序即 HomeKit
    中的顺序），不再自动追加 HDMI 或 apps。
    """
    if raw is None:
        inputs = [
            InputConfig(
                name=DEFAULT_HDMI_INPUT,
                kind="key",
                key=DEFAULT_HDMI_ACTION,
            )
        ]
        for app in apps:
            inputs.append(
                InputConfig(
                    name=app.name,
                    kind="app",
                    package=app.package,
                    activity=app.activity,
                    targets=(app.package,),
                )
            )
        return inputs

    if not isinstance(raw, list):
        raise ConfigError("inputs 必须是数组")
    if not raw:
        raise ConfigError("inputs 不能为空；不配置该字段则自动使用 apps")

    inputs: List[InputConfig] = []
    seen = set()
    for index, item in enumerate(raw):
        path = f"inputs[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{path} 必须是对象")
        _warn_unknown(item, _KNOWN_INPUT, path)

        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{path}.name 必须是非空字符串")
        name = name.strip()
        if name in seen:
            log.warning("inputs 中名称重复，已忽略: %s", name)
            continue

        action = item.get("action")
        if not isinstance(action, str):
            raise ConfigError(f"{path}.action 必须是 app 或 key")
        action = action.strip().lower()
        if action in ("app", "launch"):
            kind = "app"
        elif action in ("key", "press"):
            kind = "key"
        else:
            raise ConfigError(f"{path}.action 必须是 app 或 key")

        package: Optional[str] = None
        activity: Optional[str] = None
        key: Optional[str] = None

        if kind == "app":
            if "key" in item:
                raise ConfigError(f"{path} 为 app 时不能包含 key")
            try:
                package = ensure_package(item.get("package"))
            except ValueError as exc:
                raise ConfigError(f"{path}.package: {exc}") from exc

            activity_raw = item.get("activity")
            if activity_raw is not None:
                if not isinstance(activity_raw, str):
                    raise ConfigError(f"{path}.activity 必须是字符串")
                activity_raw = activity_raw.strip()
                if activity_raw:
                    try:
                        activity = ensure_activity(activity_raw)
                    except ValueError as exc:
                        raise ConfigError(
                            f"{path}.activity: {exc}"
                        ) from exc
        else:
            if "package" in item or "activity" in item:
                raise ConfigError(f"{path} 为 key 时不能包含 package/activity")
            key_raw = item.get("key")
            if not isinstance(key_raw, str) or not key_raw.strip():
                raise ConfigError(f"{path}.key 必须是非空字符串")
            try:
                key = normalize_action(key_raw)
            except UnknownActionError as exc:
                raise ConfigError(f"{path}.key: {exc}") from exc

        targets = _build_input_targets(item.get("target"), path)
        if kind == "app" and not targets:
            # 默认用启动包名做状态回读，符合“target 检测对应包”的用法。
            targets = (package,)

        inputs.append(
            InputConfig(
                name=name,
                kind=kind,
                package=package,
                activity=activity,
                key=key,
                targets=targets,
            )
        )
        seen.add(name)

    if not inputs:
        raise ConfigError("inputs 不能为空")
    return inputs


def _build_app_names(raw: object) -> Dict[str, str]:
    """构建包名 → 中文显示名映射。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("app_names 必须是对象（包名 → 显示名）")

    names: Dict[str, str] = {}
    for key, value in raw.items():
        try:
            package = ensure_package(key)
        except ValueError as exc:
            raise ConfigError(f"app_names: {exc}") from exc
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"app_names.{package} 必须是非空字符串"
            )
        names[package] = value.strip()
    return names


def build_config(data: Dict[str, Any], path: Optional[str] = None) -> Config:
    """从已解析的 JSON 对象构建 Config（供测试与 load_config 使用）。"""
    if not isinstance(data, dict):
        raise ConfigError("配置文件根节点必须是对象")
    _warn_unknown(data, _KNOWN_TOP_LEVEL, "根")

    device = _build_device(_section(data, "device"))
    apps = _build_apps(data.get("apps"))
    return Config(
        device=device,
        udp=_build_udp(_section(data, "udp"), device),
        web=_build_web(_section(data, "web")),
        watchdog=_build_watchdog(_section(data, "watchdog")),
        apps=apps,
        app_names=_build_app_names(data.get("app_names")),
        inputs=_build_inputs(data.get("inputs"), apps),
        allow_dangerous_actions=_boolean(
            data, "allow_dangerous_actions", False, "根"
        ),
        path=path,
    )


def load_config(path: Optional[str] = None) -> Config:
    """读取并校验配置文件。

    路径优先级：显式参数 > 环境变量 LETV_DAEMON_CONFIG > ./config.json。
    """
    candidate = path or os.environ.get("LETV_DAEMON_CONFIG") or DEFAULT_CONFIG_PATH
    config_path = Path(candidate).expanduser()

    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在: {config_path.resolve()}")

    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {config_path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件 {config_path} 不是合法 JSON"
            f"（第 {exc.lineno} 行第 {exc.colno} 列）: {exc.msg}"
        ) from exc

    return build_config(data, path=str(config_path.resolve()))
