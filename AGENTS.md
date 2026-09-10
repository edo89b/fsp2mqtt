# fsp2mqtt

Bridge from an FSP "Twin" redundant power supply (e.g. FSP930-20REB) to MQTT in
the Home Assistant discovery format. It reads the PMBus telemetry of the two
hot-swap modules through a Silicon Labs CP2112 USB-to-I2C bridge and publishes
retained state plus discovery configs; any MQTT consumer can use the same
topics.

## Tech Stack

- Python 3.13 (Docker image `python:3.13-slim`), a single script, no framework.
- `smbus2`: SMBus/I2C access to the adapter the kernel creates for the CP2112
  (`hid_cp2112` driver, `/dev/i2c-N`).
- `paho-mqtt` 2.x: the code uses `mqtt.CallbackAPIVersion.VERSION2`, so 1.x
  does not work.
- Both packages are installed unpinned by the `Dockerfile` (no
  requirements file): a rebuild takes the latest releases.
- Docker Compose, one service.

## Directory Structure

```
fsp2mqtt/
├── fsp2mqtt.py - the whole bridge: config, PMBus decoding, discovery, poll loop
├── Dockerfile - python:3.13-slim with paho-mqtt and smbus2
├── docker-compose.yml - service fsp2mqtt: mqtt_net network, i2c device nodes
├── .env.example - configuration template (copy to .env, gitignored)
├── README.md - user overview: addresses, topics, requirements
├── scripts/
│   └── doc_check.py - documentation staleness check
└── .githooks/
    └── pre-commit - runs the check before every commit
```

## Setup & Commands

Host prerequisites: the CP2112 (USB ID `10c4:ea90`) attached to the Docker host
(or passed through to the VM that runs Docker), the `hid_cp2112` driver loaded
and the i2c-dev character devices available (`/dev/i2c-N`).

```bash
grep -H . /sys/bus/i2c/devices/i2c-*/name   # the CP2112 is "CP2112 SMBus Bridge on hidrawN"
ls -l /dev/i2c-*                            # its index must be mapped in docker-compose.yml
cp .env.example .env                        # then set broker and credentials
docker network create mqtt_net              # only if the external network does not exist yet
docker compose config --services            # validates the compose file: prints fsp2mqtt
docker compose up -d --build                # build and start (also after every code change)
docker logs -f fsp2mqtt                     # bridge log
python3 -m py_compile fsp2mqtt.py           # syntax check, touches nothing
python3 scripts/doc_check.py                # documentation staleness check
```

- A healthy start logs `[i2c] using bus /dev/i2c-N`, `[mqtt] connected to ...`,
  `[psu] model ...` (when auto-detected), `[discovery] published for 2 modules`
  (default addresses) and `[psu] readable -> online`.
- There is no test suite and no PMBus simulator: verification means running on
  the real hardware and watching the topics below.
- The compose file expects an existing external network `mqtt_net` shared with
  the broker; `MQTT_HOST` is the broker's name on that network.
- Diagnose from the sysfs names and the bridge log. Scanning the bus with
  `i2cdetect` sends probe writes to a PSU that powers a running machine: its
  own manual warns it can confuse devices.

## Coding Conventions

- One script with sections (configuration, I2C/PMBus, discovery, main loop).
  Functions in `snake_case`, module constants in `UPPER_CASE`; PMBus command
  codes are named constants (`READ_VIN`, `STATUS_WORD`, ...) next to their hex
  code, never inline numbers.
- A new metric is one tuple in `METRICS` (key, command, `l11`/`l16`, unit,
  device class, icon): state topic and discovery config follow from it.
- Configuration comes only from environment variables read at import with a
  default (`os.environ.get`). A new variable goes into `.env.example`, the
  README if user-facing, and the table below in the same commit.
- The bridge only reads from the PSU. Never add PMBus writes such as OPERATION,
  ON_OFF_CONFIG or CLEAR_FAULTS: the modules power a running machine.
- Availability goes through `publish_avail`, and `on_connect` re-asserts the
  last value after every reconnection. Keep both: the LWT leaves a retained
  `offline` on the broker at every connection drop, and without the re-assert
  consumers would see the bridge as down while data keeps flowing.
