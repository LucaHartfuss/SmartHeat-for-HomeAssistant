# SmartHeat for Home Assistant

Home Assistant Add-on Repository.

## Add-ons in this repository

### [Cloudflared Access TCP-Bridge](./cloudflared_access_mqtt)

Makes a private, Cloudflare-Access-protected TCP service (e.g. an MQTT broker) reachable from
inside Home Assistant OS at `127.0.0.1:<port>` — a thin wrapper around `cloudflared access tcp`.

### [Heizungsbruecke](./heizungsbruecke)

Generic bridge logic: reads configured Home Assistant entities, reports them to the SmartHeat
server over MQTT, writes setpoints back (clamped against configured safety limits), and includes
a local boost failsafe. Requires `cloudflared_access_mqtt` to be installed and running first --
`cloudflared_access_mqtt`'s `local_port` must stay on its default `18830`, since this add-on's
MQTT connection is fixed to `127.0.0.1:18830` (no config field for it). Has no configuration UI
of its own; see [Installation](#installation) below.

## Installation

Both add-ons in this repository are configured automatically by the separate **SmartHeat** Home
Assistant integration -- neither needs manual setup in its Configuration tab.

1. In Home Assistant: Settings -> Add-ons -> Add-on Store -> (top right) -> Repositories.
2. Add this URL: `https://github.com/LucaHartfuss/SmartHeat-for-HomeAssistant`
3. The add-ons appear in the store under a new "SmartHeat for Home Assistant" section. Install
   `Cloudflared Access TCP-Bridge` and `Heizungsbruecke` -- do not configure or start either
   manually, the integration below does that for you.
4. Add the SmartHeat Home Assistant integration's repository as a HACS custom repository and
   install it.
5. In Home Assistant: Settings -> Devices & Services -> Add Integration -> "SmartHeat", and
   complete the setup dialog there. The integration writes the configuration into both add-ons
   and starts them automatically.
