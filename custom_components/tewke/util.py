"""Utilities for the Tewke integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import CONF_DEFAULT_SCENE_FAN_DIMMING

if TYPE_CHECKING:
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
