"""
Binary sensor platform for the Tewke integration.

Exposes boolean BME680 calibration status fields, delivered via CoAP
observation (local_push). Both are disabled by default as diagnostic values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import callback

from .entity import TewkeEntity

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from pytewke.data import ConfigData, RadarData, SensorData

    from .coordinator import TewkeCoordinator
    from .data import TewkeConfigEntry


@dataclass(frozen=True, kw_only=True)
class TewkeBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Tewke binary sensor entity."""

    value_fn: Callable[[SensorData], bool | None]


BINARY_SENSOR_DESCRIPTIONS: tuple[TewkeBinarySensorEntityDescription, ...] = (
    TewkeBinarySensorEntityDescription(
        key="stabilisation_status",
        name="Stabilisation Status",
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.stabilisation_status,
    ),
    TewkeBinarySensorEntityDescription(
        key="run_in_status",
        name="Run-in Status",
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.run_in_status,
    ),
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: TewkeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tewke binary sensor entities from a config entry."""
    coordinator = entry.runtime_data.coordinator
    entities: list[TewkeBinarySensor | TewkeScreenBinarySensor] = [
        TewkeScreenBinarySensor(coordinator=coordinator)
    ]
    added_bme_binary = False

    if coordinator.data.get("sensors") is not None:
        added_bme_binary = True
        entities.extend(
            TewkeBinarySensor(coordinator=coordinator, description=description)
            for description in BINARY_SENSOR_DESCRIPTIONS
        )

    async_add_entities(entities)

    @callback
    def _async_check_late_binary_sensors() -> None:
        nonlocal added_bme_binary
        if not coordinator.data:
            return
        if not added_bme_binary and coordinator.data.get("sensors") is not None:
            added_bme_binary = True
            async_add_entities(
                TewkeBinarySensor(coordinator=coordinator, description=description)
                for description in BINARY_SENSOR_DESCRIPTIONS
            )

    if not added_bme_binary:
        entry.async_on_unload(
            coordinator.async_add_listener(_async_check_late_binary_sensors)
        )


class TewkeBinarySensor(TewkeEntity, BinarySensorEntity):
    """A Tewke BME680 calibration status binary sensor."""

    entity_description: TewkeBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: TewkeCoordinator,
        description: TewkeBinarySensorEntityDescription,
    ) -> None:
        """Initialise the binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{self._hardware_id}_sensor_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return the sensor state."""
        sensors: SensorData | None = self.coordinator.data.get("sensors")
        if sensors is None:
            return None
        return self.entity_description.value_fn(sensors)


class TewkeScreenBinarySensor(TewkeEntity, BinarySensorEntity):
    """Binary sensor representing whether the Tewke panel screen is on."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_name = "Screen"

    def __init__(self, coordinator: TewkeCoordinator) -> None:
        """Initialise the screen sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._hardware_id}_screen_on"

    @property
    def is_on(self) -> bool | None:
        """Return True when the panel screen is on."""
        config: ConfigData | None = self.coordinator.data.get("config")
        radar: RadarData | None = self.coordinator.data.get("radar")
        screen_on = None

        if config is not None and config.screen_on is not None:
            screen_on = config.screen_on
        if radar is not None and radar.screen_on is not None:
            screen_on = radar.screen_on

        return screen_on
