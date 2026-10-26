"""按键路由：ADB 有 keyCode 的走 ADB，其余动作用 UDP 9900。

UDP 只用于 ADB 无法实现的关机/切源动作；两者共用同一套危险动作策略。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

from .keys import DangerousActionError, UdpAction, find_udp_action
from .udp import UDPClient

log = logging.getLogger(__name__)


class UDPUnavailableError(RuntimeError):
    """动作需要 UDP，但配置中未启用。"""


@dataclass(frozen=True)
class ActionOutcome:
    """一次动作的执行结果。"""

    action: str
    label: str
    transport: str  # adb | udp
    keycode: Optional[int] = None

    def as_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "action": self.action,
            "label": self.label,
            "transport": self.transport,
        }
        if self.keycode is not None:
            payload["keycode"] = self.keycode
        return payload


class DeviceController:
    """按动作类型选择 ADB 或 UDP 并执行。"""

    def __init__(
        self,
        adb: object,
        udp: Optional[UDPClient] = None,
        allow_dangerous: bool = False,
    ) -> None:
        self._adb = adb
        self._udp = udp
        self._allow_dangerous = allow_dangerous

    @property
    def udp_available(self) -> bool:
        return self._udp is not None

    def send(self, action: str) -> ActionOutcome:
        """发送一个动作。

        UDP 动作在 ADB 之前判定，避免落入“无 keyCode”的拒绝分支；
        其余动作完全沿用 :meth:`ADBClient.send_key` 的解析与安全策略。
        """
        udp_action = find_udp_action(action)
        if udp_action is not None:
            return self._send_udp(udp_action)

        key = self._adb.send_key(action)
        return ActionOutcome(
            action=key.action,
            label=key.label,
            transport="adb",
            keycode=key.keycode,
        )

    def close(self) -> None:
        if self._udp is not None:
            self._udp.close()

    # ---- 内部 -----------------------------------------------------

    def _send_udp(self, action: UdpAction) -> ActionOutcome:
        if action.dangerous and not self._allow_dangerous:
            raise DangerousActionError(
                f"动作 {action.action} 属于危险操作（{action.label}），"
                "已被拒绝；如确需使用请将 allow_dangerous_actions 设为 true"
            )
        if self._udp is None:
            raise UDPUnavailableError(
                "UDP 遥控未启用（config.json → udp.enabled）"
            )

        wire = action.wire or action.action
        self._udp.send_action(wire)
        log.info("已通过 UDP 发送动作 %s（%s）", wire, action.label)
        return ActionOutcome(
            action=action.action,
            label=action.label,
            transport="udp",
        )
