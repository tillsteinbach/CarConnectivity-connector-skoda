"""Module implements the connector to interact with the Skoda API."""  # pylint: disable=too-many-lines
from __future__ import annotations
from typing import TYPE_CHECKING

import threading
import os
import traceback
import logging
import netrc
from datetime import datetime, timedelta, timezone
import json

import requests


from carconnectivity.garage import Garage
from carconnectivity.vehicle import GenericVehicle
from carconnectivity.errors import AuthenticationError, TooManyRequestsError, RetrievalError, APIError, APICompatibilityError, \
    TemporaryAuthenticationError, SetterError, CommandError
from carconnectivity.util import robust_time_parse, log_extra_keys, config_remove_credentials
from carconnectivity.units import Length, Speed, Power, Temperature
from carconnectivity.doors import Doors
from carconnectivity.windows import Windows
from carconnectivity.lights import Lights
from carconnectivity.drive import GenericDrive, ElectricDrive, CombustionDrive
from carconnectivity.attributes import BooleanAttribute, DurationAttribute, TemperatureAttribute, EnumAttribute, LevelAttribute, \
    CurrentAttribute
from carconnectivity.charging import Charging
from carconnectivity.position import Position
from carconnectivity.climatization import Climatization
from carconnectivity.charging_connector import ChargingConnector
from carconnectivity.commands import Commands
from carconnectivity.command_impl import ClimatizationStartStopCommand, ChargingStartStopCommand, HonkAndFlashCommand, LockUnlockCommand, WakeSleepCommand, \
    WindowHeatingStartStopCommand
from carconnectivity.enums import ConnectionState
from carconnectivity.window_heating import WindowHeatings

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
from carconnectivity_connectors.skoda.command_impl import SpinCommand

SUPPORT_IMAGES = False
try:
    from PIL import Image
    import base64
    import io
    SUPPORT_IMAGES = True
    from carconnectivity.attributes import ImageAttribute
