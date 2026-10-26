"""letv_tv 使用的 letv_daemon Web API 客户端。

本模块刻意不依赖 ``homeassistant``，只依赖 ``aiohttp`` 与标准库，
以便脱离 HA 单独测试。协议契约见 ``ha_custom_components/README.md``。

约定：

* GET 请求失败（超时 / 连接错误）自动重试一次；
* POST 动作请求**绝不**自动重试，避免重复按键或重复切换信号源；
* 所有错误都转换为 :class:`LetvError` 的子类，由调用方决定如何呈现。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
HEALTH_TIMEOUT = 5.0
ACTION_TIMEOUT = 20.0
# 绝对音量设置需要逐个注入音量键，100 级的差值可能需要几十秒。
VOLUME_SET_TIMEOUT = 120.0
RETRY_DELAY = 0.5
VOLUME_MAX = 100

# 集成侧固定使用的动作名（与 letv_daemon/device/keys.py 保持一致）。
ACTION_POWER = "power"
ACTION_POWER_OFF = "power_off"
ACTION_VOLUME_UP = "volume_up"
ACTION_VOLUME_DOWN = "volume_down"
ACTION_MUTE = "mute"

STATE_PATH = "/api/state"
REFRESH_STATE_PATH = "/api/state?refresh=1"


class LetvError(Exception):
    """所有 letv_daemon 客户端错误的基类。"""


class LetvConnectionError(LetvError):
    """无法连接守护服务（网络错误或超时）。"""


class LetvAuthError(LetvError):
    """访问令牌缺失或被拒绝（HTTP 401）。"""


class LetvApiError(LetvError):
    """守护服务返回了错误响应或非法响应。"""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class LetvForbiddenError(LetvApiError):
    """危险动作被守护服务拒绝（HTTP 403）。"""


def _optional_str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    return None


def _optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    return None


def _optional_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


@dataclass(frozen=True)
class LetvHealth:
    """``GET /api/health`` 的响应。"""

    status: str
    version: str
    connected: bool
    serial: str


@dataclass(frozen=True)
class LetvState:
    """``GET /api/state`` 的设备状态快照。"""

    online: bool = False
    foreground_package: Optional[str] = None
    foreground_activity: Optional[str] = None
    foreground_component: Optional[str] = None
    foreground_app_name: Optional[str] = None
    screen_on: Optional[bool] = None
    screen: str = "unknown"
    updated_at: float = 0.0
    updated_at_iso: Optional[str] = None
    error: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "LetvState":
        screen = _optional_str(payload.get("screen")) or "unknown"
        return cls(
            online=bool(payload.get("online")),
            foreground_package=_optional_str(payload.get("foreground_package")),
            foreground_activity=_optional_str(payload.get("foreground_activity")),
            foreground_component=_optional_str(
                payload.get("foreground_component")
            ),
            foreground_app_name=_optional_str(
                payload.get("foreground_app_name")
            ),
            screen_on=_optional_bool(payload.get("screen_on")),
            screen=screen,
            updated_at=_optional_float(payload.get("updated_at")) or 0.0,
            updated_at_iso=_optional_str(payload.get("updated_at_iso")),
            error=_optional_str(payload.get("error")),
        )


@dataclass(frozen=True)
class LetvAction:
    """``GET /api/actions`` 中的一条动作。"""

    action: str
    label: str
    transport: str = "adb"
    keycode: Optional[int] = None
    dangerous: bool = False


@dataclass(frozen=True)
class LetvApp:
    """``GET /api/apps`` 中的一条快捷启动项。"""

    package: str
    name: str
    activity: Optional[str] = None


@dataclass(frozen=True)
class LetvInput:
    """``GET /api/inputs`` 中的一个输入源。

    ``kind`` 为 ``app`` 时用 package/activity 启动应用；为 ``key`` 时发送
    key 指定的动作。``targets`` 用于把前台包名/组件名映射回输入源。
    """

    name: str
    kind: str  # app | key
    package: Optional[str] = None
    activity: Optional[str] = None
    key: Optional[str] = None
    targets: tuple[str, ...] = ()


@dataclass(frozen=True)
class LetvVolume:
    """``GET /api/volume`` 的媒体音量。"""

    level: int
    max: int = VOLUME_MAX

    @property
    def ratio(self) -> Optional[float]:
        if self.max <= 0:
            return None
        return self.level / self.max


def _parse_volume(payload: Mapping[str, Any]) -> LetvVolume:
    level = _optional_int(payload.get("volume"))
    if level is None:
        raise LetvApiError("volume 字段不是整数")
    maximum = _optional_int(payload.get("max")) or VOLUME_MAX
    if maximum <= 0:
        maximum = VOLUME_MAX
    return LetvVolume(
        level=max(0, min(maximum, level)),
        max=maximum,
    )


def _parse_action(payload: Mapping[str, Any]) -> Optional[LetvAction]:
    action = _optional_str(payload.get("action"))
    if action is None:
        return None
    return LetvAction(
        action=action,
        label=_optional_str(payload.get("label")) or action,
        transport=_optional_str(payload.get("transport")) or "adb",
        keycode=_optional_int(payload.get("keycode")),
        dangerous=bool(payload.get("dangerous")),
    )


def _parse_app(payload: Mapping[str, Any]) -> Optional[LetvApp]:
    package = _optional_str(payload.get("package"))
    if package is None:
        return None
    return LetvApp(
        package=package,
        name=_optional_str(payload.get("name")) or package,
        activity=_optional_str(payload.get("activity")),
    )


def _parse_input(payload: Mapping[str, Any]) -> Optional[LetvInput]:
    name = _optional_str(payload.get("name"))
    action = (_optional_str(payload.get("action")) or "").lower()
    if name is None or not action:
        return None

    package = _optional_str(payload.get("package"))
    activity = _optional_str(payload.get("activity"))
    key = _optional_str(payload.get("key"))

    raw_targets = payload.get("targets")
    targets: list[str] = []
    if isinstance(raw_targets, str):
        raw_targets = [raw_targets]
    if isinstance(raw_targets, list):
        for item in raw_targets:
            if isinstance(item, str) and item.strip():
                value = item.strip()
                if value not in targets:
                    targets.append(value)

    if action in ("app", "launch"):
        if package is None:
            return None
        if not targets:
            targets = [package]
        return LetvInput(
            name=name,
            kind="app",
            package=package,
            activity=activity,
            targets=tuple(targets),
        )
    if action in ("key", "press"):
        if key is None:
            return None
        return LetvInput(
            name=name,
            kind="key",
            key=key,
            targets=tuple(targets),
        )
    return None


class LetvClient:
    """访问 letv_daemon Web API 的异步客户端。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        token: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._session = session
        self.host = host
        self.port = int(port)
        self.token = token or ""
        self.timeout = float(timeout)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ---- 基础请求 -------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
        retry: bool = False,
    ) -> dict[str, Any]:
        """执行一次请求；仅网络层失败时按 ``retry`` 重试。"""
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Token"] = self.token

        attempts = 2 if retry else 1
        last_error: Optional[BaseException] = None

        for attempt in range(attempts):
            try:
                async with self._session.request(
                    method,
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(
                        total=timeout or self.timeout
                    ),
                ) as response:
                    body = await response.read()
                    data: Any = None
                    if body:
                        try:
                            data = json.loads(body)
                        except (ValueError, UnicodeDecodeError):
                            data = None

                    status = response.status
                    details = data if isinstance(data, dict) else {}
                    code = _optional_str(details.get("code"))
                    message = _optional_str(details.get("error")) or (
                        f"HTTP {status}"
                    )

                    if status == 401:
                        raise LetvAuthError(
                            message or "访问令牌被拒绝"
                        )
                    if status == 403:
                        raise LetvForbiddenError(
                            message, status=status, code=code
                        )
                    if status >= 400:
                        raise LetvApiError(
                            message, status=status, code=code
                        )
                    if not isinstance(data, dict):
                        raise LetvApiError(
                            "守护服务返回的不是 JSON 对象",
                            status=status,
                        )
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
                last_error = err
                if attempt + 1 >= attempts:
                    break
                _LOGGER.debug(
                    "请求 %s %s 失败，%.1fs 后重试: %s",
                    method,
                    url,
                    RETRY_DELAY,
                    err,
                )
                await asyncio.sleep(RETRY_DELAY)

        raise LetvConnectionError(
            f"无法连接 letv_daemon（{self.base_url}）：{last_error}"
        ) from last_error

    # ---- API 方法 -------------------------------------------------

    async def async_get_health(
        self, timeout: float = HEALTH_TIMEOUT
    ) -> LetvHealth:
        data = await self._request(
            "GET", "/api/health", timeout=timeout, retry=True
        )
        return LetvHealth(
            status=str(data.get("status") or ""),
            version=str(data.get("version") or ""),
            connected=bool(data.get("connected")),
            serial=str(data.get("serial") or ""),
        )

    async def async_get_state(self, *, refresh: bool = False) -> LetvState:
        data = await self._request(
            "GET", REFRESH_STATE_PATH if refresh else STATE_PATH, retry=True
        )
        return LetvState.from_payload(data)

    async def async_get_actions(self) -> list[LetvAction]:
        data = await self._request("GET", "/api/actions", retry=True)
        raw = data.get("actions")
        if not isinstance(raw, list):
            raise LetvApiError("actions 字段不是列表")
        actions: list[LetvAction] = []
        for item in raw:
            if isinstance(item, dict):
                action = _parse_action(item)
                if action is not None:
                    actions.append(action)
        return actions

    async def async_get_apps(self) -> list[LetvApp]:
        data = await self._request("GET", "/api/apps", retry=True)
        raw = data.get("apps")
        if not isinstance(raw, list):
            raise LetvApiError("apps 字段不是列表")
        apps: list[LetvApp] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            app = _parse_app(item)
            if app is None or app.package in seen:
                continue
            seen.add(app.package)
            apps.append(app)
        return apps

    async def async_get_inputs(self) -> list[LetvInput]:
        data = await self._request("GET", "/api/inputs", retry=True)
        raw = data.get("inputs")
        if not isinstance(raw, list):
            raise LetvApiError("inputs 字段不是列表")
        inputs: list[LetvInput] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            parsed = _parse_input(item)
            if parsed is None or parsed.name in seen:
                continue
            seen.add(parsed.name)
            inputs.append(parsed)
        return inputs

    async def async_get_volume(self) -> LetvVolume:
        data = await self._request(
            "GET", "/api/volume", retry=True
        )
        return _parse_volume(data)

    async def async_set_volume(
        self,
        level: int,
        timeout: float = VOLUME_SET_TIMEOUT,
    ) -> int:
        """设置绝对音量（0–100），返回设备实际生效的音量。"""
        data = await self._request(
            "POST",
            "/api/volume",
            payload={"volume": int(level)},
            timeout=timeout,
        )
        actual = _optional_int(data.get("volume"))
        if actual is None:
            raise LetvApiError("volume 字段不是整数")
        return max(0, min(VOLUME_MAX, actual))

    async def async_send_key(
        self, action: str, timeout: float = ACTION_TIMEOUT
    ) -> LetvAction:
        data = await self._request(
            "POST",
            "/api/key",
            payload={"action": action},
            timeout=timeout,
        )
        return _parse_action(data) or LetvAction(
            action=action, label=action
        )

    async def async_launch(
        self,
        package: str,
        activity: Optional[str] = None,
        timeout: float = ACTION_TIMEOUT,
    ) -> str:
        payload: dict[str, str] = {"package": package}
        if activity:
            payload["activity"] = activity
        data = await self._request(
            "POST",
            "/api/launch",
            payload=payload,
            timeout=timeout,
        )
        component = _optional_str(data.get("component"))
        return component or package

    async def async_send_commands(
        self,
        commands: Sequence[str],
        *,
        repeats: int = 1,
        delay: float = 0.0,
    ) -> None:
        """按顺序发送一组动作；供 ``remote.send_command`` 使用。"""
        for _ in range(max(1, repeats)):
            for command in commands:
                await self.async_send_key(command)
                if delay > 0:
                    await asyncio.sleep(delay)
