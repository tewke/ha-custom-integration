# Tewke Home Assistant Integration

![status_badge](https://img.shields.io/badge/status-beta-red)
[![Validate HACS](https://github.com/tewke/ha-custom-integration/actions/workflows/hacs.yml/badge.svg)](https://github.com/tewke/ha-custom-integration/actions/workflows/hacs.yml)
[![Validate hassfest](https://github.com/tewke/ha-custom-integration/actions/workflows/hassfest.yml/badge.svg)](https://github.com/tewke/ha-custom-integration/actions/workflows/hassfest.yml)

- [Tewke Home Assistant Integration](#tewke-home-assistant-integration)
  - [Features](#features)
  - [Prerequisites](#prerequisites)
  - [How to install](#how-to-install)
    - [HACS](#hacs-recommended)
    - [Manual](#manual)
  - [Issues](#issues)

A Home Assistant integration for Tewke devices.

## Features

- [x] Scene control[^1]
  - [x] As lights
  - [x] As fans[^2]
  - [x] As switches
- [x] Target control (default disabled)
- [x] Climate control (bridge Home Assistant `climate` entities to the Tap Climate Control screen)
- [x] Sensor data
- [x] Repair flow for new Scenes
- [x] Reconfigure flow

[^1]: This only affects how the Scenes appear in Home Assistant. The Wall
Dock only supports resistive or near-resistive loads.

[^2]: **The Tewke Tap Wall Dock does not support inductive loads. Connecting
a fan or other inductive device will result in permanent damage to your
hardware.** This is strictly a convenience feature for separating a Scene
that controls something other than lights from the rest of the Scenes.

## Prerequisites

Before you can use this integration, you need to enable the CoAP server on
your Tewke Tap Panel. The controls for this will be available in the Tewke
mobile app when the feature is ready for general availability. To enable CoAP
now, please reach out to Tewke at <contact@tewke.com> for instructions.

You will also need to have [HACS](https://www.hacs.xyz/) installed on your
instance of Home Assistant.

## How to install

There are multiple ways of installing the integration.

### HACS (Recommended)

Simply search HACS for "Tewke", press Download,
then restart Home Assistant in order for it to pick up the integration.

### Manual

You should use the [latest release on Github](https://github.com/tewke/ha-custom-integration/releases/latest).

To install, place the contents of `custom_components` into the
`<config directory>/custom_components` folder of your Home Assistant
installation. Once installed, remember to restart your Home Assistant
instance for the integration to be picked up.

## Issues

If you have found a bug or have a feature request, please [raise it](https://github.com/tewke/ha-custom-integration/issues).
