"""Module for vehicle classes."""
from __future__ import annotations
from typing import TYPE_CHECKING

from carconnectivity.vehicle import GenericVehicle, ElectricVehicle, CombustionVehicle, HybridVehicle
from carconnectivity.charging import Charging

from carconnectivity_connectors.skoda.capability import Capabilities
from carconnectivity_connectors.skoda.charging import SkodaCharging

if TYPE_CHECKING:
    from typing import Optional
    from carconnectivity.garage import Garage
    from carconnectivity_connectors.base.connector import BaseConnector


class SkodaVehicle(GenericVehicle):  # pylint: disable=too-many-instance-attributes
    """
    A class to represent a generic Skoda vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[SkodaVehicle] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
            self.capabilities: Capabilities = origin.capabilities
            self.capabilities.parent = self
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)
            self.capabilities = Capabilities(vehicle=self)
        self.manufacturer._set_value(value='Škoda')  # pylint: disable=protected-access

    def __str__(self) -> str:
        return_string: str = f'\t{self.capabilities}\n'
        return return_string


class SkodaElectricVehicle(ElectricVehicle, SkodaVehicle):
    """
    Represents a Skoda electric vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[SkodaVehicle] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
            if isinstance(origin, ElectricVehicle):
                self.charging: Charging = SkodaCharging(origin=origin.charging)
                self.charging.parent = self
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)
            self.charging: Charging = SkodaCharging(vehicle=self)


class SkodaCombustionVehicle(CombustionVehicle, SkodaVehicle):
    """
    Represents a Skoda combustion vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[SkodaVehicle] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)


class SkodaHybridVehicle(HybridVehicle, SkodaVehicle):
    """
    Represents a Skoda hybrid vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[SkodaVehicle] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)
