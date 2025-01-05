"""Module implements the MQTT client."""
from __future__ import annotations
from typing import TYPE_CHECKING

import logging
import uuid
import ssl

from paho.mqtt.client import Client
from paho.mqtt.enums import MQTTProtocolVersion, CallbackAPIVersion, MQTTErrorCode

from carconnectivity.observable import Observable
from carconnectivity.vehicle import GenericVehicle


if TYPE_CHECKING:
    from typing import Set

    from carconnectivity_connectors.skoda.connector import Connector


LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.skoda.mqtt")


class SkodaMQTTClient(Client):  # pylint: disable=too-many-instance-attributes
    """
    MQTT client for the myskoda event push service.
    """
    def __init__(self, skoda_connector: Connector) -> None:
        super().__init__(callback_api_version=CallbackAPIVersion.VERSION2,
                         client_id="Id" + str(uuid.uuid4()) + "#" + str(uuid.uuid4()),
                         transport="tcp",
                         protocol=MQTTProtocolVersion.MQTTv311,
                         reconnect_on_failure=True,
                         clean_session=True)
        self._skoda_connector: Connector = skoda_connector

        self.username = 'android-app'

        self.on_pre_connect = self._on_pre_connect_callback
        self.on_connect = self._on_connect_callback
        self.on_message = self._on_message_callback
        self.on_disconnect = self._on_disconnect_callback
        self.on_subscribe = self._on_subscribe_callback
        self.subscribed_topics: Set[str] = set()

        self.tls_set(cert_reqs=ssl.CERT_NONE)

    def connect(self, *args, **kwargs) -> MQTTErrorCode:
        """
        Connects the MQTT client to the skoda server.

        Returns:
            MQTTErrorCode: The result of the connection attempt.
        """
        return super().connect(*args, host='mqtt.messagehub.de', port=8883, keepalive=60, **kwargs)

    def _on_pre_connect_callback(self, client, userdata) -> None:
        """
        Callback function that is called before the MQTT client connects to the broker.

        Sets the client's password to the access token.

        Args:
            client: The MQTT client instance (unused).
            userdata: The user data passed to the callback (unused).

        Returns:
            None
        """
        del client
        del userdata

        if self._skoda_connector.session.expired or self._skoda_connector.session.access_token is None:
            self._skoda_connector.session.refresh()
        if not self._skoda_connector.session.expired and self._skoda_connector.session.access_token is not None:
            # pylint: disable-next=attribute-defined-outside-init # this is a false positive, password has a setter in super class
            self._password = self._skoda_connector.session.access_token  # This is a bit hacky but if password attribute is used here there is an Exception

    def _on_carconnectivity_vehicle_enabled(self, element, flags):
        """
        Handles the event when a vehicle is enabled or disabled in the car connectivity system.

        This method is triggered when the state of a vehicle changes. It subscribes to the vehicle
        if it is enabled and unsubscribes if it is disabled.

        Args:
            element: The element whose state has changed.
            flags (Observable.ObserverEvent): The event flags indicating the state change.

        Returns:
            None
        """
        if (flags & Observable.ObserverEvent.ENABLED) and isinstance(element, GenericVehicle):
            self._subscribe_vehicle(element)
        elif (flags & Observable.ObserverEvent.DISABLED) and isinstance(element, GenericVehicle):
            self._subscribe_vehicle(element)

    def _subscribe_vehicles(self) -> None:
        """
        Subscribes to all vehicles the connector is responsible for.

        This method iterates through the list of vehicles in the carconnectivity
        garage and subscribes to eliable vehicles by calling the _subscribe_vehicle method.

        Returns:
            None
        """
        for vehicle in self._skoda_connector.car_connectivity.garage.list_vehicles():
            self._subscribe_vehicle(vehicle)

    def _unsubscribe_vehicles(self) -> None:
        """
        Unsubscribes from all vehicles the client is subscribed for.

        This method iterates through the list of vehicles in the garage and
        unsubscribes from each one by calling the _unsubscribe_vehicle method.

        Returns:
            None
        """
        for vehicle in self._skoda_connector.car_connectivity.garage.list_vehicles():
            self._unsubscribe_vehicle(vehicle)

    def _subscribe_vehicle(self, vehicle: GenericVehicle) -> None:
        """
        Subscribes to MQTT topics for a given vehicle.

        This method subscribes to various MQTT topics related to the vehicle's
        account events, operation requests, and service events. It ensures that
        the user ID is fetched if not already available and checks if the vehicle
        has a valid VIN before subscribing.

        Args:
            vehicle (GenericVehicle): The vehicle object containing VIN and other
                                      relevant information.

        Raises:
            None

        Logs:
            - Warnings if the vehicle does not have a VIN.
            - Info messages upon successful subscription to a topic.
            - Error messages if subscription to a topic fails.
        """
        # to subscribe the user_id must be known
        if self._skoda_connector.user_id is None:
            self._skoda_connector.fetch_user()
        # Can only subscribe with user_id
        if self._skoda_connector.user_id is not None:
            user_id: str = self._skoda_connector.user_id
            if not vehicle.vin.enabled or vehicle.vin.value is None:
                LOG.warning('Could not subscribe to vehicle without vin')
            else:
                vin: str = vehicle.vin.value
                # If the skoda connector is managing this vehicle
                if self._skoda_connector in vehicle.managing_connectors:
                    account_events: Set[str] = {'privacy'}
                    operation_requests: Set[str] = {
                        'air-conditioning/set-air-conditioning-at-unlock',
                        'air-conditioning/set-air-conditioning-seats-heating',
                        'air-conditioning/set-air-conditioning-timers',
                        'air-conditioning/set-air-conditioning-without-external-power',
                        'air-conditioning/set-target-temperature',
                        'air-conditioning/start-stop-air-conditioning',
                        'auxiliary-heating/start-stop-auxiliary-heating',
                        'air-conditioning/start-stop-window-heating',
                        'air-conditioning/windows-heating',
                        'charging/start-stop-charging',
                        'charging/update-battery-support',
                        'charging/update-auto-unlock-plug',
                        'charging/update-care-mode',
                        'charging/update-charge-limit',
                        'charging/update-charge-mode',
                        'charging/update-charging-profiles',
                        'charging/update-charging-current',
                        'departure/update-departure-timers',
                        'departure/update-minimal-soc',
                        'vehicle-access/honk-and-flash',
                        'vehicle-access/lock-vehicle',
                        'vehicle-services-backup/apply-backup',
                        'vehicle-wakeup/wakeup'
                    }
                    service_events: Set[str] = {
                        'air-conditioning',
                        'charging',
                        'departure',
                        'vehicle-status/access',
                        'vehicle-status/lights'
                    }
                    possible_topics: Set[str] = set()
                    # Compile all possible topics
                    for event in account_events:
                        possible_topics.add(f'{user_id}/{vin}/account-event/{event}')
                    for event in operation_requests:
                        possible_topics.add(f'{user_id}/{vin}/operation-request/{event}')
                    for event in service_events:
                        possible_topics.add(f'{user_id}/{vin}/service-event/{event}')

                    # Subscribe to all topics
                    for topic in possible_topics:
                        if topic not in self.subscribed_topics:
                            mqtt_err, mid = self.subscribe(topic)
                            if mqtt_err == MQTTErrorCode.MQTT_ERR_SUCCESS:
                                self.subscribed_topics.add(topic)
                                LOG.debug('Subscribe to topic %s with %d', topic, mid)
                            else:
                                LOG.error('Could not subscribe to topic %s (%s)', topic, mqtt_err)
        else:
            LOG.warning('Could not subscribe to vehicle without user_id')

    def _unsubscribe_vehicle(self, vehicle: GenericVehicle) -> None:
        """
        Unsubscribe from all MQTT topics related to a specific vehicle.

        This method checks if the vehicle's VIN (Vehicle Identification Number) is enabled and not None.
        If the VIN is valid, it iterates through the list of subscribed topics and unsubscribes from
        any topic that contains the VIN. It also removes the topic from the list of subscribed topics
        and logs the unsubscription.

        Args:
            vehicle (GenericVehicle): The vehicle object containing the VIN information.

        Raises:
            None

        Logs:
            - Warning if the vehicle's VIN is not enabled or is None.
            - Info for each topic successfully unsubscribed.
        """
        if not vehicle.vin.enabled or vehicle.vin.value is None:
            LOG.warning('Could not unsubscribe to vehicle without vin')
        else:
            vin: str = vehicle.vin.value
            for topic in self.subscribed_topics:
                if vin in topic:
                    self.unsubscribe(topic)
                    self.subscribed_topics.remove(topic)
                    LOG.debug('Unsubscribed from topic %s', topic)

    def _on_connect_callback(self, mqttc, obj, flags, reason_code, properties) -> None:
        """
        Callback function that is called when the MQTT client connects to the broker.

        It registers a callback to observe new vehicles being added and subscribes MQTT topics for all vehicles
        handled by this connector.

        Args:
            mqttc: The MQTT client instance (unused).
            obj: User-defined object passed to the callback (unused).
            flags: Response flags sent by the broker (unused).
            reason_code: The connection result code.
            properties: MQTT v5 properties (unused).

        Returns:
            None

        The function logs the connection status and handles different reason codes:
            - 0: Connection successful.
            - 128: Unspecified error.
            - 129: Malformed packet.
            - 130: Protocol error.
            - 131: Implementation specific error.
            - 132: Unsupported protocol version.
            - 133: Client identifier not valid.
            - 134: Bad user name or password.
            - 135: Not authorized.
            - 136: Server unavailable.
            - 137: Server busy. Retrying.
            - 138: Banned.
            - 140: Bad authentication method.
            - 144: Topic name invalid.
            - 149: Packet too large.
            - 151: Quota exceeded.
            - 154: Retain not supported.
            - 155: QoS not supported.
            - 156: Use another server.
            - 157: Server move.
            - 159: Connection rate exceeded.
            - Other: Generic connection error.
        """
        del mqttc  # unused
        del obj  # unused
        del flags  # unused
        del properties
        # reason_code 0 means success
        if reason_code == 0:
            LOG.info('Connected to Skoda MQTT server')
            self._skoda_connector.connected._set_value(value=True)  # pylint: disable=protected-access
            observer_flags: Observable.ObserverEvent = Observable.ObserverEvent.ENABLED | Observable.ObserverEvent.DISABLED
            self._skoda_connector.car_connectivity.garage.add_observer(observer=self._on_carconnectivity_vehicle_enabled,
                                                                       flag=observer_flags,
                                                                       priority=Observable.ObserverPriority.USER_MID)
            self._subscribe_vehicles()

        # Handle different reason codes
        elif reason_code == 128:
            LOG.error('Could not connect (%s): Unspecified error', reason_code)
        elif reason_code == 129:
            LOG.error('Could not connect (%s): Malformed packet', reason_code)
        elif reason_code == 130:
            LOG.error('Could not connect (%s): Protocol error', reason_code)
        elif reason_code == 131:
            LOG.error('Could not connect (%s): Implementation specific error', reason_code)
        elif reason_code == 132:
            LOG.error('Could not connect (%s): Unsupported protocol version', reason_code)
        elif reason_code == 133:
            LOG.error('Could not connect (%s): Client identifier not valid', reason_code)
        elif reason_code == 134:
            LOG.error('Could not connect (%s): Bad user name or password', reason_code)
        elif reason_code == 135:
            LOG.error('Could not connect (%s): Not authorized', reason_code)
        elif reason_code == 136:
            LOG.error('Could not connect (%s): Server unavailable', reason_code)
        elif reason_code == 137:
            LOG.error('Could not connect (%s): Server busy. Retrying', reason_code)
        elif reason_code == 138:
            LOG.error('Could not connect (%s): Banned', reason_code)
        elif reason_code == 140:
            LOG.error('Could not connect (%s): Bad authentication method', reason_code)
        elif reason_code == 144:
            LOG.error('Could not connect (%s): Topic name invalid', reason_code)
        elif reason_code == 149:
            LOG.error('Could not connect (%s): Packet too large', reason_code)
        elif reason_code == 151:
            LOG.error('Could not connect (%s): Quota exceeded', reason_code)
        elif reason_code == 154:
            LOG.error('Could not connect (%s): Retain not supported', reason_code)
        elif reason_code == 155:
            LOG.error('Could not connect (%s): QoS not supported', reason_code)
        elif reason_code == 156:
            LOG.error('Could not connect (%s): Use another server', reason_code)
        elif reason_code == 157:
            LOG.error('Could not connect (%s): Server move', reason_code)
        elif reason_code == 159:
            LOG.error('Could not connect (%s): Connection rate exceeded', reason_code)
        else:
            LOG.error('Could not connect (%s)', reason_code)

    def _on_disconnect_callback(self, client, userdata, flags, reason_code, properties) -> None:
        """
        Callback function that is called when the MQTT client disconnects.

        This function handles the disconnection of the MQTT client and logs the appropriate
        messages based on the reason code for the disconnection. It also removes the observer
        from the garage to not get any notifications for vehicles being added or removed.

        Args:
            client: The MQTT client instance that disconnected.
            userdata: The private user data as set in Client() or userdata_set().
            flags: Response flags sent by the broker.
            reason_code: The reason code for the disconnection.
            properties: The properties associated with the disconnection.

        Returns:
            None
        """
        del client
        del properties
        del flags

        self._skoda_connector.connected._set_value(value=False)  # pylint: disable=protected-access
        self._skoda_connector.car_connectivity.garage.remove_observer(observer=self._on_carconnectivity_vehicle_enabled)

        if reason_code == 0:
            LOG.info('Client successfully disconnected')
        elif reason_code == 4:
            LOG.info('Client successfully disconnected: %s', userdata)
        elif reason_code == 137:
            LOG.error('Client disconnected: Server busy')
        elif reason_code == 139:
            LOG.error('Client disconnected: Server shutting down')
        elif reason_code == 160:
            LOG.error('Client disconnected: Maximum connect time')
        else:
            LOG.error('Client unexpectedly disconnected (%s), trying to reconnect', reason_code)

    def _on_subscribe_callback(self, mqttc, obj, mid, reason_codes, properties) -> None:
        """
        Callback function for MQTT subscription.

        This method is called when the client receives a SUBACK response from the server.
        It checks the reason codes to determine if the subscription was successful.

        Args:
            mqttc: The MQTT client instance (unused).
            obj: User-defined data of any type (unused).
            mid: The message ID of the subscribe request.
            reason_codes: A list of reason codes indicating the result of the subscription.
            properties: MQTT v5.0 properties (unused).

        Returns:
            None
        """
        del mqttc  # unused
        del obj  # unused
        del properties  # unused
        if any(x in [0, 1, 2] for x in reason_codes):
            LOG.debug('sucessfully subscribed to topic of mid %d', mid)
        else:
            LOG.error('Subscribe was not successfull (%s)', ', '.join(reason_codes))

    def _on_message_callback(self, mqttc, obj, msg) -> None:  # noqa: C901
        """
        Callback function for handling incoming MQTT messages.

        This function is called when a message is received on a subscribed topic.
        It logs an error message indicating that the message is not understood.
        In the next step this needs to be implemented with real behaviour.

        Args:
            mqttc: The MQTT client instance (unused).
            obj: The user data (unused).
            msg: The MQTT message instance containing topic and payload.

        Returns:
            None
        """
        del mqttc  # unused
        del obj  # unused
        error_message = f'I don\'t understand message {msg.topic}: {msg.payload}'
        LOG.info(error_message)