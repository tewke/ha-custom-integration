# Tewke ↔ Home Assistant System Analysis

This document captures the end-to-end architecture across **`switch-ui`** (Tewke Tap Panel application), **`pytewke`** (Python CoAP client library), and **`hacs-integration`** (Tewke Home Assistant custom integration), along with an analysis of the **`switch-ui` Heating Screen** to prepare for integrating it with Home Assistant heating functionality.

---

## 1. End-to-End System Overview

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                          Tewke Tap Panel (`../switch-ui`)                           │
│                                                                                     │
│  ┌──────────────────────┐   ┌───────────────────────┐   ┌────────────────────────┐  │
│  │  Hardware / Local    │   │   Domain Services &   │   │     Presentation       │  │
│  │  - WallModule (D-Bus)│◄─►│   Repositories        │◄─►│  - Buttons / Scenes    │  │
│  │  - BME680 / ALS /    │   │  - HomeRepository     │   │  - Energy Screen       │  │
│  │    Radar             │   │  - SceneRepository    │   │  - Heating Screen      │  │
│  │  - NeoHub (WSS:4243) │   │  - EnergyOverrideSvc  │   │    (HeatingCubit /     │  │
│  │  - Sonos / Shelly    │   │  - HeatmiserStateSync │   │     ThermostatAction)  │  │
│  └──────────────────────┘   └───────────┬───────────┘   └────────────────────────┘  │
│                                         │                                           │
│                             ┌───────────▼───────────┐                               │
│                             │     HaCoapService     │                               │
│                             │  CoapServer (UDP:5683)│                               │
│                             │  Avahi mDNS Publisher │                               │
│                             └───────────┬───────────┘                               │
└─────────────────────────────────────────┼───────────────────────────────────────────┘
                                          │
                     Local LAN: UDP CoAP (:5683, CBOR payloads)
                     RFC 6690 Discovery (`/.well-known/core`)
                     RFC 7641 Observe (CON notifications + ACK)
                     mDNS: `_tewke-coap._udp.local.`
                                          │
┌─────────────────────────────────────────▼───────────────────────────────────────────┐
│                         Python Client (`../pytewke`)                                │
│                                                                                     │
│  `Tap` ──► `TewkeCoap` (aiocoap + cbor2) & `ObservationManager`                     │
│  Pydantic v2 models (`ConfigData`, `Scene`, `Target`, `SensorData`, `RadarData`,    │
│  `EnergyData`, `EnergyOverrideData`) + `WallDock` state tracking                    │
└─────────────────────────────────────────┬───────────────────────────────────────────┘
                                          │
┌─────────────────────────────────────────▼───────────────────────────────────────────┐
│                Home Assistant Custom Integration (`./custom_components/tewke`)      │
│                                                                                     │
│  - `TewkeConfigFlow` (Zeroconf + Manual IP + Reconfigure + OptionsFlow)             │
│  - `TewkeNewSceneRepairFlow` (Dynamic scene discovery & platform assignment)        │
│  - `TewkeCoordinator` (Push-first via CoAP Observe + 30s watchdog + 5m fallback)    │
│  - Entities: `light`, `fan`, `switch`, `sensor`, `binary_sensor`, `number`, `button`│
└─────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. `../switch-ui` — Tewke Tap Panel Application

### 2.1 Architecture & DI Overview
- **Framework**: Flutter application running on Flutter-Pi (`lib/`).
- **Clean Architecture Layers**:
  - `lib/app/`: Bootstrap (`main.dart`), GetIt service locator (`injection_container.dart`), mode configuration (`dev/app_config.dart`), and online app screen orchestration (`initialisation/`).
  - `lib/core/data/`: Models (`@JsonSerializable()`), repository implementations, hardware/network services (D-Bus, BlueZ, Avahi mDNS, CoAP-over-WebSocket cloud client, NeoHub, Shelly, Sonos).
  - `lib/core/domain/`: Entities, repository interfaces, domain services (`AppStateService`, `ConfigurationService`, `SettingsSyncService`, `EnergyOverrideService`, `HeatmiserStateSyncService`, `LocalDevicesService`).
  - `lib/features/`: Self-contained feature modules (`ha_coap`, `coap_server`, `heating`, `mock_heating`, `energy`, `buttons`, `mqtt_home_assistant`, `lan_http_server`, etc.).

### 2.2 Local Home Assistant Interfaces in `switch-ui`
`switch-ui` contains three local network integrations (configured via `ConfigurationService` and synced with cloud `HomeAssistantSettings`):

