"""Module for vehicle classes."""
from __future__ import annotations
from typing import TYPE_CHECKING

import threading

from datetime import datetime

from carconnectivity.vehicle import GenericVehicle, ElectricVehicle, CombustionVehicle, HybridVehicle
from carconnectivity.charging import Charging
from carconnectivity.attributes import BooleanAttribute

from carconnectivity_connectors.skoda.capability import Capabilities
from carconnectivity_connectors.skoda.charging import SkodaCharging
from carconnectivity_connectors.skoda.climatization import SkodaClimatization

SUPPORT_IMAGES = False
try:
    from PIL import Image
    SUPPORT_IMAGES = True
except ImportError:
    pass

if TYPE_CHECKING:
    from typing import Optional, Dict
    from carconnectivity.garage import Garage
    from carconnectivity_connectors.base.connector import BaseConnector


class SkodaVehicle(GenericVehicle):  # pylint: disable=too-many-instance-attributes
    """
    A class to represent a generic Skoda vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[SkodaVehicle] = None, initialization: Optional[Dict] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
            self.capabilities: Capabilities = origin.capabilities
            self.capabilities.parent = self
            self.in_motion: BooleanAttribute = origin.in_motion
            self.in_motion.parent = self
            self.ignition_on: BooleanAttribute = origin.ignition_on
            self.ignition_on.parent = self
            self.last_measurement: Optional[datetime] = origin.last_measurement
            self.official_connection_state: Optional[GenericVehicle.ConnectionState] = origin.official_connection_state
            self.online_timeout_timer: Optional[threading.Timer] = origin.online_timeout_timer
            self.online_timeout_timer_lock: threading.LockType = origin.online_timeout_timer_lock
            if SUPPORT_IMAGES:
                self._car_images = origin._car_images

        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)
            self.climatization: SkodaClimatization = SkodaClimatization(vehicle=self, origin=self.climatization,
                                                                        initialization=self.get_initialization('climatization'))
            self.capabilities: Capabilities = Capabilities(vehicle=self, initialization=self.get_initialization('capabilities'))
            self.in_motion: BooleanAttribute = BooleanAttribute(name='in_motion', parent=self, tags={'connector_custom'},
                                                                initialization=self.get_initialization('in_motion'))
            self.ignition_on: BooleanAttribute = BooleanAttribute(name='ignition_on', parent=self, tags={'connector_custom'},
                                                                  initialization=self.get_initialization('ignition_on'))
            self.last_measurement = None
            self.official_connection_state = None
            self.online_timeout_timer: Optional[threading.Timer] = None
            self.online_timeout_timer_lock: threading.LockType = threading.Lock()
            if SUPPORT_IMAGES:
                self._car_images: Dict[str, Image.Image] = {}
        self.manufacturer._set_value(value='Škoda')  # pylint: disable=protected-access

    def __del__(self) -> None:
        with self.online_timeout_timer_lock:
            if self.online_timeout_timer is not None:
                self.online_timeout_timer.cancel()
                self.online_timeout_timer = None


class SkodaElectricVehicle(ElectricVehicle, SkodaVehicle):
    """
    Represents a Skoda electric vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[SkodaVehicle] = None, initialization: Optional[Dict] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
            if isinstance(origin, ElectricVehicle):
                self.charging: Charging = SkodaCharging(vehicle=self, origin=origin.charging)
            else:
                self.charging: Charging = SkodaCharging(vehicle=self, origin=self.charging)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)
            self.charging: Charging = SkodaCharging(vehicle=self, origin=self.charging, initialization=self.get_initialization('charging'))


class SkodaCombustionVehicle(CombustionVehicle, SkodaVehicle):
    """
    Represents a Skoda combustion vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[SkodaVehicle] = None, initialization: Optional[Dict] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)


class SkodaHybridVehicle(HybridVehicle, SkodaVehicle):
    """
    Represents a Skoda hybrid vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[SkodaVehicle] = None, initialization: Optional[Dict] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)
