"""
Button platform for the Tewke integration.

Exposes a restart (reboot) button for the Tewke Tap Panel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from pytewke.error import BasePyTewkeError

from .const import LOGGER
from .entity import TewkeEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import TewkeCoordinator
    from .data import TewkeConfigEntry


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: TewkeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tewke button entities from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities([TewkeRestartButton(coordinator=coordinator)])


class TewkeRestartButton(TewkeEntity, ButtonEntity):
    """Button entity to restart the Tewke Tap Panel."""

    _attr_name = "Restart"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: TewkeCoordinator) -> None:
        """Initialise the restart button entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._hardware_id}_restart"

    async def async_press(self) -> None:
        """Send a restart command to the Tap Panel."""
        tap = self.coordinator.config_entry.runtime_data.tap
        try:
            await tap.restart()
        except TimeoutError:
            LOGGER.debug("Timed out waiting for restart ACK (device may be rebooting)")
        except BasePyTewkeError as err:
            LOGGER.error("Error restarting Tewke Tap: %s", err)
            msg = f"Failed to restart Tewke Tap: {err}"
            raise HomeAssistantError(msg) from err