1. **`HaCoapService` (`lib/features/ha_coap/` + `lib/features/coap_server/`) — Primary HA Integration**:
   - Runs a local UDP CoAP server on port **`5683`** (`ipFilter: isPrivateIp`).
   - Advertises `_tewke-coap._udp` (`.local`) via Avahi D-Bus (`AvahiServiceImpl`) with TXT records: `path=/`, `version=1.0`, `observe=true`, `name`, `hardwareId`, `home`, `room`. Dynamically republishes mDNS when the wall module name or home/room assignment changes.
   - **Lifecycle**: Managed by `AppStateService` + `ConfigurationService.haCoapEnabled`. Starts **3 seconds** after entering `AppStateInitialised`; uses a **5-minute grace timer** before shutting down when leaving `AppStateInitialised` so brief Wi-Fi/cloud blips do not disrupt local CoAP control.
2. **`MqttHomeAssistantService` (`lib/features/mqtt_home_assistant/`) — Legacy/Optional MQTT Discovery**:
   - Connects to a user-configured MQTT broker and publishes Home Assistant MQTT Discovery configs (`homeassistant/<component>/<hardwareId>/<entity>/config`) and state/command topics for targets, scenes, BME680/ALS/radar/screen sensors, power, and energy override (`tewke/energy/<id>/override/set`).
3. **`LanHttpServer` (`lib/features/lan_http_server/`) — Legacy/Auxiliary HTTP Server**:
   - Binds TCP port **`8080`** (`_tewke_control._tcp`), providing read-only JSON snapshots (`/config`, `/state`, `/state/poll`, `/sensors`, `/energy`) and MQTT credential bootstrapping (`GET/POST /mqtt/config`).

### 2.3 CoAP Server Framework & Endpoints (`lib/features/coap_server/` & `lib/features/ha_coap/`)
- **Wire Format**:
  - Responses and Observe notifications are encoded as **CBOR** (`application/cbor`, content-format `60`).
  - Requests (`CoapServerRequest.decodePayload()`) accept CBOR (content-format `60`) or UTF-8 JSON fallback.
  - Supports **RFC 6690** discovery (`GET /.well-known/core`), **RFC 7959** block-wise transfers (`Block1` & `Block2` up to 1024-byte blocks / 1 MB max), **RFC 7967** `No-Response`, and **RFC 7641** `Observe`.
- **CoAP Observe (`CoapObserveManager`)**:
  - Tracks up to 100 observers per resource keyed by `"$clientAddress:$clientPort:$tokenHex"`.
  - Calls `handler.onFirstObserverAdded()` when observer count goes `0 -> 1` and `handler.onLastObserverRemoved()` on `1 -> 0` (used by `/sensors` and `/energy` to lazy-start timers/streams).
  - Sends Confirmable (`CON`) `2.05 Content` notifications (`Max-Age: 60`) with exponential backoff retransmission (up to 6 retries before evicting an unresponsive client) and supersedes in-flight unacknowledged notifications when newer state arrives.

#### Registered CoAP Routes (`HaCoapService`)

| Path | `rt` / `if` | Methods | Request Payload | Response / Observe Payload |
| :--- | :--- | :--- | :--- | :--- |
| `/.well-known/core` | — | `GET` | Optional `?rt=`, `?if=` | RFC 6690 Link-Format string (`application/link-format`) |
| `/config` | `config` / `core.r` | `GET`, `Observe` | None | `{"hardwareId": str?, "deviceName": str?, "roomId": str?, "roomName": str?, "tewkeOsVersion": str?}` |
| `/state` | `state` / `core.r` | `GET` | None | Full snapshot: `targets`, `scenes`, `sensors`, `ambientLight`, `screenOn`, `radar`, `energy` |
| `/targets` | `targets` / `core.rw` | `GET`, `POST`, `Observe` | `{"index": int, "brightness": int? (0-100), "state": "ON"\|"OFF"?}` | **GET/Obs**: `{"0": {"name": str?, "isDimmable": bool, "brightness": int, "isOn": bool}, ...}`<br>**POST (`2.04`)**: `{"index": int, "brightness": int?, "isOn": bool}` |
| `/scenes` | `scenes` / `core.a` | `GET`, `POST`, `Observe` | `{"sceneId": str, "isActive": bool, "brightness": num? (0-100)}` | **GET/Obs**: `{"<sceneId>": {"name": str, "isActive": bool, "brightness": int}, ...}`<br>**POST (`2.04`)**: `{"sceneId": str, "isActive": bool, "brightness": num?}` *(includes local-first fallback to wired `GangController`s if cloud is unreachable)* |
| `/sensors` | `sensors` / `core.s` | `GET`, `Observe` | None | BME680 metrics (`iaq`, `compensatedTemperature`, `compensatedHumidity`, `co2Equivalent`, `rawPressure`, etc.) + `ambientLight: {"lux": float?}` (10s lazy poll when observed) |
| `/radar` | `radar` / `core.s` | `GET`, `Observe` | None | `{"screenOn": bool, "proximity": "none"\|"near"\|"far", "thresholds": {"near": {...}, "far": {...}}}` |
| `/energy` | `energy` / `core.s` | `GET`, `Observe` | None | `{"power": float?, "actualPower": float?, "override": float?, "hasOverride": bool}` |
| `/energy-override` | `energy-override` / `core.a` | `GET`, `POST` | `{"override": float? (>=0, or null to clear)}` | `{"override": float?, "active": bool}` |
| `/restart` | `restart` / `core.a` | `POST` | None | `{"restarting": true}` (triggers `Reboot` usecase) |

