"""Utilities for the Tewke integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_NAME
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytewke.error import PyTewkeObserveError

from .const import (
    CONF_DEFAULT_SCENE_FAN_DIMMING,
    CONF_DISABLED_SCENES,
    DOMAIN,
    LOGGER,
)
from .heating import (
    async_handle_heating_command,
    async_push_heating_state,
    tap_supports_heating,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytewke.data import (
        ConfigData,
        EnergyData,
        HeatingData,
        RadarData,
        Scene,
        SensorData,
        Target,
    )

    from .coordinator import TewkeCoordinator
    from .data import TewkeConfigEntry


def _get_default_scene_fan_dimming(entry: TewkeConfigEntry) -> dict[str, int]:
    """Return per-scene fan dimming defaults, preferring options over initial data."""
    return entry.options.get(CONF_DEFAULT_SCENE_FAN_DIMMING) or entry.data.get(
        CONF_DEFAULT_SCENE_FAN_DIMMING, {}
    )


def _tewke_to_ha_brightness(value: int) -> int:
    """
    Convert a Tewke brightness (0-100) to HA brightness (0-255).

    The input value is clamped to the range [0, 100].
    """
    value = max(0, min(100, value))
    return round(value / 100 * 255)


def _ha_to_tewke_brightness(value: int) -> int:
    """
    Convert a HA brightness (0-255) to a Tewke brightness (0-100).

    The input value is clamped to the range [0, 255].
    """
    value = max(0, min(255, value))
    return round(value / 255 * 100)


def _remove_deleted_scenes(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
    removed_configured_ids: list[str],
) -> dict[str, str]:
    """Remove deleted scenes from entity registry and config entry."""
    LOGGER.info(
        "Removing deleted scenes from configuration: %s",
        removed_configured_ids,
    )
    tap = entry.runtime_data.tap
    new_scene_control_types = dict(entry.runtime_data.scene_control_types)
    ent_reg = er.async_get(hass)
    hardware_id = tap.wall_dock_id or entry.unique_id

    for sid in removed_configured_ids:
        platform_domain = new_scene_control_types.pop(sid, None)
        if platform_domain and hardware_id:
            ent_id = ent_reg.async_get_entity_id(
                platform_domain, DOMAIN, f"{hardware_id}_{sid}"
            )
            if ent_id is not None:
                ent_reg.async_remove(ent_id)

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

    entry.runtime_data.scene_control_types = new_scene_control_types
    hass.config_entries.async_update_entry(entry, data=new_data)
    return new_scene_control_types


def _push_if_changed(
    coordinator: TewkeCoordinator,
    key: str,
    value: object,
) -> bool:
    """Reset the observation timeout and push data if it changed."""
    coordinator.reset_observation_timeout()
    if coordinator.data is None:
        return False
    if coordinator.data.get(key) != value:
        coordinator.async_set_updated_data({**coordinator.data, key: value})
    return True


def _sync_device_metadata(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
    config_data: ConfigData,
) -> None:
    """Sync device name and software version to the HA registries."""
    tap = entry.runtime_data.tap
    device_registry = dr.async_get(hass)
    device_id = tap.wall_dock_id
    device = device_registry.async_get_device(identifiers={(DOMAIN, device_id)})

    if device:
        new_name = config_data.device_name
        if new_name and new_name != entry.data.get(CONF_NAME):
            LOGGER.debug("Device renamed to %r, updating HA", new_name)
            hass.config_entries.async_update_entry(
                entry,
                title=new_name,
                data={**entry.data, CONF_NAME: new_name},
            )
            device_registry.async_update_device(device.id, name=new_name)

        new_version = config_data.tewke_os_version
        if new_version and new_version != device.sw_version:
            LOGGER.debug("Device updated to %r, updating HA", new_version)
            device_registry.async_update_device(device.id, sw_version=new_version)


async def async_setup_observe(  # noqa: PLR0915
    coordinator: TewkeCoordinator,
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
) -> bool:
    """
    Register CoAP observation callbacks on the Tap and start observing.

    Returns True if observe was set up successfully, False otherwise.
    The ``observe_active`` flag on ``entry.runtime_data`` is set accordingly.
    """
    tap = entry.runtime_data.tap

    tap.clear_callbacks()
    await tap.close_observations()

    def _on_scene_update(scenes: dict[str, Scene]) -> None:
        """
        Handle scene updates from the Tewke device.

        This callback is triggered when the scenes on the device change. It
        identifies new scenes and creates a repair issue to configure them.
        """
        coordinator.reset_observation_timeout()
        if coordinator.data is None:
            return

        scene_control_types = entry.runtime_data.scene_control_types

        # Handle scenes that are no longer provided by the device
        removed_configured_ids = [
            sid for sid in scene_control_types if sid not in scenes
        ]
        if removed_configured_ids:
            scene_control_types = _remove_deleted_scenes(
                hass, entry, removed_configured_ids
            )

        configured_scenes = {
            scene_id: scene
            for scene_id, scene in scenes.items()
            if scene_id in scene_control_types
        }

        coordinator.async_set_updated_data(
            {
                **coordinator.data,
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

    def _on_target_update(targets: dict[int, Target]) -> None:
        """
        Handle target updates from the Tewke device.

        This callback is triggered when the targets on the device change.
        It updates the coordinator with the new target data.
        """
        coordinator.reset_observation_timeout()
        if coordinator.data is None:
            return

        coordinator.async_set_updated_data(
            {
                **coordinator.data,
                "targets": targets,
            }
        )

    def _on_sensor_update(sensor_data: SensorData) -> None:
        """Handle sensor updates from the Tewke device."""
        _push_if_changed(coordinator, "sensors", sensor_data)

    def _on_radar_update(radar_data: RadarData) -> None:
        """Handle radar updates from the Tewke device."""
        _push_if_changed(coordinator, "radar", radar_data)

    def _on_energy_update(energy_data: EnergyData) -> None:
        """Handle energy updates from the Tewke device."""
        _push_if_changed(coordinator, "energy", energy_data)

    def _on_config_update(config_data: ConfigData) -> None:
        """Handle config updates from the Tewke device."""
        if _push_if_changed(coordinator, "config", config_data):
            _sync_device_metadata(hass, entry, config_data)

    def _on_heating_update(heating_data: HeatingData) -> None:
        """Handle heating updates and commands from the Tewke device."""
        _push_if_changed(coordinator, "heating", heating_data)
        if heating_data.command is not None:
            hass.async_create_task(
                async_handle_heating_command(hass, entry, heating_data.command)
            )

    try:
        LOGGER.debug(
            "Setting up CoAP observations for %s",
            entry.data.get(CONF_NAME, entry.entry_id),
        )
        observe_kwargs: dict[str, object] = {
            "scene_callback": _on_scene_update,
            "target_callback": _on_target_update,
            "sensor_callback": _on_sensor_update,
            "radar_callback": _on_radar_update,
            "energy_callback": _on_energy_update,
            "config_change_callback": _on_config_update,
        }
        if tap_supports_heating(tap):
            observe_kwargs["heating_callback"] = _on_heating_update
        await tap.observe(**observe_kwargs)
    except PyTewkeObserveError:
        LOGGER.warning(
            "Failed to set up CoAP observations for %s; will retry on next poll",
            entry.data.get(CONF_NAME, entry.entry_id),
            exc_info=True,
        )
        entry.runtime_data.observe_active = False
        if not coordinator.is_retrying_observe:
            coordinator.reset_observation_timeout()
        return False

    entry.runtime_data.observe_active = True
    coordinator.reset_observation_timeout()

    # Process scenes already fetched during initial discovery
    if coordinator.data and "scenes_all" in coordinator.data:
        _on_scene_update(coordinator.data["scenes_all"])

    if tap_supports_heating(tap):
        hass.async_create_task(async_push_heating_state(hass, entry))

    return True
