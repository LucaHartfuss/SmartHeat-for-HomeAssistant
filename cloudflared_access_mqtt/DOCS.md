# Cloudflared Access TCP-Bridge

Thin wrapper around the official `cloudflared` client in `access tcp` mode. Opens a local TCP
listener (`127.0.0.1:<local_port>`) that forwards through Cloudflare Access to a private TCP
service (e.g. an MQTT broker) exposed via a Cloudflare Tunnel on the other end — no inbound port,
no VPN, authenticated via a Cloudflare Access Service Token.

## Configuration

| Option | Description |
|---|---|
| `hostname` | Public hostname of the Cloudflare Access application (e.g. `mqtt.example.com`). |
| `local_port` | Local port to listen on, forwarded to the remote service. |
| `service_token_id` | Cloudflare Access Service Token Client ID. |
| `service_token_secret` | Cloudflare Access Service Token Client Secret. |

All four fields are required — the add-on refuses to start without them.

## Usage

Point any client that supports host/port configuration (e.g. Home Assistant's MQTT integration)
at `127.0.0.1:<local_port>`. Because this add-on runs with `host_network: true`, that address is
the same `127.0.0.1` Home Assistant Core itself sees.

## Architectures

This add-on is declared for `aarch64`, `amd64`, `armhf` and `armv7`. The `heizungsbruecke`
add-on it is paired with in SmartHeat is only built for `aarch64` and `amd64`, so a SmartHeat
installation needs a 64-bit system.
