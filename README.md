# SmartHeat for Home Assistant

Home Assistant Add-on Repository.

## Add-ons in this repository

### [Heizungsbruecke](./heizungsbruecke)

SmartHeat device for Home Assistant: reads the entities assigned during setup, reports to the
SmartHeat service through AWS IoT Core (the add-on connects directly with its own device
certificate) and writes the setpoints back, clamped to fixed local safety limits that never come
from the server. Comfort boost (only when the target temperature is raised) and emergency mode
stay local. No configuration UI of its own (only two lab options, `bootstrap_url` and
`log_level`); see [Installation](#installation) below.

## Installation

The add-on gets its configuration as a document from the SmartHeat server; it needs no manual
setup in its Configuration tab. Two ways, both end in the setup dialog of the **SmartHeat**
integration:

- **Integration first:** add the SmartHeat Home Assistant integration's repository as a HACS
  custom repository and install it. In Home Assistant: Settings -> Devices & Services -> Add
  Integration -> "SmartHeat". The integration adds this repository to the Add-on Store, installs
  and starts `Heizungsbruecke` and guides you through the setup via the SmartHeat server's setup
  API; the server then delivers the configuration to the add-on.
- **Add-on first:** in Home Assistant: Settings -> Add-ons -> Add-on Store -> (top right) ->
  Repositories, add `https://github.com/LucaHartfuss/SmartHeat-for-HomeAssistant`, install and
  start `Heizungsbruecke`. Home Assistant then reports "SmartHeat" as discovered; with the
  SmartHeat integration installed (HACS, as above), confirm it to start the same setup dialog.
