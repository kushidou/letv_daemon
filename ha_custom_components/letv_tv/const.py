"""letv_tv 自定义集成的常量。"""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "letv_tv"

CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 5
MIN_SCAN_INTERVAL = 3
MAX_SCAN_INTERVAL = 300

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8770
DEFAULT_NAME = "乐视电视"

MANUFACTURER = "LeTV"
MODEL = "超级电视 X4-50"

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER]

# HomeKit 遥控器按键事件（由 HA homekit 集成触发）。
HOMEKIT_TV_REMOTE_KEY_PRESSED = "homekit_tv_remote_key_pressed"
ATTR_KEY_NAME = "key_name"
ATTR_ENTITY_ID = "entity_id"

# 启动 App 后等待前台切换的时间；过早刷新会被旧的前台读数覆盖。
LAUNCH_SETTLE_SECONDS = 1.5

VOLUME_MAX = 100