### 2.4 Precedent: How `energy-override` Pushes HA Data into a `switch-ui` Screen
The `energy-override` feature is an existing pattern where Home Assistant pushes data to override/drive a UI screen on the Tap Panel:
1. **`EnergyOverrideService`** (`lib/core/domain/services/energy_override_service.dart`): Singleton domain service holding `double? currentEnergyOverride` and broadcasting `Stream<double?> energyOverrideStream`.
2. **`EnergyOverrideRouteHandler`** (`lib/features/ha_coap/handlers/energy_override_route_handler.dart`): Handles `GET /energy-override` and `POST /energy-override` (`{"override": watts}`), updating `EnergyOverrideService`.
3. **`EnergyRouteHandler`** (`lib/features/ha_coap/handlers/energy_route_handler.dart`): Combines `PowerMonitoringService.powerStream` and `EnergyOverrideService.energyOverrideStream` so `/energy` reports `power: override ?? actualPower`, `actualPower`, `override`, `hasOverride`.
4. **`HomeEnergyDataCubit`** (`lib/features/energy/presentation/energy/bloc/home_energy_data/home_energy_data_cubit.dart`): Subscribes to `energyOverrideService.energyOverrideStream` and displays `_overrideValue ?? _apiValue` on the Energy screen.
5. **Home Assistant (`custom_components/tewke/number.py`)**: Exposes `TewkeEnergyOverrideNumber` (`NumberEntity` in watts) which calls `tap.set_energy_override(...)`.

---

## 3. Heating & Climate Architecture in `../switch-ui`

### 3.1 Screen Selection & Navigation
1. **Cloud View Assignment**: Each `WallModule` in `/home/dto` has a `views` list (`List<TapScreen>`). A `"CLIMATE_CONTROL"` entry maps to `TapScreenType.climateControl`.
2. **Feature Flag Filtering (`GenerateScreensCubit`)** (`lib/app/initialisation/app_modes/online_app/bloc/generate_screens/generate_screens_cubit.dart`):
   - `climateControlScreen == false` → removes `TapScreenType.climateControl`.
   - `climateControlScreen == true && heatmiserIntegration == false` → replaces `TapScreenType.climateControl` with `TapScreen.demoClimateControl()` (`MockHeatingScreen`).
   - `climateControlScreen == true && heatmiserIntegration == true` → keeps `TapScreenType.climateControl` (`HeatingScreen`).
3. **BLoC Provisioning (`ReloadScreensManager` & `OnlineAppScreens`)**:
   - `TapScreenType.climateControl` wraps the screen tree in `HeatingBlocWrapper` (`HeatingCubit` + `ThermostatActionCubit`) and renders `HeatingScreen`.
   - `TapScreenType.demoClimateControl` wraps the screen tree in `MockHeatingBlocWrapper` (`MockHeatingCubit`) and renders `MockHeatingScreen`.

### 3.2 `lib/features/heating/` (Production Heating Feature)
Currently, `lib/features/heating/` is tightly coupled to **Heatmiser NeoHub** entities and services:

#### State & Domain Model (`HeatingState`, `HeatingZone`, `ThermostatSystemState`)
- **`ThermostatSystemState`** (`lib/features/heating/presentation/cubit/cubit/thermostat_system_state.dart`):
  - Enum with two values: `heat` (`Assets.thermostat.flame`, `'Heat'`) and `off` (`Assets.general.power`, `'Off'`).
