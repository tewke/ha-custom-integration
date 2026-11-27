"""Unit tests for the Tewke custom integration."""

# ruff: noqa: PLR2004, PT027, S101, SLF001

from __future__ import annotations

import asyncio
import unittest
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiocoap import Code as coapCode
from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.const import CONF_HOST, CONF_NAME, EntityCategory
from homeassistant.core import EVENT_STATE_CHANGED, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.entity import DATA_CUSTOMIZE, EntityPlatformState
from pytewke.data import (
    ConfigData,
    EnergyData,
    EnergyOverrideData,
    HeatingCommandData,
    HeatingData,
    ObservationStatus,
    ObservationStatusEvent,
    RadarData,
    ResourceInfo,
    Scene,
    SensorData,
    Target,
    UpdateStatus,
)
from pytewke.error import PyTewkeCoapError, PyTewkeDiscoveryError

from custom_components.tewke import async_reload_entry, async_setup_entry
from custom_components.tewke.binary_sensor import (
    BINARY_SENSOR_DESCRIPTIONS,
    TewkeBinarySensor,
    TewkeScreenBinarySensor,
)
from custom_components.tewke.button import TewkeRestartButton
from custom_components.tewke.config_flow import (
    TewkeConfigFlow,
    TewkeOptionsFlow,
    _build_unique_scene_labels,
)
from custom_components.tewke.const import (
    CONF_CLIMATE_ENTITIES,
    CONF_PRIMARY_CLIMATE_ENTITY,
    DOMAIN,
)
from custom_components.tewke.coordinator import (
    TewkeCoordinator,
    compute_recovery_delay,
)
from custom_components.tewke.data import ResourceHealthState, TewkeData
from custom_components.tewke.diagnostics import async_get_config_entry_diagnostics
from custom_components.tewke.heating import (
    async_handle_heating_command,
    async_push_heating_state,
    async_setup_climate_state_listener,
    build_heating_zones,
    tap_supports_heating,
)
from custom_components.tewke.light import async_setup_entry as async_setup_light_entry
from custom_components.tewke.number import TewkeEnergyOverrideNumber
from custom_components.tewke.scene import (
    TewkeSceneFan,
    TewkeSceneLight,
    TewkeSceneSwitch,
)
from custom_components.tewke.sensor import (
    CONFIG_SENSOR_DESCRIPTIONS,
    ENERGY_SENSOR_DESCRIPTIONS,
    RADAR_SENSOR_DESCRIPTIONS,
    SENSOR_DESCRIPTIONS,
    TewkeConfigSensor,
    TewkeEnergySensor,
    TewkeRadarSensor,
    TewkeSensor,
)
from custom_components.tewke.sensor import (
    async_setup_entry as async_setup_sensor_entry,
)
from custom_components.tewke.target import TewkeTargetLight
from custom_components.tewke.util import (
    _ha_to_tewke_brightness,
    _sync_device_metadata,
    _tewke_to_ha_brightness,
    async_setup_observe,
)


def _make_mock_entry(
    *,
    entry_id: str = "entry_1",
    unique_id: str = "dock_123",
    host: str = "192.168.1.50",
    name: str = "Living Room Tap",
    scene_control_types: dict[str, str] | None = None,
) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.unique_id = unique_id
    entry.title = name
    entry.data = {
        CONF_HOST: host,
        CONF_NAME: name,
        "room_name": "Living Room",
        "scene_control_types": scene_control_types or {"s1": "light", "s2": "fan"},
    }
    entry.options = {}
    return entry


class TestBrightnessAndLabels(unittest.TestCase):
    """Test utility functions and scene label deduplication."""

    def test_brightness_conversions(self) -> None:
        """Test converting brightness between Tewke (0-100) and HA (0-255)."""
        assert _tewke_to_ha_brightness(0) == 0
        assert _tewke_to_ha_brightness(100) == 255
        assert _ha_to_tewke_brightness(0) == 0
        assert _ha_to_tewke_brightness(255) == 100

    def test_unique_scene_labels_disambiguates_duplicates(self) -> None:
        """Test duplicate scene names get unique suffixes."""
        scenes = {
            "aaaa1111": Scene(
                id="aaaa1111", name="Evening", isActive=False, brightness=50
            ),
            "bbbb2222": Scene(
                id="bbbb2222", name="Evening", isActive=True, brightness=80
            ),
            "cccc3333": Scene(
                id="cccc3333", name="Bright", isActive=False, brightness=100
            ),
        }
        labels = _build_unique_scene_labels(scenes)
        assert labels["Evening (1111)"] == "aaaa1111"
        assert labels["Evening (2222)"] == "bbbb2222"
        assert labels["Bright"] == "cccc3333"


class TestSetupAndCoordinator(unittest.IsolatedAsyncioTestCase):
    """Test entry setup, coordinator refresh, and observation deduplication."""

    async def test_setup_entry_timeout_closes_tap_and_raises_not_ready(self) -> None:
        """Verify TimeoutError during discover closes tap and raises not-ready."""
        hass = MagicMock()
        entry = _make_mock_entry()

        with patch("custom_components.tewke.pytewke.Tap") as mock_tap_cls:
            mock_tap = mock_tap_cls.return_value
            mock_tap.discover = AsyncMock(side_effect=TimeoutError("timed out"))
            mock_tap.close = AsyncMock()

            with self.assertRaises(ConfigEntryNotReady):
                await async_setup_entry(hass, entry)

            mock_tap.close.assert_awaited_once()

    async def test_reload_entry_updates_listeners_without_full_reload(self) -> None:
        """Verify async_reload_entry updates coordinator listeners in-place."""
        hass = MagicMock()
        entry = _make_mock_entry()
        entry.runtime_data = SimpleNamespace(
            host="192.168.1.50", coordinator=MagicMock()
        )

        await async_reload_entry(hass, entry)

        entry.runtime_data.coordinator.async_update_listeners.assert_called_once()
        hass.config_entries.async_schedule_reload.assert_not_called()

    async def test_reload_entry_schedules_reload_when_host_changes(self) -> None:
        """Verify async_reload_entry schedules a full reload when CONF_HOST changes."""
        hass = MagicMock()
        entry = _make_mock_entry(host="192.168.1.99")
        entry.data = MappingProxyType(entry.data)
        entry.runtime_data = SimpleNamespace(
            host="192.168.1.50", coordinator=MagicMock()
        )

        await async_reload_entry(hass, entry)

        hass.config_entries.async_schedule_reload.assert_called_once_with(
            entry.entry_id
        )
        entry.runtime_data.coordinator.async_update_listeners.assert_not_called()

    def test_sync_device_metadata_passes_entry_id_to_identifier_lookup(self) -> None:
        """Verify _sync_device_metadata passes entry_id to identifier lookup."""
        hass = MagicMock()
        entry = _make_mock_entry()
        mock_tap = MagicMock(wall_dock_id="dock_123")
        entry.runtime_data = SimpleNamespace(tap=mock_tap)
        mock_device = SimpleNamespace(id="dev_1", sw_version="1.0.0")
        mock_registry = MagicMock()
        mock_registry.async_get_device_by_identifier = MagicMock(
            return_value=mock_device
        )

        config_data = ConfigData(
            roomId="r1",
            roomName="Bedroom",
            deviceName="Bedroom Tap",
            hardwareId="dock_123",
            tewkeOsVersion="1.3.0",
        )

        with patch(
            "custom_components.tewke.util.dr.async_get",
            return_value=mock_registry,
        ):
            _sync_device_metadata(hass, entry, config_data)

        mock_registry.async_get_device_by_identifier.assert_called_once_with(
            (DOMAIN, "dock_123"), entry.entry_id
        )
        mock_registry.async_update_device.assert_any_call("dev_1", name="Bedroom Tap")
        mock_registry.async_update_device.assert_any_call("dev_1", sw_version="1.3.0")

    async def test_coordinator_recovers_from_failed_state(self) -> None:
        """Verify _async_update_data polls when last_update_success is False."""
        hass = MagicMock()
        entry = _make_mock_entry(scene_control_types={"s1": "light"})
        mock_tap = MagicMock()
        mock_tap.wall_dock_id = "dock_123"
        mock_tap.tewke_os_version = "1.2.0"
        mock_tap.ensure_active_observations = AsyncMock(return_value=True)
        mock_tap.get_scenes = AsyncMock(
            return_value={
                "s1": Scene(id="s1", name="Relax", isActive=True, brightness=60)
            }
        )
        mock_tap.get_targets = AsyncMock(
            return_value={
                0: Target(
                    index=0,
                    name="Ceiling",
                    isDimmable=True,
                    isOn=True,
                    brightness=75,
                )
            }
        )
        mock_tap.get_sensors = AsyncMock(side_effect=TimeoutError())
        mock_tap.get_radar = AsyncMock(
            return_value=RadarData(proximity="near", screenOn=True)
        )
        mock_tap.get_energy = AsyncMock(
            return_value=EnergyData(
                power=12.5, actualPower=12.5, override=None, hasOverride=False
            )
        )
        mock_tap.get_energy_override = AsyncMock(
            return_value=EnergyOverrideData(override=None, active=False)
        )
        mock_tap.get_config = AsyncMock(
            return_value=ConfigData(
                roomId="r1",
                roomName="Living Room",
                deviceName="Living Room Tap",
                hardwareId="dock_123",
                tewkeOsVersion="1.2.0",
            )
        )

        def _noop_init(*_args: object, **_kwargs: object) -> None:
            return None

        with patch.object(TewkeCoordinator, "__init__", _noop_init):
            coordinator = TewkeCoordinator(hass=hass, logger=MagicMock(), name=DOMAIN)
            coordinator.hass = hass
            coordinator.logger = MagicMock()
            coordinator.config_entry = entry
            lock_mock = MagicMock()
            lock_mock.__aenter__ = AsyncMock()
            lock_mock.__aexit__ = AsyncMock()
            coordinator._observe_setup_lock = lock_mock
            prev_sensor = SensorData(
                iaq=50,
                rawGas=1000.0,
                staticIaq=50,
                iaqAccuracy=3,
                rawHumidity=45.0,
                rawPressure=101325.0,
                runInStatus=True,
                co2Equivalent=500,
                gasPercentage=10,
                rawTemperature=21.0,
                breathVocEquivalent=0.5,
                compensatedHumidity=45.0,
                stabilisationStatus=True,
                compensatedTemperature=21.0,
            )
            coordinator.data = {"sensors": prev_sensor}
            coordinator.last_update_success = False

            entry.runtime_data = TewkeData(
                host="192.168.1.50",
                tap=mock_tap,
                coordinator=coordinator,
                scene_control_types={"s1": "light"},
                observe_active=True,
            )

            data = await coordinator._async_update_data()

            # Polled scenes/targets and preserved previous sensor data on timeout
            mock_tap.get_scenes.assert_awaited_once()
            mock_tap.get_targets.assert_awaited_once()
            assert data["sensors"] is prev_sensor
            assert data["scenes"]["s1"].brightness == 60

    async def test_sensor_observe_deduplicates_unchanged_data(self) -> None:
        """Verify _on_sensor_update resets timeout and skips unchanged state push."""
        hass = MagicMock()
        entry = _make_mock_entry()
        mock_tap = MagicMock()
        mock_tap.close_observations = AsyncMock()
        mock_tap.observe = AsyncMock()

        coordinator = MagicMock()
        sensor = SensorData(
            iaq=50,
            rawGas=1000.0,
            staticIaq=50,
            iaqAccuracy=3,
            rawHumidity=45.0,
            rawPressure=101325.0,
            runInStatus=True,
            co2Equivalent=500,
            gasPercentage=10,
            rawTemperature=21.0,
            breathVocEquivalent=0.5,
            compensatedHumidity=45.0,
            stabilisationStatus=True,
            compensatedTemperature=21.0,
        )
        coordinator.data = {"sensors": sensor}
        entry.runtime_data = TewkeData(
            host="192.168.1.50",
            tap=mock_tap,
            coordinator=coordinator,
            scene_control_types={},
        )

        await async_setup_observe(coordinator, hass, entry)
        sensor_cb = mock_tap.observe.call_args.kwargs["sensor_callback"]

        coordinator.mark_resource_data_received.return_value = False
        coordinator.mark_resource_data_received.reset_mock()
        coordinator.async_set_updated_data.reset_mock()
        coordinator.async_update_listeners.reset_mock()

        # Identical sensor data records live receipt without pushing state
        sensor_cb(sensor)
        coordinator.mark_resource_data_received.assert_called_once_with("sensors")
        coordinator.async_set_updated_data.assert_not_called()
        coordinator.async_update_listeners.assert_not_called()

        # Identical sensor data when recovering from unavailable notifies listeners
        coordinator.mark_resource_data_received.return_value = True
        sensor_cb(sensor)
        assert coordinator.mark_resource_data_received.call_count == 2
        coordinator.async_update_listeners.assert_called_once()

        # Updated sensor data fires async_set_updated_data
        coordinator.mark_resource_data_received.return_value = False
        updated_sensor = sensor.model_copy(update={"iaq": 75})
        sensor_cb(updated_sensor)
        assert coordinator.mark_resource_data_received.call_count == 3
        coordinator.async_set_updated_data.assert_called_once()


