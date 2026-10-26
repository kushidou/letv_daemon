"""遥控动作表与安全策略。

ADB 按键（有 keyCode）与仅 UDP 动作（无 keyCode）分开维护；
动作名与 ``letv_docs/遥控器按键清单-乐视X4-50.md``、
``letv_docs/网络遥控-乐视X4-50.md`` 保持一致，keyCode 均来自真机实测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


class UnknownActionError(ValueError):
    """动作名不在按键表中。"""


class DangerousActionError(PermissionError):
    """动作会改变设备状态，当前配置不允许执行。"""


@dataclass(frozen=True)
class Key:
    """一个可注入的遥控按键。"""

    action: str
    keycode: int
    label: str
    dangerous: bool = False

    def as_dict(self) -> Dict[str, object]:
        return {
            "action": self.action,
            "keycode": self.keycode,
            "label": self.label,
            "dangerous": self.dangerous,
        }


def _key(action: str, keycode: int, label: str, dangerous: bool = False) -> Key:
    return Key(action=action, keycode=keycode, label=label, dangerous=dangerous)


@dataclass(frozen=True)
class UdpAction:
    """ADB 没有可靠 keyCode、只能通过 UDP 9900 发送的动作。

    ``wire`` 是实际写进 ``CONTROL_ACTION`` 的动作名；为空时与 ``action``
    相同。用于对外动作名与电视固件实际支持的名称不一致的情况。
    """

    action: str
    label: str
    dangerous: bool = True
    wire: Optional[str] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "action": self.action,
            "label": self.label,
            "dangerous": self.dangerous,
            "transport": "udp",
        }


_KEYS: List[Key] = [
    # 导航 / 确认
    _key("home", 3, "主页"),
    _key("return", 4, "返回"),
    _key("menu", 82, "菜单"),
    _key("up", 19, "上"),
    _key("down", 20, "下"),
    _key("left", 21, "左"),
    _key("right", 22, "右"),
    _key("ok", 23, "确定"),
    # 音量
    _key("volume_up", 24, "音量+"),
    _key("volume_down", 25, "音量-"),
    _key("mute", 164, "静音"),
    # 频道
    _key("channel_up", 166, "频道+"),
    _key("channel_down", 167, "频道-"),
    _key("last_channel", 229, "上一频道"),
    # 数字键
    *[_key(f"num_{n}", 7 + n, f"数字 {n}") for n in range(10)],
    # 设置 / 媒体
    _key("setting", 176, "设置"),
    _key("media_play_pause", 85, "播放/暂停"),
    _key("media_stop", 86, "停止"),
    _key("media_next", 87, "下一个"),
    _key("media_previous", 88, "上一个"),
    _key("media_rewind", 89, "快退"),
    _key("media_fast_forward", 90, "快进"),
    _key("media_play", 126, "播放"),
    _key("media_pause", 127, "暂停"),
    # 电视功能键（MStar 扩展区）
    _key("tv_sound_mode", 276, "声音模式"),
    _key("tv_picture_mode", 277, "图像模式"),
    _key("tv_epg", 278, "电子节目单"),
    _key("tv_list", 279, "频道列表"),
    _key("tv_subtitle", 280, "字幕"),
    _key("tv_mts", 282, "声道"),
    _key("tv_freeze", 283, "画面冻结"),
    _key("tv_cc", 284, "隐藏字幕"),
    _key("tv_setting", 294, "电视设置"),
    _key("tv_zoom_mode", 255, "缩放模式"),
    _key("tv_teletext", 233, "图文电视"),
    _key("guide", 172, "节目指南"),
    _key("dvr", 173, "DVR"),
    _key("info", 165, "信息"),
    _key("window", 171, "窗口"),
    _key("tv", 170, "TV"),
    _key("tv_input", 178, "输入源"),
    # 乐视扩展键
    _key("letv", 4415, "乐视键"),
    _key("soft_info", 4416, "软信息"),
    _key("list", 4436, "列表"),
    # 彩色键
    _key("red", 183, "红"),
    _key("green", 184, "绿"),
    _key("yellow", 185, "黄"),
    _key("blue", 186, "蓝"),
    # F1-F12
    *[_key(f"f{i}", 130 + i, f"F{i}") for i in range(1, 13)],
    # 危险键：默认拒绝，仅 allow_dangerous_actions=true 时可用
    _key("power", 26, "电源", dangerous=True),
    _key("tv_power", 177, "电视电源", dangerous=True),
    _key("sleep", 223, "睡眠", dangerous=True),
]

_KEYS_BY_ACTION: Dict[str, Key] = {key.action: key for key in _KEYS}

# 只走 UDP 的动作：ADB 没有可靠 keyCode（见 letv_docs/网络遥控-乐视X4-50.md §4.3）。
# 这里只收录“网页需要按钮”的关机与切源/模式动作，工厂模式等仍保持拒绝。
_UDP_ACTIONS: List[UdpAction] = [
    # 固件只认 "power"（待机/唤醒切换）；发送 "power_off" 无任何反应。
    UdpAction("power_off", "关机", wire="power"),
    UdpAction("switch_source", "信号源"),
    UdpAction("switch_hdmi_0", "HDMI 1"),
    UdpAction("switch_hdmi_1", "HDMI 2"),
    UdpAction("switch_hdmi_2", "HDMI 3"),
    UdpAction("switch_hdmi_3", "HDMI 4"),
    UdpAction("switch_tv", "TV"),
    UdpAction("switch_vga", "VGA"),
    UdpAction("switch_cvbs", "AV"),
    UdpAction("switch_station", "切换台"),
    UdpAction("switch_network_model", "网络模式"),
    UdpAction("switch_bit_stream", "码流"),
    UdpAction("switch_play_mode", "播放模式"),
]

_UDP_BY_ACTION: Dict[str, UdpAction] = {
    action.action: action for action in _UDP_ACTIONS
}

_ALIASES: Dict[str, str] = {
    "back": "return",
    "center": "ok",
    "enter": "ok",
    "select": "ok",
    "dpad_center": "ok",
    "settings": "setting",
    "play_pause": "media_play_pause",
    "prev": "media_previous",
    "previous": "media_previous",
    "next": "media_next",
    "rew": "media_rewind",
    "rewind": "media_rewind",
    "ff": "media_fast_forward",
    "fast_forward": "media_fast_forward",
}

# 协议中存在、但没有可靠 keyCode 的危险动作，只能拒绝。
_DANGEROUS_ONLY = frozenset(
    {
        "power_off",
        "source",
        "swap",
        "backview",
        "vga",
        "hdmi",
        "cvbs",
        "av",
        "ctv_reset",
        "screenshot",
    }
)
_DANGEROUS_PREFIXES: Tuple[str, ...] = ("switch_", "factory_")


def normalize_action(action: object) -> str:
    """把用户输入规范化为小写下划线形式。"""
    if not isinstance(action, str):
        raise UnknownActionError("动作名必须是字符串")
    name = action.strip().lower().replace("-", "_").replace(" ", "_")
    if not name:
        raise UnknownActionError("动作名不能为空")
    return name


def is_dangerous_action(action: object) -> bool:
    """判断动作是否属于危险操作（不关心当前是否放行）。"""
    name = normalize_action(action)
    canonical = _ALIASES.get(name, name)
    if canonical in _DANGEROUS_ONLY or name in _DANGEROUS_ONLY:
        return True
    if canonical.startswith(_DANGEROUS_PREFIXES) or name.startswith(_DANGEROUS_PREFIXES):
        return True
    key = _KEYS_BY_ACTION.get(canonical)
    return bool(key and key.dangerous)


def resolve(action: object, allow_dangerous: bool = False) -> Key:
    """把动作名解析为 Key；未知动作或未放行的危险动作抛异常。"""
    name = normalize_action(action)
    canonical = _ALIASES.get(name, name)
    key = _KEYS_BY_ACTION.get(canonical)

    if key is not None:
        if key.dangerous and not allow_dangerous:
            raise DangerousActionError(
                f"动作 {name} 属于危险操作（{key.label}），已被拒绝；"
                "如确需使用请将 allow_dangerous_actions 设为 true"
            )
        return key

    if is_dangerous_action(name):
        if not allow_dangerous:
            raise DangerousActionError(f"动作 {name} 属于危险操作，已被拒绝")
        raise UnknownActionError(f"动作 {name} 没有已知 keyCode，无法通过 ADB 注入")

    raise UnknownActionError(f"未知动作: {name}")


def list_keys(allow_dangerous: bool = False) -> List[Key]:
    """返回可展示 / 可用的 ADB 按键列表（保持定义顺序）。"""
    return [key for key in _KEYS if allow_dangerous or not key.dangerous]


def find_udp_action(action: object) -> Optional[UdpAction]:
    """查找只能走 UDP 的动作；不是 UDP 动作时返回 None。"""
    name = normalize_action(action)
    return _UDP_BY_ACTION.get(_ALIASES.get(name, name))


def list_udp_actions(allow_dangerous: bool = False) -> List[UdpAction]:
    """返回可展示 / 可用的 UDP 动作列表（当前全部属于危险动作）。"""
    return [
        action
        for action in _UDP_ACTIONS
        if allow_dangerous or not action.dangerous
    ]