except ImportError:
    pass

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
        BaseConnector.__init__(self, connector_id=connector_id, car_connectivity=car_connectivity, config=config, log=LOG, api_log=LOG_API)

        self._mqtt_client: SkodaMQTTClient = SkodaMQTTClient(skoda_connector=self)

        self._background_thread: Optional[threading.Thread] = None
        self._background_connect_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self.connection_state: EnumAttribute = EnumAttribute(name="connection_state", parent=self, value_type=ConnectionState,
                                                             value=ConnectionState.DISCONNECTED, tags={'connector_custom'})
        self.rest_connected: bool = False
        self.mqtt_connected: bool = False
        self.interval: DurationAttribute = DurationAttribute(name="interval", parent=self, tags={'connector_custom'})
        self.interval.minimum = timedelta(seconds=180)
        self.interval._is_changeable = True  # pylint: disable=protected-access

        self.commands: Commands = Commands(parent=self)

        self.user_id: Optional[str] = None

        LOG.info("Loading skoda connector with config %s", config_remove_credentials(config))

        if 'spin' in config and config['spin'] is not None:
            self.active_config['spin'] = config['spin']
        else:
            self.active_config['spin'] = None

        self.active_config['username'] = None
        self.active_config['password'] = None
        if 'username' in config and 'password' in config:
            self.active_config['username'] = config['username']
            self.active_config['password'] = config['password']
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
                self.active_config['username'], account, self.active_config['password'] = secret

                if self.active_config['spin'] is None and account is not None:
                    try:
                        self.active_config['spin'] = account
                    except ValueError as err:
                        LOG.error('Could not parse spin from netrc: %s', err)
            except netrc.NetrcParseError as err:
                LOG.error('Authentification using %s failed: %s', self.active_config['netrc'], err)
                raise AuthenticationError(f'Authentication using {self.active_config["netrc"]} failed: {err}') from err
            except TypeError as err:
                if 'username' not in config:
                    raise AuthenticationError(f'"skoda" entry was not found in {self.active_config["netrc"]} netrc-file.'
                                              ' Create it or provide username and password in config') from err
            except FileNotFoundError as err:
                raise AuthenticationError(f'{self.active_config["netrc"]} netrc-file was not found. Create it or provide username and password in config') \
                                          from err

        self.active_config['interval'] = 300
        if 'interval' in config:
            self.active_config['interval'] = config['interval']
            if self.active_config['interval'] < 180:
                raise ValueError('Intervall must be at least 180 seconds')
        self.active_config['max_age'] = self.active_config['interval'] - 1
        if 'max_age' in config:
            self.active_config['max_age'] = config['max_age']
        self.interval._set_value(timedelta(seconds=self.active_config['interval']))  # pylint: disable=protected-access

        if self.active_config['username'] is None or self.active_config['password'] is None:
            raise AuthenticationError('Username or password not provided')

        self._manager: SessionManager = SessionManager(tokenstore=car_connectivity.get_tokenstore(), cache=car_connectivity.get_cache())
        session: requests.Session = self._manager.get_session(Service.MY_SKODA, SessionUser(username=self.active_config['username'],
                                                                                            password=self.active_config['password']))
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
        self._background_thread.name = 'carconnectivity.connectors.skoda-background'
        self._background_thread.start()
        # Start background thread for MQTT connection
        self._background_connect_thread = threading.Thread(target=self._background_connect_loop, daemon=False)
        self._background_connect_thread.name = 'carconnectivity.connectors.skoda-background_connect'
        self._background_connect_thread.start()
        # Start MQTT thread
        self._mqtt_client.loop_start()
        self.healthy._set_value(value=True)  # pylint: disable=protected-access

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
        self.connection_state._set_value(value=ConnectionState.CONNECTING)  # pylint: disable=protected-access
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
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self.rest_connected = False
                self._stop_event.wait(900)
            except RetrievalError as err:
                LOG.error('Retrieval error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self.rest_connected = False
                self._stop_event.wait(interval)
            except APIError as err:
                LOG.error('API error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self.rest_connected = False
                self._stop_event.wait(interval)
            except APICompatibilityError as err:
                LOG.error('API compatability error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self.rest_connected = False
                self._stop_event.wait(interval)
            except TemporaryAuthenticationError as err:
                LOG.error('Temporary authentification error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self.rest_connected = False
                self._stop_event.wait(interval)
            except Exception as err:
                LOG.critical('Critical error during update: %s', traceback.format_exc())
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self.rest_connected = False
                self.healthy._set_value(value=False)  # pylint: disable=protected-access
                raise err
            else:
                self.rest_connected = True
                if self.mqtt_connected:
                    self.connection_state._set_value(value=ConnectionState.CONNECTED)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
        # When leaving the loop, set the connection state to disconnected
        self.connection_state._set_value(value=ConnectionState.DISCONNECTED)  # pylint: disable=protected-access
        self.rest_connected = False

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
        self.session.close()
        if self._background_thread is not None:
            self._background_thread.join()
        if self._background_connect_thread is not None:
            self._background_connect_thread.join()
        self.persist()
        return super().shutdown()

    def fetch_all(self) -> None:
        """
        Fetches all necessary data for the connector.

        This method calls the `fetch_vehicles` method to retrieve vehicle data.
        """
        # Add spin command
        if self.commands is not None and not self.commands.contains_command('spin'):
            spin_command = SpinCommand(parent=self.commands)
            spin_command._add_on_set_hook(self.__on_spin)  # pylint: disable=protected-access
            spin_command.enabled = True
            self.commands.add_command(spin_command)
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
                        if SUPPORT_IMAGES:
                            vehicle = self.fetch_vehicle_images(vehicle)
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
                    if vehicle_to_update.capabilities.has_capability('READINESS', check_status_ok=True):
                        vehicle_to_update = self.fetch_connection_status(vehicle_to_update)
                    if vehicle_to_update.capabilities.has_capability('PARKING_POSITION', check_status_ok=True):
                        vehicle_to_update = self.fetch_position(vehicle_to_update)
                    if vehicle_to_update.capabilities.has_capability('CHARGING', check_status_ok=True) and isinstance(vehicle_to_update, SkodaElectricVehicle):
                        vehicle_to_update = self.fetch_charging(vehicle_to_update)
                    if vehicle_to_update.capabilities.has_capability('AIR_CONDITIONING', check_status_ok=True):
                        vehicle_to_update = self.fetch_air_conditioning(vehicle_to_update)
                    if vehicle_to_update.capabilities.has_capability('VEHICLE_HEALTH_INSPECTION', check_status_ok=True):
                        vehicle_to_update = self.fetch_maintenance(vehicle_to_update)
                vehicle_to_update = self.decide_state(vehicle_to_update)
        self.car_connectivity.transaction_end()

    def decide_state(self, vehicle: SkodaVehicle) -> SkodaVehicle:
        """
        Decides the state of the vehicle based on the current data.

        Args:
            vehicle (SkodaVehicle): The Skoda vehicle object.

        Returns:
            SkodaVehicle: The Skoda vehicle object with the updated state.
        """
        if vehicle is not None:
            if vehicle.in_motion is not None and vehicle.in_motion.enabled and vehicle.in_motion.value:
                vehicle.state._set_value(GenericVehicle.State.IGNITION_ON)  # pylint: disable=protected-access
            elif vehicle.position is not None and vehicle.position.enabled and vehicle.position.position_type is not None \
                    and vehicle.position.position_type.enabled and vehicle.position.position_type.value == Position.PositionType.PARKING:
                vehicle.state._set_value(GenericVehicle.State.PARKED)  # pylint: disable=protected-access
            else:
                vehicle.state._set_value(None)  # pylint: disable=protected-access
        return vehicle

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
            if not vehicle.charging.commands.contains_command('start-stop'):
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
                    vehicle.charging.settings.target_level.minimum = 50.0
                    vehicle.charging.settings.target_level.maximum = 100.0
                    vehicle.charging.settings.target_level.precision = 10.0
                    vehicle.charging.settings.target_level._add_on_set_hook(self.__on_charging_target_level_change)  # pylint: disable=protected-access
                    vehicle.charging.settings.target_level._is_changeable = True  # pylint: disable=protected-access
                    # pylint: disable-next=protected-access
                    vehicle.charging.settings.target_level._set_value(value=data['settings']['targetStateOfChargeInPercent'], measured=captured_at)
                else:
                    vehicle.charging.settings.target_level._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                if 'maxChargeCurrentAc' in data['settings'] and data['settings']['maxChargeCurrentAc'] is not None \
                        and vehicle.charging is not None and vehicle.charging.settings is not None:
                    vehicle.charging.settings.maximum_current.minimum = 6.0
                    vehicle.charging.settings.maximum_current.maximum = 16.0
                    vehicle.charging.settings.maximum_current.precision = 1.0
                    vehicle.charging.settings.maximum_current._add_on_set_hook(self.__on_charging_maximum_current_change)  # pylint: disable=protected-access
                    vehicle.charging.settings.maximum_current._is_changeable = True  # pylint: disable=protected-access
                    if data['settings']['maxChargeCurrentAc'] == 'MAXIMUM':
                        vehicle.charging.settings.maximum_current._set_value(value=16, measured=captured_at)  # pylint: disable=protected-access
                    elif data['settings']['maxChargeCurrentAc'] == 'REDUCED':
                        vehicle.charging.settings.maximum_current._set_value(value=6, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        LOG_API.info('Unknown maxChargeCurrentAc %s not in %s', data['settings']['maxChargeCurrentAc'], ['MAXIMUM', 'REDUCED'])
                        vehicle.charging.settings.maximum_current._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                else:
                    vehicle.charging.settings.maximum_current._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                if 'autoUnlockPlugWhenCharged' in data['settings'] and data['settings']['autoUnlockPlugWhenCharged'] is not None:
                    vehicle.charging.settings.auto_unlock._add_on_set_hook(self.__on_charging_auto_unlock_change)  # pylint: disable=protected-access
                    vehicle.charging.settings.auto_unlock._is_changeable = True  # pylint: disable=protected-access
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
    
    def fetch_connection_status(self, vehicle: SkodaVehicle, no_cache: bool = False) -> SkodaVehicle:
        """
        Fetches the connection status of the given Skoda vehicle and updates its connection attributes.

        Args:
            vehicle (SkodaVehicle): The Skoda vehicle object containing the VIN and connection attributes.

        Returns:
            SkodaVehicle: The updated Skoda vehicle object with the fetched connection data.

        Raises:
            APIError: If the VIN is missing.
            ValueError: If the vehicle has no connection object.
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        url = f'https://mysmob.api.connect.skoda-auto.cz/api/v2/connection-status/{vin}/readiness'
        data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        #  {'unreachable': False, 'inMotion': False, 'batteryProtectionLimitOn': False}
        if data is not None:
            if 'unreachable' in data and data['unreachable'] is not None:
                if data['unreachable']:
                    vehicle.connection_state._set_value(vehicle.ConnectionState.OFFLINE)  # pylint: disable=protected-access
                else:
                    vehicle.connection_state._set_value(vehicle.ConnectionState.REACHABLE)  # pylint: disable=protected-access
            else:
                vehicle.connection_state._set_value(None)  # pylint: disable=protected-access
            if 'inMotion' in data and data['inMotion'] is not None:
                vehicle.in_motion._set_value(data['inMotion'])  # pylint: disable=protected-access
            else:
                vehicle.in_motion._set_value(None)  # pylint: disable=protected-access
            log_extra_keys(LOG_API, 'connection status', data,  {'unreachable', 'inMotion'})
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
        data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache, allow_empty=True)
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
        else:
            vehicle.position.latitude._set_value(None)  # pylint: disable=protected-access
            vehicle.position.longitude._set_value(None)  # pylint: disable=protected-access
            vehicle.position.position_type._set_value(None)  # pylint: disable=protected-access
        return vehicle

    def fetch_maintenance(self, vehicle: SkodaVehicle, no_cache: bool = False) -> SkodaVehicle:
        """
        Fetches the maintenance information for a given Skoda vehicle.

        Args:
            vehicle (SkodaVehicle): The vehicle object for which maintenance information is to be fetched.
            no_cache (bool, optional): If True, bypasses the cache and fetches fresh data. Defaults to False.

        Returns:
            SkodaVehicle: The vehicle object with updated maintenance information.

        Raises:
            APIError: If the VIN is missing or if the 'capturedAt' field is missing in the fetched data.
            ValueError: If the vehicle has no charging object.
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        if vehicle.position is None:
            raise ValueError('Vehicle has no charging object')
        url = f'https://mysmob.api.connect.skoda-auto.cz/api/v3/vehicle-maintenance/vehicles/{vin}/report'
        data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        #{'capturedAt': '2025-02-24T19:54:32.728Z', 'inspectionDueInDays': 620, 'mileageInKm': 2512}
        if data is not None:
            if 'capturedAt' in data and data['capturedAt'] is not None:
                captured_at: datetime = robust_time_parse(data['capturedAt'])
            else:
                raise APIError('Could not fetch maintenance, capturedAt missing')
            if 'mileageInKm' in data and data['mileageInKm'] is not None:
                vehicle.odometer._set_value(value=data['mileageInKm'], measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access
            else:
                vehicle.odometer._set_value(None)  # pylint: disable=protected-access
            if 'inspectionDueInDays' in data and data['inspectionDueInDays'] is not None:
                inspection_due: timedelta = timedelta(days=data['inspectionDueInDays'])
                inspection_date: datetime = captured_at + inspection_due
                inspection_date = inspection_date.replace(hour=0, minute=0, second=0, microsecond=0)
                # pylint: disable-next=protected-access
                vehicle.maintenance.inspection_due_at._set_value(value=inspection_date, measured=captured_at)
            else:
                vehicle.maintenance.inspection_due_at._set_value(None)  # pylint: disable=protected-access
            log_extra_keys(LOG_API, 'maintenance', data,  {'capturedAt', 'mileageInKm', 'inspectionDueInDays'})

        #url = f'https://mysmob.api.connect.skoda-auto.cz/api/v1/vehicle-health-report/warning-lights/{vin}'
        #data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        #{'capturedAt': '2025-02-24T15:32:35.032Z', 'mileageInKm': 2512, 'warningLights': [{'category': 'ASSISTANCE', 'defects': []}, {'category': 'COMFORT', 'defects': []}, {'category': 'BRAKE', 'defects': []}, {'category': 'ELECTRIC_ENGINE', 'defects': []}, {'category': 'LIGHTING', 'defects': []}, {'category': 'TIRE', 'defects': []}, {'category': 'OTHER', 'defects': []}]}
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
                precision: float = 0.5
                min_temperature: Optional[float] = None
                max_temperature: Optional[float] = None
                unit: Temperature = Temperature.UNKNOWN
                if 'unitInCar' in data['targetTemperature'] and data['targetTemperature']['unitInCar'] is not None:
                    if data['targetTemperature']['unitInCar'] == 'CELSIUS':
                        unit = Temperature.C
                        min_temperature: Optional[float] = 16
                        max_temperature: Optional[float] = 29.5
                    elif data['targetTemperature']['unitInCar'] == 'FAHRENHEIT':
                        unit = Temperature.F
                        min_temperature: Optional[float] = 61
                        max_temperature: Optional[float] = 85
                    elif data['targetTemperature']['unitInCar'] == 'KELVIN':
                        unit = Temperature.K
                    else:
                        LOG_API.info('Unknown temperature unit for targetTemperature in air-conditioning %s', data['targetTemperature']['unitInCar'])
                if 'temperatureValue' in data['targetTemperature'] and data['targetTemperature']['temperatureValue'] is not None:
                    # pylint: disable-next=protected-access
                    vehicle.climatization.settings.target_temperature._set_value(value=data['targetTemperature']['temperatureValue'],
                                                                                 measured=captured_at,
                                                                                 unit=unit)
                    vehicle.climatization.settings.target_temperature.precision = precision
                    vehicle.climatization.settings.target_temperature.minimum = min_temperature
                    vehicle.climatization.settings.target_temperature.maximum = max_temperature

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
                heating_on: bool = False
                all_heating_invalid: bool = True
                for window_id, state in data['windowHeatingState'].items():
                    if window_id != 'unspecified':
                        if window_id in vehicle.window_heatings.windows:
                            window: WindowHeatings.WindowHeating = vehicle.window_heatings.windows[window_id]
                        else:
                            window = WindowHeatings.WindowHeating(window_id=window_id, window_heatings=vehicle.window_heatings)
                            vehicle.window_heatings.windows[window_id] = window
                        
                        if state.lower() in [item.value for item in WindowHeatings.HeatingState]:
                            window_heating_state: WindowHeatings.HeatingState = WindowHeatings.HeatingState(state.lower())
                            if window_heating_state == WindowHeatings.HeatingState.ON:
                                heating_on = True
                            if window_heating_state in [WindowHeatings.HeatingState.ON,
                                                        WindowHeatings.HeatingState.OFF]:
                                all_heating_invalid = False
                            window.heating_state._set_value(window_heating_state, measured=captured_at)  # pylint: disable=protected-access
                        else:
                            LOG_API.info('Unknown window heating state %s not in %s', state.lower(), str(WindowHeatings.HeatingState))
                            # pylint: disable-next=protected-access
                            window.heating_state._set_value(WindowHeatings.HeatingState.UNKNOWN, measured=captured_at)
                if all_heating_invalid:
                    # pylint: disable-next=protected-access
                    vehicle.window_heatings.heating_state._set_value(WindowHeatings.HeatingState.INVALID, measured=captured_at)
                else:
                    if heating_on:
                        # pylint: disable-next=protected-access
                        vehicle.window_heatings.heating_state._set_value(WindowHeatings.HeatingState.ON, measured=captured_at)
                    else:
                        # pylint: disable-next=protected-access
                        vehicle.window_heatings.heating_state._set_value(WindowHeatings.HeatingState.OFF, measured=captured_at)
                if vehicle.window_heatings is not None and vehicle.window_heatings.commands is not None \
                        and not vehicle.window_heatings.commands.contains_command('start-stop'):
                    start_stop_command = WindowHeatingStartStopCommand(parent=vehicle.window_heatings.commands)
                    start_stop_command._add_on_set_hook(self.__on_window_heating_start_stop)  # pylint: disable=protected-access
                    start_stop_command.enabled = True
                    vehicle.window_heatings.commands.add_command(start_stop_command)
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
                                                             'windowHeatingEnabled', 'seatHeatingActivated', 'windowHeatingState', 'errors'})
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
                            if 'statuses' in capability_dict and capability_dict['statuses'] is not None:
                                statuses = capability_dict['statuses']
                                if isinstance(statuses, list):
                                    for status in statuses:
                                        if status in [item.name for item in Capability.Status]:
                                            capability.status.value.append(Capability.Status[status])
                                        else:
                                            LOG_API.warning('Capability status unkown %s', status)
                                            capability.status.value.append(Capability.Status.UNKNOWN)
                                else:
                                    LOG_API.warning('Capability status not a list in %s', statuses)
                            else:
                                capability.status.value.clear()
                            log_extra_keys(LOG_API, 'capability', capability_dict, {'id', 'statuses'})
                        else:
                            raise APIError('Could not parse capability, id missing')
                    for capability_id in vehicle.capabilities.capabilities.keys() - found_capabilities:
                        vehicle.capabilities.remove_capability(capability_id)
                else:
                    vehicle.capabilities.clear_capabilities()
            else:
                vehicle.capabilities.clear_capabilities()

            if vehicle.capabilities.has_capability('VEHICLE_WAKE_UP_TRIGGER', check_status_ok=True):
                if vehicle.commands is not None and vehicle.commands.commands is not None \
                        and not vehicle.commands.contains_command('wake-sleep'):
                    wake_sleep_command = WakeSleepCommand(parent=vehicle.commands)
                    wake_sleep_command._add_on_set_hook(self.__on_wake_sleep)  # pylint: disable=protected-access
                    wake_sleep_command.enabled = True
                    vehicle.commands.add_command(wake_sleep_command)

            # Add HONK_AND_FLASH command if necessary capabilities are available
            if vehicle.capabilities.has_capability('HONK_AND_FLASH', check_status_ok=True) \
                    and vehicle.capabilities.has_capability('PARKING_POSITION', check_status_ok=True):
                if vehicle.commands is not None and vehicle.commands.commands is not None \
                        and not vehicle.commands.contains_command('honk-flash'):
                    honk_flash_command = HonkAndFlashCommand(parent=vehicle.commands)
                    honk_flash_command._add_on_set_hook(self.__on_honk_flash)  # pylint: disable=protected-access
                    honk_flash_command.enabled = True
                    vehicle.commands.add_command(honk_flash_command)

            # Add lock and unlock command
            if vehicle.capabilities.has_capability('ACCESS', check_status_ok=True):
                if vehicle.doors is not None and vehicle.doors.commands is not None and vehicle.doors.commands.commands is not None \
                        and not vehicle.doors.commands.contains_command('lock-unlock'):
                    lock_unlock_command = LockUnlockCommand(parent=vehicle.doors.commands)
                    lock_unlock_command._add_on_set_hook(self.__on_lock_unlock)  # pylint: disable=protected-access
                    lock_unlock_command.enabled = True
                    vehicle.doors.commands.add_command(lock_unlock_command)

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

    def fetch_vehicle_images(self, vehicle: SkodaVehicle, no_cache: bool = False) -> SkodaVehicle:
        if SUPPORT_IMAGES:
            url: str = f'https://mysmob.api.connect.skoda-auto.cz/api/v1/vehicle-information/{vehicle.vin.value}/renders'
            data = self._fetch_data(url, session=self.session, allow_http_error=True)
            if data is not None and 'compositeRenders' in data:  # pylint: disable=too-many-nested-blocks
                for image in data['compositeRenders']:
                    if 'layers' not in image or image['layers'] is None or len(image['layers']) == 0:
                        continue
                    image_url: Optional[str] = None
                    for layer in image['layers']:
                        if 'url' in layer and layer['url'] is not None:
                            image_url = layer['url']
                            break
                    if image_url is None:
                        continue
                    img = None
                    cache_date = None
                    if self.active_config['max_age'] is not None and self.session.cache is not None and image_url in self.session.cache:
                        img, cache_date_string = self.session.cache[image_url]
                        img = base64.b64decode(img)  # pyright: ignore[reportPossiblyUnboundVariable]
                        img = Image.open(io.BytesIO(img))  # pyright: ignore[reportPossiblyUnboundVariable]
                        cache_date = datetime.fromisoformat(cache_date_string)
                    if img is None or self.active_config['max_age'] is None \
                            or (cache_date is not None and cache_date < (datetime.utcnow() - timedelta(seconds=self.active_config['max_age']))):
                        try:
                            image_download_response = requests.get(image_url, stream=True)
                            if image_download_response.status_code == requests.codes['ok']:
                                img = Image.open(image_download_response.raw)  # pyright: ignore[reportPossiblyUnboundVariable]
                                if self.session.cache is not None:
                                    buffered = io.BytesIO()  # pyright: ignore[reportPossiblyUnboundVariable]
                                    img.save(buffered, format="PNG")
                                    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")  # pyright: ignore[reportPossiblyUnboundVariable]
                                    self.session.cache[image_url] = (img_str, str(datetime.utcnow()))
                            elif image_download_response.status_code == requests.codes['unauthorized']:
                                LOG.info('Server asks for new authorization')
                                self.session.login()
                                image_download_response = self.session.get(image_url, stream=True)
                                if image_download_response.status_code == requests.codes['ok']:
                                    img = Image.open(image_download_response.raw)  # pyright: ignore[reportPossiblyUnboundVariable]
                                    if self.session.cache is not None:
                                        buffered = io.BytesIO()  # pyright: ignore[reportPossiblyUnboundVariable]
                                        img.save(buffered, format="PNG")
                                        img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")  # pyright: ignore[reportPossiblyUnboundVariable]
                                        self.session.cache[image_url] = (img_str, str(datetime.utcnow()))
                        except requests.exceptions.ConnectionError as connection_error:
                            raise RetrievalError(f'Connection error: {connection_error}') from connection_error
                        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
                            raise RetrievalError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
                        except requests.exceptions.ReadTimeout as timeout_error:
                            raise RetrievalError(f'Timeout during read: {timeout_error}') from timeout_error
                        except requests.exceptions.RetryError as retry_error:
                            raise RetrievalError(f'Retrying failed: {retry_error}') from retry_error
                    if img is not None:
                        vehicle._car_images[image['viewType']] = img  # pylint: disable=protected-access
                        if image['viewType'] == 'UNMODIFIED_EXTERIOR_FRONT':
                            if 'car_picture' in vehicle.images.images:
                                vehicle.images.images['car_picture']._set_value(img)  # pylint: disable=protected-access
                            else:
                                vehicle.images.images['car_picture'] = ImageAttribute(name="car_picture", parent=vehicle.images,
                                                                                      value=img, tags={'carconnectivity'})
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
            if 'carCapturedTimestamp' not in range_data or range_data['carCapturedTimestamp'] is None:
                raise APIError('Could not fetch driving range, carCapturedTimestamp missing')
            captured_at: datetime = robust_time_parse(range_data['carCapturedTimestamp'])
            # Check vehicle type and if it does not match the current vehicle type, create a new vehicle object using copy constructor
            if 'carType' in range_data and range_data['carType'] is not None:
                try:
                    car_type = GenericVehicle.Type(range_data['carType'])
                    if car_type == GenericVehicle.Type.ELECTRIC and not isinstance(vehicle, SkodaElectricVehicle):
                        LOG.debug('Promoting %s to SkodaElectricVehicle object for %s', vehicle.__class__.__name__, vin)
                        vehicle = SkodaElectricVehicle(garage=self.car_connectivity.garage, origin=vehicle)
                        self.car_connectivity.garage.replace_vehicle(vin, vehicle)
                    elif car_type in [GenericVehicle.Type.FUEL,
                                      GenericVehicle.Type.GASOLINE,
                                      GenericVehicle.Type.PETROL,
                                      GenericVehicle.Type.DIESEL,
                                      GenericVehicle.Type.CNG,
                                      GenericVehicle.Type.LPG] \
                            and not isinstance(vehicle, SkodaCombustionVehicle):
                        LOG.debug('Promoting %s to SkodaCombustionVehicle object for %s', vehicle.__class__.__name__, vin)
                        vehicle = SkodaCombustionVehicle(garage=self.car_connectivity.garage, origin=vehicle)
                        self.car_connectivity.garage.replace_vehicle(vin, vehicle)
                    elif car_type == GenericVehicle.Type.HYBRID and not isinstance(vehicle, SkodaHybridVehicle):
                        LOG.debug('Promoting %s to SkodaHybridVehicle object for %s', vehicle.__class__.__name__, vin)
                        vehicle = SkodaHybridVehicle(garage=self.car_connectivity.garage, origin=vehicle)
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
                    elif vehicle_status_data['overall']['doorsLocked'] == 'NO':
                        vehicle.doors.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                        vehicle.doors.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
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
        if not no_cache and (self.active_config['max_age'] is not None and session.cache is not None and url in session.cache):
            data, cache_date_string = session.cache[url]
            cache_date = datetime.fromisoformat(cache_date_string)
        if data is None or self.active_config['max_age'] is None \
                or (cache_date is not None and cache_date < (datetime.utcnow() - timedelta(seconds=self.active_config['max_age']))):
            try:
                status_response: requests.Response = session.get(url, allow_redirects=False)
                self._record_elapsed(status_response.elapsed)
                if status_response.status_code in (requests.codes['ok'], requests.codes['multiple_status']):
                    data = status_response.json()
                    if session.cache is not None:
                        session.cache[url] = (data, str(datetime.utcnow()))
                elif status_response.status_code == requests.codes['no_content'] and allow_empty:
                    data = None
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

    def get_type(self) -> str:
        return "carconnectivity-connector-skoda"

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
        try:
            settings_response: requests.Response = self.session.post(url, data=json.dumps(setting_dict), allow_redirects=True)
            if settings_response.status_code != requests.codes['accepted']:
                LOG.error('Could not set target temperature (%s)', settings_response.status_code)
                raise SetterError(f'Could not set value ({settings_response.status_code})')
        except requests.exceptions.ConnectionError as connection_error:
            raise SetterError(f'Connection error: {connection_error}.'
                              ' If this happens frequently, please check if other applications communicate with the Skoda server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise SetterError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise SetterError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise SetterError(f'Retrying failed: {retry_error}') from retry_error
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
        try:
            settings_response: requests.Response = self.session.post(url, data=json.dumps(setting_dict), allow_redirects=True)
            if settings_response.status_code != requests.codes['accepted']:
                LOG.error('Could not set air conditioning at unlock (%s)', settings_response.status_code)
                raise SetterError(f'Could not set value ({settings_response.status_code})')
        except requests.exceptions.ConnectionError as connection_error:
            raise SetterError(f'Connection error: {connection_error}.'
                                ' If this happens frequently, please check if other applications communicate with the Skoda server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise SetterError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise SetterError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise SetterError(f'Retrying failed: {retry_error}') from retry_error
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
        try:
            settings_response: requests.Response = self.session.post(url, data=json.dumps(setting_dict), allow_redirects=True)
            if settings_response.status_code != requests.codes['accepted']:
                LOG.error('Could not set air conditioning window heating (%s)', settings_response.status_code)
                raise SetterError(f'Could not set value ({settings_response.status_code})')
        except requests.exceptions.ConnectionError as connection_error:
            raise SetterError(f'Connection error: {connection_error}.'
                                ' If this happens frequently, please check if other applications communicate with the Skoda server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise SetterError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise SetterError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise SetterError(f'Retrying failed: {retry_error}') from retry_error
        return window_heating_value

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
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        command_dict = {}
        try:
            if command_arguments['command'] == ClimatizationStartStopCommand.Command.START:
                command_dict['heaterSource'] = 'ELECTRIC'
                command_dict['targetTemperature'] = {}
                precision: float = 0.5
                if 'target_temperature' in command_arguments:
                    # Round target temperature to nearest 0.5
                    command_dict['targetTemperature']['temperatureValue'] = round(command_arguments['target_temperature'] / precision) * precision
                    if 'target_temperature_unit' in command_arguments:
                        if not isinstance(command_arguments['target_temperature_unit'], Temperature):
                            raise CommandError('Temperature unit is not of type Temperature')
                        if command_arguments['target_temperature_unit'] == Temperature.C:
                            command_dict['targetTemperature']['unitInCar'] = 'CELSIUS'
                        elif command_arguments['target_temperature_unit'] == Temperature.F:
                            command_dict['targetTemperature']['unitInCar'] = 'FAHRENHEIT'
                        elif command_arguments['target_temperature_unit'] == Temperature.K:
                            command_dict['targetTemperature']['unitInCar'] = 'KELVIN'
                        else:
                            raise CommandError(f'Unknown temperature unit {command_arguments["target_temperature_unit"]}')
                    else:
                        command_dict['targetTemperature']['unitInCar'] = 'CELSIUS'
                elif start_stop_command.parent is not None and (climatization := start_stop_command.parent.parent) is not None \
                        and isinstance(climatization, Climatization) and climatization.settings is not None \
                        and climatization.settings.target_temperature is not None and climatization.settings.target_temperature.enabled \
                        and climatization.settings.target_temperature.value is not None:  # pylint: disable=too-many-boolean-expressions
                    if climatization.settings.target_temperature.precision is not None:
                        precision = climatization.settings.target_temperature.precision
                    # Round target temperature to nearest 0.5
                    command_dict['targetTemperature']['temperatureValue'] = round(climatization.settings.target_temperature.value / precision) * precision
                    if climatization.settings.target_temperature.unit == Temperature.C:
                        command_dict['targetTemperature']['unitInCar'] = 'CELSIUS'
                    elif climatization.settings.target_temperature.unit == Temperature.F:
                        command_dict['targetTemperature']['unitInCar'] = 'FAHRENHEIT'
                    elif climatization.settings.target_temperature.unit == Temperature.K:
                        command_dict['targetTemperature']['unitInCar'] = 'KELVIN'
                    else:
                        raise CommandError(f'Unknown temperature unit {climatization.settings.target_temperature.unit}')
                else:
                    command_dict['targetTemperature']['temperatureValue'] = 25.0
                    command_dict['targetTemperature']['unitInCar'] = 'CELSIUS'
                url = f'https://mysmob.api.connect.skoda-auto.cz/api/v2/air-conditioning/{vin}/start'
                command_response: requests.Response = self.session.post(url, data=json.dumps(command_dict), allow_redirects=True)
            elif command_arguments['command'] == ClimatizationStartStopCommand.Command.STOP:
                url = f'https://mysmob.api.connect.skoda-auto.cz/api/v2/air-conditioning/{vin}/stop'
                command_response: requests.Response = self.session.post(url, allow_redirects=True)
            else:
                raise CommandError(f'Unknown command {command_arguments["command"]}')

            if command_response.status_code != requests.codes['accepted']:
                LOG.error('Could not start/stop air conditioning (%s: %s)', command_response.status_code, command_response.text)
                raise CommandError(f'Could not start/stop air conditioning ({command_response.status_code}: {command_response.text})')
        except requests.exceptions.ConnectionError as connection_error:
            raise CommandError(f'Connection error: {connection_error}.'
                               ' If this happens frequently, please check if other applications communicate with the Skoda server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise CommandError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise CommandError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise CommandError(f'Retrying failed: {retry_error}') from retry_error
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
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        try:
            if command_arguments['command'] == ChargingStartStopCommand.Command.START:
                url = f'https://mysmob.api.connect.skoda-auto.cz/api/v1/charging/{vin}/start'
                command_response: requests.Response = self.session.post(url, allow_redirects=True)
            elif command_arguments['command'] == ChargingStartStopCommand.Command.STOP:
                url = f'https://mysmob.api.connect.skoda-auto.cz/api/v1/charging/{vin}/stop'
                
                command_response: requests.Response = self.session.post(url, allow_redirects=True)
            else:
                raise CommandError(f'Unknown command {command_arguments["command"]}')

            if command_response.status_code != requests.codes['accepted']:
                LOG.error('Could not start/stop charging (%s: %s)', command_response.status_code, command_response.text)
                raise CommandError(f'Could not start/stop charging ({command_response.status_code}: {command_response.text})')
        except requests.exceptions.ConnectionError as connection_error:
            raise CommandError(f'Connection error: {connection_error}.'
                               ' If this happens frequently, please check if other applications communicate with the Skoda server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise CommandError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise CommandError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise CommandError(f'Retrying failed: {retry_error}') from retry_error
        return command_arguments

    def __on_honk_flash(self, honk_flash_command: HonkAndFlashCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if honk_flash_command.parent is None or honk_flash_command.parent.parent is None \
                or not isinstance(honk_flash_command.parent.parent, SkodaVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        vehicle: SkodaVehicle = honk_flash_command.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        if 'duration' in command_arguments:
            LOG.warning('Duration argument is not supported by the Skoda API')
        command_dict = {}
        if command_arguments['command'] in [HonkAndFlashCommand.Command.FLASH, HonkAndFlashCommand.Command.HONK_AND_FLASH]:
            command_dict['mode'] = command_arguments['command'].name
            command_dict['vehiclePosition'] = {}
            if vehicle.position is None or vehicle.position.latitude is None or vehicle.position.longitude is None \
                    or vehicle.position.latitude.value is None or vehicle.position.longitude.value is None \
                    or not vehicle.position.latitude.enabled or not vehicle.position.longitude.enabled:
                raise CommandError('Can only execute honk and flash commands if vehicle position is known')
            command_dict['vehiclePosition']['latitude'] = vehicle.position.latitude.value
            command_dict['vehiclePosition']['longitude'] = vehicle.position.longitude.value

            url = f'https://mysmob.api.connect.skoda-auto.cz/api/v1/vehicle-access/{vin}/honk-and-flash'
            try:
                command_response: requests.Response = self.session.post(url, data=json.dumps(command_dict), allow_redirects=True)
                if command_response.status_code != requests.codes['accepted']:
                    LOG.error('Could not execute honk or flash command (%s: %s)', command_response.status_code, command_response.text)
                    raise CommandError(f'Could not execute honk or flash command ({command_response.status_code}: {command_response.text})')
            except requests.exceptions.ConnectionError as connection_error:
                raise CommandError(f'Connection error: {connection_error}.'
                                   ' If this happens frequently, please check if other applications communicate with the Skoda server.') from connection_error
            except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
                raise SetterError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
            except requests.exceptions.ReadTimeout as timeout_error:
                raise CommandError(f'Timeout during read: {timeout_error}') from timeout_error
            except requests.exceptions.RetryError as retry_error:
                raise CommandError(f'Retrying failed: {retry_error}') from retry_error
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')
        return command_arguments

    def __on_lock_unlock(self, lock_unlock_command: LockUnlockCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if lock_unlock_command.parent is None or lock_unlock_command.parent.parent is None \
                or lock_unlock_command.parent.parent.parent is None or not isinstance(lock_unlock_command.parent.parent.parent, SkodaVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise SetterError('Command arguments are not a dictionary')
        vehicle: SkodaVehicle = lock_unlock_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        command_dict = {}
        if 'spin' in command_arguments:
            command_dict['currentSpin'] = command_arguments['spin']
        else:
            if self.active_config['spin'] is None:
                raise CommandError('S-PIN is missing, please add S-PIN to your configuration or .netrc file')
            command_dict['currentSpin'] = self.active_config['spin']
        if command_arguments['command'] == LockUnlockCommand.Command.LOCK:
            url = f'https://mysmob.api.connect.skoda-auto.cz/api/v1/vehicle-access/{vin}/lock'
        elif command_arguments['command'] == LockUnlockCommand.Command.UNLOCK:
            url = f'https://mysmob.api.connect.skoda-auto.cz/api/v1/vehicle-access/{vin}/unlock'
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')
        try:
            command_response: requests.Response = self.session.post(url, data=json.dumps(command_dict), allow_redirects=True)
            if command_response.status_code != requests.codes['accepted']:
                LOG.error('Could not execute locking command (%s: %s)', command_response.status_code, command_response.text)
                raise CommandError(f'Could not execute locking command ({command_response.status_code}: {command_response.text})')
        except requests.exceptions.ConnectionError as connection_error:
            raise CommandError(f'Connection error: {connection_error}.'
                                ' If this happens frequently, please check if other applications communicate with the Skoda server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise CommandError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise CommandError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise CommandError(f'Retrying failed: {retry_error}') from retry_error
        return command_arguments

    def __on_wake_sleep(self, wake_sleep_command: WakeSleepCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if wake_sleep_command.parent is None or wake_sleep_command.parent.parent is None \
                or not isinstance(wake_sleep_command.parent.parent, GenericVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        vehicle: GenericVehicle = wake_sleep_command.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        if command_arguments['command'] == WakeSleepCommand.Command.WAKE:
            url = f'https://mysmob.api.connect.skoda-auto.cz/api/v1/vehicle-wakeup/{vin}?applyRequestLimiter=true'

            try:
                command_response: requests.Response = self.session.post(url, data='{}', allow_redirects=True)
                if command_response.status_code != requests.codes['accepted']:
                    LOG.error('Could not execute wake command (%s: %s)', command_response.status_code, command_response.text)
                    raise CommandError(f'Could not execute wake command ({command_response.status_code}: {command_response.text})')
            except requests.exceptions.ConnectionError as connection_error:
                raise CommandError(f'Connection error: {connection_error}.'
                                   ' If this happens frequently, please check if other applications communicate with the Skoda server.') from connection_error
            except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
                raise CommandError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
            except requests.exceptions.ReadTimeout as timeout_error:
                raise CommandError(f'Timeout during read: {timeout_error}') from timeout_error
            except requests.exceptions.RetryError as retry_error:
                raise CommandError(f'Retrying failed: {retry_error}') from retry_error
        elif command_arguments['command'] == WakeSleepCommand.Command.SLEEP:
            raise CommandError('Sleep command not supported by vehicle. Vehicle will put itself to sleep')
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')
        return command_arguments

    def __on_spin(self, spin_command: SpinCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        del spin_command
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        command_dict = {}
        if self.active_config['spin'] is None:
            raise CommandError('S-PIN is missing, please add S-PIN to your configuration or .netrc file')
        if 'spin' in command_arguments:
            command_dict['currentSpin'] = command_arguments['spin']
        else:
            if self.active_config['spin'] is None or self.active_config['spin'] == '':
                raise CommandError('S-PIN is missing, please add S-PIN to your configuration or .netrc file')
            command_dict['currentSpin'] = self.active_config['spin']
        if command_arguments['command'] == SpinCommand.Command.VERIFY:
            url = 'https://mysmob.api.connect.skoda-auto.cz/api/v1/spin/verify'
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')
        try:
            command_response: requests.Response = self.session.post(url, data=json.dumps(command_dict), allow_redirects=True)
            if command_response.status_code != requests.codes['ok']:
                LOG.error('Could not execute spin command (%s: %s)', command_response.status_code, command_response.text)
                raise CommandError(f'Could not execute spin command ({command_response.status_code}: {command_response.text})')
            else:
                LOG.info('Spin verify command executed successfully')
        except requests.exceptions.ConnectionError as connection_error:
            raise CommandError(f'Connection error: {connection_error}.'
                               ' If this happens frequently, please check if other applications communicate with the Skoda server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise CommandError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise CommandError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise CommandError(f'Retrying failed: {retry_error}') from retry_error
        return command_arguments

    def __on_window_heating_start_stop(self, start_stop_command: WindowHeatingStartStopCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if start_stop_command.parent is None or start_stop_command.parent.parent is None \
                or start_stop_command.parent.parent.parent is None or not isinstance(start_stop_command.parent.parent.parent, SkodaVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        vehicle: SkodaVehicle = start_stop_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        try:
            if command_arguments['command'] == WindowHeatingStartStopCommand.Command.START:
                url = f'https://mysmob.api.connect.skoda-auto.cz/api/v2/air-conditioning/{vin}/start-window-heating'
                command_response: requests.Response = self.session.post(url, allow_redirects=True)
            elif command_arguments['command'] == WindowHeatingStartStopCommand.Command.STOP:
                url = f'https://mysmob.api.connect.skoda-auto.cz/api/v2/air-conditioning/{vin}/stop-window-heating'
                
                command_response: requests.Response = self.session.post(url, allow_redirects=True)
            else:
                raise CommandError(f'Unknown command {command_arguments["command"]}')

            if command_response.status_code != requests.codes['accepted']:
                LOG.error('Could not start/stop window heating (%s: %s)', command_response.status_code, command_response.text)
                raise CommandError(f'Could not start/stop window heating ({command_response.status_code}: {command_response.text})')
        except requests.exceptions.ConnectionError as connection_error:
            raise CommandError(f'Connection error: {connection_error}.'
                               ' If this happens frequently, please check if other applications communicate with the Skoda server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise CommandError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise CommandError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise CommandError(f'Retrying failed: {retry_error}') from retry_error
        return command_arguments

    def __on_charging_target_level_change(self, level_attribute: LevelAttribute, target_level: float) -> float:
        """
        Callback for the charging target level change.

        Args:
            level_attribute (LevelAttribute): The level attribute that changed.
            target_level (float): The new target level.
        """
        if level_attribute.parent is None or level_attribute.parent.parent is None \
                or level_attribute.parent.parent.parent is None or not isinstance(level_attribute.parent.parent.parent, SkodaVehicle):
            raise SetterError('Object hierarchy is not as expected')
        vehicle: SkodaVehicle = level_attribute.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise SetterError('VIN in object hierarchy missing')
        precision: float = level_attribute.precision if level_attribute.precision is not None else 10.0
        target_level = round(target_level / precision) * precision
        setting_dict = {}
        setting_dict['targetSOCInPercent'] = target_level

        url = f'https://mysmob.api.connect.skoda-auto.cz/api/v1/charging/{vin}/set-charge-limit'
        try:
            settings_response: requests.Response = self.session.put(url, data=json.dumps(setting_dict), allow_redirects=True)
            if settings_response.status_code != requests.codes['accepted']:
                LOG.error('Could not set target level (%s)', settings_response.status_code)
                raise SetterError(f'Could not set value ({settings_response.status_code})')
        except requests.exceptions.ConnectionError as connection_error:
            raise SetterError(f'Connection error: {connection_error}.'
                              ' If this happens frequently, please check if other applications communicate with the Skoda server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise SetterError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise SetterError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise SetterError(f'Retrying failed: {retry_error}') from retry_error
        return target_level

    def __on_charging_maximum_current_change(self, current_attribute: CurrentAttribute, maximum_current: float) -> float:
        """
        Callback for the charging target level change.

        Args:
            current_attribute (CurrentAttribute): The current attribute that changed.
            maximum_current (float): The new maximum current.
        """
        if current_attribute.parent is None or current_attribute.parent.parent is None \
                or current_attribute.parent.parent.parent is None or not isinstance(current_attribute.parent.parent.parent, SkodaVehicle):
            raise SetterError('Object hierarchy is not as expected')
        vehicle: SkodaVehicle = current_attribute.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise SetterError('VIN in object hierarchy missing')
        setting_dict = {}
        precision: float = current_attribute.precision if current_attribute.precision is not None else 1.0
        maximum_current = round(maximum_current / precision) * precision
        if maximum_current < 16:
            setting_dict['chargingCurrent'] = "REDUCED"
            maximum_current = 6.0
        else:
            setting_dict['chargingCurrent'] = "MAXIMUM"
            maximum_current = 16.0

        url = f'https://mysmob.api.connect.skoda-auto.cz/api/v1/charging/{vin}/set-charging-current'
        try:
            settings_response: requests.Response = self.session.put(url, data=json.dumps(setting_dict), allow_redirects=True)
            if settings_response.status_code != requests.codes['accepted']:
                LOG.error('Could not set target charging current (%s)', settings_response.status_code)
                raise SetterError(f'Could not set value ({settings_response.status_code})')
        except requests.exceptions.ConnectionError as connection_error:
            raise SetterError(f'Connection error: {connection_error}.'
                              ' If this happens frequently, please check if other applications communicate with the Skoda server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise SetterError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise SetterError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise SetterError(f'Retrying failed: {retry_error}') from retry_error
        return maximum_current

    def __on_charging_auto_unlock_change(self, boolean_attribute: BooleanAttribute, auto_unlock: bool) -> bool:
        """
        Callback for the charging target level change.

        Args:
            boolean_attribute (BooleanAttribute): The boolean attribute that changed.
            auto_unlock (float): The new auto_unlock setting.
        """
        if boolean_attribute.parent is None or boolean_attribute.parent.parent is None \
                or boolean_attribute.parent.parent.parent is None or not isinstance(boolean_attribute.parent.parent.parent, SkodaVehicle):
            raise SetterError('Object hierarchy is not as expected')
        vehicle: SkodaVehicle = boolean_attribute.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise SetterError('VIN in object hierarchy missing')
        setting_dict = {}
        if auto_unlock:
            setting_dict['autoUnlockPlug'] = "PERMANENT"
        else:
            setting_dict['autoUnlockPlug'] = "OFF"

        url = f'https://mysmob.api.connect.skoda-auto.cz/api/v1/charging/{vin}/set-auto-unlock-plug'
        try:
            settings_response: requests.Response = self.session.put(url, data=json.dumps(setting_dict), allow_redirects=True)
            if settings_response.status_code != requests.codes['accepted']:
                LOG.error('Could not set auto unlock setting (%s)', settings_response.status_code)
                raise SetterError(f'Could not set value ({settings_response.status_code})')
        except requests.exceptions.ConnectionError as connection_error:
            raise SetterError(f'Connection error: {connection_error}.'
                              ' If this happens frequently, please check if other applications communicate with the Skoda server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise SetterError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise SetterError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise SetterError(f'Retrying failed: {retry_error}') from retry_error
        return auto_unlock

    def get_name(self) -> str:
        return "Skoda Connector"