class TestEntitiesAndDiagnostics(unittest.IsolatedAsyncioTestCase):
    """Test entities, optimistic state updates, error handling, and diagnostics."""

    def _make_coordinator(self) -> tuple[MagicMock, MagicMock]:
        entry = _make_mock_entry()
        mock_tap = MagicMock()
        mock_tap.wall_dock_id = "dock_123"
        mock_tap.tewke_os_version = "1.2.0"
        mock_tap.resources = {}

        scene1 = Scene(id="s1", name="Movie", isActive=False, brightness=40)
        scene2 = Scene(id="s2", name="Fan Scene", isActive=True, brightness=100)
        target0 = Target(index=0, name=None, isDimmable=True, isOn=False, brightness=0)

        coordinator = MagicMock()
        coordinator.config_entry = entry
        coordinator.is_retrying_observe = False
        coordinator.data = {
            "scenes": {"s1": scene1, "s2": scene2},
            "scenes_all": {"s1": scene1, "s2": scene2},
            "targets": {0: target0},
            "sensors": None,
            "radar": RadarData(proximity="near", screenOn=True),
            "energy": None,
            "energy_override": EnergyOverrideData(override=150.0, active=True),
            "config": None,  # Verify entities work even when config is None!
        }
        entry.runtime_data = TewkeData(
            host="192.168.1.50",
            tap=mock_tap,
            coordinator=coordinator,
            scene_control_types={"s1": "light", "s2": "fan"},
            observe_active=True,
        )
        return coordinator, mock_tap

    async def test_entities_initialise_when_config_is_none(self) -> None:
        """Entities must not crash when coordinator.data['config'] is None."""
        coordinator, _ = self._make_coordinator()
        scene_light = TewkeSceneLight(coordinator, coordinator.data["scenes"]["s1"])
        scene_switch = TewkeSceneSwitch(coordinator, coordinator.data["scenes"]["s1"])
        scene_fan = TewkeSceneFan(
            coordinator, coordinator.data["scenes"]["s2"], default_dimming=55
        )
        target_light = TewkeTargetLight(coordinator, coordinator.data["targets"][0])
        screen_sensor = TewkeScreenBinarySensor(coordinator)
        override_num = TewkeEnergyOverrideNumber(coordinator)
        restart_btn = TewkeRestartButton(coordinator)
        update_sensor = TewkeConfigSensor(coordinator, CONFIG_SENSOR_DESCRIPTIONS[0])

        assert scene_light.unique_id == "dock_123_s1"
        assert not scene_switch.is_on
        assert scene_fan.is_on
        assert scene_fan.percentage == 55
        assert target_light.name == "Target 1"
        assert screen_sensor.is_on
        assert override_num.native_value == 150.0
        assert restart_btn.unique_id == "dock_123_restart"
        assert update_sensor.unique_id == "dock_123_update_status"
        assert update_sensor.native_value is None
        assert (
            update_sensor.entity_description.entity_category
            == EntityCategory.DIAGNOSTIC
        )
        assert update_sensor.entity_description.device_class == SensorDeviceClass.ENUM

    async def test_software_update_status_sensor_and_backwards_compatibility(
        self,
    ) -> None:
        """Software update status sensor handles old firmware and late discovery."""
        coordinator, _ = self._make_coordinator()
        # Simulate older switch-ui firmware that sends /config without updateStatus
        coordinator.data["config"] = ConfigData(
            roomId="r1",
            roomName="Living Room",
            deviceName="Living Room Tap",
            hardwareId="dock_123",
            tewkeOsVersion="1.2.0",
        )
        added: list[object] = []
        listeners: list[object] = []
        coordinator.async_add_listener = lambda cb: (
            listeners.append(cb) or (lambda: None)
        )

        await async_setup_sensor_entry(
            MagicMock(), coordinator.config_entry, added.extend
        )

        # Radar sensors were added, but update_status was skipped because it is None
        assert not any(isinstance(e, TewkeConfigSensor) for e in added)
        assert len(listeners) == 1

        # Simulate Tap upgrading to new switch-ui that emits updateStatus in /config
        cfg_with_status = ConfigData(
            roomId="r1",
            roomName="Living Room",
            deviceName="Living Room Tap",
            hardwareId="dock_123",
            tewkeOsVersion="1.2.0",
            updateStatus="downloading",
        )
        coordinator.data["config"] = cfg_with_status
        listeners[0]()

        config_sensors = [e for e in added if isinstance(e, TewkeConfigSensor)]
        assert len(config_sensors) == 1
        assert config_sensors[0].native_value == "downloading"

        # Transition to pending_reboot and complete
        cfg_with_status.update_status = UpdateStatus.PENDING_REBOOT
        assert config_sensors[0].native_value == "pending_reboot"
        cfg_with_status.update_status = UpdateStatus.COMPLETE
        assert config_sensors[0].native_value == "complete"

    async def test_radar_channel_diagnostic_sensor(self) -> None:
        """Radar channel sensor returns channel or None on old firmware."""
        coordinator, _ = self._make_coordinator()
        channel_desc = next(
            d for d in RADAR_SENSOR_DESCRIPTIONS if d.key == "radar_channel"
        )
        channel_sensor = TewkeRadarSensor(coordinator, channel_desc)

        assert channel_sensor.unique_id == "dock_123_radar_channel"
        assert (
            channel_sensor.entity_description.entity_category
            == EntityCategory.DIAGNOSTIC
        )
        assert not channel_sensor.entity_description.entity_registry_enabled_default
        # Old firmware without channel reports None
        assert channel_sensor.native_value is None

        # New firmware with channel reports 1-based channel index
        coordinator.data["radar"] = RadarData(proximity="near", channel=4)
        assert channel_sensor.native_value == 4

    async def test_scene_light_optimistic_state_preserved(self) -> None:
        """Turning on a scene light updates _scene in-place before writing state."""
        coordinator, mock_tap = self._make_coordinator()
        mock_tap.set_scene = AsyncMock()
        scene_light = TewkeSceneLight(coordinator, coordinator.data["scenes"]["s1"])
        scene_light.async_write_ha_state = MagicMock()

        await scene_light.async_turn_on(brightness=255)

        assert scene_light.is_on
        assert scene_light.brightness == 255
        assert coordinator.data["scenes"]["s1"].is_active
        assert coordinator.data["scenes"]["s1"].brightness == 100

    async def test_scene_and_target_revert_ha_state_on_coap_error(self) -> None:
        """Verify failed scene and target commands revert and emit state_changed."""
        coordinator, mock_tap = self._make_coordinator()
        mock_tap.set_scene = AsyncMock(
            side_effect=PyTewkeCoapError("5.03 Service Unavailable", MagicMock())
        )
        mock_tap.set_target = AsyncMock(
            side_effect=PyTewkeCoapError("5.03 Service Unavailable", MagicMock())
        )

        hass = HomeAssistant(".")
        hass.data[DATA_CUSTOMIZE] = {}
        state_changed_events: list[object] = []

        @callback
        def _record_state_changed(event: object) -> None:
            state_changed_events.append(event)

        hass.bus.async_listen(EVENT_STATE_CHANGED, _record_state_changed)

        scene_light = TewkeSceneLight(coordinator, coordinator.data["scenes"]["s1"])
        scene_light.hass = hass
        scene_light.platform = MagicMock()
        scene_light.entity_id = "light.relax"
        scene_light._platform_state = EntityPlatformState.ADDED
        scene_light.async_write_ha_state()
        assert len(state_changed_events) == 1

        with self.assertRaises(HomeAssistantError):
            await scene_light.async_turn_on(brightness=200)

        mock_tap.set_scene.assert_awaited_once_with(
            scene_id="s1",
            state=True,
            brightness=_ha_to_tewke_brightness(200),
        )
        assert not scene_light.is_on
        assert scene_light.brightness == _tewke_to_ha_brightness(40)
        assert len(state_changed_events) == 2
        assert hass.states.get("light.relax").state == "off"

        target_light = TewkeTargetLight(coordinator, coordinator.data["targets"][0])
        target_light.hass = hass
        target_light.platform = MagicMock()
        target_light.entity_id = "light.spot"
        target_light._platform_state = EntityPlatformState.ADDED
        target_light.async_write_ha_state()
        assert len(state_changed_events) == 3

        with self.assertRaises(HomeAssistantError):
            await target_light.async_turn_on(brightness=200)

        mock_tap.set_target.assert_awaited_once_with(
            target=0,
            brightness=_ha_to_tewke_brightness(200),
        )
        assert not target_light.is_on
        assert len(state_changed_events) == 4
        assert hass.states.get("light.spot").state == "off"

    async def test_number_and_button_error_handling(self) -> None:
        """Number and button entities wrap CoAP failures in HomeAssistantError."""
        coordinator, mock_tap = self._make_coordinator()
        mock_tap.set_energy_override = AsyncMock(
            side_effect=PyTewkeCoapError("fail", MagicMock())
        )
        mock_tap.restart = AsyncMock(side_effect=PyTewkeCoapError("fail", MagicMock()))

        override_num = TewkeEnergyOverrideNumber(coordinator)
        restart_btn = TewkeRestartButton(coordinator)

        with self.assertRaises(HomeAssistantError):
            await override_num.async_set_native_value(100.0)

        with self.assertRaises(HomeAssistantError):
            await restart_btn.async_press()

    async def test_diagnostics_redacts_host(self) -> None:
        """Diagnostics output redacts sensitive host and hardwareId fields."""
        coordinator, _ = self._make_coordinator()
        hass = MagicMock()
        diag = await async_get_config_entry_diagnostics(hass, coordinator.config_entry)
        assert diag["entry"]["data"][CONF_HOST] == "**REDACTED**"
        assert diag["device"]["observe_active"]