- Everything published is retained; availability and discovery use QoS 1,
  measurements QoS 0. The bridge subscribes to nothing.
- Logging is `print` through `log()` with a `[i2c]`/`[mqtt]`/`[psu]`/
  `[discovery]` tag; never print the MQTT password.
- Comments, docstrings and log messages in English (public repository).

## Git Workflow and operating rules

- Single branch `main`, pushed to `origin` on GitHub. No CI.
- Commit messages in English, imperative subject, optional lowercase scope
  prefix (e.g. `compose: map all i2c buses ...`).
- Commits, pushes and releases are done by the maintainer only; nothing is
  pushed without an explicit decision.
- Public repository: never commit `.env`, IP addresses, hostnames or host
  specific paths. `.env.example` holds placeholders only.
- Deploying a change means `docker compose up -d --build` on the host with the
  CP2112; the image does not follow the repository by itself. Discovery is sent
  once per process start, so changed discovery payloads need a restart.
- Documentation-code coherence: a change that makes a sentence of the
  documentation false fixes it in the same commit. When the staleness check
  fails, fix the document, do not silence the check. The check catches broken
  references (names that no longer exist), not descriptions that became false.
- Staleness check: `python3 scripts/doc_check.py` (manual run). The versioned
  hook `.githooks/pre-commit` runs it on every commit once enabled, once per
  clone: `git config core.hooksPath .githooks`. Markers for legitimate
  exceptions: `<!-- doc-check:ignore -->` (line), `-start`/`-end` (block),
  `-file` (whole document).

## Key Files & Directories

- `fsp2mqtt.py`: all runtime code.
- `docker-compose.yml`: restart policy `unless-stopped`, device mapping
  `/dev/i2c-0` to `/dev/i2c-5`, json-file logs capped at 3 x 10 MB.
- `.env.example`: configuration template; the real `.env` is gitignored.
- Tests: none. Documentation: this file and `README.md`.

## Related documents

- [README.md](README.md): user overview, addresses, published topics.

## Integrations (census)

Before adding or changing a read on the bus or a published topic, check this
census; every new read or topic is added here in the same commit, before the
code.

PMBus reads, per module address in `PSU_ADDRESSES` (default `0x58` = PSU 1,
`0x59` = PSU 2):

| # | Code | Command | Decoding | Published as |
|---|---|---|---|---|
| 1 | `0x20` | `VOUT_MODE` | 5-bit signed exponent | scaling for `vout` |
| 2 | `0x88` | `READ_VIN` | LINEAR11 | `vin` (V) |
| 3 | `0x8B` | `READ_VOUT` | LINEAR16 | `vout` (V) |
| 4 | `0x8C` | `READ_IOUT` | LINEAR11 | `iout` (A) |
| 5 | `0x8D` | `READ_TEMP1` | LINEAR11 | `temp1` (°C) |
| 6 | `0x8E` | `READ_TEMP2` | LINEAR11 | `temp2` (°C) |
| 7 | `0x90` | `READ_FAN1` | LINEAR11 | `fan` (rpm) |
| 8 | `0x96` | `READ_POUT` | LINEAR11 | `pout` (W) |
| 9 | `0x97` | `READ_PIN` | LINEAR11 | `pin` (W) |
| 10 | `0x79` | `STATUS_WORD` | raw word | `status_raw`, `fault` |
| 11 | `0x9A` | `MFR_MODEL` | block read, first address, once | discovery `model` |

MQTT topics, all retained (`N` = 1-based position in `PSU_ADDRESSES`):

