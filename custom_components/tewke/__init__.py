"""The Tewke integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytewke
from homeassistant.const import CONF_HOST, Platform
from homeassistant.exceptions import ConfigEntryNotReady
from pytewke.error import PyTewkeDiscoveryError

from .const import DOMAIN, LOGGER
from .coordinator import TewkeCoordinator
from .data import TewkeData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import TewkeConfigEntry

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.FAN,
    Platform.LIGHT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
) -> bool:
    """Set up a Tewke device from a config entry."""
    host = entry.data[CONF_HOST]
    LOGGER.debug("[%s] async_setup_entry: starting setup for host %s", entry.entry_id, host)

    tap = pytewke.Tap(host)

    try:
        await tap.discover()
        LOGGER.debug("[%s] Tap discovery succeeded", entry.entry_id)
    except PyTewkeDiscoveryError as err:
        msg = f"Unable to connect to Tewke device at {host}"
        LOGGER.debug("[%s] Tap discovery failed: %s", entry.entry_id, err)
        raise ConfigEntryNotReady(msg) from err

    tewke_coordinator = TewkeCoordinator(
        hass=hass,
        logger=LOGGER,
        name=DOMAIN,
    )
    # Pass the config entry to the coordinator
    tewke_coordinator.config_entry = entry

    entry.runtime_data = TewkeData(
        host=host,
        tap=tap,
        coordinator=tewke_coordinator,
        scene_control_types=entry.data.get("scene_control_types", {}),
    )

    LOGGER.debug("[%s] Running first coordinator refresh", entry.entry_id)
    await tewke_coordinator.async_config_entry_first_refresh()
    LOGGER.debug("[%s] First coordinator refresh complete", entry.entry_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    entry.async_on_unload(tewke_coordinator.cancel_observation_timeout)
    entry.async_on_unload(tap.close)

    LOGGER.debug("[%s] async_setup_entry: setup complete", entry.entry_id)
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    LOGGER.debug("[%s] async_unload_entry: unloading platforms", entry.entry_id)
    result = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    LOGGER.debug("[%s] async_unload_entry: unload result=%s", entry.entry_id, result)
    return result


async def async_reload_entry(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
) -> None:
    """Reload config entry."""
    LOGGER.debug("[%s] async_reload_entry: reload triggered", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)
