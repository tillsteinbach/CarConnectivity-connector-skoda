"""Module implements the connector to interact with the Skoda API."""  # pylint: disable=too-many-lines
from __future__ import annotations
from typing import TYPE_CHECKING

import threading
import os
import logging
import netrc
from datetime import datetime, timedelta, timezone
import json

import requests


from carconnectivity.garage import Garage
from carconnectivity.vehicle import GenericVehicle
from carconnectivity.errors import AuthenticationError, TooManyRequestsError, RetrievalError, APIError, APICompatibilityError, \
    TemporaryAuthenticationError, ConfigurationError, SetterError
from carconnectivity.util import robust_time_parse, log_extra_keys, config_remove_credentials
from carconnectivity.units import Length, Speed, Power, Temperature
from carconnectivity.doors import Doors
from carconnectivity.windows import Windows
from carconnectivity.lights import Lights
from carconnectivity.drive import GenericDrive, ElectricDrive, CombustionDrive
from carconnectivity.attributes import BooleanAttribute, DurationAttribute, TemperatureAttribute
from carconnectivity.charging import Charging
from carconnectivity.position import Position
from carconnectivity.climatization import Climatization
from carconnectivity.charging_connector import ChargingConnector
from carconnectivity.command_impl import ClimatizationStartStopCommand, ChargingStartStopCommand

from carconnectivity_connectors.base.connector import BaseConnector
from carconnectivity_connectors.skoda.auth.session_manager import SessionManager, SessionUser, Service
from carconnectivity_connectors.skoda.auth.my_skoda_session import MySkodaSession
from carconnectivity_connectors.skoda.vehicle import SkodaVehicle, SkodaElectricVehicle, SkodaCombustionVehicle, SkodaHybridVehicle
from carconnectivity_connectors.skoda.capability import Capability
from carconnectivity_connectors.skoda.charging import SkodaCharging, mapping_skoda_charging_state
from carconnectivity_connectors.skoda.climatization import SkodaClimatization
from carconnectivity_connectors.skoda.error import Error
from carconnectivity_connectors.skoda._version import __version__
from carconnectivity_connectors.skoda.mqtt_client import SkodaMQTTClient