- **`HeatingZone`** (`lib/features/heating/presentation/cubit/cubit/heating_zone.dart`):
  - `thermostat`: `Device` (expected to be `Device<HeatmiserThermostatMetadata>` — note: `HeatingScreenInitial`, `HeatingScreenLoaded`, `HeatingScreenEditTemperature`, and `SystemModeSelectorScreen` cast `activeZone.thermostat.metadata as HeatmiserThermostatMetadata` to get `.name` for `zoneName`!).
  - `isThisRoom`: `bool` (whether the thermostat is in the same room as this Tap).
  - `currentTemperature`: `double`
  - `targetTemperature`: `double`
  - `systemMode`: `ThermostatSystemState` (`heat` or `off`; forced to `off` if `heatmiserAwayMode || heatmiserHolidayEnd != null`).
  - `frostProtectionTemperature`: `double?`
  - `activeProfile`: `HeatmiserProfile?` (schedule comfort levels used by `StatusChip` and `cancelOverride`).
  - `heatmiserHolidayEnd`: `DateTime?`
  - `heatmiserAwayMode`: `bool`
  - `online`: `bool`
  - `lastSeen`: `DateTime?`
  - `heatOn`: `bool`
  - `activelyHeating`: `bool`
  - `hold`: `HeatmiserHold?` (`endTime`, `temperature`).
- **`HeatingState`** (`lib/features/heating/presentation/cubit/cubit/heating_state.dart`):
  - `HeatingStateInitial`
  - `HeatingStateInitialising`
  - `HeatingStateLoaded({zones, selectedThermostatId, error, isHubOffline, isPendingSync, optimisticSystemMode, optimisticTargetTemp})`
  - `HeatingStateLoadedNoZones`
  - `HeatingStateNoHubFound`
  - `HeatingStateNeoHubCommunicationError({message})`

#### Cubits (`HeatingCubit` & `ThermostatActionCubit`)
- **`HeatingCubit`** (`lib/features/heating/presentation/cubit/cubit/heating_cubit.dart`):
  - **Dependencies**: `OnlineAppCubit`, `HeatmiserStateSyncService`, `StateRepository`, `AnalyticsService`.
  - **Data Sources**:
    1. **Local NeoHub Polling**: When `HeatingScreen` mounts (`startSyncingData()`), checks `heatmiserStateService.selectedNeoHub` (emitting `HeatingStateNoHubFound` if null), then polls `heatmiserStateService.syncThermostatStates()` every **10 seconds**. After 3 consecutive failures, sets `isHubOffline: true`.
    2. **Cloud State Stream**: Listens to `stateRepository.environmentStateStream` (`/state` CoAP-over-WS from Tewke Cloud) and extracts `ThermostatTargetState` for each thermostat in `heatmiserStateService.thermostats`.
  - **Actions**: `selectZone(HeatingZone)`, `syncAfterAction({optimisticSystemMode, targetTemp})`.
- **`ThermostatActionCubit`** (`lib/features/heating/presentation/cubit/thermostat_action/thermostat_action_cubit.dart`):
  - **Dependencies**: `HeatmiserRepository`, `HeatingCubit`, `AnalyticsService`.
  - **Methods**:
    - `setThermostatMode({required ThermostatSystemState mode, required String zoneName})` → calls `heatmiserRepository.setStandbyMode(zoneName: zoneName, enable: mode == ThermostatSystemState.off)` then `heatingCubit.syncAfterAction(optimisticSystemMode: mode)`.
    - `setTargetTemp({required double temp, required String zoneName, bool isFromDial = false})` → calls `heatmiserRepository.setTargetTemperature(zoneName: zoneName, temperature: temp)` then `heatingCubit.syncAfterAction(targetTemp: temp)`.
    - `cancelOverride({required double temp, required String zoneName})` → calls `_executeSetTemp` with the active profile's current comfort level temperature.
    - `cancelHold({required String zoneName})` → calls `heatmiserRepository.cancelHold(zoneName: zoneName)` then `heatingCubit.syncAfterAction()`.

