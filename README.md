# Tewke Home Assistant Integration

![status_badge](https://img.shields.io/badge/status-beta-red)
[![Validate HACS](https://github.com/tewke/hacs-integration/actions/workflows/hacs.yml/badge.svg)](https://github.com/tewke/hacs-integration/actions/workflows/hacs.yml)
[![Validate hassfest](https://github.com/tewke/hacs-integration/actions/workflows/hassfest.yml/badge.svg)](https://github.com/tewke/hacs-integration/actions/workflows/hassfest.yml)

- [ Tewke Home Assistant Integration](#Tewke-Home-Assistant-Integration)
    - [Features](#features)
    - [Prerequisites](#prerequisites)
    - [How to install](#how-to-install)
        - [HACS](#hacs)
        - [Manual](#manual)
    - [Issues](#issues)

A pre-release Home Assistant integration for Tewke devices.

## Features

- [x] Scene control
    - [x] As lights
    - [x] As fans
    - [x] As switches
- [x] Target control (default disabled)
- [x] Sensor data
- [x] Repair flow for new Scenes
- [x] Reconfigure flow

## Prerequisites

Before you can use this integration, you need to enable the CoAP server on
your Tewke Tap Panel. The controls for this will be available in the Tewke
mobile app when the feature is ready for general availability.

You will also need to have [HACS](https://www.hacs.xyz/) installed on your
instance of Home Assistant.

## How to install

There are multiple ways of installing the integration.

### HACS

Click the following button to add this repository to HACS and install it:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=tewke&repository=hacs-integration&category=integration)

### Manual

You should use the latest commit on [main](https://github.com/tewke/hacs-integration/tree/main).

To install, place the contents of `custom_components` into the
`<config directory>/custom_components` folder of your Home Assistant
installation. Once installed, remember to restart your Home Assistant
instance for the integration to be picked up.

## Issues

If you have found a bug or have a feature request, please [raise it](https://github.com/tewke/hacs-integration/issues).
