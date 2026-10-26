"""配置流与选项流。"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    LetvAuthError,
    LetvClient,
    LetvConnectionError,
    LetvError,
)
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_HOST,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_HOST,
                default=defaults.get(CONF_HOST, DEFAULT_HOST),
            ): str,
            vol.Required(
                CONF_PORT,
                default=defaults.get(CONF_PORT, DEFAULT_PORT),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Optional(
                CONF_TOKEN, default=defaults.get(CONF_TOKEN, "")
            ): str,
            vol.Required(
                CONF_NAME,
                default=defaults.get(CONF_NAME, DEFAULT_NAME),
            ): str,
        }
    )


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    host = str(data.get(CONF_HOST) or DEFAULT_HOST).strip()
    port = int(data.get(CONF_PORT) or DEFAULT_PORT)
    token = str(data.get(CONF_TOKEN) or "").strip()
    name = str(data.get(CONF_NAME) or "").strip() or DEFAULT_NAME
    return {
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_TOKEN: token,
        CONF_NAME: name,
    }


class LetvTvConfigFlow(ConfigFlow, domain=DOMAIN):
    """通过 UI 或 YAML 添加一个 letv_tv 实例。"""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            data = _normalize(user_input)
            host = data[CONF_HOST]
            port = data[CONF_PORT]

            if not host:
                errors["base"] = "invalid_host"
            else:
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()

                client = LetvClient(
                    async_get_clientsession(self.hass),
                    host,
                    port,
                    data[CONF_TOKEN],
                )
                try:
                    await client.async_get_health()
                except LetvAuthError:
                    errors["base"] = "invalid_auth"
                except LetvConnectionError:
                    errors["base"] = "cannot_connect"
                except LetvError:
                    _LOGGER.exception("验证 letv_daemon 连接时出错")
                    errors["base"] = "unknown"
                else:
                    return self.async_create_entry(
                        title=data[CONF_NAME], data=data
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input),
            errors=errors,
        )

    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """YAML 导入：不访问守护进程，避免 HA 因服务重启而启动失败。"""
        data = _normalize(import_data)
        if not data[CONF_HOST]:
            return self.async_abort(reason="invalid_host")

        raw_interval = import_data.get(CONF_SCAN_INTERVAL)
        if raw_interval is not None:
            try:
                interval = int(raw_interval)
            except (TypeError, ValueError):
                interval = DEFAULT_SCAN_INTERVAL
            data[CONF_SCAN_INTERVAL] = max(
                MIN_SCAN_INTERVAL, min(MAX_SCAN_INTERVAL, interval)
            )

        await self.async_set_unique_id(
            f"{data[CONF_HOST]}:{data[CONF_PORT]}"
        )
        # YAML 修改名称/token/间隔时，幂等更新已有条目。
        self._abort_if_unique_id_configured(updates=data)
        return self.async_create_entry(title=data[CONF_NAME], data=data)

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            token = str(user_input.get(CONF_TOKEN) or "").strip()
            client = LetvClient(
                async_get_clientsession(self.hass),
                entry.data[CONF_HOST],
                entry.data[CONF_PORT],
                token,
            )
            try:
                await client.async_get_health()
            except LetvAuthError:
                errors["base"] = "invalid_auth"
            except LetvConnectionError:
                errors["base"] = "cannot_connect"
            except LetvError:
                _LOGGER.exception("重新验证 letv_daemon 访问令牌时出错")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: token}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_TOKEN,
                        default=entry.data.get(CONF_TOKEN, ""),
                    ): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "host": entry.data[CONF_HOST],
                "port": str(entry.data[CONF_PORT]),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        return LetvTvOptionsFlow()


class LetvTvOptionsFlow(OptionsFlow):
    """轮询间隔设置。"""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=current
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                        ),
                    )
                }
            ),
        )
