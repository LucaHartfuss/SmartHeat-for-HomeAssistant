# SmartHeat for Home Assistant

Home Assistant Add-on Repository.

## Add-ons in this repository

### [Heizungsbruecke](./heizungsbruecke)

Generic bridge logic: reads configured Home Assistant entities, reports them to the SmartHeat
server through AWS IoT Core (the bridge connects to IoT Core directly with its device
certificate), writes setpoints back (clamped against configured safety limits), and includes a
local boost failsafe. Has no configuration UI of its own; see [Installation](#installation)
below.

## Installation

The add-on in this repository is configured automatically by the separate **SmartHeat** Home
Assistant integration -- it needs no manual setup in its Configuration tab.

1. In Home Assistant: Settings -> Add-ons -> Add-on Store -> (top right) -> Repositories.
2. Add this URL: `https://github.com/LucaHartfuss/SmartHeat-for-HomeAssistant`
3. The add-on appears in the store under a new "SmartHeat for Home Assistant" section. Install
   `Heizungsbruecke` -- do not configure or start it manually, the integration below does that
   for you.
4. Add the SmartHeat Home Assistant integration's repository as a HACS custom repository and
   install it.
5. In Home Assistant: Settings -> Devices & Services -> Add Integration -> "SmartHeat", and
   complete the setup dialog there. The integration writes the configuration into the add-on
   and starts it automatically.
