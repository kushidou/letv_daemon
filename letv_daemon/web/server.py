"""基于标准库 http.server 的控制台与 JSON API。

API 契约：

    GET  /api/health             服务与连接状态
    GET  /api/state              设备状态（?refresh=1 强制刷新）
    GET  /api/volume             媒体音量（百分比）
    GET  /api/actions            可用按键
    GET  /api/apps               快捷启动列表
    GET  /api/inputs             可配置的输入源（名称/动作/回读目标）
    GET  /api/user-apps          用户应用清单（缓存，不访问设备）
    GET  /api/packages           已安装包名（?third_party=1）
    GET  /api/launcher-config    读取自建桌面 config.json（含格式化文本）
    GET  /api/watchdog           桌面守护状态
    POST /api/key                {"action": "home"}（ADB/UDP 自动路由）
    POST /api/volume             {"volume": 0..100}（音量键差值调整）
    POST /api/launch             {"package": "...", "activity": "..."}
    POST /api/user-apps/refresh  重新读取用户应用清单
    POST /api/uninstall          {"package": "..."}（pm uninstall）
    POST /api/apk                原始 APK 请求体（adb install -r）
    POST /api/wallpaper          原始图片请求体（adb push）
    POST /api/launcher-config    JSON 请求体，校验后原子替换桌面配置
    POST /api/refresh            立即采集状态
    POST /api/watchdog           {"action": "start|stop|pause|resume"}
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import shlex
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from .. import __version__
from ..config import Config
from ..device.adb import (
    ADBClient,
    AdbCommandError,
    AdbConnectionError,
    AdbError,
    AdbFileNotFoundError,
    VOLUME_MAX,
)
from ..device.apps import UserAppCatalog
from ..device.control import DeviceController, UDPUnavailableError
from ..device.keys import (
    DangerousActionError,
    UnknownActionError,
    list_keys,
    list_udp_actions,
)
from ..device.state import DeviceState, StateMonitor
from ..device.udp import UdpError
from ..validation import ensure_package
from ..watchdog import Watchdog

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 64 * 1024

# 上传限制：APK 通常几十 MB，壁纸几 MB；给足余量但避免磁盘被占满。
MAX_APK_BYTES = 300 * 1024 * 1024
MAX_WALLPAPER_BYTES = 20 * 1024 * 1024
UPLOAD_CHUNK = 1024 * 1024
APK_INSTALL_TIMEOUT = 600.0
PUSH_TIMEOUT = 120.0
WALLPAPER_DIR = "/sdcard/Android/data/com.peter.tvhome.letv/files/wallpapers"
LAUNCHER_CONFIG_PATH = (
    "/sdcard/Android/data/com.peter.tvhome.letv/files/config.json"
)
MAX_LAUNCHER_CONFIG_BYTES = 1024 * 1024
UPLOAD_PATHS = ("/api/apk", "/api/wallpaper", "/api/launcher-config")

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    411: "length_required",
    413: "payload_too_large",
    500: "internal_error",
    502: "bad_gateway",
    503: "unavailable",
}


@dataclass
class ServerContext:
    config: Config
    adb: ADBClient
    monitor: StateMonitor
    app_catalog: Optional[UserAppCatalog] = None
    watchdog: Optional[Watchdog] = None
    controller: Optional[DeviceController] = None


class LetvHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    ctx: ServerContext


def detect_image_extension(path: str) -> Optional[str]:
    """按文件头识别图片格式，返回设备上使用的扩展名。"""
    try:
        with open(path, "rb") as handle:
            header = handle.read(12)
    except OSError:
        return None

    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if header.startswith(b"BM"):
        return ".bmp"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return ".webp"
    return None


def wallpaper_filename(extension: str) -> str:
    """用操作时间生成壁纸文件名，精确到微秒以避免覆盖。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f") + extension


_BOM = chr(0xFEFF)


