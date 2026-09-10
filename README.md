# SmartHeat for Home Assistant

Home Assistant Add-on Repository.

## Add-ons in this repository

### [Cloudflared Access TCP-Bridge](./cloudflared_access_mqtt)

Makes a private, Cloudflare-Access-protected TCP service (e.g. an MQTT broker) reachable from
inside Home Assistant OS at `127.0.0.1:<port>` — a thin wrapper around `cloudflared access tcp`.

### [Heizungsbruecke](./heizungsbruecke)

Generic bridge logic: reads configured Home Assistant entities, reports them to the SmartHeat
server over MQTT, writes setpoints back (clamped against configured safety limits), and includes
a local boost failsafe. Requires `cloudflared_access_mqtt` to be installed and running first (its
`local_port` must match this add-on's `mqtt_port`).

## Installation

1. In Home Assistant: Settings -> Add-ons -> Add-on Store -> (top right) -> Repositories.
2. Add this URL: `https://github.com/LucaHartfuss/SmartHeat-for-HomeAssistant`
3. The add-ons appear in the store under a new "SmartHeat for Home Assistant" section. Install
   `Cloudflared Access TCP-Bridge` first, configure (`hostname`, `local_port`, `service_token_id`,
   `service_token_secret`), start. Then install `Heizungsbruecke`, configure it (see its `DOCS.md`
   for all fields), start.
