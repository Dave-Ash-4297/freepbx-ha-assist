"""FreePBX Assist: talk to Home Assistant Assist from any PBX extension."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import CONF_SIP_PORT, DEFAULT_SIP_PORT, DOMAIN, EXTENSION_AREAS
from .voip import FreePBXVoipProtocol

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start the SIP server."""
    port = entry.data.get(CONF_SIP_PORT, DEFAULT_SIP_PORT)

    # Pre-create a device per known room phone, already assigned to its area.
    dev_reg = dr.async_get(hass)
    for extension, area in EXTENSION_AREAS.items():
        dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, extension)},
            name=f"{area} phone ({extension})",
            manufacturer="FreePBX",
            model="SIP extension",
            suggested_area=area,
        )

    try:
        transport, _protocol = await hass.loop.create_datagram_endpoint(
            lambda: FreePBXVoipProtocol(hass, entry),
            local_addr=("0.0.0.0", port),
        )
    except OSError as err:
        raise ConfigEntryNotReady(
            f"Could not bind SIP server to UDP port {port}: {err}. "
            "If the core VoIP integration is also enabled it may already own "
            "port 5060 - pick a different port in this integration's settings."
        ) from err

    _LOGGER.info("FreePBX Assist SIP server listening on UDP port %s", port)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = transport
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop the SIP server."""
    transport = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if transport is not None:
        transport.close()
    return True
