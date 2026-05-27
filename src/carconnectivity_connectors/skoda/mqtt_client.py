"""Module implements the MQTT client."""
from __future__ import annotations
from typing import TYPE_CHECKING

import re
import logging
import uuid
import ssl
import json
import locale
import threading
import hashlib
import hmac
import struct
import asyncio
from datetime import timedelta, timezone, datetime

import aiohttp
import requests
from base64 import urlsafe_b64encode
from firebase_messaging import FcmRegisterConfig
from firebase_messaging.fcmregister import FcmRegister
from firebase_messaging.proto.android_checkin_pb2 import (
    AndroidCheckinProto,
    ChromeBuildProto,
    DEVICE_ANDROID_OS,
)
from firebase_messaging.proto.checkin_pb2 import AndroidCheckinRequest

from paho.mqtt.client import Client
from paho.mqtt.enums import MQTTProtocolVersion, CallbackAPIVersion, MQTTErrorCode
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from carconnectivity.errors import CarConnectivityError, TemporaryAuthenticationError
from carconnectivity.observable import Observable
from carconnectivity.vehicle import GenericVehicle

from carconnectivity.drive import ElectricDrive
from carconnectivity.util import robust_time_parse, log_extra_keys
from carconnectivity.charging import Charging
from carconnectivity.climatization import Climatization
from carconnectivity.units import Speed, Power, Length
from carconnectivity.enums import ConnectionState

from carconnectivity_connectors.skoda.vehicle import SkodaVehicle, SkodaElectricVehicle
from carconnectivity_connectors.skoda.charging import SkodaCharging, mapping_skoda_charging_state


if TYPE_CHECKING:
    from typing import Set, Dict, Any, Optional, List

    from paho.mqtt.client import MQTTMessage, DisconnectFlags, ConnectFlags
    from paho.mqtt.reasoncodes import ReasonCode

    from carconnectivity.attributes import GenericAttribute

    from carconnectivity_connectors.skoda.connector import Connector


FIREBASE_PROJECT_ID: str = "678067506455"
FIREBASE_APP_ID: str = "1:678067506455:android:4afca86c91d6d4c235bb52"
FIREBASE_API_KEY: str = "AIzaSyBlJdDfVR6ltRhKpA87F3SmCe2hHqhyEd8"
FIREBASE_SENDER_ID: str = "678067506455"
FIREBASE_ANDROID_PACKAGE: str = "cz.skodaauto.myskoda"
FIREBASE_ANDROID_CERT: str = "E567A2E2E6C5E889CDB37EF07EBEC1576C196325"
MYSKODA_APP_VERSION: str = "8.12.0"
MYSKODA_APP_VERSION_CODE: str = "260430001"
FIREBASE_ANDROID_FCM_CLIENT_VERSION: str = "fcm-25.0.1"
FIREBASE_ANDROID_SDK_VERSION: str = "a:19.0.1"
FIREBASE_ANDROID_OS_VERSION: str = "34"
MQTT_SESSION_EXPIRY_INTERVAL_SECONDS: int = 86400
NOTIFICATIONS_SUBSCRIPTIONS_URL: str = "https://mysmob.api.connect.skoda-auto.cz/api/v1/notifications-subscriptions/"
FCM_CREDENTIALS_KEY: str = "CarConnectivity-connector-skoda:fcm_credentials"
APP_INSTALLATION_ID_KEY: str = "CarConnectivity-connector-skoda:app_installation_id"


LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.skoda.mqtt")
LOG_API: logging.Logger = logging.getLogger("carconnectivity.connectors.skoda-api-debug")