class TestConfigFlowUserStep(unittest.IsolatedAsyncioTestCase):
    """Test manual user config flow step."""

    async def test_user_step_connection_error_shows_form_error(self) -> None:
        """Test that a connection failure in async_step_user sets cannot_connect."""
        flow = TewkeConfigFlow()
        flow.hass = MagicMock()

        with patch("custom_components.tewke.config_flow.pytewke.Tap") as mock_tap_cls:
            mock_tap = mock_tap_cls.return_value
            mock_tap.discover = AsyncMock(side_effect=PyTewkeDiscoveryError("boom"))
            mock_tap.close = AsyncMock()

            result = await flow.async_step_user({CONF_HOST: "192.168.1.99"})

            assert result["type"] == "form"
            assert result["errors"] == {"base": "cannot_connect"}
            mock_tap.close.assert_awaited_once()


class TestHeatingBridgeAndFlows(unittest.IsolatedAsyncioTestCase):
    """Test Home Assistant heating bridge, config/options flows, and compatibility."""

    async def test_backwards_compatibility_when_heating_not_supported(self) -> None:
        """Verify older Tap firmware without /heating skips heating calls and steps."""
        hass = MagicMock()
        entry = _make_mock_entry()
        mock_tap = MagicMock()
        mock_tap.resources = {"config": MagicMock()}
        mock_tap.close_observations = AsyncMock()
        mock_tap.observe = AsyncMock()
        mock_tap.set_heating = AsyncMock()
        mock_tap.clear_heating = AsyncMock()

        coordinator = MagicMock()
        coordinator.data = {}
        entry.runtime_data = TewkeData(
            host="192.168.1.50",
            tap=mock_tap,
            coordinator=coordinator,
            scene_control_types={},
        )

        assert not tap_supports_heating(mock_tap)
        assert await async_push_heating_state(hass, entry) is None
        mock_tap.set_heating.assert_not_called()
        mock_tap.clear_heating.assert_not_called()

        await async_setup_observe(coordinator, hass, entry)
        assert "heating_callback" not in mock_tap.observe.call_args.kwargs

        # Options flow aborts with no_fan_scenes when neither fan scenes nor /heating
        entry.data = {"scene_control_types": {"s1": "light"}}
        options_flow = TewkeOptionsFlow()
        options_flow.hass = hass
        with patch.object(TewkeOptionsFlow, "config_entry", new=entry):
            res = await options_flow.async_step_init()
            assert res["type"] == "abort"
            assert res["reason"] == "no_fan_scenes"

    async def test_heating_zones_push_and_command_handling(self) -> None:
        """Verify HA climate states build HeatingZoneData and commands call HA."""
        hass = MagicMock()
        hass.services.async_call = AsyncMock()
        lr_state = SimpleNamespace(
            state="cool",
            attributes={
                "friendly_name": "Living Room Thermostat",
                "current_temperature": 24.5,
                "temperature": 21.0,
                "hvac_action": "cooling",
                "hvac_modes": ("heat", "cool", "dry", "fan_only", "auto", "off"),
                "min_temp": 7.0,
                "max_temp": 30.0,
                "preset_mode": "comfort",
                "preset_modes": ["none", "eco", "comfort", "boost"],
                "fan_mode": "auto",
                "fan_modes": ("auto", "low", "medium", "high"),
            },
        )
        br_state = SimpleNamespace(
            state="dry",
            attributes={
                "friendly_name": "Bedroom Thermostat",
                "current_temperature": 18.0,
                "temperature": 19.0,
                "hvac_action": "drying",
                "hvac_modes": ["cool", "dry", "fan_only", "off"],
            },
        )
        states_map = {
            "climate.living_room": lr_state,
            "climate.bedroom": br_state,
        }
        hass.states.get = MagicMock(side_effect=states_map.get)

        entry = _make_mock_entry()
        entry.data[CONF_CLIMATE_ENTITIES] = [
            "climate.living_room",
            "climate.bedroom",
        ]
        entry.data[CONF_PRIMARY_CLIMATE_ENTITY] = "climate.living_room"

        zones = build_heating_zones(hass, entry)
        assert len(zones) == 2
        assert zones["climate.living_room"].name == "Living Room Thermostat"
        assert zones["climate.living_room"].current_temperature == 24.5
        assert zones["climate.living_room"].target_temperature == 21.0
        assert zones["climate.living_room"].system_mode == "cool"
        assert not zones["climate.living_room"].actively_heating
        assert zones["climate.living_room"].actively_cooling
        assert zones["climate.living_room"].is_this_room
        assert zones["climate.living_room"].min_temp == 7.0
        assert zones["climate.living_room"].max_temp == 30.0
        assert zones["climate.living_room"].system_modes == [
            "heat",
            "cool",
            "dry",
            "fan_only",
            "auto",
            "off",
        ]
        assert zones["climate.living_room"].preset_mode == "comfort"
        assert zones["climate.living_room"].preset_modes == [
            "none",
            "eco",
            "comfort",
            "boost",
        ]
        assert zones["climate.living_room"].fan_mode == "auto"
        assert zones["climate.living_room"].fan_modes == [
            "auto",
            "low",
            "medium",
            "high",
        ]

        assert zones["climate.bedroom"].system_mode == "dry"
        assert zones["climate.bedroom"].system_modes == [
            "cool",
            "dry",
            "fan_only",
            "off",
        ]
        assert not zones["climate.bedroom"].actively_heating
        assert not zones["climate.bedroom"].actively_cooling
        assert not zones["climate.bedroom"].is_this_room

        mock_tap = MagicMock()
        mock_tap.resources = {"heating": MagicMock()}
        mock_tap.set_heating = AsyncMock(
            return_value=HeatingData(active=True, zones=zones)
        )
        coordinator = MagicMock()
        coordinator.data = {}
        entry.runtime_data = TewkeData(
            host="192.168.1.50",
            tap=mock_tap,
            coordinator=coordinator,
            scene_control_types={},
        )

        pushed = await async_push_heating_state(hass, entry)
        assert pushed is not None
        assert pushed.active
        mock_tap.set_heating.assert_awaited_once_with(zones, active=True)

        # Command from Tap triggers climate service calls
        cmd = HeatingCommandData(
            zone_id="climate.living_room",
            target_temperature=19.5,
            system_mode="cool",
            preset_mode="boost",
            fan_mode="high",
        )
        await async_handle_heating_command(hass, entry, cmd)
        assert hass.services.async_call.await_count == 4
        hass.services.async_call.assert_any_await(
            "climate",
            "set_hvac_mode",
            {"entity_id": "climate.living_room", "hvac_mode": "cool"},
            blocking=True,
        )
        hass.services.async_call.assert_any_await(
            "climate",
            "set_temperature",
            {"entity_id": "climate.living_room", "temperature": 19.5},
            blocking=True,
        )
        hass.services.async_call.assert_any_await(
            "climate",
            "set_preset_mode",
            {"entity_id": "climate.living_room", "preset_mode": "boost"},
            blocking=True,
        )
        hass.services.async_call.assert_any_await(
            "climate",
            "set_fan_mode",
            {"entity_id": "climate.living_room", "fan_mode": "high"},
            blocking=True,
        )

    async def test_config_and_options_flow_with_heating_support(self) -> None:
        """Verify config flow steps through heating when /heating is advertised."""
        flow = TewkeConfigFlow()
        flow.hass = MagicMock()
        flow._discovered_host = "192.168.1.50"
        flow._discovered_name = "Living Room Tap"
        flow._room_name = "Living Room"

        mock_tap = MagicMock()
        mock_tap.resources = {"heating": MagicMock()}
        mock_tap.get_scenes = AsyncMock(return_value={})
        mock_tap.get_heating = AsyncMock(
            return_value=HeatingData(active=False, zones={})
        )
        mock_tap.close = AsyncMock()
        flow._tap = mock_tap

        # No scenes -> proceeds directly to heating step because /heating is present
        step_res = await flow.async_step_confirm_control_types()
        assert step_res["type"] == "form"
        assert step_res["step_id"] == "heating"

        # Submit heating entities -> proceeds to confirmation
        step_res2 = await flow.async_step_heating(
            {
                CONF_CLIMATE_ENTITIES: ["climate.living_room"],
                CONF_PRIMARY_CLIMATE_ENTITY: "climate.living_room",
            }
        )
        assert step_res2["type"] == "form"
        assert step_res2["step_id"] == "confirmation"

        # Submit confirmation -> creates entry with climate entities
        create_res = await flow.async_step_confirmation({})
        assert create_res["type"] == "create_entry"
        assert create_res["data"][CONF_CLIMATE_ENTITIES] == ["climate.living_room"]
        assert create_res["data"][CONF_PRIMARY_CLIMATE_ENTITY] == "climate.living_room"

    async def test_reconfigure_flow_updates_climate_entities(self) -> None:
        """Verify reconfigure flow allows changing and saving climate entities."""
        entry = _make_mock_entry()
        entry.data[CONF_CLIMATE_ENTITIES] = ["climate.living_room"]
        entry.data[CONF_PRIMARY_CLIMATE_ENTITY] = "climate.living_room"
        entry.options = {
            CONF_CLIMATE_ENTITIES: ["climate.old_option"],
            CONF_PRIMARY_CLIMATE_ENTITY: "climate.old_option",
        }

        flow = TewkeConfigFlow()
        flow.hass = MagicMock()
        flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
        flow._get_reconfigure_entry = MagicMock(return_value=entry)

        mock_tap = MagicMock()
        mock_tap.resources = {"heating": MagicMock()}
        mock_tap.get_scenes = AsyncMock(return_value={})
        mock_tap.close = AsyncMock()
        flow._tap = mock_tap

        step_res = await flow.async_step_reconfigure()
        assert step_res["type"] == "form"
        assert step_res["step_id"] == "heating"

        await flow.async_step_heating(
            {
                CONF_CLIMATE_ENTITIES: ["climate.bedroom", "climate.kitchen"],
                CONF_PRIMARY_CLIMATE_ENTITY: "climate.bedroom",
            }
        )
        abort_res = await flow.async_step_confirmation({})
        assert abort_res["reason"] == "reconfigure_successful"
        updated_data = flow.hass.config_entries.async_update_entry.call_args.kwargs[
            "data"
        ]
        updated_options = flow.hass.config_entries.async_update_entry.call_args.kwargs[
            "options"
        ]
        flow.hass.config_entries.async_schedule_reload.assert_called_once_with(
            entry.entry_id
        )
        assert updated_data[CONF_CLIMATE_ENTITIES] == [
            "climate.bedroom",
            "climate.kitchen",
        ]
        assert updated_data[CONF_PRIMARY_CLIMATE_ENTITY] == "climate.bedroom"
        assert CONF_CLIMATE_ENTITIES not in updated_options
        assert CONF_PRIMARY_CLIMATE_ENTITY not in updated_options

    async def test_options_flow_can_clear_all_climate_entities(self) -> None:
        """Verify options flow allows clearing all climate entities to empty list."""
        hass = MagicMock()
        entry = _make_mock_entry(scene_control_types={"s1": "light"})
        entry.data[CONF_CLIMATE_ENTITIES] = ["climate.living_room"]
        entry.data[CONF_PRIMARY_CLIMATE_ENTITY] = "climate.living_room"
        mock_tap = MagicMock()
        mock_tap.resources = {"heating": MagicMock()}
        mock_tap.clear_heating = AsyncMock(
            return_value=HeatingData(active=False, zones={})
        )
        coordinator = MagicMock()
        coordinator.data = {"scenes": {}}
        entry.runtime_data = TewkeData(
            host="192.168.1.50",
            tap=mock_tap,
            coordinator=coordinator,
            scene_control_types={"s1": "light"},
        )

        options_flow = TewkeOptionsFlow()
        options_flow.hass = hass
        with patch.object(TewkeOptionsFlow, "config_entry", new=entry):
            # User clears CONF_CLIMATE_ENTITIES (even if primary was still filled)
            res = await options_flow.async_step_init(
                {
                    CONF_CLIMATE_ENTITIES: [],
                    CONF_PRIMARY_CLIMATE_ENTITY: "climate.living_room",
                }
            )
            assert res["type"] == "create_entry"
            assert res["data"][CONF_CLIMATE_ENTITIES] == []
            assert res["data"][CONF_PRIMARY_CLIMATE_ENTITY] is None

            entry.options = res["data"]
            await async_push_heating_state(hass, entry)
            mock_tap.clear_heating.assert_awaited_once()

    async def test_auto_heat_cool_swing_and_range_deadband(self) -> None:
        """Verify auto/heat_cool active actions, rigid range deadbands, and swing."""
        hass = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())

        range_ac = SimpleNamespace(
            state="heat_cool",
            attributes={
                "friendly_name": "Lounge Heat Pump",
                "current_temperature": 22.0,
                "temperature": None,
                "target_temp_low": 20.0,
                "target_temp_high": 21.5,
                "target_temp_step": 0.5,
                "hvac_action": "cooling",
                "hvac_modes": [
                    "off",
                    "heat",
                    "cool",
                    "heat_cool",
                    "auto",
                    "dry",
                    "fan_only",
                ],
                "min_temp": 16.0,
                "max_temp": 30.0,
                "supported_features": (
                    ClimateEntityFeature.TARGET_TEMPERATURE
                    | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
                    | ClimateEntityFeature.SWING_MODE
                ),
                "swing_mode": "vertical",
                "swing_modes": ["off", "vertical", "both"],
            },
        )
        sched_auto = SimpleNamespace(
            state="auto",
            attributes={
                "friendly_name": "Hall Schedule",
                "current_temperature": 19.5,
                "temperature": None,
                "hvac_action": "heating",
                "hvac_modes": ["off", "heat", "auto"],
                "supported_features": ClimateEntityFeature.TARGET_TEMPERATURE,
            },
        )
        states_map = {"climate.lounge": range_ac, "climate.hall": sched_auto}
        hass.states.get = MagicMock(side_effect=states_map.get)

        entry = _make_mock_entry()
        entry.data[CONF_CLIMATE_ENTITIES] = ["climate.lounge", "climate.hall"]
        entry.data[CONF_PRIMARY_CLIMATE_ENTITY] = "climate.lounge"
        entry.runtime_data = TewkeData(
            host="192.168.1.50",
            tap=MagicMock(resources={"heating": MagicMock()}),
            coordinator=MagicMock(data={}),
            scene_control_types={},
        )

        zones = build_heating_zones(hass, entry)
        lounge = zones["climate.lounge"]
        assert lounge.system_mode == "heat_cool"
        assert lounge.actively_cooling is True
        assert lounge.actively_heating is False
        assert lounge.can_set_temperature is True
        assert lounge.target_temperature == 21.0
        assert (lounge.min_temp, lounge.max_temp) == (17.0, 29.0)
        assert (lounge.swing_mode, lounge.swing_modes) == (
            "vertical",
            ["off", "vertical", "both"],
        )

        hall = zones["climate.hall"]
        assert hall.system_mode == "auto"
        assert hall.actively_heating is True
        assert hall.can_set_temperature is False
        assert hall.target_temperature == 19.5

        # Odd-deadband target 20.5 preserves 1.5C span (19.5..21.0)
        await async_handle_heating_command(
            hass,
            entry,
            HeatingCommandData(
                zone_id="climate.lounge",
                target_temperature=20.5,
                swing_mode="both",
            ),
        )
        hass.services.async_call.assert_any_await(
            "climate",
            "set_temperature",
            {
                "entity_id": "climate.lounge",
                "target_temp_low": 19.5,
                "target_temp_high": 21.0,
            },
            blocking=True,
        )
        hass.services.async_call.assert_any_await(
            "climate",
            "set_swing_mode",
            {"entity_id": "climate.lounge", "swing_mode": "both"},
            blocking=True,
        )

        # Split-leg Z-Wave update stays masked and preserves 1.5C span on next command
        range_ac.attributes["target_temp_low"] = 19.5
        split_zones = build_heating_zones(hass, entry)
        assert split_zones["climate.lounge"].target_temperature == 20.5
        assert split_zones["climate.lounge"].swing_mode == "both"
        assert entry.runtime_data.zone_deadband_spans["climate.lounge"] == 1.5

        hass.services.async_call.reset_mock()
        await async_handle_heating_command(
            hass,
            entry,
            HeatingCommandData(
                zone_id="climate.lounge",
                target_temperature=21.0,
                swing_mode="off",
            ),
        )
        hass.services.async_call.assert_any_await(
            "climate",
            "set_temperature",
            {
                "entity_id": "climate.lounge",
                "target_temp_low": 20.0,
                "target_temp_high": 21.5,
            },
            blocking=True,
        )
        # Delayed intermediate "both" swing report does not clear newer pending "off"
        range_ac.attributes["swing_mode"] = "both"
        assert build_heating_zones(hass, entry)["climate.lounge"].swing_mode == "off"

        range_ac.attributes["target_temp_low"] = 23.0
        range_ac.attributes["target_temp_high"] = 25.0
        assert (
            build_heating_zones(hass, entry)["climate.lounge"].target_temperature
            == 24.0
        )

        # Schedule-only auto -> heat masks can_set_temperature and clears active flame
        await async_handle_heating_command(
            hass,
            entry,
            HeatingCommandData(zone_id="climate.hall", system_mode="heat"),
        )
        unconfirmed_hall = build_heating_zones(hass, entry)["climate.hall"]
        assert unconfirmed_hall.system_mode == "heat"
        assert unconfirmed_hall.can_set_temperature is True
        assert unconfirmed_hall.actively_heating is False

    async def test_range_clamping_wake_observe_and_diagnostics(self) -> None:
        """Verify deferred range wake, dry-mode setpoint, and swing rejection."""
        scheduled_tasks: list[object] = []
        hass = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock(side_effect=scheduled_tasks.append)

        range_ac = SimpleNamespace(
            state="off",
            attributes={
                "friendly_name": "Lounge Heat Pump",
                "current_temperature": 22.0,
                "temperature": 20.0,
                "target_temp_low": None,
                "target_temp_high": None,
                "target_temp_step": 1.0,
                "hvac_modes": ["off", "heat_cool"],
                "min_temp": 16.0,
                "max_temp": 30.0,
                "supported_features": (
                    ClimateEntityFeature.TARGET_TEMPERATURE
                    | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
                    | ClimateEntityFeature.SWING_MODE
                ),
                "swing_mode": "off",
                "swing_modes": ["off", "vertical"],
            },
        )
        single_ac = SimpleNamespace(
            state="dry",
            attributes={
                "friendly_name": "Study Split AC",
                "current_temperature": 21.0,
                "temperature": 20.5,
                "hvac_modes": ["off", "heat_cool", "auto", "dry"],
                "supported_features": ClimateEntityFeature.TARGET_TEMPERATURE,
            },
        )
        states_map = {"climate.lounge": range_ac, "climate.study": single_ac}
        hass.states.get = MagicMock(side_effect=states_map.get)

        entry = _make_mock_entry()
        entry.data[CONF_CLIMATE_ENTITIES] = ["climate.lounge", "climate.study"]
        entry.data[CONF_PRIMARY_CLIMATE_ENTITY] = "climate.lounge"
        entry.runtime_data = TewkeData(
            host="192.168.1.50",
            tap=MagicMock(
                resources={"heating": MagicMock()},
                set_heating=AsyncMock(
                    side_effect=lambda zones, active=True: HeatingData(
                        active=active, zones=zones
                    )
                ),
            ),
            coordinator=MagicMock(data={}),
            scene_control_types={},
        )

        # Wake off -> heat_cool defers range set_temperature until HA reports heat_cool
        with patch(
            "custom_components.tewke.heating.async_track_state_change_event",
            return_value=MagicMock(),
        ) as mock_track:
            async_setup_climate_state_listener(hass, entry)
            state_listener = mock_track.call_args.args[2]

        await async_handle_heating_command(
            hass,
            entry,
            HeatingCommandData(
                zone_id="climate.lounge",
                system_mode="heat_cool",
                target_temperature=17.5,
            ),
        )
        hass.services.async_call.assert_awaited_once_with(
            "climate",
            "set_hvac_mode",
            {"entity_id": "climate.lounge", "hvac_mode": "heat_cool"},
            blocking=True,
        )
        # HA enters heat_cool (stored 20..22); deferred write sets 16..19 (mid 17.5)
        hass.services.async_call.reset_mock()
        for task_coro in scheduled_tasks:
            task_coro.close()
        scheduled_tasks.clear()
        old_lounge = SimpleNamespace(state="off", attributes=dict(range_ac.attributes))
        range_ac.state = "heat_cool"
        range_ac.attributes["target_temp_low"] = 20.0
        range_ac.attributes["target_temp_high"] = 22.0
        state_listener(
            SimpleNamespace(
                data={
                    "entity_id": "climate.lounge",
                    "old_state": old_lounge,
                    "new_state": range_ac,
                }
            )
        )
        for task_coro in scheduled_tasks:
            await task_coro
        scheduled_tasks.clear()
        hass.services.async_call.assert_awaited_once_with(
            "climate",
            "set_temperature",
            {
                "entity_id": "climate.lounge",
                "target_temp_low": 16.0,
                "target_temp_high": 19.0,
            },
            blocking=True,
        )
        # Multi-step 0.5 nudges on step=1.0 never ratchet deadband wider than 2.0C
        range_ac.attributes["target_temp_low"] = 16.0
        range_ac.attributes["target_temp_high"] = 19.0
        hass.services.async_call.reset_mock()
        await async_handle_heating_command(
            hass,
            entry,
            HeatingCommandData(zone_id="climate.lounge", target_temperature=20.0),
        )
        hass.services.async_call.assert_awaited_once_with(
            "climate",
            "set_temperature",
            {
                "entity_id": "climate.lounge",
                "target_temp_low": 19.0,
                "target_temp_high": 21.0,
            },
            blocking=True,
        )

        # Dry-mode entity with TARGET_TEMPERATURE receives scalar set_temperature
        hass.services.async_call.reset_mock()
        await async_handle_heating_command(
            hass,
            entry,
            HeatingCommandData(zone_id="climate.study", target_temperature=22.0),
        )
        hass.services.async_call.assert_awaited_once_with(
            "climate",
            "set_temperature",
            {"entity_id": "climate.study", "temperature": 22.0},
            blocking=True,
        )

        # Rejected swing service clears pending swing mask immediately
        hass.services.async_call = AsyncMock(side_effect=RuntimeError("invalid swing"))
        await async_handle_heating_command(
            hass,
            entry,
            HeatingCommandData(zone_id="climate.lounge", swing_mode="invalid"),
        )
        assert build_heating_zones(hass, entry)["climate.lounge"].swing_mode == "off"
        for task_coro in scheduled_tasks:
            task_coro.close()

    async def test_command_only_observe_fixed_reconcile_and_diagnostics(self) -> None:
        """Verify command-only observe preservation, fixed-clock reconcile, and diag."""
        scheduled_tasks: list[object] = []
        hass = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock(side_effect=scheduled_tasks.append)

        states_map = {
            "climate.lounge": SimpleNamespace(
                state="cool",
                attributes={
                    "friendly_name": "Lounge",
                    "current_temperature": 22.0,
                    "temperature": 20.0,
                    "hvac_modes": ["off", "cool"],
                    "swing_mode": "off",
                    "swing_modes": ["off", "vertical"],
                },
            ),
            "climate.study": SimpleNamespace(
                state="auto",
                attributes={
                    "friendly_name": "Study",
                    "current_temperature": 21.0,
                    "temperature": 20.5,
                    "hvac_modes": ["off", "auto"],
                },
            ),
        }
        hass.states.get = MagicMock(side_effect=states_map.get)

        entry = _make_mock_entry()
        entry.data[CONF_CLIMATE_ENTITIES] = ["climate.lounge", "climate.study"]
        entry.data[CONF_PRIMARY_CLIMATE_ENTITY] = "climate.lounge"
        mock_tap = MagicMock(
            wall_dock_id="dock-1",
            tewke_os_version="2.0.0",
            update_status=UpdateStatus.IDLE,
            resources={"heating": MagicMock()},
            observe=AsyncMock(),
            close_observations=AsyncMock(),
            discover=AsyncMock(),
        )
        coordinator = MagicMock(is_retrying_observe=False, data={})
        entry.runtime_data = TewkeData(
            host="192.168.1.50",
            tap=mock_tap,
            coordinator=coordinator,
            scene_control_types={},
        )

        zones = build_heating_zones(hass, entry)
        coordinator.data = {"heating": HeatingData(active=True, zones=zones)}
        await async_setup_observe(coordinator, hass, entry)
        heating_cb = mock_tap.observe.call_args.kwargs["heating_callback"]
        heating_cb(
            HeatingData(
                active=True,
                zones={},
                command=HeatingCommandData(
                    zone_id="climate.study",
                    system_mode="off",
                ),
            )
        )
        pushed = coordinator.async_set_updated_data.call_args.args[0]["heating"]
        assert set(pushed.zones.keys()) == {"climate.lounge", "climate.study"}

        hass.loop = MagicMock()
        with (
            patch(
                "custom_components.tewke.heating.time.monotonic",
                return_value=100.0,
            ),
            patch(
                "custom_components.tewke.heating.async_call_later",
                return_value=MagicMock(),
            ) as mock_call_later,
        ):
            await async_handle_heating_command(
                hass,
                entry,
                HeatingCommandData(zone_id="climate.lounge", swing_mode="vertical"),
            )
            entry.runtime_data.pending_heating_commands[
                "climate.lounge"
            ].expires_at = 105.0
            await async_handle_heating_command(
                hass,
                entry,
                HeatingCommandData(zone_id="climate.study", system_mode="off"),
            )
            assert abs(mock_call_later.call_args.args[1] - 5.1) < 1e-6

            diag = await async_get_config_entry_diagnostics(hass, entry)
            assert diag["heating"]["configured_climate_entities"] == [
                "climate.lounge",
                "climate.study",
            ]
            assert diag["heating"]["primary_climate_entity"] == "climate.lounge"
            assert "climate.lounge" in diag["heating"]["pending_heating_command_zones"]

        for task_coro in scheduled_tasks:
            task_coro.close()

    async def test_superseded_bounds_off_scalar_and_rejected_mode_guards(
        self,
    ) -> None:
        """Verify superseded range acks, off scalar writes, and rejected mode guards."""
        scheduled_tasks: list[object] = []
        hass = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock(side_effect=scheduled_tasks.append)

        range_ac = SimpleNamespace(
            state="heat_cool",
            attributes={
                "friendly_name": "Lounge",
                "current_temperature": 22.0,
                "temperature": None,
                "target_temp_low": 20.0,
                "target_temp_high": 21.5,
                "target_temp_step": 0.5,
                "hvac_modes": ["off", "heat_cool"],
                "min_temp": 16.0,
                "max_temp": 30.0,
                "supported_features": (
                    ClimateEntityFeature.TARGET_TEMPERATURE
                    | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
                ),
            },
        )
        mqtt_off = SimpleNamespace(
            state="off",
            attributes={
                "friendly_name": "MQTT Heater",
                "current_temperature": 18.0,
                "temperature": 19.0,
                "hvac_modes": ["off", "heat"],
                "supported_features": ClimateEntityFeature.TARGET_TEMPERATURE,
            },
        )
        states_map = {"climate.lounge": range_ac, "climate.mqtt": mqtt_off}
        hass.states.get = MagicMock(side_effect=states_map.get)

        entry = _make_mock_entry()
        entry.data[CONF_CLIMATE_ENTITIES] = ["climate.lounge", "climate.mqtt"]
        entry.runtime_data = TewkeData(
            host="192.168.1.50",
            tap=MagicMock(
                resources={"heating": MagicMock()},
                set_heating=AsyncMock(
                    side_effect=lambda zones, active=True: HeatingData(
                        active=active, zones=zones
                    )
                ),
            ),
            coordinator=MagicMock(data={}),
            scene_control_types={},
        )

        # 1. Request 20.5 (19.5..21.0) then 22.0 (21.5..23.0); delayed 19.5 stays masked
        build_heating_zones(hass, entry)
        await async_handle_heating_command(
            hass,
            entry,
            HeatingCommandData(zone_id="climate.lounge", target_temperature=20.5),
        )
        await async_handle_heating_command(
            hass,
            entry,
            HeatingCommandData(zone_id="climate.lounge", target_temperature=22.0),
        )
        range_ac.attributes["target_temp_low"] = 19.5
        assert (
            build_heating_zones(hass, entry)["climate.lounge"].target_temperature
            == 22.0
        )

        # 2. Temperature-only scalar write while MQTT entity is off still dispatches
        hass.services.async_call.reset_mock()
        await async_handle_heating_command(
            hass,
            entry,
            HeatingCommandData(zone_id="climate.mqtt", target_temperature=21.0),
        )
        hass.services.async_call.assert_awaited_once_with(
            "climate",
            "set_temperature",
            {"entity_id": "climate.mqtt", "temperature": 21.0},
            blocking=True,
        )

        # 3. Rejected off -> heat_cool clears dependent setpoint rather than deferring
        range_ac.state = "off"
        range_ac.attributes["target_temp_low"] = None
        range_ac.attributes["target_temp_high"] = None
        hass.services.async_call = AsyncMock(side_effect=RuntimeError("mode failed"))
        await async_handle_heating_command(
            hass,
            entry,
            HeatingCommandData(
                zone_id="climate.lounge",
                system_mode="heat_cool",
                target_temperature=23.0,
            ),
        )
        assert "climate.lounge" not in entry.runtime_data.pending_heating_commands
        for task_coro in scheduled_tasks:
            task_coro.close()


