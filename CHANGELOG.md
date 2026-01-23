# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]
- No unreleased changes so far

## [0.11.8] - 2026-01-23
### Fixed
- Improves handling of token refresh (thanks to user @mikrohard)
- Vehicle images will not be downloaded more often than every 24h

## [0.11.7] - 2026-01-11
### Fixed
- Fixes compatibility with CarConnectivity version 0.11.5

## [0.11.6] - 2026-01-09
## Fixed
- Fixes a bug where the MQTT connection crashed during parsing of data

## Added
- Attribute of time of last received event
- More vehicle images are offered
- UI status page with current state of MQTT connection

## Changed
- Improved log output
- Improved behaviour of going from online to previous state

## [0.11.5] - 2026-01-08
## Fixed
- Fixes bug where the vehicle state was not updated on MQTT push messages

## [0.11.4] - 2026-01-07
### Added
- Adds reaction on previously unknown MQTT push messages

## [0.11.3] - 2026-01-06
### Changed
- Adds fetching connection status when MQTT push message is received. This allows to have more up-to-date connection status information and improves timing of state changes.

## [0.11.2] - 2026-01-05
### Fixed
- Fixes bug with online timeout timer locking mechanism

## [0.11.1] - 2026-01-05
### Added
- Adds new DRIVING state when the vehicle is in motion vs IGNITION_ON when parked with ignition on

### Changed
- Uses more reliable attributes for door locks

## [0.11] - 2026-01-04
### Added
- Support for initializing attributes on startup form static entries in the configuration

Note: This connector is required for compatibility with CarConnectivity version 0.11 and higher.

## [0.10] - 2026-01-01
### Added
- Online state of vehicle is now tracked
- For electric vehicles the battery capacity is taken from the vehicle specification

## [0.9] - 2025-11-09
### Fixed
- Fixes login behaviour that was broken after a change on Skoda side
### Added
- Add more maintenance data (thanks to user @acfischer42)
- Add support to show/log enabled optional features

## [0.8.2] - 2025-11-03
### Fixed
- Fixes bug that may have persisted wrong expires_in calculation

## [0.8.1] - 2025-11-02
### Fixed
- Relogin when MQTT client receives a bad username or password error

### Added
- Warning when connector detects suspicious token expiry

### Changed
- Updated some dependencies

## [0.8] - 2025-06-27
### Added
- Support for adblue range

## [0.7.3] - 2025-06-20
### Fixed
- Fixes bug that registers hooks several times, causing multiple calls to the servers

### Changed
- Updated dependencies

## [0.7.2] - 2025-04-19
### Fixed
- Fix for problems introduced with PyJWT

## [0.7.1] - 2025-04-18
### Fixed
- Fixed problem where driving range was not fetched when measurements capability had an expired license. Now it will be also fetched when charging license is active.

## [0.7] - 2025-04-17
### Fixed
- Bug in mode attribute that caused the connector to crash

### Changed
- Updated dependencies
- stripping of leading and trailing spaces in commands

### Added
- Precision for all attributes is now used when displaying values

## [0.6] - 2025-04-02
### Fixed
- Only fetch range when measurement capability is available (fix for older cars)
- Allowes to have multiple instances of this connector running

### Changed
- Updated dependencies

## [0.5] - 2025-03-20
### Added
- Support for window heating attributes
- Support for window heating command
- SUpport for changing charging settings

## [0.4.1] - 2025-03-04
### Fixed
- Fixed potential http error when parking position was fetched but due to error not available

## [0.4] - 2025-03-02
### Added
- Added better feedback when consent is needed
- added better access to connection state
- added better access to health state
- added attribute for vehicle in_motion
- added possibility to online change interval
- added named threads for better debugging
- added vehcile connection state
- added global vehicle state
- added maintainance objects
- added checks for min/max values with climatization temperatures
- improved error handling with commands

## [0.3] - 2025-02-19
### Added
- Added support for images
- Added tags to attributes
- Added support for webui via carconnectivity-plugin-webui

## [0.2] - 2025-02-02
### Added
- Wake Sleep command

## [0.1] - 2025-01-25
Initial release, let's go and give this to the public to try out...
The API is not yet implemented completely but most functions already work

[unreleased]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/compare/v0.11.8...HEAD
[0.11.8]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.11.8
[0.11.7]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.11.7
[0.11.6]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.11.6
[0.11.5]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.11.5
[0.11.4]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.11.4
[0.11.3]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.11.3
[0.11.2]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.11.2
[0.11.1]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.11.1
[0.11]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.11
[0.10]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.10
[0.9]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.9
[0.8.2]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.8.2
[0.8.1]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.8.1
[0.8]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.8
[0.7.3]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.7.3
[0.7.2]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.7.2
[0.7.1]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.7.1
[0.7]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.7
[0.6]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.6
[0.5]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.5
[0.4.1]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.4.1
[0.4]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.4
[0.3]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.3
[0.2]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.2
[0.1]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/releases/tag/v0.1
