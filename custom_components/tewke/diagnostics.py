"""Diagnostics support for the Tewke integration."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST

from .coordinator import GRACE_PERIOD_SECS, sanitise_error_text
from .heating import (
    _get_active_pending_commands,
    get_configured_climate_entities,
    get_configured_primary_climate_entity,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import TewkeConfigEntry

TO_REDACT = {CONF_HOST, "hardware_id", "hardwareId", "wall_dock_id"}


def _serialize_value(val: Any) -> Any:
    """Convert Pydantic models and mappings to JSON-serializable structures."""
    if val is None:
        return None
    if hasattr(val, "as_dict"):
        return val.as_dict()
    if hasattr(val, "model_dump"):
        return val.model_dump(mode="json")
    if isinstance(val, dict):
        return {str(k): _serialize_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_serialize_value(v) for v in val]
    return val


async def async_get_config_entry_diagnostics(
    _hass: HomeAssistant,
    entry: TewkeConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime_data = entry.runtime_data
    tap = runtime_data.tap
    coordinator = runtime_data.coordinator
    raw_resources = tap.resources if isinstance(tap.resources, dict) else {}

    resources = {key: _serialize_value(info) for key, info in raw_resources.items()}
    coordinator_data = _serialize_value(coordinator.data) if coordinator.data else None

    now_fn = getattr(coordinator, "_monotonic_fn", None)
    raw_now = now_fn() if callable(now_fn) else None
    now = float(raw_now) if isinstance(raw_now, (int, float)) else time.monotonic()

    last_ok = getattr(coordinator, "last_update_success", True) is not False
    health_items = runtime_data.resource_health or {}
    any_reachable = last_ok and any(
        s.active
        or (
            s.has_data
            and s.unhealthy_since is not None
            and (now - s.unhealthy_since) < GRACE_PERIOD_SECS
        )
        for s in health_items.values()
    )
    dock_id = getattr(tap, "wall_dock_id", None)
    unique_id = getattr(entry, "unique_id", None)

    resource_health: dict[str, dict[str, object]] = {}
    for res, state in sorted(health_items.items()):
        is_non_obs = (
            res in raw_resources
            and getattr(raw_resources[res], "observable", True) is False
        )
        in_grace = bool(
            state.has_data
            and not state.active
            and state.unhealthy_since is not None
            and (now - state.unhealthy_since) < GRACE_PERIOD_SECS
        )
        if not last_ok:
            available = False
        elif is_non_obs:
            non_obs_ok = (
                state.unhealthy_since is None
                or (now - state.unhealthy_since) < GRACE_PERIOD_SECS
            )
            available = bool(state.has_data and non_obs_ok and any_reachable)
        else:
            available = bool(state.has_data and (state.active or in_grace))

        resource_health[res] = {
            "desired": state.desired,
            "active": state.active,
            "has_data": state.has_data,
            "in_grace_window": in_grace,
            "available": available,
            "attempt_count": state.attempt_count,
            "post_grace_attempts": state.post_grace_attempts,
            "last_error": (
                sanitise_error_text(
                    state.last_error,
                    runtime_data.host,
                    dock_id if isinstance(dock_id, str) else None,
                    unique_id if isinstance(unique_id, str) else None,
                )
                if state.last_error
                else None
            ),
        }

    raw_next_retry = getattr(coordinator, "next_retry_in_secs", None)
    raw_next_grace = getattr(coordinator, "next_grace_expiry_in_secs", None)
    recovery_scheduler = {
        "is_retrying": bool(getattr(coordinator, "is_retrying_observe", False)),
        "next_retry_in_secs": (
            float(raw_next_retry) if isinstance(raw_next_retry, (int, float)) else None
        ),
        "next_grace_expiry_in_secs": (
            float(raw_next_grace) if isinstance(raw_next_grace, (int, float)) else None
        ),
    }

    return async_redact_data(
        {
            "entry": {
                "title": entry.title,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "device": {
                "wall_dock_id": tap.wall_dock_id,
                "tewke_os_version": tap.tewke_os_version,
                "update_status": (
                    tap.update_status.value
                    if getattr(tap, "update_status", None) is not None
                    else None
                ),
                "observe_active": runtime_data.observe_active,
                "is_retrying_observe": bool(
                    getattr(coordinator, "is_retrying_observe", False)
                ),
                "pending_scenes_count": len(runtime_data.pending_scenes),
                "resources": resources,
                "resource_health": resource_health,
                "recovery_scheduler": recovery_scheduler,
            },
            "heating": {
                "configured_climate_entities": get_configured_climate_entities(entry),
                "primary_climate_entity": get_configured_primary_climate_entity(entry),
                "pending_heating_command_zones": sorted(
                    _get_active_pending_commands(entry)
                ),
            },
            "coordinator_data": coordinator_data,
        },
        TO_REDACT,
    )