| # | Topic | Content |
|---|---|---|
| 1 | `<STATE_PREFIX>/availability` | `online` if at least one module answers, else `offline`; also the LWT |
| 2 | `<STATE_PREFIX>/psuN/<metric>` | the metrics above, rounded to 2 decimals |
| 3 | `<STATE_PREFIX>/psuN/fault` | `ON` when `STATUS_WORD` is not zero (any bit), else `OFF` |
| 4 | `<STATE_PREFIX>/psuN/status_raw` | `STATUS_WORD` in hex, for diagnosis |
| 5 | `<STATE_PREFIX>/total_pout` | sum of `pout` of the modules that answered, 1 decimal |
| 6 | `<STATE_PREFIX>/redundancy` | `ON` (degraded) if a module is missing or in fault |
| 7 | `<DISCOVERY_PREFIX>/sensor/<DEVICE_ID>/<object>/config` | one per metric and module, plus `total_pout` |
| 8 | `<DISCOVERY_PREFIX>/binary_sensor/<DEVICE_ID>/<object>/config` | `psuN_fault`, `redundancy`, `online` |

The `online` binary sensor has no availability topic, so it reports `offline`
while the bridge is down; every other entity uses `<STATE_PREFIX>/availability`.

## Known issues and operating traps

1. **Bus index changes across reboots.** The CP2112 got a different
   `/dev/i2c-N` after a reboot (USB enumeration against the GPU and chipset
   adapters). Symptom: `[i2c] using bus /dev/i2c-N` followed by
   `FileNotFoundError` for that node. Remedy: keep `I2C_BUS=auto` (the adapter
   is found by name in `/sys`) and make sure the index is among the `devices:`
   mapped in `docker-compose.yml`; add it if it is higher than 5.
2. **Crash loop at host boot.** If the broker is not resolvable yet, `connect()`
   raises `socket.gaierror: [Errno -3] Temporary failure in name resolution`
   and the process exits; the restart policy retries until the broker is up
   (about two minutes seen). Act only if it persists: check the broker and the
   `mqtt_net` network.
3. **Rejected MQTT login is silent.** `[mqtt] connected to ...` is logged as
   soon as the TCP connection opens; `on_connect` ignores the reason code, so
   wrong credentials show no error here. Check the broker log.
4. **Stale values of a missing module.** A module that stops answering (any
   read failing) is skipped whole: its `psuN/*` topics keep the last retained
   values. The signal is `redundancy = ON`, not the per-module topics.
5. **Model detection.** `MFR_MODEL` is read once, from the first address only;
   if that read fails at the first poll, discovery goes out without a model and
   is not resent.

## Environment Variables and secrets

All variables live in `.env` (gitignored, loaded by `env_file`); the reference
with placeholders is `.env.example`.

| # | Variable | Default | Meaning |
|---|---|---|---|
| 1 | `I2C_BUS` | `auto` | `auto` finds the CP2112 by adapter name, or a bus number |
| 2 | `PSU_ADDRESSES` | `0x58,0x59` | module addresses, comma separated (hex or decimal) |
| 3 | `MQTT_HOST` / `MQTT_PORT` | `mqtt` / `1883` | broker |
| 4 | `MQTT_USER` / `MQTT_PASS` | `fsp2mqtt` / empty | broker credentials; an empty user means anonymous |
| 5 | `MQTT_CLIENT` | `fsp2mqtt` | MQTT client id: must be unique on the broker |
| 6 | `DISCOVERY_PREFIX` | `homeassistant` | discovery root |
| 7 | `DEVICE_ID` | `fsp_twin_psu` | device identifier, part of discovery topics and unique ids |
| 8 | `STATE_PREFIX` | `fsp/<DEVICE_ID>` | state root; `.env.example` sets it explicitly, so it does not follow a changed `DEVICE_ID` |
| 9 | `DEVICE_NAME` / `DEVICE_MANUFACTURER` | `FSP Twin PSU` / `FSP-GROUP` | discovery device block |
| 10 | `DEVICE_MODEL` | empty | empty means auto-detect from `MFR_MODEL` |
| 11 | `SUGGESTED_AREA` | empty | optional discovery `suggested_area` |
| 12 | `POLL_INTERVAL` | `15` | seconds between polls |

- The only secret is `MQTT_PASS`: only in `.env`, never in the repository, in
  logs or in chat.
- Give the bridge its own broker user: it only publishes, under
  `<STATE_PREFIX>/` and `<DISCOVERY_PREFIX>/+/<DEVICE_ID>/`, and subscribes to
  nothing.
