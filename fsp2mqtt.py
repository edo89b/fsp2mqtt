#!/usr/bin/env python3
"""
fsp2mqtt - bridge from an FSP "Twin" redundant PSU (PMBus over a Silicon Labs
CP2112 HID-to-I2C bridge) to MQTT, using the Home Assistant discovery format.

The FSP930-20REB Twin exposes two hot-swap PSU modules on the I2C bus:
  - 0x58 = PSU module 1
  - 0x59 = PSU module 2
Each module answers standard PMBus telemetry commands (VIN/VOUT/IOUT/TEMP/FAN/
POUT/PIN/STATUS_WORD) in LINEAR11/LINEAR16 format.

The CP2112 is exposed by the Linux `hid_cp2112` driver as a regular I2C adapter
(/dev/i2c-N), so we just talk SMBus to it via smbus2.

Published:
  - retained HA discovery config topics under DISCOVERY_PREFIX (default `homeassistant/`)
  - retained state topics under STATE_PREFIX (default `fsp/<device_id>`)
  - availability/LWT on STATE_PREFIX/availability (online|offline)

Home Assistant auto-discovers the device; OpenHAB can consume the same topics.
"""
import glob
import os
import json
import sys
import time

from smbus2 import SMBus, i2c_msg
import paho.mqtt.client as mqtt

# --- Configuration from environment ------------------------------------------
I2C_BUS = os.environ.get("I2C_BUS", "auto")        # "auto" -> find the CP2112 adapter
PSU_ADDRESSES = [int(x, 0) for x in os.environ.get("PSU_ADDRESSES", "0x58,0x59").split(",")]

MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "fsp2mqtt")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
MQTT_CLIENT = os.environ.get("MQTT_CLIENT", "fsp2mqtt")

DISCOVERY_PREFIX = os.environ.get("DISCOVERY_PREFIX", "homeassistant").rstrip("/")
DEVICE_ID = os.environ.get("DEVICE_ID", "fsp_twin_psu")
STATE_PREFIX = os.environ.get("STATE_PREFIX", f"fsp/{DEVICE_ID}").rstrip("/")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "FSP Twin PSU")
DEVICE_MANUFACTURER = os.environ.get("DEVICE_MANUFACTURER", "FSP-GROUP")
DEVICE_MODEL = os.environ.get("DEVICE_MODEL", "")          # auto-detected from MFR_MODEL if empty
SUGGESTED_AREA = os.environ.get("SUGGESTED_AREA", "")

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "15"))

AVAIL_TOPIC = f"{STATE_PREFIX}/availability"

# PMBus command codes
VOUT_MODE = 0x20
STATUS_WORD = 0x79
READ_VIN = 0x88
READ_VOUT = 0x8B
READ_IOUT = 0x8C
READ_TEMP1 = 0x8D
READ_TEMP2 = 0x8E
READ_FAN1 = 0x90
READ_POUT = 0x96
READ_PIN = 0x97
MFR_MODEL = 0x9A

# Per-module metrics: (key, pmbus_cmd, format, unit, device_class, icon)
METRICS = [
    ("vin",   READ_VIN,   "l11", "V",   "voltage",     None),
    ("vout",  READ_VOUT,  "l16", "V",   "voltage",     None),
    ("iout",  READ_IOUT,  "l11", "A",   "current",     None),
    ("temp1", READ_TEMP1, "l11", "°C",  "temperature", None),
    ("temp2", READ_TEMP2, "l11", "°C",  "temperature", None),
    ("fan",   READ_FAN1,  "l11", "rpm", None,          "mdi:fan"),
    ("pout",  READ_POUT,  "l11", "W",   "power",       None),
    ("pin",   READ_PIN,   "l11", "W",   "power",       None),
]


def log(*a):
    print(*a, flush=True)


