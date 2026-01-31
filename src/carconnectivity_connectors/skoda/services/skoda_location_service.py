"""Module containing the Skoda location service."""
# pylint: disable=duplicate-code
from __future__ import annotations
from typing import TYPE_CHECKING, Dict

import json
import requests

from carconnectivity_services.base.service import ServiceType
from carconnectivity_services.location.location_service import LocationService
from carconnectivity.location import Location
from carconnectivity.charging_station import ChargingStation

if TYPE_CHECKING:
    from typing import Optional, Any

    import logging

    from carconnectivity.carconnectivity import CarConnectivity
    from carconnectivity_connectors.skoda.connector import Connector


class SkodaLocationService(LocationService):  # pylint: disable=too-few-public-methods, too-many-instance-attributes
    def __init__(self, service_id: str, car_connectivity: CarConnectivity, log: logging.Logger, connector: Connector) -> None:
        super().__init__(service_id, car_connectivity, log)
        self.connector: Connector = connector

    def get_types(self) -> list[tuple[ServiceType, int]]:
        return [(ServiceType.LOCATION_REVERSE, 200), (ServiceType.LOCATION_CHARGING_STATION, 100), (ServiceType.LOCATION_GAS_STATION, 100)]

    def location_from_lat_lon(self, latitude: float, longitude: float, location: Optional[Location]) -> Optional[Location]:
        url: str = f'https://mysmob.api.connect.skoda-auto.cz/api/v3/maps/places?latitude={latitude}&longitude={longitude}&searchRadius=0'
        data: Dict[str, Any] | None = self.connector._fetch_data(url, session=self.connector.session)  # pylint: disable=protected-access
        if data is not None:
            if 'id' in data and 'address' in data:
                if location is None:
                    location = Location(name=str(data['id']), parent=None)
                location.uid._set_value(value=str(data['id']))  # pylint: disable=protected-access
                location.source._set_value(value='Skoda')  # pylint: disable=protected-access
                address: dict = data['address']
                if 'formattedAddress' in address:
                    location.display_name._set_value(value=str(address['formattedAddress']))  # pylint: disable=protected-access
                if 'houseNumber' in address:
                    location.house_number._set_value(value=str(address['houseNumber']))  # pylint: disable=protected-access
                if 'street' in address:
                    location.road._set_value(value=str(address['street']))  # pylint: disable=protected-access
                if 'city' in address:
                    location.city._set_value(value=str(address['city']))  # pylint: disable=protected-access
                if 'zipCode' in address:
                    location.postcode._set_value(value=str(address['zipCode']))  # pylint: disable=protected-access
                if 'country' in address:
                    location.country._set_value(value=str(address['country']))  # pylint: disable=protected-access
                location.raw._set_value(value=json.dumps(data))  # pylint: disable=protected-access
                return location
        return None

    def charging_station_from_lat_lon(self, latitude: float, longitude: float, radius: int,  # pylint: disable=too-many-branches,too-many-statements
                                      charging_station: Optional[ChargingStation] = None) -> Optional[ChargingStation]:
        url: str = 'https://mysmob.api.connect.skoda-auto.cz/api/v3/maps/nearby-places'
        data = {
            "placeTypes": ["CHARGING_STATION"], #GAS_STATION, "CHARGING_STATION" "LOCATION"
            "location": {"latitude": latitude, "longitude": longitude},
            "radiusInMeters": radius,
            "requirements" : {},
        }
        #{"nearbyPlaces":[{"id":"b2044fd8-8d14-441f-96bf-74f75be9765e","name":"Wirelane Am Stühm Süd 69","placeType":"CHARGING_STATION","location":{"latitude":53.624472,"longitude":10.098722},"address":{"city":"Hamburg","country":"Deutschland","countryCode":"de","street":"Am Stühm-Süd","houseNumber":"69","zipCode":"22175","formattedAddress":"Am Stühm-Süd 69, 22175 Hamburg, Deutschland"},"chargingStation":{"maxElectricPowerInKw":22.0,"totalCountChargingPoints":2,"availableCountChargingPoints":2,"currentType":"AC","vwGroupPartners":["BOCN","WE_CHARGE","PCS"]}}]}
        status_response: requests.Response = self.connector.session.post(url, allow_redirects=False, data=json.dumps(data))
        try:
            data = status_response.json()
            if data is not None and 'nearbyPlaces' in data:
                for place in data['nearbyPlaces']:
                    if 'location' in place and 'latitude' in place['location'] and 'longitude' in place['location']:
                        lat_diff = place['location']['latitude'] - latitude
                        lon_diff = place['location']['longitude'] - longitude
                        distance = (lat_diff**2 + lon_diff**2)**0.5 * 111000  # Approximate conversion to meters
                        place['distance'] = distance
                    else:
                        place['distance'] = float('inf')
                sorted_places = sorted(data['nearbyPlaces'], key=lambda x: x['distance'])
                if sorted_places:
                    closest_place = sorted_places[0]
                    if 'id' in closest_place:
                        if charging_station is None:
                            charging_station = ChargingStation(name=str(closest_place['id']), parent=None)
                        charging_station.uid._set_value(closest_place['id'])  # pylint: disable=protected-access
                        charging_station.source._set_value('Skoda')  # pylint: disable=protected-access
                        if 'name' in closest_place:
                            charging_station.name._set_value(closest_place['name'])  # pylint: disable=protected-access
                        if 'location' in closest_place:
                            charging_station.latitude._set_value(closest_place['location']['latitude'])  # pylint: disable=protected-access
                            charging_station.longitude._set_value(closest_place['location']['longitude'])  # pylint: disable=protected-access
                        if 'address' in closest_place:
                            address = closest_place['address']
                            address_parts = []
                            if 'street' in address:
                                address_parts.append(address['street'])
                            if 'houseNumber' in address:
                                address_parts[-1] += f" {address['houseNumber']}"
                            if 'zipCode' in address:
                                address_parts.append(address['zipCode'])
                            if 'city' in address:
                                address_parts.append(address['city'])
                            if 'country' in address:
                                address_parts.append(address['country'])
                            full_address = ', '.join(address_parts)
                            charging_station.address._set_value(full_address)  # pylint: disable=protected-access
                        if 'chargingStation' in closest_place:
                            charging_info = closest_place['chargingStation']
                            if 'totalCountChargingPoints' in charging_info:
                                charging_station.num_spots._set_value(charging_info['totalCountChargingPoints'])  # pylint: disable=protected-access
                            if 'maxElectricPowerInKw' in charging_info:
                                charging_station.max_power._set_value(charging_info['maxElectricPowerInKw'])  # pylint: disable=protected-access
                        detail_url = f"https://mysmob.api.connect.skoda-auto.cz/api/v3/maps/places/{closest_place['id']}?type=CHARGING_STATION"
                        detail_response: requests.Response = self.connector.session.get(detail_url, allow_redirects=False)
                        try:
                            detail_data = detail_response.json()
                            if 'contact' in detail_data and 'website' in detail_data['contact']:
                                charging_station.operator_id._set_value(detail_data['contact']['website'])  # pylint: disable=protected-access
                                charging_station.operator_name._set_value(detail_data['contact']['website'])  # pylint: disable=protected-access
                        except requests.exceptions.JSONDecodeError as json_error:
                            self.log.error(f"Error decoding JSON response from Skoda API for charging station details: {json_error}")
                        return charging_station
        except requests.exceptions.ConnectionError as connection_error:
            self.log.error(f'Connection error: {connection_error}.'
                           ' If this happens frequently, please check if other applications communicate with the Skoda server.')
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            self.log.error(f'Error: {chunked_encoding_error}')
        except requests.exceptions.ReadTimeout as timeout_error:
            self.log.error(f'Timeout during read: {timeout_error}')
        except requests.exceptions.RetryError as retry_error:
            self.log.error(f'Retrying failed: {retry_error}')
        except requests.exceptions.JSONDecodeError as json_error:
            self.log.error(f"Error decoding JSON response from Skoda API: {json_error}")
            return None
        return None

    def gas_station_from_lat_lon(self, latitude: float, longitude: float, radius: int, location: Location | None) -> Location | None:
        url: str = 'https://mysmob.api.connect.skoda-auto.cz/api/v3/maps/nearby-places'
        data = {
            "placeTypes": ["GAS_STATION"], #GAS_STATION, "CHARGING_STATION" "LOCATION"
            "location": {"latitude": latitude, "longitude": longitude},
            "radiusInMeters": radius,
            "requirements": {},
        }
        status_response: requests.Response = self.connector.session.post(url, allow_redirects=False, data=json.dumps(data))
        try:
            data = status_response.json()
            if data is not None and 'nearbyPlaces' in data:
                for place in data['nearbyPlaces']:
                    if 'location' in place and 'latitude' in place['location'] and 'longitude' in place['location']:
                        lat_diff = place['location']['latitude'] - latitude
                        lon_diff = place['location']['longitude'] - longitude
                        distance = (lat_diff**2 + lon_diff**2)**0.5 * 111000  # Approximate conversion to meters
                        place['distance'] = distance
                    else:
                        place['distance'] = float('inf')
                sorted_places = sorted(data['nearbyPlaces'], key=lambda x: x['distance'])
                if sorted_places:
                    closest_place = sorted_places[0]
                    if 'id' in closest_place:
                        if location is None:
                            location = Location(name=str(closest_place['id']), parent=None)
                        location.uid._set_value(value=closest_place['id'])  # pylint: disable=protected-access
                        location.source._set_value(value='Skoda')  # pylint: disable=protected-access
                        if 'name' in closest_place:
                            location.name._set_value(value=closest_place['name'])  # pylint: disable=protected-access
                        if 'address' in closest_place:
                            address = closest_place['address']
                            if 'formattedAddress' in address:
                                display_name: str = ''
                                if 'name' in closest_place:
                                    display_name = closest_place['name'] + ', '
                                location.display_name._set_value(value=display_name + str(address['formattedAddress']))  # pylint: disable=protected-access
                            if 'houseNumber' in address:
                                location.house_number._set_value(value=str(address['houseNumber']))  # pylint: disable=protected-access
                            if 'street' in address:
                                location.road._set_value(value=str(address['street']))  # pylint: disable=protected-access
                            if 'city' in address:
                                location.city._set_value(value=str(address['city']))  # pylint: disable=protected-access
                            if 'zipCode' in address:
                                location.postcode._set_value(value=str(address['zipCode']))  # pylint: disable=protected-access
                            if 'country' in address:
                                location.country._set_value(value=str(address['country']))  # pylint: disable=protected-access

                        location.raw._set_value(value=json.dumps(closest_place))  # pylint: disable=protected-access
                        return location
        except requests.exceptions.ConnectionError as connection_error:
            self.log.error(f'Connection error: {connection_error}.'
                           ' If this happens frequently, please check if other applications communicate with the Skoda server.')
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            self.log.error(f'Error: {chunked_encoding_error}')
        except requests.exceptions.ReadTimeout as timeout_error:
            self.log.error(f'Timeout during read: {timeout_error}')
        except requests.exceptions.RetryError as retry_error:
            self.log.error(f'Retrying failed: {retry_error}')
        except requests.exceptions.JSONDecodeError as json_error:
            self.log.error(f"Error decoding JSON response from Skoda API: {json_error}")
            return None
        return None
