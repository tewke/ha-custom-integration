"""Home Assistant climate entity bridge for the Tewke Tap Heating Screen."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_FAN_MODE,
    ATTR_FAN_MODES,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_PRESET_MODE,
    ATTR_PRESET_MODES,
    ATTR_SWING_MODE,
    ATTR_SWING_MODES,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_STEP,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_SWING_MODE,
    SERVICE_SET_TEMPERATURE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    ATTR_SUPPORTED_FEATURES,
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from pytewke.data import (
    HeatingCommandData,
    HeatingData,
    HeatingSystemMode,
    HeatingZoneData,
)
from pytewke.error import BasePyTewkeError

from .const import (
    CONF_CLIMATE_ENTITIES,
    CONF_PRIMARY_CLIMATE_ENTITY,
    LOGGER,
)
from .data import PendingZoneCommand

if TYPE_CHECKING:
    from datetime import datetime

    from .data import TewkeConfigEntry

_DEFAULT_TEMP = 20.0
_DEFAULT_MIN_TEMP = 5.0
_DEFAULT_MAX_TEMP = 35.0
_DEFAULT_STEP = 0.5
_DEFAULT_DEADBAND_SPAN = 2.0
_COMMAND_GRACE_SECS = 15.0
_FLOAT_TOL = 0.05
_DEFAULT_SYSTEM_MODES: tuple[str, ...] = (HVACMode.HEAT, HVACMode.OFF)
_HEAT_ACTIVE_MODES: frozenset[str] = frozenset({"heat", "auto", "heat_cool"})
_COOL_ACTIVE_MODES: frozenset[str] = frozenset({"cool", "auto", "heat_cool"})


def tap_supports_heating(tap: object) -> bool:
    """Return True if the Tap advertises the `/heating` CoAP resource."""
    resources = getattr(tap, "resources", None)
    return isinstance(resources, Mapping) and "heating" in resources


def get_configured_climate_entities(entry: TewkeConfigEntry) -> list[str]:
    """Return configured HA climate entity IDs, preferring options over data."""
    if CONF_CLIMATE_ENTITIES in entry.options:
        raw = entry.options.get(CONF_CLIMATE_ENTITIES)
    else:
        raw = entry.data.get(CONF_CLIMATE_ENTITIES)
    if not isinstance(raw, list):
        return []
    return [str(entity_id) for entity_id in raw if isinstance(entity_id, str)]


def get_configured_primary_climate_entity(entry: TewkeConfigEntry) -> str | None:
    """Return configured primary climate entity ID, preferring options over data."""
    if CONF_PRIMARY_CLIMATE_ENTITY in entry.options:
        raw = entry.options.get(CONF_PRIMARY_CLIMATE_ENTITY)
    else:
        raw = entry.data.get(CONF_PRIMARY_CLIMATE_ENTITY)
    return raw if isinstance(raw, str) and raw else None


def _resolve_primary_entity(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
    climate_entities: list[str],
) -> str | None:
    """Determine which climate entity represents the Tap's own room."""
    if not climate_entities:
        return None

    explicit = get_configured_primary_climate_entity(entry)
    if explicit and explicit in climate_entities:
        return explicit

    if len(climate_entities) == 1:
        return climate_entities[0]

    room_name = entry.data.get("room_name")
    if isinstance(room_name, str) and room_name.strip():
        target_room = room_name.strip().lower()
        try:
            ent_reg = er.async_get(hass)
            dev_reg = dr.async_get(hass)
            area_reg = ar.async_get(hass)
            for entity_id in climate_entities:
                ent_entry = ent_reg.async_get(entity_id)
                if ent_entry is None:
                    continue
                area_id = ent_entry.area_id
                if not area_id and ent_entry.device_id:
                    dev_entry = dev_reg.async_get(ent_entry.device_id)
                    if dev_entry is not None:
                        area_id = dev_entry.area_id
                if area_id:
                    area = area_reg.async_get_area(area_id)
                    if area and area.name.strip().lower() == target_room:
                        return entity_id
        except Exception:  # noqa: BLE001
            LOGGER.debug("Unable to match climate entities by HA area", exc_info=True)

    return climate_entities[0]


def _parse_float(raw: Any, default: float) -> float:
    """Convert raw attribute value to finite float, falling back to default."""
    if raw is None or isinstance(raw, bool):
        return default
    try:
        val = float(raw)
    except TypeError, ValueError:
        return default
    return val if math.isfinite(val) else default