# --- I2C / PMBus -------------------------------------------------------------
def find_cp2112_bus():
    """Return the i2c bus number whose adapter name contains 'CP2112', else None."""
    for path in glob.glob("/sys/bus/i2c/devices/i2c-*/name"):
        try:
            if "cp2112" in open(path).read().strip().lower():
                return int(path.split("i2c-")[1].split("/")[0])
        except (OSError, ValueError):
            continue
    return None


def linear11(word):
    """Decode a PMBus LINEAR11 value (5-bit signed exp, 11-bit signed mantissa)."""
    y = word & 0x7FF
    n = (word >> 11) & 0x1F
    if y >= 0x400:
        y -= 0x800
    if n >= 0x10:
        n -= 0x20
    return y * (2.0 ** n)


def linear16(word, exp):
    """Decode a PMBus LINEAR16 value (mantissa * 2^exp, exp from VOUT_MODE)."""
    return word * (2.0 ** exp)


def vout_exponent(bus, addr):
    """Read VOUT_MODE and return the signed 5-bit exponent (usually negative)."""
    mode = bus.read_byte_data(addr, VOUT_MODE)
    exp = mode & 0x1F
    if exp >= 0x10:
        exp -= 0x20
    return exp


def read_model(bus, addr):
    """PMBus block read of MFR_MODEL (count byte first). Best-effort."""
    try:
        write = i2c_msg.write(addr, [MFR_MODEL])
        read = i2c_msg.read(addr, 32)
        bus.i2c_rdwr(write, read)
        data = list(read)
        count = data[0]
        return bytes(data[1:1 + count]).split(b"\x00")[0].decode("ascii", "ignore").strip()
    except Exception:
        return None


def read_module(bus, addr):
    """Read all telemetry for one PSU module. Returns dict or None if absent."""
    try:
        exp = vout_exponent(bus, addr)
        out = {}
        for key, cmd, fmt, *_ in METRICS:
            w = bus.read_word_data(addr, cmd)
            out[key] = round(linear16(w, exp) if fmt == "l16" else linear11(w), 2)
        out["status_raw"] = bus.read_word_data(addr, STATUS_WORD)
        out["fault"] = "ON" if out["status_raw"] != 0 else "OFF"
        return out
    except OSError:
        return None


# --- MQTT discovery ----------------------------------------------------------
def device_block():
    blk = {"identifiers": [DEVICE_ID], "name": DEVICE_NAME, "manufacturer": DEVICE_MANUFACTURER}
    if DEVICE_MODEL:
        blk["model"] = DEVICE_MODEL
    if SUGGESTED_AREA:
        blk["suggested_area"] = SUGGESTED_AREA
    return blk


def publish_discovery(client, modules):
    dev = device_block()

    def cfg(component, obj_id, payload):
        topic = f"{DISCOVERY_PREFIX}/{component}/{DEVICE_ID}/{obj_id}/config"
        payload["device"] = dev
        payload["availability_topic"] = AVAIL_TOPIC
        payload["unique_id"] = f"{DEVICE_ID}_{obj_id}"
        payload["object_id"] = f"{DEVICE_ID}_{obj_id}"
        client.publish(topic, json.dumps(payload), qos=1, retain=True)

    for idx in modules:
        n = idx + 1  # module number (1-based)
        for key, cmd, fmt, unit, dev_class, icon in METRICS:
            obj = f"psu{n}_{key}"
            payload = {
                "name": f"PSU{n} {key.upper()}",
                "state_topic": f"{STATE_PREFIX}/psu{n}/{key}",
                "state_class": "measurement",
                "unit_of_measurement": unit,
            }
            if dev_class:
                payload["device_class"] = dev_class
            if icon:
                payload["icon"] = icon
            cfg("sensor", obj, payload)
        # Per-module fault status
        cfg("binary_sensor", f"psu{n}_fault", {
            "name": f"PSU{n} fault",
            "state_topic": f"{STATE_PREFIX}/psu{n}/fault",
            "device_class": "problem", "payload_on": "ON", "payload_off": "OFF",
        })

    # Aggregate output power across modules
    cfg("sensor", "total_pout", {
        "name": "Total output power",
        "state_topic": f"{STATE_PREFIX}/total_pout",
        "device_class": "power", "unit_of_measurement": "W", "state_class": "measurement",
    })
    # Redundancy: OFF = redundant/healthy, ON = degraded (a module missing or faulted)
    cfg("binary_sensor", "redundancy", {
        "name": "Redundancy degraded",
        "state_topic": f"{STATE_PREFIX}/redundancy",
        "device_class": "problem", "payload_on": "ON", "payload_off": "OFF",
    })
    # Bridge connectivity (no availability_topic, so it reports even when the bridge is down)
    client.publish(
        f"{DISCOVERY_PREFIX}/binary_sensor/{DEVICE_ID}/online/config",
        json.dumps({
            "name": "Online", "state_topic": AVAIL_TOPIC,
            "device_class": "connectivity", "payload_on": "online", "payload_off": "offline",
            "device": dev, "unique_id": f"{DEVICE_ID}_online", "object_id": f"{DEVICE_ID}_online",
        }), qos=1, retain=True)


