"""UDP 9900 网络遥控通道。

电视侧的 ``com.stv.smartControl`` 监听 UDP 9900，收到
``{"CONTROL_ACTION":"<动作名>"}`` 后注入对应遥控按键：单向、无认证、无应答。
这里只实现 ADB 无法完成的动作（关机、切源/模式），常规按键仍走 ADB。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

DEFAULT_UDP_PORT = 9900


class UdpError(RuntimeError):
    """UDP 报文发送失败。"""


class UDPClient:
    """极简 UDP 发送器；socket 延迟创建，失败后下次重试。"""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_UDP_PORT,
        timeout: float = 2.0,
        socket_factory: Optional[Callable[..., socket.socket]] = None,
    ) -> None:
        host = host.strip() if isinstance(host, str) else ""
        if not host:
            raise ValueError("UDP 主机不能为空")
        self._host = host
        self._port = int(port)
        self._timeout = max(0.1, float(timeout))
        self._socket_factory = socket_factory or socket.socket
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None

    @property
    def address(self) -> str:
        return f"{self._host}:{self._port}"

    def send_action(self, action: str) -> None:
        """发送一条 ``CONTROL_ACTION`` 报文（fire-and-forget）。"""
        if not isinstance(action, str) or not action.strip():
            raise ValueError("UDP 动作名必须是非空字符串")
        payload = json.dumps(
            {"CONTROL_ACTION": action.strip()}, separators=(",", ":")
        ).encode("utf-8")

        with self._lock:
            sock = self._ensure_socket()
            try:
                sock.sendto(payload, (self._host, self._port))
            except OSError as exc:
                self._close_locked()
                raise UdpError(
                    f"UDP 发送到 {self.address} 失败: {exc}"
                ) from exc

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    # ---- 内部 -----------------------------------------------------

    def _ensure_socket(self) -> socket.socket:
        if self._sock is None:
            sock = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(self._timeout)
            self._sock = sock
        return self._sock

    def _close_locked(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