class SkodaFcmRegister(FcmRegister):
    """FcmRegister variant that identifies as the MySkoda Android app."""

    def _get_checkin_payload(
        self, android_id: int | None = None, security_token: int | None = None
    ) -> AndroidCheckinRequest:
        """Build a GCM checkin payload that identifies as an Android device."""
        chrome = ChromeBuildProto()
        chrome.platform = ChromeBuildProto.Platform.PLATFORM_ANDROID
        chrome.chrome_version = MYSKODA_APP_VERSION
        chrome.channel = ChromeBuildProto.Channel.CHANNEL_STABLE

        checkin = AndroidCheckinProto()
        checkin.type = DEVICE_ANDROID_OS
        checkin.chrome_build.CopyFrom(chrome)

        payload = AndroidCheckinRequest()
        payload.user_serial_number = 0
        payload.checkin.CopyFrom(checkin)
        payload.version = 3
        if android_id is not None and security_token is not None:
            payload.id = int(android_id)
            payload.security_token = int(security_token)
        return payload

    async def gcm_register(
        self,
        options: dict,
        retries: int = 2,
    ) -> dict | None:
        """Register with GCM using Skoda Android app identity instead of Chrome defaults."""
        android_id = options["androidId"]
        security_token = options["securityToken"]

        token = await self._post_gcm_form(
            headers={
                "Authorization": f"AidLogin {android_id}:{security_token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body={
                "app": FIREBASE_ANDROID_PACKAGE,
                "X-subtype": FIREBASE_SENDER_ID,
                "device": android_id,
                "sender": FIREBASE_SENDER_ID,
                "X-scope": "*",
            },
            label="GCM register",
            retries=retries,
        )
        if token is None:
            return None
        return {
            "token": token,
            "app_id": FIREBASE_SENDER_ID,
            "android_id": android_id,
            "security_token": security_token,
        }

    async def _post_gcm_form(self, headers: dict[str, Any], body: dict[str, Any], label: str, retries: int) -> str | None:
        """POST a GCM register form and return its token value."""
        from urllib.parse import parse_qs  # pylint: disable=import-outside-toplevel
        from firebase_messaging.const import GCM_REGISTER_URL  # pylint: disable=import-outside-toplevel

        last_error: str | Exception | None = None
        for try_num in range(retries):
            try:
                async with self._session.post(
                    url=GCM_REGISTER_URL,
                    headers=headers,
                    data=body,
                    timeout=self.CLIENT_TIMEOUT,
                ) as resp:
                    response_text = await resp.text()
                    status = resp.status
                if status != 200 or "Error" in response_text:
                    last_error = response_text
                    LOG.warning("%s attempt %d/%d failed: HTTP %s %s",
                                label, try_num + 1, retries, status, response_text[:160])
                else:
                    token = parse_qs(response_text).get("token", [None])[0]
                    if token:
                        return token
                    last_error = response_text
                    LOG.warning("%s attempt %d/%d returned no token: %s",
                                label, try_num + 1, retries, response_text[:160])
            except Exception as exc:  # pylint: disable=broad-except
                last_error = exc
                LOG.warning("%s attempt %d/%d exception: %s", label, try_num + 1, retries, exc)
            await asyncio.sleep(1)

        LOG.error("%s failed after %d tries: %s", label, retries, last_error)
        return None

    async def _android_fcm_register(
        self,
        android_id: int,
        security_token: int,
        installation: dict[str, Any],
        retries: int = 2,
    ) -> dict[str, Any] | None:
        """Request an Android FCM token instead of the library's web-push registration."""
        token = await self._post_gcm_form(
            headers={
                "Authorization": f"AidLogin {android_id}:{security_token}",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Android-Package": FIREBASE_ANDROID_PACKAGE,
                "X-Android-Cert": FIREBASE_ANDROID_CERT,
                "x-goog-firebase-installations-auth": installation["token"],
            },
            body={
                "app": FIREBASE_ANDROID_PACKAGE,
                "appid": installation["fid"],
                "sender": FIREBASE_SENDER_ID,
                "subtype": FIREBASE_SENDER_ID,
                "scope": "*",
                "X-subtype": FIREBASE_SENDER_ID,
                "X-scope": "*",
                "gmp_app_id": FIREBASE_APP_ID,
                "app_ver": MYSKODA_APP_VERSION_CODE,
                "app_ver_name": MYSKODA_APP_VERSION,
                "osv": FIREBASE_ANDROID_OS_VERSION,
                "cliv": FIREBASE_ANDROID_FCM_CLIENT_VERSION,
                "Goog-Firebase-Installations-Auth": installation["token"],
                "device": android_id,
            },
            label="Android FCM register",
            retries=retries,
        )
        return {"token": token} if token else None

    async def fcm_refresh_install_token(self) -> dict | None:
        """Refresh the FIS auth token using Android headers and Android SDK version."""
        from firebase_messaging.const import FCM_INSTALLATION, AUTH_VERSION  # pylint: disable=import-outside-toplevel

        if not self.credentials:
            raise RuntimeError("Credentials must be set to refresh install token")

        installation = self.credentials["fcm"]["installation"]
        hb_header = urlsafe_b64encode(
            json.dumps({"heartbeats": [], "version": 2}).encode()
        ).decode()
        headers = {
            "Authorization": f"{AUTH_VERSION} {installation['refresh_token']}",
            "x-firebase-client": hb_header,
            "x-goog-api-key": self.config.api_key,
            "X-Android-Package": FIREBASE_ANDROID_PACKAGE,
            "X-Android-Cert": FIREBASE_ANDROID_CERT,
            "Cache-Control": "no-cache",
        }
        payload = {
            "installation": {
                "sdkVersion": FIREBASE_ANDROID_SDK_VERSION,
                "appId": self.config.app_id,
            }
        }
        url = (
            FCM_INSTALLATION + f"projects/{self.config.project_id}/"
            f"installations/{installation['fid']}/authTokens:generate"
        )
        async with self._session.post(
            url=url,
            headers=headers,
            json=payload,
            timeout=self.CLIENT_TIMEOUT,
        ) as resp:
            if resp.status == 200:
                refreshed = await resp.json()
                return {
                    "token": refreshed["token"],
                    "expires_in": int(refreshed["expiresIn"][:-1:]),
                    "created_at": asyncio.get_running_loop().time(),
                }
            text = await resp.text()
            LOG.error("FIS auth token refresh failed: %s", text)
            return None

    async def register(self) -> dict:
        """Register using Android-like GCM/FIS/FCM steps instead of web push."""
        checkin_data = await self.gcm_check_in()
        if checkin_data is None:
            raise RuntimeError(
                "Unable to establish subscription with Google Cloud Messaging."
            )
        gcm_data = {
            "android_id": checkin_data["androidId"],
            "security_token": checkin_data["securityToken"],
        }

        installation = await self.fcm_install()
        if not installation:
            raise RuntimeError("Unable to register with Firebase Installations")

        registration = await self._android_fcm_register(
            android_id=gcm_data["android_id"],
            security_token=gcm_data["security_token"],
            installation=installation,
        )
        if not registration:
            raise RuntimeError("Unable to register Android token with FCM")

        res: dict[str, Any] = {
            "gcm": gcm_data,
            "fcm": {
                "registration": registration,
                "installation": installation,
            },
            "config": {
                "bundle_id": self.config.bundle_id,
                "project_id": self.config.project_id,
            },
        }
        LOG.info("Registered with Android FCM flow")
        return res

    async def checkin_or_register(self) -> dict[str, Any]:
        """Reuse Android credentials, upgrade legacy web credentials, otherwise register."""
        if self.credentials:
            gcm_data = await self.gcm_check_in(
                self.credentials["gcm"]["android_id"],
                self.credentials["gcm"]["security_token"],
            )
            if gcm_data:
                registration: dict[str, Any] = self.credentials.get("fcm", {}).get("registration", {})
                if "web" in registration:
                    LOG.info("Attempting to upgrade cached legacy web FCM registration")
                    installation = self.credentials.get("fcm", {}).get("installation")
                    if installation and installation.get("refresh_token") and installation.get("fid"):
                        refreshed_installation = await self.fcm_refresh_install_token()
                        if refreshed_installation:
                            installation = {**installation, **refreshed_installation}
                    if not installation or not installation.get("token"):
                        installation = await self.fcm_install()
                    if installation:
                        android_registration = await self._android_fcm_register(
                            android_id=self.credentials["gcm"]["android_id"],
                            security_token=self.credentials["gcm"]["security_token"],
                            installation=installation,
                        )
                        if android_registration:
                            self.credentials["fcm"] = {
                                "registration": android_registration,
                                "installation": installation,
                            }
                            self.credentials["config"] = {
                                "bundle_id": self.config.bundle_id,
                                "project_id": self.config.project_id,
                            }
                            if self.credentials_updated_callback:
                                self.credentials_updated_callback(self.credentials)
                            return self.credentials
                    raise RuntimeError("Unable to upgrade legacy web FCM registration to Android")
                return self.credentials

        self.credentials = await self.register()
        if self.credentials_updated_callback:
            self.credentials_updated_callback(self.credentials)
        return self.credentials

    async def fcm_install(self) -> dict | None:
        """Create a Firebase Installation using Android SDK version and explicit Android headers."""
        import secrets  # pylint: disable=import-outside-toplevel
        import time  # pylint: disable=import-outside-toplevel
        from firebase_messaging.const import FCM_INSTALLATION, AUTH_VERSION  # pylint: disable=import-outside-toplevel

        fid = bytearray(secrets.token_bytes(17))
        fid[0] = 0b01110000 + (fid[0] % 0b00010000)
        fid64 = urlsafe_b64encode(fid).decode().rstrip("=")

        hb_header = urlsafe_b64encode(
            json.dumps({"heartbeats": [], "version": 2}).encode()
        ).decode()

        headers = {
            "x-firebase-client": hb_header,
            "x-goog-api-key": self.config.api_key,
            "X-Android-Package": FIREBASE_ANDROID_PACKAGE,
            "X-Android-Cert": FIREBASE_ANDROID_CERT,
            "Cache-Control": "no-cache",
        }
        payload = {
            "appId": self.config.app_id,
            "authVersion": AUTH_VERSION,
            "fid": fid64,
            "sdkVersion": FIREBASE_ANDROID_SDK_VERSION,
        }
        url = FCM_INSTALLATION + f"projects/{self.config.project_id}/installations"
        async with self._session.post(
            url=url,
            headers=headers,
            json=payload,
            timeout=self.CLIENT_TIMEOUT,
        ) as resp:
            if resp.status == 200:
                fcm_install = await resp.json()
                return {
                    "token": fcm_install["authToken"]["token"],
                    "expires_in": int(fcm_install["authToken"]["expiresIn"][:-1:]),
                    "refresh_token": fcm_install["refreshToken"],
                    "fid": fcm_install["fid"],
                    "created_at": time.monotonic(),
                }
            text = await resp.text()
            LOG.error("FIS install failed: %s", text)
            return None


