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

> **Rate limit:** The public API allows **20 requests per hour per API key** (max. 5 keys). If `interval` is not configured, the connector automatically picks a poll interval based on how many API keys are currently active/available (see [Dynamic poll interval](#dynamic-poll-interval)). If you configure `interval` explicitly, do not lower it below 300 s for a single key. Configure multiple API keys (see [Multiple API keys](#multiple-api-keys)) to combine their rate-limit budgets.

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
| `api_key` | see below | — | One API key, or a list of API keys, created in the MyŠkoda app. When multiple keys are configured, requests are distributed across all of them to combine their rate-limit budgets. |
| `vins` | ✅ | — | List of VINs (or comma-separated string) the key(s) cover |
| `interval` | ❌ | dynamic (see below) | Poll interval in seconds (minimum 300 if set explicitly) |
| `max_age` | ❌ | `interval - 1` | Maximum cache age in seconds for vehicle data |
| `max_age_static` | ❌ | `86400` (24 hours) | Maximum cache age in seconds for vehicle images |
| `netrc` | ❌ | `~/.netrc` | Path to a netrc file containing the API key(s) |

### Multiple API keys

The public API allows up to 5 API keys, each limited to 20 requests/hour. If you have more vehicles
or want more headroom for commands, create several keys in the MyŠkoda app and configure them as a
list:

```json
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "skoda",
                "config": {
                    "api_key": ["your-first-api-key", "your-second-api-key"],
                    "vins": ["TMBJB9NY5RF999999"]
                }
            }
        ]
    }
}
```

Requests are distributed evenly across all configured keys. When a key expires (based on the
`X-API-Key-Expires-At` response header) it is automatically removed from the pool and a warning is
logged; an info message is logged once a key is within 7 days of expiring. If every configured key
expires, an error is logged and the connector is marked unhealthy.

### Dynamic poll interval

If `interval` is **not** set in the configuration, the connector automatically derives the poll
interval from the number of currently active (non-expired) API keys, so that the combined
rate-limit budget of all keys is used without configuration:

| Active API keys | Poll interval |
|---|---|
| 1 | 240 s |
| 2 | 120 s |
| 3 | 65 s |
| 4 | 50 s |
| 5 (or more) | 40 s |

The interval is re-evaluated whenever a key expires and is removed from the pool, so the connector
automatically slows down as fewer keys remain available. `max_age` follows the dynamic interval
(`interval - 1`) unless `max_age` is explicitly configured. Setting `interval` explicitly disables
this dynamic behaviour and always enforces the 300 s minimum.

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

**Option 2 — netrc file** (API key in the `password` field, multiple keys can be comma-separated):
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
