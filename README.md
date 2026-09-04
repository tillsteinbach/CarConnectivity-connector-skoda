# CarConnectivity Connector for Skoda Vehicles
[![GitHub sourcecode](https://img.shields.io/badge/Source-GitHub-green)](https://github.com/tillsteinbach/CarConnectivity-connector-skoda/)
[![GitHub release (latest by date)](https://img.shields.io/github/v/release/tillsteinbach/CarConnectivity-connector-skoda)](https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/latest)
[![GitHub](https://img.shields.io/github/license/tillsteinbach/CarConnectivity-connector-skoda)](https://github.com/tillsteinbach/CarConnectivity-connector-skoda/blob/master/LICENSE)
[![GitHub issues](https://img.shields.io/github/issues/tillsteinbach/CarConnectivity-connector-skoda)](https://github.com/tillsteinbach/CarConnectivity-connector-skoda/issues)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/carconnectivity-connector-skoda?label=PyPI%20Downloads)](https://pypi.org/project/carconnectivity-connector-skoda/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/carconnectivity-connector-skoda)](https://pypi.org/project/carconnectivity-connector-skoda/)
[![Donate at PayPal](https://img.shields.io/badge/Donate-PayPal-2997d8)](https://www.paypal.com/donate?hosted_button_id=2BVFF5GJ9SXAJ)
[![Sponsor at Github](https://img.shields.io/badge/Sponsor-GitHub-28a745)](https://github.com/sponsors/tillsteinbach)

## CarConnectivity will become the successor of [WeConnect-python](https://github.com/tillsteinbach/WeConnect-python) in 2025 with similar functionality but support for other brands beyond Volkswagen!

[CarConnectivity](https://github.com/tillsteinbach/CarConnectivity) is a python API to connect to various car services. This connector enables the integration of Škoda vehicles through the **official Škoda Public API** (`public.api.connect.skoda-auto.cz`). Look at [CarConnectivity](https://github.com/tillsteinbach/CarConnectivity) for other supported brands.

## Getting an API key

Every request needs an API key. Keys are created and managed in the MyŠkoda app.

Are you on a mobile phone with MyŠkoda installed? [Manage API keys](https://go.skoda.eu/api-keys)

Reading this on a computer? Scan the QR code below with your phone to open key management in the app:

![QR code linking to API key management in the MyŠkoda app](https://public.api.connect.skoda-auto.cz/docs/api-keys-qr.svg)

Don't have MyŠkoda? [Download it here](https://go.skoda.eu/myskoda).

## Prerequisites

This connector uses the **official Škoda Public Vehicle API** (beta). To use it you need:

1. **MyŠkoda app v8.16+** — create an API key in the app's key-management screen / QR flow.
2. **A list of VINs** — the public API is vehicle-bound and has no list-vehicles endpoint; you must supply your VIN(s) explicitly.

> **Rate limit:** The public API allows **20 requests per hour per API key**. The default poll interval of 300 s (5 min) leaves headroom for commands. Do not lower the interval below 300 s.

## Features supported by the public API

| Feature | Status |
|---|---|
| Vehicle status (doors, windows, lights) | ✅ |
| Fuel / battery level and range | ✅ |
| Odometer | ✅ |
| Parking position (GPS) | ✅ (requires Remote Access licence) |
| Air conditioning status + start/stop | ✅ |
| Window heating status | ✅ |
| Charging status + start/stop | ✅ (EV/hybrid only) |
| Charging settings (target SoC, mode, etc.) | ✅ read-only |
| Honk & flash | ❌ not in public API |
| Lock / unlock | ❌ not in public API |
| Wake-up | ❌ not in public API |
| Maintenance / inspection info | ❌ not in public API |
| MQTT push events | ❌ not in public API |

## Configuration

In your `carconnectivity.json` add a section for the skoda connector:

```json
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "skoda",
                "config": {
                    "api_key": "your-api-key-from-myskoda-app",
                    "vins": ["TMBJB9NY5RF999999"]
                }
            }
        ]
    }
}
```

### Configuration options

| Key | Required | Default | Description |
|---|---|---|---|
| `api_key` | see below | — | API key created in the MyŠkoda app |
| `vins` | ✅ | — | List of VINs (or comma-separated string) the key covers |
| `interval` | ❌ | `300` | Poll interval in seconds (minimum 300) |
| `max_age` | ❌ | `interval - 1` | Maximum cache age in seconds for vehicle data |
| `max_age_static` | ❌ | `86400` (24 hours) | Maximum cache age in seconds for vehicle images |
| `netrc` | ❌ | `~/.netrc` | Path to a netrc file containing the API key |

### Credentials

The API key can be provided either directly in the config file or via a `.netrc` file.

**Option 1 — config file:**
```json
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "skoda",
                "config": {
                    "api_key": "your-api-key-from-myskoda-app",
                    "vins": ["TMBJB9NY5RF999999"]
                }
            }
        ]
    }
}
```

**Option 2 — netrc file** (API key in the `password` field):
```
# ~/.netrc
machine skoda
login unused
password your-api-key-from-myskoda-app
```

With netrc the config can omit `api_key`:
```json
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "skoda",
                "config": {
                    "vins": ["TMBJB9NY5RF999999"]
                }
            }
        ]
    }
}
```

You can also point to a custom netrc path:
```json
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "skoda",
                "config": {
                    "netrc": "/some/path/on/your/filesystem",
                    "vins": ["TMBJB9NY5RF999999"]
                }
            }
        ]
    }
}
```

### Known issues

#### Unexpected keys found
Not all items that are presented in the data from the server are already implemented by the connector. Feel free to report interesting findings in your log data in the [Discussions](https://github.com/tillsteinbach/CarConnectivity-connector-skoda/discussions) section or as an [Issue (Enhancement)](https://github.com/tillsteinbach/CarConnectivity-connector-skoda/issues). My time is very limited, so usually new features take some time to get into the library, also because I need to align functionality between the connectors of all brands.
