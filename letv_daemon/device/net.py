"""网络可达性探测。

电视离线时只能靠 TCP 连接 ADB 端口判断是否回到网络；这里抽出公共
实现，供状态采样与 ADB 重连共用，避免 ``state`` 与 ``adb`` 循环依赖。
"""

from __future__ import annotations

import socket
from typing import Optional, Tuple


def tcp_probe(host: str, port: int, timeout: float = 1.0) -> bool:
    """TCP 探测端口是否可达。

    电视关机后不会响应 ARP（``ip neigh`` 为 ``INCOMPLETE``），因此这里
    与 ping 得到的结果相同，但代价只有一次 SYN。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def parse_host_port(serial: str) -> Optional[Tuple[str, int]]:
    """解析 ``host:port`` 形式的 adb serial。

    USB 序列号没有端口信息，返回 ``None``，调用方应跳过 TCP 门控。
    """
    if not serial:
        return None
    host, sep, port_text = serial.rpartition(":")
    if not sep or not host:
        return None
    try:
        port = int(port_text)
    except ValueError:
        return None
    if not 0 < port < 65536:
        return None
    return host, port
