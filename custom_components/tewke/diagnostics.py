"""Diagnostics support for the Tewke integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import TewkeConfigEntry

TO_REDACT = {CONF_HOST, "hardware_id", "hardwareId"}


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

    resources = {
        key: _serialize_value(info) for key, info in (tap.resources or {}).items()
    }
    coordinator_data = _serialize_value(coordinator.data) if coordinator.data else None

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
                "is_retrying_observe": coordinator.is_retrying_observe,
                "pending_scenes_count": len(runtime_data.pending_scenes),
                "resources": resources,
            },
            "coordinator_data": coordinator_data,
        },
        TO_REDACT,
    )
