"""包名 / Activity 名称校验。

这些字符串会被拼进设备端 shell 命令，必须先做严格校验，避免命令注入。
"""

from __future__ import annotations

import re

# Android 包名至少两段，例如 tv.emby.embyatv。
PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
# Activity 可以是全限定名，也可以是 .MainActivity 这种相对写法。
ACTIVITY_RE = re.compile(r"^[A-Za-z0-9_.$]+$")


def ensure_package(value: object) -> str:
    """校验并返回包名，非法时抛 ValueError。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("包名不能为空")
    value = value.strip()
    if not PACKAGE_RE.fullmatch(value):
        raise ValueError(f"非法包名: {value}")
    return value


def ensure_activity(value: object) -> str:
    """校验并返回 Activity 名，非法时抛 ValueError。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Activity 名称不能为空")
    value = value.strip()
    if not ACTIVITY_RE.fullmatch(value):
        raise ValueError(f"非法 Activity 名称: {value}")
    return value
