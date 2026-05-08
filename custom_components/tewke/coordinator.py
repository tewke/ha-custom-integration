"""DataUpdateCoordinator for the Tewke integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, TypedDict

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pytewke.error import (
    PyTewkeCoapError,
    PyTewkeInvalidResponseError,
    PyTewkeUnknownError,
)

from .const import LOGGER
from .util import async_setup_observe

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable

    from homeassistant.core import HomeAssistant
    from pytewke.data import (
        ConfigData,
        EnergyData,
        RadarData,
        Scene,
        SensorData,
        Target,
    )

    from .data import TewkeConfigEntry

# Transient errors that are worth retrying (device/network busy or timed out).
# PyTewkeInvalidResponseError is intentionally excluded — it means the device
# returned malformed data, which retrying is unlikely to fix.
_RETRYABLE_ERRORS = (PyTewkeCoapError, PyTewkeUnknownError, TimeoutError)

# Delay sequence (seconds) between successive retry attempts.
_RETRY_DELAYS: list[float] = [1.0, 2.0, 4.0]

# Fallback polling interval.  The coordinator is primarily push-based; this
# acts as a safety-net so that if observations go silent the coordinator can
# still recover without requiring a restart.
_RECOVERY_INTERVAL = timedelta(minutes=5)


async def _fetch_with_retries[T](fn: Callable[[], Awaitable[T]]) -> T:
    """
    Await *fn()*, retrying on transient errors with exponential back-off.

    Raises the original exception if all attempts are exhausted, or immediately
    if the error is not considered transient.
    """
    last_err: Exception | None = None
    for attempt, delay in enumerate(_RETRY_DELAYS):
        try:
            return await fn()
        except _RETRYABLE_ERRORS as err:
            last_err = err
            LOGGER.debug(
                "Transient Tap error (attempt %d/%d), retrying in %.0fs: %s",
                attempt + 1,
                len(_RETRY_DELAYS) + 1,
                delay,
                err,
            )
            await asyncio.sleep(delay)
        except PyTewkeInvalidResponseError, Exception:
            # Non-transient or unexpected error — fail immediately.
            raise
    raise last_err


class TewkeCoordinatorData(TypedDict):
    """Typed data held by TewkeCoordinator."""

    scenes: dict[str, Scene]
    scenes_all: dict[str, Scene]
    targets: dict[int, Target]
    sensors: SensorData | None
    radar: RadarData | None
    energy: EnergyData | None
    config: ConfigData | None


class TewkeCoordinator(DataUpdateCoordinator[TewkeCoordinatorData]):
    """
    Coordinator for all Tewke state (scenes, targets, sensors).

    Updates are primarily push-based via CoAP observation callbacks registered
    in `async_setup_entry`.  `_async_update_data` runs at initial setup and
    whenever an entity service call triggers `async_request_refresh`.  A
    periodic fallback interval (`_RECOVERY_INTERVAL`) ensures the coordinator
    can self-heal if observations go silent.

    See TewkeCoordinatorData for the full shape of the coordinator data.
    """

    config_entry: TewkeConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        name: str,
    ) -> None:
        """Initialise coordinator with a fallback recovery interval."""
        super().__init__(
            hass=hass,
            logger=logger,
            name=name,
            update_interval=_RECOVERY_INTERVAL,
        )

    async def _setup_observe(self) -> None:
        def handle_timeout() -> None:
            self.config_entry.runtime_data.observe_active = False
            self.hass.async_create_task(self._setup_observe())

        if not self.config_entry.runtime_data.observe_active:
            LOGGER.info(
                "CoAP observations not active for %s; attempting to re-establish",
                self.config_entry.entry_id,
            )
            await async_setup_observe(
                self,
                self.hass,
                self.config_entry,
                timeout_callback=handle_timeout,
            )

    async def _async_update_data(self) -> TewkeCoordinatorData:
        """Fetch current state for all resources, retrying on transient errors."""
        tap = self.config_entry.runtime_data.tap
        try:
            scenes_all = await _fetch_with_retries(tap.get_scenes)
            targets = await _fetch_with_retries(tap.get_targets)
        except (
            PyTewkeCoapError,
            PyTewkeInvalidResponseError,
            PyTewkeUnknownError,
            TimeoutError,
        ) as err:
            msg = f"Error communicating with Tewke Tap: {err}"
            raise UpdateFailed(msg) from err

        scene_control_types = self.config_entry.runtime_data.scene_control_types
        configured_scenes = {
            scene_id: scene
            for scene_id, scene in scenes_all.items()
            if scene_id in scene_control_types
        }

        try:
            sensors: SensorData | None = await tap.get_sensors()
        except (
            PyTewkeCoapError,
            PyTewkeInvalidResponseError,
            PyTewkeUnknownError,
            TimeoutError,
        ) as err:
            LOGGER.debug("Sensor data not available from Tewke Tap: %s", err)
            sensors = None

        try:
            radar: RadarData | None = await tap.get_radar()
        except (
            PyTewkeCoapError,
            PyTewkeInvalidResponseError,
            PyTewkeUnknownError,
            TimeoutError,
        ) as err:
            LOGGER.debug("Radar data not available from Tewke Tap: %s", err)
            radar = None

        try:
            energy: EnergyData | None = await tap.get_energy()
        except (
            PyTewkeCoapError,
            PyTewkeInvalidResponseError,
            PyTewkeUnknownError,
            TimeoutError,
        ) as err:
            LOGGER.debug("Energy data not available from Tewke Tap: %s", err)
            energy = None

        try:
            config: ConfigData | None = await tap.get_config()
        except (
            PyTewkeCoapError,
            PyTewkeInvalidResponseError,
            PyTewkeUnknownError,
            TimeoutError,
        ) as err:
            LOGGER.debug("Config data not available from Tewke Tap: %s", err)
            config = None

        return TewkeCoordinatorData(
            scenes=configured_scenes,
            scenes_all=scenes_all,
            targets=targets,
            sensors=sensors,
            radar=radar,
            energy=energy,
            config=config,
        )
