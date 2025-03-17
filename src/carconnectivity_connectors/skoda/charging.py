"""
Module for charging for skoda vehicles.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from enum import Enum

from carconnectivity.charging import Charging
from carconnectivity.objects import GenericObject
from carconnectivity.vehicle import ElectricVehicle
from carconnectivity.attributes import BooleanAttribute, EnumAttribute, StringAttribute

from carconnectivity_connectors.skoda.error import Error

if TYPE_CHECKING:
    from typing import Optional, Dict


class SkodaCharging(Charging):  # pylint: disable=too-many-instance-attributes
    """
    SkodaCharging class for handling Skoda vehicle charging information.

    This class extends the Charging class and includes an enumeration of various
    charging states specific to Skoda vehicles.
    """
    def __init__(self, vehicle: ElectricVehicle | None = None, origin: Optional[Charging] = None) -> None:
        if origin is not None:
            super().__init__(vehicle=vehicle, origin=origin)
            self.settings: Charging.Settings = SkodaCharging.Settings(parent=self, origin=origin.settings)
        else:
            super().__init__(vehicle=vehicle)
            self.settings: Charging.Settings = SkodaCharging.Settings(parent=self, origin=self.settings)
        self.errors: Dict[str, Error] = {}
        self.is_in_saved_location: BooleanAttribute = BooleanAttribute("is_in_saved_location", parent=self, tags={'connector_custom'})

    class Settings(Charging.Settings):
        """
        This class represents the settings for a skoda car charging.
        """
        def __init__(self, parent: Optional[GenericObject] = None, origin: Optional[Charging.Settings] = None) -> None:
            if origin is not None:
                super().__init__(parent=parent, origin=origin)
            else:
                super().__init__(parent=parent)
            self.preferred_charge_mode: EnumAttribute = EnumAttribute("preferred_charge_mode", parent=self, tags={'connector_custom'})
            self.available_charge_modes: StringAttribute = StringAttribute("available_charge_modes", parent=self, tags={'connector_custom'})
            self.charging_care_mode: EnumAttribute = EnumAttribute("charging_care_mode", parent=self, tags={'connector_custom'})
            self.battery_support: EnumAttribute = EnumAttribute("battery_support", parent=self, tags={'connector_custom'})

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
        CONSERVING = 'conserving'
        CHARGE_PURPOSE_REACHED_NOT_CONSERVATION_CHARGING = 'chargePurposeReachedAndNotConservationCharging'
        CHARGE_PURPOSE_REACHED_CONSERVATION = 'chargePurposeReachedAndConservation'
        CHARGING = 'charging'
        ERROR = 'error'
        UNSUPPORTED = 'unsupported'
        DISCHARGING = 'discharging'
        UNKNOWN = 'unknown charging state'

    class SkodaChargeMode(Enum,):
        """
        Enum class representing different Skoda charge modes.

        Attributes:
            HOME_STORAGE_CHARGING (str): Charge mode for home storage charging.
            IMMEDIATE_DISCHARGING (str): Charge mode for immediate discharging.
            ONLY_OWN_CURRENT (str): Charge mode for using only own current.
            PREFERRED_CHARGING_TIMES (str): Charge mode for preferred charging times.
            TIMER_CHARGING_WITH_CLIMATISATION (str): Charge mode for timer charging with climatisation.
            TIMER (str): Charge mode for timer-based charging.
            MANUAL (str): Charge mode for manual charging.
            OFF (str): Charge mode for turning off charging.
        """
        HOME_STORAGE_CHARGING = 'HOME_STORAGE_CHARGING'
        IMMEDIATE_DISCHARGING = 'IMMEDIATE_DISCHARGING'
        ONLY_OWN_CURRENT = 'ONLY_OWN_CURRENT'
        PREFERRED_CHARGING_TIMES = 'PREFERRED_CHARGING_TIMES'
        TIMER_CHARGING_WITH_CLIMATISATION = 'TIMER_CHARGING_WITH_CLIMATISATION'
        TIMER = 'timer'
        MANUAL = 'manual'
        OFF = 'off'
        UNKNOWN = 'unknown charge mode'

    class SkodaChargingCareMode(Enum,):
        """
        Enum representing the charging care mode for Skoda vehicles.
        """
        ACTIVATED = 'ACTIVATED'
        DEACTIVATED = 'DEACTIVATED'
        UNKNOWN = 'UNKNOWN'

    class SkodaBatterySupport(Enum,):
        """
        SkodaBatterySupport is an enumeration that represents the different states of battery support for Skoda vehicles.
        """
        ENABLED = 'ENABLED'
        DISABLED = 'DISABLED'
        NOT_ALLOWED = 'NOT_ALLOWED'
        UNKNOWN = 'UNKNOWN'


# Mapping of Skoda charging states to generic charging states
mapping_skoda_charging_state: Dict[SkodaCharging.SkodaChargingState, Charging.ChargingState] = {
    SkodaCharging.SkodaChargingState.OFF: Charging.ChargingState.OFF,
    SkodaCharging.SkodaChargingState.CONNECT_CABLE: Charging.ChargingState.OFF,
    SkodaCharging.SkodaChargingState.READY_FOR_CHARGING: Charging.ChargingState.READY_FOR_CHARGING,
    SkodaCharging.SkodaChargingState.NOT_READY_FOR_CHARGING: Charging.ChargingState.OFF,
    SkodaCharging.SkodaChargingState.CONSERVING: Charging.ChargingState.CONSERVATION,
    SkodaCharging.SkodaChargingState.CHARGE_PURPOSE_REACHED_NOT_CONSERVATION_CHARGING: Charging.ChargingState.READY_FOR_CHARGING,
    SkodaCharging.SkodaChargingState.CHARGE_PURPOSE_REACHED_CONSERVATION: Charging.ChargingState.CONSERVATION,
    SkodaCharging.SkodaChargingState.CHARGING: Charging.ChargingState.CHARGING,
    SkodaCharging.SkodaChargingState.ERROR: Charging.ChargingState.ERROR,
    SkodaCharging.SkodaChargingState.UNSUPPORTED: Charging.ChargingState.UNSUPPORTED,
    SkodaCharging.SkodaChargingState.DISCHARGING: Charging.ChargingState.DISCHARGING,
    SkodaCharging.SkodaChargingState.UNKNOWN: Charging.ChargingState.UNKNOWN
}
