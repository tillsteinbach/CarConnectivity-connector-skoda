"""Module implements the connector to interact with the Skoda API."""
from __future__ import annotations
from typing import TYPE_CHECKING

import threading
import os
import logging
import netrc
from datetime import datetime, timedelta
import requests

from carconnectivity.vehicle import GenericVehicle
from carconnectivity.garage import Garage
from carconnectivity.errors import AuthenticationError, APIError, TooManyRequestsError, RetrievalError
from carconnectivity.util import robust_time_parse, log_extra_keys
from carconnectivity.units import Length
from carconnectivity.doors import Doors
from carconnectivity.windows import Windows
from carconnectivity.lights import Lights

from carconnectivity_connectors.base.connector import BaseConnector
from carconnectivity_connectors.skoda.auth.session_manager import SessionManager, SessionUser, Service
from carconnectivity_connectors.skoda.vehicle import SkodaElectricVehicle
from carconnectivity_connectors.skoda.capability import Capability
from carconnectivity_connectors.skoda._version import __version__

if TYPE_CHECKING:
    from typing import Dict, List, Optional, Any

    from requests import Session

    from carconnectivity.carconnectivity import CarConnectivity

LOG: logging.Logger = logging.getLogger("carconnectivity-connector-skoda")
LOG_API_DEBUG: logging.Logger = logging.getLogger("carconnectivity-connector-skoda-api-debug")


