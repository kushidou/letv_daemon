"""HA 自定义集成的常量。"""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "letv_daemon"

CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 5
MIN_SCAN_INTERVAL = 3
MAX_SCAN_INTERVAL = 300

DEFAULT_NAME = "乐视电视"
DEFAULT_PORT = 8770

MANUFACTURER = "LeTV"
MODEL = "超级电视 X4-50"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.REMOTE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]
