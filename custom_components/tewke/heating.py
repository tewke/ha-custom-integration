"""Home Assistant climate entity bridge for the Tewke Tap Heating Screen."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

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
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from pytewke.data import HeatingCommandData, HeatingData, HeatingZoneData
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
_COMMAND_GRACE_SECS = 15.0
_SUPPORTED_SYSTEM_MODES: tuple[str, ...] = (
    HVACMode.HEAT,
    HVACMode.COOL,
    HVACMode.DRY,
    HVACMode.FAN_ONLY,
    HVACMode.OFF,
)
_DEFAULT_SYSTEM_MODES: tuple[str, ...] = (HVACMode.HEAT, HVACMode.OFF)


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
    entities = (
        [str(entity_id) for entity_id in raw if isinstance(entity_id, str)]
        if isinstance(raw, list)
        else []
    )
    primary = get_configured_primary_climate_entity(entry)
    if primary and primary not in entities:
        entities.insert(0, primary)
    return entities


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
    """Convert raw attribute value to float, falling back to default."""
    if raw is None:
        return default
    try:
        return float(raw)
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


_DIRECT_MODE_MAP: dict[str, Literal["heat", "cool", "dry", "fan_only", "off"]] = {
    HVACMode.OFF: "off",
    HVACMode.COOL: "cool",
    "cooling": "cool",
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
) -> Literal["heat", "cool", "dry", "fan_only", "off"]:
    """Map an HA climate state string to a Tewke system mode."""
    direct = _DIRECT_MODE_MAP.get(str(state_str).strip().lower())
    if direct is not None:
        return direct
    if actively_cooling:
        return "cool"
    if actively_heating:
        return "heat"
    for candidate in ("heat", "cool", "dry", "fan_only"):
        if candidate in system_modes:
            return candidate
    return "heat"


def _apply_pending_command(
    zone: HeatingZoneData,
    pending: PendingZoneCommand | None,
) -> HeatingZoneData:
    """Overlay unexpired Tap command values if HA state has not caught up yet."""
    if pending is None:
        return zone

    if pending.system_mode is not None:
        if zone.system_mode == pending.system_mode:
            pending.system_mode = None
        elif pending.prev_system_mode in (zone.system_mode, "off"):
            zone.system_mode = pending.system_mode  # type: ignore[assignment]
        else:
            pending.system_mode = None

    if (
        pending.target_temperature is not None
        and zone.target_temperature == pending.prev_target_temperature
    ):
        zone.target_temperature = pending.target_temperature
    else:
        pending.target_temperature = None

    if pending.preset_mode is not None and zone.preset_mode == pending.prev_preset_mode:
        zone.preset_mode = pending.preset_mode
    else:
        pending.preset_mode = None

    if pending.fan_mode is not None and zone.fan_mode == pending.prev_fan_mode:
        zone.fan_mode = pending.fan_mode
    else:
        pending.fan_mode = None

    return zone


def _build_zone_from_state(
    entity_id: str,
    state: Any,
    *,
    is_this_room: bool,
    pending: PendingZoneCommand | None = None,
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
    system_mode = _resolve_system_mode(
        state.state,
        actively_cooling=raw_cooling,
        actively_heating=raw_heating,
        system_modes=system_modes,
    )

    raw_target = attrs.get(ATTR_TEMPERATURE)
    if raw_target is None:
        raw_target = (
            attrs.get(ATTR_TARGET_TEMP_HIGH)
            if system_mode == "cool"
            else attrs.get(ATTR_TARGET_TEMP_LOW)
        )
    raw_current = attrs.get(ATTR_CURRENT_TEMPERATURE)
    current_temp = _parse_float(
        raw_current if raw_current is not None else raw_target,
        _DEFAULT_TEMP,
    )
    target_temp = _parse_float(raw_target, current_temp)

    zone = HeatingZoneData(
        id=entity_id,
        name=name,
        current_temperature=current_temp,
        target_temperature=target_temp,
        system_mode=system_mode,
        actively_heating=raw_heating and system_mode == "heat",
        actively_cooling=raw_cooling and system_mode == "cool",
        online=online,
        is_this_room=is_this_room,
        min_temp=_parse_float(attrs.get(ATTR_MIN_TEMP), _DEFAULT_MIN_TEMP),
        max_temp=_parse_float(attrs.get(ATTR_MAX_TEMP), _DEFAULT_MAX_TEMP),
        system_modes=system_modes,
        preset_mode=_parse_non_empty_str(attrs.get(ATTR_PRESET_MODE)),
        preset_modes=_parse_str_list(attrs.get(ATTR_PRESET_MODES)),
        fan_mode=_parse_non_empty_str(attrs.get(ATTR_FAN_MODE)),
        fan_modes=_parse_str_list(attrs.get(ATTR_FAN_MODES)),
    )
    resolved = _apply_pending_command(zone, pending)
    if resolved.system_mode != "heat":
        resolved.actively_heating = False
    if resolved.system_mode != "cool":
        resolved.actively_cooling = False
    LOGGER.debug(
        "Built zone %s: ha_state=%r -> system_mode=%r (modes=%s, fan=%r)",
        entity_id,
        state.state,
        resolved.system_mode,
        resolved.system_modes,
        resolved.fan_mode,
    )
    return resolved


def _get_active_pending_commands(
    entry: TewkeConfigEntry,
) -> dict[str, PendingZoneCommand]:
    """Return unexpired pending Tap commands for the entry."""
    runtime_data = getattr(entry, "runtime_data", None)
    raw = getattr(runtime_data, "pending_heating_commands", None)
    if not isinstance(raw, dict):
        return {}
    now = time.monotonic()
    expired = [zid for zid, cmd in raw.items() if cmd.expires_at <= now]
    for zid in expired:
        raw.pop(zid, None)
    return raw


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
    return {
        entity_id: _build_zone_from_state(
            entity_id,
            hass.states.get(entity_id),
            is_this_room=entity_id == primary_entity,
            pending=pending_map.get(entity_id),
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


def _record_pending_command(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
    command: HeatingCommandData,
) -> None:
    """Record a Tap command so stale HA states do not echo back during execution."""
    runtime_data = getattr(entry, "runtime_data", None)
    pending_map = getattr(runtime_data, "pending_heating_commands", None)
    if not isinstance(pending_map, dict):
        return

    current_zone = _build_zone_from_state(
        command.zone_id,
        hass.states.get(command.zone_id),
        is_this_room=False,
    )
    existing = pending_map.get(command.zone_id)
    pending_map[command.zone_id] = PendingZoneCommand(
        expires_at=time.monotonic() + _COMMAND_GRACE_SECS,
        system_mode=command.system_mode
        if command.system_mode is not None
        else (existing.system_mode if existing else None),
        prev_system_mode=current_zone.system_mode
        if command.system_mode is not None
        else (existing.prev_system_mode if existing else None),
        target_temperature=command.target_temperature
        if command.target_temperature is not None
        else (existing.target_temperature if existing else None),
        prev_target_temperature=current_zone.target_temperature
        if command.target_temperature is not None
        else (existing.prev_target_temperature if existing else None),
        preset_mode=command.preset_mode
        if command.preset_mode is not None
        else (existing.preset_mode if existing else None),
        prev_preset_mode=current_zone.preset_mode
        if command.preset_mode is not None
        else (existing.prev_preset_mode if existing else None),
        fan_mode=command.fan_mode
        if command.fan_mode is not None
        else (existing.fan_mode if existing else None),
        prev_fan_mode=current_zone.fan_mode
        if command.fan_mode is not None
        else (existing.prev_fan_mode if existing else None),
    )


def _schedule_reconcile_push(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
) -> None:
    """Schedule a post-command reconciliation push once the grace period ends."""
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        return
    prev_unsub: Callable[[], None] | None = getattr(
        runtime_data, "heating_reconcile_unsub", None
    )
    if prev_unsub is not None:
        prev_unsub()
        runtime_data.heating_reconcile_unsub = None

    if not hasattr(hass, "loop"):
        return

    @callback
    def _on_reconcile(_now: datetime) -> None:
        runtime_data.heating_reconcile_unsub = None
        hass.async_create_task(async_push_heating_state(hass, entry))

    try:
        runtime_data.heating_reconcile_unsub = async_call_later(
            hass, _COMMAND_GRACE_SECS + 0.1, _on_reconcile
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
) -> None:
    """Call a single Home Assistant climate service, logging any error."""
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

    runtime_data = getattr(entry, "runtime_data", None)
    _record_pending_command(hass, entry, command)
    if runtime_data is not None and hasattr(runtime_data, "heating_commands_in_flight"):
        runtime_data.heating_commands_in_flight += 1

    try:
        if command.system_mode is not None:
            await _async_call_climate_service(
                hass,
                SERVICE_SET_HVAC_MODE,
                {
                    ATTR_ENTITY_ID: command.zone_id,
                    ATTR_HVAC_MODE: command.system_mode,
                },
            )
        if command.target_temperature is not None and command.system_mode not in (
            HVACMode.OFF,
            HVACMode.FAN_ONLY,
        ):
            await _async_call_climate_service(
                hass,
                SERVICE_SET_TEMPERATURE,
                {
                    ATTR_ENTITY_ID: command.zone_id,
                    ATTR_TEMPERATURE: command.target_temperature,
                },
            )
        if command.preset_mode is not None:
            await _async_call_climate_service(
                hass,
                SERVICE_SET_PRESET_MODE,
                {
                    ATTR_ENTITY_ID: command.zone_id,
                    ATTR_PRESET_MODE: command.preset_mode,
                },
            )
        if command.fan_mode is not None:
            await _async_call_climate_service(
                hass,
                SERVICE_SET_FAN_MODE,
                {
                    ATTR_ENTITY_ID: command.zone_id,
                    ATTR_FAN_MODE: command.fan_mode,
                },
            )
    finally:
        pending_map = getattr(runtime_data, "pending_heating_commands", None)
        if isinstance(pending_map, dict) and command.zone_id in pending_map:
            pending_map[command.zone_id].expires_at = (
                time.monotonic() + _COMMAND_GRACE_SECS
            )
        if runtime_data is not None and hasattr(
            runtime_data, "heating_commands_in_flight"
        ):
            runtime_data.heating_commands_in_flight = max(
                0, runtime_data.heating_commands_in_flight - 1
            )
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
        if getattr(runtime_data, "heating_commands_in_flight", 0) > 0:
            return
        entity_id = event.data.get("entity_id")
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        pending_map = _get_active_pending_commands(entry)
        pending = pending_map.get(entity_id) if isinstance(entity_id, str) else None
        if (
            pending is not None
            and pending.system_mode not in (None, "off")
            and old_state is not None
            and old_state.state in (HVACMode.OFF, STATE_UNAVAILABLE, STATE_UNKNOWN)
            and new_state is not None
            and new_state.state
            not in (
                pending.system_mode,
                HVACMode.OFF,
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            )
        ):
            target_mode = pending.system_mode
            pending.prev_system_mode = _DIRECT_MODE_MAP.get(
                str(new_state.state).strip().lower(), "cool"
            )
            hass.async_create_task(
                _async_call_climate_service(
                    hass,
                    SERVICE_SET_HVAC_MODE,
                    {
                        ATTR_ENTITY_ID: entity_id,
                        ATTR_HVAC_MODE: target_mode,
                    },
                )
            )
        hass.async_create_task(async_push_heating_state(hass, entry))

    entry.runtime_data.climate_unsub = async_track_state_change_event(
        hass,
        climate_entities,
        _async_on_climate_state_change,
    )
