"""Home Assistant climate entity bridge for the Tewke Tap Heating Screen."""

from __future__ import annotations

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
from homeassistant.helpers.event import async_track_state_change_event
from pytewke.data import HeatingCommandData, HeatingData, HeatingZoneData
from pytewke.error import BasePyTewkeError

from .const import (
    CONF_CLIMATE_ENTITIES,
    CONF_PRIMARY_CLIMATE_ENTITY,
    LOGGER,
)

if TYPE_CHECKING:
    from .data import TewkeConfigEntry

_DEFAULT_TEMP = 20.0
_DEFAULT_MIN_TEMP = 5.0
_DEFAULT_MAX_TEMP = 35.0
_SUPPORTED_SYSTEM_MODES: tuple[str, ...] = (
    HVACMode.HEAT,
    HVACMode.COOL,
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
    return str(raw) if isinstance(raw, str) and raw else None


def _parse_str_list(raw: Any) -> list[str]:
    """Return a list of non-empty strings from a raw attribute list."""
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item]


def _resolve_system_modes(raw_hvac_modes: Any) -> list[str]:
    """Filter HA hvac_modes to supported Tewke system modes."""
    if not isinstance(raw_hvac_modes, list):
        return list(_DEFAULT_SYSTEM_MODES)
    modes: list[str] = []
    for mode in raw_hvac_modes:
        mode_str = str(mode)
        if mode_str in _SUPPORTED_SYSTEM_MODES and mode_str not in modes:
            modes.append(mode_str)
    return modes or list(_DEFAULT_SYSTEM_MODES)


def _resolve_system_mode(
    state_str: str,
    *,
    actively_cooling: bool,
    system_modes: list[str],
) -> Literal["heat", "cool", "off"]:
    """Map an HA climate state string to a Tewke system mode."""
    if state_str == HVACMode.OFF:
        return "off"
    if state_str == HVACMode.COOL:
        return "cool"
    if state_str != HVACMode.HEAT and (
        actively_cooling
        or (HVACMode.COOL in system_modes and HVACMode.HEAT not in system_modes)
    ):
        return "cool"
    return "heat"


def _build_zone_from_state(
    entity_id: str,
    state: Any,
    *,
    is_this_room: bool,
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

    hvac_action = attrs.get(ATTR_HVAC_ACTION)
    actively_heating = bool(hvac_action == HVACAction.HEATING)
    actively_cooling = bool(hvac_action == HVACAction.COOLING)
    system_mode = _resolve_system_mode(
        state.state,
        actively_cooling=actively_cooling,
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

    return HeatingZoneData(
        id=entity_id,
        name=name,
        current_temperature=current_temp,
        target_temperature=target_temp,
        system_mode=system_mode,
        actively_heating=actively_heating,
        actively_cooling=actively_cooling,
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


def build_heating_zones(
    hass: HomeAssistant,
    entry: TewkeConfigEntry,
) -> dict[str, HeatingZoneData]:
    """Build `HeatingZoneData` objects from current Home Assistant climate states."""
    climate_entities = get_configured_climate_entities(entry)
    if not climate_entities:
        return {}

    primary_entity = _resolve_primary_entity(hass, entry, climate_entities)
    return {
        entity_id: _build_zone_from_state(
            entity_id,
            hass.states.get(entity_id),
            is_this_room=entity_id == primary_entity,
        )
        for entity_id in climate_entities
    }


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

    try:
        if command.system_mode is not None:
            await hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_HVAC_MODE,
                {
                    ATTR_ENTITY_ID: command.zone_id,
                    ATTR_HVAC_MODE: command.system_mode,
                },
                blocking=True,
            )
        if command.target_temperature is not None:
            await hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_TEMPERATURE,
                {
                    ATTR_ENTITY_ID: command.zone_id,
                    ATTR_TEMPERATURE: command.target_temperature,
                },
                blocking=True,
            )
        if command.preset_mode is not None:
            await hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_PRESET_MODE,
                {
                    ATTR_ENTITY_ID: command.zone_id,
                    ATTR_PRESET_MODE: command.preset_mode,
                },
                blocking=True,
            )
        if command.fan_mode is not None:
            await hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_FAN_MODE,
                {
                    ATTR_ENTITY_ID: command.zone_id,
                    ATTR_FAN_MODE: command.fan_mode,
                },
                blocking=True,
            )
    except Exception:  # noqa: BLE001
        LOGGER.exception(
            "Failed to apply Tap heating command for %s",
            command.zone_id,
        )


def cancel_climate_state_listener(entry: TewkeConfigEntry) -> None:
    """Unsubscribe any active HA climate state listener for the config entry."""
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        return
    unsub: Callable[[], None] | None = getattr(runtime_data, "climate_unsub", None)
    if unsub is not None:
        unsub()
        runtime_data.climate_unsub = None


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
    hass.async_create_task(async_push_heating_state(hass, entry))

    if not climate_entities:
        return

    @callback
    def _async_on_climate_state_change(
        _event: Event[EventStateChangedData],
    ) -> None:
        hass.async_create_task(async_push_heating_state(hass, entry))

    entry.runtime_data.climate_unsub = async_track_state_change_event(
        hass,
        climate_entities,
        _async_on_climate_state_change,
    )