class Connector(BaseConnector):
    """
    Connector class for Skoda API connectivity.
    Args:
        car_connectivity (CarConnectivity): An instance of CarConnectivity.
        config (Dict): Configuration dictionary containing connection details.
    Attributes:
        max_age (Optional[int]): Maximum age for cached data in seconds.
    """
    def __init__(self, car_connectivity: CarConnectivity, config: Dict) -> None:
        BaseConnector.__init__(self, car_connectivity, config)
        LOG.info("Loading skoda connector with config %s", self.config)

        self._background_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

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
                    raise AuthenticationError(f'skoda entry was not found in {netrc_filename} netrc-file.'
                                              ' Create it or provide username and password in config') from err
            except FileNotFoundError as err:
                raise AuthenticationError(f'{netrc_filename} netrc-file was not found. Create it or provide username and password in config') from err

        self.max_age: int = 300
        if 'max_age' in self.config:
            self.max_age = self.config['max_age']

        self.intervall: int = 300
        if 'interval' in self.config:
            self.intervall = self.config['interval']
            if self.intervall < 180:
                raise ValueError('Intervall must be at least 180 seconds')

        if username is None or password is None:
            raise AuthenticationError('Username or password not provided')

        self._manager: SessionManager = SessionManager(tokenstore=car_connectivity.get_tokenstore(), cache=car_connectivity.get_cache())
        self._session: Session = self._manager.get_session(Service.MY_SKODA, SessionUser(username=username, password=password))
        self._session2: Session = self._manager.get_session(Service.MY_SKODA2, SessionUser(username=username, password=password))

        self._elapsed: List[timedelta] = []

    def startup(self) -> None:
        self._background_thread = threading.Thread(target=self._background_loop, daemon=True)
        self._background_thread.start()

    def _background_loop(self) -> None:
        self._stop_event.clear()
        while not self._stop_event.is_set():
            self.fetch_all()
            self._stop_event.wait(self.intervall)

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
        self._stop_event.set()
        if self._background_thread is not None:
            self._background_thread.join()
        self.persist()
        self._session.close()
        self._session2.close()
        return super().shutdown()

    def fetch_all(self) -> None:
        """
        Fetches all necessary data for the connector.

        This method calls the `fetch_vehicles` method to retrieve vehicle data.
        """
        self.fetch_vehicles()

    def fetch_vehicles(self) -> None:
        """
        Fetches the list of vehicles from the Skoda Connect API and updates the garage with new vehicles.
        This method sends a request to the Skoda Connect API to retrieve the list of vehicles associated with the user's account.
        If new vehicles are found in the response, they are added to the garage.

        Returns:
            None
        """
        garage: Garage = self.car_connectivity.garage
        url = 'https://api.connect.skoda-auto.cz/api/v4/garage'
        data: Dict[str, Any] | None = self._fetch_data(url, session=self._session)
        print(data)
        seen_vehicle_vins: set[str] = set()
        if data is not None:
            if 'vehicles' in data and data['vehicles'] is not None:
                for vehicle_dict in data['vehicles']:
                    if 'vin' in vehicle_dict and vehicle_dict['vin'] is not None:
                        seen_vehicle_vins.add(vehicle_dict['vin'])
                        vehicle: Optional[SkodaElectricVehicle] = garage.get_vehicle(vehicle_dict['vin'])  # pyright: ignore[reportAssignmentType]
                        if not vehicle:
                            vehicle = SkodaElectricVehicle(vin=vehicle_dict['vin'], garage=garage)
                            garage.add_vehicle(vehicle_dict['vin'], vehicle)

                        if 'licensePlate' in vehicle_dict and vehicle_dict['licensePlate'] is not None:
                            vehicle.license_plate._set_value(vehicle_dict['licensePlate'])  # pylint: disable=protected-access
                        else:
                            vehicle.license_plate._set_value(None)  # pylint: disable=protected-access

                        if 'capabilities' in vehicle_dict and vehicle_dict['capabilities'] is not None:
                            if 'capabilities' in vehicle_dict['capabilities'] and vehicle_dict['capabilities']['capabilities'] is not None:
                                found_capabilities = set()
                                for capability_dict in vehicle_dict['capabilities']['capabilities']:
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

                        if 'specification' in vehicle_dict and vehicle_dict['specification'] is not None:
                            if 'model' in vehicle_dict['specification'] and vehicle_dict['specification']['model'] is not None:
                                vehicle.model._set_value(vehicle_dict['specification']['model'])  # pylint: disable=protected-access
                            else:
                                vehicle.model._set_value(None)  # pylint: disable=protected-access
                            log_extra_keys(LOG_API_DEBUG, 'specification', vehicle_dict['specification'],  {'model'})
                        else:
                            vehicle.model._set_value(None)  # pylint: disable=protected-access
                        log_extra_keys(LOG_API_DEBUG, 'vehicles', vehicle_dict,  {'vin', 'licensePlate', 'capabilities', 'specification'})
                        self.fetch_vehicle_status(vehicle)
                    else:
                        raise APIError('Could not parse vehicle, vin missing')
        for vin in set(garage.list_vehicle_vins()) - seen_vehicle_vins:
            vehicle_to_remove = garage.get_vehicle(vin)
            if vehicle_to_remove is not None and vehicle_to_remove.is_managed_by_connector(self):
                garage.remove_vehicle(vin)

    def fetch_vehicle_status(self, vehicle: GenericVehicle) -> None:
        """
        Fetches the status of a vehicle from the Skoda API.

        Args:
            vehicle (GenericVehicle): The vehicle object containing the VIN.

        Returns:
            None
        """
        vin = vehicle.vin.value
        url = f'https://api.connect.skoda-auto.cz/api/v2/vehicle-status/{vin}'
        data: Dict[str, Any] | None = self._fetch_data(url, self._session2)
        if data is not None and 'remote' in data and data['remote'] is not None:
            remote = data['remote']
            if 'capturedAt' in remote and remote['capturedAt'] is not None:
                captured_at: datetime = robust_time_parse(remote['capturedAt'])
            else:
                raise APIError('Could not fetch vehicle status, capturedAt missing')
            if 'mileageInKm' in remote and remote['mileageInKm'] is not None:
                vehicle.odometer._set_value(value=remote['mileageInKm'], measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access
            else:
                vehicle.odometer._set_value(value=None, measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access
            if 'status' in remote and remote['status'] is not None:
                if 'open' in remote['status'] and remote['status']['open'] is not None:
                    if remote['status']['open'] == 'YES':
                        vehicle.doors.open_state._set_value(Doors.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                    elif remote['status']['open'] == 'NO':
                        vehicle.doors.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        vehicle.doors.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                        LOG_API_DEBUG.info('Unknown door open state: %s', remote['status']['open'])
                else:
                    vehicle.doors.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                if 'locked' in remote['status'] and remote['status']['locked'] is not None:
                    if remote['status']['locked'] == 'YES':
                        vehicle.doors.lock_state._set_value(Doors.LockState.LOCKED, measured=captured_at)  # pylint: disable=protected-access
                    elif remote['status']['locked'] == 'NO':
                        vehicle.doors.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        vehicle.doors.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                        LOG_API_DEBUG.info('Unknown door lock state: %s', remote['status']['locked'])
                else:
                    vehicle.doors.lock_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
            else:
                vehicle.doors.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                vehicle.doors.lock_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
            if 'doors' in remote and remote['doors'] is not None:
                seen_door_ids: set[str] = set()
                for door_status in remote['doors']:
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
                                LOG_API_DEBUG.info('Unknown door status %s', door_status['status'])
                                door.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                                door.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                        else:
                            door.lock_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                            door.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        raise APIError('Could not parse door, name missing')
                    log_extra_keys(LOG_API_DEBUG, 'doors', door_status, {'name', 'status'})
                for door_to_remove in set(vehicle.doors.doors) - seen_door_ids:
                    vehicle.doors.doors[door_to_remove].enabled = False
                    vehicle.doors.doors.pop(door_to_remove)
                log_extra_keys(LOG_API_DEBUG, 'status', remote['status'],  {'open', 'locked'})
            else:
                vehicle.doors.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                vehicle.doors.doors = {}
            if 'windows' in remote and remote['windows'] is not None:
                seen_window_ids: set[str] = set()
                all_windows_closed: bool = True
                for window_status in remote['windows']:
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
                                LOG_API_DEBUG.info('Unknown window status %s', window_status['status'])
                                window.open_state._set_value(Windows.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                        else:
                            window.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        raise APIError('Could not parse window, name missing')
                    log_extra_keys(LOG_API_DEBUG, 'doors', window_status, {'name', 'status'})
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
            if 'lights' in remote and remote['lights'] is not None:
                seen_light_ids: set[str] = set()
                if 'overallStatus' in remote['lights'] and remote['lights']['overallStatus'] is not None:
                    if remote['lights']['overallStatus'] == 'ON':
                        vehicle.lights.light_state._set_value(Lights.LightState.ON, measured=captured_at)  # pylint: disable=protected-access
                    elif remote['lights']['overallStatus'] == 'OFF':
                        vehicle.lights.light_state._set_value(Lights.LightState.OFF, measured=captured_at)  # pylint: disable=protected-access
                    elif remote['lights']['overallStatus'] == 'INVALID':
                        vehicle.lights.light_state._set_value(Lights.LightState.INVALID, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        LOG_API_DEBUG.info('Unknown light status %s', remote['lights']['overallStatus'])
                        vehicle.lights.light_state._set_value(Lights.LightState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                else:
                    vehicle.lights.light_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                if 'lightsStatus' in remote['lights'] and remote['lights']['lightsStatus'] is not None:
                    for light_status in remote['lights']['lightsStatus']:
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
                                    LOG_API_DEBUG.info('Unknown light status %s', light_status['status'])
                                    light.light_state._set_value(Lights.LightState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                            else:
                                light.light_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                        else:
                            raise APIError('Could not parse light, name missing')
                        log_extra_keys(LOG_API_DEBUG, 'lights', light_status, {'name', 'status'})
                    for light_to_remove in set(vehicle.lights.lights) - seen_light_ids:
                        vehicle.lights.lights[light_to_remove].enabled = False
                        vehicle.lights.lights.pop(light_to_remove)
                else:
                    vehicle.lights.lights = {}
                log_extra_keys(LOG_API_DEBUG, 'lights', remote['lights'], {'overallStatus', 'lightsStatus'})
            log_extra_keys(LOG_API_DEBUG, 'vehicles', remote,  {'capturedAt', 'mileageInKm', 'status', 'doors', 'windows', 'lights'})

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
                raise RetrievalError from connection_error
            except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
                raise RetrievalError from chunked_encoding_error
            except requests.exceptions.ReadTimeout as timeout_error:
                raise RetrievalError from timeout_error
            except requests.exceptions.RetryError as retry_error:
                raise RetrievalError from retry_error
            except requests.exceptions.JSONDecodeError as json_error:
                if allow_empty:
                    data = None
                else:
                    raise RetrievalError from json_error
        return data

    def get_version(self) -> str:
        return __version__