def _parse_optional_float(raw: Any) -> float | None:
    """Convert raw attribute value to finite float, returning None if invalid."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except TypeError, ValueError:
        return None
    return val if math.isfinite(val) else None


def _parse_int(raw: Any, default: int = 0) -> int:
    """Convert raw attribute value to int, falling back to default."""
    if raw is None or isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except TypeError, ValueError:
        return default


def _parse_non_empty_str(raw: Any) -> str | None:
    """Return stripped non-empty string or None."""
    if raw is None or isinstance(raw, bool):
        return None
    return str(raw).strip() or None


def _parse_str_list(raw: Any) -> list[str]:
    """Return a list of non-empty strings from a raw attribute sequence."""
    if not isinstance(raw, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in raw:
        val = _parse_non_empty_str(item)
        if val is not None:
            result.append(val)
    return result


def _half_up_to_step(value: float, step: float) -> float:
    """Quantise `value` onto `step` increments using half-up rounding."""
    effective_step = step if step > 0 else _DEFAULT_STEP
    return round(math.floor(value / effective_step + 0.5) * effective_step, 4)


def _half_down_to_step(value: float, step: float) -> float:
    """Quantise `value` onto `step` increments using half-down rounding."""
    effective_step = step if step > 0 else _DEFAULT_STEP
    return round(math.ceil(value / effective_step - 0.5) * effective_step, 4)


_DIRECT_MODE_MAP: dict[str, HeatingSystemMode] = {
    HVACMode.OFF: "off",
    HVACMode.COOL: "cool",
    "cooling": "cool",
    HVACMode.HEAT_COOL: "heat_cool",
    "heatcool": "heat_cool",
    HVACMode.AUTO: "auto",
    HVACMode.DRY: "dry",
    "drying": "dry",
    HVACMode.FAN_ONLY: "fan_only",
    "fanonly": "fan_only",
    "fan": "fan_only",
    HVACMode.HEAT: "heat",
    "heating": "heat",
}


def _resolve_system_modes(raw_hvac_modes: Any) -> list[str]:
    """Filter HA hvac_modes to supported Tewke system modes."""
    if not isinstance(raw_hvac_modes, (list, tuple, set)):
        return list(_DEFAULT_SYSTEM_MODES)
    modes: list[str] = []
    for mode in raw_hvac_modes:
        mapped = _DIRECT_MODE_MAP.get(str(mode).strip().lower())
        if mapped is not None and mapped not in modes:
            modes.append(mapped)
    return modes or list(_DEFAULT_SYSTEM_MODES)


def _resolve_system_mode(
    state_str: str,
    *,
    actively_cooling: bool,
    actively_heating: bool,
    system_modes: list[str],
) -> HeatingSystemMode:
    """Map an HA climate state string to a Tewke system mode."""
    direct = _DIRECT_MODE_MAP.get(str(state_str).strip().lower())
    if direct is not None:
        return direct
    if actively_cooling:
        return "cool"
    if actively_heating:
        return "heat"
    for candidate in ("heat", "cool", "heat_cool", "auto", "dry", "fan_only"):
        if candidate in system_modes:
            return candidate
    return "heat"


def _is_range_mode(
    *,
    supported_features: int,
    effective_mode: str,
    raw_single: float | None,
    raw_low: float | None,
    raw_high: float | None,
) -> bool:
    """Return True when an entity uses `target_temp_low` / `target_temp_high`."""
    supports_single = bool(supported_features & ClimateEntityFeature.TARGET_TEMPERATURE)
    supports_range = bool(
        supported_features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    )
    if supports_range and not supports_single:
        return True
    if supports_range and effective_mode == "heat_cool":
        return True
    if supports_range and effective_mode == "auto" and raw_single is None:
        return True
    return (
        supported_features == 0
        and raw_single is None
        and raw_low is not None
        and raw_high is not None
    )


def _can_set_temperature(
    *,
    supported_features: int,
    system_mode: str,
    raw_single: float | None,
    raw_low: float | None,
    raw_high: float | None,
) -> bool:
    """Return True if the entity supports writable setpoints in active thermal modes."""
    supports_single = bool(supported_features & ClimateEntityFeature.TARGET_TEMPERATURE)
    supports_range = bool(
        supported_features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    )
    if supported_features != 0 and not supports_single and not supports_range:
        return False
    return not (
        system_mode == "auto"
        and raw_single is None
        and raw_low is None
        and raw_high is None
    )


def _resolve_pending_mode(
    raw_mode: HeatingSystemMode,
    pending: PendingZoneCommand | None,
    *,
    mutate: bool = True,
) -> tuple[HeatingSystemMode, bool]:
    """Resolve `system_mode` against `pending` and return `(mode, is_confirmed)`."""
    if pending is None or pending.system_mode is None:
        return raw_mode, True
    if raw_mode == pending.system_mode:
        if mutate:
            pending.system_mode = None
            pending.prev_system_mode = None
        return raw_mode, True
    if not mutate or pending.prev_system_mode in (raw_mode, "off"):
        return pending.system_mode, False  # type: ignore[return-value]
    pending.system_mode = None
    pending.prev_system_mode = None
    return raw_mode, True


def _reconcile_str_field(
    incoming: str | None,
    pending_val: str | None,
    prev_vals: set[str | None],
    *,
    mutate: bool = True,
) -> tuple[str | None, str | None]:
    """Return `(resolved, next_pending)` for a string field."""
    if pending_val is None or incoming == pending_val:
        if mutate:
            prev_vals.clear()
        return incoming, (None if mutate else pending_val)
    if not mutate or incoming in prev_vals:
        return pending_val, pending_val
    prev_vals.clear()
    return incoming, None


def _matches_any(val: float | None, candidates: tuple[float | None, ...]) -> bool:
    """Return True if `val` matches any non-None candidate within `_FLOAT_TOL`."""
    if val is None:
        return False
    return any(
        c is not None and math.isclose(val, c, abs_tol=_FLOAT_TOL) for c in candidates
    )


def _matches_any_set(val: float | None, candidates: set[float]) -> bool:
    """Return True if `val` matches any entry in `candidates` within `_FLOAT_TOL`."""
    if val is None:
        return False
    return any(math.isclose(val, c, abs_tol=_FLOAT_TOL) for c in candidates)


def _clear_pending_temp(pending: PendingZoneCommand) -> None:
    """Clear all target-temperature fields on `pending`."""
    pending.target_temperature = None
    pending.deferred_temp_mode = None
    pending.expected_midpoint = None
    pending.expected_low = None
    pending.expected_high = None
    pending.prev_targets.clear()
    pending.prev_lows.clear()
    pending.prev_highs.clear()


def _reconcile_target_temperature(
    incoming_temp: float,
    pending: PendingZoneCommand,
    *,
    raw_low: float | None,
    raw_high: float | None,
    mutate: bool = True,
) -> float:
    """Resolve `target_temperature` against `pending`."""
    if pending.target_temperature is None:
        return incoming_temp

    expected = (
        pending.expected_midpoint
        if pending.expected_midpoint is not None
        else pending.target_temperature
    )
    if not mutate or pending.deferred_temp_mode is not None:
        return expected

    if pending.expected_low is not None and pending.expected_high is not None:
        settled = _matches_any(raw_low, (pending.expected_low,)) and _matches_any(
            raw_high, (pending.expected_high,)
        )
        in_flight = (
            not settled
            and (
                raw_low is None
                or _matches_any(raw_low, (pending.expected_low,))
                or _matches_any_set(raw_low, pending.prev_lows)
            )
            and (
                raw_high is None
                or _matches_any(raw_high, (pending.expected_high,))
                or _matches_any_set(raw_high, pending.prev_highs)
            )
        )
    else:
        settled = math.isclose(incoming_temp, expected, abs_tol=_FLOAT_TOL)
        in_flight = not settled and _matches_any_set(
            incoming_temp, pending.prev_targets
        )

    if in_flight:
        return expected
    _clear_pending_temp(pending)
    return incoming_temp


def _compute_grid_bounds(
    raw_min: float,
    raw_max: float,
    step: float,
) -> tuple[float, float]:
    """Return `(min_grid, max_grid)` aligned inward onto `step`."""
    min_temp, max_temp = (
        (raw_min, raw_max) if raw_min <= raw_max else (raw_max, raw_min)
    )
    min_grid = round(math.ceil(min_temp / step) * step, 4)
    max_grid = round(math.floor(max_temp / step) * step, 4)
    return (min_grid, max_grid) if min_grid <= max_grid else (min_temp, max_temp)


def _matches_commanded_range(
    raw_low: float,
    raw_high: float,
    cmd_range: tuple[float, float] | None,
) -> bool:
    """Return True if `(raw_low, raw_high)` matches the last Tap-commanded bounds."""
    if cmd_range is None:
        return False
    return math.isclose(raw_low, cmd_range[0], abs_tol=_FLOAT_TOL) and math.isclose(
        raw_high, cmd_range[1], abs_tol=_FLOAT_TOL
    )


def _resolve_temperatures_and_bounds(
    entity_id: str,
    attrs: Mapping[str, Any],
    *,
    effective_mode: str,
    deadband_spans: dict[str, float] | None,
    preserve_cached_span: bool,
) -> tuple[float, float, float, float, bool, float | None, float | None]:
    """Compute `(current_temp, target_temp, min_t, max_t, can_set, low, high)`."""
    supported_features = _parse_int(attrs.get(ATTR_SUPPORTED_FEATURES), 0)
    raw_single = _parse_optional_float(attrs.get(ATTR_TEMPERATURE))
    raw_low = _parse_optional_float(attrs.get(ATTR_TARGET_TEMP_LOW))
    raw_high = _parse_optional_float(attrs.get(ATTR_TARGET_TEMP_HIGH))
    raw_current = _parse_optional_float(attrs.get(ATTR_CURRENT_TEMPERATURE))
    raw_min = _parse_float(attrs.get(ATTR_MIN_TEMP), _DEFAULT_MIN_TEMP)
    raw_max = _parse_float(attrs.get(ATTR_MAX_TEMP), _DEFAULT_MAX_TEMP)
    min_temp, max_temp = (
        (raw_min, raw_max) if raw_min <= raw_max else (raw_max, raw_min)
    )
    step = _parse_float(attrs.get(ATTR_TARGET_TEMP_STEP), _DEFAULT_STEP)
    if step <= 0:
        step = _DEFAULT_STEP

    can_set_temp = _can_set_temperature(
        supported_features=supported_features,
        system_mode=effective_mode,
        raw_single=raw_single,
        raw_low=raw_low,
        raw_high=raw_high,
    )
    if not _is_range_mode(
        supported_features=supported_features,
        effective_mode=effective_mode,
        raw_single=raw_single,
        raw_low=raw_low,
        raw_high=raw_high,
    ):
        fallback_target = (
            raw_single
            if raw_single is not None
            else (raw_high if effective_mode == "cool" else raw_low)
        )
        current_temp = (
            raw_current
            if raw_current is not None
            else (fallback_target if fallback_target is not None else _DEFAULT_TEMP)
        )
        target_temp = fallback_target if fallback_target is not None else current_temp
        return (
            current_temp,
            target_temp,
            min_temp,
            max_temp,
            can_set_temp,
            raw_low,
            raw_high,
        )

    cached_span = deadband_spans.get(entity_id) if deadband_spans is not None else None
    if raw_low is not None and raw_high is not None and raw_high >= raw_low:
        live_span = (
            _DEFAULT_DEADBAND_SPAN
            if math.isclose(raw_high, raw_low, abs_tol=1e-6)
            else max(_half_up_to_step(raw_high - raw_low, step), step)
        )
        if not preserve_cached_span and deadband_spans is not None:
            deadband_spans[entity_id] = live_span
        span = cached_span if (preserve_cached_span and cached_span) else live_span
        target_temp = _half_up_to_step((raw_low + raw_high) / 2.0, _DEFAULT_STEP)
    else:
        span = cached_span if cached_span is not None else _DEFAULT_DEADBAND_SPAN
        target_temp = raw_current if raw_current is not None else _DEFAULT_TEMP
    current_temp = raw_current if raw_current is not None else target_temp

    min_grid, max_grid = _compute_grid_bounds(min_temp, max_temp, step)
    cand_min = math.ceil((min_grid + span / 2.0) * 2.0) / 2.0
    cand_max = math.floor((max_grid - span / 2.0) * 2.0) / 2.0
    if cand_min <= cand_max:
        effective_min, effective_max = cand_min, cand_max
    else:
        collapsed = _half_up_to_step((min_grid + max_grid) / 2.0, _DEFAULT_STEP)
        effective_min, effective_max = collapsed, collapsed
    return (
        current_temp,
        target_temp,
        effective_min,
        effective_max,
        can_set_temp,
        raw_low,
        raw_high,
    )


def _apply_pending_fields(
    zone: HeatingZoneData,
    pending: PendingZoneCommand | None,
    *,
    raw_low: float | None,
    raw_high: float | None,
    mutate: bool,
) -> HeatingZoneData:
    """Reconcile non-mode pending fields on `zone`."""
    if pending is None:
        return zone
    zone.target_temperature = _reconcile_target_temperature(
        zone.target_temperature,
        pending,
        raw_low=raw_low,
        raw_high=raw_high,
        mutate=mutate,
    )
    zone.preset_mode, pending.preset_mode = _reconcile_str_field(
        zone.preset_mode,
        pending.preset_mode,
        pending.prev_preset_modes,
        mutate=mutate,
    )
    zone.fan_mode, pending.fan_mode = _reconcile_str_field(
        zone.fan_mode,
        pending.fan_mode,
        pending.prev_fan_modes,
        mutate=mutate,
    )
    zone.swing_mode, pending.swing_mode = _reconcile_str_field(
        zone.swing_mode,
        pending.swing_mode,
        pending.prev_swing_modes,
        mutate=mutate,
    )
    return zone


def _build_zone_from_state(
    entity_id: str,
    state: Any,
    *,
    is_this_room: bool,
    pending: PendingZoneCommand | None = None,
    deadband_spans: dict[str, float] | None = None,
) -> HeatingZoneData:
    """Build a single `HeatingZoneData` from an HA climate state object."""
    if state is None:
        return HeatingZoneData(
            id=entity_id,
            name=entity_id,
            current_temperature=_DEFAULT_TEMP,
            target_temperature=_DEFAULT_TEMP,
            system_mode="off",
            actively_heating=False,
            actively_cooling=False,
            online=False,
            is_this_room=is_this_room,
            min_temp=_DEFAULT_MIN_TEMP,
            max_temp=_DEFAULT_MAX_TEMP,
        )

    online = state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
    attrs = state.attributes
    name = str(attrs.get(ATTR_FRIENDLY_NAME) or entity_id)
    system_modes = _resolve_system_modes(attrs.get(ATTR_HVAC_MODES))

    hvac_action = str(attrs.get(ATTR_HVAC_ACTION) or "").strip().lower()
    raw_heating = bool(hvac_action == HVACAction.HEATING)
    raw_cooling = bool(hvac_action == HVACAction.COOLING)
    raw_mode = _resolve_system_mode(
        state.state,
        actively_cooling=raw_cooling,
        actively_heating=raw_heating,
        system_modes=system_modes,
    )
    mutate_pending = pending is None or pending.in_flight_count <= 0
    resolved_mode, mode_confirmed = _resolve_pending_mode(
        raw_mode, pending, mutate=mutate_pending
    )
    preserve_span = bool(pending is not None and pending.target_temperature is not None)
    if (
        not preserve_span
        and deadband_spans is not None
        and entity_id in deadband_spans
        and deadband_spans.get(f"__cmd_low__:{entity_id}") is not None
    ):
        raw_low_attr = _parse_optional_float(attrs.get(ATTR_TARGET_TEMP_LOW))
        raw_high_attr = _parse_optional_float(attrs.get(ATTR_TARGET_TEMP_HIGH))
        cmd_pair = (
            deadband_spans.get(f"__cmd_low__:{entity_id}", -999.0),
            deadband_spans.get(f"__cmd_high__:{entity_id}", -999.0),
        )
        preserve_span = bool(
            raw_low_attr is not None
            and raw_high_attr is not None
            and _matches_commanded_range(raw_low_attr, raw_high_attr, cmd_pair)
        )
    (
        current_temp,
        target_temp,
        effective_min,
        effective_max,
        can_set_temp,
        raw_low,
        raw_high,
    ) = _resolve_temperatures_and_bounds(
        entity_id,
        attrs,
        effective_mode=resolved_mode,
        deadband_spans=deadband_spans,
        preserve_cached_span=preserve_span,
    )

    actively_heating = (
        mode_confirmed and raw_heating and resolved_mode in _HEAT_ACTIVE_MODES
    )
    actively_cooling = (
        mode_confirmed
        and raw_cooling
        and resolved_mode in _COOL_ACTIVE_MODES
        and not actively_heating
    )

    zone = HeatingZoneData(
        id=entity_id,
        name=name,
        current_temperature=current_temp,
        target_temperature=target_temp,
        system_mode=resolved_mode,
        actively_heating=actively_heating,
        actively_cooling=actively_cooling,
        online=online,
        is_this_room=is_this_room,
        min_temp=effective_min,
        max_temp=effective_max,
        can_set_temperature=can_set_temp,
        system_modes=system_modes,
        preset_mode=_parse_non_empty_str(attrs.get(ATTR_PRESET_MODE)),
        preset_modes=_parse_str_list(attrs.get(ATTR_PRESET_MODES)),
        fan_mode=_parse_non_empty_str(attrs.get(ATTR_FAN_MODE)),
        fan_modes=_parse_str_list(attrs.get(ATTR_FAN_MODES)),
        swing_mode=_parse_non_empty_str(attrs.get(ATTR_SWING_MODE)),
        swing_modes=_parse_str_list(attrs.get(ATTR_SWING_MODES)),
    )
    resolved = _apply_pending_fields(
        zone,
        pending,
        raw_low=raw_low,
        raw_high=raw_high,
        mutate=mutate_pending,
    )
    LOGGER.debug(
        "Built zone %s: state=%r -> mode=%r (modes=%s, fan=%r, swing=%r, set_temp=%s)",
        entity_id,
        state.state,
        resolved.system_mode,
        resolved.system_modes,
        resolved.fan_mode,
        resolved.swing_mode,
        resolved.can_set_temperature,
    )
    return resolved


def _is_pending_empty(cmd: PendingZoneCommand) -> bool:
    """Return True if all tracked command fields on `cmd` have been cleared."""
    return (
        cmd.system_mode is None
        and cmd.target_temperature is None
        and cmd.preset_mode is None
        and cmd.fan_mode is None
        and cmd.swing_mode is None
    )


def _get_active_pending_commands(
    entry: TewkeConfigEntry,
) -> dict[str, PendingZoneCommand]:
    """Return unexpired or actively running pending Tap commands for the entry."""
    runtime_data = getattr(entry, "runtime_data", None)
    raw = getattr(runtime_data, "pending_heating_commands", None)
    if not isinstance(raw, dict):
        return {}
    now = time.monotonic()
    stale = [
        zid
        for zid, cmd in raw.items()
        if cmd.in_flight_count <= 0
        and (cmd.expires_at <= now or _is_pending_empty(cmd))
    ]
    for zid in stale:
        raw.pop(zid, None)
    return raw


def _get_deadband_spans(entry: TewkeConfigEntry) -> dict[str, float] | None:
    """Return the per-zone deadband span cache on `entry.runtime_data` if available."""
    runtime_data = getattr(entry, "runtime_data", None)
    spans = getattr(runtime_data, "zone_deadband_spans", None)
    return spans if isinstance(spans, dict) else None


def _get_commanded_ranges(
    entry: TewkeConfigEntry,
) -> dict[str, tuple[float, float]] | None:
    """Return the per-zone last Tap-commanded `(low, high)` cache if available."""
    runtime_data = getattr(entry, "runtime_data", None)
    ranges = getattr(runtime_data, "zone_commanded_ranges", None)
    return ranges if isinstance(ranges, dict) else None


def _get_zone_lock(entry: TewkeConfigEntry, zone_id: str) -> asyncio.Lock | None:
    """Return the per-zone `asyncio.Lock` on `entry.runtime_data` if available."""
    runtime_data = getattr(entry, "runtime_data", None)
    locks = getattr(runtime_data, "heating_zone_locks", None)
    if not isinstance(locks, dict):
        return None
    if zone_id not in locks:
        locks[zone_id] = asyncio.Lock()
    return locks[zone_id]


def build_heating_zones(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
) -> dict[str, HeatingZoneData]:
    """Build `HeatingZoneData` objects from current Home Assistant climate states."""
    climate_entities = get_configured_climate_entities(entry)
    if not climate_entities:
        return {}

    primary_entity = _resolve_primary_entity(hass, entry, climate_entities)
    pending_map = _get_active_pending_commands(entry)
    deadband_spans = _get_deadband_spans(entry)
    return {
        entity_id: _build_zone_from_state(
            entity_id,
            hass.states.get(entity_id),
            is_this_room=entity_id == primary_entity,
            pending=pending_map.get(entity_id),
            deadband_spans=deadband_spans,
        )
        for entity_id in climate_entities
    }


def _adopt_tap_zones_if_unconfigured(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
) -> list[str]:
    """Adopt active heating zones already present on the Tap on initial startup."""
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None or getattr(
        runtime_data, "initial_heating_sync_done", False
    ):
        return []
    coordinator = getattr(runtime_data, "coordinator", None)
    coord_data = getattr(coordinator, "data", None)
    if not isinstance(coord_data, Mapping):
        return []
    tap_heating: HeatingData | None = coord_data.get("heating")
    if tap_heating is None or not tap_heating.active or not tap_heating.zones:
        return []

    valid_entities = [
        zid for zid in tap_heating.zones if hass.states.get(zid) is not None
    ]
    if not valid_entities:
        return []

    primary_id = next(
        (
            z.id
            for z in tap_heating.zones.values()
            if z.is_this_room and z.id in valid_entities
        ),
        valid_entities[0],
    )
    LOGGER.info(
        "Adopting active climate entities from Tap %s: %s (primary=%s)",
        entry.entry_id,
        valid_entities,
        primary_id,
    )
    if hasattr(hass, "config_entries") and hasattr(
        hass.config_entries, "async_update_entry"
    ):
        new_options = {
            **entry.options,
            CONF_CLIMATE_ENTITIES: valid_entities,
            CONF_PRIMARY_CLIMATE_ENTITY: primary_id,
        }
        hass.config_entries.async_update_entry(entry, options=new_options)
    return valid_entities


def _merge_prev_set(
    new_cmd_val: str | None,
    current_val: str | None,
    existing_pending_val: str | None,
    existing_prev_set: set[str | None] | None,
) -> set[str | None]:
    """Build the eligible baseline set when updating or retaining a pending string."""
    if new_cmd_val is None:
        return set(existing_prev_set) if existing_prev_set else set()
    merged: set[str | None] = {current_val}
    if existing_pending_val is not None:
        merged.add(existing_pending_val)
        if existing_prev_set:
            merged.update(existing_prev_set)
    return merged


def _merge_prev_float_set(
    new_temp: float | None,
    current_val: float | None,
    superseded_val: float | None,
    existing_set: set[float] | None,
) -> set[float]:
    """Build the eligible float baseline set when recording a temperature command."""
    if new_temp is None:
        return set(existing_set) if existing_set else set()
    merged: set[float] = set(existing_set) if existing_set else set()
    if current_val is not None:
        merged.add(current_val)
    if superseded_val is not None:
        merged.add(superseded_val)
    return merged


def _record_pending_command(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
    command: HeatingCommandData,
) -> PendingZoneCommand | None:
    """Record a Tap command so stale HA states do not echo back during execution."""
    runtime_data = getattr(entry, "runtime_data", None)
    pending_map = getattr(runtime_data, "pending_heating_commands", None)
    if not isinstance(pending_map, dict):
        return None

    active = _get_active_pending_commands(entry)
    existing = active.get(command.zone_id)
    raw_state = hass.states.get(command.zone_id)
    raw_attrs = raw_state.attributes if raw_state is not None else {}
    raw_low = _parse_optional_float(raw_attrs.get(ATTR_TARGET_TEMP_LOW))
    raw_high = _parse_optional_float(raw_attrs.get(ATTR_TARGET_TEMP_HIGH))

    current_zone = _build_zone_from_state(
        command.zone_id,
        raw_state,
        is_this_room=False,
        pending=existing,
        deadband_spans=_get_deadband_spans(entry),
    )
    has_prev_mode = existing is not None and existing.system_mode is not None
    has_new_temp = command.target_temperature is not None

    recorded = PendingZoneCommand(
        expires_at=time.monotonic() + _COMMAND_GRACE_SECS,
        in_flight_count=(existing.in_flight_count if existing is not None else 0) + 1,
        system_mode=command.system_mode
        if command.system_mode is not None
        else (existing.system_mode if existing else None),
        prev_system_mode=(
            existing.prev_system_mode
            if (command.system_mode is None or has_prev_mode) and existing
            else (current_zone.system_mode if command.system_mode is not None else None)
        ),
        target_temperature=command.target_temperature
        if has_new_temp
        else (existing.target_temperature if existing else None),
        had_unsettled_temp=bool(
            existing is not None and existing.target_temperature is not None
        ),
        deferred_temp_mode=None
        if has_new_temp
        else (existing.deferred_temp_mode if existing else None),
        expected_midpoint=None
        if has_new_temp
        else (existing.expected_midpoint if existing else None),
        expected_low=None
        if has_new_temp
        else (existing.expected_low if existing else None),
        expected_high=None
        if has_new_temp
        else (existing.expected_high if existing else None),
        prev_targets=_merge_prev_float_set(
            command.target_temperature,
            current_zone.target_temperature,
            (existing.expected_midpoint or existing.target_temperature)
            if existing is not None
            else None,
            existing.prev_targets if existing is not None else None,
        ),
        prev_lows=_merge_prev_float_set(
            command.target_temperature,
            raw_low,
            existing.expected_low if existing is not None else None,
            existing.prev_lows if existing is not None else None,
        ),
        prev_highs=_merge_prev_float_set(
            command.target_temperature,
            raw_high,
            existing.expected_high if existing is not None else None,
            existing.prev_highs if existing is not None else None,
        ),
        preset_mode=command.preset_mode
        if command.preset_mode is not None
        else (existing.preset_mode if existing else None),
        prev_preset_modes=_merge_prev_set(
            command.preset_mode,
            current_zone.preset_mode,
            existing.preset_mode if existing else None,
            existing.prev_preset_modes if existing else None,
        ),
        fan_mode=command.fan_mode
        if command.fan_mode is not None
        else (existing.fan_mode if existing else None),
        prev_fan_modes=_merge_prev_set(
            command.fan_mode,
            current_zone.fan_mode,
            existing.fan_mode if existing else None,
            existing.prev_fan_modes if existing else None,
        ),
        swing_mode=command.swing_mode
        if command.swing_mode is not None
        else (existing.swing_mode if existing else None),
        prev_swing_modes=_merge_prev_set(
            command.swing_mode,
            current_zone.swing_mode,
            existing.swing_mode if existing else None,
            existing.prev_swing_modes if existing else None,
        ),
    )
    pending_map[command.zone_id] = recorded
    return recorded


def _schedule_reconcile_push(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
    *,
    force_immediate_if_empty: bool = False,
) -> None:
    """Schedule a post-command reconciliation push at the earliest grace expiry."""
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        return
    prev_unsub: Callable[[], None] | None = getattr(
        runtime_data, "heating_reconcile_unsub", None
    )
    if prev_unsub is not None:
        prev_unsub()
        runtime_data.heating_reconcile_unsub = None

    active_pending = _get_active_pending_commands(entry)
    if not active_pending:
        if force_immediate_if_empty and hasattr(hass, "async_create_task"):
            hass.async_create_task(async_push_heating_state(hass, entry))
        return
    settled_cmds = [c for c in active_pending.values() if c.in_flight_count <= 0]
    if not settled_cmds or not hasattr(hass, "loop"):
        return

    delay = max(
        0.1,
        min(cmd.expires_at for cmd in settled_cmds) - time.monotonic() + 0.1,
    )

    @callback
    def _on_reconcile(_now: datetime) -> None:
        runtime_data.heating_reconcile_unsub = None
        hass.async_create_task(async_push_heating_state(hass, entry))
        if _get_active_pending_commands(entry):
            _schedule_reconcile_push(hass, entry)

    try:
        runtime_data.heating_reconcile_unsub = async_call_later(
            hass, delay, _on_reconcile
        )
    except Exception:  # noqa: BLE001
        LOGGER.debug("Skipping delayed heating reconcile timer", exc_info=True)


async def async_push_heating_state(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
) -> HeatingData | None:
    """Push current HA climate zone states (or clear heating) to the Tap over CoAP."""
    if not hasattr(entry, "runtime_data") or entry.runtime_data is None:
        return None

    tap = entry.runtime_data.tap
    if not tap_supports_heating(tap):
        return None

    climate_entities = get_configured_climate_entities(entry)
    if not climate_entities:
        climate_entities = _adopt_tap_zones_if_unconfigured(hass, entry)
    entry.runtime_data.initial_heating_sync_done = True

    try:
        if not climate_entities:
            updated = await tap.clear_heating()
        else:
            zones = build_heating_zones(hass, entry)
            updated = await tap.set_heating(zones, active=True)
    except (BasePyTewkeError, TimeoutError) as err:
        LOGGER.warning("Failed to push heating state to Tewke Tap: %s", err)
        return None

    coordinator = entry.runtime_data.coordinator
    if coordinator.data is not None:
        coordinator.async_set_updated_data({**coordinator.data, "heating": updated})
    return updated


async def _async_call_climate_service(
    hass: HomeAssistant,
    service: str,
    data: dict[str, Any],
) -> bool:
    """Call a single Home Assistant climate service, returning True on success."""
    try:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            service,
            data,
            blocking=True,
        )
    except Exception:  # noqa: BLE001
        LOGGER.exception(
            "Failed to call climate.%s with %s",
            service,
            data,
        )
        return False
    return True


def _resolve_command_span(
    entry: TewkeConfigEntry,
    zone_id: str,
    raw_low: float | None,
    raw_high: float | None,
    step: float,
) -> float:
    """Resolve the nominal deadband span for an outbound range command."""
    deadband_spans = _get_deadband_spans(entry)
    cached_span = deadband_spans.get(zone_id) if deadband_spans is not None else None
    cmd_ranges = _get_commanded_ranges(entry)
    cmd_range = cmd_ranges.get(zone_id) if cmd_ranges is not None else None
    pending_cmd = _get_active_pending_commands(entry).get(zone_id)
    has_unsettled_or_commanded = bool(
        (
            raw_low is not None
            and raw_high is not None
            and _matches_commanded_range(raw_low, raw_high, cmd_range)
        )
        or (
            pending_cmd is not None
            and (pending_cmd.expected_low is not None or pending_cmd.had_unsettled_temp)
        )
    )
    if has_unsettled_or_commanded and cached_span is not None:
        span = cached_span
    elif raw_low is not None and raw_high is not None and raw_high > raw_low:
        span = max(_half_up_to_step(raw_high - raw_low, step), step)
    elif cached_span is not None:
        span = cached_span
    else:
        span = _DEFAULT_DEADBAND_SPAN
    if deadband_spans is not None:
        deadband_spans[zone_id] = span
    return span


def _build_set_temperature_payload(
    entry: TewkeConfigEntry,
    zone_id: str,
    target_temp: float,
    *,
    effective_mode: str,
    state: Any,
) -> tuple[dict[str, Any] | None, float | None, float | None, float | None]:
    """Build `set_temperature` service data and `(expected_mid, low, high)`."""
    pending_cmd = _get_active_pending_commands(entry).get(zone_id)
    explicit_non_temp_mode = effective_mode in (
        HVACMode.OFF,
        HVACMode.FAN_ONLY,
    ) and (pending_cmd is not None and pending_cmd.system_mode == effective_mode)
    if not math.isfinite(target_temp) or explicit_non_temp_mode:
        return None, None, None, None

    attrs = state.attributes if state is not None else {}
    supported_features = _parse_int(attrs.get(ATTR_SUPPORTED_FEATURES), 0)
    raw_single = _parse_optional_float(attrs.get(ATTR_TEMPERATURE))
    raw_low = _parse_optional_float(attrs.get(ATTR_TARGET_TEMP_LOW))
    raw_high = _parse_optional_float(attrs.get(ATTR_TARGET_TEMP_HIGH))
    state_mode = (
        _DIRECT_MODE_MAP.get(str(state.state).strip().lower(), "")
        if state is not None
        else ""
    )
    if not _can_set_temperature(
        supported_features=supported_features,
        system_mode=effective_mode if state_mode == effective_mode else "heat",
        raw_single=raw_single,
        raw_low=raw_low,
        raw_high=raw_high,
    ):
        return None, None, None, None

    if effective_mode in (HVACMode.OFF, HVACMode.FAN_ONLY) and (
        raw_single is None
        and not (supported_features & ClimateEntityFeature.TARGET_TEMPERATURE)
    ):
        return None, None, None, None

    if not _is_range_mode(
        supported_features=supported_features,
        effective_mode=effective_mode,
        raw_single=raw_single,
        raw_low=raw_low,
        raw_high=raw_high,
    ):
        return (
            {ATTR_ENTITY_ID: zone_id, ATTR_TEMPERATURE: target_temp},
            target_temp,
            None,
            None,
        )

    raw_min = _parse_float(attrs.get(ATTR_MIN_TEMP), _DEFAULT_MIN_TEMP)
    raw_max = _parse_float(attrs.get(ATTR_MAX_TEMP), _DEFAULT_MAX_TEMP)
    step = _parse_float(attrs.get(ATTR_TARGET_TEMP_STEP), _DEFAULT_STEP)
    if step <= 0:
        step = _DEFAULT_STEP
    min_grid, max_grid = _compute_grid_bounds(raw_min, raw_max, step)

    span = min(
        _resolve_command_span(entry, zone_id, raw_low, raw_high, step),
        max(0.0, max_grid - min_grid),
    )
    low = _half_down_to_step(target_temp - span / 2.0, step)
    low = min(max(low, min_grid), max(min_grid, max_grid - span))
    base_high = min(max_grid, _half_up_to_step(low + span, step))
    base_mid = _half_up_to_step((low + base_high) / 2.0, _DEFAULT_STEP)
    mirror_high = _half_up_to_step(2.0 * target_temp - low, step)
    if (
        not math.isclose(base_mid, target_temp, abs_tol=_FLOAT_TOL)
        and mirror_high > base_high
        and mirror_high <= max_grid
        and math.isclose((low + mirror_high) / 2.0, target_temp, abs_tol=_FLOAT_TOL)
    ):
        high = mirror_high
    else:
        high = base_high
    high = max(high, low)

    expected_midpoint = _half_up_to_step((low + high) / 2.0, _DEFAULT_STEP)
    LOGGER.debug(
        "Range setpoint %s (%s): target=%.2f -> low=%.2f, high=%.2f (mid=%.2f)",
        zone_id,
        effective_mode,
        target_temp,
        low,
        high,
        expected_midpoint,
    )
    return (
        {
            ATTR_ENTITY_ID: zone_id,
            ATTR_TARGET_TEMP_LOW: round(low, 2),
            ATTR_TARGET_TEMP_HIGH: round(high, 2),
        },
        expected_midpoint,
        round(low, 2),
        round(high, 2),
    )


def _refresh_pending_temp_baselines(
    pending: PendingZoneCommand,
    state: Any,
    *,
    is_range: bool,
) -> None:
    """Refresh `prev_*` temperature baselines from `state` once mode is active."""
    if state is None:
        return
    attrs = state.attributes
    if is_range:
        cur_low = _parse_optional_float(attrs.get(ATTR_TARGET_TEMP_LOW))
        cur_high = _parse_optional_float(attrs.get(ATTR_TARGET_TEMP_HIGH))
        if cur_low is not None:
            pending.prev_lows.add(cur_low)
        if cur_high is not None:
            pending.prev_highs.add(cur_high)
        if cur_low is not None and cur_high is not None:
            pending.prev_targets.add(
                _half_up_to_step((cur_low + cur_high) / 2.0, _DEFAULT_STEP)
            )
    else:
        cur_single = _parse_optional_float(attrs.get(ATTR_TEMPERATURE))
        if cur_single is not None:
            pending.prev_targets.add(cur_single)


def _commit_accepted_range(
    entry: TewkeConfigEntry,
    zone_id: str,
    low: float,
    high: float,
) -> None:
    """Record `(low, high)` as the last accepted Tap-commanded range."""
    cmd_ranges = _get_commanded_ranges(entry)
    if cmd_ranges is not None:
        cmd_ranges[zone_id] = (low, high)
    deadband_spans = _get_deadband_spans(entry)
    if deadband_spans is not None:
        deadband_spans[f"__cmd_low__:{zone_id}"] = low
        deadband_spans[f"__cmd_high__:{zone_id}"] = high


def _reconcile_partial_failed_range(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
    zone_id: str,
    attempted_low: float,
    attempted_high: float,
) -> None:
    """Update accepted range per leg if a failed call partially mutated state."""
    cmd_ranges = _get_commanded_ranges(entry)
    prev_accepted = cmd_ranges.get(zone_id) if cmd_ranges is not None else None
    state = hass.states.get(zone_id)
    if state is None or prev_accepted is None:
        return
    cur_low = _parse_optional_float(state.attributes.get(ATTR_TARGET_TEMP_LOW))
    cur_high = _parse_optional_float(state.attributes.get(ATTR_TARGET_TEMP_HIGH))
    next_low = (
        attempted_low if _matches_any(cur_low, (attempted_low,)) else prev_accepted[0]
    )
    next_high = (
        attempted_high
        if _matches_any(cur_high, (attempted_high,))
        else prev_accepted[1]
    )
    _commit_accepted_range(entry, zone_id, next_low, next_high)


async def _dispatch_target_temperature(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
    zone_id: str,
    target_temperature: float,
    *,
    requested_mode: str | None = None,
) -> None:
    """Resolve and execute (or defer) `climate.set_temperature` for `zone_id`."""
    state = hass.states.get(zone_id)
    pending_cmd = _get_active_pending_commands(entry).get(zone_id)
    state_mode = (
        _DIRECT_MODE_MAP.get(str(state.state).strip().lower(), "")
        if state is not None
        else ""
    )
    effective_mode = (
        requested_mode
        or (pending_cmd.system_mode if pending_cmd is not None else None)
        or (state_mode or "heat")
    )
    temp_payload, expected_mid, exp_low, exp_high = _build_set_temperature_payload(
        entry,
        zone_id,
        target_temperature,
        effective_mode=effective_mode,
        state=state,
    )
    runtime_data = getattr(entry, "runtime_data", None)
    pending_map = getattr(runtime_data, "pending_heating_commands", None)
    pending = pending_map.get(zone_id) if isinstance(pending_map, dict) else None
    if pending is not None:
        if temp_payload is None:
            _clear_pending_temp(pending)
            return
        pending.expected_midpoint = expected_mid
        pending.expected_low = exp_low
        pending.expected_high = exp_high
        if exp_low is not None and state is not None and state_mode != effective_mode:
            pending.deferred_temp_mode = effective_mode
            return
        pending.deferred_temp_mode = None
        if state_mode == effective_mode:
            _refresh_pending_temp_baselines(
                pending, state, is_range=exp_low is not None
            )

    if temp_payload is not None:
        accepted = await _async_call_climate_service(
            hass, SERVICE_SET_TEMPERATURE, temp_payload
        )
        if accepted:
            if exp_low is not None and exp_high is not None:
                _commit_accepted_range(entry, zone_id, exp_low, exp_high)
        else:
            if exp_low is not None and exp_high is not None:
                _reconcile_partial_failed_range(hass, entry, zone_id, exp_low, exp_high)
            if pending is not None:
                _clear_pending_temp(pending)


async def _dispatch_auxiliary_modes(
    hass: HomeAssistant,
    command: HeatingCommandData,
    recorded: PendingZoneCommand | None,
) -> None:
    """Execute preset, fan, and swing service calls and clear rejected masks."""
    if command.preset_mode is not None:
        accepted = await _async_call_climate_service(
            hass,
            SERVICE_SET_PRESET_MODE,
            {
                ATTR_ENTITY_ID: command.zone_id,
                ATTR_PRESET_MODE: command.preset_mode,
            },
        )
        if not accepted and recorded is not None:
            recorded.preset_mode = None
            recorded.prev_preset_modes.clear()
    if command.fan_mode is not None:
        accepted = await _async_call_climate_service(
            hass,
            SERVICE_SET_FAN_MODE,
            {
                ATTR_ENTITY_ID: command.zone_id,
                ATTR_FAN_MODE: command.fan_mode,
            },
        )
        if not accepted and recorded is not None:
            recorded.fan_mode = None
            recorded.prev_fan_modes.clear()
    if command.swing_mode is not None:
        accepted = await _async_call_climate_service(
            hass,
            SERVICE_SET_SWING_MODE,
            {
                ATTR_ENTITY_ID: command.zone_id,
                ATTR_SWING_MODE: command.swing_mode,
            },
        )
        if not accepted and recorded is not None:
            recorded.swing_mode = None
            recorded.prev_swing_modes.clear()


async def _flush_ready_deferred_temp(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
    zone_id: str,
    recorded: PendingZoneCommand | None,
) -> None:
    """Flush `deferred_temp_mode` if HA entered that mode during auxiliary calls."""
    if (
        recorded is None
        or recorded.deferred_temp_mode is None
        or recorded.target_temperature is None
    ):
        return
    state = hass.states.get(zone_id)
    state_mode = (
        _DIRECT_MODE_MAP.get(str(state.state).strip().lower(), "")
        if state is not None
        else ""
    )
    if state_mode == recorded.deferred_temp_mode:
        await _dispatch_target_temperature(
            hass,
            entry,
            zone_id,
            recorded.target_temperature,
            requested_mode=state_mode,
        )


async def _execute_zone_command(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
    command: HeatingCommandData,
) -> None:
    """Execute the service sequence for a single `HeatingCommandData`."""
    runtime_data = getattr(entry, "runtime_data", None)
    recorded = _record_pending_command(hass, entry, command)
    if runtime_data is not None and hasattr(runtime_data, "heating_commands_in_flight"):
        runtime_data.heating_commands_in_flight += 1

    try:
        resolved_req_mode = command.system_mode
        skip_temp = False
        if command.system_mode is not None:
            accepted = await _async_call_climate_service(
                hass,
                SERVICE_SET_HVAC_MODE,
                {
                    ATTR_ENTITY_ID: command.zone_id,
                    ATTR_HVAC_MODE: command.system_mode,
                },
            )
            if not accepted:
                if recorded is not None:
                    recorded.system_mode = None
                    recorded.prev_system_mode = None
                live_state = hass.states.get(command.zone_id)
                live_mode = (
                    _DIRECT_MODE_MAP.get(str(live_state.state).strip().lower(), "")
                    if live_state is not None
                    else ""
                )
                if live_mode == command.system_mode:
                    resolved_req_mode = live_mode
                else:
                    if recorded is not None:
                        _clear_pending_temp(recorded)
                    skip_temp = True
        if command.target_temperature is not None and not skip_temp:
            await _dispatch_target_temperature(
                hass,
                entry,
                command.zone_id,
                command.target_temperature,
                requested_mode=resolved_req_mode,
            )
        await _dispatch_auxiliary_modes(hass, command, recorded)
        await _flush_ready_deferred_temp(hass, entry, command.zone_id, recorded)
    finally:
        pending_map = getattr(runtime_data, "pending_heating_commands", None)
        if recorded is not None:
            recorded.in_flight_count = max(0, recorded.in_flight_count - 1)
            recorded.expires_at = time.monotonic() + _COMMAND_GRACE_SECS
            if isinstance(pending_map, dict) and not _is_pending_empty(recorded):
                pending_map[command.zone_id] = recorded
        if runtime_data is not None and hasattr(
            runtime_data, "heating_commands_in_flight"
        ):
            runtime_data.heating_commands_in_flight = max(
                0, runtime_data.heating_commands_in_flight - 1
            )
        no_in_flight = getattr(runtime_data, "heating_commands_in_flight", 0) == 0
        _schedule_reconcile_push(
            hass,
            entry,
            force_immediate_if_empty=no_in_flight,
        )


async def async_handle_heating_command(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
    command: HeatingCommandData,
) -> None:
    """Execute HA climate service calls in response to a Tap heating command."""
    climate_entities = get_configured_climate_entities(entry)
    if command.zone_id not in climate_entities:
        LOGGER.warning(
            "Ignoring heating command for unconfigured entity %s",
            command.zone_id,
        )
        return

    zone_lock = _get_zone_lock(entry, command.zone_id)
    if zone_lock is not None:
        async with zone_lock:
            await _execute_zone_command(hass, entry, command)
    else:
        await _execute_zone_command(hass, entry, command)


async def _async_apply_deferred_or_wake_retry(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
    entity_id: str,
    *,
    wake_retry_mode: str | None,
) -> None:
    """Execute wake-up mode retry or deferred range setpoint, then push state."""
    zone_lock = _get_zone_lock(entry, entity_id)
    if zone_lock is not None:
        async with zone_lock:
            await _run_locked_state_followup(
                hass,
                entry,
                entity_id,
                wake_retry_mode=wake_retry_mode,
            )
    else:
        await _run_locked_state_followup(
            hass,
            entry,
            entity_id,
            wake_retry_mode=wake_retry_mode,
        )
    await async_push_heating_state(hass, entry)


async def _run_locked_state_followup(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
    entity_id: str,
    *,
    wake_retry_mode: str | None,
) -> None:
    """Run serialised follow-up service calls if still current."""
    pending = _get_active_pending_commands(entry).get(entity_id)
    if pending is None:
        return
    pending.in_flight_count += 1
    try:
        if wake_retry_mode is not None and pending.system_mode == wake_retry_mode:
            accepted = await _async_call_climate_service(
                hass,
                SERVICE_SET_HVAC_MODE,
                {
                    ATTR_ENTITY_ID: entity_id,
                    ATTR_HVAC_MODE: wake_retry_mode,
                },
            )
            if not accepted:
                pending.system_mode = None
                pending.prev_system_mode = None
        if (
            pending.deferred_temp_mode is not None
            and pending.target_temperature is not None
        ):
            state = hass.states.get(entity_id)
            state_mode = (
                _DIRECT_MODE_MAP.get(str(state.state).strip().lower(), "")
                if state is not None
                else ""
            )
            if state_mode == pending.deferred_temp_mode:
                await _dispatch_target_temperature(
                    hass,
                    entry,
                    entity_id,
                    pending.target_temperature,
                    requested_mode=state_mode,
                )
    finally:
        pending.in_flight_count = max(0, pending.in_flight_count - 1)
        pending.expires_at = time.monotonic() + _COMMAND_GRACE_SECS
        _schedule_reconcile_push(hass, entry)


def cancel_climate_state_listener(entry: TewkeConfigEntry) -> None:
    """Unsubscribe any active HA climate state listener for the config entry."""
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        return
    unsub: Callable[[], None] | None = getattr(runtime_data, "climate_unsub", None)
    if unsub is not None:
        unsub()
        runtime_data.climate_unsub = None
    reconcile_unsub: Callable[[], None] | None = getattr(
        runtime_data, "heating_reconcile_unsub", None
    )
    if reconcile_unsub is not None:
        reconcile_unsub()
        runtime_data.heating_reconcile_unsub = None


@callback
def async_setup_climate_state_listener(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
) -> None:
    """Subscribe to configured HA climate entities and sync state to the Tap."""
    cancel_climate_state_listener(entry)
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        return

    tap = getattr(runtime_data, "tap", None)
    if tap is None or not tap_supports_heating(tap):
        return

    climate_entities = get_configured_climate_entities(entry)
    if not climate_entities:
        climate_entities = _adopt_tap_zones_if_unconfigured(hass, entry)

    hass.async_create_task(async_push_heating_state(hass, entry))

    if not climate_entities:
        return

    @callback
    def _async_on_climate_state_change(
        event: Event[EventStateChangedData],
    ) -> None:
        entity_id = event.data.get("entity_id")
        if not isinstance(entity_id, str):
            return
        pending = _get_active_pending_commands(entry).get(entity_id)
        if pending is not None and pending.in_flight_count > 0:
            return
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        wake_retry_mode: str | None = None
        apply_deferred = False
        if pending is not None and new_state is not None:
            new_mapped = _DIRECT_MODE_MAP.get(str(new_state.state).strip().lower())
            if (
                pending.system_mode not in (None, "off")
                and old_state is not None
                and old_state.state in (HVACMode.OFF, STATE_UNAVAILABLE, STATE_UNKNOWN)
                and new_state.state
                not in (
                    pending.system_mode,
                    HVACMode.OFF,
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                )
            ):
                wake_retry_mode = pending.system_mode
                attrs = new_state.attributes
                hvac_action = str(attrs.get(ATTR_HVAC_ACTION) or "").strip().lower()
                pending.prev_system_mode = _resolve_system_mode(
                    new_state.state,
                    actively_cooling=hvac_action == HVACAction.COOLING,
                    actively_heating=hvac_action == HVACAction.HEATING,
                    system_modes=_resolve_system_modes(attrs.get(ATTR_HVAC_MODES)),
                )
            if (
                pending.deferred_temp_mode is not None
                and new_mapped == pending.deferred_temp_mode
            ):
                apply_deferred = True

        if wake_retry_mode is not None or apply_deferred:
            hass.async_create_task(
                _async_apply_deferred_or_wake_retry(
                    hass,
                    entry,
                    entity_id,
                    wake_retry_mode=wake_retry_mode,
                )
            )
            return
        hass.async_create_task(async_push_heating_state(hass, entry))

    entry.runtime_data.climate_unsub = async_track_state_change_event(
        hass,
        climate_entities,
        _async_on_climate_state_change,
    )
