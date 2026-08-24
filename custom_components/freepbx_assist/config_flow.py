"""Config flow for FreePBX Assist."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_ALLOWED_EXTENSIONS,
    CONF_PBX_HOST,
    CONF_PIPELINE_MAP,
    CONF_PIPELINE_TIMEOUT,
    CONF_SIP_PORT,
    DEFAULT_PIPELINE_TIMEOUT,
    DEFAULT_SIP_PORT,
    DOMAIN,
)


class FreePBXAssistConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(
                title="FreePBX Assist",
                data=user_input,
                options={CONF_ALLOWED_EXTENSIONS: "1000, 2000, 3000, 4000"},
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SIP_PORT, default=DEFAULT_SIP_PORT): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=65535)
                    ),
                    vol.Optional(CONF_PBX_HOST, default=""): str,
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Options: extension allow-list, per-extension pipelines, timeout."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ALLOWED_EXTENSIONS,
                        default=options.get(CONF_ALLOWED_EXTENSIONS, ""),
                    ): str,
                    vol.Optional(
                        CONF_PIPELINE_MAP,
                        default=options.get(CONF_PIPELINE_MAP, ""),
                    ): str,
                    vol.Optional(
                        CONF_PIPELINE_TIMEOUT,
                        default=options.get(
                            CONF_PIPELINE_TIMEOUT, DEFAULT_PIPELINE_TIMEOUT
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=5, max=120)),
                }
            ),
        )
