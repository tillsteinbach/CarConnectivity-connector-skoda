"""This module defines the classes that represent attributes in the CarConnectivity system."""
from __future__ import annotations
from typing import TYPE_CHECKING, Dict, Union

from enum import Enum
import argparse
import logging

from carconnectivity.commands import GenericCommand
from carconnectivity.objects import GenericObject
from carconnectivity.errors import SetterError
from carconnectivity.util import ThrowingArgumentParser

if TYPE_CHECKING:
    from carconnectivity.objects import Optional

LOG: logging.Logger = logging.getLogger("carconnectivity")


class SpinCommand(GenericCommand):
    """
    LockUnlockCommand is a command class for locking and unlocking the vehicle.

    Command (Enum): Enum class representing different commands for locking.

    """
    def __init__(self, name: str = 'spin', parent: Optional[GenericObject] = None) -> None:
        super().__init__(name=name, parent=parent)

    @property
    def value(self) -> Optional[Union[str, Dict]]:
        return super().value

    @value.setter
    def value(self, new_value: Optional[Union[str, Dict]]) -> None:
        if isinstance(new_value, str):
            parser = ThrowingArgumentParser(prog='', add_help=False, exit_on_error=False)
            parser.add_argument('command', help='Command to execute', type=SpinCommand.Command,
                                choices=list(SpinCommand.Command))
            try:
                args = parser.parse_args(new_value.split(sep=' '))
            except argparse.ArgumentError as e:
                raise SetterError(f'Invalid format for SpinCommand: {e.message} {parser.format_usage()}') from e

            newvalue_dict = {}
            newvalue_dict['command'] = args.command
            new_value = newvalue_dict
        elif isinstance(new_value, dict):
            if 'command' in new_value and isinstance(new_value['command'], str):
                if new_value['command'] in SpinCommand.Command:
                    new_value['command'] = SpinCommand.Command(new_value['command'])
                else:
                    raise ValueError('Invalid value for SpinCommand. '
                                     f'Command must be one of {SpinCommand.Command}')
        if self._is_changeable:
            for hook in self._on_set_hooks:
                new_value = hook(self, new_value)
            self._set_value(new_value)
        else:
            raise TypeError('You cannot set this attribute. Attribute is not mutable.')

    class Command(Enum):
        """
        Enum class representing different commands for SPIN.

        """
        VERIFY = 'verify'

        def __str__(self) -> str:
            return self.value
