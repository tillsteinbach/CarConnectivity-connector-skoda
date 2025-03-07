

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

[CarConnectivity](https://github.com/tillsteinbach/CarConnectivity) is a python API to connect to various car services. This connector enables the integration of skoda vehicles through the WeConnect API. Look at [CarConnectivity](https://github.com/tillsteinbach/CarConnectivity) for other supported brands.

## Configuration
In your carconnectivity.json configuration add a section for the skoda connector like this:
```
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "skoda",
                "config": {
                    "username": "test@test.de",
                    "password": "testpassword123"
                }
            }
        ]
    }
}
```
### Credentials
If you do not want to provide your username or password inside the configuration you have to create a ".netrc" file at the appropriate location (usually this is your home folder):
```
# For WeConnect
machine skoda
login test@test.de
password testpassword123
```
In this case the configuration needs to look like this:
```
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "skoda",
                "config": {
                }
            }
        ]
    }
}
```

You can also provide the location of the netrc file in the configuration.
```
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "skoda",
                "config": {
                    "netrc": "/some/path/on/your/filesystem"
                }
            }
        ]
    }
}
```
The optional S-PIN needed for some commands can be provided in the account section of the netrc:
```
# For WeConnect
machine skoda
login test@test.de
password testpassword123
account 1234
```