#### UI Views & User Flows (`lib/features/heating/presentation/views/`)
- **`HeatingScreen`**: Lifecycle wrapper calling `startSyncingData()` on init and `stopSyncingData()` on dispose; renders loading shimmer, error state, `"No Heatmiser NeoHub found."`, or `HeatingScreenLoaded`.
- **`HeatingScreenLoaded`**:
  - Renders a `Stack` with **Screen A (`HeatingScreenInitial`)** and **Screen B (`HeatingScreenEditTemperature`)**.
  - **Screen A (`HeatingScreenInitial`)**:
    - Top `ZoneSelector` pill → opens `ZoneSelectorScreen` bottom sheet (lists all zones, with `isThisRoom` pinned first).
    - `HeatingSystemIcon` button → opens `SystemModeSelectorScreen` bottom sheet (`Heat` / `Off`).
    - `LargeTemperatureText` → displays `activeZone.currentTemperature`.
    - `TemperatureDialInactive` → displays `displayTargetTemp` and starts horizontal drag gesture to open Screen B.
    - Bottom `StatusChip` → displays hub/thermostat offline status, command error, Away, Holiday, Frost Protection, Hold (`"<temp>° until <time> [X]"`), Override (`"<temp>° until <nextTime> [X]"`), or active profile schedule (`"<Profile> · <nextTemp>° at <nextTime>"`).
  - **Screen B (`HeatingScreenEditTemperature`)**:
    - Activates `ManageScreensBloc` focus mode (`ActivateScreenFocusMode(screenType: TapScreenType.climateControl)`) to prevent horizontal page swiping while adjusting temperature.
    - Supports two interaction modes:
      1. **Dial Dragging**: Holding and dragging left/right outside the deadzone steps `_targetTempNotifier` by `±0.5°C` or `±1.0°C` every 350ms (clamped `5.0..35.0°C`) with click audio; releasing debounces for 2s, calls `setTargetTemp` + `setThermostatMode(heat)`, and auto-closes Screen B after 500ms.
      2. **`-` / `+` Buttons**: Tapping `-` or `+` adjusts `_targetTempNotifier` by `±0.5°C` and reveals a `"Save"` pill button (`state.hasChanges == true`), which calls `setTargetTemp` and closes Screen B on tap.

### 3.3 `lib/features/mock_heating/` (Standalone Demo Feature)
- Used when `featureFlags.climateControlScreen == true && featureFlags.heatmiserIntegration == false` (`TapScreenType.demoClimateControl`).
- Completely self-contained in-memory `MockHeatingCubit` with 4 hardcoded rooms (`Kitchen`, `Living Room`, `Master Bedroom`, `Hall`), presets (`Comfort` 21°C, `Sleep` 18°C, `Away` 16°C), schedules, and system modes (`heat`, `cool`, `off`).

---

## 4. `../pytewke` — Python CoAP Client Library

### 4.1 Architecture & Components
- **`Tap` (`src/pytewke/tap.py`)**: High-level async client representing a single Tewke Tap Panel at `host`.
- **`TewkeCoap` (`src/pytewke/tewke_coap.py`)**: Low-level `aiocoap` wrapper over UDP port `5683` (10s default timeout). Encodes request payloads and decodes responses/observations using `cbor2`. Pre-loads `aiocoap` client transports in an executor thread to avoid blocking the Home Assistant event loop.
- **`ObservationManager` (`src/pytewke/observation_manager.py`)**: Manages per-resource `asyncio.Task` observations protected by per-resource `asyncio.Lock`s. Supports starting, cancelling (with explicit `observe=1` deregistration in `TewkeCoap._deregister_observation`), `retrigger_observe(resource_name)`, `retrigger_all_observations()`, and checking active observation tasks.
- **`WallDock` (`src/pytewke/wall_dock.py`)**: Tracks `id` (`hardware_id`), `targets: dict[int, Target]`, and `scenes: dict[str, Scene]` in memory, kept updated by both GET responses and Observe callbacks.

### 4.2 Discovery, Observation & Control Methods on `Tap`
1. **`await tap.discover()`**:
   - Calls `GET /.well-known/core` and parses RFC 6690 CoRE Link Format into `self.resources: dict[str, ResourceInfo]` (keyed by `rt` resource type).
   - Calls `await self.get_config()` (`GET /config`), which sets `self.tewke_os_version` and initializes `self.wall_dock` by fetching `get_targets()` and `get_scenes()`.
2. **Read Methods**:
   - `get_config() -> ConfigData` (`GET /config`)
   - `get_scenes() -> dict[str, Scene]` (`GET /scenes`)
   - `get_targets() -> dict[int, Target]` (`GET /targets`)
   - `get_sensors() -> SensorData` (`GET /sensors`)
   - `get_radar() -> RadarData` (`GET /radar`)
   - `get_energy() -> EnergyData` (`GET /energy`)
   - `get_energy_override() -> EnergyOverrideData` (`GET /energy-override`)
   - `get_device_state() -> dict[str, dict]` (`GET /state`)
3. **Write / Action Methods**:
   - `set_scene(*, scene_id=None, scene_name=None, state: bool, brightness: int | None = None)` (`POST /scenes` with `{"sceneId": ..., "isActive": state, "brightness": ...}`)
   - `set_target(*, target: int, brightness: int)` (`POST /targets` with `{"index": target, "isOn": brightness > 0, "brightness": brightness}`)
   - `set_energy_override(override: float | None) -> EnergyOverrideData` (`POST /energy-override` with `{"override": override}`)
   - `restart()` (`POST /restart` with empty body)