if TYPE_CHECKING:
    from typing import Dict, List, Optional, Any, Set, Union

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
            if config['log_level'] in logging._nameToLevel:
                LOG.setLevel(config['log_level'])
                self.log_level._set_value(config['log_level'])  # pylint: disable=protected-access
                logging.getLogger('requests').setLevel(config['log_level'])
                logging.getLogger('urllib3').setLevel(config['log_level'])
                logging.getLogger('oauthlib').setLevel(config['log_level'])
            else:
                raise ConfigurationError(f'Invalid log level: "{config["log_level"]}" not in {list(logging._nameToLevel.keys())}')
        if 'api_log_level' in config and config['api_log_level'] is not None:
            config['api_log_level'] = config['api_log_level'].upper()
            if config['api_log_level'] in logging._nameToLevel:
                LOG_API.setLevel(config['api_log_level'])
            else:
                raise ConfigurationError(f'Invalid log level: "{config["log_level"]}" not in {list(logging._nameToLevel.keys())}')
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
        self.session.retries = 3
        self.session.timeout = 180
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
        fetch: bool = True
        while not self._stop_event.is_set():
            interval = 300
            try:
                try:
                    if fetch:
                        self.fetch_all()
                        fetch = False
                    else:
                        self.update_vehicles()
                    self.last_update._set_value(value=datetime.now(tz=timezone.utc))  # pylint: disable=protected-access
                    if self.interval.value is not None:
                        interval: float = self.interval.value.total_seconds()
                except Exception:
                    if self.interval.value is not None:
                        interval: float = self.interval.value.total_seconds()
                    raise
            except TooManyRequestsError as err:
                LOG.error('Retrieval error during update. Too many requests from your account (%s). Will try again after 15 minutes', str(err))
                self._stop_event.wait(900)
            except RetrievalError as err:
                LOG.error('Retrieval error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self._stop_event.wait(interval)
            except APIError as err:
                LOG.error('API error during update (%s). Will try again after configured interval of %ss', str(err), interval)
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

                        if 'name' in vehicle_dict and vehicle_dict['name'] is not None:
                            vehicle.name._set_value(vehicle_dict['name'])  # pylint: disable=protected-access
                        else:
                            vehicle.name._set_value(None)  # pylint: disable=protected-access

                        log_extra_keys(LOG_API, 'vehicles', vehicle_dict,  {'vin', 'licensePlate', 'name'})

                        vehicle = self.fetch_vehicle_details(vehicle)
                    else:
                        raise APIError('Could not parse vehicle, vin missing')
        for vin in set(garage.list_vehicle_vins()) - seen_vehicle_vins:
            vehicle_to_remove = garage.get_vehicle(vin)
            if vehicle_to_remove is not None and vehicle_to_remove.is_managed_by_connector(self):
                garage.remove_vehicle(vin)
        self.update_vehicles()

    def update_vehicles(self) -> None:
        """
        Updates the status of all vehicles in the garage managed by this connector.

        This method iterates through all vehicle VINs in the garage, and for each vehicle that is
        managed by this connector and is an instance of SkodaVehicle, it updates the vehicle's status
        by fetching data from various APIs. If the vehicle is an instance of SkodaElectricVehicle,
        it also fetches charging information.

        Returns:
            None
        """
        garage: Garage = self.car_connectivity.garage
        for vin in set(garage.list_vehicle_vins()):
            vehicle_to_update: Optional[GenericVehicle] = garage.get_vehicle(vin)
            if vehicle_to_update is not None and isinstance(vehicle_to_update, SkodaVehicle) and vehicle_to_update.is_managed_by_connector(self):
                vehicle_to_update = self.fetch_vehicle_status(vehicle_to_update)
                vehicle_to_update = self.fetch_driving_range(vehicle_to_update)
                if vehicle_to_update.capabilities is not None and vehicle_to_update.capabilities.enabled:
                    if vehicle_to_update.capabilities.has_capability('PARKING_POSITION'):
                        vehicle_to_update = self.fetch_position(vehicle_to_update)
                    if vehicle_to_update.capabilities.has_capability('CHARGING') and isinstance(vehicle_to_update, SkodaElectricVehicle):
                        vehicle_to_update = self.fetch_charging(vehicle_to_update)
                    if vehicle_to_update.capabilities.has_capability('AIR_CONDITIONING'):
                        vehicle_to_update = self.fetch_air_conditioning(vehicle_to_update)

    def fetch_charging(self, vehicle: SkodaElectricVehicle, no_cache: bool = False) -> SkodaElectricVehicle:
        """
        Fetches the charging information for a given Skoda electric vehicle.

        Args:
            vehicle (SkodaElectricVehicle): The Skoda electric vehicle object.

        Raises:
            APIError: If the VIN is missing or if the carCapturedTimestamp is missing in the response data.
            ValueError: If the vehicle has no charging object.
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        if vehicle.charging is None:
            raise ValueError('Vehicle has no charging object')
        url = f'https://mysmob.api.connect.skoda-auto.cz/api/v1/charging/{vin}'
        data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        if data is not None:
            start_stop_command: ChargingStartStopCommand = ChargingStartStopCommand(parent=vehicle.charging.commands)
            start_stop_command._add_on_set_hook(self.__on_charging_start_stop)  # pylint: disable=protected-access
            start_stop_command.enabled = True
            vehicle.charging.commands.add_command(start_stop_command)
            if 'carCapturedTimestamp' in data and data['carCapturedTimestamp'] is not None:
                captured_at: datetime = robust_time_parse(data['carCapturedTimestamp'])
            else:
                raise APIError('Could not fetch charging, carCapturedTimestamp missing')
            if 'isVehicleInSavedLocation' in data and data['isVehicleInSavedLocation'] is not None:
                if vehicle.charging is not None:
                    if not isinstance(vehicle.charging, SkodaCharging):
                        vehicle.charging = SkodaCharging(origin=vehicle.charging)
                    # pylint: disable-next=protected-access
                    vehicle.charging.is_in_saved_location._set_value(data['isVehicleInSavedLocation'], measured=captured_at)
            if 'status' in data and data['status'] is not None:
                if 'state' in data['status'] and data['status']['state'] is not None:
                    if data['status']['state'] in [item.name for item in SkodaCharging.SkodaChargingState]:
                        skoda_charging_state = SkodaCharging.SkodaChargingState[data['status']['state']]
                        charging_state: Charging.ChargingState = mapping_skoda_charging_state[skoda_charging_state]
                    else:
                        LOG_API.info('Unkown charging state %s not in %s', data['status']['state'], str(SkodaCharging.SkodaChargingState))
                        charging_state = Charging.ChargingState.UNKNOWN

                    # pylint: disable-next=protected-access
                    vehicle.charging.state._set_value(value=charging_state, measured=captured_at)
                else:
                    vehicle.charging.state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                if 'chargingRateInKilometersPerHour' in data['status'] and data['status']['chargingRateInKilometersPerHour'] is not None:
                    # pylint: disable-next=protected-access
                    vehicle.charging.rate._set_value(value=data['status']['chargingRateInKilometersPerHour'], measured=captured_at, unit=Speed.KMH)
                else:
                    vehicle.charging.rate._set_value(None, measured=captured_at, unit=Speed.KMH)  # pylint: disable=protected-access
                if 'chargePowerInKw' in data['status'] and data['status']['chargePowerInKw'] is not None:
                    # pylint: disable-next=protected-access
                    vehicle.charging.power._set_value(value=data['status']['chargePowerInKw'], measured=captured_at, unit=Power.KW)
                else:
                    vehicle.charging.power._set_value(None, measured=captured_at, unit=Power.KW)  # pylint: disable=protected-access
                if 'remainingTimeToFullyChargedInMinutes' in data['status'] and data['status']['remainingTimeToFullyChargedInMinutes'] is not None:
                    remaining_duration: timedelta = timedelta(minutes=data['status']['remainingTimeToFullyChargedInMinutes'])
                    estimated_date_reached: datetime = captured_at + remaining_duration
                    estimated_date_reached = estimated_date_reached.replace(second=0, microsecond=0)
                    # pylint: disable-next=protected-access
                    vehicle.charging.estimated_date_reached._set_value(value=estimated_date_reached, measured=captured_at)
                else:
                    vehicle.charging.estimated_date_reached._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                if 'chargeType' in data['status'] and data['status']['chargeType'] is not None:
                    if data['status']['chargeType'] in [item.name for item in Charging.ChargingType]:
                        charge_type: Charging.ChargingType = Charging.ChargingType[data['status']['chargeType']]
                    else:
                        LOG_API.info('Unknown charge type %s not in %s', data['status']['chargeType'], str(Charging.ChargingType))
                        charge_type = Charging.ChargingType.UNKNOWN
                    # pylint: disable-next=protected-access
                    vehicle.charging.type._set_value(value=charge_type, measured=captured_at)
                else:
                    # pylint: disable-next=protected-access
                    vehicle.charging.type._set_value(None, measured=captured_at)
                if 'battery' in data['status'] and data['status']['battery'] is not None:
                    for drive in vehicle.drives.drives.values():
                        # Assume first electric drive is the right one
                        if isinstance(drive, ElectricDrive):
                            if 'remainingCruisingRangeInMeters' in data['status']['battery'] \
                                    and data['status']['battery']['remainingCruisingRangeInMeters'] is not None:
                                cruising_range_in_km: float = data['status']['battery']['remainingCruisingRangeInMeters'] / 1000
                                # pylint: disable-next=protected-access
                                drive.range._set_value(value=cruising_range_in_km, measured=captured_at, unit=Length.KM)
                            if 'stateOfChargeInPercent' in data['status']['battery'] \
                                    and data['status']['battery']['stateOfChargeInPercent'] is not None:
                                # pylint: disable-next=protected-access
                                drive.level._set_value(value=data['status']['battery']['stateOfChargeInPercent'], measured=captured_at)
                            log_extra_keys(LOG_API, 'status', data['status']['battery'],  {'remainingCruisingRangeInMeters',
                                                                                           'stateOfChargeInPercent'})
                            break
                log_extra_keys(LOG_API, 'status', data['status'],  {'chargingRateInKilometersPerHour',
                                                                    'chargePowerInKw',
                                                                    'remainingTimeToFullyChargedInMinutes',
                                                                    'state',
                                                                    'chargeType',
                                                                    'battery'})
            if 'settings' in data and data['settings'] is not None:
                if 'targetStateOfChargeInPercent' in data['settings'] and data['settings']['targetStateOfChargeInPercent'] is not None \
                        and vehicle.charging is not None and vehicle.charging.settings is not None:
                    # pylint: disable-next=protected-access
                    vehicle.charging.settings.target_level._set_value(value=data['settings']['targetStateOfChargeInPercent'], measured=captured_at)
                else:
                    vehicle.charging.settings.target_level._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                if 'maxChargeCurrentAc' in data['settings'] and data['settings']['maxChargeCurrentAc'] is not None \
                        and vehicle.charging is not None and vehicle.charging.settings is not None:
                    if data['settings']['maxChargeCurrentAc'] == 'MAXIMUM':
                        vehicle.charging.settings.maximum_current._set_value(value=11, measured=captured_at)  # pylint: disable=protected-access
                    elif data['settings']['maxChargeCurrentAc'] == 'REDUCED':
                        vehicle.charging.settings.maximum_current._set_value(value=6, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        LOG_API.info('Unknown maxChargeCurrentAc %s not in %s', data['settings']['maxChargeCurrentAc'], ['MAXIMUM', 'REDUCED'])
                        vehicle.charging.settings.maximum_current._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                else:
                    vehicle.charging.settings.maximum_current._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                if 'autoUnlockPlugWhenCharged' in data['settings'] and data['settings']['autoUnlockPlugWhenCharged'] is not None:
                    if data['settings']['autoUnlockPlugWhenCharged'] in ['ON', 'PERMANENT']:
                        vehicle.charging.settings.auto_unlock._set_value(True, measured=captured_at)  # pylint: disable=protected-access
                    elif data['settings']['autoUnlockPlugWhenCharged'] == 'OFF':
                        vehicle.charging.settings.auto_unlock._set_value(False, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        LOG_API.info('Unknown autoUnlockPlugWhenCharged %s not in %s', data['settings']['autoUnlockPlugWhenCharged'],
                                     ['ON', 'PERMANENT', 'OFF'])
                        vehicle.charging.settings.auto_unlock._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                if 'preferredChargeMode' in data['settings'] and data['settings']['preferredChargeMode'] is not None:
                    if not isinstance(vehicle.charging, SkodaCharging):
                        vehicle.charging = SkodaCharging(origin=vehicle.charging)
                    if data['settings']['preferredChargeMode'] in [item.name for item in SkodaCharging.SkodaChargeMode]:
                        preferred_charge_mode: SkodaCharging.SkodaChargeMode = SkodaCharging.SkodaChargeMode[data['settings']['preferredChargeMode']]
                    else:
                        LOG_API.info('Unkown charge mode %s not in %s', data['settings']['preferredChargeMode'], str(SkodaCharging.SkodaChargeMode))
                        preferred_charge_mode = SkodaCharging.SkodaChargeMode.UNKNOWN

                    if isinstance(vehicle.charging.settings, SkodaCharging.Settings):
                        # pylint: disable-next=protected-access
                        vehicle.charging.settings.preferred_charge_mode._set_value(value=preferred_charge_mode, measured=captured_at)
                else:
                    if vehicle.charging is not None and isinstance(vehicle.charging.settings, SkodaCharging.Settings):
                        vehicle.charging.settings.preferred_charge_mode._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                if 'availableChargeModes' in data['settings'] and data['settings']['availableChargeModes'] is not None:
                    if not isinstance(vehicle.charging, SkodaCharging):
                        vehicle.charging = SkodaCharging(origin=vehicle.charging)
                    available_charge_modes: list[str] = data['settings']['availableChargeModes']
                    if vehicle.charging is not None and isinstance(vehicle.charging.settings, SkodaCharging.Settings):
                        # pylint: disable-next=protected-access
                        vehicle.charging.settings.available_charge_modes._set_value('.'.join(available_charge_modes), measured=captured_at)
                else:
                    if vehicle.charging is not None and isinstance(vehicle.charging.settings, SkodaCharging.Settings):
                        vehicle.charging.settings.available_charge_modes._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                if 'chargingCareMode' in data['settings'] and data['settings']['chargingCareMode'] is not None:
                    if not isinstance(vehicle.charging, SkodaCharging):
                        vehicle.charging = SkodaCharging(origin=vehicle.charging)
                    if data['settings']['chargingCareMode'] in [item.name for item in SkodaCharging.SkodaChargingCareMode]:
                        charge_mode: SkodaCharging.SkodaChargingCareMode = SkodaCharging.SkodaChargingCareMode[data['settings']['chargingCareMode']]
                    else:
                        LOG_API.info('Unknown charging care mode %s not in %s', data['settings']['chargingCareMode'], str(SkodaCharging.SkodaChargingCareMode))
                        charge_mode = SkodaCharging.SkodaChargingCareMode.UNKNOWN
                    if vehicle.charging is not None and isinstance(vehicle.charging.settings, SkodaCharging.Settings):
                        # pylint: disable-next=protected-access
                        vehicle.charging.settings.charging_care_mode._set_value(value=charge_mode, measured=captured_at)
                else:
                    if vehicle.charging is not None and isinstance(vehicle.charging.settings, SkodaCharging.Settings):
                        vehicle.charging.settings.charging_care_mode._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                if 'batterySupport' in data['settings'] and data['settings']['batterySupport'] is not None:
                    if not isinstance(vehicle.charging, SkodaCharging):
                        vehicle.charging = SkodaCharging(origin=vehicle.charging)
                    if data['settings']['batterySupport'] in [item.name for item in SkodaCharging.SkodaBatterySupport]:
                        battery_support: SkodaCharging.SkodaBatterySupport = SkodaCharging.SkodaBatterySupport[data['settings']['batterySupport']]
                    else:
                        LOG_API.info('Unknown battery support %s not in %s', data['settings']['batterySupport'], str(SkodaCharging.SkodaBatterySupport))
                        battery_support = SkodaCharging.SkodaBatterySupport.UNKNOWN
                    if vehicle.charging is not None and isinstance(vehicle.charging.settings, SkodaCharging.Settings):
                        # pylint: disable-next=protected-access
                        vehicle.charging.settings.battery_support._set_value(value=battery_support, measured=captured_at)
                else:
                    if vehicle.charging is not None and isinstance(vehicle.charging.settings, SkodaCharging.Settings):
                        vehicle.charging.settings.battery_support._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                log_extra_keys(LOG_API, 'settings', data['settings'],  {'targetStateOfChargeInPercent', 'maxChargeCurrentAc', 'autoUnlockPlugWhenCharged',
                                                                        'preferredChargeMode', 'availableChargeModes', 'chargingCareMode', 'batterySupport'})
            if 'errors' in data and data['errors'] is not None:
                found_errors: Set[str] = set()
                if not isinstance(vehicle.charging, SkodaCharging):
                    vehicle.charging = SkodaCharging(origin=vehicle.charging)
                for error_dict in data['errors']:
                    if 'type' in error_dict and error_dict['type'] is not None:
                        if error_dict['type'] not in vehicle.charging.errors:
                            error: Error = Error(object_id=error_dict['type'])
                        else:
                            error = vehicle.charging.errors[error_dict['type']]
                        if error_dict['type'] in [item.name for item in Error.ChargingError]:
                            error_type: Error.ChargingError = Error.ChargingError[error_dict['type']]
                        else:
                            LOG_API.info('Unknown charging error type %s not in %s', error_dict['type'], str(Error.ChargingError))
                            error_type = Error.ChargingError.UNKNOWN
                        error.type._set_value(error_type, measured=captured_at)  # pylint: disable=protected-access
                        if 'description' in error_dict and error_dict['description'] is not None:
                            error.description._set_value(error_dict['description'], measured=captured_at)  # pylint: disable=protected-access
                    log_extra_keys(LOG_API, 'errors', error_dict,  {'type', 'description'})
                if vehicle.charging is not None and vehicle.charging.errors is not None and len(vehicle.charging.errors) > 0:
                    for error_id in vehicle.charging.errors.keys()-found_errors:
                        vehicle.charging.errors.pop(error_id)
            else:
                if isinstance(vehicle.charging, SkodaCharging):
                    vehicle.charging.errors.clear()
            log_extra_keys(LOG_API, 'charging data', data,  {'carCapturedTimestamp', 'status', 'isVehicleInSavedLocation', 'errors', 'settings'})
        return vehicle

    def fetch_position(self, vehicle: SkodaVehicle, no_cache: bool = False) -> SkodaVehicle:
        """
        Fetches the position of the given Skoda vehicle and updates its position attributes.

        Args:
            vehicle (SkodaVehicle): The Skoda vehicle object containing the VIN and position attributes.

        Returns:
            SkodaVehicle: The updated Skoda vehicle object with the fetched position data.

        Raises:
            APIError: If the VIN is missing.
            ValueError: If the vehicle has no position object.
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        if vehicle.position is None:
            raise ValueError('Vehicle has no charging object')
        url = f'https://mysmob.api.connect.skoda-auto.cz/api/v1/maps/positions?vin={vin}'
        data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        if data is not None:
            if 'positions' in data and data['positions'] is not None:
                for position_dict in data['positions']:
                    if 'type' in position_dict and position_dict['type'] == 'VEHICLE':
                        if 'gpsCoordinates' in position_dict and position_dict['gpsCoordinates'] is not None:
                            if 'latitude' in position_dict['gpsCoordinates'] and position_dict['gpsCoordinates']['latitude'] is not None:
                                latitude: Optional[float] = position_dict['gpsCoordinates']['latitude']
                            else:
                                latitude = None
                            if 'longitude' in position_dict['gpsCoordinates'] and position_dict['gpsCoordinates']['longitude'] is not None:
                                longitude: Optional[float] = position_dict['gpsCoordinates']['longitude']
                            else:
                                longitude = None
                            vehicle.position.latitude._set_value(latitude)  # pylint: disable=protected-access
                            vehicle.position.longitude._set_value(longitude)  # pylint: disable=protected-access
                            vehicle.position.position_type._set_value(Position.PositionType.PARKING)  # pylint: disable=protected-access
                        else:
                            vehicle.position.latitude._set_value(None)  # pylint: disable=protected-access
                            vehicle.position.longitude._set_value(None)  # pylint: disable=protected-access
                            vehicle.position.position_type._set_value(None)  # pylint: disable=protected-access
                    else:
                        vehicle.position.latitude._set_value(None)  # pylint: disable=protected-access
                        vehicle.position.longitude._set_value(None)  # pylint: disable=protected-access
                        vehicle.position.position_type._set_value(None)  # pylint: disable=protected-access
                    log_extra_keys(LOG_API, 'positions', position_dict,  {'type',
                                                                          'gpsCoordinates',
                                                                          'address'})
            else:
                vehicle.position.latitude._set_value(None)  # pylint: disable=protected-access
                vehicle.position.longitude._set_value(None)  # pylint: disable=protected-access
                vehicle.position.position_type._set_value(None)  # pylint: disable=protected-access
        return vehicle

    def fetch_air_conditioning(self, vehicle: SkodaVehicle, no_cache: bool = False) -> SkodaVehicle:
        """
        Fetches the air conditioning data for a given Skoda vehicle and updates the vehicle object with the retrieved data.

        Args:
            vehicle (SkodaVehicle): The vehicle object for which to fetch air conditioning data.

        Returns:
            SkodaVehicle: The updated vehicle object with the fetched air conditioning data.

        Raises:
            APIError: If the VIN is missing or if the carCapturedTimestamp is missing in the response data.
            ValueError: If the vehicle has no charging object.

        Notes:
            - The method fetches data from the Skoda API using the vehicle's VIN.
            - It updates the vehicle's climatization state, estimated date to reach target temperature, target temperature, and outside temperature.
            - Logs additional keys found in the response data for debugging purposes.
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        if vehicle.position is None:
            raise ValueError('Vehicle has no charging object')
        url = f'https://mysmob.api.connect.skoda-auto.cz/api/v2/air-conditioning/{vin}'
        data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        if data is not None:
            if vehicle.climatization is not None and vehicle.climatization.commands is not None \
                    and not vehicle.climatization.commands.contains_command('start-stop'):
                start_stop_command = ClimatizationStartStopCommand(parent=vehicle.climatization.commands)
                start_stop_command._add_on_set_hook(self.__on_air_conditioning_start_stop)  # pylint: disable=protected-access
                start_stop_command.enabled = True
                vehicle.climatization.commands.add_command(start_stop_command)

            if 'carCapturedTimestamp' in data and data['carCapturedTimestamp'] is not None:
                captured_at: datetime = robust_time_parse(data['carCapturedTimestamp'])
            else:
                raise APIError('Could not fetch air conditioning, carCapturedTimestamp missing')
            if 'state' in data and data['state'] is not None:
                if data['state'] in [item.name for item in Climatization.ClimatizationState]:
                    climatization_state: Climatization.ClimatizationState = Climatization.ClimatizationState[data['state']]
                else:
                    LOG_API.info('Unknown climatization state %s not in %s', data['state'], str(Climatization.ClimatizationState))
                    climatization_state = Climatization.ClimatizationState.UNKNOWN
                vehicle.climatization.state._set_value(value=climatization_state, measured=captured_at)  # pylint: disable=protected-access
            else:
                vehicle.climatization.state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
            if 'estimatedDateTimeToReachTargetTemperature' in data and data['estimatedDateTimeToReachTargetTemperature'] is not None:
                estimated_reach: datetime = robust_time_parse(data['estimatedDateTimeToReachTargetTemperature'])
                if estimated_reach is not None:
                    vehicle.climatization.estimated_date_reached._set_value(value=estimated_reach, measured=captured_at)  # pylint: disable=protected-access
                else:
                    vehicle.climatization.estimated_date_reached._set_value(value=None, measured=captured_at)  # pylint: disable=protected-access
            else:
                vehicle.climatization.estimated_date_reached._set_value(value=None, measured=captured_at)  # pylint: disable=protected-access
            if 'targetTemperature' in data and data['targetTemperature'] is not None:
                # pylint: disable-next=protected-access
                vehicle.climatization.settings.target_temperature._add_on_set_hook(self.__on_air_conditioning_target_temperature_change)
                vehicle.climatization.settings.target_temperature._is_changeable = True  # pylint: disable=protected-access
                unit: Temperature = Temperature.UNKNOWN
                if 'unitInCar' in data['targetTemperature'] and data['targetTemperature']['unitInCar'] is not None:
                    if data['targetTemperature']['unitInCar'] == 'CELSIUS':
                        unit = Temperature.C
                    elif data['targetTemperature']['unitInCar'] == 'FAHRENHEIT':
                        unit = Temperature.F
                    elif data['targetTemperature']['unitInCar'] == 'KELVIN':
                        unit = Temperature.K
                    else:
                        LOG_API.info('Unknown temperature unit for targetTemperature in air-conditioning %s', data['targetTemperature']['unitInCar'])
                if 'temperatureValue' in data['targetTemperature'] and data['targetTemperature']['temperatureValue'] is not None:
                    # pylint: disable-next=protected-access
                    vehicle.climatization.settings.target_temperature._set_value(value=data['targetTemperature']['temperatureValue'],
                                                                                 measured=captured_at,
                                                                                 unit=unit)
                else:
                    # pylint: disable-next=protected-access
                    vehicle.climatization.settings.target_temperature._set_value(value=None, measured=captured_at, unit=unit)
                log_extra_keys(LOG_API, 'targetTemperature', data['targetTemperature'],  {'unitInCar', 'temperatureValue'})
            else:
                # pylint: disable-next=protected-access
                vehicle.climatization.settings.target_temperature._set_value(value=None, measured=captured_at, unit=Temperature.UNKNOWN)
            if 'outsideTemperature' in data and data['outsideTemperature'] is not None:
                if 'carCapturedTimestamp' in data['outsideTemperature'] and data['outsideTemperature']['carCapturedTimestamp'] is not None:
                    outside_captured_at: datetime = robust_time_parse(data['outsideTemperature']['carCapturedTimestamp'])
                else:
                    outside_captured_at = captured_at
                if 'temperatureUnit' in data['outsideTemperature'] and data['outsideTemperature']['temperatureUnit'] is not None:
                    unit: Temperature = Temperature.UNKNOWN
                    if data['outsideTemperature']['temperatureUnit'] == 'CELSIUS':
                        unit = Temperature.C
                    elif data['outsideTemperature']['temperatureUnit'] == 'FAHRENHEIT':
                        unit = Temperature.F
                    elif data['outsideTemperature']['temperatureUnit'] == 'KELVIN':
                        unit = Temperature.K
                    else:
                        LOG_API.info('Unknown temperature unit for outsideTemperature in air-conditioning %s', data['outsideTemperature']['temperatureUnit'])
                    if 'temperatureValue' in data['outsideTemperature'] and data['outsideTemperature']['temperatureValue'] is not None:
                        # pylint: disable-next=protected-access
                        vehicle.outside_temperature._set_value(value=data['outsideTemperature']['temperatureValue'],
                                                               measured=outside_captured_at,
                                                               unit=unit)
                    else:
                        # pylint: disable-next=protected-access
                        vehicle.outside_temperature._set_value(value=None, measured=outside_captured_at, unit=Temperature.UNKNOWN)
                else:
                    # pylint: disable-next=protected-access
                    vehicle.outside_temperature._set_value(value=None, measured=outside_captured_at, unit=Temperature.UNKNOWN)
                log_extra_keys(LOG_API, 'targetTemperature', data['outsideTemperature'],  {'carCapturedTimestamp', 'temperatureUnit', 'temperatureValue'})
            else:
                vehicle.outside_temperature._set_value(value=None, measured=None, unit=Temperature.UNKNOWN)  # pylint: disable=protected-access
            if 'airConditioningAtUnlock' in data and data['airConditioningAtUnlock'] is not None:
                if vehicle.climatization is not None and vehicle.climatization.settings is not None:
                    # pylint: disable-next=protected-access
                    vehicle.climatization.settings.climatization_at_unlock._add_on_set_hook(self.__on_air_conditioning_at_unlock_change)
                    vehicle.climatization.settings.climatization_at_unlock._is_changeable = True  # pylint: disable=protected-access
                    if data['airConditioningAtUnlock'] is True:
                        # pylint: disable-next=protected-access
                        vehicle.climatization.settings.climatization_at_unlock._set_value(True, measured=captured_at)
                    elif data['airConditioningAtUnlock'] is False:
                        # pylint: disable-next=protected-access
                        vehicle.climatization.settings.climatization_at_unlock._set_value(False, measured=captured_at)
                    else:
                        # pylint: disable-next=protected-access
                        vehicle.climatization.settings.climatization_at_unlock._set_value(None, measured=captured_at)
            else:
                if vehicle.climatization is not None and vehicle.climatization.settings is not None:
                    # pylint: disable-next=protected-access
                    vehicle.climatization.settings.climatization_at_unlock._set_value(None, measured=captured_at)
            if 'steeringWheelPosition' in data and data['steeringWheelPosition'] is not None:
                if vehicle.specification is not None:
                    if data['steeringWheelPosition'] in [item.name for item in GenericVehicle.VehicleSpecification.SteeringPosition]:
                        steering_wheel_position: GenericVehicle.VehicleSpecification.SteeringPosition = \
                            GenericVehicle.VehicleSpecification.SteeringPosition[data['steeringWheelPosition']]
                    else:
                        LOG_API.info('Unknown steering wheel position %s not in %s', data['steeringWheelPosition'],
                                     str(GenericVehicle.VehicleSpecification.SteeringPosition))
                        steering_wheel_position = GenericVehicle.VehicleSpecification.SteeringPosition.UNKNOWN
                    # pylint: disable-next=protected-access
                    vehicle.specification.steering_wheel_position._set_value(value=steering_wheel_position, measured=captured_at)
            else:
                if vehicle.specification is not None:
                    # pylint: disable-next=protected-access
                    vehicle.specification.steering_wheel_position._set_value(None, measured=captured_at)
            if 'windowHeatingEnabled' in data and data['windowHeatingEnabled'] is not None:
                if vehicle.climatization is not None and vehicle.climatization.settings is not None:
                    # pylint: disable-next=protected-access
                    vehicle.climatization.settings.window_heating._add_on_set_hook(self.__on_air_conditioning_window_heating_change)
                    vehicle.climatization.settings.window_heating._is_changeable = True  # pylint: disable=protected-access
                    if data['windowHeatingEnabled'] is True:
                        # pylint: disable-next=protected-access
                        vehicle.climatization.settings.window_heating._set_value(True, measured=captured_at)
                    elif data['windowHeatingEnabled'] is False:
                        # pylint: disable-next=protected-access
                        vehicle.climatization.settings.window_heating._set_value(False, measured=captured_at)
                    else:
                        # pylint: disable-next=protected-access
                        vehicle.climatization.settings.window_heating._set_value(None, measured=captured_at)
            else:
                if vehicle.climatization is not None and vehicle.climatization.settings is not None:
                    # pylint: disable-next=protected-access
                    vehicle.climatization.settings.window_heating._set_value(None, measured=captured_at)
            if 'seatHeatingActivated' in data and data['seatHeatingActivated'] is not None:
                if vehicle.climatization is not None and vehicle.climatization.settings is not None:
                    if data['seatHeatingActivated'] is True:
                        # pylint: disable-next=protected-access
                        vehicle.climatization.settings.seat_heating._set_value(True, measured=captured_at)
                    elif data['seatHeatingActivated'] is False:
                        # pylint: disable-next=protected-access
                        vehicle.climatization.settings.seat_heating._set_value(False, measured=captured_at)
                    else:
                        # pylint: disable-next=protected-access
                        vehicle.climatization.settings.seat_heating._set_value(None, measured=captured_at)
            else:
                if vehicle.climatization is not None and vehicle.climatization.settings is not None:
                    # pylint: disable-next=protected-access
                    vehicle.climatization.settings.seat_heating._set_value(None, measured=captured_at)
            if isinstance(vehicle, SkodaElectricVehicle):
                if 'chargerConnectionState' in data and data['chargerConnectionState'] is not None \
                        and vehicle.charging is not None and vehicle.charging.connector is not None:
                    if data['chargerConnectionState'] in [item.name for item in ChargingConnector.ChargingConnectorConnectionState]:
                        charging_connector_state: ChargingConnector.ChargingConnectorConnectionState = \
                            ChargingConnector.ChargingConnectorConnectionState[data['chargerConnectionState']]
                        # pylint: disable-next=protected-access
                        vehicle.charging.connector.connection_state._set_value(value=charging_connector_state, measured=captured_at)
                    else:
                        LOG_API.info('Unkown connector state %s not in %s', data['chargerConnectionState'],
                                     str(ChargingConnector.ChargingConnectorConnectionState))
                        # pylint: disable-next=protected-access
                        vehicle.charging.connector.connection_state._set_value(value=SkodaCharging.SkodaChargingState.UNKNOWN, measured=captured_at)
                else:
                    # pylint: disable-next=protected-access
                    vehicle.charging.connector.connection_state._set_value(value=None, measured=captured_at)
                if 'chargerLockState' in data and data['chargerLockState'] is not None \
                        and vehicle.charging is not None and vehicle.charging.connector is not None:
                    if data['chargerLockState'] in [item.name for item in ChargingConnector.ChargingConnectorLockState]:
                        charging_connector_lockstate: ChargingConnector.ChargingConnectorLockState = \
                            ChargingConnector.ChargingConnectorLockState[data['chargerLockState']]
                        # pylint: disable-next=protected-access
                        vehicle.charging.connector.lock_state._set_value(value=charging_connector_lockstate, measured=captured_at)
                    else:
                        LOG_API.info('Unkown connector lock state %s not in %s', data['chargerLockState'],
                                     str(ChargingConnector.ChargingConnectorLockState))
                        # pylint: disable-next=protected-access
                        vehicle.charging.connector.lock_state._set_value(value=SkodaCharging.SkodaChargingState.UNKNOWN, measured=captured_at)
                else:
                    # pylint: disable-next=protected-access
                    vehicle.charging.connector.lock_state._set_value(value=None, measured=captured_at)
            if 'windowHeatingState' in data and data['windowHeatingState'] is not None:
                pass
            if 'errors' in data and data['errors'] is not None:
                found_errors: Set[str] = set()
                if not isinstance(vehicle.climatization, SkodaClimatization):
                    vehicle.climatization = SkodaClimatization(origin=vehicle.climatization)
                for error_dict in data['errors']:
                    if 'type' in error_dict and error_dict['type'] is not None:
                        if error_dict['type'] not in vehicle.climatization.errors:
                            error: Error = Error(object_id=error_dict['type'])
                        else:
                            error = vehicle.climatization.errors[error_dict['type']]
                        if error_dict['type'] in [item.name for item in Error.ClimatizationError]:
                            error_type: Error.ClimatizationError = Error.ClimatizationError[error_dict['type']]
                        else:
                            LOG_API.info('Unknown climatization error type %s not in %s', error_dict['type'], str(Error.ClimatizationError))
                            error_type = Error.ClimatizationError.UNKNOWN
                        error.type._set_value(error_type, measured=captured_at)  # pylint: disable=protected-access
                        if 'description' in error_dict and error_dict['description'] is not None:
                            error.description._set_value(error_dict['description'], measured=captured_at)  # pylint: disable=protected-access
                    log_extra_keys(LOG_API, 'errors', error_dict,  {'type', 'description'})
                if vehicle.climatization is not None and vehicle.climatization.errors is not None and len(vehicle.climatization.errors) > 0:
                    for error_id in vehicle.climatization.errors.keys()-found_errors:
                        vehicle.climatization.errors.pop(error_id)
            else:
                if isinstance(vehicle.climatization, SkodaClimatization):
                    vehicle.climatization.errors.clear()
            log_extra_keys(LOG_API, 'air-condition', data,  {'carCapturedTimestamp', 'state', 'estimatedDateTimeToReachTargetTemperature',
                                                             'targetTemperature', 'outsideTemperature', 'chargerConnectionState',
                                                             'chargerLockState', 'airConditioningAtUnlock', 'steeringWheelPosition',
                                                             'windowHeatingEnabled', 'seatHeatingActivated', 'errors'})
        return vehicle

    def fetch_vehicle_details(self, vehicle: SkodaVehicle, no_cache: bool = False) -> SkodaVehicle:
        """
        Fetches the details of a vehicle from the Skoda API.

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
        vehicle_data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
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
        return vehicle

    def fetch_driving_range(self, vehicle: SkodaVehicle, no_cache: bool = False) -> SkodaVehicle:
        """
        Fetches the driving range data for a given Skoda vehicle and updates the vehicle object accordingly.

        Args:
            vehicle (SkodaVehicle): The Skoda vehicle object for which to fetch the driving range data.

        Returns:
            SkodaVehicle: The updated Skoda vehicle object with the fetched driving range data.

        Raises:
            APIError: If the vehicle's VIN is missing.

        Notes:
            - The method fetches data from the Skoda API using the vehicle's VIN.
            - It updates the vehicle's type if the fetched data indicates a different type (e.g., electric, combustion, hybrid).
            - It updates the vehicle's total range and individual drive ranges (primary and secondary) based on the fetched data.
            - It logs warnings for unknown car types and engine types.
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        url = f'https://mysmob.api.connect.skoda-auto.cz/api/v2/vehicle-status/{vin}/driving-range'
        range_data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
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
        return vehicle

    def fetch_vehicle_status(self, vehicle: SkodaVehicle, no_cache: bool = False) -> SkodaVehicle:
        """
        Fetches the status of a vehicle from other Skoda API.

        Args:
            vehicle (GenericVehicle): The vehicle object containing the VIN.

        Returns:
            None
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        url = f'https://mysmob.api.connect.skoda-auto.cz/api/v2/vehicle-status/{vin}'
        vehicle_status_data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        if vehicle_status_data:
            if 'carCapturedTimestamp' in vehicle_status_data and vehicle_status_data['carCapturedTimestamp'] is not None:
                captured_at: Optional[datetime] = robust_time_parse(vehicle_status_data['carCapturedTimestamp'])
            else:
                captured_at: Optional[datetime] = None
            if 'overall' in vehicle_status_data and vehicle_status_data['overall'] is not None:
                if 'doorsLocked' in vehicle_status_data['overall'] and vehicle_status_data['overall']['doorsLocked'] is not None \
                        and vehicle.doors is not None:
                    if vehicle_status_data['overall']['doorsLocked'] == 'YES':
                        vehicle.doors.lock_state._set_value(Doors.LockState.LOCKED, measured=captured_at)  # pylint: disable=protected-access
                        vehicle.doors.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['overall']['doorsLocked'] == 'OPENED':
                        vehicle.doors.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                        vehicle.doors.open_state._set_value(Doors.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['overall']['doorsLocked'] == 'UNLOCKED':
                        vehicle.doors.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                        vehicle.doors.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['overall']['doorsLocked'] == 'TRUNK_OPENED':
                        vehicle.doors.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                        vehicle.doors.open_state._set_value(Doors.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['overall']['doorsLocked'] == 'UNKNOWN':
                        vehicle.doors.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                        vehicle.doors.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        LOG_API.info('Unknown doorsLocked state %s', vehicle_status_data['overall']['doorsLocked'])
                        vehicle.doors.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                        vehicle.doors.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                if 'locked' in vehicle_status_data['overall'] and vehicle_status_data['overall']['locked'] is not None:
                    if vehicle_status_data['overall']['locked'] == 'YES':
                        vehicle.doors.lock_state._set_value(Doors.LockState.LOCKED, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['overall']['locked'] == 'NO':
                        vehicle.doors.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['overall']['locked'] == 'UNKNOWN':
                        vehicle.doors.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        LOG_API.info('Unknown locked state %s', vehicle_status_data['overall']['locked'])
                        vehicle.doors.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                if 'doors' in vehicle_status_data['overall'] and vehicle_status_data['overall']['doors'] is not None:
                    if vehicle_status_data['overall']['doors'] == 'CLOSED':
                        vehicle.doors.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['overall']['doors'] == 'OPEN':
                        vehicle.doors.open_state._set_value(Doors.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['overall']['doors'] == 'UNSUPPORTED':
                        vehicle.doors.open_state._set_value(Doors.OpenState.UNSUPPORTED, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['overall']['doors'] == 'UNKNOWN':
                        vehicle.doors.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        LOG_API.info('Unknown doors state %s', vehicle_status_data['overall']['doors'])
                        vehicle.doors.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                if 'windows' in vehicle_status_data['overall'] and vehicle_status_data['overall']['windows'] is not None:
                    if vehicle_status_data['overall']['windows'] == 'CLOSED':
                        vehicle.windows.open_state._set_value(Windows.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['overall']['windows'] == 'OPEN':
                        vehicle.windows.open_state._set_value(Windows.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['overall']['windows'] == 'UNKNOWN':
                        vehicle.windows.open_state._set_value(Windows.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['overall']['windows'] == 'UNSUPPORTED':
                        vehicle.windows.open_state._set_value(Windows.OpenState.UNSUPPORTED, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        LOG_API.info('Unknown windows state %s', vehicle_status_data['overall']['windows'])
                        vehicle.windows.open_state._set_value(Windows.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                if 'lights' in vehicle_status_data['overall'] and vehicle_status_data['overall']['lights'] is not None:
                    if vehicle_status_data['overall']['lights'] == 'ON':
                        vehicle.lights.light_state._set_value(Lights.LightState.ON, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['overall']['lights'] == 'OFF':
                        vehicle.lights.light_state._set_value(Lights.LightState.OFF, measured=captured_at)  # pylint: disable=protected-access
                    elif vehicle_status_data['overall']['lights'] == 'UNKNOWN':
                        vehicle.lights.light_state._set_value(Lights.LightState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        LOG_API.info('Unknown lights state %s', vehicle_status_data['overall']['lights'])
                        vehicle.lights.light_state._set_value(Lights.LightState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                log_extra_keys(LOG_API, 'status overall', vehicle_status_data['overall'], {'doorsLocked',
                                                                                           'locked',
                                                                                           'doors',
                                                                                           'windows',
                                                                                           'lights'})
            log_extra_keys(LOG_API, f'/api/v2/vehicle-status/{vin}', vehicle_status_data, {'overall', 'carCapturedTimestamp'})
        return vehicle

        # def fetch_vehicle_status_second_api(self, vehicle: SkodaVehicle, no_cache: bool = False) -> SkodaVehicle:
        #     """
        #     Fetches the status of a vehicle from other Skoda API.
        #
        #     Args:
        #         vehicle (GenericVehicle): The vehicle object containing the VIN.
        #
        #     Returns:
        #         None
        #     """
        #     vin = vehicle.vin.value
        #     if vin is None:
        #         raise APIError('VIN is missing')
        #     url = f'https://api.connect.skoda-auto.cz/api/v2/vehicle-status/{vin}'
        #     vehicle_status_data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        #     if vehicle_status_data:
        #         if 'remote' in vehicle_status_data and vehicle_status_data['remote'] is not None:
        #             vehicle_status_data = vehicle_status_data['remote']
        #     if vehicle_status_data:
        #         if 'capturedAt' in vehicle_status_data and vehicle_status_data['capturedAt'] is not None:
        #             captured_at: datetime = robust_time_parse(vehicle_status_data['capturedAt'])
        #         else:
        #             raise APIError('Could not fetch vehicle status, capturedAt missing')
        #         if 'mileageInKm' in vehicle_status_data and vehicle_status_data['mileageInKm'] is not None:
        #             # pylint: disable-next=protected-access
        #             vehicle.odometer._set_value(value=vehicle_status_data['mileageInKm'], measured=captured_at, unit=Length.KM)
        #         else:
        #             vehicle.odometer._set_value(value=None, measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access
        #         if 'status' in vehicle_status_data and vehicle_status_data['status'] is not None:
        #             if 'open' in vehicle_status_data['status'] and vehicle_status_data['status']['open'] is not None:
        #                 if vehicle_status_data['status']['open'] == 'YES':
        #                     vehicle.doors.open_state._set_value(Doors.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
        #                 elif vehicle_status_data['status']['open'] == 'NO':
        #                     vehicle.doors.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
        #                 else:
        #                     vehicle.doors.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
        #                     LOG_API.info('Unknown door open state: %s', vehicle_status_data['status']['open'])
        #             else:
        #                 vehicle.doors.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
        #             if 'locked' in vehicle_status_data['status'] and vehicle_status_data['status']['locked'] is not None:
        #                 if vehicle_status_data['status']['locked'] == 'YES':
        #                     vehicle.doors.lock_state._set_value(Doors.LockState.LOCKED, measured=captured_at)  # pylint: disable=protected-access
        #                 elif vehicle_status_data['status']['locked'] == 'NO':
        #                     vehicle.doors.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
        #                 else:
        #                     vehicle.doors.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
        #                     LOG_API.info('Unknown door lock state: %s', vehicle_status_data['status']['locked'])
        #             else:
        #                 vehicle.doors.lock_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
        #         else:
        #             vehicle.doors.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
        #             vehicle.doors.lock_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
        #         if 'doors' in vehicle_status_data and vehicle_status_data['doors'] is not None:
        #             seen_door_ids: set[str] = set()
        #             for door_status in vehicle_status_data['doors']:
        #                 if 'name' in door_status and door_status['name'] is not None:
        #                     door_id = door_status['name']
        #                     seen_door_ids.add(door_id)
        #                     if door_id in vehicle.doors.doors:
        #                         door: Doors.Door = vehicle.doors.doors[door_id]
        #                     else:
        #                         door = Doors.Door(door_id=door_id, doors=vehicle.doors)
        #                         vehicle.doors.doors[door_id] = door
        #                     if 'status' in door_status and door_status['status'] is not None:
        #                         if door_status['status'] == 'OPEN':
        #                             door.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
        #                             door.open_state._set_value(Doors.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
        #                         elif door_status['status'] == 'CLOSED':
        #                             door.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
        #                             door.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
        #                         elif door_status['status'] == 'LOCKED':
        #                             door.lock_state._set_value(Doors.LockState.LOCKED, measured=captured_at)  # pylint: disable=protected-access
        #                             door.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
        #                         elif door_status['status'] == 'UNSUPPORTED':
        #                             door.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
        #                             door.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
        #                         else:
        #                             LOG_API.info('Unknown door status %s', door_status['status'])
        #                             door.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
        #                             door.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
        #                     else:
        #                         door.lock_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
        #                         door.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
        #                 else:
        #                     raise APIError('Could not parse door, name missing')
        #                 log_extra_keys(LOG_API, 'doors', door_status, {'name', 'status'})
        #             for door_to_remove in set(vehicle.doors.doors) - seen_door_ids:
        #                 vehicle.doors.doors[door_to_remove].enabled = False
        #                 vehicle.doors.doors.pop(door_to_remove)
        #             log_extra_keys(LOG_API, 'status', vehicle_status_data['status'],  {'open', 'locked'})
        #         else:
        #             vehicle.doors.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
        #             vehicle.doors.doors = {}
        #         if 'windows' in vehicle_status_data and vehicle_status_data['windows'] is not None:
        #             seen_window_ids: set[str] = set()
        #             all_windows_closed: bool = True
        #             for window_status in vehicle_status_data['windows']:
        #                 if 'name' in window_status and window_status['name'] is not None:
        #                     window_id = window_status['name']
        #                     seen_window_ids.add(window_id)
        #                     if window_id in vehicle.windows.windows:
        #                         window: Windows.Window = vehicle.windows.windows[window_id]
        #                     else:
        #                         window = Windows.Window(window_id=window_id, windows=vehicle.windows)
        #                         vehicle.windows.windows[window_id] = window
        #                     if 'status' in window_status and window_status['status'] is not None:
        #                         if window_status['status'] == 'OPEN':
        #                             all_windows_closed = False
        #                             window.open_state._set_value(Windows.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
        #                         elif window_status['status'] == 'CLOSED':
        #                             window.open_state._set_value(Windows.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
        #                         elif window_status['status'] == 'UNSUPPORTED':
        #                             window.open_state._set_value(Windows.OpenState.UNSUPPORTED, measured=captured_at)  # pylint: disable=protected-access
        #                         elif window_status['status'] == 'INVALID':
        #                             window.open_state._set_value(Windows.OpenState.INVALID, measured=captured_at)  # pylint: disable=protected-access
        #                         else:
        #                             LOG_API.info('Unknown window status %s', window_status['status'])
        #                             window.open_state._set_value(Windows.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
        #                     else:
        #                         window.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
        #                 else:
        #                     raise APIError('Could not parse window, name missing')
        #                 log_extra_keys(LOG_API, 'doors', window_status, {'name', 'status'})
        #             for window_to_remove in set(vehicle.windows.windows) - seen_window_ids:
        #                 vehicle.windows.windows[window_to_remove].enabled = False
        #                 vehicle.windows.windows.pop(window_to_remove)
        #             if all_windows_closed:
        #                 vehicle.windows.open_state._set_value(Windows.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
        #             else:
        #                 vehicle.windows.open_state._set_value(Windows.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
        #         else:
        #             vehicle.windows.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
        #             vehicle.windows.windows = {}
        #         if 'lights' in vehicle_status_data and vehicle_status_data['lights'] is not None:
        #             seen_light_ids: set[str] = set()
        #             if 'overallStatus' in vehicle_status_data['lights'] and vehicle_status_data['lights']['overallStatus'] is not None:
        #                 if vehicle_status_data['lights']['overallStatus'] == 'ON':
        #                     vehicle.lights.light_state._set_value(Lights.LightState.ON, measured=captured_at)  # pylint: disable=protected-access
        #                 elif vehicle_status_data['lights']['overallStatus'] == 'OFF':
        #                     vehicle.lights.light_state._set_value(Lights.LightState.OFF, measured=captured_at)  # pylint: disable=protected-access
        #                 elif vehicle_status_data['lights']['overallStatus'] == 'INVALID':
        #                     vehicle.lights.light_state._set_value(Lights.LightState.INVALID, measured=captured_at)  # pylint: disable=protected-access
        #                 else:
        #                     LOG_API.info('Unknown light status %s', vehicle_status_data['lights']['overallStatus'])
        #                     vehicle.lights.light_state._set_value(Lights.LightState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
        #             else:
        #                 vehicle.lights.light_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
        #             if 'lightsStatus' in vehicle_status_data['lights'] and vehicle_status_data['lights']['lightsStatus'] is not None:
        #                 for light_status in vehicle_status_data['lights']['lightsStatus']:
        #                     if 'name' in light_status and light_status['name'] is not None:
        #                         light_id: str = light_status['name']
        #                         seen_light_ids.add(light_id)
        #                         if light_id in vehicle.lights.lights:
        #                             light: Lights.Light = vehicle.lights.lights[light_id]
        #                         else:
        #                             light = Lights.Light(light_id=light_id, lights=vehicle.lights)
        #                             vehicle.lights.lights[light_id] = light
        #                         if 'status' in light_status and light_status['status'] is not None:
        #                             if light_status['status'] == 'ON':
        #                                 light.light_state._set_value(Lights.LightState.ON, measured=captured_at)  # pylint: disable=protected-access
        #                             elif light_status['status'] == 'OFF':
        #                                 light.light_state._set_value(Lights.LightState.OFF, measured=captured_at)  # pylint: disable=protected-access
        #                             elif light_status['status'] == 'INVALID':
        #                                 light.light_state._set_value(Lights.LightState.INVALID, measured=captured_at)  # pylint: disable=protected-access
        #                             else:
        #                                 LOG_API.info('Unknown light status %s', light_status['status'])
        #                                 light.light_state._set_value(Lights.LightState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
        #                         else:
        #                             light.light_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
        #                     else:
        #                         raise APIError('Could not parse light, name missing')
        #                     log_extra_keys(LOG_API, 'lights', light_status, {'name', 'status'})
        #                 for light_to_remove in set(vehicle.lights.lights) - seen_light_ids:
        #                     vehicle.lights.lights[light_to_remove].enabled = False
        #                     vehicle.lights.lights.pop(light_to_remove)
        #             else:
        #                 vehicle.lights.lights = {}
        #             log_extra_keys(LOG_API, 'lights', vehicle_status_data['lights'], {'overallStatus', 'lightsStatus'})
        #         log_extra_keys(LOG_API, 'vehicles', vehicle_status_data,  {'capturedAt', 'mileageInKm', 'status', 'doors', 'windows', 'lights'})
        #     return vehicle

    def _record_elapsed(self, elapsed: timedelta) -> None:
        """
        Records the elapsed time.

        Args:
            elapsed (timedelta): The elapsed time to record.
        """
        self._elapsed.append(elapsed)

    def _fetch_data(self, url, session, no_cache=False, allow_empty=False, allow_http_error=False,
                    allowed_errors=None) -> Optional[Dict[str, Any]]:  # noqa: C901
        data: Optional[Dict[str, Any]] = None
        cache_date: Optional[datetime] = None
        if not no_cache and (self.max_age is not None and session.cache is not None and url in session.cache):
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

    def __on_air_conditioning_target_temperature_change(self, temperature_attribute: TemperatureAttribute, target_temperature: float) -> float:
        """
        Callback for the climatization target temperature change.

        Args:
            temperature_attribute (TemperatureAttribute): The temperature attribute that changed.
            target_temperature (float): The new target temperature.
        """
        if temperature_attribute.parent is None or temperature_attribute.parent.parent is None \
                or temperature_attribute.parent.parent.parent is None or not isinstance(temperature_attribute.parent.parent.parent, SkodaVehicle):
            raise SetterError('Object hierarchy is not as expected')
        vehicle: SkodaVehicle = temperature_attribute.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise SetterError('VIN in object hierarchy missing')
        setting_dict = {}
        # Round target temperature to nearest 0.5
        setting_dict['temperatureValue'] = round(target_temperature * 2) / 2
        if temperature_attribute.unit == Temperature.C:
            setting_dict['unitInCar'] = 'CELSIUS'
        elif temperature_attribute.unit == Temperature.F:
            setting_dict['unitInCar'] = 'FAHRENHEIT'
        elif temperature_attribute.unit == Temperature.K:
            setting_dict['unitInCar'] = 'KELVIN'
        else:
            raise SetterError(f'Unknown temperature unit {temperature_attribute.unit}')

        url = f'https://mysmob.api.connect.skoda-auto.cz/api/v2/air-conditioning/{vin}/settings/target-temperature'
        settings_response: requests.Response = self.session.post(url, data=json.dumps(setting_dict), allow_redirects=True)
        if settings_response.status_code != requests.codes['accepted']:
            LOG.error('Could not set target temperature (%s)', settings_response.status_code)
            raise SetterError(f'Could not set value ({settings_response.status_code})')
        return target_temperature

    def __on_air_conditioning_at_unlock_change(self, at_unlock_attribute: BooleanAttribute, at_unlock_value: bool) -> bool:
        if at_unlock_attribute.parent is None or at_unlock_attribute.parent.parent is None \
                or at_unlock_attribute.parent.parent.parent is None or not isinstance(at_unlock_attribute.parent.parent.parent, SkodaVehicle):
            raise SetterError('Object hierarchy is not as expected')
        vehicle: SkodaVehicle = at_unlock_attribute.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise SetterError('VIN in object hierarchy missing')
        setting_dict = {}
        # Round target temperature to nearest 0.5
        setting_dict['airConditioningAtUnlockEnabled'] = at_unlock_value

        url = f'https://mysmob.api.connect.skoda-auto.cz/api/v2/air-conditioning/{vin}/settings/ac-at-unlock'
        settings_response: requests.Response = self.session.post(url, data=json.dumps(setting_dict), allow_redirects=True)
        if settings_response.status_code != requests.codes['accepted']:
            LOG.error('Could not set air conditioning at unlock (%s)', settings_response.status_code)
            raise SetterError(f'Could not set value ({settings_response.status_code})')
        return at_unlock_value

    def __on_air_conditioning_window_heating_change(self, window_heating_attribute: BooleanAttribute, window_heating_value: bool) -> bool:
        if window_heating_attribute.parent is None or window_heating_attribute.parent.parent is None \
                or window_heating_attribute.parent.parent.parent is None or not isinstance(window_heating_attribute.parent.parent.parent, SkodaVehicle):
            raise SetterError('Object hierarchy is not as expected')
        vehicle: SkodaVehicle = window_heating_attribute.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise SetterError('VIN in object hierarchy missing')
        setting_dict = {}
        # Round target temperature to nearest 0.5
        setting_dict['windowHeatingEnabled'] = window_heating_value

        url = f'https://mysmob.api.connect.skoda-auto.cz/api/v2/air-conditioning/{vin}/settings/ac-at-unlock'
        settings_response: requests.Response = self.session.post(url, data=json.dumps(setting_dict), allow_redirects=True)
        if settings_response.status_code != requests.codes['accepted']:
            LOG.error('Could not set air conditioning window heating (%s)', settings_response.status_code)
            raise SetterError(f'Could not set value ({settings_response.status_code})')
        return window_heating_value

    def __on_air_conditioning_start_stop(self, start_stop_command: ClimatizationStartStopCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if start_stop_command.parent is None or start_stop_command.parent.parent is None \
                or start_stop_command.parent.parent.parent is None or not isinstance(start_stop_command.parent.parent.parent, SkodaVehicle):
            raise SetterError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise SetterError('Command arguments are not a dictionary')
        vehicle: SkodaVehicle = start_stop_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise SetterError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise SetterError('Command argument missing')
        command_dict = {}
        if command_arguments['command'] == ClimatizationStartStopCommand.Command.START:
            command_dict['heaterSource'] = 'ELECTRIC'
            command_dict['targetTemperature'] = {}
            if 'target_temperature' in command_arguments:
                # Round target temperature to nearest 0.5
                command_dict['targetTemperature']['temperatureValue'] = round(command_arguments['target_temperature'] * 2) / 2
                if 'target_temperature_unit' in command_arguments:
                    if not isinstance(command_arguments['target_temperature_unit'], Temperature):
                        raise SetterError('Temperature unit is not of type Temperature')
                    if command_arguments['target_temperature_unit'] == Temperature.C:
                        command_dict['targetTemperature']['unitInCar'] = 'CELSIUS'
                    elif command_arguments['target_temperature_unit'] == Temperature.F:
                        command_dict['targetTemperature']['unitInCar'] = 'FAHRENHEIT'
                    elif command_arguments['target_temperature_unit'] == Temperature.K:
                        command_dict['targetTemperature']['unitInCar'] = 'KELVIN'
                    else:
                        raise SetterError(f'Unknown temperature unit {command_arguments['target_temperature_unit']}')
                else:
                    command_dict['targetTemperature']['unitInCar'] = 'CELSIUS'
            elif start_stop_command.parent is not None and (climatization := start_stop_command.parent.parent) is not None \
                    and isinstance(climatization, Climatization) and climatization.settings is not None \
                    and climatization.settings.target_temperature is not None and climatization.settings.target_temperature.enabled \
                    and climatization.settings.target_temperature.value is not None:  # pylint: disable=too-many-boolean-expressions
                # Round target temperature to nearest 0.5
                command_dict['targetTemperature']['temperatureValue'] = round(climatization.settings.target_temperature.value * 2) / 2
                if climatization.settings.target_temperature.unit == Temperature.C:
                    command_dict['targetTemperature']['unitInCar'] = 'CELSIUS'
                elif climatization.settings.target_temperature.unit == Temperature.F:
                    command_dict['targetTemperature']['unitInCar'] = 'FAHRENHEIT'
                elif climatization.settings.target_temperature.unit == Temperature.K:
                    command_dict['targetTemperature']['unitInCar'] = 'KELVIN'
                else:
                    raise SetterError(f'Unknown temperature unit {climatization.settings.target_temperature.unit}')
            else:
                command_dict['targetTemperature']['temperatureValue'] = 25.0
                command_dict['targetTemperature']['unitInCar'] = 'CELSIUS'
            url = f'https://mysmob.api.connect.skoda-auto.cz/api/v2/air-conditioning/{vin}/start'
            settings_response: requests.Response = self.session.post(url, data=json.dumps(command_dict), allow_redirects=True)
        elif command_arguments['command'] == ClimatizationStartStopCommand.Command.STOP:
            url = f'https://mysmob.api.connect.skoda-auto.cz/api/v2/air-conditioning/{vin}/stop'
            settings_response: requests.Response = self.session.post(url, allow_redirects=True)
        else:
            raise SetterError(f'Unknown command {command_arguments["command"]}')

        if settings_response.status_code != requests.codes['accepted']:
            LOG.error('Could not start/stop air conditioning (%s: %s)', settings_response.status_code, settings_response.text)
            raise SetterError(f'Could not start/stop air conditioning ({settings_response.status_code}: {settings_response.text})')
        return command_arguments

    def __on_charging_start_stop(self, start_stop_command: ChargingStartStopCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if start_stop_command.parent is None or start_stop_command.parent.parent is None \
                or start_stop_command.parent.parent.parent is None or not isinstance(start_stop_command.parent.parent.parent, SkodaVehicle):
            raise SetterError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise SetterError('Command arguments are not a dictionary')
        vehicle: SkodaVehicle = start_stop_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise SetterError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise SetterError('Command argument missing')
        if command_arguments['command'] == ChargingStartStopCommand.Command.START:
            url = f'https://mysmob.api.connect.skoda-auto.cz/api/v1/charging/{vin}/start'
            settings_response: requests.Response = self.session.post(url, allow_redirects=True)
        elif command_arguments['command'] == ChargingStartStopCommand.Command.STOP:
            url = f'https://mysmob.api.connect.skoda-auto.cz/api/v1/charging/{vin}/stop'
            settings_response: requests.Response = self.session.post(url, allow_redirects=True)
        else:
            raise SetterError(f'Unknown command {command_arguments["command"]}')

        if settings_response.status_code != requests.codes['accepted']:
            LOG.error('Could not start/stop charging (%s: %s)', settings_response.status_code, settings_response.text)
            raise SetterError(f'Could not start/stop charging ({settings_response.status_code}: {settings_response.text})')
        return command_arguments
