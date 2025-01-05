"""Module implements the connector to interact with the Skoda API."""
from __future__ import annotations
from typing import TYPE_CHECKING

import threading
import os
import logging
import netrc
from datetime import datetime, timedelta
import requests

from carconnectivity.garage import Garage
from carconnectivity.vehicle import GenericVehicle
from carconnectivity.errors import AuthenticationError, TooManyRequestsError, RetrievalError, APIError, APICompatibilityError, \
    TemporaryAuthenticationError, ConfigurationError
from carconnectivity.util import robust_time_parse, log_extra_keys, config_remove_credentials
from carconnectivity.units import Length
from carconnectivity.doors import Doors
from carconnectivity.windows import Windows
from carconnectivity.lights import Lights
from carconnectivity.drive import GenericDrive, ElectricDrive, CombustionDrive
from carconnectivity.attributes import BooleanAttribute, DurationAttribute

from carconnectivity_connectors.base.connector import BaseConnector
from carconnectivity_connectors.skoda.auth.session_manager import SessionManager, SessionUser, Service
from carconnectivity_connectors.skoda.auth.my_skoda_session import MySkodaSession
from carconnectivity_connectors.skoda.vehicle import SkodaVehicle, SkodaElectricVehicle, SkodaCombustionVehicle, SkodaHybridVehicle
from carconnectivity_connectors.skoda.capability import Capability
from carconnectivity_connectors.skoda._version import __version__
from carconnectivity_connectors.skoda.mqtt_client import SkodaMQTTClient

if TYPE_CHECKING:
    from typing import Dict, List, Optional, Any

    from carconnectivity.carconnectivity import CarConnectivity

LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.skoda")
LOG_API: logging.Logger = logging.getLogger("carconnectivity.connectors.skoda-api-debug")