class _FakeClock:
    """Deterministic monotonic clock and `async_call_later` scheduler for tests."""

    def __init__(self, initial: float = 1000.0) -> None:
        self.now = initial
        self._timers: list[list[object]] = []

    def monotonic(self) -> float:
        return self.now

    def call_later(self, _hass: object, delay: float, job_or_cb: object) -> object:
        entry: list[object] = [self.now + float(delay), job_or_cb, False]
        self._timers.append(entry)

        def _unsub() -> None:
            entry[2] = True

        return _unsub

    def advance(self, seconds: float) -> None:
        target = self.now + seconds
        while True:
            due = [
                e
                for e in self._timers
                if not e[2] and float(e[0]) <= target + 1e-9  # type: ignore[arg-type]
            ]
            if not due:
                break
            due.sort(key=lambda e: float(e[0]))  # type: ignore[arg-type]
            nxt = due[0]
            nxt[2] = True
            self.now = float(nxt[0])  # type: ignore[arg-type]
            target_fn = getattr(nxt[1], "target", nxt[1])
            target_fn(self.now)  # type: ignore[operator]
        self.now = target


def _sample_sensor(iaq: int = 40) -> SensorData:
    return SensorData(
        iaq=iaq,
        rawGas=1000.0,
        staticIaq=iaq,
        iaqAccuracy=3,
        rawHumidity=45.0,
        rawPressure=101325.0,
        runInStatus=True,
        co2Equivalent=500,
        gasPercentage=10,
        rawTemperature=21.0,
        breathVocEquivalent=0.5,
        compensatedHumidity=45.0,
        stabilisationStatus=True,
        compensatedTemperature=21.0,
    )


