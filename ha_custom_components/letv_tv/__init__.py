"""把乐视电视以「App 即输入源」的形式暴露给 HomeKit。"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import (
    LetvApiError,
    LetvApp,
    LetvAuthError,
    LetvClient,
    LetvError,
    LetvInput,
)
from .homekit_compat import install_homekit_volume_fix
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_HOST,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    PLATFORMS,
)
from .coordinator import LetvTvCoordinator, LetvTvRuntimeData
from .sources import (
    HDMI_ACTION,
    HDMI_SOURCE,
    build_source_targets,
    source_list,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN): vol.Any(
            None,
            vol.Schema(
                {
                    vol.Optional(
                        CONF_HOST, default=DEFAULT_HOST
                    ): cv.string,
                    vol.Optional(
                        CONF_PORT, default=DEFAULT_PORT
                    ): cv.port,
                    vol.Optional(CONF_TOKEN, default=""): cv.string,
                    vol.Optional(
                        CONF_NAME, default=DEFAULT_NAME
                    ): cv.string,
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=DEFAULT_SCAN_INTERVAL,
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                        ),
                    ),
                }
            ),
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def _build_client(hass: HomeAssistant, entry: ConfigEntry) -> LetvClient:
    return LetvClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data.get(CONF_TOKEN, ""),
    )


def _fallback_inputs(apps: list[LetvApp]) -> list[LetvInput]:
    """旧版守护进程没有 /api/inputs 时，保持 HDMI 1 + apps 的行为。"""
    inputs = [
        LetvInput(name=HDMI_SOURCE, kind="key", key=HDMI_ACTION)
    ]
    for app in apps:
        inputs.append(
            LetvInput(
                name=app.name,
                kind="app",
                package=app.package,
                activity=app.activity,
                targets=(app.package,),
            )
        )
    return inputs


async def _async_load_inputs(client: LetvClient) -> list[LetvInput]:
    try:
        inputs = await client.async_get_inputs()
    except LetvApiError as err:
        if err.status != 404:
            raise
        # 兼容尚未升级的守护进程。
        apps = await client.async_get_apps()
        return _fallback_inputs(apps)

    if inputs:
        return inputs
    # 新守护进程返回空列表说明配置有问题；退回 apps 保证电视仍有输入源。
    _LOGGER.warning(
        "letv_daemon 返回的输入源为空，退回 /api/apps 的默认列表"
    )
    apps = await client.async_get_apps()
    return _fallback_inputs(apps)


def _scan_interval(entry: ConfigEntry) -> int:
    raw = entry.options.get(
        CONF_SCAN_INTERVAL,
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_SCAN_INTERVAL
    return max(MIN_SCAN_INTERVAL, min(MAX_SCAN_INTERVAL, value))


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """把 YAML 中的 ``letv_tv:`` 转成配置条目。"""
    if DOMAIN not in config:
        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data=dict(config.get(DOMAIN) or {}),
        )
    )
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """连接守护进程、构造输入源并加载媒体播放器。"""
    client = _build_client(hass, entry)

    try:
        health = await client.async_get_health()
        actions = await client.async_get_actions()
        inputs = await _async_load_inputs(client)
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
            "letv_daemon 可达，但其 ADB 尚未连上电视；"
            "实体将在电视恢复后自动可用（%s）",
            health.serial or entry.data[CONF_HOST],
        )

    targets = build_source_targets(inputs)
    coordinator = LetvTvCoordinator(
        hass, entry, client, _scan_interval(entry)
    )
    await coordinator.async_config_entry_first_refresh()

    # HomeKit 只接受 0–1 的 volume_set，而上游把 0–100 原样传入；
    # 在 HomeKit 启动前替换其 Television 配件实现。
    install_homekit_volume_fix()

    entry.runtime_data = LetvTvRuntimeData(
        entry_id=entry.entry_id,
        name=entry.data.get(CONF_NAME) or entry.title or DOMAIN,
        client=client,
        coordinator=coordinator,
        actions={action.action: action for action in actions},
        inputs=inputs,
        source_targets=targets,
        source_list=source_list(targets),
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
