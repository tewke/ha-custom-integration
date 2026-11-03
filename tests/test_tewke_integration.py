"""Unit tests for the Tewke custom integration."""

# ruff: noqa: PLR2004, PT027, S101, SLF001

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import CONF_HOST, CONF_NAME, EntityCategory
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from pytewke.data import (
    ConfigData,
    EnergyData,
    EnergyOverrideData,
    RadarData,
    Scene,
    SensorData,
    Target,
    UpdateStatus,
)
from pytewke.error import PyTewkeCoapError, PyTewkeDiscoveryError

from custom_components.tewke import async_reload_entry, async_setup_entry
from custom_components.tewke.binary_sensor import TewkeScreenBinarySensor
from custom_components.tewke.button import TewkeRestartButton
from custom_components.tewke.config_flow import (
    TewkeConfigFlow,
    _build_unique_scene_labels,
)
from custom_components.tewke.const import DOMAIN
from custom_components.tewke.coordinator import TewkeCoordinator
from custom_components.tewke.data import TewkeData
from custom_components.tewke.diagnostics import async_get_config_entry_diagnostics
from custom_components.tewke.number import TewkeEnergyOverrideNumber
from custom_components.tewke.scene import (
    TewkeSceneFan,
    TewkeSceneLight,
    TewkeSceneSwitch,
)
from custom_components.tewke.sensor import (
    CONFIG_SENSOR_DESCRIPTIONS,
    RADAR_SENSOR_DESCRIPTIONS,
    TewkeConfigSensor,
    TewkeRadarSensor,
)
from custom_components.tewke.sensor import (
    async_setup_entry as async_setup_sensor_entry,
)
from custom_components.tewke.target import TewkeTargetLight
from custom_components.tewke.util import (
    _ha_to_tewke_brightness,
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
        entry.runtime_data = SimpleNamespace(coordinator=MagicMock())

        await async_reload_entry(hass, entry)

        entry.runtime_data.coordinator.async_update_listeners.assert_called_once()

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

        coordinator.reset_observation_timeout.reset_mock()
        coordinator.async_set_updated_data.reset_mock()

        # Identical sensor data resets timeout without firing async_set_updated_data
        sensor_cb(sensor)
        coordinator.reset_observation_timeout.assert_called_once()
        coordinator.async_set_updated_data.assert_not_called()

        # Updated sensor data fires both
        updated_sensor = sensor.model_copy(update={"iaq": 75})
        sensor_cb(updated_sensor)
        assert coordinator.reset_observation_timeout.call_count == 2
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
        coordinator.async_add_listener = lambda cb: listeners.append(cb) or (
            lambda: None
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


if __name__ == "__main__":
    unittest.main()
