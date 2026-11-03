"""Constants for the Tewke integration."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "tewke"
DISPATCHER_ADD_SCENES = "tewke_add_scenes"

CONF_DEFAULT_SCENE_FAN_DIMMING = "default_scene_fan_dimming"
CONF_DISABLED_SCENES = "disabled_scenes"
CONF_CLIMATE_ENTITIES = "climate_entities"
CONF_PRIMARY_CLIMATE_ENTITY = "primary_climate_entity"
DEFAULT_SCENE_FAN_DIMMING = 50
