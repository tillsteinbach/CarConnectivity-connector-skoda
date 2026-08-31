"""Module implements the connector to interact with the Skoda Public API."""  # pylint: disable=too-many-lines
from __future__ import annotations
from typing import TYPE_CHECKING

import threading
import traceback
import logging
import netrc
import os
from datetime import datetime, timedelta, timezone

from carconnectivity.garage import Garage
from carconnectivity.vehicle import GenericVehicle
from carconnectivity.errors import AuthenticationError, TooManyRequestsError, RetrievalError, APIError, APICompatibilityError, \
    TemporaryAuthenticationError, CommandError
from carconnectivity.util import robust_time_parse, log_extra_keys, config_remove_credentials
from carconnectivity.units import Length, Speed, Power, Temperature
from carconnectivity.doors import Doors
from carconnectivity.windows import Windows
from carconnectivity.lights import Lights
from carconnectivity.drive import GenericDrive, ElectricDrive, CombustionDrive, DieselDrive
from carconnectivity.attributes import BooleanAttribute, DurationAttribute, TemperatureAttribute, EnumAttribute, LevelAttribute
from carconnectivity.commands import Commands
from carconnectivity.charging import Charging
from carconnectivity.position import Position
from carconnectivity.climatization import Climatization
from carconnectivity.command_impl import ClimatizationStartStopCommand, ChargingStartStopCommand
from carconnectivity.enums import ConnectionState
from carconnectivity.window_heating import WindowHeatings

from carconnectivity_connectors.base.connector import BaseConnector
from carconnectivity_connectors.skoda.auth.public_api_session import PublicApiSession
from carconnectivity_connectors.skoda.vehicle import SkodaVehicle, SkodaElectricVehicle, SkodaCombustionVehicle, SkodaHybridVehicle, SUPPORT_IMAGES
from carconnectivity_connectors.skoda.charging import SkodaCharging, mapping_skoda_charging_state
from carconnectivity_connectors.skoda.climatization import SkodaClimatization
from carconnectivity_connectors.skoda._version import __version__

if TYPE_CHECKING:
    from typing import Dict, List, Optional, Any, Union

    from carconnectivity.carconnectivity import CarConnectivity

LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.skoda")
LOG_API: logging.Logger = logging.getLogger("carconnectivity.connectors.skoda-api-debug")

# Public API hard rate limit: 20 requests per hour per key.
# With a minimum interval of 300 s each GET counts against the budget.
PUBLIC_API_RATE_LIMIT_PER_HOUR: int = 20
PUBLIC_API_MINIMUM_INTERVAL_SECONDS: int = 300


