# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]
- No unreleased changes so far

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

[unreleased]: https://github.com/tillsteinbach/CarConnectivity-connector-skoda/compare/v0.8...HEAD
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
