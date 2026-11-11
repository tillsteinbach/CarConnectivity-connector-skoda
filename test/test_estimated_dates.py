"""Tests for vehicle completion estimates."""
from datetime import datetime, timezone

import pytest

from carconnectivity.garage import Garage
from carconnectivity_connectors.skoda.connector import Connector
from carconnectivity_connectors.skoda.vehicle import SkodaElectricVehicle, SkodaVehicle


@pytest.mark.parametrize('state', ['HEATING', 'HEATING_AUXILIARY', 'COOLING'])
def test_air_conditioning_estimate(state):
    vehicle = SkodaVehicle(vin='TESTVIN1234567890', garage=Garage(parent=None))
    connector = Connector.__new__(Connector)

    connector._update_air_conditioning(vehicle, {'airConditioning': {  # pylint: disable=protected-access
        'state': state,
        'carCapturedTimestamp': '2026-06-12T07:00:00Z',
        'estimatedReachOfTargetTemperatureAt': '2026-06-12T09:30:00+02:00',
    }})

    assert vehicle.climatization.estimated_date_reached.value == datetime(2026, 6, 12, 7, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize('estimate', [None, 'invalid'])
def test_air_conditioning_clears_unavailable_estimate(estimate, caplog):
    vehicle = SkodaVehicle(vin='TESTVIN1234567890', garage=Garage(parent=None))
    connector = Connector.__new__(Connector)
    connector._update_air_conditioning(vehicle, {'airConditioning': {  # pylint: disable=protected-access
        'carCapturedTimestamp': '2026-06-12T07:00:00Z',
        'estimatedReachOfTargetTemperatureAt': '2026-06-12T07:30:00Z',
    }})
    connector._update_air_conditioning(vehicle, {'airConditioning': {  # pylint: disable=protected-access
        'carCapturedTimestamp': '2026-06-12T07:01:00Z',
        'estimatedReachOfTargetTemperatureAt': estimate,
    }})

    assert vehicle.climatization.estimated_date_reached.value is None
    if estimate == 'invalid':
        assert 'Could not parse estimatedReachOfTargetTemperatureAt' in caplog.text


@pytest.mark.parametrize(('status', 'expected'), [
    ({'fullyChargedAt': '2026-06-12T08:00:00Z', 'remainingTimeToFullyChargedInMinutes': 15},
     datetime(2026, 6, 12, 8, 0, tzinfo=timezone.utc)),
    ({'remainingTimeToFullyChargedInMinutes': 15}, datetime(2026, 6, 12, 7, 15, tzinfo=timezone.utc)),
    ({'fullyChargedAt': 'invalid', 'remainingTimeToFullyChargedInMinutes': 15},
     datetime(2026, 6, 12, 7, 15, tzinfo=timezone.utc)),
    ({}, None),
])
def test_charging_estimate(status, expected):
    vehicle = SkodaElectricVehicle(vin='TESTVIN1234567890', garage=Garage(parent=None))
    connector = Connector.__new__(Connector)

    connector._update_charging(vehicle, {'charging': {  # pylint: disable=protected-access
        'carCapturedTimestamp': '2026-06-12T07:00:20Z',
        'status': status,
    }})

    assert vehicle.charging.estimated_date_reached.value == expected
