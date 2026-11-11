"""The Tewke integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytewke
from homeassistant.const import CONF_HOST, Platform
from homeassistant.exceptions import ConfigEntryNotReady
from pytewke.error import BasePyTewkeError

from .const import DOMAIN, LOGGER
from .coordinator import TewkeCoordinator
from .data import TewkeData
from .heating import (
    async_setup_climate_state_listener,
    cancel_climate_state_listener,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import TewkeConfigEntry

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.FAN,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
) -> bool:
    """Set up a Tewke device from a config entry."""
    tap = pytewke.Tap(entry.data[CONF_HOST])

    try:
        await tap.discover()
    except (BasePyTewkeError, TimeoutError) as err:
        await tap.close()
        msg = f"Unable to connect to Tewke device at {entry.data[CONF_HOST]}"
        raise ConfigEntryNotReady(msg) from err

    tewke_coordinator = TewkeCoordinator(
        hass=hass,
        logger=LOGGER,
        name=DOMAIN,
    )
    # Pass the config entry to the coordinator
    tewke_coordinator.config_entry = entry

    entry.runtime_data = TewkeData(
        host=entry.data[CONF_HOST],
        tap=tap,
        coordinator=tewke_coordinator,
        scene_control_types=entry.data.get("scene_control_types", {}),
    )

    try:
        await tewke_coordinator.async_config_entry_first_refresh()
    except Exception:
        tewke_coordinator.cancel_observation_timeout()
        await tap.close()
        raise

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_setup_climate_state_listener(hass, entry)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    entry.async_on_unload(lambda: cancel_climate_state_listener(entry))
    entry.async_on_unload(tewke_coordinator.cancel_observation_timeout)
    entry.async_on_unload(tap.close)

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
) -> None:
    """Handle config entry options/data update."""
    if hasattr(entry, "runtime_data") and entry.runtime_data is not None:
        current_host = (
            entry.data.get(CONF_HOST)
            if isinstance(getattr(entry, "data", None), dict)
            else None
        )
        runtime_host = getattr(entry.runtime_data, "host", None)
        if (
            current_host is not None
            and runtime_host is not None
            and current_host != runtime_host
        ):
            hass.config_entries.async_schedule_reload(entry.entry_id)
            return
        async_setup_climate_state_listener(hass, entry)
        entry.runtime_data.coordinator.async_update_listeners()
