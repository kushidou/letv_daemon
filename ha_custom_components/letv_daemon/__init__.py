"""乐视电视守护服务的 Home Assistant 自定义集成。"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LetvAuthError, LetvClient, LetvError
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    PLATFORMS,
)
from .coordinator import LetvCoordinator, LetvRuntimeData

_LOGGER = logging.getLogger(__name__)


def _build_client(hass: HomeAssistant, entry: ConfigEntry) -> LetvClient:
    return LetvClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data.get(CONF_TOKEN, ""),
    )


def _scan_interval(entry: ConfigEntry) -> int:
    raw = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_SCAN_INTERVAL
    return max(MIN_SCAN_INTERVAL, min(MAX_SCAN_INTERVAL, value))


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """建立连接、拉取动作表与快捷启动列表，再加载各平台。"""
    client = _build_client(hass, entry)

    try:
        health = await client.async_get_health()
        actions = await client.async_get_actions()
        apps = await client.async_get_apps()
    except LetvAuthError as err:
        raise ConfigEntryAuthFailed(
            f"letv_daemon 拒绝访问令牌: {err}"
        ) from err
    except LetvError as err:
        raise ConfigEntryNotReady(
            f"无法连接 letv_daemon: {err}"
        ) from err

    if not health.connected:
        _LOGGER.warning(
            "letv_daemon 已连接，但其 ADB 尚未连上电视；"
            "实体将在电视恢复后自动可用（%s）",
            health.serial or entry.data[CONF_HOST],
        )

    coordinator = LetvCoordinator(
        hass, entry, client, _scan_interval(entry)
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = LetvRuntimeData(
        entry_id=entry.entry_id,
        name=entry.data.get(CONF_NAME) or entry.title or DOMAIN,
        client=client,
        coordinator=coordinator,
        actions={action.action: action for action in actions},
        apps=apps,
    )

    await hass.config_entries.async_forward_entry_setups(
        entry, PLATFORMS
    )
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    runtime = getattr(entry, "runtime_data", None)
    if unloaded and runtime is not None:
        await runtime.coordinator.async_shutdown()
    return unloaded


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