4. **Observation Methods**:
   - `observe(scene_callback, target_callback, config_change_callback, sensor_callback, radar_callback, energy_callback, ...)`
   - Individual `observe_scenes`, `observe_targets`, `observe_config`, `observe_sensors`, `observe_radar`, `observe_energy`
   - `retry_observes() -> bool`, `retry_observe(resource_name)`, `close_observations()`.

---

## 5. `./` (`hacs-integration`) — Home Assistant Custom Integration

### 5.1 Entry Lifecycle & Config Flow (`custom_components/tewke/`)
- **Discovery & Setup (`config_flow.py`, `__init__.py`)**:
  - Supports **Zeroconf** (`_tewke-coap._udp.local.`, reading `hardwareId`, `name`, `room` from mDNS TXT records) and **Manual User Entry** (IP/hostname, calling `tap.discover()` and `tap.get_config()`).
  - Config entry `unique_id` is set to `hardwareId` (`wall_dock_id`).
  - During `confirm_control_types`, fetches `tap.get_scenes()` and prompts the user to enable/disable each scene and choose its HA platform (`light`, `switch`, or `fan`). If any enabled scene is mapped to `fan`, prompts `fan_default_speeds` (1–100%, default 50%).
  - Supports **Reconfigure Flow** (`async_step_reconfigure`) and **Options Flow** (`TewkeOptionsFlow` for adjusting fan default speeds).
- **Dynamic Scene Discovery & Repair Flow (`util.py`, `repairs.py`)**:
  - When `_on_scene_update` receives scenes from CoAP Observe:
    - Removed scenes are automatically cleaned up from the HA entity registry and config entry (`_remove_deleted_scenes`).
    - Newly added scenes on the Tap are placed in `entry.runtime_data.pending_scenes` and raise a fixable HA Repair issue (`new_scenes_found_<entry_id>`).
    - `TewkeNewSceneRepairFlow` lets the user assign control types (`light`/`switch`/`fan`) to the new scenes and dispatches `DISPATCHER_ADD_SCENES_<entry_id>` to dynamically add the entities without reloading the entry.

### 5.2 Data Coordinator & Push Architecture (`coordinator.py`, `util.py`)
- **`TewkeCoordinator`**:
  - Holds `TewkeCoordinatorData`: `scenes`, `scenes_all`, `targets`, `sensors`, `radar`, `energy`, `energy_override`, `config`.
  - **Push-first via CoAP Observe**: `async_setup_observe` registers callbacks for all 6 observable CoAP resources (`scenes`, `targets`, `sensors`, `radar`, `energy`, `config`).
  - **Watchdog & Recovery**:
    - Every received observation resets a **30-second inactivity timer** (`_OBSERVATION_TIMEOUT_SECS = 30`). Since `/sensors` emits every 10s while observed, silence for 30s indicates connection loss.
    - On timeout (`_handle_observation_timeout`), retries observations with delays `[30, 60, 90]` seconds (`tap.retry_observes()` on attempt 0, full `_setup_observe()` on subsequent attempts). After 3 failed attempts, marks coordinator entities unavailable via `async_set_update_error`.
    - Also runs a **5-minute fallback poll** (`_RECOVERY_INTERVAL = timedelta(minutes=5)`), which skips manual GET requests when `observe_active` is True and data is already populated.
  - **Device Swap Protection**: Verifies `tap.wall_dock_id == entry.unique_id` during full polls.
  - **Dynamic Metadata Sync**: `_on_config_update` updates the HA Device Registry and config entry title when `device_name` or `tewke_os_version` changes on the Tap.

### 5.3 Exposed Home Assistant Platforms & Entities

