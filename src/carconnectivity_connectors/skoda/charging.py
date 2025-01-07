"""
Module for charging for skoda vehicles.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from enum import Enum

from carconnectivity.charging import Charging

if TYPE_CHECKING:
    from typing import Dict


class SkodaCharging(Charging):  # pylint: disable=too-many-instance-attributes
    """
    SkodaCharging class for handling Skoda vehicle charging information.

    This class extends the Charging class and includes an enumeration of various 
    charging states specific to Skoda vehicles.
    """
    class SkodaChargingState(Enum,):
        """
        Enum representing the various charging states for a Skoda vehicle.

        Attributes:
            OFF: The vehicle is not charging.
            READY_FOR_CHARGING: The vehicle is ready to start charging.
            NOT_READY_FOR_CHARGING: The vehicle is not ready to start charging.
            CONSERVATION: The vehicle is in conservation mode.
            CHARGE_PURPOSE_REACHED_NOT_CONSERVATION_CHARGING: The vehicle has reached its charging purpose and is not in conservation charging mode.
            CHARGE_PURPOSE_REACHED_CONSERVATION: The vehicle has reached its charging purpose and is in conservation charging mode.
            CHARGING: The vehicle is currently charging.
            ERROR: There is an error in the charging process.
            UNSUPPORTED: The charging state is unsupported.
            DISCHARGING: The vehicle is discharging.
            UNKNOWN: The charging state is unknown.
        """
        OFF = 'off'
        CONNECT_CABLE = 'connectCable'
        READY_FOR_CHARGING = 'readyForCharging'
        NOT_READY_FOR_CHARGING = 'notReadyForCharging'
        CONSERVATION = 'conservation'
        CHARGE_PURPOSE_REACHED_NOT_CONSERVATION_CHARGING = 'chargePurposeReachedAndNotConservationCharging'
        CHARGE_PURPOSE_REACHED_CONSERVATION = 'chargePurposeReachedAndConservation'
        CHARGING = 'charging'
        ERROR = 'error'
        UNSUPPORTED = 'unsupported'
        DISCHARGING = 'discharging'
        UNKNOWN = 'unknown charging state'


# Mapping of Skoda charging states to generic charging states
mapping_skoda_charging_state: Dict[SkodaCharging.SkodaChargingState, Charging.ChargingState] = {
    SkodaCharging.SkodaChargingState.OFF: Charging.ChargingState.OFF,
    SkodaCharging.SkodaChargingState.CONNECT_CABLE: Charging.ChargingState.OFF,
    SkodaCharging.SkodaChargingState.READY_FOR_CHARGING: Charging.ChargingState.READY_FOR_CHARGING,
    SkodaCharging.SkodaChargingState.NOT_READY_FOR_CHARGING: Charging.ChargingState.OFF,
    SkodaCharging.SkodaChargingState.CONSERVATION: Charging.ChargingState.CONSERVATION,
    SkodaCharging.SkodaChargingState.CHARGE_PURPOSE_REACHED_NOT_CONSERVATION_CHARGING: Charging.ChargingState.READY_FOR_CHARGING,
    SkodaCharging.SkodaChargingState.CHARGE_PURPOSE_REACHED_CONSERVATION: Charging.ChargingState.CONSERVATION,
    SkodaCharging.SkodaChargingState.CHARGING: Charging.ChargingState.CHARGING,
    SkodaCharging.SkodaChargingState.ERROR: Charging.ChargingState.ERROR,
    SkodaCharging.SkodaChargingState.UNSUPPORTED: Charging.ChargingState.UNSUPPORTED,
    SkodaCharging.SkodaChargingState.DISCHARGING: Charging.ChargingState.DISCHARGING,
    SkodaCharging.SkodaChargingState.UNKNOWN: Charging.ChargingState.UNKNOWN
}
