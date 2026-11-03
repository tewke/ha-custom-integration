"""Custom types for the Tewke integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytewke
    from homeassistant.config_entries import ConfigEntry
    from pytewke.data import Scene

    from .coordinator import TewkeCoordinator

type TewkeConfigEntry = ConfigEntry[TewkeData]


@dataclass
class PendingZoneCommand:
    """Tracks a recent Tap-originated command to prevent stale HA state echoes."""

    expires_at: float
    system_mode: str | None = None
    prev_system_mode: str | None = None
    target_temperature: float | None = None
    prev_target_temperature: float | None = None
    preset_mode: str | None = None
    prev_preset_mode: str | None = None
    fan_mode: str | None = None
    prev_fan_mode: str | None = None


@dataclass
class TewkeData:
    """Data for the Tewke integration."""

    host: str
    tap: pytewke.Tap
    coordinator: TewkeCoordinator
    scene_control_types: dict[str, str]
    pending_scenes: dict[str, Scene] = field(default_factory=dict)
    observe_active: bool = False
    initial_heating_sync_done: bool = False
    climate_unsub: Callable[[], None] | None = None
    heating_reconcile_unsub: Callable[[], None] | None = None
    heating_commands_in_flight: int = 0
    pending_heating_commands: dict[str, PendingZoneCommand] = field(
        default_factory=dict
    )
