"""Module for Skoda vehicle capability class."""
from __future__ import annotations
from typing import TYPE_CHECKING

from enum import Enum

from carconnectivity.objects import GenericObject
from carconnectivity.attributes import EnumAttribute, StringAttribute

if TYPE_CHECKING:
    from typing import Optional


class Error(GenericObject):
    """
    Represents an error object in the car connectivity context.
    """

    def __init__(self, object_id, parent: Optional[GenericObject] = None) -> None:
        super().__init__(object_id, parent=parent)
        self.type: EnumAttribute = EnumAttribute("type", parent=self, tags={'connector_custom'})
        self.description: StringAttribute = StringAttribute("description", parent=self, tags={'connector_custom'})

    class ChargingError(Enum):
        """
        Enum representing various charging errors for Skoda car connectivity.

        Attributes:
            STATUS_OF_CHARGING_NOT_AVAILABLE: Indicates that the status of charging is not available.
            STATUS_OF_CONNECTION_NOT_AVAILABLE: Indicates that the status of connection is not available.
            CARE_MODE_IS_NOT_AVAILABLE: Indicates that the care mode is not available.
            AUTO_UNLOCK_IS_NOT_AVAILABLE: Indicates that the auto unlock feature is not available.
            MAX_CHARGE_CURRENT_IS_NOT_AVAILABLE: Indicates that the maximum charge current setting is not available.
            CHARGE_LIMIT_IS_NOT_AVAILABLE: Indicates that the charge limit setting is not available.
        """
        STATUS_OF_CHARGING_NOT_AVAILABLE = 'STATUS_OF_CHARGING_NOT_AVAILABLE'
        STATUS_OF_CONNECTION_NOT_AVAILABLE = 'STATUS_OF_CONNECTION_NOT_AVAILABLE'
        CARE_MODE_IS_NOT_AVAILABLE = 'CARE_MODE_IS_NOT_AVAILABLE'
        AUTO_UNLOCK_IS_NOT_AVAILABLE = 'AUTO_UNLOCK_IS_NOT_AVAILABLE'
        MAX_CHARGE_CURRENT_IS_NOT_AVAILABLE = 'MAX_CHARGE_CURRENT_IS_NOT_AVAILABLE'
        CHARGE_LIMIT_IS_NOT_AVAILABLE = 'CHARGE_LIMIT_IS_NOT_AVAILABLE'
        UNKNOWN = 'UNKNOWN'

    class ClimatizationError(Enum):
        """
        ClimatizationError is an enumeration for representing various errors
        related to the climatization system in a Skoda car.

        This enum can be extended to include specific error codes and messages
        that correspond to different climatization issues.
        """
        UNAVAILABLE_CHARGING_INFORMATION = 'UNAVAILABLE_CHARGING_INFORMATION'
        UNKNOWN = 'UNKNOWN'