class SkodaMQTTClient(Client):  # pylint: disable=too-many-instance-attributes
    """
    MQTT client for the myskoda event push service.
    """
    def __init__(self, skoda_connector: Connector) -> None:
        self._skoda_connector: Connector = skoda_connector
        self._app_installation_id: str = self._get_app_installation_id(skoda_connector)
        super().__init__(callback_api_version=CallbackAPIVersion.VERSION2,
                         client_id=f"{self._app_installation_id}#{uuid.uuid4()}",
                         transport="tcp",
                         protocol=MQTTProtocolVersion.MQTTv5,
                         reconnect_on_failure=True)

        self.on_pre_connect = self._on_pre_connect_callback
        self.on_connect = self._on_connect_callback
        self.on_message = self._on_message_callback
        self.on_disconnect = self._on_disconnect_callback
        self.on_subscribe = self._on_subscribe_callback
        self.subscribed_topics: Set[str] = set()

        self.delayed_access_function_timers: Dict[str, threading.Timer] = {}

        self.tls_set(cert_reqs=ssl.CERT_NONE)

        self._retry_refresh_login_once = True
        self._fcm_token: Optional[str] = None
        self._fcm_token_registered: bool = False

        # Start fetching the FCM token in the background so connect() stays fast.
        self._fcm_token_event: threading.Event = threading.Event()
        threading.Thread(target=self._prefetch_fcm_token, daemon=True,
                         name='skoda-fcm-prefetch').start()

    @staticmethod
    def _get_app_installation_id(skoda_connector: Connector) -> str:
        """Return a stable app installation id matching the Android MQTT client id format."""
        tokenstore: Dict[str, Any] = skoda_connector._manager.tokenstore  # pylint: disable=protected-access
        app_installation_id: Optional[str] = tokenstore.get(APP_INSTALLATION_ID_KEY)
        if app_installation_id is None:
            app_installation_id = str(uuid.uuid4())
            tokenstore[APP_INSTALLATION_ID_KEY] = app_installation_id
            skoda_connector.car_connectivity.persist()
        return app_installation_id

    @staticmethod
    def _get_device_locale() -> tuple[str, str]:
        """Return language and country values close to Android's default locale fields."""
        locale_name = locale.getlocale()[0] or "en_US"
        language, _, country = locale_name.partition("_")
        return language or "en", country or "US"

    def _prefetch_fcm_token(self) -> None:
        """Fetch the FCM token in a daemon thread so connect() is not blocked."""
        try:
            self._fcm_token = self._get_fcm_token()
            LOG.debug('Successfully obtained FCM token for MQTT authentication')
        except Exception as exc:  # pylint: disable=broad-except
            LOG.error('Could not obtain FCM token for MQTT authentication: %s', exc)
            LOG.warning('MQTT connection will likely fail without a valid FCM token; TOTP authentication will be skipped')
        finally:
            self._fcm_token_event.set()

    async def _async_get_fcm_token(self) -> str:
        """Get an Android FCM token from Firebase asynchronously."""
        tokenstore: Dict[str, Any] = self._skoda_connector._manager.tokenstore  # pylint: disable=protected-access
        existing_credentials: Optional[Dict[str, Any]] = tokenstore.get(FCM_CREDENTIALS_KEY)
        if existing_credentials is not None:
            configured_bundle_id: Optional[str] = existing_credentials.get("config", {}).get("bundle_id")
            if configured_bundle_id != FIREBASE_ANDROID_PACKAGE:
                LOG.info("Cached FCM credentials use bundle id %s; attempting Android upgrade", configured_bundle_id)

        fcm_config: FcmRegisterConfig = FcmRegisterConfig(
            FIREBASE_PROJECT_ID,
            FIREBASE_APP_ID,
            FIREBASE_API_KEY,
            FIREBASE_SENDER_ID,
            FIREBASE_ANDROID_PACKAGE,
        )
        async with aiohttp.ClientSession(headers={
            "X-Android-Package": FIREBASE_ANDROID_PACKAGE,
            "X-Android-Cert": FIREBASE_ANDROID_CERT,
        }) as firebase_session:
            register: SkodaFcmRegister = SkodaFcmRegister(
                fcm_config,
                existing_credentials,
                self._on_fcm_credentials_updated,
                http_client_session=firebase_session,
            )
            try:
                credentials = await register.checkin_or_register()
            finally:
                await register.close()
            token = credentials.get("fcm", {}).get("registration", {}).get("token") if credentials else None
            if not token:
                raise CarConnectivityError("FCM registration did not return a valid token")
            return token

    def _on_fcm_credentials_updated(self, credentials: Dict[str, Any]) -> None:
        """Save updated FCM credentials to the tokenstore so they survive restarts."""
        tokenstore: Dict[str, Any] = self._skoda_connector._manager.tokenstore  # pylint: disable=protected-access
        tokenstore[FCM_CREDENTIALS_KEY] = credentials
        self._skoda_connector.car_connectivity.persist()
        LOG.debug('FCM credentials updated and saved to tokenstore')

    def _get_fcm_token(self) -> str:
        """Get an FCM token from Firebase."""
        return asyncio.run(self._async_get_fcm_token())

    def _register_fcm_token_with_skoda(self, fcm_token: str) -> None:
        """Register the FCM token with Skoda's notifications API."""
        url: str = f'{NOTIFICATIONS_SUBSCRIPTIONS_URL}{fcm_token}'
        language, country = self._get_device_locale()
        try:
            response: requests.Response = self._skoda_connector.session.put(
                url,
                data=json.dumps({
                    "devicePlatform": "ANDROID",
                    "appVersion": MYSKODA_APP_VERSION,
                    "language": language,
                    "deviceStatus": "ACTIVE",
                }),
                headers={
                    "content-type": "application/json",
                    "X-APP-VERSION-NAME": MYSKODA_APP_VERSION,
                    "X-APP-VERSION-CODE": MYSKODA_APP_VERSION_CODE,
                    "X-APP-INSTALLATION-ID": self._app_installation_id,
                    "X-APP-PLATFORM": "Android",
                    "X-DEVICE-LANGUAGE": language,
                    "X-DEVICE-COUNTRY": country,
                    "User-Agent": f"MySkoda/Android/{MYSKODA_APP_VERSION}/{MYSKODA_APP_VERSION_CODE}",
                },
                allow_redirects=True,
            )
            if response.status_code in (200, 201):
                LOG.debug('FCM token registered with Skoda notifications API (HTTP %s)', response.status_code)
            else:
                LOG.warning('FCM token registration with Skoda returned unexpected status %s: %s',
                            response.status_code, response.text[:200])
        except Exception as exc:  # pylint: disable=broad-except
            LOG.warning('Could not register FCM token with Skoda notifications API: %s', exc)

    @staticmethod
    def _generate_totp(fcm_token: str) -> str:
        """Generate a Time-Based One-Time Password (TOTP) derived from an FCM token."""
        key: bytes = hashlib.sha256(fcm_token.encode('utf-8')).digest()
        time_step: bytes = struct.pack('>Q', int(datetime.now(timezone.utc).timestamp()) // 30)
        mac: bytes = hmac.new(key, time_step, hashlib.sha256).digest()
        offset: int = mac[-1] & 0x0F
        code: int = (
            ((mac[offset] & 0x7F) << 24)
            | ((mac[offset + 1] & 0xFF) << 16)
            | ((mac[offset + 2] & 0xFF) << 8)
            | (mac[offset + 3] & 0xFF)
        )
        return str(code % (10 ** 6)).zfill(6)

    def connect(self, *args, **kwargs) -> MQTTErrorCode:
        """
        Connects the MQTT client to the skoda server.

        The FCM token is fetched in a background thread (started during __init__).
        The TOTP CONNECT properties are applied in _on_pre_connect_callback, which
        paho calls right before sending the CONNECT packet on every connect/reconnect.

        Returns:
            MQTTErrorCode: The result of the connection attempt.
        """
        self._skoda_connector.connection_state._set_value(value=ConnectionState.CONNECTING)  # pylint: disable=protected-access

        return super().connect(*args, host='mqtt.messagehub.de', port=8883, keepalive=60,
                               clean_start=True, **kwargs)

    def _on_pre_connect_callback(self, client: Client, userdata: Any) -> None:
        """
        Callback function that is called before the MQTT client connects to the broker.

        Waits for the background FCM-token fetch to complete, then sets fresh TOTP
        CONNECT properties and updates the access-token password.

        Args:
            client: The MQTT client instance (unused).
            userdata: The user data passed to the callback (unused).

        Returns:
            None
        """
        del client
        del userdata

        # Wait for the background FCM prefetch (with a generous timeout).
        # This runs inside paho's reconnect() before the CONNECT packet is sent,
        # so it is safe to block briefly here.
        if not self._fcm_token_event.wait(timeout=60):
            LOG.warning('Timed out waiting for FCM token; TOTP authentication will be skipped')

        connect_props: Properties = Properties(PacketTypes.CONNECT)
        connect_props.SessionExpiryInterval = MQTT_SESSION_EXPIRY_INTERVAL_SECONDS

        if self._fcm_token is not None:
            connect_props.UserProperty = [
                ('auth_method', 'totp_v1'),
                ('auth_credentials', self._generate_totp(self._fcm_token)),
            ]

            if not self._fcm_token_registered:
                self._register_fcm_token_with_skoda(self._fcm_token)
                self._fcm_token_registered = True

        self._connect_properties = connect_props  # pylint: disable=attribute-defined-outside-init

        if self._skoda_connector.session.expired or self._skoda_connector.session.access_token is None:
            try:
                self._skoda_connector.session.refresh()
            except ConnectionError as exc:
                LOG.error('Token refresh failed due to connection error: %s', exc)
            except TemporaryAuthenticationError as exc:
                LOG.error('Token refresh failed due to temporary MySkoda error: %s', exc)
        if not self._skoda_connector.session.expired and self._skoda_connector.session.access_token is not None:
            # The broker requires the actual user_id as the MQTT username (not a fixed string).
            # Fetch it now if it hasn't been retrieved yet.
            if self._skoda_connector.user_id is None:
                self._skoda_connector.fetch_user()
            self.username_pw_set(username=self._skoda_connector.user_id or 'android-app',
                                 password=self._skoda_connector.session.access_token)

    def _on_carconnectivity_vehicle_enabled(self, element: GenericAttribute, flags: Observable.ObserverEvent) -> None:
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
            self._unsubscribe_vehicle(element)

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
                    account_events: Set[str] = {'privacy',
                                                'guest-user-nomination',
                                                'primary-user-nomination'}
                    vehicle_status_events: Set[str] = {'vehicle-connection-status'}
                    vehicle_event: Set[str] = {'vehicle-connection-status-update'
                                               'vehicle-ignition-status'}
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
                        'charging-statistics',
                        'departure',
                        'vehicle-status/access',
                        'vehicle-status/lights',
                        'vehicle-status/odometer'
                        }
                    possible_topics: Set[str] = set()
                    # Compile all possible topics
                    for event in account_events:
                        possible_topics.add(f'{user_id}/{vin}/account-event/{event}')
                    for event in vehicle_status_events:
                        possible_topics.add(f'{user_id}/{vin}/vehicle-status/{event}')
                    for event in vehicle_event:
                        possible_topics.add(f'{user_id}/{vin}/vehicle-event/{event}')
                    for event in operation_requests:
                        possible_topics.add(f'{user_id}/{vin}/operation-request/{event}')
                    for event in service_events:
                        possible_topics.add(f'{user_id}/{vin}/service-event/{event}')

                    # Subscribe wildcard topics
                    self.subscribe(f'{user_id}/{vin}/#')
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
        vin: str = vehicle.id
        for topic in self.subscribed_topics:
            if vin in topic:
                self.unsubscribe(topic)
                self.subscribed_topics.remove(topic)
                LOG.debug('Unsubscribed from topic %s', topic)

    def _on_connect_callback(self, client: Client, obj: Any, flags: ConnectFlags, reason_code: ReasonCode, properties: Optional[Properties]) -> None:
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
        del client  # unused
        del obj  # unused
        del flags  # unused
        del properties
        # reason_code 0 means success
        if reason_code == 0:
            LOG.info('Connected to Skoda MQTT server')
            if self._skoda_connector.rest_connected:
                self._skoda_connector.connection_state._set_value(value=ConnectionState.CONNECTED)  # pylint: disable=protected-access
            self._skoda_connector.mqtt_connected = True
            observer_flags: Observable.ObserverEvent = Observable.ObserverEvent.ENABLED | Observable.ObserverEvent.DISABLED
            self._skoda_connector.car_connectivity.garage.add_observer(observer=self._on_carconnectivity_vehicle_enabled,
                                                                       flag=observer_flags,
                                                                       priority=Observable.ObserverPriority.USER_MID)
            self._retry_refresh_login_once = True
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
            self._refresh_mqtt_access_token_once('bad username or password')
        elif reason_code == 135:
            LOG.error('Could not connect (%s): Not authorized', reason_code)
            self._refresh_mqtt_access_token_once('not authorized')
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

    def _refresh_mqtt_access_token_once(self, reason: str) -> None:
        """Refresh auth once after an MQTT authentication failure."""
        if self._retry_refresh_login_once is True:
            self._retry_refresh_login_once = False
            LOG.info('trying a token refresh once to resolve MQTT %s error', reason)
            try:
                self._skoda_connector.session.refresh()
            except TemporaryAuthenticationError as exc:
                LOG.error('Token refresh failed due to temporary MySkoda error: %s', exc)
            except ConnectionError as exc:
                LOG.error('Token refresh failed due to connection error: %s', exc)

    def _on_disconnect_callback(self, client: Client, userdata, flags: DisconnectFlags, reason_code: ReasonCode, properties: Optional[Properties]) -> None:
        """["Client", Any, DisconnectFlags, ReasonCode, Union[Properties, None]
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

        self._skoda_connector.connection_state._set_value(value=ConnectionState.DISCONNECTED)  # pylint: disable=protected-access
        self._skoda_connector.mqtt_connected = False
        self._skoda_connector.car_connectivity.garage.remove_observer(observer=self._on_carconnectivity_vehicle_enabled)

        self.subscribed_topics.clear()

        if reason_code == 0:
            LOG.info('Client successfully disconnected')
        elif reason_code == 4:
            LOG.info('Client successfully disconnected: %s', userdata)
        elif reason_code == 128:
            LOG.info('Client disconnected: Needs new access token, trying to reconnect')
        elif reason_code == 137:
            LOG.error('Client disconnected: Server busy')
        elif reason_code == 139:
            LOG.error('Client disconnected: Server shutting down')
        elif reason_code == 160:
            LOG.error('Client disconnected: Maximum connect time')
        else:
            LOG.error('Client unexpectedly disconnected (%d: %s), trying to reconnect', reason_code.value, reason_code.getName())

    def _on_subscribe_callback(self, client: Client, obj: Any, mid: int, reason_codes: List[ReasonCode], properties: Optional[Properties]) -> None:
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
        del client  # unused
        del obj  # unused
        del properties  # unused
        if any(x in [0, 1, 2] for x in reason_codes):
            LOG.debug('sucessfully subscribed to topic of mid %d', mid)
        else:
            LOG.error('Subscribe was not successfull (%s)', ', '.join([reason_code.getName() for reason_code in reason_codes]))

    def _on_message_callback(self, client: Client, obj: Any, msg: MQTTMessage) -> None:  # noqa: C901
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
        del client  # unused
        del obj  # unused
        if len(msg.payload) == 0:
            LOG_API.debug('MQTT topic %s: ignoring empty message', msg.topic)
            return

        self._skoda_connector.last_event._set_value(value=datetime.now(tz=timezone.utc))  # pylint: disable=protected-access

        # service_events
        match = re.match(r'^(?P<user_id>[0-9a-fA-F-]+)/(?P<vin>[A-Z0-9]+)/vehicle-event/(?P<vehicle_event>[a-zA-Z0-9-_/]+)$', msg.topic)
        if match:
            user_id: str = match.group('user_id')
            vin: str = match.group('vin')
            vehicle_event: str = match.group('vehicle_event')
            data: Dict[str, Any] = json.loads(msg.payload)
            if data is not None:
                if 'timestamp' in data and data['timestamp'] is not None:
                    measured_at: datetime = robust_time_parse(data['timestamp'])
                    vehicle: Optional[GenericVehicle] = self._skoda_connector.car_connectivity.garage.get_vehicle(vin)
                else:
                    measured_at: datetime = datetime.now(tz=timezone.utc)
                if vehicle_event == 'vehicle-connection-status-update':
                    vehicle: Optional[GenericVehicle] = self._skoda_connector.car_connectivity.garage.get_vehicle(vin)
                    if vehicle is not None:
                        if 'name' in data and data['name'] == 'vehicle-awake':
                            if isinstance(vehicle, SkodaVehicle):
                                self._skoda_connector._update_online_tracking(vehicle=vehicle, last_measurement=measured_at)  # pylint: disable=protected-access
                                vehicle = self._skoda_connector.fetch_connection_status(vehicle, no_cache=True)
                                vehicle = self._skoda_connector.decide_state(vehicle)
                                self._skoda_connector.car_connectivity.transaction_end()
                                LOG_API.info('Vehicle %s is awake', vin)
                            return
                        else:
                            LOG_API.info('Received unknown name %s for vehicle event %s for vehicle %s from user %s: %s', data['name'],
                                         vehicle_event, vin, user_id, msg.payload)
                            return
                elif vehicle_event == 'vehicle-ignition-status':
                    vehicle: Optional[GenericVehicle] = self._skoda_connector.car_connectivity.garage.get_vehicle(vin)
                    if vehicle is not None:
                        if 'name' in data and data['name'] == 'vehicle-ignition-status-changed':
                            if 'data' in data and data['data'] is not None and 'ignitionStatus' in data['data']:
                                ignition_status: str = data['data']['ignitionStatus']
                                if ignition_status == 'ON':
                                    if isinstance(vehicle, SkodaVehicle):
                                        # pylint: disable-next=protected-access
                                        vehicle.ignition_on._set_value(value=True, measured=measured_at)
                                        LOG.info('Vehicle %s ignition turned ON', vin)
                                elif ignition_status == 'OFF':
                                    if isinstance(vehicle, SkodaVehicle):
                                        # pylint: disable-next=protected-access
                                        vehicle.ignition_on._set_value(value=False, measured=measured_at)
                                        LOG.info('Vehicle %s ignition turned OFF', vin)
                                if isinstance(vehicle, SkodaVehicle):
                                    if vehicle.capabilities is not None and vehicle.capabilities.enabled \
                                            and vehicle.capabilities.has_capability('PARKING_POSITION'):
                                        try:
                                            self._skoda_connector.fetch_position(vehicle, no_cache=True)
                                        except CarConnectivityError as e:
                                            LOG.error('Error while fetching position: %s', e)
                                    vehicle = self._skoda_connector.decide_state(vehicle)
                                    self._skoda_connector.car_connectivity.transaction_end()
                            return
                        else:
                            LOG_API.info('Received unknown name %s for vehicle event %s for vehicle %s from user %s: %s', data['name'],
                                         vehicle_event, vin, user_id, msg.payload)
                            return
                else:
                    LOG_API.info('Received unknown vehicle event %s for vehicle %s from user %s: %s', vehicle_event, vin, user_id, msg.payload)
            return
        # service_events
        match = re.match(r'^(?P<user_id>[0-9a-fA-F-]+)/(?P<vin>[A-Z0-9]+)/service-event/(?P<service_event>[a-zA-Z0-9-_/]+)$', msg.topic)
        if match:
            user_id: str = match.group('user_id')
            vin: str = match.group('vin')
            service_event: str = match.group('service_event')
            data: Dict[str, Any] = json.loads(msg.payload)
            if data is not None:
                if 'timestamp' in data and data['timestamp'] is not None:
                    measured_at: datetime = robust_time_parse(data['timestamp'])
                else:
                    measured_at: datetime = datetime.now(tz=timezone.utc)
                if service_event == 'charging':
                    if 'name' in data and data['name'] == 'change-charge-mode' or data['name'] == 'change-soc':
                        if 'data' in data and data['data'] is not None:
                            vehicle: Optional[GenericVehicle] = self._skoda_connector.car_connectivity.garage.get_vehicle(vin)
                            if isinstance(vehicle, SkodaElectricVehicle):
                                self.__parse_charging_message_data(vehicle=vehicle, data=data['data'], measured_at=measured_at)
                                self._skoda_connector.car_connectivity.transaction_end()
                                LOG.debug('Received %s event for vehicle %s from user %s', data['name'], vin, user_id)
                                return
                            else:
                                LOG.debug('Discarded %s event for vehicle %s from user %s: vehicle is not an electric vehicle', data['name'], vin, user_id)
                    else:
                        LOG_API.info('Received unkown event name %s service event %s for vehicle %s from user %s: %s', data['name'],
                                     service_event, vin, user_id, msg.payload)
                    return
                elif service_event == 'air-conditioning':
                    if 'name' in data and data['name'] == 'change-remaining-time':
                        if 'data' in data and data['data'] is not None:
                            vehicle: Optional[GenericVehicle] = self._skoda_connector.car_connectivity.garage.get_vehicle(vin)
                            if isinstance(vehicle, SkodaVehicle):
                                try:
                                    self._skoda_connector.fetch_air_conditioning(vehicle, no_cache=True)
                                    self._skoda_connector.car_connectivity.transaction_end()
                                except CarConnectivityError as e:
                                    LOG.error('Error while fetching air conditioning: %s', e)
                    elif 'name' in data and data['name'] == 'climatisation-completed':
                        if 'data' in data and data['data'] is not None:
                            vehicle: Optional[GenericVehicle] = self._skoda_connector.car_connectivity.garage.get_vehicle(vin)
                            if vehicle is not None and vehicle.climatization is not None:
                                # pylint: disable-next=protected-access
                                vehicle.climatization.state._set_value(value=Climatization.ClimatizationState.OFF, measured=measured_at)
                                # pylint: disable-next=protected-access
                                vehicle.climatization.estimated_date_reached._set_value(value=measured_at, measured=measured_at)
                    else:
                        LOG_API.info('Received unknown event name %s service event %s for vehicle %s from user %s: %s', data['name'],
                                     service_event, vin, user_id, msg.payload)
                    return
                elif service_event == 'charging-statistics':
                    if 'name' in data and data['name'] == 'charging-plugstatus-disconnected':
                        vehicle: Optional[GenericVehicle] = self._skoda_connector.car_connectivity.garage.get_vehicle(vin)
                        if isinstance(vehicle, SkodaElectricVehicle):
                            try:
                                self._skoda_connector.fetch_charging(vehicle, no_cache=True)
                                self._skoda_connector.car_connectivity.transaction_end()
                            except CarConnectivityError as e:
                                LOG.error('Error while fetching charging statistics: %s', e)
                    elif 'name' in data and data['name'] == 'charging-started':
                        vehicle: Optional[GenericVehicle] = self._skoda_connector.car_connectivity.garage.get_vehicle(vin)
                        if isinstance(vehicle, SkodaElectricVehicle):
                            try:
                                self._skoda_connector.fetch_charging(vehicle, no_cache=True)
                                self._skoda_connector.car_connectivity.transaction_end()
                            except CarConnectivityError as e:
                                LOG.error('Error while fetching charging statistics: %s', e)
                    elif 'name' in data and data['name'] == 'charging-update':
                        vehicle: Optional[GenericVehicle] = self._skoda_connector.car_connectivity.garage.get_vehicle(vin)
                        if isinstance(vehicle, SkodaElectricVehicle):
                            self.__parse_charging_message_data(vehicle=vehicle, data=data['data'], measured_at=measured_at)
                            self._skoda_connector.car_connectivity.transaction_end()
                    elif 'name' in data and data['name'] == 'charging-completed':
                        vehicle: Optional[GenericVehicle] = self._skoda_connector.car_connectivity.garage.get_vehicle(vin)
                        if isinstance(vehicle, SkodaElectricVehicle):
                            self.__parse_charging_message_data(vehicle=vehicle, data=data['data'], measured_at=measured_at)
                            self._skoda_connector.car_connectivity.transaction_end()
                    else:
                        LOG_API.info('Received unknown event name %s service event %s for vehicle %s from user %s: %s', data['name'],
                                     service_event, vin, user_id, msg.payload)
                    return
                elif service_event == 'vehicle-status/access':
                    if 'name' in data and data['name'] == 'change-access':
                        if 'data' in data and data['data'] is not None:
                            vehicle: Optional[GenericVehicle] = self._skoda_connector.car_connectivity.garage.get_vehicle(vin)
                            if isinstance(vehicle, SkodaVehicle):
                                def delayed_access_function(vehicle: SkodaVehicle):
                                    """
                                    Function to be executed after a delay of two seconds.
                                    """
                                    vin = vehicle.id
                                    self.delayed_access_function_timers.pop(vin)
                                    try:
                                        self._skoda_connector.fetch_vehicle_status(vehicle, no_cache=True)
                                    except CarConnectivityError as e:
                                        LOG.error('Error while fetching vehicle status: %s', e)
                                    if vehicle.capabilities is not None and vehicle.capabilities.enabled \
                                            and vehicle.capabilities.has_capability('AIR_CONDITIONING'):
                                        try:
                                            self._skoda_connector.fetch_air_conditioning(vehicle, no_cache=True)
                                        except CarConnectivityError as e:
                                            LOG.error('Error while fetching air conditioning: %s', e)
                                    self._skoda_connector.car_connectivity.transaction_end()

                                if vin in self.delayed_access_function_timers:
                                    self.delayed_access_function_timers[vin].cancel()
                                self.delayed_access_function_timers[vin] = threading.Timer(2.0, delayed_access_function, kwargs={'vehicle': vehicle})
                                self.delayed_access_function_timers[vin].start()

                    return
                elif service_event == 'vehicle-status/lights':
                    if 'name' in data and data['name'] == 'change-lights':
                        if 'data' in data and data['data'] is not None:
                            vehicle: Optional[GenericVehicle] = self._skoda_connector.car_connectivity.garage.get_vehicle(vin)
                            if isinstance(vehicle, SkodaVehicle):
                                try:
                                    self._skoda_connector.fetch_vehicle_status(vehicle, no_cache=True)
                                    self._skoda_connector.car_connectivity.transaction_end()
                                except CarConnectivityError as e:
                                    LOG.error('Error while fetching vehicle status: %s', e)
                elif service_event == 'vehicle-status/odometer':
                    if 'name' in data and data['name'] == 'change-odometer':
                        if 'data' in data and data['data'] is not None:
                            vehicle: Optional[GenericVehicle] = self._skoda_connector.car_connectivity.garage.get_vehicle(vin)
                            if isinstance(vehicle, SkodaVehicle):
                                try:
                                    self._skoda_connector.fetch_maintenance(vehicle, no_cache=True)  # todo: check if there is a better way to fetch odometer
                                    self._skoda_connector.car_connectivity.transaction_end()
                                except CarConnectivityError as e:
                                    LOG.error('Error while fetching vehicle status: %s', e)
                return
            LOG_API.info('Received unknown service event %s for vehicle %s from user %s: %s', service_event, vin, user_id, msg.payload)
            return
        # operation-requests
        match = re.match(r'^(?P<user_id>[0-9a-fA-F-]+)/(?P<vin>[A-Z0-9]+)/operation-request/(?P<operation_request>[a-zA-Z0-9-_/]+)$', msg.topic)
        if match:
            user_id: str = match.group('user_id')
            vin: str = match.group('vin')
            operation_request: str = match.group('operation_request')
            data: Dict[str, Any] = json.loads(msg.payload)
            if data is not None:
                vehicle: Optional[GenericVehicle] = self._skoda_connector.car_connectivity.garage.get_vehicle(vin)
                if operation_request == 'air-conditioning/set-air-conditioning-at-unlock' \
                        or operation_request == 'air-conditioning/set-air-conditioning-seats-heating' \
                        or operation_request == 'air-conditioning/set-air-conditioning-timers' \
                        or operation_request == 'air-conditioning/set-air-conditioning-without-external-power' \
                        or operation_request == 'air-conditioning/set-target-temperature' \
                        or operation_request == 'air-conditioning/start-stop-air-conditioning' \
                        or operation_request == 'air-conditioning/start-stop-window-heating' \
                        or operation_request == 'air-conditioning/windows-heating':
                    if isinstance(vehicle, SkodaVehicle):
                        if 'status' in data and data['status'] is not None:
                            if data['status'] == 'COMPLETED_SUCCESS':
                                LOG.debug('Received %s operation request for vehicle %s from user %s', operation_request, vin, user_id)
                                try:
                                    self._skoda_connector.fetch_air_conditioning(vehicle, no_cache=True)
                                    self._skoda_connector.car_connectivity.transaction_end()
                                except CarConnectivityError as e:
                                    LOG.error('Error while fetching air-conditioning: %s', e)
                                return
                            elif data['status'] == 'IN_PROGRESS':
                                LOG.debug('Received %s operation request for vehicle %s from user %s', operation_request, vin, user_id)
                                return
                elif operation_request == 'charging/start-stop-charging' \
                        or operation_request == 'charging/update-battery-support' \
                        or operation_request == 'charging/update-auto-unlock-plug' \
                        or operation_request == 'charging/update-care-mode' \
                        or operation_request == 'charging/update-charge-limit' \
                        or operation_request == 'charging/update-charge-mode' \
                        or operation_request == 'charging/update-charging-profiles' \
                        or operation_request == 'charging/update-charging-current':
                    if isinstance(vehicle, SkodaElectricVehicle):
                        if 'status' in data and data['status'] is not None:
                            if data['status'] == 'COMPLETED_SUCCESS':
                                LOG.debug('Received %s operation request for vehicle %s from user %s', operation_request, vin, user_id)
                                try:
                                    self._skoda_connector.fetch_charging(vehicle, no_cache=True)
                                    self._skoda_connector.car_connectivity.transaction_end()
                                except CarConnectivityError as e:
                                    LOG.error('Error while fetching charging: %s', e)
                                return
                            elif data['status'] == 'IN_PROGRESS':
                                LOG.debug('Received %s operation request for vehicle %s from user %s', operation_request, vin, user_id)
                                return
                LOG_API.info('Received unknown operation request %s for vehicle %s from user %s: %s', operation_request, vin, user_id, msg.payload)
                return
        LOG_API.info('I don\'t understand message %s: %s', msg.topic, msg.payload)

    def __parse_charging_message_data(self, vehicle: SkodaElectricVehicle, data: Dict[str, Any], measured_at: datetime) -> None:
        """
        Parse charging data from MQTT message and update vehicle state.

        This method processes the charging data received in the MQTT message payload
        and updates the corresponding vehicle's charging state accordingly.

        Args:
            vehicle: The vehicle instance to update.
            data: The dictionary containing charging data.

        Returns:
            None
        """
        electric_drive: Optional[ElectricDrive] = vehicle.get_electric_drive()
        if electric_drive is not None:
            charging_state: Optional[Charging.ChargingState] = vehicle.charging.state.value
            old_charging_state: Optional[Charging.ChargingState] = charging_state
            if 'carCapturedTimestamp' in data and data['carCapturedTimestamp'] is not None:
                measured_at = robust_time_parse(data['carCapturedTimestamp'])
                self._skoda_connector._update_online_tracking(vehicle=vehicle, last_measurement=measured_at)  # pylint: disable=protected-access
            if 'mode' in data and data['mode'] is not None \
                    and vehicle.charging is not None and isinstance(vehicle.charging.settings, SkodaCharging.Settings):
                if data['mode'] in [item.value for item in SkodaCharging.SkodaChargeMode]:
                    skoda_charging_mode = SkodaCharging.SkodaChargeMode(data['mode'])
                else:
                    LOG_API.info('Unkown charging mode %s not in %s', data['mode'], str(SkodaCharging.SkodaChargeMode))
                    skoda_charging_mode = SkodaCharging.SkodaChargeMode.UNKNOWN
                # pylint: disable-next=protected-access
                vehicle.charging.settings.preferred_charge_mode._set_value(value=skoda_charging_mode, measured=measured_at)
            if 'state' in data and data['state'] is not None:
                if data['state'] in [item.value for item in SkodaCharging.SkodaChargingState]:
                    skoda_charging_state = SkodaCharging.SkodaChargingState(data['state'])
                    charging_state = mapping_skoda_charging_state[skoda_charging_state]
                else:
                    LOG_API.info('Unkown charging state %s not in %s', data['state'], str(SkodaCharging.SkodaChargingState))
                    charging_state = Charging.ChargingState.UNKNOWN
                # pylint: disable-next=protected-access
                vehicle.charging.state._set_value(value=charging_state, measured=measured_at)
                if charging_state == Charging.ChargingState.OFF:
                    # pylint: disable-next=protected-access
                    vehicle.charging.type._set_value(value=Charging.ChargingType.OFF, measured=measured_at)
                    # pylint: disable-next=protected-access
                    vehicle.charging.rate._set_value(value=0, measured=measured_at, unit=Speed.KMH)
                    # pylint: disable-next=protected-access
                    vehicle.charging.power._set_value(value=0, measured=measured_at, unit=Power.KW)
            if 'soc' in data and data['soc'] is not None:
                if isinstance(data['soc'], str):
                    data['soc'] = int(data['soc'])
                electric_drive.level._set_value(measured=measured_at, value=data['soc'])  # pylint: disable=protected-access
            if 'chargedRange' in data and data['chargedRange'] is not None:
                # pylint: disable-next=protected-access
                electric_drive.range._set_value(measured=measured_at, value=data['chargedRange'], unit=Length.KM)
            # If charging state changed, fetch charging again
            if old_charging_state != charging_state:
                try:
                    self._skoda_connector.fetch_charging(vehicle, no_cache=True)
                    self._skoda_connector.car_connectivity.transaction_end()
                except CarConnectivityError as e:
                    LOG.error('Error while fetching charging: %s', e)
        if 'timeToFinish' in data and data['timeToFinish'] is not None \
                and vehicle.charging is not None:
            try:
                remaining_duration: Optional[timedelta] = timedelta(minutes=int(data['timeToFinish']))
                estimated_date_reached: Optional[datetime] = measured_at + remaining_duration
                estimated_date_reached = estimated_date_reached.replace(second=0, microsecond=0)
            except ValueError:
                estimated_date_reached: Optional[datetime] = None
            # pylint: disable-next=protected-access
            vehicle.charging.estimated_date_reached._set_value(measured=measured_at, value=estimated_date_reached)
        if 'chargingType' in data and data['chargingType'] is not None \
                and vehicle.charging is not None:
            if data['chargingType'] in [item.value for item in Charging.ChargingType]:
                charging_type: Charging.ChargingType = Charging.ChargingType(data['chargingType'])
            else:
                LOG_API.info('Unkown charging type %s not in %s', data['chargingType'], str(Charging.ChargingType))
                charging_type = Charging.ChargingType.UNKNOWN
            vehicle.charging.type._set_value(value=charging_type, measured=measured_at)  # pylint: disable=protected-access
        if 'power' in data and data['power'] is not None \
                and vehicle.charging is not None:
            try:
                power_value: float = float(data['power'])
                vehicle.charging.power._set_value(value=power_value, measured=measured_at, unit=Power.KW)  # pylint: disable=protected-access
            except ValueError:
                LOG_API.warning('Invalid power value received: %s', data['power'])
        if 'odometer' in data and data['odometer'] is not None:
            if 'odometerTimestamp' in data and data['odometerTimestamp'] is not None:
                measured_at = robust_time_parse(data['odometerTimestamp'])
                self._skoda_connector._update_online_tracking(vehicle=vehicle, last_measurement=measured_at)  # pylint: disable=protected-access
            try:
                odometer_value: float = float(data['odometer'])
                vehicle.odometer._set_value(value=odometer_value, measured=measured_at, unit=Length.KM)  # pylint: disable=protected-access
            except ValueError:
                LOG_API.warning('Invalid odometer value received: %s', data['odometer'])
        log_extra_keys(LOG_API, 'data', data,  {'vin', 'userId', 'soc', 'chargedRange', 'timeToFinish', 'state', 'mode', 'chargingType', 'odometer',
                                                'power', 'targetSoc', 'odometerTimestamp', 'carCapturedTimestamp'})