class Connector(BaseConnector):
    """
    Connector class for Skoda API connectivity.
    Args:
        car_connectivity (CarConnectivity): An instance of CarConnectivity.
        config (Dict): Configuration dictionary containing connection details.
    Attributes:
        max_age (Optional[int]): Maximum age for cached data in seconds.
    """
    def __init__(self, connector_id: str, car_connectivity: CarConnectivity, config: Dict) -> None:
        BaseConnector.__init__(self, connector_id=connector_id, car_connectivity=car_connectivity, config=config)

        self._mqtt_client: SkodaMQTTClient = SkodaMQTTClient(skoda_connector=self)

        self._background_thread: Optional[threading.Thread] = None
        self._background_connect_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self.connected: BooleanAttribute = BooleanAttribute(name="connected", parent=self)
        self.interval: DurationAttribute = DurationAttribute(name="interval", parent=self)

        self.user_id: Optional[str] = None

        # Configure logging
        if 'log_level' in config and config['log_level'] is not None:
            config['log_level'] = config['log_level'].upper()
            if config['log_level'] in logging.getLevelNamesMapping():
                LOG.setLevel(config['log_level'])
                self.log_level._set_value(config['log_level'])  # pylint: disable=protected-access
                logging.getLogger('requests').setLevel(config['log_level'])
                logging.getLogger('urllib3').setLevel(config['log_level'])
                logging.getLogger('oauthlib').setLevel(config['log_level'])
            else:
                raise ConfigurationError(f'Invalid log level: "{config["log_level"]}" not in {list(logging.getLevelNamesMapping().keys())}')
        if 'api_log_level' in config and config['api_log_level'] is not None:
            config['api_log_level'] = config['api_log_level'].upper()
            if config['api_log_level'] in logging.getLevelNamesMapping():
                LOG_API.setLevel(config['api_log_level'])
            else:
                raise ConfigurationError(f'Invalid log level: "{config["log_level"]}" not in {list(logging.getLevelNamesMapping().keys())}')
        LOG.info("Loading skoda connector with config %s", config_remove_credentials(self.config))

        username: Optional[str] = None
        password: Optional[str] = None
        if 'username' in self.config and 'password' in self.config:
            username = self.config['username']
            password = self.config['password']
        else:
            if 'netrc' in self.config:
                netrc_filename: str = self.config['netrc']
            else:
                netrc_filename = os.path.join(os.path.expanduser("~"), ".netrc")
            try:
                secrets = netrc.netrc(file=netrc_filename)
                secret: tuple[str, str, str] | None = secrets.authenticators("skoda")
                if secret is None:
                    raise AuthenticationError(f'Authentication using {netrc_filename} failed: skoda not found in netrc')
                username, _, password = secret
            except netrc.NetrcParseError as err:
                LOG.error('Authentification using %s failed: %s', netrc_filename, err)
                raise AuthenticationError(f'Authentication using {netrc_filename} failed: {err}') from err
            except TypeError as err:
                if 'username' not in self.config:
                    raise AuthenticationError(f'"skoda" entry was not found in {netrc_filename} netrc-file.'
                                              ' Create it or provide username and password in config') from err
            except FileNotFoundError as err:
                raise AuthenticationError(f'{netrc_filename} netrc-file was not found. Create it or provide username and password in config') from err

        interval: int = 300
        if 'interval' in self.config:
            interval = self.config['interval']
            if interval < 300:
                raise ValueError('Intervall must be at least 300 seconds')
        self.max_age: int = interval - 1
        if 'max_age' in self.config:
            self.max_age = self.config['max_age']
        self.interval._set_value(timedelta(seconds=interval))  # pylint: disable=protected-access

        if username is None or password is None:
            raise AuthenticationError('Username or password not provided')

        self._manager: SessionManager = SessionManager(tokenstore=car_connectivity.get_tokenstore(), cache=car_connectivity.get_cache())
        session: requests.Session = self._manager.get_session(Service.MY_SKODA, SessionUser(username=username, password=password))
        if not isinstance(session, MySkodaSession):
            raise AuthenticationError('Could not create session')
        self.session: MySkodaSession = session
        self.session.refresh()

        self._elapsed: List[timedelta] = []

    def startup(self) -> None:
        self._stop_event.clear()
        # Start background thread for Rest API polling
        self._background_thread = threading.Thread(target=self._background_loop, daemon=False)
        self._background_thread.start()
        # Start background thread for MQTT connection
        self._background_connect_thread = threading.Thread(target=self._background_connect_loop, daemon=False)
        self._background_connect_thread.start()
        # Start MQTT thread
        self._mqtt_client.loop_start()

    def _background_connect_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._mqtt_client.connect()
                break
            except ConnectionRefusedError as e:
                LOG.error('Could not connect to MQTT-Server: %s, will retry in 10 seconds', e)
                self._stop_event.wait(10)

    def _background_loop(self) -> None:
        self._stop_event.clear()
        while not self._stop_event.is_set():
            interval = 300
            try:
                try:
                    self.fetch_all()
                    self.last_update._set_value(value=datetime.now())  # pylint: disable=protected-access
                    if self.interval.value is not None:
                        interval: int = self.interval.value.total_seconds()
                except Exception:
                    if self.interval.value is not None:
                        interval: int = self.interval.value.total_seconds()
                    raise
            except TooManyRequestsError as err:
                LOG.error('Retrieval error during update. Too many requests from your account (%s). Will try again after 15 minutes', str(err))
                self._stop_event.wait(900)
            except RetrievalError as err:
                LOG.error('Retrieval error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self._stop_event.wait(interval)
            except APICompatibilityError as err:
                LOG.error('API compatability error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self._stop_event.wait(interval)
            except TemporaryAuthenticationError as err:
                LOG.error('Temporary authentification error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self._stop_event.wait(interval)
            else:
                self._stop_event.wait(interval)

    def persist(self) -> None:
        """
        Persists the current state using the manager's persist method.

        This method calls the `persist` method of the `_manager` attribute to save the current state.
        """
        self._manager.persist()
        return

    def shutdown(self) -> None:
        """
        Shuts down the connector by persisting current state, closing the session,
        and cleaning up resources.

        This method performs the following actions:
        1. Persists the current state.
        2. Closes the session.
        3. Sets the session and manager to None.
        4. Calls the shutdown method of the base connector.
        """
        self._mqtt_client.disconnect()
        # Stop MQTT thread
        self._mqtt_client.loop_stop()
        # Disable and remove all vehicles managed soley by this connector
        for vehicle in self.car_connectivity.garage.list_vehicles():
            if len(vehicle.managing_connectors) == 1 and self in vehicle.managing_connectors:
                self.car_connectivity.garage.remove_vehicle(vehicle.id)
                vehicle.enabled = False
        self._stop_event.set()
        if self._background_thread is not None:
            self._background_thread.join()
        if self._background_connect_thread is not None:
            self._background_connect_thread.join()
        self.persist()
        self.session.close()
        return super().shutdown()

    def fetch_all(self) -> None:
        """
        Fetches all necessary data for the connector.

        This method calls the `fetch_vehicles` method to retrieve vehicle data.
        """
        self.fetch_vehicles()
        self.car_connectivity.transaction_end()

    def fetch_user(self) -> None:
        """
        Fetches the user data from the Skoda Connect API.

        This method sends a request to the Skoda Connect API to retrieve the user data associated with the user's account.

        Returns:
            None
        """
        url = 'https://mysmob.api.connect.skoda-auto.cz/api/v1/users'
        data: Dict[str, Any] | None = self._fetch_data(url, session=self.session)
        if data:
            if 'id' in data and data['id'] is not None:
                self.user_id = data['id']

    def fetch_vehicles(self) -> None:
        """
        Fetches the list of vehicles from the Skoda Connect API and updates the garage with new vehicles.
        This method sends a request to the Skoda Connect API to retrieve the list of vehicles associated with the user's account.
        If new vehicles are found in the response, they are added to the garage.

        Returns:
            None
        """
        garage: Garage = self.car_connectivity.garage
        url = 'https://mysmob.api.connect.skoda-auto.cz/api/v2/garage'
        data: Dict[str, Any] | None = self._fetch_data(url, session=self.session)
        seen_vehicle_vins: set[str] = set()
        if data is not None:
            if 'vehicles' in data and data['vehicles'] is not None:
                for vehicle_dict in data['vehicles']:
                    if 'vin' in vehicle_dict and vehicle_dict['vin'] is not None:
                        seen_vehicle_vins.add(vehicle_dict['vin'])
                        vehicle: Optional[SkodaVehicle] = garage.get_vehicle(vehicle_dict['vin'])  # pyright: ignore[reportAssignmentType]
                        if not vehicle:
                            vehicle = SkodaVehicle(vin=vehicle_dict['vin'], garage=garage, managing_connector=self)
                            garage.add_vehicle(vehicle_dict['vin'], vehicle)

                        if 'licensePlate' in vehicle_dict and vehicle_dict['licensePlate'] is not None:
                            vehicle.license_plate._set_value(vehicle_dict['licensePlate'])  # pylint: disable=protected-access
                        else:
                            vehicle.license_plate._set_value(None)  # pylint: disable=protected-access

                        log_extra_keys(LOG_API, 'vehicles', vehicle_dict,  {'vin', 'licensePlate'})
                        self.fetch_vehicle_status(vehicle)
                    else:
                        raise APIError('Could not parse vehicle, vin missing')
        for vin in set(garage.list_vehicle_vins()) - seen_vehicle_vins:
            vehicle_to_remove = garage.get_vehicle(vin)
            if vehicle_to_remove is not None and vehicle_to_remove.is_managed_by_connector(self):
                garage.remove_vehicle(vin)

    def fetch_vehicle_status(self, vehicle: SkodaVehicle) -> None:
        """
        Fetches the status of a vehicle from the Skoda API.

        Args:
            vehicle (GenericVehicle): The vehicle object containing the VIN.

        Returns:
            None
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        url = f'https://mysmob.api.connect.skoda-auto.cz/api/v2/garage/vehicles/{vin}?' \
            'connectivityGenerations=MOD1&connectivityGenerations=MOD2&connectivityGenerations=MOD3&connectivityGenerations=MOD4'
        vehicle_data: Dict[str, Any] | None = self._fetch_data(url, self.session)
        if vehicle_data:
            if 'softwareVersion' in vehicle_data and vehicle_data['softwareVersion'] is not None:
                vehicle.software.version._set_value(vehicle_data['softwareVersion'])  # pylint: disable=protected-access
            else:
                vehicle.software.version._set_value(None)  # pylint: disable=protected-access
            if 'capabilities' in vehicle_data and vehicle_data['capabilities'] is not None:
                if 'capabilities' in vehicle_data['capabilities'] and vehicle_data['capabilities']['capabilities'] is not None:
                    found_capabilities = set()
                    for capability_dict in vehicle_data['capabilities']['capabilities']:
                        if 'id' in capability_dict and capability_dict['id'] is not None:
                            capability_id = capability_dict['id']
                            found_capabilities.add(capability_id)
                            if vehicle.capabilities.has_capability(capability_id):
                                capability: Capability = vehicle.capabilities.get_capability(capability_id)  # pyright: ignore[reportAssignmentType]
                            else:
                                capability = Capability(capability_id=capability_id, capabilities=vehicle.capabilities)
                                vehicle.capabilities.add_capability(capability_id, capability)
                        else:
                            raise APIError('Could not parse capability, id missing')
                    for capability_id in vehicle.capabilities.capabilities.keys() - found_capabilities:
                        vehicle.capabilities.remove_capability(capability_id)
                else:
                    vehicle.capabilities.clear_capabilities()
            else:
                vehicle.capabilities.clear_capabilities()

            if 'specification' in vehicle_data and vehicle_data['specification'] is not None:
                if 'model' in vehicle_data['specification'] and vehicle_data['specification']['model'] is not None:
                    vehicle.model._set_value(vehicle_data['specification']['model'])  # pylint: disable=protected-access
                else:
                    vehicle.model._set_value(None)  # pylint: disable=protected-access
                log_extra_keys(LOG_API, 'specification', vehicle_data['specification'],  {'model'})
            else:
                vehicle.model._set_value(None)  # pylint: disable=protected-access
            log_extra_keys(LOG_API, 'api/v2/garage/vehicles/VIN', vehicle_data, {'softwareVersion'})

        url = f'https://mysmob.api.connect.skoda-auto.cz/api/v2/vehicle-status/{vin}/driving-range'
        range_data: Dict[str, Any] | None = self._fetch_data(url, self.session)
        if range_data:
            captured_at: datetime = robust_time_parse(range_data['carCapturedTimestamp'])
            # Check vehicle type and if it does not match the current vehicle type, create a new vehicle object using copy constructor
            if 'carType' in range_data and range_data['carType'] is not None:
                try:
                    car_type = GenericVehicle.Type(range_data['carType'])
                    if car_type == GenericVehicle.Type.ELECTRIC and not isinstance(vehicle, SkodaElectricVehicle):
                        LOG.debug('Promoting %s to SkodaElectricVehicle object for %s', vehicle.__class__.__name__, vin)
                        vehicle = SkodaElectricVehicle(origin=vehicle)
                        self.car_connectivity.garage.replace_vehicle(vin, vehicle)
                    elif car_type in [GenericVehicle.Type.FUEL,
                                      GenericVehicle.Type.GASOLINE,
                                      GenericVehicle.Type.PETROL,
                                      GenericVehicle.Type.DIESEL,
                                      GenericVehicle.Type.CNG,
                                      GenericVehicle.Type.LPG] \
                            and not isinstance(vehicle, SkodaCombustionVehicle):
                        LOG.debug('Promoting %s to SkodaCombustionVehicle object for %s', vehicle.__class__.__name__, vin)
                        vehicle = SkodaCombustionVehicle(origin=vehicle)
                        self.car_connectivity.garage.replace_vehicle(vin, vehicle)
                    elif car_type == GenericVehicle.Type.HYBRID and not isinstance(vehicle, SkodaHybridVehicle):
                        LOG.debug('Promoting %s to SkodaHybridVehicle object for %s', vehicle.__class__.__name__, vin)
                        vehicle = SkodaHybridVehicle(origin=vehicle)
                        self.car_connectivity.garage.replace_vehicle(vin, vehicle)
                except ValueError:
                    LOG_API.warning('Unknown car type %s', range_data['carType'])
                    car_type = GenericVehicle.Type.UNKNOWN
                vehicle.type._set_value(car_type)  # pylint: disable=protected-access
            if 'totalRangeInKm' in range_data and range_data['totalRangeInKm'] is not None:
                # pylint: disable-next=protected-access
                vehicle.drives.total_range._set_value(value=range_data['totalRangeInKm'], measured=captured_at, unit=Length.KM)
            else:
                vehicle.drives.total_range._set_value(None, measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access

            drive_ids: set[str] = {'primary', 'secondary'}
            for drive_id in drive_ids:
                if f'{drive_id}EngineRange' in range_data and range_data[f'{drive_id}EngineRange'] is not None:
                    try:
                        engine_type: GenericDrive.Type = GenericDrive.Type(range_data[f'{drive_id}EngineRange']['engineType'])
                    except ValueError:
                        LOG_API.warning('Unknown engine_type type %s', range_data[f'{drive_id}EngineRange']['engineType'])
                        engine_type: GenericDrive.Type = GenericDrive.Type.UNKNOWN

                    if drive_id in vehicle.drives.drives:
                        drive: GenericDrive = vehicle.drives.drives[drive_id]
                    else:
                        if engine_type == GenericDrive.Type.ELECTRIC:
                            drive = ElectricDrive(drive_id=drive_id, drives=vehicle.drives)
                        elif engine_type in [GenericDrive.Type.FUEL,
                                             GenericDrive.Type.GASOLINE,
                                             GenericDrive.Type.PETROL,
                                             GenericDrive.Type.DIESEL,
                                             GenericDrive.Type.CNG,
                                             GenericDrive.Type.LPG]:
                            drive = CombustionDrive(drive_id=drive_id, drives=vehicle.drives)
                        else:
                            drive = GenericDrive(drive_id=drive_id, drives=vehicle.drives)
                        drive.type._set_value(engine_type)  # pylint: disable=protected-access
                        vehicle.drives.add_drive(drive)
                    if 'currentSoCInPercent' in range_data[f'{drive_id}EngineRange'] \
                            and range_data[f'{drive_id}EngineRange']['currentSoCInPercent'] is not None:
                        # pylint: disable-next=protected-access
                        drive.level._set_value(value=range_data[f'{drive_id}EngineRange']['currentSoCInPercent'], measured=captured_at)
                    elif 'currentFuelLevelInPercent' in range_data[f'{drive_id}EngineRange'] \
                            and range_data[f'{drive_id}EngineRange']['currentFuelLevelInPercent'] is not None:
                        # pylint: disable-next=protected-access
                        drive.level._set_value(value=range_data[f'{drive_id}EngineRange']['currentFuelLevelInPercent'], measured=captured_at)
                    else:
                        drive.level._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                    if 'remainingRangeInKm' in range_data[f'{drive_id}EngineRange'] and range_data[f'{drive_id}EngineRange']['remainingRangeInKm'] is not None:
                        # pylint: disable-next=protected-access
                        drive.range._set_value(value=range_data[f'{drive_id}EngineRange']['remainingRangeInKm'], measured=captured_at, unit=Length.KM)
                    else:
                        drive.range._set_value(None, measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access

                    log_extra_keys(LOG_API, f'{drive_id}EngineRange', range_data[f'{drive_id}EngineRange'], {'engineType',
                                                                                                             'currentSoCInPercent',
                                                                                                             'currentFuelLevelInPercent',
                                                                                                             'remainingRangeInKm'})
            log_extra_keys(LOG_API, '/api/v2/vehicle-status/{vin}/driving-range', range_data, {'carCapturedTimestamp',
                                                                                               'carType',
                                                                                               'totalRangeInKm',
                                                                                               'primaryEngineRange',
                                                                                               'secondaryEngineRange'})

        url = f'https://api.connect.skoda-auto.cz/api/v2/vehicle-status/{vin}'
        vehicle_status_data: Dict[str, Any] | None = self._fetch_data(url, self.session)
        if vehicle_status_data:
            if 'remote' in vehicle_status_data and vehicle_status_data['remote'] is not None:
                vehicle_status_data = vehicle_status_data['remote']
        if vehicle_status_data:
            if 'capturedAt' in vehicle_status_data and vehicle_status_data['capturedAt'] is not None:
                captured_at: datetime = robust_time_parse(vehicle_status_data['capturedAt'])
            else:
                raise APIError('Could not fetch vehicle status, capturedAt missing')
            if 'mileageInKm' in vehicle_status_data and vehicle_status_data['mileageInKm'] is not None:
                vehicle.odometer._set_value(value=vehicle_status_data['mileageInKm'], measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access
            else:
                vehicle.odometer._set_value(value=None, measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access
            if 'status' in vehicle_status_data and vehicle_status_data['status'] is not None:
                if 'open' in vehicle_status_data['status'] and vehicle_status_data['status']['open'] is not None:
                    if vehicle_status_data['status']['open'] == 'YES':
                        vehicle.doors.open_state._set_value(Doors.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['status']['open'] == 'NO':
                        vehicle.doors.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        vehicle.doors.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                        LOG_API.info('Unknown door open state: %s', vehicle_status_data['status']['open'])
                else:
                    vehicle.doors.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                if 'locked' in vehicle_status_data['status'] and vehicle_status_data['status']['locked'] is not None:
                    if vehicle_status_data['status']['locked'] == 'YES':
                        vehicle.doors.lock_state._set_value(Doors.LockState.LOCKED, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['status']['locked'] == 'NO':
                        vehicle.doors.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        vehicle.doors.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                        LOG_API.info('Unknown door lock state: %s', vehicle_status_data['status']['locked'])
                else:
                    vehicle.doors.lock_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
            else:
                vehicle.doors.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                vehicle.doors.lock_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
            if 'doors' in vehicle_status_data and vehicle_status_data['doors'] is not None:
                seen_door_ids: set[str] = set()
                for door_status in vehicle_status_data['doors']:
                    if 'name' in door_status and door_status['name'] is not None:
                        door_id = door_status['name']
                        seen_door_ids.add(door_id)
                        if door_id in vehicle.doors.doors:
                            door: Doors.Door = vehicle.doors.doors[door_id]
                        else:
                            door = Doors.Door(door_id=door_id, doors=vehicle.doors)
                            vehicle.doors.doors[door_id] = door
                        if 'status' in door_status and door_status['status'] is not None:
                            if door_status['status'] == 'OPEN':
                                door.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                                door.open_state._set_value(Doors.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                            elif door_status['status'] == 'CLOSED':
                                door.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                                door.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                            elif door_status['status'] == 'LOCKED':
                                door.lock_state._set_value(Doors.LockState.LOCKED, measured=captured_at)  # pylint: disable=protected-access
                                door.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                            elif door_status['status'] == 'UNSUPPORTED':
                                door.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                                door.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                            else:
                                LOG_API.info('Unknown door status %s', door_status['status'])
                                door.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                                door.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                        else:
                            door.lock_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                            door.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        raise APIError('Could not parse door, name missing')
                    log_extra_keys(LOG_API, 'doors', door_status, {'name', 'status'})
                for door_to_remove in set(vehicle.doors.doors) - seen_door_ids:
                    vehicle.doors.doors[door_to_remove].enabled = False
                    vehicle.doors.doors.pop(door_to_remove)
                log_extra_keys(LOG_API, 'status', vehicle_status_data['status'],  {'open', 'locked'})
            else:
                vehicle.doors.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                vehicle.doors.doors = {}
            if 'windows' in vehicle_status_data and vehicle_status_data['windows'] is not None:
                seen_window_ids: set[str] = set()
                all_windows_closed: bool = True
                for window_status in vehicle_status_data['windows']:
                    if 'name' in window_status and window_status['name'] is not None:
                        window_id = window_status['name']
                        seen_window_ids.add(window_id)
                        if window_id in vehicle.windows.windows:
                            window: Windows.Window = vehicle.windows.windows[window_id]
                        else:
                            window = Windows.Window(window_id=window_id, windows=vehicle.windows)
                            vehicle.windows.windows[window_id] = window
                        if 'status' in window_status and window_status['status'] is not None:
                            if window_status['status'] == 'OPEN':
                                all_windows_closed = False
                                window.open_state._set_value(Windows.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                            elif window_status['status'] == 'CLOSED':
                                window.open_state._set_value(Windows.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                            elif window_status['status'] == 'UNSUPPORTED':
                                window.open_state._set_value(Windows.OpenState.UNSUPPORTED, measured=captured_at)  # pylint: disable=protected-access
                            elif window_status['status'] == 'INVALID':
                                window.open_state._set_value(Windows.OpenState.INVALID, measured=captured_at)  # pylint: disable=protected-access
                            else:
                                LOG_API.info('Unknown window status %s', window_status['status'])
                                window.open_state._set_value(Windows.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                        else:
                            window.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        raise APIError('Could not parse window, name missing')
                    log_extra_keys(LOG_API, 'doors', window_status, {'name', 'status'})
                for window_to_remove in set(vehicle.windows.windows) - seen_window_ids:
                    vehicle.windows.windows[window_to_remove].enabled = False
                    vehicle.windows.windows.pop(window_to_remove)
                if all_windows_closed:
                    vehicle.windows.open_state._set_value(Windows.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                else:
                    vehicle.windows.open_state._set_value(Windows.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
            else:
                vehicle.windows.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                vehicle.windows.windows = {}
            if 'lights' in vehicle_status_data and vehicle_status_data['lights'] is not None:
                seen_light_ids: set[str] = set()
                if 'overallStatus' in vehicle_status_data['lights'] and vehicle_status_data['lights']['overallStatus'] is not None:
                    if vehicle_status_data['lights']['overallStatus'] == 'ON':
                        vehicle.lights.light_state._set_value(Lights.LightState.ON, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['lights']['overallStatus'] == 'OFF':
                        vehicle.lights.light_state._set_value(Lights.LightState.OFF, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['lights']['overallStatus'] == 'INVALID':
                        vehicle.lights.light_state._set_value(Lights.LightState.INVALID, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        LOG_API.info('Unknown light status %s', vehicle_status_data['lights']['overallStatus'])
                        vehicle.lights.light_state._set_value(Lights.LightState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                else:
                    vehicle.lights.light_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                if 'lightsStatus' in vehicle_status_data['lights'] and vehicle_status_data['lights']['lightsStatus'] is not None:
                    for light_status in vehicle_status_data['lights']['lightsStatus']:
                        if 'name' in light_status and light_status['name'] is not None:
                            light_id: str = light_status['name']
                            seen_light_ids.add(light_id)
                            if light_id in vehicle.lights.lights:
                                light: Lights.Light = vehicle.lights.lights[light_id]
                            else:
                                light = Lights.Light(light_id=light_id, lights=vehicle.lights)
                                vehicle.lights.lights[light_id] = light
                            if 'status' in light_status and light_status['status'] is not None:
                                if light_status['status'] == 'ON':
                                    light.light_state._set_value(Lights.LightState.ON, measured=captured_at)  # pylint: disable=protected-access
                                elif light_status['status'] == 'OFF':
                                    light.light_state._set_value(Lights.LightState.OFF, measured=captured_at)  # pylint: disable=protected-access
                                elif light_status['status'] == 'INVALID':
                                    light.light_state._set_value(Lights.LightState.INVALID, measured=captured_at)  # pylint: disable=protected-access
                                else:
                                    LOG_API.info('Unknown light status %s', light_status['status'])
                                    light.light_state._set_value(Lights.LightState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                            else:
                                light.light_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                        else:
                            raise APIError('Could not parse light, name missing')
                        log_extra_keys(LOG_API, 'lights', light_status, {'name', 'status'})
                    for light_to_remove in set(vehicle.lights.lights) - seen_light_ids:
                        vehicle.lights.lights[light_to_remove].enabled = False
                        vehicle.lights.lights.pop(light_to_remove)
                else:
                    vehicle.lights.lights = {}
                log_extra_keys(LOG_API, 'lights', vehicle_status_data['lights'], {'overallStatus', 'lightsStatus'})
            log_extra_keys(LOG_API, 'vehicles', vehicle_status_data,  {'capturedAt', 'mileageInKm', 'status', 'doors', 'windows', 'lights'})

    def _record_elapsed(self, elapsed: timedelta) -> None:
        """
        Records the elapsed time.

        Args:
            elapsed (timedelta): The elapsed time to record.
        """
        self._elapsed.append(elapsed)

    def _fetch_data(self, url, session, force=False, allow_empty=False, allow_http_error=False, allowed_errors=None) -> Optional[Dict[str, Any]]:  # noqa: C901
        data: Optional[Dict[str, Any]] = None
        cache_date: Optional[datetime] = None
        if not force and (self.max_age is not None and session.cache is not None and url in session.cache):
            data, cache_date_string = session.cache[url]
            cache_date = datetime.fromisoformat(cache_date_string)
        if data is None or self.max_age is None \
                or (cache_date is not None and cache_date < (datetime.utcnow() - timedelta(seconds=self.max_age))):
            try:
                status_response: requests.Response = session.get(url, allow_redirects=False)
                self._record_elapsed(status_response.elapsed)
                if status_response.status_code in (requests.codes['ok'], requests.codes['multiple_status']):
                    data = status_response.json()
                    if session.cache is not None:
                        session.cache[url] = (data, str(datetime.utcnow()))
                elif status_response.status_code == requests.codes['too_many_requests']:
                    raise TooManyRequestsError('Could not fetch data due to too many requests from your account. '
                                               f'Status Code was: {status_response.status_code}')
                elif status_response.status_code == requests.codes['unauthorized']:
                    LOG.info('Server asks for new authorization')
                    session.login()
                    status_response = session.get(url, allow_redirects=False)

                    if status_response.status_code in (requests.codes['ok'], requests.codes['multiple_status']):
                        data = status_response.json()
                        if session.cache is not None:
                            session.cache[url] = (data, str(datetime.utcnow()))
                    elif not allow_http_error or (allowed_errors is not None and status_response.status_code not in allowed_errors):
                        raise RetrievalError(f'Could not fetch data even after re-authorization. Status Code was: {status_response.status_code}')
                elif not allow_http_error or (allowed_errors is not None and status_response.status_code not in allowed_errors):
                    raise RetrievalError(f'Could not fetch data. Status Code was: {status_response.status_code}')
            except requests.exceptions.ConnectionError as connection_error:
                raise RetrievalError(f'Connection error: {connection_error}.'
                                     ' If this happens frequently, please check if other applications communicate with the Skoda server.') from connection_error
            except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
                raise RetrievalError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
            except requests.exceptions.ReadTimeout as timeout_error:
                raise RetrievalError(f'Timeout during read: {timeout_error}') from timeout_error
            except requests.exceptions.RetryError as retry_error:
                raise RetrievalError(f'Retrying failed: {retry_error}') from retry_error
            except requests.exceptions.JSONDecodeError as json_error:
                if allow_empty:
                    data = None
                else:
                    raise RetrievalError(f'JSON decode error: {json_error}') from json_error
        return data

    def get_version(self) -> str:
        return __version__