class TestPerResourceAvailabilityAndRecovery(unittest.IsolatedAsyncioTestCase):
    """Verify 5m grace caching, 30s -> 60s..900s backoff, /config wake, and boot."""

    def _build_real_coordinator(
        self,
        clock: _FakeClock,
        *,
        scene_control_types: dict[str, str] | None = None,
    ) -> tuple[MagicMock, MagicMock, MagicMock, TewkeCoordinator]:
        hass = MagicMock()
        hass.loop = asyncio.get_running_loop()
        hass.async_create_task = asyncio.create_task
        hass.config_entries.async_update_entry = MagicMock()
        entry = _make_mock_entry(
            scene_control_types=scene_control_types or {"s1": "light"}
        )
        mock_tap = MagicMock()
        mock_tap.wall_dock_id = "dock_123"
        mock_tap.tewke_os_version = "2.0.0"
        mock_tap.resources = {
            name: ResourceInfo(path=f"/{name}", rt=name, obs=True)
            for name in ("scenes", "targets", "config", "sensors", "radar", "energy")
        }

        def _set_updated_data(self_coord: TewkeCoordinator, data: object) -> None:
            self_coord.data = data  # type: ignore[assignment]
            self_coord.last_update_success = True
            self_coord.async_update_listeners()

        with patch("homeassistant.helpers.frame.report_usage"):
            coordinator = TewkeCoordinator(
                hass=hass,
                logger=MagicMock(),
                name=DOMAIN,
                call_later_fn=clock.call_later,  # type: ignore[arg-type]
                monotonic_fn=clock.monotonic,
            )
        coordinator.async_set_updated_data = _set_updated_data.__get__(  # type: ignore[method-assign]
            coordinator, TewkeCoordinator
        )
        coordinator.config_entry = entry
        entry.runtime_data = TewkeData(
            host="192.168.1.50",
            tap=mock_tap,
            coordinator=coordinator,
            scene_control_types=dict(scene_control_types or {"s1": "light"}),
        )
        return hass, entry, mock_tap, coordinator

    async def test_scenes_503_grace_window_and_post_grace_backoff(self) -> None:
        """Scenes stay available for 5m at 30s retries despite 10s /sensors pushes."""
        assert [compute_recovery_delay(t, n) for t, n in [(0.0, 0), (299.9, 0)]] == [
            30.0,
            30.0,
        ]
        assert [compute_recovery_delay(300.0, n) for n in range(6)] == [
            60.0,
            120.0,
            240.0,
            480.0,
            900.0,
            900.0,
        ]

        clock = _FakeClock()
        hass, entry, mock_tap, coordinator = self._build_real_coordinator(clock)
        s1 = Scene(id="s1", name="Movie", isActive=True, brightness=70)
        t0 = Target(index=0, name="Desk", isDimmable=True, isOn=True, brightness=50)
        all_res = ["config", "targets", "scenes", "sensors", "radar", "energy"]
        coordinator.data = {
            "scenes": {"s1": s1},
            "scenes_all": {"s1": s1},
            "targets": {0: t0},
            "sensors": _sample_sensor(40),
            "radar": RadarData(proximity="near", screenOn=True),
            "energy": EnergyData(
                power=10.0, actualPower=10.0, override=None, hasOverride=False
            ),
            "energy_override": None,
            "config": ConfigData(
                roomId="r1",
                roomName="Office",
                deviceName="Office Tap",
                hardwareId="dock_123",
                tewkeOsVersion="2.0.0",
                availableResources=all_res,
            ),
            "heating": None,
        }
        for res in all_res:
            coordinator.mark_resource_data_received(res)
            entry.runtime_data.resource_health[res].desired = True
        coordinator.handle_config_available_resources(coordinator.data["config"])

        scene_light = TewkeSceneLight(coordinator, s1)
        target_light = TewkeTargetLight(coordinator, t0)
        retry_timestamps: list[float] = []

        async def _fail_retry(res: str) -> None:
            retry_timestamps.append(clock.now - 1000.0)
            msg = f"coap://192.168.1.50/{res} 5.03"
            raise PyTewkeCoapError(msg, coapCode.SERVICE_UNAVAILABLE)

        mock_tap.retry_observe = AsyncMock(side_effect=_fail_retry)
        mock_ent_reg = MagicMock()

        with patch(
            "custom_components.tewke.util.er.async_get", return_value=mock_ent_reg
        ):
            evict_msg = "coap://192.168.1.50/scenes 5.03"
            coordinator.handle_observation_status(
                ObservationStatusEvent(
                    resource="scenes",
                    status=ObservationStatus.UNAVAILABLE,
                    error=PyTewkeCoapError(evict_msg, coapCode.SERVICE_UNAVAILABLE),
                )
            )
            coordinator.handle_config_available_resources(
                coordinator.data["config"].model_copy(
                    update={
                        "available_resources": [r for r in all_res if r != "scenes"]
                    }
                )
            )

            for step in range(29):
                clock.advance(10.0)
                await asyncio.sleep(0)
                coordinator.mark_resource_data_received("sensors")
                coordinator.async_set_updated_data(
                    {**coordinator.data, "sensors": _sample_sensor(41 + step)}
                )
                assert (scene_light.available, target_light.available) == (True, True)

            assert retry_timestamps == [float(30 * k) for k in range(1, 10)]
            mock_ent_reg.async_remove.assert_not_called()

            clock.advance(10.0)
            await asyncio.sleep(0)
            assert (scene_light.available, target_light.available) == (False, True)
            mock_ent_reg.async_remove.assert_not_called()

            for delta in (60.0, 120.0, 240.0, 480.0, 900.0, 900.0):
                for _ in range(int(delta // 10)):
                    clock.advance(10.0)
                    await asyncio.sleep(0)
                    coordinator.mark_resource_data_received("sensors")
            assert retry_timestamps[9:] == [
                300.0,
                360.0,
                480.0,
                720.0,
                1200.0,
                2100.0,
                3000.0,
            ]

            diag = await async_get_config_entry_diagnostics(hass, entry)
            assert diag["device"]["wall_dock_id"] == "**REDACTED**"
            scenes_diag = diag["device"]["resource_health"]["scenes"]
            assert not scenes_diag["available"]
            assert "192.168.1.50" not in (scenes_diag["last_error"] or "")

        coordinator.cancel_observation_timeout()

    async def test_mid_flight_config_wake_requests_immediate_followup(self) -> None:
        """Edge-triggered /config push wakes unhealthy /scenes immediately."""
        clock = _FakeClock()
        _, entry, mock_tap, coordinator = self._build_real_coordinator(clock)
        s1 = Scene(id="s1", name="Movie", isActive=True, brightness=70)
        base_cfg = ConfigData(
            roomId="r1",
            roomName="Office",
            deviceName="Office Tap",
            hardwareId="dock_123",
            tewkeOsVersion="2.0.0",
            availableResources=["config", "targets"],
        )
        coordinator.data = {
            "scenes": {"s1": s1},
            "scenes_all": {"s1": s1},
            "targets": {},
            "sensors": None,
            "radar": None,
            "energy": None,
            "energy_override": None,
            "config": base_cfg,
            "heating": None,
        }
        entry.runtime_data.resource_health["scenes"] = ResourceHealthState(
            desired=True,
            active=False,
            has_data=True,
            unhealthy_since=clock.now - 600.0,
            post_grace_attempts=4,
            next_retry_at=clock.now + 900.0,
        )
        coordinator.handle_config_available_resources(base_cfg)
        scene_light = TewkeSceneLight(coordinator, s1)
        assert not scene_light.available

        in_flight_gate = asyncio.Event()
        attempts_on_wake = 0

        async def _mid_flight_then_recover(_res: str) -> None:
            nonlocal attempts_on_wake
            attempts_on_wake += 1
            if attempts_on_wake == 1:
                await in_flight_gate.wait()
                err_msg = "raced"
                raise TimeoutError(err_msg)
            coordinator.mark_resource_data_received("scenes")
            coordinator.handle_observation_status(
                ObservationStatusEvent(
                    resource="scenes", status=ObservationStatus.ACTIVE
                )
            )

        mock_tap.retry_observe = AsyncMock(side_effect=_mid_flight_then_recover)
        woke_cfg = base_cfg.model_copy(
            update={"available_resources": ["config", "targets", "scenes"]}
        )
        coordinator.handle_config_available_resources(woke_cfg)
        clock.advance(0.0)
        await asyncio.sleep(0)

        # Second edge while attempt 1 is still awaiting sets wake_requested.
        coordinator._prev_available_resources = frozenset({"config", "targets"})
        coordinator.handle_config_available_resources(woke_cfg)
        in_flight_gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert attempts_on_wake == 2
        assert scene_light.available
        coordinator.cancel_observation_timeout()

    async def test_cold_boot_scenes_503_preserves_registry_names_and_hydrates(
        self,
    ) -> None:
        """Cold boot with /scenes 5.03 keeps stored names unavailable then hydrates."""
        clock = _FakeClock()
        hass, entry, mock_tap, coordinator = self._build_real_coordinator(
            clock, scene_control_types={"s1": "light"}
        )
        t0 = Target(index=0, name="Ceiling", isDimmable=True, isOn=True, brightness=80)
        mock_tap.close_observations = AsyncMock()
        mock_tap.desired_observations = frozenset({"scenes", "targets", "config"})
        mock_tap.active_observations = frozenset({"targets", "config"})
        mock_tap.get_targets = AsyncMock(return_value={0: t0})
        mock_tap.get_scenes = AsyncMock()
        mock_tap.get_sensors = AsyncMock(return_value=_sample_sensor())
        mock_tap.get_radar = AsyncMock(
            return_value=RadarData(proximity="none", screenOn=False)
        )
        mock_tap.get_energy = AsyncMock(
            return_value=EnergyData(
                power=5.0, actualPower=5.0, override=None, hasOverride=False
            )
        )
        mock_tap.get_energy_override = AsyncMock(return_value=None)
        mock_tap.get_config = AsyncMock(
            return_value=ConfigData(
                roomId="r1",
                roomName="Bedroom",
                deviceName="Bedroom Tap",
                hardwareId="dock_123",
                tewkeOsVersion="2.0.0",
                availableResources=["config", "targets"],
            )
        )

        captured_scene_cb: list[object] = []

        async def _fake_observe(**kwargs: object) -> None:
            status_cb = kwargs["observation_status_callback"]
            captured_scene_cb.append(kwargs["scene_callback"])
            status_cb(  # type: ignore[operator]
                ObservationStatusEvent(
                    resource="scenes",
                    status=ObservationStatus.UNAVAILABLE,
                    error=PyTewkeCoapError("5.03", coapCode.SERVICE_UNAVAILABLE),
                )
            )
            kwargs["target_callback"]({0: t0})  # type: ignore[operator]
            status_cb(  # type: ignore[operator]
                ObservationStatusEvent(
                    resource="targets", status=ObservationStatus.ACTIVE
                )
            )
            status_cb(  # type: ignore[operator]
                ObservationStatusEvent(
                    resource="config", status=ObservationStatus.ACTIVE
                )
            )

        mock_tap.observe = AsyncMock(side_effect=_fake_observe)

        data = await coordinator._async_update_data()
        coordinator.data = data
        mock_tap.get_scenes.assert_not_awaited()

        mock_ent_reg = MagicMock()
        mock_ent_reg.async_get_entity_id.return_value = "light.bedroom_evening_glow"
        mock_ent_reg.async_get.return_value = SimpleNamespace(
            original_name="Evening Glow"
        )
        added_entities: list[object] = []

        with (
            patch(
                "custom_components.tewke.scene.er.async_get", return_value=mock_ent_reg
            ),
            patch(
                "custom_components.tewke.light.async_dispatcher_connect",
                return_value=lambda: None,
            ),
        ):
            await async_setup_light_entry(hass, entry, added_entities.extend)

        scene_entities = [e for e in added_entities if isinstance(e, TewkeSceneLight)]
        target_entities = [e for e in added_entities if isinstance(e, TewkeTargetLight)]
        assert len(scene_entities) == 1
        assert len(target_entities) == 1
        assert scene_entities[0].name == "Evening Glow"
        assert not scene_entities[0].available
        assert target_entities[0].available

        # Hydrate when /scenes recovers!
        with (
            patch(
                "custom_components.tewke.util.er.async_get", return_value=mock_ent_reg
            ),
            patch("custom_components.tewke.util.ir.async_delete_issue"),
        ):
            captured_scene_cb[0](  # type: ignore[operator]
                {
                    "s1": Scene(
                        id="s1", name="Evening Glow", isActive=True, brightness=65
                    )
                }
            )
        assert scene_entities[0].available
        assert scene_entities[0].is_on
        coordinator.cancel_observation_timeout()

    async def test_sensors_503_skips_watchdog_and_heating_recovery_pushes_state(
        self,
    ) -> None:
        """Sensors 5.03 skips 30s teardown; active silence enters grace; HVAC syncs."""
        clock = _FakeClock()
        hass, entry, mock_tap, coordinator = self._build_real_coordinator(clock)
        mock_tap.resources["heating"] = ResourceInfo(
            path="/heating", rt="heating", obs=True
        )
        coordinator.data = {
            "scenes": {},
            "scenes_all": {},
            "targets": {
                0: Target(
                    index=0, name="Main", isDimmable=True, isOn=False, brightness=0
                )
            },
            "sensors": _sample_sensor(),
            "radar": RadarData(proximity="near", screenOn=True),
            "energy": EnergyData(
                power=4.0, actualPower=4.0, override=None, hasOverride=False
            ),
            "energy_override": EnergyOverrideData(override=None, active=False),
            "config": ConfigData(
                roomId="r1",
                roomName="Hall",
                deviceName="Hall Tap",
                hardwareId="dock_123",
                tewkeOsVersion="2.0.0",
                updateStatus=UpdateStatus.IDLE,
            ),
            "heating": HeatingData(active=False, zones={}),
        }
        for res in ("targets", "config", "radar", "energy", "heating"):
            coordinator.mark_resource_data_received(res)
            entry.runtime_data.resource_health[res].desired = True

        # 1. Sensors UNAVAILABLE (BME680 warm-up): 60s silence keeps siblings active.
        entry.runtime_data.resource_health["sensors"] = ResourceHealthState(
            desired=True, active=False, has_data=False, unhealthy_since=clock.now
        )
        coordinator.reset_observation_timeout()
        clock.advance(60.0)
        assert entry.runtime_data.resource_health["targets"].active
        assert entry.runtime_data.resource_health["radar"].active

        # Per-entity availability matrix check across platforms.
        bme_sensor = TewkeSensor(coordinator, SENSOR_DESCRIPTIONS[0])
        bme_bin = TewkeBinarySensor(coordinator, BINARY_SENSOR_DESCRIPTIONS[0])
        radar_sensor = TewkeRadarSensor(coordinator, RADAR_SENSOR_DESCRIPTIONS[0])
        screen_bin = TewkeScreenBinarySensor(coordinator)
        energy_sensor = TewkeEnergySensor(coordinator, ENERGY_SENSOR_DESCRIPTIONS[0])
        config_sensor = TewkeConfigSensor(coordinator, CONFIG_SENSOR_DESCRIPTIONS[0])
        override_num = TewkeEnergyOverrideNumber(coordinator)
        restart_btn = TewkeRestartButton(coordinator)
        assert not bme_sensor.available
        assert not bme_bin.available
        assert radar_sensor.available
        assert screen_bin.available
        assert energy_sensor.available
        assert config_sensor.available
        assert override_num.available
        assert restart_btn.available

        # 2. Active sensors + 30s total silence enters 5m grace window and retries.
        udp_err = "udp dropped"
        mock_tap.retry_observe = AsyncMock(side_effect=TimeoutError(udp_err))
        coordinator.mark_resource_data_received("sensors")
        assert bme_sensor.available
        clock.advance(30.0)
        await asyncio.sleep(0)
        assert not entry.runtime_data.resource_health["targets"].active
        assert radar_sensor.available
        assert bme_sensor.available

        # 3. Real Observe: 0 recovery order (payload before ACTIVE) resyncs once!
        with patch(
            "custom_components.tewke.coordinator.async_push_heating_state",
            new=AsyncMock(),
        ) as mock_push_heating:
            coordinator.mark_resource_data_received("heating")
            coordinator.handle_observation_status(
                ObservationStatusEvent(
                    resource="heating", status=ObservationStatus.ACTIVE
                )
            )
            await asyncio.sleep(0)
            mock_push_heating.assert_awaited_once_with(hass, entry)

        coordinator.cancel_observation_timeout()

    async def test_cold_boot_205_reconciles_scenes_and_unload_aborts_timers(
        self,
    ) -> None:
        """Healthy 2.05 boot reconciles scenes; unload stops timer resurrection."""
        clock = _FakeClock()
        hass, entry, mock_tap, coordinator = self._build_real_coordinator(
            clock, scene_control_types={"s_deleted": "light", "s_kept": "light"}
        )
        mock_tap.resources["energy"] = ResourceInfo(
            path="/energy", rt="energy", obs=False
        )
        t0 = Target(index=0, name="Desk", isDimmable=True, isOn=True, brightness=50)
        s_kept = Scene(id="s_kept", name="Kept", isActive=False, brightness=40)
        s_new = Scene(id="s_new", name="Added Offline", isActive=True, brightness=90)
        mock_tap.close_observations = AsyncMock()
        mock_tap.desired_observations = frozenset({"scenes", "targets", "config"})
        mock_tap.active_observations = frozenset({"scenes", "targets", "config"})
        mock_tap.wall_dock = SimpleNamespace(
            scenes={"s_kept": s_kept, "s_new": s_new}, scenes_loaded=True
        )
        mock_tap.get_targets = AsyncMock(return_value={0: t0})
        for attr in ("get_sensors", "get_radar", "get_energy_override", "get_config"):
            setattr(mock_tap, attr, AsyncMock(return_value=None))
        mock_tap.get_energy = AsyncMock(
            return_value=EnergyData(
                power=8.0, actualPower=8.0, override=None, hasOverride=False
            )
        )

        async def _healthy_observe(**kwargs: object) -> None:
            status_cb = kwargs["observation_status_callback"]
            kwargs["scene_callback"]({"s_kept": s_kept, "s_new": s_new})  # type: ignore[operator]
            kwargs["target_callback"]({0: t0})  # type: ignore[operator]
            for res in ("scenes", "targets"):
                status_cb(  # type: ignore[operator]
                    ObservationStatusEvent(
                        resource=res, status=ObservationStatus.ACTIVE
                    )
                )

        mock_tap.observe = AsyncMock(side_effect=_healthy_observe)
        mock_ent_reg = MagicMock()
        mock_ent_reg.async_get_entity_id.return_value = "light.deleted_scene"

        with (
            patch(
                "custom_components.tewke.util.er.async_get", return_value=mock_ent_reg
            ),
            patch("custom_components.tewke.util.ir.async_create_issue") as mock_issue,
        ):
            data = await coordinator._async_update_data()
            coordinator.data = data

        mock_ent_reg.async_remove.assert_called_once_with("light.deleted_scene")
        assert (set(data["scenes"]), "s_new" in entry.runtime_data.pending_scenes) == (
            {"s_kept"},
            True,
        )
        mock_issue.assert_called_once()

        coordinator.mark_resource_data_received("energy")
        entry.runtime_data.resource_health["sensors"] = ResourceHealthState(
            desired=True,
            active=False,
            has_data=False,
            unhealthy_since=clock.now,
            next_retry_at=clock.now + 30.0,
            last_error="coap://[fd00::abcd]:5683/sensors failed for dock_123",
        )
        diag = await async_get_config_entry_diagnostics(hass, entry)
        assert diag["device"]["resource_health"]["energy"]["available"]
        sanitised = diag["device"]["resource_health"]["sensors"]["last_error"] or ""
        assert ("fd00::abcd" in sanitised, "dock_123" in sanitised) == (False, False)

        hang_gate = asyncio.Event()

        async def _hang(_r: str) -> None:
            await hang_gate.wait()

        mock_tap.retry_observe = AsyncMock(side_effect=_hang)
        coordinator._reschedule_retry_timer()
        clock.advance(30.0)
        await asyncio.sleep(0)
        assert "sensors" in coordinator._get_retry_tasks()

        coordinator.cancel_observation_timeout()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert (coordinator._recovery_retry_unsub, coordinator._legacy_poll_unsub) == (
            None,
            None,
        )

    async def test_reconfigure_and_options_flow_during_scenes_503(self) -> None:
        """Reconfigure and Options flows preserve scenes when /scenes returns 5.03."""
        # 1. ConfigFlow reconfigure step tolerates 5.03 without wiping control types.
        flow = TewkeConfigFlow()
        flow.hass = MagicMock()
        flow.context = {"source": SOURCE_RECONFIGURE}
        flow._discovered_host = "192.168.1.50"
        flow._discovered_name = "Office Tap"
        flow._scene_control_types = {"s_fan": "fan"}
        flow._disabled_scenes = ["s_disabled"]
        flow._default_scene_fan_dimming = {"s_fan": 65}
        mock_tap = MagicMock()
        mock_tap.resources = {"config": MagicMock()}
        mock_tap.get_scenes = AsyncMock(
            side_effect=PyTewkeCoapError("5.03", coapCode.SERVICE_UNAVAILABLE)
        )
        flow._tap = mock_tap
        flow._async_next_after_scenes = AsyncMock(return_value={"type": "create_entry"})  # type: ignore[method-assign]

        res = await flow.async_step_confirm_control_types()
        assert res["type"] == "create_entry"
        assert flow._scene_control_types == {"s_fan": "fan"}
        assert flow._disabled_scenes == ["s_disabled"]
        assert flow._default_scene_fan_dimming == {"s_fan": 65}

        # 2. OptionsFlow falls back to EntityRegistry names when scenes are offline.
        entry = _make_mock_entry(scene_control_types={"s_fan": "fan"})
        entry.data["default_scene_fan_dimming"] = {"s_fan": 70}
        coordinator = MagicMock()
        coordinator.data = {"scenes": {}}
        entry.runtime_data = TewkeData(
            host="192.168.1.50",
            tap=MagicMock(wall_dock_id="dock_123", resources={}),
            coordinator=coordinator,
            scene_control_types={"s_fan": "fan"},
        )
        opt_flow = TewkeOptionsFlow()
        opt_flow.hass = MagicMock()
        mock_ent_reg = MagicMock()
        mock_ent_reg.async_get_entity_id.return_value = "fan.office_ceiling_fan"
        mock_ent_reg.async_get.return_value = SimpleNamespace(
            original_name="Ceiling Fan"
        )
        with (
            patch.object(
                TewkeOptionsFlow,
                "config_entry",
                new_callable=unittest.mock.PropertyMock,
                return_value=entry,
            ),
            patch(
                "custom_components.tewke.config_flow.er.async_get",
                return_value=mock_ent_reg,
            ),
        ):
            form = await opt_flow.async_step_init()
            assert form["type"] == "form"
            submitted = await opt_flow.async_step_init({"Ceiling Fan": 85})
            assert submitted["type"] == "create_entry"
            assert submitted["data"]["default_scene_fan_dimming"] == {"s_fan": 85}


if __name__ == "__main__":
    unittest.main()