| Platform | Entity Class | Source Data | Notes |
| :--- | :--- | :--- | :--- |
| `light` | `TewkeSceneLight` | `coordinator.data["scenes"]` | Dimmable (`ColorMode.BRIGHTNESS`), maps HA `0..255` ↔ Tewke `0..100` |
| `light` | `TewkeTargetLight` | `coordinator.data["targets"]` | Physical Wall Dock outputs (`BRIGHTNESS` or `ONOFF`), **disabled by default**, dynamically adds new target indices |
| `fan` | `TewkeSceneFan` | `coordinator.data["scenes"]` | Percentage `0..100` (`SET_SPEED \| TURN_ON \| TURN_OFF`), substitutes configured default dimming when raw scene brightness is 100% |
| `switch` | `TewkeSceneSwitch` | `coordinator.data["scenes"]` | On/Off scene activation without brightness |
| `sensor` | `TewkeSensor` (×13) | `coordinator.data["sensors"]` | Enabled: Air Quality (`iaq`), Static IAQ, Temperature (`compensated_temperature`), Humidity (`compensated_humidity`), CO₂, Pressure, Gas %, Ambient Light (`lux`). Disabled by default: IAQ Accuracy, Breath VOC, Raw Temp/Humidity/Gas |
| `sensor` | `TewkeRadarSensor` (×5) | `coordinator.data["radar"]` | Enabled: `radar_proximity` (`none`/`near`/`far`). Disabled by default: near/far thresholds & hysteresis |
| `sensor` | `TewkeEnergySensor` (×2) | `coordinator.data["energy"]` | Enabled: `power` (W). Disabled by default: `actual_power` (W) |
| `binary_sensor` | `TewkeScreenBinarySensor` | `config.screen_on` / `radar.screen_on` | `BinarySensorDeviceClass.RUNNING`, enabled by default |
| `binary_sensor` | `TewkeBinarySensor` (×2) | `coordinator.data["sensors"]` | `stabilisation_status`, `run_in_status` (disabled by default) |
| `number` | `TewkeEnergyOverrideNumber` | `coordinator.data["energy_override"]` | Box number in Watts (`0` clears override, `>0` sets override via `tap.set_energy_override`), disabled by default |
| `button` | `TewkeRestartButton` | Action only | Diagnostic restart button (`tap.restart()`), disabled by default |

---

## 6. Finalized Implementation Plan: Home Assistant Heating Integration (`P-ENG-311`)

