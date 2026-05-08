# Home Assistant Tewke

![status_badge](https://img.shields.io/badge/status-alpha-red)

- [Home Assistant Tewke](#home-assistant-tewke)
  - [Features](#features)
  - [Prerequisites](#prerequisites)
  - [How to install](#how-to-install)
    - [HACS](#hacs)
    - [Manual](#manual)
  - [Issues](#issues)

A pre-release Home Assistant integration for Tewke devices.

## Features

- [x] Scene control
  - [x] As lights, fans, or switches
- [x] Target control (default disabled)
- [x] Sensor data
- [ ] Repair flows:
  - [x] New Scenes
  - [ ] Rename Scenes
- [x] Handle deleted Scenes
- [x] Reconfigure flow


## Prerequisites

Before you can use this integration, you need to enable the CoAP server on
your Tewke Tap Panel. The controls for this will be available in the Tewke 
mobile app when the feature is ready for general availability.

## How to install

There are multiple ways of installing the integration.

### HACS

This integration can be installed via [HACS](https://www.hacs.xyz/). To install:

* [Add the repository](https://my.home-assistant.io/redirect/hacs_repository/?owner=tewke&repository=hacs-integration&category=integration) to your HACS installation
* Click `Download`

### Manual

You should use the latest commit on [main](https://github.com/tewke/hacs-integration/tree/main).

To install, place the contents of `custom_components` into the 
`<config directory>/custom_components` folder of your Home Assistant 
installation. Once installed, remember to restart your Home Assistant 
instance for the integration to be picked up.

## Issues

If you have found a bug or have a feature request, please [raise it](https://github.com/tewke/hacs-integration/issues).