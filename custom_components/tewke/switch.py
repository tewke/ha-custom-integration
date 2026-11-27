"""Switch platform for the Tewke integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import CONF_DISABLED_SCENES, DISPATCHER_ADD_SCENES
from .scene import TewkeSceneSwitch, build_configured_scene_entities

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from pytewke.data import Scene

    from .data import TewkeConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tewke switch entities from a config entry."""
    coordinator = entry.runtime_data.coordinator
    entities, known_scene_ids = build_configured_scene_entities(
        hass,
        entry,
        "switch",
        lambda scene, enabled: TewkeSceneSwitch(
            coordinator=coordinator,
            scene=scene,
            enabled_default=enabled,
        ),
    )
    async_add_entities(entities)

    @callback
    def _async_add_new_scenes(scenes: list[Scene]) -> None:
        current_types = entry.runtime_data.scene_control_types
        to_add = [
            scene
            for scene in scenes
            if current_types.get(scene.id) == "switch"
            and scene.id not in known_scene_ids
        ]
        if not to_add:
            return
        known_scene_ids.update(scene.id for scene in to_add)
        async_add_entities(
            TewkeSceneSwitch(
                coordinator=coordinator,
                scene=scene,
                enabled_default=scene.id
                not in entry.data.get(CONF_DISABLED_SCENES, []),
            )
            for scene in to_add
        )

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{DISPATCHER_ADD_SCENES}_{entry.entry_id}",
            _async_add_new_scenes,
        )
    )