# --- Main loop ---------------------------------------------------------------
def main():
    global DEVICE_MODEL

    bus_num = find_cp2112_bus() if I2C_BUS == "auto" else int(I2C_BUS)
    if bus_num is None:
        log("[i2c] CP2112 adapter not found; set I2C_BUS explicitly")
        sys.exit(1)
    log(f"[i2c] using bus /dev/i2c-{bus_num}, PSU addresses {[hex(a) for a in PSU_ADDRESSES]}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT, clean_session=True)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.will_set(AVAIL_TOPIC, "offline", qos=1, retain=True)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    log(f"[mqtt] connected to {MQTT_HOST}:{MQTT_PORT} as {MQTT_USER}")

    discovery_sent = False
    online = None
    n_modules = len(PSU_ADDRESSES)

    try:
        while True:
            with SMBus(bus_num) as bus:
                # Detect the model once (for the HA device block) before discovery
                if not DEVICE_MODEL:
                    m = read_model(bus, PSU_ADDRESSES[0])
                    if m:
                        DEVICE_MODEL = m
                        log(f"[psu] model {m}")

                results = {i: read_module(bus, a) for i, a in enumerate(PSU_ADDRESSES)}

            present = {i: r for i, r in results.items() if r is not None}

            if not present:
                if online is not False:
                    client.publish(AVAIL_TOPIC, "offline", qos=1, retain=True)
                    online = False
                    log("[psu] no module readable -> offline")
                time.sleep(POLL_INTERVAL)
                continue

            if not discovery_sent:
                publish_discovery(client, list(range(n_modules)))
                discovery_sent = True
                log(f"[discovery] published for {n_modules} modules")

            if online is not True:
                client.publish(AVAIL_TOPIC, "online", qos=1, retain=True)
                online = True
                log("[psu] readable -> online")

            total_pout = 0.0
            degraded = len(present) < n_modules
            for i, r in present.items():
                n = i + 1
                for key, *_ in METRICS:
                    client.publish(f"{STATE_PREFIX}/psu{n}/{key}", r[key], qos=0, retain=True)
                client.publish(f"{STATE_PREFIX}/psu{n}/fault", r["fault"], qos=0, retain=True)
                client.publish(f"{STATE_PREFIX}/psu{n}/status_raw", hex(r["status_raw"]), qos=0, retain=True)
                total_pout += r["pout"]
                if r["fault"] == "ON":
                    degraded = True

            client.publish(f"{STATE_PREFIX}/total_pout", round(total_pout, 1), qos=0, retain=True)
            client.publish(f"{STATE_PREFIX}/redundancy", "ON" if degraded else "OFF", qos=0, retain=True)

            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        client.publish(AVAIL_TOPIC, "offline", qos=1, retain=True)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
