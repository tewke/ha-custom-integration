"""Custom types for the Tewke integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
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
    in_flight_count: int = 0
    system_mode: str | None = None
    prev_system_mode: str | None = None
    target_temperature: float | None = None
    deferred_temp_mode: str | None = None
    expected_midpoint: float | None = None
    expected_low: float | None = None
    expected_high: float | None = None
    prev_target_temperature: float | None = None
    prev_low: float | None = None
    prev_high: float | None = None
    preset_mode: str | None = None
    prev_preset_modes: set[str | None] = field(default_factory=set)
    fan_mode: str | None = None
    prev_fan_modes: set[str | None] = field(default_factory=set)
    swing_mode: str | None = None
    prev_swing_modes: set[str | None] = field(default_factory=set)


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
    zone_deadband_spans: dict[str, float] = field(default_factory=dict)
    zone_commanded_ranges: dict[str, tuple[float, float]] = field(default_factory=dict)
    heating_zone_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