class Connector(BaseConnector):
    """
    Connector class for the Skoda Public API.

    Configuration keys:
        api_key (str):  X-API-Key issued by the MyŠkoda app.
        vins (list):    List of VINs the key covers (the public API has no
                        list-vehicles endpoint, so VINs must be configured).
        interval (int): Poll interval in seconds (min 300, default 300).
        max_age (int):  Maximum cache age in seconds (default interval - 1).
    """

    def __init__(self, connector_id: str, car_connectivity: CarConnectivity, config: Dict, *args, initialization: Optional[Dict] = None, **kwargs) -> None:
        BaseConnector.__init__(self, connector_id=connector_id, car_connectivity=car_connectivity, config=config, log=LOG, api_log=LOG_API, *args,
                               initialization=initialization, **kwargs)

        self._background_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self.connection_state: EnumAttribute[ConnectionState] = EnumAttribute(name="connection_state", parent=self, value_type=ConnectionState,
                                                                              value=ConnectionState.DISCONNECTED, tags={'connector_custom'})
        self.interval: DurationAttribute = DurationAttribute(name="interval", parent=self, tags={'connector_custom'})
        self.interval.minimum = timedelta(seconds=PUBLIC_API_MINIMUM_INTERVAL_SECONDS)
        self.interval._is_changeable = True  # pylint: disable=protected-access

        LOG.info("Loading skoda connector (public API) with config %s", config_remove_credentials(config))

        # Validate and extract api_key — can come from config directly or from .netrc
        if 'api_key' in config and config['api_key']:
            self.active_config['api_key'] = config['api_key']
        else:
            if 'netrc' in config:
                self.active_config['netrc'] = config['netrc']
            else:
                self.active_config['netrc'] = os.path.join(os.path.expanduser("~"), ".netrc")
            try:
                secrets = netrc.netrc(file=self.active_config['netrc'])
                secret: tuple[str, str, str] | None = secrets.authenticators("skoda")
                if secret is None:
                    raise AuthenticationError(f'Authentication using {self.active_config["netrc"]} failed: skoda not found in netrc')
                # Convention: store the API key in the password field of the netrc entry
                _login, _account, password = secret
                if not password:
                    raise AuthenticationError(f'Authentication using {self.active_config["netrc"]} failed: '
                                              'no password (API key) found for skoda entry')
                self.active_config['api_key'] = password
            except netrc.NetrcParseError as err:
                raise AuthenticationError(f'Authentication using {self.active_config["netrc"]} failed: {err}') from err
            except FileNotFoundError as err:
                raise AuthenticationError(f'{self.active_config["netrc"]} netrc-file was not found. '
                                          'Create it or provide api_key in config') from err
        if not self.active_config.get('api_key'):
            raise AuthenticationError('api_key must be provided in the connector configuration or via .netrc')

        # VINs must be explicitly configured
        if 'vins' not in config or not config['vins']:
            raise APIError('vins must be provided in the connector configuration (the public API has no list-vehicles endpoint)')
        vins = config['vins']
        if isinstance(vins, str):
            vins = [v.strip() for v in vins.split(',') if v.strip()]
        self.active_config['vins'] = list(vins)

        # Poll interval
        self.active_config['interval'] = PUBLIC_API_MINIMUM_INTERVAL_SECONDS
        if 'interval' in config:
            self.active_config['interval'] = int(config['interval'])
            if self.active_config['interval'] < PUBLIC_API_MINIMUM_INTERVAL_SECONDS:
                raise ValueError(f'Interval must be at least {PUBLIC_API_MINIMUM_INTERVAL_SECONDS} seconds '
                                 f'(the public API is rate-limited to {PUBLIC_API_RATE_LIMIT_PER_HOUR} requests/hour/key)')
        self.active_config['max_age'] = self.active_config['interval'] - 1
        if 'max_age' in config:
            self.active_config['max_age'] = config['max_age']

        self.interval._set_value(timedelta(seconds=self.active_config['interval']))  # pylint: disable=protected-access

        # Create session
        self.session: PublicApiSession = PublicApiSession(api_key=self.active_config['api_key'])
        self.session.timeout = 60

        self._elapsed: List[timedelta] = []

    def startup(self) -> None:
        self._stop_event.clear()
        self._background_thread = threading.Thread(target=self._background_loop, daemon=False)
        self._background_thread.name = 'carconnectivity.connectors.skoda-background'
        self._background_thread.start()
        self.healthy._set_value(value=True)  # pylint: disable=protected-access

    def _background_loop(self) -> None:
        self._stop_event.clear()
        self.connection_state._set_value(value=ConnectionState.CONNECTING)  # pylint: disable=protected-access
        while not self._stop_event.is_set():
            interval = self.active_config['interval']
            try:
                self.fetch_all()
                self.last_update._set_value(value=datetime.now(tz=timezone.utc))  # pylint: disable=protected-access
            except TooManyRequestsError as err:
                LOG.error('Too many requests (%s). The public API allows only %d req/hour/key. '
                          'Will wait 15 minutes.', str(err), PUBLIC_API_RATE_LIMIT_PER_HOUR)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(900)
                continue
            except RetrievalError as err:
                LOG.error('Retrieval error during update (%s). Will retry after %ss.', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
                continue
            except APIError as err:
                LOG.error('API error during update (%s). Will retry after %ss.', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
                continue
            except APICompatibilityError as err:
                LOG.error('API compatibility error during update (%s). Will retry after %ss.', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
                continue
            except AuthenticationError as err:
                LOG.error('Authentication error during update (%s). Check that your API key is valid and not expired. '
                          'Will retry after %ss.', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
                continue
            except TemporaryAuthenticationError as err:
                LOG.error('Temporary authentication error during update (%s). Will retry after %ss.', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
                continue
            except Exception as err:
                LOG.critical('Critical error during update: %s', traceback.format_exc())
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self.healthy._set_value(value=False)  # pylint: disable=protected-access
                raise err
            else:
                self.connection_state._set_value(value=ConnectionState.CONNECTED)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
        self.connection_state._set_value(value=ConnectionState.DISCONNECTED)  # pylint: disable=protected-access

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._background_thread is not None:
            self._background_thread.join(timeout=5)
            self._background_thread = None
        self.connection_state._set_value(value=ConnectionState.DISCONNECTED)  # pylint: disable=protected-access
        self.healthy._set_value(value=False)  # pylint: disable=protected-access

    def persist(self) -> None:
        return

    def fetch_all(self) -> None:
        """Fetch data for all configured VINs and update the garage."""
        garage: Garage = self.car_connectivity.garage
        for vin in self.active_config['vins']:
            # Fetch or create vehicle
            vehicle: Optional[SkodaVehicle] = garage.get_vehicle(vin)  # pyright: ignore[reportAssignmentType]
            if not vehicle:
                vehicle = SkodaVehicle(vin=vin, garage=garage, managing_connector=self,
                                       initialization=garage.get_initialization(vin))
                garage.add_vehicle(vin, vehicle)

            vehicle = self.fetch_vehicle(vehicle)
        self.car_connectivity.transaction_end()

    def fetch_vehicle(self, vehicle: SkodaVehicle) -> SkodaVehicle:  # noqa: C901  pylint: disable=too-many-branches,too-many-statements
        """
        Fetch all current data for one vehicle using GET /api/v1/vehicles/{vin}.

        Args:
            vehicle (SkodaVehicle): The vehicle to update.

        Returns:
            SkodaVehicle: The updated vehicle.
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')

        response_data = self.session.get_vehicle(vin)
        vehicle_data: Dict[str, Any] = response_data.get('vehicle', {})
        api_errors: List[Dict[str, Any]] = response_data.get('errors', [])

        if api_errors:
            for err in api_errors:
                LOG_API.info('Public API reported error for %s: %s — %s',
                             vin, err.get('type'), err.get('description'))

        # Basic vehicle info
        if 'name' in vehicle_data and vehicle_data['name'] is not None:
            vehicle.name._set_value(vehicle_data['name'])  # pylint: disable=protected-access
        if 'licensePlate' in vehicle_data and vehicle_data['licensePlate'] is not None:
            vehicle.license_plate._set_value(vehicle_data['licensePlate'])  # pylint: disable=protected-access

        # Render URL / vehicle image — download into _car_images['car_picture'] when Pillow is available
        render_url = vehicle_data.get('renderUrl')
        if render_url is not None and SUPPORT_IMAGES:
            try:
                import io
                import requests as _requests
                from PIL import Image as PILImage
                img_response = _requests.get(render_url, timeout=10)
                img_response.raise_for_status()
                img = PILImage.open(io.BytesIO(img_response.content)).convert('RGBA')
                vehicle._car_images['car_picture'] = img  # pylint: disable=protected-access
            except Exception as img_err:  # pylint: disable=broad-except
                LOG.debug('Could not download render image for %s: %s', vin, img_err)

        # Determine the vehicle type from fuelStatus before updating drive ranges
        vehicle = self._update_fuel_status(vehicle, vehicle_data)

        # Odometer
        vehicle = self._update_odometer(vehicle, vehicle_data)

        # Vehicle status (doors, windows, lights)
        vehicle = self._update_vehicle_status(vehicle, vehicle_data)

        # Parking position
        vehicle = self._update_parking_position(vehicle, vehicle_data)

        # Charging (only for electric/hybrid vehicles)
        if isinstance(vehicle, SkodaElectricVehicle):
            vehicle = self._update_charging(vehicle, vehicle_data)

        # Air conditioning
        vehicle = self._update_air_conditioning(vehicle, vehicle_data)

        return vehicle

    def _update_fuel_status(self, vehicle: SkodaVehicle, vehicle_data: Dict[str, Any]) -> SkodaVehicle:  # pylint: disable=too-many-branches,too-many-statements
        """Parse fuelStatus and update drives / promote vehicle type."""
        fuel_status = vehicle_data.get('fuelStatus')
        if fuel_status is None:
            # fuelStatus absent; if charging is present the vehicle is a BEV
            if vehicle_data.get('charging') is not None:
                if not isinstance(vehicle, SkodaElectricVehicle):
                    LOG.debug('Promoting %s to SkodaElectricVehicle for %s (no fuelStatus, charging present)', vehicle.__class__.__name__, vehicle.vin.value)
                    vehicle = SkodaElectricVehicle(garage=self.car_connectivity.garage, origin=vehicle)
                    self.car_connectivity.garage.replace_vehicle(vehicle.vin.value, vehicle)
                # Ensure an ElectricDrive exists so charging code can update level/range
                if 'primary' not in vehicle.drives.drives:
                    drive = ElectricDrive(drive_id='primary', drives=vehicle.drives,
                                         initialization=vehicle.drives.get_initialization('primary'))
                    drive.type._set_value(GenericDrive.Type.ELECTRIC)  # pylint: disable=protected-access
                    vehicle.drives.add_drive(drive)
            return vehicle

        captured_at: Optional[datetime] = robust_time_parse(fuel_status.get('carCapturedTimestamp')) if fuel_status.get('carCapturedTimestamp') else None

        # Promote vehicle type based on carType
        car_type_str = fuel_status.get('carType', 'UNKNOWN')
        try:
            car_type = GenericVehicle.Type[car_type_str.upper()]
        except KeyError:
            LOG_API.warning('Unknown carType %s', car_type_str)
            car_type = GenericVehicle.Type.UNKNOWN

        # Also check if primaryEngineRange is ELECTRIC to detect BEVs not flagged in carType
        primary_range = fuel_status.get('primaryEngineRange') or {}
        primary_engine_type_str = primary_range.get('engineType', 'UNKNOWN')
        secondary_range = fuel_status.get('secondaryEngineRange')

        is_electric_primary = primary_engine_type_str == 'ELECTRIC'
        is_hybrid = (car_type == GenericVehicle.Type.HYBRID) or (is_electric_primary and secondary_range is not None)

        if is_hybrid and not isinstance(vehicle, SkodaHybridVehicle):
            LOG.debug('Promoting %s to SkodaHybridVehicle for %s', vehicle.__class__.__name__, vehicle.vin.value)
            vehicle = SkodaHybridVehicle(garage=self.car_connectivity.garage, origin=vehicle)
            self.car_connectivity.garage.replace_vehicle(vehicle.vin.value, vehicle)
        elif is_electric_primary and not is_hybrid and not isinstance(vehicle, SkodaElectricVehicle):
            LOG.debug('Promoting %s to SkodaElectricVehicle for %s', vehicle.__class__.__name__, vehicle.vin.value)
            vehicle = SkodaElectricVehicle(garage=self.car_connectivity.garage, origin=vehicle)
            self.car_connectivity.garage.replace_vehicle(vehicle.vin.value, vehicle)
        elif car_type in (GenericVehicle.Type.GASOLINE, GenericVehicle.Type.PETROL, GenericVehicle.Type.DIESEL,
                          GenericVehicle.Type.CNG, GenericVehicle.Type.LPG) and not isinstance(vehicle, SkodaCombustionVehicle):
            LOG.debug('Promoting %s to SkodaCombustionVehicle for %s', vehicle.__class__.__name__, vehicle.vin.value)
            vehicle = SkodaCombustionVehicle(garage=self.car_connectivity.garage, origin=vehicle)
            self.car_connectivity.garage.replace_vehicle(vehicle.vin.value, vehicle)

        if car_type != GenericVehicle.Type.UNKNOWN:
            vehicle.type._set_value(car_type)  # pylint: disable=protected-access

        # Total range
        if 'totalRangeInKm' in fuel_status and fuel_status['totalRangeInKm'] is not None:
            vehicle.drives.total_range._set_value(value=fuel_status['totalRangeInKm'], measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access
            vehicle.drives.total_range.precision = 1

        for drive_key, drive_data in [('primary', primary_range), ('secondary', secondary_range or {})]:
            if not drive_data:
                continue
            try:
                engine_type: GenericDrive.Type = GenericDrive.Type[drive_data.get('engineType', 'UNKNOWN').upper()]
            except KeyError:
                LOG_API.warning('Unknown engineType %s', drive_data.get('engineType'))
                engine_type = GenericDrive.Type.UNKNOWN

            if drive_key in vehicle.drives.drives:
                drive: GenericDrive = vehicle.drives.drives[drive_key]
            else:
                if engine_type == GenericDrive.Type.ELECTRIC:
                    drive = ElectricDrive(drive_id=drive_key, drives=vehicle.drives, initialization=vehicle.drives.get_initialization(drive_key))
                elif engine_type == GenericDrive.Type.DIESEL:
                    drive = DieselDrive(drive_id=drive_key, drives=vehicle.drives, initialization=vehicle.drives.get_initialization(drive_key))
                elif engine_type in (GenericDrive.Type.FUEL, GenericDrive.Type.GASOLINE, GenericDrive.Type.PETROL,
                                     GenericDrive.Type.CNG, GenericDrive.Type.LPG):
                    drive = CombustionDrive(drive_id=drive_key, drives=vehicle.drives, initialization=vehicle.drives.get_initialization(drive_key))
                else:
                    drive = GenericDrive(drive_id=drive_key, drives=vehicle.drives, initialization=vehicle.drives.get_initialization(drive_key))
                drive.type._set_value(engine_type)  # pylint: disable=protected-access
                vehicle.drives.add_drive(drive)

            soc = drive_data.get('currentSoCInPercent')
            fuel_level = drive_data.get('currentFuelLevelInPercent')
            if soc is not None:
                drive.level._set_value(value=soc, measured=captured_at)  # pylint: disable=protected-access
                drive.level.precision = 1
            elif fuel_level is not None:
                drive.level._set_value(value=fuel_level, measured=captured_at)  # pylint: disable=protected-access
                drive.level.precision = 1
            else:
                drive.level._set_value(None, measured=captured_at)  # pylint: disable=protected-access

            remaining_km = drive_data.get('remainingRangeInKm')
            if remaining_km is not None:
                drive.range._set_value(value=remaining_km, measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access
                drive.range.precision = 1
            else:
                drive.range._set_value(None, measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access

            log_extra_keys(LOG_API, f'{drive_key}EngineRange', drive_data, {'engineType', 'currentSoCInPercent',
                                                                             'currentFuelLevelInPercent', 'remainingRangeInKm'})

        # adBlue for diesel
        adblue = fuel_status.get('adBlueRange')
        for drive in vehicle.drives.drives.values():
            if isinstance(drive, DieselDrive):
                if adblue is not None:
                    drive.adblue_range._set_value(value=adblue, measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access
                    drive.adblue_range.precision = 1
                else:
                    drive.adblue_range._set_value(None, measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access

        log_extra_keys(LOG_API, 'fuelStatus', fuel_status, {'carType', 'totalRangeInKm', 'adBlueRange',
                                                            'primaryEngineRange', 'secondaryEngineRange', 'carCapturedTimestamp'})
        return vehicle

    def _update_odometer(self, vehicle: SkodaVehicle, vehicle_data: Dict[str, Any]) -> SkodaVehicle:
        """Parse odometer reading."""
        odometer = vehicle_data.get('odometer')
        if odometer is None:
            return vehicle
        captured_at: Optional[datetime] = robust_time_parse(odometer.get('carCapturedTimestamp')) if odometer.get('carCapturedTimestamp') else None
        mileage = odometer.get('mileageInKm')
        if mileage is not None:
            vehicle.odometer._set_value(value=mileage, measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access
            vehicle.odometer.precision = 1
        log_extra_keys(LOG_API, 'odometer', odometer, {'mileageInKm', 'carCapturedTimestamp'})
        return vehicle

    def _update_vehicle_status(self, vehicle: SkodaVehicle, vehicle_data: Dict[str, Any]) -> SkodaVehicle:  # noqa: C901 pylint: disable=too-many-branches
        """Parse doors/windows/lights from the status section."""
        status = vehicle_data.get('status')
        if status is None:
            return vehicle
        captured_at: Optional[datetime] = robust_time_parse(status.get('carCapturedTimestamp')) if status.get('carCapturedTimestamp') else None
        overall = status.get('overall')
        if overall is not None:
            if vehicle.doors is not None:
                # doorsLocked
                doors_locked = overall.get('doorsLocked')
                if doors_locked == 'YES':
                    vehicle.doors.lock_state._set_value(Doors.LockState.LOCKED, measured=captured_at)  # pylint: disable=protected-access
                    vehicle.doors.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                elif doors_locked == 'NO':
                    vehicle.doors.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                    vehicle.doors.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                elif doors_locked == 'OPENED':
                    vehicle.doors.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                    vehicle.doors.open_state._set_value(Doors.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                elif doors_locked == 'TRUNK_OPENED':
                    vehicle.doors.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                    vehicle.doors.open_state._set_value(Doors.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                elif doors_locked == 'UNKNOWN':
                    vehicle.doors.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                    vehicle.doors.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                elif doors_locked is not None:
                    LOG_API.info('Unknown doorsLocked value %s', doors_locked)

                # reliableLockStatus overrides lock_state when present
                reliable_lock = overall.get('reliableLockStatus')
                if reliable_lock == 'LOCKED':
                    vehicle.doors.lock_state._set_value(Doors.LockState.LOCKED, measured=captured_at)  # pylint: disable=protected-access
                elif reliable_lock == 'UNLOCKED':
                    vehicle.doors.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                elif reliable_lock == 'UNKNOWN':
                    vehicle.doors.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                elif reliable_lock is not None:
                    LOG_API.info('Unknown reliableLockStatus value %s', reliable_lock)

                # doors open state
                doors = overall.get('doors')
                if doors == 'CLOSED':
                    vehicle.doors.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                elif doors == 'OPEN':
                    vehicle.doors.open_state._set_value(Doors.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                elif doors == 'UNKNOWN':
                    vehicle.doors.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                elif doors == 'UNSUPPORTED':
                    vehicle.doors.open_state._set_value(Doors.OpenState.UNSUPPORTED, measured=captured_at)  # pylint: disable=protected-access
                elif doors is not None:
                    LOG_API.info('Unknown doors value %s', doors)

            if vehicle.windows is not None:
                windows = overall.get('windows')
                if windows == 'CLOSED':
                    vehicle.windows.open_state._set_value(Windows.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                elif windows == 'OPEN':
                    vehicle.windows.open_state._set_value(Windows.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                elif windows == 'UNKNOWN':
                    vehicle.windows.open_state._set_value(Windows.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                elif windows == 'UNSUPPORTED':
                    vehicle.windows.open_state._set_value(Windows.OpenState.UNSUPPORTED, measured=captured_at)  # pylint: disable=protected-access
                elif windows is not None:
                    LOG_API.info('Unknown windows value %s', windows)

            if vehicle.lights is not None:
                lights = overall.get('lights')
                if lights == 'ON':
                    vehicle.lights.light_state._set_value(Lights.LightState.ON, measured=captured_at)  # pylint: disable=protected-access
                elif lights == 'OFF':
                    vehicle.lights.light_state._set_value(Lights.LightState.OFF, measured=captured_at)  # pylint: disable=protected-access
                elif lights == 'UNKNOWN':
                    vehicle.lights.light_state._set_value(Lights.LightState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                elif lights is not None:
                    LOG_API.info('Unknown lights value %s', lights)

            log_extra_keys(LOG_API, 'status.overall', overall, {'doorsLocked', 'locked', 'doors', 'windows', 'lights', 'reliableLockStatus'})

        # detail: individual door/hatch open states (sunroof, trunk, bonnet)
        detail = status.get('detail')
        if detail is not None and vehicle.doors is not None:
            detail_state_map = {
                'CLOSED': Doors.OpenState.CLOSED,
                'OPEN': Doors.OpenState.OPEN,
                'UNSUPPORTED': Doors.OpenState.UNSUPPORTED,
                'UNKNOWN': Doors.OpenState.UNKNOWN,
            }
            for part_id in ('sunroof', 'trunk', 'bonnet'):
                part_str = detail.get(part_id)
                if part_str is not None:
                    part_state = detail_state_map.get(part_str, Doors.OpenState.UNKNOWN)
                    if part_str not in detail_state_map:
                        LOG_API.info('Unknown %s state %s', part_id, part_str)
                    if part_id not in vehicle.doors.doors:
                        vehicle.doors.doors[part_id] = Doors.Door(door_id=part_id, doors=vehicle.doors)
                    vehicle.doors.doors[part_id].open_state._set_value(part_state, measured=captured_at)  # pylint: disable=protected-access
            log_extra_keys(LOG_API, 'status.detail', detail, {'sunroof', 'trunk', 'bonnet'})
        log_extra_keys(LOG_API, 'status', status, {'overall', 'detail', 'carCapturedTimestamp'})
        return vehicle

    def _update_parking_position(self, vehicle: SkodaVehicle, vehicle_data: Dict[str, Any]) -> SkodaVehicle:
        """Parse parkingPosition."""
        parking = vehicle_data.get('parkingPosition')
        if parking is None:
            return vehicle
        if vehicle.position is None:
            return vehicle
        state = parking.get('state')
        gps = parking.get('gpsCoordinates')
        if state == 'PARKED' and gps is not None:
            lat = gps.get('latitude')
            lon = gps.get('longitude')
            vehicle.position.latitude._set_value(lat)  # pylint: disable=protected-access
            vehicle.position.latitude.precision = 0.000001
            vehicle.position.longitude._set_value(lon)  # pylint: disable=protected-access
            vehicle.position.longitude.precision = 0.000001
            vehicle.position.position_type._set_value(Position.PositionType.PARKING)  # pylint: disable=protected-access
        elif state == 'IN_MOTION':
            vehicle.position.position_type._set_value(Position.PositionType.MOVING)  # pylint: disable=protected-access
        else:
            vehicle.position.latitude._set_value(None)  # pylint: disable=protected-access
            vehicle.position.longitude._set_value(None)  # pylint: disable=protected-access
            vehicle.position.position_type._set_value(None)  # pylint: disable=protected-access
        log_extra_keys(LOG_API, 'parkingPosition', parking, {'state', 'gpsCoordinates', 'formattedAddress'})
        return vehicle

    def _update_charging(self, vehicle: SkodaElectricVehicle, vehicle_data: Dict[str, Any]) -> SkodaElectricVehicle:  # noqa: C901 pylint: disable=too-many-branches,too-many-statements
        """Parse charging data from the vehicle response."""
        charging_data = vehicle_data.get('charging')
        if charging_data is None:
            return vehicle

        if vehicle.charging is None:
            return vehicle

        # Ensure start-stop command is registered
        if not vehicle.charging.commands.contains_command('start-stop'):
            start_stop_command: ChargingStartStopCommand = ChargingStartStopCommand(parent=vehicle.charging.commands)
            start_stop_command._add_on_set_hook(self.__on_charging_start_stop)  # pylint: disable=protected-access
            start_stop_command.enabled = True
            vehicle.charging.commands.add_command(start_stop_command)

        captured_at: Optional[datetime] = robust_time_parse(charging_data.get('carCapturedTimestamp')) if charging_data.get('carCapturedTimestamp') else None

        if 'isVehicleInSavedLocation' in charging_data and charging_data['isVehicleInSavedLocation'] is not None:
            if not isinstance(vehicle.charging, SkodaCharging):
                vehicle.charging = SkodaCharging(origin=vehicle.charging)
            vehicle.charging.is_in_saved_location._set_value(charging_data['isVehicleInSavedLocation'], measured=captured_at)  # pylint: disable=protected-access

        status = charging_data.get('status')
        if status is not None:
            state_str = status.get('state')
            if state_str is not None:
                # Public API values: CONNECT_CABLE, CHARGING, CONSERVING, READY_FOR_CHARGING, DISCHARGING, CHARGING_INTERRUPTED
                if state_str in [item.name for item in SkodaCharging.SkodaChargingState]:
                    skoda_state: SkodaCharging.SkodaChargingState = SkodaCharging.SkodaChargingState[state_str]
                    charging_state: Charging.ChargingState = mapping_skoda_charging_state[skoda_state]
                else:
                    LOG_API.info('Unknown charging state %s', state_str)
                    charging_state = Charging.ChargingState.UNKNOWN
                vehicle.charging.state._set_value(value=charging_state, measured=captured_at)  # pylint: disable=protected-access
            else:
                vehicle.charging.state._set_value(None, measured=captured_at)  # pylint: disable=protected-access

            rate = status.get('chargingRateInKilometersPerHour')
            if rate is not None:
                vehicle.charging.rate._set_value(value=rate, measured=captured_at, unit=Speed.KMH)  # pylint: disable=protected-access
            else:
                vehicle.charging.rate._set_value(None, measured=captured_at, unit=Speed.KMH)  # pylint: disable=protected-access

            power = status.get('chargePowerInKw')
            if power is not None:
                vehicle.charging.power._set_value(value=power, measured=captured_at, unit=Power.KW)  # pylint: disable=protected-access
            else:
                vehicle.charging.power._set_value(None, measured=captured_at, unit=Power.KW)  # pylint: disable=protected-access

            remaining_min = status.get('remainingTimeToFullyChargedInMinutes')
            if remaining_min is not None and captured_at is not None:
                estimated_date_reached = (captured_at + timedelta(minutes=remaining_min)).replace(second=0, microsecond=0)
                vehicle.charging.estimated_date_reached._set_value(value=estimated_date_reached, measured=captured_at)  # pylint: disable=protected-access
            else:
                vehicle.charging.estimated_date_reached._set_value(None, measured=captured_at)  # pylint: disable=protected-access

            charge_type_str = status.get('chargeType')
            if charge_type_str is not None:
                if charge_type_str in [item.name for item in Charging.ChargingType]:
                    charge_type: Charging.ChargingType = Charging.ChargingType[charge_type_str]
                else:
                    LOG_API.info('Unknown chargeType %s', charge_type_str)
                    charge_type = Charging.ChargingType.UNKNOWN
                vehicle.charging.type._set_value(value=charge_type, measured=captured_at)  # pylint: disable=protected-access
            else:
                vehicle.charging.type._set_value(None, measured=captured_at)  # pylint: disable=protected-access

            battery = status.get('battery')
            if battery is not None:
                for drive in vehicle.drives.drives.values():
                    if isinstance(drive, ElectricDrive):
                        range_m = battery.get('remainingCruisingRangeInMeters')
                        if range_m is not None:
                            drive.range._set_value(value=range_m / 1000, measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access
                            drive.range.precision = 1
                        soc = battery.get('stateOfChargeInPercent')
                        if soc is not None:
                            drive.level._set_value(value=soc, measured=captured_at)  # pylint: disable=protected-access
                            drive.level.precision = 1
                        log_extra_keys(LOG_API, 'charging.status.battery', battery, {'remainingCruisingRangeInMeters', 'stateOfChargeInPercent'})
                        break
            log_extra_keys(LOG_API, 'charging.status', status, {'chargingRateInKilometersPerHour', 'chargePowerInKw',
                                                                 'remainingTimeToFullyChargedInMinutes', 'state', 'chargeType', 'battery'})

        settings = charging_data.get('settings')
        if settings is not None:
            target_soc = settings.get('targetStateOfChargeInPercent')
            if target_soc is not None and vehicle.charging is not None and vehicle.charging.settings is not None:
                vehicle.charging.settings.target_level.minimum = 50.0
                vehicle.charging.settings.target_level.maximum = 100.0
                vehicle.charging.settings.target_level.precision = 10.0
                vehicle.charging.settings.target_level._set_value(value=target_soc, measured=captured_at)  # pylint: disable=protected-access
            else:
                vehicle.charging.settings.target_level._set_value(None, measured=captured_at)  # pylint: disable=protected-access

            auto_unlock = settings.get('autoUnlockPlugWhenCharged')
            if auto_unlock is not None:
                if auto_unlock in ('ON', 'PERMANENT'):
                    vehicle.charging.settings.auto_unlock._set_value(True, measured=captured_at)  # pylint: disable=protected-access
                elif auto_unlock == 'OFF':
                    vehicle.charging.settings.auto_unlock._set_value(False, measured=captured_at)  # pylint: disable=protected-access
                else:
                    LOG_API.info('Unknown autoUnlockPlugWhenCharged %s', auto_unlock)
                    vehicle.charging.settings.auto_unlock._set_value(None, measured=captured_at)  # pylint: disable=protected-access
            else:
                vehicle.charging.settings.auto_unlock._set_value(None, measured=captured_at)  # pylint: disable=protected-access

            max_current_str = settings.get('maxChargeCurrentAc')
            if max_current_str is not None:
                vehicle.charging.settings.maximum_current.minimum = 6.0
                vehicle.charging.settings.maximum_current.maximum = 32.0
                vehicle.charging.settings.maximum_current.precision = 1.0
                # Prefer the numeric ampere value when available
                max_current_a = settings.get('maxChargeCurrentAcAmpere')
                if max_current_a is not None:
                    vehicle.charging.settings.maximum_current._set_value(value=float(max_current_a), measured=captured_at)  # pylint: disable=protected-access
                elif max_current_str == 'MAXIMUM':
                    vehicle.charging.settings.maximum_current._set_value(value=32.0, measured=captured_at)  # pylint: disable=protected-access
                elif max_current_str == 'REDUCED':
                    vehicle.charging.settings.maximum_current._set_value(value=6.0, measured=captured_at)  # pylint: disable=protected-access
                else:
                    vehicle.charging.settings.maximum_current._set_value(None, measured=captured_at)  # pylint: disable=protected-access
            else:
                vehicle.charging.settings.maximum_current._set_value(None, measured=captured_at)  # pylint: disable=protected-access

            preferred_mode_str = settings.get('preferredChargeMode')
            if preferred_mode_str is not None:
                if not isinstance(vehicle.charging, SkodaCharging):
                    vehicle.charging = SkodaCharging(origin=vehicle.charging)
                if preferred_mode_str in [item.name for item in SkodaCharging.SkodaChargeMode]:
                    preferred_mode = SkodaCharging.SkodaChargeMode[preferred_mode_str]
                else:
                    LOG_API.info('Unknown preferredChargeMode %s', preferred_mode_str)
                    preferred_mode = SkodaCharging.SkodaChargeMode.UNKNOWN
                if isinstance(vehicle.charging.settings, SkodaCharging.Settings):
                    vehicle.charging.settings.preferred_charge_mode._set_value(value=preferred_mode, measured=captured_at)  # pylint: disable=protected-access
            elif isinstance(vehicle.charging, SkodaCharging) and isinstance(vehicle.charging.settings, SkodaCharging.Settings):
                vehicle.charging.settings.preferred_charge_mode._set_value(None, measured=captured_at)  # pylint: disable=protected-access

            available_modes = settings.get('availableChargeModes')
            if available_modes is not None:
                if not isinstance(vehicle.charging, SkodaCharging):
                    vehicle.charging = SkodaCharging(origin=vehicle.charging)
                if isinstance(vehicle.charging.settings, SkodaCharging.Settings):
                    vehicle.charging.settings.available_charge_modes._set_value('.'.join(available_modes), measured=captured_at)  # pylint: disable=protected-access

            care_mode_str = settings.get('chargingCareMode')
            if care_mode_str is not None:
                if not isinstance(vehicle.charging, SkodaCharging):
                    vehicle.charging = SkodaCharging(origin=vehicle.charging)
                if care_mode_str in [item.name for item in SkodaCharging.SkodaChargingCareMode]:
                    care_mode = SkodaCharging.SkodaChargingCareMode[care_mode_str]
                else:
                    LOG_API.info('Unknown chargingCareMode %s', care_mode_str)
                    care_mode = SkodaCharging.SkodaChargingCareMode.UNKNOWN
                if isinstance(vehicle.charging, SkodaCharging) and isinstance(vehicle.charging.settings, SkodaCharging.Settings):
                    vehicle.charging.settings.charging_care_mode._set_value(value=care_mode, measured=captured_at)  # pylint: disable=protected-access
            elif isinstance(vehicle.charging, SkodaCharging) and isinstance(vehicle.charging.settings, SkodaCharging.Settings):
                vehicle.charging.settings.charging_care_mode._set_value(None, measured=captured_at)  # pylint: disable=protected-access

            log_extra_keys(LOG_API, 'charging.settings', settings, {'targetStateOfChargeInPercent', 'maxChargeCurrentAc',
                                                                     'maxChargeCurrentAcAmpere', 'autoUnlockPlugWhenCharged',
                                                                     'preferredChargeMode', 'availableChargeModes', 'chargingCareMode',
                                                                     'batteryCareModeTargetValueInPercent'})
        log_extra_keys(LOG_API, 'charging', charging_data, {'carCapturedTimestamp', 'status', 'isVehicleInSavedLocation', 'settings'})
        return vehicle

    def _update_air_conditioning(self, vehicle: SkodaVehicle, vehicle_data: Dict[str, Any]) -> SkodaVehicle:  # noqa: C901 pylint: disable=too-many-branches,too-many-statements
        """Parse airConditioning data."""
        ac_data = vehicle_data.get('airConditioning')
        if ac_data is None:
            return vehicle

        captured_at_str = ac_data.get('carCapturedTimestamp')
        if captured_at_str is None:
            LOG_API.debug('airConditioning has no carCapturedTimestamp for %s', vehicle.vin.value)
            return vehicle
        captured_at: datetime = robust_time_parse(captured_at_str)

        if not isinstance(vehicle.climatization, SkodaClimatization):
            vehicle.climatization = SkodaClimatization(vehicle=vehicle, origin=vehicle.climatization)

        # Register start-stop command
        if not vehicle.climatization.commands.contains_command('start-stop'):
            start_stop_command: ClimatizationStartStopCommand = ClimatizationStartStopCommand(parent=vehicle.climatization.commands)
            start_stop_command._add_on_set_hook(self.__on_air_conditioning_start_stop)  # pylint: disable=protected-access
            start_stop_command.enabled = True
            vehicle.climatization.commands.add_command(start_stop_command)

        state_str = ac_data.get('state')
        if state_str is not None:
            # Public API states: OFF, COOLING, HEATING, HEATING_AUXILIARY, VENTILATION, COMPLETED, UNKNOWN
            state_map = {
                'OFF': Climatization.ClimatizationState.OFF,
                'COOLING': Climatization.ClimatizationState.COOLING,
                'HEATING': Climatization.ClimatizationState.HEATING,
                'HEATING_AUXILIARY': Climatization.ClimatizationState.HEATING,
                'VENTILATION': Climatization.ClimatizationState.VENTILATION,
                'COMPLETED': Climatization.ClimatizationState.OFF,
                'UNKNOWN': Climatization.ClimatizationState.UNKNOWN,
                'UNSUPPORTED': Climatization.ClimatizationState.UNKNOWN,
            }
            clim_state = state_map.get(state_str, Climatization.ClimatizationState.UNKNOWN)
            if state_str not in state_map:
                LOG_API.info('Unknown airConditioning state %s', state_str)
            vehicle.climatization.state._set_value(value=clim_state, measured=captured_at)  # pylint: disable=protected-access
        else:
            vehicle.climatization.state._set_value(None, measured=captured_at)  # pylint: disable=protected-access

        target_temp = ac_data.get('targetTemperature')
        if target_temp is not None and vehicle.climatization.settings is not None:
            temp_value = target_temp.get('value')
            temp_unit_str = target_temp.get('unit', 'CELSIUS')
            if temp_value is not None:
                unit_map = {'CELSIUS': Temperature.C, 'FAHRENHEIT': Temperature.F}
                temp_unit = unit_map.get(temp_unit_str, Temperature.C)
                vehicle.climatization.settings.target_temperature._set_value(  # pylint: disable=protected-access
                    value=temp_value, measured=captured_at, unit=temp_unit)
                vehicle.climatization.settings.target_temperature.precision = 0.5
                vehicle.climatization.settings.target_temperature._add_on_set_hook(  # pylint: disable=protected-access
                    self.__on_air_conditioning_target_temperature_change)
                vehicle.climatization.settings.target_temperature._is_changeable = True  # pylint: disable=protected-access
            log_extra_keys(LOG_API, 'airConditioning.targetTemperature', target_temp, {'value', 'unit'})

        ac_without_power = ac_data.get('airConditioningWithoutExternalPower')
        if ac_without_power is not None and vehicle.climatization.settings is not None:
            vehicle.climatization.settings.without_heat_source._set_value(value=ac_without_power, measured=captured_at)  # pylint: disable=protected-access

        # Window heating status
        window_heating = ac_data.get('windowHeating')
        if window_heating is not None and vehicle.window_heatings is not None:
            state_wh_map = {
                'ON': WindowHeatings.HeatingState.ON,
                'OFF': WindowHeatings.HeatingState.OFF,
                'UNKNOWN': WindowHeatings.HeatingState.UNKNOWN,
                'UNSUPPORTED': WindowHeatings.HeatingState.UNSUPPORTED,
            }
            for window_id, api_key in (('front', 'front'), ('rear', 'rear')):
                window_str = window_heating.get(api_key)
                if window_str is not None:
                    wh_state = state_wh_map.get(window_str, WindowHeatings.HeatingState.UNKNOWN)
                    if window_str not in ('ON', 'OFF', 'UNKNOWN', 'UNSUPPORTED'):
                        LOG_API.info('Unknown window heating state %s for %s', window_str, window_id)
                    if window_id not in vehicle.window_heatings.windows:
                        wh_obj = WindowHeatings.WindowHeating(window_id=window_id, window_heatings=vehicle.window_heatings)
                        vehicle.window_heatings.windows[window_id] = wh_obj
                    vehicle.window_heatings.windows[window_id].heating_state._set_value(wh_state, measured=captured_at)  # pylint: disable=protected-access
            log_extra_keys(LOG_API, 'airConditioning.windowHeating', window_heating, {'enabled', 'front', 'rear'})

        log_extra_keys(LOG_API, 'airConditioning', ac_data, {'state', 'targetTemperature', 'estimatedReachOfTargetTemperatureAt',
                                                             'airConditioningWithoutExternalPower', 'airConditioningAtUnlock',
                                                             'windowHeating', 'carCapturedTimestamp'})
        return vehicle

    # ------------------------------------------------------------------ commands

    def __on_air_conditioning_target_temperature_change(self, temperature_attribute: TemperatureAttribute, target_temperature: float) -> float:
        if temperature_attribute.parent is None or temperature_attribute.parent.parent is None \
                or temperature_attribute.parent.parent.parent is None or not isinstance(temperature_attribute.parent.parent.parent, SkodaVehicle):
            raise CommandError('Object hierarchy is not as expected')
        vehicle: SkodaVehicle = temperature_attribute.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN is missing')
        precision = 0.5
        if temperature_attribute.precision is not None:
            precision = temperature_attribute.precision
        rounded_temp = round(target_temperature / precision) * precision
        unit = temperature_attribute.unit or Temperature.C
        unit_str = 'CELSIUS'
        if unit == Temperature.F:
            unit_str = 'FAHRENHEIT'
        body = {'targetTemperature': {'value': rounded_temp, 'unit': unit_str}}
        # Only stop first if AC is currently active; then start with the new target temperature.
        climatization = temperature_attribute.parent.parent
        if isinstance(climatization, Climatization) and climatization.state is not None \
                and climatization.state.enabled and climatization.state.value not in (None,
                                                                                      Climatization.ClimatizationState.OFF,
                                                                                      Climatization.ClimatizationState.UNKNOWN):
            try:
                self.session.post_action(f'/api/v1/vehicles/{vin}/air-conditioning/stop')
            except (RetrievalError, CommandError):
                pass  # Ignore stop failures; proceed to start with new temperature
        self.session.post_action(f'/api/v1/vehicles/{vin}/air-conditioning/start', json_body=body)
        return target_temperature

    def __on_air_conditioning_start_stop(self, start_stop_command: ClimatizationStartStopCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if start_stop_command.parent is None or start_stop_command.parent.parent is None \
                or start_stop_command.parent.parent.parent is None or not isinstance(start_stop_command.parent.parent.parent, SkodaVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        vehicle: SkodaVehicle = start_stop_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN is missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        if command_arguments['command'] == ClimatizationStartStopCommand.Command.START:
            precision = 0.5
            body: Dict[str, Any] = {}
            if 'target_temperature' in command_arguments:
                temp_val = round(command_arguments['target_temperature'] / precision) * precision
                temp_unit_str = 'CELSIUS'
                if 'target_temperature_unit' in command_arguments:
                    if not isinstance(command_arguments['target_temperature_unit'], Temperature):
                        raise CommandError('Temperature unit is not of type Temperature')
                    if command_arguments['target_temperature_unit'] == Temperature.F:
                        temp_unit_str = 'FAHRENHEIT'
                body['targetTemperature'] = {'value': temp_val, 'unit': temp_unit_str}
            elif (start_stop_command.parent is not None
                  and (climatization := start_stop_command.parent.parent) is not None
                  and isinstance(climatization, Climatization)
                  and climatization.settings is not None
                  and climatization.settings.target_temperature is not None
                  and climatization.settings.target_temperature.enabled
                  and climatization.settings.target_temperature.value is not None):
                if climatization.settings.target_temperature.precision is not None:
                    precision = climatization.settings.target_temperature.precision
                temp_val = round(climatization.settings.target_temperature.value / precision) * precision
                temp_unit = climatization.settings.target_temperature.unit or Temperature.C
                temp_unit_str = 'FAHRENHEIT' if temp_unit == Temperature.F else 'CELSIUS'
                body['targetTemperature'] = {'value': temp_val, 'unit': temp_unit_str}
            else:
                body['targetTemperature'] = {'value': 22.0, 'unit': 'CELSIUS'}
            self.session.post_action(f'/api/v1/vehicles/{vin}/air-conditioning/start', json_body=body)
        elif command_arguments['command'] == ClimatizationStartStopCommand.Command.STOP:
            self.session.post_action(f'/api/v1/vehicles/{vin}/air-conditioning/stop')
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')
        return command_arguments

    def __on_charging_start_stop(self, start_stop_command: ChargingStartStopCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if start_stop_command.parent is None or start_stop_command.parent.parent is None \
                or start_stop_command.parent.parent.parent is None or not isinstance(start_stop_command.parent.parent.parent, SkodaVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        vehicle: SkodaVehicle = start_stop_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN is missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        if command_arguments['command'] == ChargingStartStopCommand.Command.START:
            self.session.post_action(f'/api/v1/vehicles/{vin}/charging/start')
        elif command_arguments['command'] == ChargingStartStopCommand.Command.STOP:
            self.session.post_action(f'/api/v1/vehicles/{vin}/charging/stop')
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')
        return command_arguments

    # ------------------------------------------------------------------ BaseConnector interface

    def get_version(self) -> str:
        return __version__

    def get_features(self) -> dict[str, tuple[bool, str]]:
        return {}

    def get_type(self) -> str:
        return "carconnectivity-connector-skoda"

    def get_name(self) -> str:
        return "Skoda"
