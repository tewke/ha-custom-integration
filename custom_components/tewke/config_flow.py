"""Config flow for the Tewke integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytewke
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from pytewke.error import BasePyTewkeError

from .const import (
    CONF_DEFAULT_SCENE_FAN_DIMMING,
    CONF_DISABLED_SCENES,
    DEFAULT_SCENE_FAN_DIMMING,
    DOMAIN,
    LOGGER,
)
from .util import _get_default_scene_fan_dimming

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
    from pytewke.data import Scene

    from .coordinator import TewkeCoordinator

_CONTROL_TYPE_OPTIONS = [
    selector.SelectOptionDict(value="light", label="Light"),
    selector.SelectOptionDict(value="switch", label="Switch"),
    selector.SelectOptionDict(value="fan", label="Fan"),
]

_FAN_SPEED_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=1,
        max=100,
        step=1,
        mode=selector.NumberSelectorMode.SLIDER,
    )
)


def _build_unique_scene_labels(scenes: dict[str, Scene]) -> dict[str, str]:
    """Return a mapping of unique form field label -> scene_id."""
    name_counts: dict[str, int] = {}
    for scene in scenes.values():
        name_counts[scene.name] = name_counts.get(scene.name, 0) + 1

    label_to_id: dict[str, str] = {}
    for scene_id, scene in scenes.items():
        label = (
            f"{scene.name} ({scene_id[-4:]})"
            if name_counts[scene.name] > 1
            else scene.name
        )
        label_to_id[label] = scene_id
    return label_to_id


class TewkeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tewke."""

    VERSION = 1

    _discovered_host: str
    _discovered_name: str
    _room_name: str | None = None
    _scene_control_types: dict[str, str]
    _default_scene_fan_dimming: dict[str, int]
    _disabled_scenes: list[str]
    _scenes: dict[str, Scene]
    _tap: pytewke.Tap | None = None

    @staticmethod
    @callback
    def async_get_options_flow(_config_entry: ConfigEntry) -> TewkeOptionsFlow:
        """Return the Options flow handler."""
        return TewkeOptionsFlow()

    async def async_step_user(
        self,
        user_input: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user (manual IP/hostname entry)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            tap = pytewke.Tap(host)
            try:
                await tap.discover()
                config = await tap.get_config()
            except (BasePyTewkeError, TimeoutError) as err:
                LOGGER.debug("Failed to connect to Tewke device at %s: %s", host, err)
                await tap.close()
                errors["base"] = "cannot_connect"
            else:
                unique_id = config.hardware_id or tap.wall_dock_id
                if not unique_id:
                    await tap.close()
                    return self.async_abort(reason="cannot_identify")

                await self.async_set_unique_id(unique_id, raise_on_progress=False)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})

                self._tap = tap
                self._discovered_host = host
                self._discovered_name = config.device_name or f"Tewke ({host})"
                self._room_name = config.room_name
                return await self.async_step_confirm_control_types()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self,
        _user_input: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Handle a reconfiguration flow initialised by the user."""
        entry = self._get_reconfigure_entry()
        self._discovered_host = entry.data[CONF_HOST]
        self._discovered_name = entry.data[CONF_NAME]
        self._room_name = entry.data.get("room_name")
        self._scene_control_types = entry.data.get("scene_control_types", {})
        self._disabled_scenes = list(entry.data.get(CONF_DISABLED_SCENES, []))
        self._default_scene_fan_dimming = _get_default_scene_fan_dimming(entry)
        return await self.async_step_confirm_control_types()

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle zeroconf discovery."""
        LOGGER.debug("Zeroconf discovery: %s", discovery_info)

        unique_id = discovery_info.properties.get("hardwareId")
        if not unique_id:
            LOGGER.error("Failed to get unique ID from mDNS TXT records")
            return self.async_abort(reason="cannot_identify")

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: discovery_info.host})

        self._discovered_host = discovery_info.host
        self._discovered_name = discovery_info.properties.get(
            "name"
        ) or discovery_info.name.replace("._tewke-coap._udp.local.", "")
        self._room_name = discovery_info.properties.get("room") or None

        display_name = (
            f"{self._discovered_name} ({self._room_name})"
            if self._room_name
            else self._discovered_name
        )
        self.context["title_placeholders"] = {"name": display_name}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Confirm the discovered device and proceed to scene setup."""
        if user_input is not None:
            return await self.async_step_confirm_control_types()

        room_suffix = f", in room **{self._room_name}**" if self._room_name else ""
        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={
                "name": self._discovered_name,
                "room_suffix": room_suffix,
            },
        )

    async def _async_close_tap(self) -> None:
        """Close the temporary Tap client if one was created during the flow."""
        if self._tap is not None:
            await self._tap.close()
            self._tap = None

    async def async_step_confirm_control_types(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Assign a Home Assistant platform type to each scene."""
        tap = self._tap if self._tap is not None else pytewke.Tap(self._discovered_host)
        self._tap = tap

        try:
            if not tap.resources:
                await tap.discover()
            scenes = await tap.get_scenes()
        except (BasePyTewkeError, TimeoutError) as err:
            LOGGER.error(
                "Error communicating with Tewke device at %s: %s",
                self._discovered_host,
                err,
            )
            await self._async_close_tap()
            return self.async_abort(reason="cannot_connect")

        self._scenes = scenes
        LOGGER.debug("Discovered scenes: %s", scenes)
        label_to_id = _build_unique_scene_labels(scenes)

        if user_input is not None:
            scene_configs = user_input.items()
            self._scene_control_types = {}
            self._disabled_scenes = []

            for name, config in scene_configs:
                if name not in label_to_id:
                    continue

                if not isinstance(config, dict) or not all(
                    isinstance(k, str) and isinstance(v, (str, bool))
                    for k, v in config.items()
                ):
                    continue

                scene_id = label_to_id[name]
                self._scene_control_types[scene_id] = config["Control type"]
                if not config["Enabled"]:
                    self._disabled_scenes.append(scene_id)

            if "fan" in self._scene_control_types.values() and [
                sceneid
                for sceneid, scene_type in self._scene_control_types.items()
                if scene_type == "fan" and sceneid not in self._disabled_scenes
            ]:
                return await self.async_step_fan_default_speeds()

            self._default_scene_fan_dimming = {}
            return await self.async_step_confirmation()

        if not scenes:
            return await self.async_step_confirmation()

        fields: dict = {}
        existing_types = getattr(self, "_scene_control_types", {})
        existing_disabled = getattr(self, "_disabled_scenes", [])
        for label, scene_id in label_to_id.items():
            fields[vol.Required(label)] = section(
                vol.Schema(
                    {
                        vol.Optional(
                            "Enabled", default=scene_id not in existing_disabled
                        ): selector.BooleanSelector(),
                        vol.Required(
                            "Control type",
                            default=existing_types.get(scene_id, "light"),
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=_CONTROL_TYPE_OPTIONS,
                                mode=selector.SelectSelectorMode.DROPDOWN,
                            )
                        ),
                    }
                )
            )
        return self.async_show_form(
            step_id="confirm_control_types",
            data_schema=vol.Schema(fields),
        )

    async def async_step_fan_default_speeds(
        self, user_input: dict[str, float] | None = None
    ) -> ConfigFlowResult:
        """Set a default scene dimming value for each fan scene."""
        fan_scenes = {
            scene_id: self._scenes[scene_id]
            for scene_id, ct in self._scene_control_types.items()
            if ct == "fan"
            and scene_id in self._scenes
            and scene_id not in self._disabled_scenes
        }
        label_to_id = _build_unique_scene_labels(fan_scenes)

        if user_input is not None:
            self._default_scene_fan_dimming = {
                label_to_id[name]: int(value)
                for name, value in user_input.items()
                if name in label_to_id
            }
            return await self.async_step_confirmation()

        existing_dimming = getattr(self, "_default_scene_fan_dimming", {})

        return self.async_show_form(
            step_id="fan_default_speeds",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        label,
                        default=existing_dimming.get(
                            scene_id, DEFAULT_SCENE_FAN_DIMMING
                        ),
                    ): _FAN_SPEED_SELECTOR
                    for label, scene_id in label_to_id.items()
                }
            ),
        )

    async def async_step_confirmation(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Show confirmation before creating the config entry."""
        if user_input is not None:
            await self._async_close_tap()
            data = {
                CONF_HOST: self._discovered_host,
                CONF_NAME: self._discovered_name,
                "room_name": self._room_name,
                "scene_control_types": self._scene_control_types or {},
                CONF_DEFAULT_SCENE_FAN_DIMMING: getattr(
                    self, "_default_scene_fan_dimming", {}
                ),
                CONF_DISABLED_SCENES: getattr(self, "_disabled_scenes", []),
            }

            if self.source == SOURCE_RECONFIGURE:
                entry = self._get_reconfigure_entry()
                options = dict(entry.options)
                options.pop(CONF_DEFAULT_SCENE_FAN_DIMMING, None)

                return self.async_update_reload_and_abort(
                    entry,
                    data=data,
                    options=options,
                )

            return self.async_create_entry(
                title=self._discovered_name,
                data=data,
            )

        return self.async_show_form(
            step_id="confirmation",
            description_placeholders={"name": self._discovered_name},
        )


class TewkeOptionsFlow(OptionsFlow):
    """Handle options for the Tewke integration."""

    async def async_step_init(
        self, user_input: dict[str, float] | None = None
    ) -> ConfigFlowResult:
        """Manage fan default speed options."""
        entry = self.config_entry
        scene_control_types: dict[str, str] = entry.data.get("scene_control_types", {})
        disabled_scenes = entry.data.get(CONF_DISABLED_SCENES, [])

        coordinator: TewkeCoordinator | None = getattr(
            getattr(entry, "runtime_data", None), "coordinator", None
        )
        scenes: dict[str, Scene] = (
            coordinator.data["scenes"] if coordinator and coordinator.data else {}
        )

        fan_scenes = {
            scene_id: scene
            for scene_id, scene in scenes.items()
            if scene_control_types.get(scene_id) == "fan"
            and scene_id not in disabled_scenes
        }

        if not fan_scenes:
            return self.async_abort(reason="no_fan_scenes")

        current_speeds = _get_default_scene_fan_dimming(entry)
        label_to_id = _build_unique_scene_labels(fan_scenes)

        if user_input is not None:
            default_scene_fan_dimming = {
                label_to_id[name]: int(value)
                for name, value in user_input.items()
                if name in label_to_id
            }
            return self.async_create_entry(
                data={CONF_DEFAULT_SCENE_FAN_DIMMING: default_scene_fan_dimming}
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        label,
                        default=current_speeds.get(scene_id, DEFAULT_SCENE_FAN_DIMMING),
                    ): _FAN_SPEED_SELECTOR
                    for label, scene_id in label_to_id.items()
                }
            ),
        )