- **Linear Project**: [Home Assistant Heating Integration (`P-ENG-311`)](https://linear.app/tewke/project/home-assistant-heating-integration-69d26ad6c77f)

### 6.1 Product & Architectural Decisions
1. **Integration Model (Bridge HA `climate.*` Entities)**:
   - Users select one or more HA `climate.*` entities in the Tewke HA integration setup, reconfigure, or options flow (plus an optional primary zone for the Tap's own room).
   - Home Assistant tracks state changes on those `climate.*` entities (`async_track_state_change_event`) and pushes zone state updates to the Tap over CoAP (`POST /heating`).
   - When the user changes target temperature or toggles `Heat`/`Off` mode on the Tap's Heating Screen, the Tap emits a CoAP Observe notification on `/heating` with a `command` object, which the HA integration translates into `climate.set_temperature` and/or `climate.set_hvac_mode` service calls.
2. **Multi-Zone Support**:
   - Multiple HA climate zones are supported on the Tap via `ZoneSelector` and `ZoneSelectorScreen`, with the primary room zone (`isThisRoom: true`) pinned first and selected by default.
3. **Automatic Screen Activation**:
   - `GenerateScreensCubit` listens to `HaHeatingService.activeStream` and automatically includes `TapScreen.climateControl()` (`HeatingScreen`) in the Tap's screen carousel whenever HA heating is active—without requiring `featureFlags.heatmiserIntegration` or manual cloud view assignment.
4. **V1 UI Scope (Core Temp & Mode)**:
   - Displays current temperature (`LargeTemperatureText`), target temperature (`TemperatureDialInactive` + `HeatingScreenEditTemperature` dial drag and `+/-` buttons), system mode (`Heat` / `Off`), active heating flame (`activelyHeating` from HA `hvac_action == "heating"`), and offline/error states in `StatusChip` (hiding Heatmiser-only schedule/hold/frost-protection chip states when online).

### 6.2 CoAP `/heating` Resource Specification (CBOR)
- **Discovery (`/.well-known/core`)**:
  `</heating>;rt="heating";if="core.a";title="Heating Control";obs`
- **`POST /heating` (HA → Tap)**:
  ```json
  {
    "active": true,
    "zones": {
      "climate.living_room": {
        "id": "climate.living_room",
        "name": "Living Room",
        "currentTemperature": 20.5,
        "targetTemperature": 21.0,
        "systemMode": "heat",
        "activelyHeating": true,
        "online": true,
        "isThisRoom": true,
        "minTemp": 5.0,
        "maxTemp": 35.0
      }
    }
  }
  ```
  *(Sending `{"active": false, "zones": {}}` clears HA heating on the Tap.)*
- **`GET /heating` & `Observe /heating` (Tap → HA)**:
  ```json
  {
    "active": true,
    "zones": { ... },
    "command": {
      "zoneId": "climate.living_room",
      "targetTemperature": 22.0,
      "systemMode": "heat"
    }
  }
  ```
  - `command` is `null` on `GET` and on state updates pushed by HA (preventing echo loops).
  - `command` is populated only when the user performs an action on the Tap UI (`setTargetTemp` or `setThermostatMode`).

### 6.3 Linear Tickets by Repository

| Repo | Issue | Title | Dependencies |
| :--- | :--- | :--- | :--- |
| `switch-ui` | [ENG-3953](https://linear.app/tewke/issue/ENG-3953/switch-ui-implement-haheatingservice-and-heating-coap-route-handler) | `[switch-ui] Implement HaHeatingService and /heating CoAP route handler` | — |
| `switch-ui` | [ENG-3954](https://linear.app/tewke/issue/ENG-3954/switch-ui-decouple-heatingscreencubits-from-heatmiser-metadata-and) | `[switch-ui] Decouple HeatingScreen/Cubits from Heatmiser metadata and connect HaHeatingService` | Blocked by `ENG-3953` |
| `switch-ui` | [ENG-3955](https://linear.app/tewke/issue/ENG-3955/switch-ui-auto-enable-heatingscreen-in-generatescreenscubit-when-ha) | `[switch-ui] Auto-enable HeatingScreen in GenerateScreensCubit when HA heating is active` | Blocked by `ENG-3953`, `ENG-3954` |
| `pytewke` | [ENG-3956](https://linear.app/tewke/issue/ENG-3956/pytewke-add-heating-coap-models-tap-control-methods-and-heating) | `[pytewke] Add Heating CoAP models, Tap control methods, and heating observation support` | Blocked by `ENG-3953` |
| `hacs-integration` | [ENG-3957](https://linear.app/tewke/issue/ENG-3957/hacs-integration-add-climate-entity-selection-to-config-reconfigure) | `[hacs-integration] Add climate entity selection to Config, Reconfigure, and Options flows` | — |
| `hacs-integration` | [ENG-3958](https://linear.app/tewke/issue/ENG-3958/hacs-integration-bridge-ha-climate-entity-states-and-coap-heating) | `[hacs-integration] Bridge HA climate entity states and CoAP /heating commands in coordinator` | Blocked by `ENG-3956`, `ENG-3957` |

### 6.4 Backwards Compatibility Guarantees

1. **New `pytewke` & `hacs-integration` talking to Old Tap Firmware (no `/heating` resource)**:
   - **Feature Detection via `/.well-known/core`**: `tap.supports_heating` (`"heating" in tap.resources`).
   - **`Tap.observe()` Safety (`pytewke`)**: `ObservationManager._update_observation_locked` raises `PyTewkeObserveError` if a requested resource is missing from `self.resources`, which in `hacs-integration` (`async_setup_observe`) would abort **all** observations (`observe_active = False`). Additionally, when `timeout_in_secs` is used in `Tap.observe()`, `self._timeout_timer.create_wrapped_callback(None)` returns a non-`None` callable that makes `has_callbacks = True`. Therefore, `Tap.observe()` must check `if self.supports_heating:` before calling `observe_heating()` or wrapping `heating_callback`.
   - **No Spurious Errors or Exceptions (`hacs-integration`)**: `TewkeCoordinator` checks `if tap.supports_heating:` before calling `tap.get_heating()`, `tap.set_heating()`, or `tap.clear_heating()` (avoiding `pytewke` logging `ERROR: Resource 'heating' not found` or raising `PyTewkeInvalidRequestError`).
   - **Config & Options Flow UX (`hacs-integration`)**: When `not tap.supports_heating`, `TewkeConfigFlow` skips `async_step_heating` and `TewkeOptionsFlow` hides the climate entity selector (retaining the existing `no_fan_scenes` abort if the device also has no fan scenes).
   - **OTA Upgrade Detection**: Re-running `await tap.discover()` when re-establishing observations after a timeout allows HA to detect `/heating` after a Tap OTA update without restarting Home Assistant.
2. **Existing `hacs-integration` Config Entries (Upgrade Path)**:
   - Existing `entry.data` and `entry.options` in HA will not have `CONF_CLIMATE_ENTITIES` or `CONF_PRIMARY_CLIMATE_ENTITY`. Reading them with `.get(CONF_CLIMATE_ENTITIES, [])` and `.get(CONF_PRIMARY_CLIMATE_ENTITY, None)` ensures zero-downtime upgrades without bumping `ConfigFlow.VERSION`.
3. **`pytewke` Public API Signature**:
   - In `Tap.observe(...)`, `heating_callback: HeatingCallback | None = None` is placed **after** `timeout_in_secs` so existing positional and keyword callers remain 100% compatible.
4. **New Tap Firmware (`switch-ui`) talking to Old `pytewke` & `hacs-integration`**:
   - `/.well-known/core` includes `</heating>;rt="heating";...`, which older `pytewke` parses into `tap.resources["heating"]` and ignores. All existing CoAP endpoints (`/config`, `/state`, `/targets`, `/scenes`, `/sensors`, `/radar`, `/energy`, `/energy-override`, `/restart`) remain unchanged.
