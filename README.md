# fsp2mqtt

Bridge from an **FSP "Twin" redundant PSU** (e.g. FSP930-20REB) to MQTT, using the
Home Assistant discovery format. OpenHAB consumes the same topics.

The PSU exposes PMBus telemetry over a **Silicon Labs CP2112 HID-to-I2C bridge**.
The Linux `hid_cp2112` driver presents it as a regular I2C adapter (`/dev/i2c-N`),
so the bridge just talks SMBus to the two PSU modules:

| Address | Module |
|---------|--------|
| `0x58`  | PSU 1  |
| `0x59`  | PSU 2  |

Per module it reads VIN, VOUT, IOUT, TEMP1/TEMP2, FAN, POUT, PIN and STATUS_WORD
(decoded from PMBus LINEAR11/LINEAR16), plus an aggregate output power and a
redundancy-degraded indicator.

## Published topics

- HA discovery configs under `homeassistant/{sensor,binary_sensor}/<device_id>/.../config` (retained)
- State under `fsp/<device_id>/psuN/<metric>` and `fsp/<device_id>/total_pout` (retained)
- Availability / LWT on `fsp/<device_id>/availability` (`online`/`offline`)

## Networking

The container only needs to reach the MQTT broker. In `docker-compose.yml` it joins an
internal bridge network shared with the broker and resolves it by name (`MQTT_HOST=mqtt`),
so it needs no LAN IP. Adjust `MQTT_HOST` / the network to match your setup (a plain
bridge that can reach your broker also works).

## Requirements

- The CP2112 USB device available on the host running the container (e.g. passed through
  to a VM). The `hid_cp2112` kernel driver exposes it as an I2C adapter (`/dev/i2c-N`).
- The `/dev/i2c-N` node mapped into the container (see `docker-compose.yml`).
  `I2C_BUS=auto` finds the CP2112 adapter by name; if the bus index changes, update
  the `devices:` mapping accordingly.

## Setup

1. Copy `.env.example` to `.env` and set your MQTT broker host/credentials.
2. `docker compose up -d --build`

## Debug

```bash
docker logs -f fsp2mqtt
mosquitto_sub -h <broker> -u <user> -P '<pwd>' -t 'fsp/#' -v
```
