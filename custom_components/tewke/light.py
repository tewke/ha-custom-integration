"""Light platform for the Tewke integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import CONF_DISABLED_SCENES, DISPATCHER_ADD_SCENES
from .scene import TewkeSceneLight
from .target import TewkeTargetLight

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
    """Set up Tewke light entities from a config entry."""
    coordinator = entry.runtime_data.coordinator
    scene_control_types = entry.runtime_data.scene_control_types
    disabled_scenes: list[str] = entry.data.get(CONF_DISABLED_SCENES, [])

    entities = [
        TewkeSceneLight(
            coordinator=coordinator,
            scene=scene,
            enabled_default=scene_id not in disabled_scenes,
        )
        for scene_id, scene in coordinator.data["scenes"].items()
        if scene_control_types.get(scene_id) == "light"
    ]
    entities += [
        TewkeTargetLight(coordinator=coordinator, target=target)
        for target in coordinator.data["targets"].values()
    ]
    known_target_indices: set[int] = set(coordinator.data["targets"].keys())
    async_add_entities(entities)

    @callback
    def _async_check_new_targets() -> None:
        if not coordinator.data:
            return
        targets = coordinator.data.get("targets", {})
        new_targets = [
            target for idx, target in targets.items() if idx not in known_target_indices
        ]
        if new_targets:
            known_target_indices.update(t.index for t in new_targets)
            async_add_entities(
                TewkeTargetLight(coordinator=coordinator, target=target)
                for target in new_targets
            )

    entry.async_on_unload(coordinator.async_add_listener(_async_check_new_targets))

    @callback
    def _async_add_new_scenes(scenes: list[Scene]) -> None:
        current_types = entry.runtime_data.scene_control_types
        async_add_entities(
            TewkeSceneLight(
                coordinator=coordinator,
                scene=scene,
                enabled_default=scene.id
                not in entry.data.get(CONF_DISABLED_SCENES, []),
            )
            for scene in scenes
            if current_types.get(scene.id) == "light"
        )

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{DISPATCHER_ADD_SCENES}_{entry.entry_id}",
            _async_add_new_scenes,
        )
    )
