"""
Module for climatization for skoda vehicles.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from carconnectivity.climatization import Climatization
from carconnectivity.objects import GenericObject
from carconnectivity.vehicle import GenericVehicle

from carconnectivity_connectors.skoda.error import Error

if TYPE_CHECKING:
    from typing import Optional, Dict


class SkodaClimatization(Climatization):  # pylint: disable=too-many-instance-attributes
    """
    SkodaClimatization class for handling Skoda vehicle climatization information.

    This class extends the Climatization class and includes an enumeration of various
    charging states specific to Skoda vehicles.
    """
    def __init__(self, vehicle: GenericVehicle | None = None, origin: Optional[Climatization] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
            if not isinstance(self.settings, SkodaClimatization.Settings):
                self.settings: Climatization.Settings = SkodaClimatization.Settings(parent=self, origin=origin.settings)
            self.settings.parent = self
        else:
            super().__init__(vehicle=vehicle)
            self.settings: Climatization.Settings = SkodaClimatization.Settings(parent=self)
        self.errors: Dict[str, Error] = {}
        # Add custom Skoda-specific attributes
        from carconnectivity.attributes import GenericAttribute
        from carconnectivity.objects import GenericObject
        self.running_requests: GenericAttribute = GenericAttribute(name='running_requests', parent=self)
        
        # Add active ventilation timers support
        if hasattr(origin, "active_ventilation_timers"):
            self.active_ventilation_timers: SkodaClimatization.ActiveVentilationTimers = origin.active_ventilation_timers
            self.active_ventilation_timers.parent = self
        else:
            self.active_ventilation_timers: SkodaClimatization.ActiveVentilationTimers = SkodaClimatization.ActiveVentilationTimers(parent=self)



    class ActiveVentilationTimers(GenericObject):
        """
        This class represents the active ventilation timers for Skoda car climatization.
        """

        def __init__(self, parent: Optional[GenericObject] = None) -> None:
            super().__init__(object_id="active_ventilation_timers", parent=parent)

            # Raw timer data from API - will be updated with actual timer list
            from carconnectivity.attributes import GenericAttribute
            self.raw_data = GenericAttribute("raw_data", self, value=None, tags={"connector_custom"})

            # Individual timer objects will be created dynamically based on API response
            # Example: self.timer_1, self.timer_2, etc.

        def update_timers(self, timers_data: list, captured_at) -> None:
            """Update timer data from API response"""
            # Store raw data
            self.raw_data._set_value(value=timers_data, measured=captured_at)

            # Clear existing timer attributes properly by removing parent relationship first
            existing_timers = [attr for attr in dir(self) if attr.startswith("timer_") and not attr.startswith("_")]
            for timer_attr in existing_timers:
                try:
                    timer_obj = getattr(self, timer_attr)
                    # Remove parent relationship first - this removes it from parent's children list
                    timer_obj.parent = None
                    # Now delete the attribute
                    delattr(self, timer_attr)
                except (AttributeError, TypeError):
                    pass

            # Create individual timer objects
            for timer in timers_data:
                if "id" in timer:
                    timer_id = timer["id"]
                    timer_attr_name = f"timer_{timer_id}"

                    # Create a GenericObject for this timer
                    timer_obj = GenericObject(object_id=timer_attr_name, parent=self)

                    # Add timer properties
                    from carconnectivity.attributes import BooleanAttribute, StringAttribute, GenericAttribute
                    timer_obj.timer_enabled = BooleanAttribute(
                        "timer_enabled", timer_obj, value=timer.get("enabled", False), tags={"connector_custom"}
                    )
                    timer_obj.time = StringAttribute(
                        "time", timer_obj, value=timer.get("time", "00:00"), tags={"connector_custom"}
                    )
                    timer_obj.type = StringAttribute(
                        "type", timer_obj, value=timer.get("type", "ONE_OFF"), tags={"connector_custom"}
                    )
                    timer_obj.selected_days = GenericAttribute(
                        "selected_days", timer_obj, value=timer.get("selectedDays", []), tags={"connector_custom"}
                    )

                    # Set the timer object as an attribute of this Timers object
                    setattr(self, timer_attr_name, timer_obj)

    class Settings(Climatization.Settings):
        """
        This class represents the settings for a skoda car climatiation.
        """
        def __init__(self, parent: Optional[GenericObject] = None, origin: Optional[Climatization.Settings] = None) -> None:
            if origin is not None:
                super().__init__(parent=parent, origin=origin)
            else:
                super().__init__(parent=parent)
