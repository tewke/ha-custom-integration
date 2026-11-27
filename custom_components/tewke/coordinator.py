"""DataUpdateCoordinator for the Tewke integration."""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any, TypedDict

from homeassistant.const import CONF_HOST
from homeassistant.core import HassJob, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pytewke.data import ObservationStatus, ObservationStatusEvent
from pytewke.error import (
    PyTewkeCoapError,
    PyTewkeInvalidResponseError,
    PyTewkeUnknownError,
)

from .const import LOGGER
from .data import ResourceHealthState
from .heating import async_push_heating_state, tap_supports_heating
from .util import async_setup_observe, reconcile_authoritative_scenes

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from pytewke.data import (
        ConfigData,
        EnergyData,
        EnergyOverrideData,
        HeatingData,
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

# Delay sequence (seconds) between successive GET retry attempts.
_RETRY_DELAYS: list[float] = [1.0, 2.0, 4.0]

# Anti-flap availability grace period (5 minutes) and retry schedule constants.
GRACE_PERIOD_SECS = 300.0
GRACE_RETRY_INTERVAL_SECS = 30.0
POST_GRACE_BASE_SECS = 60.0
POST_GRACE_MAX_SECS = 900.0

# Seconds of observation silence while `/sensors` is active before re-observing.
_OBSERVATION_TIMEOUT_SECS = 30.0

# Poll interval for legacy Tap firmware exposing non-observable CoAP resources.
_LEGACY_POLL_INTERVAL_SECS = 60.0

_COAP_URI_HOST_RE = re.compile(r"coap://(?:\[[^\]]+\]|[^/\s:]+)")


def compute_recovery_delay(
    elapsed_unhealthy_secs: float, post_grace_attempts: int
) -> float:
    """
    Compute retry delay (seconds) for an unhealthy CoAP resource.

    Polls every 30s during the initial 5-minute grace window, then backs off
    exponentially (`60s -> 120s -> 240s -> 480s -> 900s`, capped at 15m).
    """
    if elapsed_unhealthy_secs < GRACE_PERIOD_SECS:
        return GRACE_RETRY_INTERVAL_SECS
    return float(
        min(POST_GRACE_MAX_SECS, POST_GRACE_BASE_SECS * (2**post_grace_attempts))
    )


def sanitise_error_text(raw: object, *redactions: str | None) -> str:
    """Strip CoAP host/IP URIs and identifiers from error text for diagnostics."""
    text = _COAP_URI_HOST_RE.sub("coap://**REDACTED**", str(raw))
    for secret in redactions:
        if secret:
            text = text.replace(secret, "**REDACTED**")
    return text


async def _fetch_optional[T](
    fn: Callable[[], Awaitable[T]],
    fallback: T | None,
    label: str,
) -> tuple[T | None, bool]:
    """Fetch an optional resource from the Tap, returning `(value, succeeded)`."""
    try:
        return await fn(), True
    except (
        PyTewkeCoapError,
        PyTewkeInvalidResponseError,
        PyTewkeUnknownError,
        TimeoutError,
    ) as err:
        LOGGER.debug("%s data not available from Tewke Tap: %s", label, err)
        return fallback, False


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
            raise
    if last_err is not None:
        raise last_err
    msg = "Exhausted retry loop without capturing an exception"
    raise RuntimeError(msg)


class TewkeCoordinatorData(TypedDict):
    """Typed data held by TewkeCoordinator."""

    scenes: dict[str, Scene]
    scenes_all: dict[str, Scene]
    targets: dict[int, Target]
    sensors: SensorData | None
    radar: RadarData | None
    energy: EnergyData | None
    energy_override: EnergyOverrideData | None
    config: ConfigData | None
    heating: HeatingData | None


class TewkeCoordinator(DataUpdateCoordinator[TewkeCoordinatorData]):
    """Coordinator owning per-resource CoAP health, 5m grace caching, and retries."""

    config_entry: TewkeConfigEntry

    _monotonic_fn: Callable[[], float] = staticmethod(time.monotonic)
    _call_later_fn: Callable[..., Callable[[], None]] = staticmethod(async_call_later)
    _grace_expiry_unsub: Callable[[], None] | None = None
    _recovery_retry_unsub: Callable[[], None] | None = None
    _observation_timeout_unsub: Callable[[], None] | None = None
    _legacy_poll_unsub: Callable[[], None] | None = None
    _legacy_poll_task: asyncio.Task[None] | None = None
    _retry_tasks: dict[str, asyncio.Task[None]] | None = None
    _prev_available_resources: frozenset[str] | None = None
    _prev_config_room_id: str | None = None
    _seen_initial_config: bool = False
    _is_shutdown: bool = False

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        name: str,
        *,
        call_later_fn: Callable[
            [HomeAssistant, float, HassJob[..., Any] | Callable[..., Any]],
            Callable[[], None],
        ]
        | None = None,
        monotonic_fn: Callable[[], float] | None = None,
    ) -> None:
        """Initialise push-driven coordinator without HA's reset-on-push poll timer."""
        super().__init__(
            hass=hass,
            logger=logger,
            name=name,
            update_interval=None,
        )
        if call_later_fn is not None:
            self._call_later_fn = call_later_fn
        if monotonic_fn is not None:
            self._monotonic_fn = monotonic_fn
        self._observe_setup_lock = asyncio.Lock()
        self._retry_tasks = {}
        self._grace_expiry_job = HassJob(
            self._handle_grace_expiry, cancel_on_shutdown=True
        )
        self._recovery_retry_job = HassJob(
            self._handle_recovery_retry, cancel_on_shutdown=True
        )
        self._observation_timeout_job = HassJob(
            self._handle_observation_timeout, cancel_on_shutdown=True
        )
        self._legacy_poll_job = HassJob(
            self._handle_legacy_poll, cancel_on_shutdown=True
        )

    def _get_retry_tasks(self) -> dict[str, asyncio.Task[None]]:
        if self._retry_tasks is None:
            self._retry_tasks = {}
        return self._retry_tasks

    def _health_map(self) -> dict[str, ResourceHealthState]:
        runtime_data = getattr(self.config_entry, "runtime_data", None)
        if runtime_data is None:
            return {}
        return runtime_data.resource_health

    def _ensure_state(self, resource_name: str) -> ResourceHealthState:
        return self._health_map().setdefault(resource_name, ResourceHealthState())

    def _sync_observe_active(self) -> None:
        runtime_data = getattr(self.config_entry, "runtime_data", None)
        if runtime_data is None:
            return
        desired = [s for s in runtime_data.resource_health.values() if s.desired]
        if desired:
            runtime_data.observe_active = all(s.active for s in desired)

    def _entry_host(self) -> str | None:
        entry = getattr(self, "config_entry", None)
        if entry is None:
            return None
        data = getattr(entry, "data", None)
        if isinstance(data, dict):
            raw = data.get(CONF_HOST)
            return raw if isinstance(raw, str) else None
        return None

    def _sanitise_error(self, err: object) -> str:
        entry = getattr(self, "config_entry", None)
        runtime_data = getattr(entry, "runtime_data", None)
        tap = getattr(runtime_data, "tap", None)
        dock_id = getattr(tap, "wall_dock_id", None)
        unique_id = getattr(entry, "unique_id", None)
        return sanitise_error_text(
            err,
            self._entry_host(),
            dock_id if isinstance(dock_id, str) else None,
            unique_id if isinstance(unique_id, str) else None,
        )

    def _is_legacy_non_observable(self, resource_name: str) -> bool:
        runtime_data = getattr(self.config_entry, "runtime_data", None)
        tap = getattr(runtime_data, "tap", None)
        resources = getattr(tap, "resources", None)
        if not isinstance(resources, dict) or resource_name not in resources:
            return False
        return getattr(resources[resource_name], "observable", True) is False

    def has_resource_data(self, resource_name: str) -> bool:
        """Return True if *resource_name* has received at least one valid payload."""
        state = self._health_map().get(resource_name)
        return state is not None and state.has_data

    def is_resource_authoritative(self, resource_name: str) -> bool:
        """Return True when *resource_name* holds live/authoritative data."""
        return self.has_resource_data(resource_name)

    def is_resource_available(self, resource_name: str) -> bool:
        """Return True if *resource_name* is active or inside its 5m grace window."""
        if not self.last_update_success:
            return False
        health = self._health_map()
        if not health:
            return True

        state = health.get(resource_name)
        now = self._monotonic_fn()

        if self._is_legacy_non_observable(resource_name):
            if state is None:
                has_cached = (
                    self.data is not None and self.data.get(resource_name) is not None
                )
                return has_cached and self.is_device_reachable()
            in_grace = (
                state.unhealthy_since is None
                or (now - state.unhealthy_since) < GRACE_PERIOD_SECS
            )
            return state.has_data and in_grace and self.is_device_reachable()

        if state is None or not state.has_data:
            return False
        return state.active or (
            state.unhealthy_since is not None
            and (now - state.unhealthy_since) < GRACE_PERIOD_SECS
        )

    def is_device_reachable(self) -> bool:
        """Return True while any resource is active or within its 5m grace window."""
        if not self.last_update_success:
            return False
        health = self._health_map()
        if not health:
            return True
        now = self._monotonic_fn()
        return any(
            s.active
            or (
                s.has_data
                and s.unhealthy_since is not None
                and (now - s.unhealthy_since) < GRACE_PERIOD_SECS
            )
            for s in health.values()
        )

    @property
    def is_retrying_observe(self) -> bool:
        """Return True if per-resource retry scheduling or a retry task is active."""
        retry_tasks = self._retry_tasks or {}
        return self._recovery_retry_unsub is not None or any(
            not t.done() for t in retry_tasks.values()
        )

    @property
    def next_retry_in_secs(self) -> float | None:
        """Return seconds until the next scheduled per-resource retry, if any."""
        now = self._monotonic_fn()
        pending = [
            max(0.0, s.next_retry_at - now)
            for s in self._health_map().values()
            if s.desired and not s.active and s.next_retry_at is not None
        ]
        return round(min(pending), 1) if pending else None

    @property
    def next_grace_expiry_in_secs(self) -> float | None:
        """Return seconds until the earliest active 5-minute grace window expires."""
        now = self._monotonic_fn()
        remaining = [
            max(0.0, (s.unhealthy_since + GRACE_PERIOD_SECS) - now)
            for s in self._health_map().values()
            if s.has_data
            and not s.active
            and s.unhealthy_since is not None
            and (now - s.unhealthy_since) < GRACE_PERIOD_SECS
        ]
        return round(min(remaining), 1) if remaining else None

    def _cancel_silence_watchdog(self) -> None:
        if self._observation_timeout_unsub is not None:
            self._observation_timeout_unsub()
            self._observation_timeout_unsub = None

    def reset_observation_timeout(self) -> None:
        """Arm or reset the 30s silence watchdog strictly while `/sensors` is active."""
        if self._is_shutdown:
            return
        sensors_state = self._health_map().get("sensors")
        if sensors_state is None or not sensors_state.active:
            self._cancel_silence_watchdog()
            return
        if self._observation_timeout_unsub is not None:
            self._observation_timeout_unsub()
        job = getattr(self, "_observation_timeout_job", None) or HassJob(
            self._handle_observation_timeout, cancel_on_shutdown=True
        )
        self._observation_timeout_job = job
        self._observation_timeout_unsub = self._call_later_fn(
            self.hass, _OBSERVATION_TIMEOUT_SECS, job
        )

    def cancel_observation_timeout(self) -> None:
        """Cancel all grace, retry, silence-watchdog, and legacy-poll timers/tasks."""
        self._is_shutdown = True
        self._cancel_silence_watchdog()
        if self._grace_expiry_unsub is not None:
            self._grace_expiry_unsub()
            self._grace_expiry_unsub = None
        if self._recovery_retry_unsub is not None:
            self._recovery_retry_unsub()
            self._recovery_retry_unsub = None
        if self._legacy_poll_unsub is not None:
            self._legacy_poll_unsub()
            self._legacy_poll_unsub = None
        if self._legacy_poll_task is not None and not self._legacy_poll_task.done():
            self._legacy_poll_task.cancel()
        self._legacy_poll_task = None
        if self._retry_tasks:
            for task in list(self._retry_tasks.values()):
                if not task.done():
                    task.cancel()
            self._retry_tasks.clear()

    def _reschedule_grace_timer(self) -> None:
        if self._grace_expiry_unsub is not None:
            self._grace_expiry_unsub()
            self._grace_expiry_unsub = None
        if self._is_shutdown:
            return
        now = self._monotonic_fn()
        expiries = [
            s.unhealthy_since + GRACE_PERIOD_SECS
            for s in self._health_map().values()
            if s.has_data
            and not s.active
            and s.unhealthy_since is not None
            and (now - s.unhealthy_since) < GRACE_PERIOD_SECS
        ]
        if not expiries:
            return
        delay = max(0.0, min(expiries) - now)
        job = getattr(self, "_grace_expiry_job", None) or HassJob(
            self._handle_grace_expiry, cancel_on_shutdown=True
        )
        self._grace_expiry_job = job
        self._grace_expiry_unsub = self._call_later_fn(self.hass, delay, job)

    def _reschedule_retry_timer(self) -> None:
        if self._recovery_retry_unsub is not None:
            self._recovery_retry_unsub()
            self._recovery_retry_unsub = None
        if self._is_shutdown:
            return
        retry_tasks = self._get_retry_tasks()
        due_times = [
            s.next_retry_at
            for res, s in self._health_map().items()
            if s.desired
            and not s.active
            and s.next_retry_at is not None
            and res not in retry_tasks
        ]
        if not due_times:
            return
        delay = max(0.0, min(due_times) - self._monotonic_fn())
        job = getattr(self, "_recovery_retry_job", None) or HassJob(
            self._handle_recovery_retry, cancel_on_shutdown=True
        )
        self._recovery_retry_job = job
        self._recovery_retry_unsub = self._call_later_fn(self.hass, delay, job)

    @callback
    def _handle_grace_expiry(self, _now: datetime | float | None = None) -> None:
        """Transition grace-expired unhealthy resources to unavailable in HA."""
        self._grace_expiry_unsub = None
        now = self._monotonic_fn()
        dock_id = self.config_entry.runtime_data.tap.wall_dock_id
        for res, state in self._health_map().items():
            if (
                state.has_data
                and not state.active
                and state.unhealthy_since is not None
                and (now - state.unhealthy_since) >= GRACE_PERIOD_SECS - 1e-6
            ):
                self.logger.warning(
                    "CoAP resource '%s' on Tap %s unavailable after %.0fs grace "
                    "window; marking affected entities unavailable",
                    res,
                    dock_id,
                    GRACE_PERIOD_SECS,
                )
        self._reschedule_grace_timer()
        if self.data is not None:
            self.async_update_listeners()

    @callback
    def _handle_recovery_retry(self, _now: datetime | float | None = None) -> None:
        """Spawn independent retry tasks for all due unhealthy resources."""
        self._recovery_retry_unsub = None
        now = self._monotonic_fn()
        retry_tasks = self._get_retry_tasks()
        for res, state in list(self._health_map().items()):
            if (
                state.desired
                and not state.active
                and state.next_retry_at is not None
                and state.next_retry_at <= now + 1e-6
                and res not in retry_tasks
            ):
                state.next_retry_at = None
                retry_tasks[res] = self.hass.async_create_task(
                    self._run_resource_retry(res)
                )
        self._reschedule_retry_timer()

    async def _run_resource_retry(self, resource_name: str) -> None:
        """Retry observing one unhealthy CoAP resource without blocking siblings."""
        retry_tasks = self._get_retry_tasks()
        try:
            while True:
                state = self._health_map().get(resource_name)
                if state is None or not state.desired or state.active:
                    return
                state.wake_requested = False
                state.attempt_count += 1
                tap = self.config_entry.runtime_data.tap
                try:
                    await tap.retry_observe(resource_name)
                except Exception as err:  # noqa: BLE001
                    if self._is_shutdown or not state.desired or state.active:
                        return
                    state.last_error = self._sanitise_error(err)
                    self.logger.debug(
                        "Retry %d for CoAP resource '%s' failed: %s",
                        state.attempt_count,
                        resource_name,
                        state.last_error,
                    )
                    if state.wake_requested:
                        state.wake_requested = False
                        continue
                    now = self._monotonic_fn()
                    if state.unhealthy_since is None:
                        state.unhealthy_since = now
                    elapsed = max(0.0, now - state.unhealthy_since)
                    delay = compute_recovery_delay(elapsed, state.post_grace_attempts)
                    if elapsed >= GRACE_PERIOD_SECS:
                        state.post_grace_attempts += 1
                    state.next_retry_at = now + delay
                    return
                else:
                    if not state.active:
                        self.handle_observation_status(
                            ObservationStatusEvent(
                                resource=resource_name,
                                status=ObservationStatus.ACTIVE,
                            )
                        )
                    return
        finally:
            retry_tasks.pop(resource_name, None)
            self._reschedule_retry_timer()

    def _mark_resource_unhealthy(
        self, resource_name: str, *, error: object | None = None
    ) -> None:
        state = self._ensure_state(resource_name)
        now = self._monotonic_fn()
        first_transition = state.active or state.unhealthy_since is None
        state.active = False
        if state.unhealthy_since is None:
            state.unhealthy_since = now
        if error is not None:
            state.last_error = self._sanitise_error(error)
        if resource_name == "sensors":
            self._cancel_silence_watchdog()

        retry_tasks = self._get_retry_tasks()
        if (
            state.desired
            and state.next_retry_at is None
            and resource_name not in retry_tasks
        ):
            elapsed = max(0.0, now - state.unhealthy_since)
            state.next_retry_at = now + compute_recovery_delay(
                elapsed, state.post_grace_attempts
            )

        if first_transition:
            dock_id = self.config_entry.runtime_data.tap.wall_dock_id
            if state.has_data:
                self.logger.info(
                    "CoAP resource '%s' became unhealthy on Tap %s; keeping cached "
                    "entities available for %.0fs grace window",
                    resource_name,
                    dock_id,
                    GRACE_PERIOD_SECS,
                )
            else:
                self.logger.warning(
                    "CoAP resource '%s' unavailable at startup on Tap %s; "
                    "scheduling background recovery",
                    resource_name,
                    dock_id,
                )

        self._sync_observe_active()
        self._reschedule_grace_timer()
        self._reschedule_retry_timer()

    def _on_resource_recovered(
        self, resource_name: str, *, was_unhealthy: bool, prev_attempts: int
    ) -> None:
        if not was_unhealthy:
            return
        if prev_attempts > 0:
            self.logger.info(
                "CoAP resource '%s' recovered on Tap %s after %d attempt(s)",
                resource_name,
                self.config_entry.runtime_data.tap.wall_dock_id,
                prev_attempts,
            )
        if resource_name == "heating" and self.data is not None:
            self.hass.async_create_task(
                async_push_heating_state(self.hass, self.config_entry)
            )

    def mark_resource_data_received(self, resource_name: str) -> bool:
        """Record live data for *resource_name* and return if it was unavailable."""
        was_unavailable = bool(self._health_map()) and not self.is_resource_available(
            resource_name
        )
        state = self._ensure_state(resource_name)
        was_unhealthy = state.unhealthy_since is not None
        prev_attempts = state.attempt_count

        state.active = not self._is_legacy_non_observable(resource_name)
        state.has_data = True
        state.unhealthy_since = None
        state.attempt_count = 0
        state.post_grace_attempts = 0
        state.next_retry_at = None
        state.wake_requested = False
        state.last_error = None

        self._on_resource_recovered(
            resource_name, was_unhealthy=was_unhealthy, prev_attempts=prev_attempts
        )
        self._sync_observe_active()
        self.reset_observation_timeout()
        self._reschedule_grace_timer()
        self._reschedule_retry_timer()
        return was_unavailable

    @callback
    def handle_observation_status(self, event: ObservationStatusEvent) -> None:
        """Handle immediate stream status transitions from `pytewke`."""
        res = event.resource
        state = self._ensure_state(res)
        state.desired = True

        if event.status == ObservationStatus.ACTIVE:
            was_unavailable = self.data is not None and not self.is_resource_available(
                res
            )
            was_unhealthy = state.unhealthy_since is not None
            prev_attempts = state.attempt_count
            state.active = True
            state.unhealthy_since = None
            state.attempt_count = 0
            state.post_grace_attempts = 0
            state.next_retry_at = None
            state.wake_requested = False
            state.last_error = None

            self._on_resource_recovered(
                res, was_unhealthy=was_unhealthy, prev_attempts=prev_attempts
            )
            self._sync_observe_active()
            self.reset_observation_timeout()
            self._reschedule_grace_timer()
            self._reschedule_retry_timer()
            if was_unavailable and self.data is not None:
                self.async_update_listeners()
            return

        was_available = self.data is not None and self.is_resource_available(res)
        self._mark_resource_unhealthy(res, error=event.error)
        now_available = self.data is not None and self.is_resource_available(res)
        if self.data is not None and was_available != now_available:
            self.async_update_listeners()

    def _trigger_immediate_wake(self, resource_name: str) -> None:
        state = self._health_map().get(resource_name)
        if state is None or not state.desired or state.active:
            return
        state.post_grace_attempts = 0
        retry_tasks = self._get_retry_tasks()
        running = retry_tasks.get(resource_name)
        if running is not None and not running.done():
            state.wake_requested = True
            return
        state.next_retry_at = self._monotonic_fn()
        self._reschedule_retry_timer()

    @callback
    def handle_config_available_resources(self, config: ConfigData) -> None:
        """Wake unhealthy resources newly advertised in `/config`."""
        avail = getattr(config, "available_resources", None)
        if isinstance(avail, list):
            new_avail = frozenset(avail)
            prev_avail = self._prev_available_resources
            self._prev_available_resources = new_avail
            self._prev_config_room_id = config.room_id
            self._seen_initial_config = True
            newly_added = new_avail if prev_avail is None else (new_avail - prev_avail)
            for res in newly_added:
                self._trigger_immediate_wake(res)
            return

        was_seen = self._seen_initial_config
        prev_room_id = self._prev_config_room_id
        self._seen_initial_config = True
        self._prev_config_room_id = config.room_id
        if was_seen and prev_room_id is None and config.room_id is not None:
            self._trigger_immediate_wake("scenes")

    @callback
    def _handle_observation_timeout(self, _now: datetime | float | None = None) -> None:
        """Fire after 30s of zero observations while `/sensors` was active."""
        self._observation_timeout_unsub = None
        health = self._health_map()
        sensors_state = health.get("sensors")
        if sensors_state is not None and not sensors_state.active:
            return

        self.logger.info(
            "Observations timed out for Tap %s while /sensors was active; "
            "entering grace window and retrying",
            self.config_entry.runtime_data.tap.wall_dock_id,
        )
        if not health:
            self.config_entry.runtime_data.observe_active = False
            return

        now = self._monotonic_fn()
        for state in health.values():
            if state.desired and state.active:
                state.active = False
                if state.unhealthy_since is None:
                    state.unhealthy_since = now
                state.post_grace_attempts = 0
                state.next_retry_at = now
        self._sync_observe_active()
        self._reschedule_grace_timer()
        self._reschedule_retry_timer()

    def _ensure_legacy_poll_scheduled(self) -> None:
        if self._is_shutdown:
            return
        tap = self.config_entry.runtime_data.tap
        resources = getattr(tap, "resources", None)
        if not isinstance(resources, dict):
            return
        has_non_obs = any(
            getattr(info, "observable", True) is False for info in resources.values()
        )
        if not has_non_obs or self._legacy_poll_unsub is not None:
            return
        job = getattr(self, "_legacy_poll_job", None) or HassJob(
            self._handle_legacy_poll, cancel_on_shutdown=True
        )
        self._legacy_poll_job = job
        self._legacy_poll_unsub = self._call_later_fn(
            self.hass, _LEGACY_POLL_INTERVAL_SECS, job
        )

    @callback
    def _handle_legacy_poll(self, _now: datetime | float | None = None) -> None:
        self._legacy_poll_unsub = None
        if self._legacy_poll_task is None or self._legacy_poll_task.done():
            self._legacy_poll_task = self.hass.async_create_task(
                self._run_legacy_poll()
            )

    async def _run_legacy_poll(self) -> None:
        try:
            tap = self.config_entry.runtime_data.tap
            resources = getattr(tap, "resources", None)
            if not isinstance(resources, dict) or self.data is None:
                return
            updates: dict[str, object] = {}
            try:
                cfg = await tap.get_config()
                self.mark_resource_data_received("config")
                self.handle_config_available_resources(cfg)
                updates["config"] = cfg
            except _RETRYABLE_ERRORS as err:
                self._mark_resource_unhealthy("config", error=err)

            if self._is_legacy_non_observable("sensors"):
                try:
                    updates["sensors"] = await tap.get_sensors()
                    self.mark_resource_data_received("sensors")
                except _RETRYABLE_ERRORS as err:
                    self._mark_resource_unhealthy("sensors", error=err)

            if self._is_legacy_non_observable("energy"):
                try:
                    updates["energy"] = await tap.get_energy()
                    self.mark_resource_data_received("energy")
                except _RETRYABLE_ERRORS as err:
                    self._mark_resource_unhealthy("energy", error=err)

            if updates and self.data is not None:
                self.async_set_updated_data({**self.data, **updates})  # type: ignore[arg-type]
        finally:
            self._ensure_legacy_poll_scheduled()

    async def _setup_observe(self) -> bool:
        self._is_shutdown = False
        async with self._observe_setup_lock:
            has_any_active = any(s.active for s in self._health_map().values())
            if self.config_entry.runtime_data.observe_active or has_any_active:
                tap = self.config_entry.runtime_data.tap
                ensure_ok = await tap.ensure_active_observations()
                if ensure_ok or any(s.active for s in self._health_map().values()):
                    return True
                self.config_entry.runtime_data.observe_active = False
            LOGGER.info(
                "CoAP observations not active for %s; attempting to establish",
                self.config_entry.entry_id,
            )
            return await async_setup_observe(self, self.hass, self.config_entry)

    async def _resolve_refresh_scenes(
        self, tap: Any, prev: dict[str, Any]
    ) -> dict[str, Scene]:
        """Resolve scenes during `_async_update_data` without failing on 5.03."""
        scenes_state = self._health_map().get("scenes")
        if (
            scenes_state is not None
            and not scenes_state.active
            and scenes_state.unhealthy_since is not None
        ):
            # `/scenes` failed Observe: 0 in `_setup_observe()`; skip 7s retry stall.
            return dict(prev.get("scenes_all", {}))
        if (
            scenes_state is not None
            and scenes_state.active
            and scenes_state.has_data
            and getattr(tap.wall_dock, "scenes_loaded", True) is True
            and isinstance(getattr(tap.wall_dock, "scenes", None), dict)
        ):
            return dict(tap.wall_dock.scenes)
        try:
            scenes_all = await _fetch_with_retries(tap.get_scenes)
            self._ensure_state("scenes").has_data = True
        except _RETRYABLE_ERRORS as err:
            LOGGER.warning(
                "Scenes unavailable on Tap %s during refresh (%s); continuing "
                "in degraded mode",
                tap.wall_dock_id,
                err,
            )
            self._ensure_state("scenes").desired = True
            self._mark_resource_unhealthy("scenes", error=err)
            return dict(prev.get("scenes_all", {}))
        except PyTewkeInvalidResponseError as err:
            msg = f"Invalid scenes payload from Tewke Tap: {err}"
            raise UpdateFailed(msg) from err
        return scenes_all

    async def _async_update_data(self) -> TewkeCoordinatorData:
        """Fetch initial or fallback state while tolerating degraded `/scenes`."""
        try:
            await self._setup_observe()
        except Exception as err:
            raise UpdateFailed from err

        if (
            self.config_entry.runtime_data.observe_active
            and self.data is not None
            and self.last_update_success
        ):
            return self.data

        tap = self.config_entry.runtime_data.tap
        expected_dock_id = self.config_entry.unique_id

        if tap.wall_dock_id != expected_dock_id:
            msg = (
                f"Device swap detected! Coordinator expected {expected_dock_id} "
                f"but IP is reporting {tap.wall_dock_id}. Waiting for Zeroconf update."
            )
            raise UpdateFailed(msg)

        prev: dict[str, Any] = dict(self.data) if self.data is not None else {}

        try:
            targets = await _fetch_with_retries(tap.get_targets)
        except (
            PyTewkeCoapError,
            PyTewkeInvalidResponseError,
            PyTewkeUnknownError,
            TimeoutError,
        ) as err:
            self._mark_resource_unhealthy("targets", error=err)
            msg = f"Error communicating with Tewke Tap: {err}"
            raise UpdateFailed(msg) from err

        self._ensure_state("targets").has_data = True
        scenes_all = await self._resolve_refresh_scenes(tap, prev)

        if self.has_resource_data("scenes"):
            configured_scenes = reconcile_authoritative_scenes(
                self.hass, self.config_entry, scenes_all
            )
        else:
            scene_control_types = self.config_entry.runtime_data.scene_control_types
            configured_scenes = {
                scene_id: scene
                for scene_id, scene in scenes_all.items()
                if scene_id in scene_control_types
            }

        sensors, sensors_ok = await _fetch_optional(
            tap.get_sensors, prev.get("sensors"), "Sensor"
        )
        if sensors_ok and sensors is not None:
            self._ensure_state("sensors").has_data = True

        radar, radar_ok = await _fetch_optional(
            tap.get_radar, prev.get("radar"), "Radar"
        )
        if radar_ok and radar is not None:
            self._ensure_state("radar").has_data = True

        energy, energy_ok = await _fetch_optional(
            tap.get_energy, prev.get("energy"), "Energy"
        )
        if energy_ok and energy is not None:
            self._ensure_state("energy").has_data = True

        energy_override, _ = await _fetch_optional(
            tap.get_energy_override, prev.get("energy_override"), "Energy override"
        )

        config, config_ok = await _fetch_optional(
            tap.get_config, prev.get("config"), "Config"
        )
        if config_ok and config is not None:
            self._ensure_state("config").has_data = True
            self.handle_config_available_resources(config)

        heating: HeatingData | None = prev.get("heating")
        if tap_supports_heating(tap):
            heating, heating_ok = await _fetch_optional(
                tap.get_heating, heating, "Heating"
            )
            if heating_ok and heating is not None:
                self._ensure_state("heating").has_data = True

        self._ensure_legacy_poll_scheduled()
        self.reset_observation_timeout()

        return TewkeCoordinatorData(
            scenes=configured_scenes,
            scenes_all=scenes_all,
            targets=targets,
            sensors=sensors,
            radar=radar,
            energy=energy,
            energy_override=energy_override,
            config=config,
            heating=heating,
        )