def pretty_json(content: str) -> Tuple[bool, str]:
    """返回 (是否为合法 JSON, 格式化文本)；非法时原样返回。"""
    try:
        value = json.loads(content.lstrip(_BOM))
    except (json.JSONDecodeError, ValueError):
        return False, content
    return True, json.dumps(value, ensure_ascii=False, indent=2) + "\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"letv-daemon/{__version__}"
    sys_version = ""

    # ---- 基础响应 -------------------------------------------------

    def _send_bytes(
        self,
        status: int,
        data: bytes,
        content_type: str,
        extra_headers: Optional[Sequence[Tuple[str, str]]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in extra_headers or ():
            self.send_header(key, value)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _send_json(
        self,
        status: int,
        payload: Dict[str, object],
        extra_headers: Optional[Sequence[Tuple[str, str]]] = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            extra_headers,
        )

    def _error(self, status: int, message: str) -> None:
        self._send_json(
            status,
            {"error": message, "code": _STATUS_CODES.get(status, "error")},
        )

    def log_message(self, fmt: str, *args: object) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---- 鉴权 -----------------------------------------------------

    def _require_auth(self) -> bool:
        token = self.server.ctx.config.web.token
        if not token:
            return True

        supplied = self.headers.get("X-Token") or ""
        if not supplied:
            authorization = self.headers.get("Authorization", "")
            if authorization.lower().startswith("bearer "):
                supplied = authorization[7:].strip()
        if not supplied:
            query = parse_qs(urlsplit(self.path).query)
            supplied = (query.get("token") or [""])[0]

        if supplied and hmac.compare_digest(str(supplied), token):
            return True

        self._send_json(
            401,
            {"error": "需要访问令牌", "code": "unauthorized"},
            extra_headers=[("WWW-Authenticate", 'Bearer realm="letv-daemon"')],
        )
        return False

    # ---- 请求分发 -------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._serve_index()
                return
            if path.startswith("/static/"):
                self._serve_static(path)
                return
            if path.startswith("/api/"):
                if not self._require_auth():
                    return
                self._route_get(path)
                return
            self._error(404, "未找到")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("处理 GET %s 失败", path)
            self._error(500, "服务器内部错误")

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if not path.startswith("/api/"):
            self._error(404, "未找到")
            return
        if path in UPLOAD_PATHS:
            # 上传路由可能在未读完请求体时拒绝（401/413），
            # 不让残留字节污染同一条 keep-alive 连接。
            self.close_connection = True
        if not self._require_auth():
            return
        try:
            self._route_post(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("处理 POST %s 失败", path)
            self._error(500, "服务器内部错误")

    def _route_get(self, path: str) -> None:
        if path == "/api/health":
            self._api_health()
        elif path == "/api/state":
            self._api_state()
        elif path == "/api/volume":
            self._api_volume()
        elif path == "/api/actions":
            self._api_actions()
        elif path == "/api/apps":
            self._api_apps()
        elif path == "/api/inputs":
            self._api_inputs()
        elif path == "/api/user-apps":
            self._api_user_apps()
        elif path == "/api/packages":
            self._api_packages()
        elif path == "/api/launcher-config":
            self._api_launcher_config()
        elif path == "/api/watchdog":
            self._api_watchdog_status()
        else:
            self._error(404, "未知 API")

    def _route_post(self, path: str) -> None:
        if path == "/api/key":
            self._api_key()
        elif path == "/api/volume":
            self._api_volume_post()
        elif path == "/api/launch":
            self._api_launch()
        elif path == "/api/user-apps/refresh":
            self._api_user_apps_refresh()
        elif path == "/api/uninstall":
            self._api_uninstall()
        elif path == "/api/apk":
            self._api_apk()
        elif path == "/api/wallpaper":
            self._api_wallpaper()
        elif path == "/api/launcher-config":
            self._api_launcher_config_update()
        elif path == "/api/refresh":
            self._api_refresh()
        elif path == "/api/watchdog":
            self._api_watchdog_action()
        else:
            self._error(404, "未知 API")

    # ---- GET API --------------------------------------------------

    def _api_health(self) -> None:
        ctx = self.server.ctx
        try:
            connected = ctx.adb.is_connected()
        except Exception:
            connected = False
        self._send_json(
            200,
            {
                "status": "ok",
                "version": __version__,
                "connected": connected,
                "serial": ctx.config.device.serial,
            },
        )

    def _api_state(self) -> None:
        ctx = self.server.ctx
        query = parse_qs(urlsplit(self.path).query)
        refresh = (query.get("refresh") or ["0"])[0].lower()
        if refresh in ("1", "true", "yes", "on"):
            state = ctx.monitor.refresh()
        else:
            state = ctx.monitor.get()
        self._send_json(200, self._state_payload(state))

    def _state_payload(self, state: DeviceState) -> Dict[str, object]:
        ctx = self.server.ctx
        payload = state.as_dict()
        app = ctx.config.find_app(state.foreground_package)
        payload["foreground_app_name"] = app.name if app else None
        return payload

    def _api_volume(self) -> None:
        # 设备离线时直接返回缓存结论，避免 HA 每次轮询都触发一次 TCP 探测。
        state = self.server.ctx.monitor.get()
        if state.updated_at and not state.online:
            self._error(503, state.error or "设备离线")
            return

        try:
            volume = self.server.ctx.adb.get_volume()
        except AdbConnectionError as exc:
            self._error(503, str(exc))
            return
        except AdbError as exc:
            self._error(502, str(exc))
            return
        self._send_json(200, {"volume": volume, "max": VOLUME_MAX})

    def _api_volume_post(self) -> None:
        body = self._read_json()
        if body is None:
            return

        volume = body.get("volume")
        # bool 是 int 的子类，必须显式排除，否则 true/false 会被当成 1/0。
        if isinstance(volume, bool) or not isinstance(volume, int):
            self._error(400, "volume 必须是 0–100 的整数")
            return
        if volume < 0 or volume > VOLUME_MAX:
            self._error(400, f"volume 必须在 0–{VOLUME_MAX} 之间")
            return

        try:
            actual = self.server.ctx.adb.set_volume(volume)
        except AdbConnectionError as exc:
            self._error(503, str(exc))
            return
        except AdbError as exc:
            self._error(502, str(exc))
            return
        self._send_json(200, {"volume": actual, "max": VOLUME_MAX})

    def _api_actions(self) -> None:
        ctx = self.server.ctx
        allow = ctx.config.allow_dangerous_actions
        actions = [
            dict(key.as_dict(), transport="adb")
            for key in list_keys(allow_dangerous=allow)
        ]
        if ctx.controller is not None and ctx.controller.udp_available:
            actions.extend(
                action.as_dict()
                for action in list_udp_actions(allow_dangerous=allow)
            )
        self._send_json(200, {"actions": actions})

    def _api_apps(self) -> None:
        apps = self.server.ctx.config.apps
        self._send_json(
            200,
            {
                "apps": [
                    {
                        "package": app.package,
                        "name": app.name,
                        "activity": app.activity,
                    }
                    for app in apps
                ]
            },
        )

    def _api_inputs(self) -> None:
        inputs = self.server.ctx.config.inputs
        self._send_json(
            200,
            {
                "inputs": [
                    {
                        "name": item.name,
                        "action": item.kind,
                        "package": item.package,
                        "activity": item.activity,
                        "key": item.key,
                        "targets": list(item.targets),
                    }
                    for item in inputs
                ]
            },
        )

    def _user_apps_payload(self) -> Optional[Dict[str, object]]:
        ctx = self.server.ctx
        catalog = ctx.app_catalog
        if catalog is None:
            self._error(503, "应用清单不可用")
            return None
        payload = catalog.snapshot()
        payload["protected"] = sorted(ctx.config.protected_packages())
        return payload

    def _api_user_apps(self) -> None:
        """返回缓存的应用清单，绝不在读取时访问设备。"""
        payload = self._user_apps_payload()
        if payload is not None:
            self._send_json(200, payload)

    def _api_user_apps_refresh(self) -> None:
        catalog = self.server.ctx.app_catalog
        if catalog is None:
            self._error(503, "应用清单不可用")
            return

        try:
            catalog.refresh()
        except AdbConnectionError as exc:
            catalog.mark_error(exc)
            self._error(503, str(exc))
            return
        except AdbError as exc:
            catalog.mark_error(exc)
            self._error(502, str(exc))
            return

        payload = self._user_apps_payload()
        if payload is not None:
            self._send_json(200, payload)

    def _api_uninstall(self) -> None:
        body = self._read_json()
        if body is None:
            return

        try:
            package = ensure_package(body.get("package"))
        except ValueError as exc:
            self._error(400, str(exc))
            return

        ctx = self.server.ctx
        if not ctx.config.allow_dangerous_actions:
            self._error(
                403,
                "卸载应用属于危险操作，已被拒绝；"
                "如确需使用请将 allow_dangerous_actions 设为 true",
            )
            return
        if package in ctx.config.protected_packages():
            self._error(
                403,
                f"{package} 是桌面守护的关键应用，禁止通过网页卸载",
            )
            return

        catalog = ctx.app_catalog
        if catalog is None:
            self._error(503, "应用清单不可用")
            return
        if not catalog.contains(package):
            self._error(
                400,
                f"{package} 不在当前用户应用清单中，请先刷新清单",
            )
            return

        try:
            output = ctx.adb.uninstall_app(package)
        except AdbConnectionError as exc:
            self._error(503, str(exc))
            return
        except AdbError as exc:
            self._error(502, str(exc))
            return

        catalog.discard(package)
        payload = self._user_apps_payload()
        if payload is None:
            return
        payload["status"] = "ok"
        payload["package"] = package
        payload["output"] = output
        self._send_json(200, payload)

    def _api_packages(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        raw = (query.get("third_party") or ["0"])[0].lower()
        third_party = raw in ("1", "true", "yes", "on")
        try:
            packages = self.server.ctx.adb.list_packages(third_party=third_party)
        except AdbConnectionError as exc:
            self._error(503, str(exc))
            return
        except AdbError as exc:
            self._error(502, str(exc))
            return
        self._send_json(200, {"packages": packages})

    def _api_launcher_config(self) -> None:
        try:
            content = self.server.ctx.adb.read_text_file(LAUNCHER_CONFIG_PATH)
        except AdbFileNotFoundError as exc:
            self._error(404, str(exc))
            return
        except AdbConnectionError as exc:
            self._error(503, str(exc))
            return
        except AdbError as exc:
            self._error(502, str(exc))
            return

        size = len(content.encode("utf-8"))
        if size > MAX_LAUNCHER_CONFIG_BYTES:
            self._error(502, "桌面配置超过 1 MiB，已拒绝读取")
            return
        valid, pretty = pretty_json(content)
        self._send_json(
            200,
            {
                "path": LAUNCHER_CONFIG_PATH,
                "size": size,
                "valid": valid,
                "content": content,
                "pretty": pretty,
            },
        )

    def _api_watchdog_status(self) -> None:
        watchdog = self.server.ctx.watchdog
        if watchdog is None:
            self._send_json(
                200,
                {
                    "available": False,
                    "enabled": False,
                    "running": False,
                    "paused": False,
                },
            )
            return
        self._send_json(200, dict(watchdog.status(), available=True))

    # ---- POST API -------------------------------------------------

    def _read_json(self) -> Optional[Dict[str, object]]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._error(411, "缺少 Content-Length")
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self._error(400, "Content-Length 非法")
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            self._error(413 if length > MAX_BODY_BYTES else 400, "请求体为空或过大")
            return None

        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "请求体不是合法 JSON")
            return None
        if not isinstance(data, dict):
            self._error(400, "请求体必须是 JSON 对象")
            return None
        return data

    @staticmethod
    def _discard_file(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    def _upload_name(self) -> str:
        raw = self.headers.get("X-Filename", "")
        name = os.path.basename(unquote(raw)).strip()
        return name[:200]

    def _receive_upload(
        self, limit: int, suffix: str
    ) -> Optional[Tuple[str, int]]:
        """把原始请求体流式写入临时文件；失败时已发送错误响应。"""
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._error(411, "缺少 Content-Length")
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self._error(400, "Content-Length 非法")
            return None
        if length <= 0:
            self._error(400, "上传内容为空")
            return None
        if length > limit:
            self._error(
                413,
                f"文件过大，最大允许 {limit // (1024 * 1024)} MiB",
            )
            return None

        fd, path = tempfile.mkstemp(prefix="letv-upload-", suffix=suffix)
        interrupted = False
        try:
            with os.fdopen(fd, "wb") as handle:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(UPLOAD_CHUNK, remaining))
                    if not chunk:
                        interrupted = True
                        break
                    handle.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            self._discard_file(path)
            raise

        if interrupted:
            self._discard_file(path)
            self._error(400, "上传中断，请重试")
            return None
        return path, length

    def _api_key(self) -> None:
        body = self._read_json()
        if body is None:
            return

        action = body.get("action")
        if not isinstance(action, str) or not action.strip():
            self._error(400, "action 必须是非空字符串")
            return

        controller = self.server.ctx.controller
        if controller is None:
            self._error(503, "控制通道不可用")
            return
        try:
            outcome = controller.send(action.strip())
        except DangerousActionError as exc:
            self._error(403, str(exc))
            return
        except UDPUnavailableError as exc:
            self._error(503, str(exc))
            return
        except UnknownActionError as exc:
            self._error(400, str(exc))
            return
        except AdbConnectionError as exc:
            self._error(503, str(exc))
            return
        except AdbError as exc:
            self._error(502, str(exc))
            return
        except UdpError as exc:
            self._error(502, str(exc))
            return

        self._send_json(200, outcome.as_dict())

    def _api_launch(self) -> None:
        body = self._read_json()
        if body is None:
            return

        package = body.get("package")
        activity = body.get("activity")
        if not isinstance(package, str) or not package.strip():
            self._error(400, "package 必须是非空字符串")
            return
        if activity is not None and not isinstance(activity, str):
            self._error(400, "activity 必须是字符串")
            return

        try:
            component = self.server.ctx.adb.launch_app(
                package.strip(), activity.strip() if activity else None
            )
        except ValueError as exc:
            self._error(400, str(exc))
            return
        except AdbConnectionError as exc:
            self._error(503, str(exc))
            return
        except AdbError as exc:
            self._error(502, str(exc))
            return

        self._send_json(
            200,
            {"package": package.strip(), "component": component},
        )

    def _api_apk(self) -> None:
        upload = self._receive_upload(MAX_APK_BYTES, ".apk")
        if upload is None:
            return
        path, size = upload
        try:
            with open(path, "rb") as handle:
                magic = handle.read(2)
            if magic != b"PK":
                self._error(400, "文件不是有效的 APK（ZIP 头校验失败）")
                return

            filename = self._upload_name() or "upload.apk"
            output = self.server.ctx.adb.install_apk(
                path, timeout=APK_INSTALL_TIMEOUT
            )
        except AdbConnectionError as exc:
            self._error(503, str(exc))
            return
        except AdbError as exc:
            self._error(502, str(exc))
            return
        except OSError as exc:
            self._error(500, f"读取上传文件失败: {exc}")
            return
        finally:
            self._discard_file(path)

        self._send_json(
            200,
            {
                "status": "ok",
                "filename": filename,
                "size": size,
                "output": output,
            },
        )

    def _api_wallpaper(self) -> None:
        upload = self._receive_upload(MAX_WALLPAPER_BYTES, ".img")
        if upload is None:
            return
        path, size = upload
        try:
            extension = detect_image_extension(path)
            if extension is None:
                self._error(
                    400, "仅支持 PNG / JPEG / WebP / GIF / BMP 图片"
                )
                return

            filename = wallpaper_filename(extension)
            remote = f"{WALLPAPER_DIR}/{filename}"
            self.server.ctx.adb.shell(
                f"mkdir -p {shlex.quote(WALLPAPER_DIR)}"
            )
            output = self.server.ctx.adb.push_file(
                path, remote, timeout=PUSH_TIMEOUT
            )
        except AdbConnectionError as exc:
            self._error(503, str(exc))
            return
        except AdbError as exc:
            self._error(502, str(exc))
            return
        finally:
            self._discard_file(path)

        self._send_json(
            200,
            {
                "status": "ok",
                "filename": filename,
                "remote_path": remote,
                "size": size,
                "output": output,
            },
        )

    def _api_launcher_config_update(self) -> None:
        upload = self._receive_upload(
            MAX_LAUNCHER_CONFIG_BYTES, ".json"
        )
        if upload is None:
            return

        path, size = upload
        try:
            try:
                with open(path, "rb") as handle:
                    raw = handle.read()
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                self._error(400, "config.json 必须是 UTF-8 编码")
                return
            except OSError as exc:
                self._error(500, f"读取上传文件失败: {exc}")
                return

            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                self._error(400, f"config.json 不是合法 JSON: {exc}")
                return
            if not isinstance(data, dict):
                self._error(400, "config.json 顶层必须是 JSON 对象")
                return
            pretty = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

            remote_dir = os.path.dirname(LAUNCHER_CONFIG_PATH)
            remote_tmp = LAUNCHER_CONFIG_PATH + ".tmp"
            adb = self.server.ctx.adb
            adb.shell(f"mkdir -p {shlex.quote(remote_dir)}")
            try:
                adb.push_file(path, remote_tmp, timeout=PUSH_TIMEOUT)
                adb.shell(
                    f"mv -f {shlex.quote(remote_tmp)} "
                    f"{shlex.quote(LAUNCHER_CONFIG_PATH)}"
                )
            except AdbError:
                try:
                    adb.shell(f"rm -f {shlex.quote(remote_tmp)}")
                except AdbError:
                    pass
                raise
        except AdbConnectionError as exc:
            self._error(503, str(exc))
            return
        except AdbError as exc:
            self._error(502, str(exc))
            return
        finally:
            self._discard_file(path)

        self._send_json(
            200,
            {
                "status": "ok",
                "path": LAUNCHER_CONFIG_PATH,
                "size": size,
                "pretty": pretty,
            },
        )

    def _api_refresh(self) -> None:
        state = self.server.ctx.monitor.refresh()
        self._send_json(200, self._state_payload(state))

    def _api_watchdog_action(self) -> None:
        body = self._read_json()
        if body is None:
            return

        action = body.get("action")
        if not isinstance(action, str) or not action.strip():
            self._error(400, "action 必须是非空字符串")
            return
        action = action.strip().lower()

        watchdog = self.server.ctx.watchdog
        if watchdog is None:
            self._error(503, "桌面守护不可用")
            return

        try:
            if action == "start":
                watchdog.start()
            elif action == "stop":
                watchdog.stop()
            elif action == "pause":
                watchdog.pause()
            elif action == "resume":
                watchdog.resume()
            else:
                self._error(400, f"未知的守护动作: {action}")
                return
        except OSError as exc:
            log.warning("桌面守护操作失败（%s）: %s", action, exc)
            self._error(500, f"桌面守护操作失败: {exc}")
            return

        self._send_json(200, dict(watchdog.status(), available=True))

    # ---- 静态文件 -------------------------------------------------

    def _serve_index(self) -> None:
        index = STATIC_DIR / "index.html"
        if not index.is_file():
            self._error(500, "控制台页面缺失")
            return
        self._send_bytes(
            200, index.read_bytes(), "text/html; charset=utf-8"
        )

    def _serve_static(self, path: str) -> None:
        relative = unquote(path[len("/static/") :])
        if not relative:
            self._error(404, "未找到")
            return

        try:
            target = (STATIC_DIR / relative).resolve()
            target.relative_to(STATIC_DIR)
        except (ValueError, OSError):
            log.warning("拒绝越界的静态文件请求: %s", path)
            self._error(404, "未找到")
            return

        if not target.is_file():
            self._error(404, "未找到")
            return

        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or target.suffix in (
            ".html",
            ".css",
            ".js",
            ".svg",
        ):
            if "charset" not in content_type:
                content_type += "; charset=utf-8"
        self._send_bytes(200, target.read_bytes(), content_type)


class WebServer:
    """把 HTTP 服务封装成可启动 / 停止的对象。"""

    def __init__(
        self,
        config: Config,
        adb: ADBClient,
        monitor: StateMonitor,
        watchdog: Optional[Watchdog] = None,
        controller: Optional[DeviceController] = None,
        app_catalog: Optional[UserAppCatalog] = None,
    ) -> None:
        self.config = config
        self.adb = adb
        self.monitor = monitor
        self.watchdog = watchdog
        self.app_catalog = app_catalog
        self.controller = controller or DeviceController(
            adb, None, allow_dangerous=config.allow_dangerous_actions
        )
        self._httpd: Optional[LetvHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return int(self.config.web.port)

    def start(self) -> None:
        if self._httpd is not None:
            return
        httpd = LetvHTTPServer(
            (self.config.web.host, self.config.web.port), Handler
        )
        httpd.ctx = ServerContext(
            config=self.config,
            adb=self.adb,
            monitor=self.monitor,
            app_catalog=self.app_catalog,
            watchdog=self.watchdog,
            controller=self.controller,
        )
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="letv-web", daemon=True
        )
        self._thread.start()
        log.info(
            "Web 控制台正在监听 %s:%d",
            self.config.web.host,
            self.port,
        )

    def stop(self) -> None:
        httpd = self._httpd
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
        self._httpd = None
        self._thread = None
