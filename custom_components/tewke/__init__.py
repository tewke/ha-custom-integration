"""The Tewke integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytewke
from homeassistant.const import CONF_HOST, CONF_NAME, Platform
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytewke.error import PyTewkeDiscoveryError

from .const import (
    CONF_DEFAULT_SCENE_FAN_DIMMING,
    CONF_DISABLED_SCENES,
    DOMAIN,
    LOGGER,
)
from .coordinator import TewkeCoordinator
from .data import TewkeData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytewke.data import (
        ConfigData,
        EnergyData,
        RadarData,
        Scene,
        SensorData,
        Target,
    )

    from .data import TewkeConfigEntry

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.FAN,
    Platform.LIGHT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(  # noqa: PLR0915
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
) -> bool:
    """Set up a Tewke device from a config entry."""
    tap = pytewke.Tap(entry.data[CONF_HOST])

    try:
        await tap.discover()
    except PyTewkeDiscoveryError as err:
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

    await tewke_coordinator.async_config_entry_first_refresh()

    def _on_scene_update(scenes: dict[str, Scene]) -> None:
        """
        Handle scene updates from the Tewke device.

        This callback is triggered when the scenes on the device change. It
        identifies new scenes and creates a repair issue to configure them.
        """
        if tewke_coordinator.data is None:
            return

        scene_control_types = entry.runtime_data.scene_control_types

        # Handle scenes that are no longer provided by the device
        removed_configured_ids = [
            sid for sid in scene_control_types if sid not in scenes
        ]
        if removed_configured_ids:
            LOGGER.info(
                "Marking deleted scenes as unavailable: %s", removed_configured_ids
            )
            new_scene_control_types = dict(scene_control_types)

            for sid in removed_configured_ids:
                del new_scene_control_types[sid]

            new_data = dict(entry.data)
            new_data["scene_control_types"] = new_scene_control_types

            if CONF_DISABLED_SCENES in new_data:
                new_data[CONF_DISABLED_SCENES] = [
                    sid
                    for sid in new_data[CONF_DISABLED_SCENES]
                    if sid not in removed_configured_ids
                ]
            if CONF_DEFAULT_SCENE_FAN_DIMMING in new_data:
                new_fan_dimming = dict(new_data[CONF_DEFAULT_SCENE_FAN_DIMMING])
                for sid in removed_configured_ids:
                    new_fan_dimming.pop(sid, None)
                new_data[CONF_DEFAULT_SCENE_FAN_DIMMING] = new_fan_dimming

            hass.config_entries.async_update_entry(entry, data=new_data)
            return

        configured_scenes = {
            scene_id: scene
            for scene_id, scene in scenes.items()
            if scene_id in scene_control_types
        }

        tewke_coordinator.async_set_updated_data(
            {
                **tewke_coordinator.data,
                "scenes": configured_scenes,
                "scenes_all": scenes,
            }
        )

        # Remove pending scenes that no longer exist on the device
        stale_ids = [
            sid for sid in entry.runtime_data.pending_scenes if sid not in scenes
        ]
        for sid in stale_ids:
            del entry.runtime_data.pending_scenes[sid]

        new_scenes = {
            scene_id: scene
            for scene_id, scene in scenes.items()
            if scene_id not in scene_control_types
            and scene_id not in entry.runtime_data.pending_scenes
        }

        if not new_scenes and not entry.runtime_data.pending_scenes:
            ir.async_delete_issue(hass, DOMAIN, f"new_scenes_found_{entry.entry_id}")

        if new_scenes:
            LOGGER.info("Discovered new scenes, pending configuration: %s", new_scenes)
            entry.runtime_data.pending_scenes.update(new_scenes)
            ir.async_create_issue(
                hass,
                DOMAIN,
                f"new_scenes_found_{entry.entry_id}",
                data={"entry_id": entry.entry_id},
                is_fixable=True,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="new_scenes_found",
                translation_placeholders={"name": entry.title},
            )

    # Process initial scenes found during discovery (using scenes_all)
    if tewke_coordinator.data and "scenes_all" in tewke_coordinator.data:
        _on_scene_update(tewke_coordinator.data["scenes_all"])

    def _on_target_update(targets: dict[int, Target]) -> None:
        """
        Handle target updates from the Tewke device.

        This callback is triggered when the targets on the device change.
        It updates the coordinator with the new target data.
        """
        if tewke_coordinator.data is None:
            return

        tewke_coordinator.async_set_updated_data(
            {
                **tewke_coordinator.data,
                "targets": targets,
            }
        )

    def _on_sensor_update(sensor_data: SensorData) -> None:
        """
        Handle sensor updates from the Tewke device.

        This callback is triggered when the sensors on the device change.
        """
        if tewke_coordinator.data is None:
            return
        tewke_coordinator.async_set_updated_data(
            {**tewke_coordinator.data, "sensors": sensor_data}
        )

    def _on_radar_update(radar_data: RadarData) -> None:
        """
        Handle radar updates from the Tewke device.

        This callback is triggered when the radar on the device changes.
        """
        if tewke_coordinator.data is None:
            return
        tewke_coordinator.async_set_updated_data(
            {**tewke_coordinator.data, "radar": radar_data}
        )

    def _on_energy_update(energy_data: EnergyData) -> None:
        """
        Handle energy updates from the Tewke device.

        This callback is triggered when the energy on the device changes.
        """
        if tewke_coordinator.data is None:
            return
        tewke_coordinator.async_set_updated_data(
            {**tewke_coordinator.data, "energy": energy_data}
        )

    def _on_config_update(config_data: ConfigData) -> None:
        """
        Handle config updates from the Tewke device.

        This callback is triggered when the config on the device changes.
        """
        if tewke_coordinator.data is None:
            return
        tewke_coordinator.async_set_updated_data(
            {**tewke_coordinator.data, "config": config_data}
        )

        new_name = config_data.device_name
        if new_name and new_name != entry.data.get(CONF_NAME):
            LOGGER.debug("Device renamed to %r, updating HA", new_name)
            hass.config_entries.async_update_entry(
                entry,
                title=new_name,
                data={**entry.data, CONF_NAME: new_name},
            )
            device_registry = dr.async_get(hass)
            device_id = tap.wall_dock_id
            device = device_registry.async_get_device(identifiers={(DOMAIN, device_id)})
            if device:
                device_registry.async_update_device(device.id, name=new_name)

    await tap.observe(
        scene_callback=_on_scene_update,
        target_callback=_on_target_update,
        sensor_callback=_on_sensor_update,
        radar_callback=_on_radar_update,
        energy_callback=_on_energy_update,
        config_change_callback=_on_config_update,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
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
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
