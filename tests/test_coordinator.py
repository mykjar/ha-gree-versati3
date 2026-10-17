"""Tests for coordinator polling of setpoints and measured temperatures."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.gree_versati.client import GreeVersatiClientError
from custom_components.gree_versati.constants import (
    MEASUREMENT_POLL_KEYS,
    MEASUREMENT_RETRY_BACKOFF_SECONDS,
    POLL_KEYS,
    SETTING_POLL_KEYS,
    STATUS_POLL_KEYS,
)
from custom_components.gree_versati.coordinator import GreeVersatiCoordinator

MAIN_VALUES = {"HeWatOutTemSet": 35, "Pow": 1, "Mod": 1, "AllErr": 0}
MEASUREMENT_VALUES = {"AllOutWatTemHi": 132, "AllOutWatTemLo": 2}
SETTING_VALUES = {"HetHtWter": 1}
STATUS_VALUES = {"SvSt": 0, "SyAnFroRunSta": 0}


class FakeClient:
    """Client double that answers per requested key set."""

    def __init__(self, *, measurements: dict[str, Any] | Exception) -> None:
        self.measurements = measurements
        self.settings: dict[str, Any] | Exception = SETTING_VALUES
        self.status: dict[str, Any] | Exception = STATUS_VALUES
        self.main: dict[str, Any] | Exception = MAIN_VALUES
        self.calls: list[list[str]] = []

    async def async_get(self, keys: list[str]) -> dict[str, Any]:
        self.calls.append(keys)
        if keys == list(MEASUREMENT_POLL_KEYS):
            result = self.measurements
        elif keys == list(SETTING_POLL_KEYS):
            result = self.settings
        elif keys == list(STATUS_POLL_KEYS):
            result = self.status
        else:
            result = self.main
        if isinstance(result, Exception):
            raise result
        return dict(result)


def _measurement_calls(client: FakeClient) -> int:
    return sum(1 for keys in client.calls if keys == list(MEASUREMENT_POLL_KEYS))


async def test_update_merges_measurements(hass: HomeAssistant) -> None:
    client = FakeClient(measurements=MEASUREMENT_VALUES)
    coordinator = GreeVersatiCoordinator(hass, client, 30)

    data = await coordinator._async_update_data()

    assert data == {**MAIN_VALUES, **MEASUREMENT_VALUES, **SETTING_VALUES}
    assert client.calls == [
        list(POLL_KEYS),
        list(MEASUREMENT_POLL_KEYS),
        list(SETTING_POLL_KEYS),
    ]


async def test_measurement_failure_does_not_fail_update(hass: HomeAssistant) -> None:
    client = FakeClient(measurements=GreeVersatiClientError("no values"))
    coordinator = GreeVersatiCoordinator(hass, client, 30)

    data = await coordinator._async_update_data()

    assert data == {**MAIN_VALUES, **SETTING_VALUES}


async def test_measurement_failure_backs_off(hass: HomeAssistant) -> None:
    client = FakeClient(measurements=GreeVersatiClientError("no values"))
    coordinator = GreeVersatiCoordinator(hass, client, 30)
    module = "custom_components.gree_versati.coordinator.time.monotonic"

    with patch(module, return_value=1000.0):
        await coordinator._async_update_data()
    with patch(module, return_value=1000.0 + MEASUREMENT_RETRY_BACKOFF_SECONDS - 1):
        await coordinator._async_update_data()
    assert _measurement_calls(client) == 1

    client.measurements = MEASUREMENT_VALUES
    with patch(module, return_value=1000.0 + MEASUREMENT_RETRY_BACKOFF_SECONDS):
        data = await coordinator._async_update_data()
    assert _measurement_calls(client) == 2
    assert data["AllOutWatTemHi"] == 132


async def test_main_poll_failure_raises(hass: HomeAssistant) -> None:
    client = FakeClient(measurements=MEASUREMENT_VALUES)
    client.main = GreeVersatiClientError("timeout")
    coordinator = GreeVersatiCoordinator(hass, client, 30)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert _measurement_calls(client) == 0


async def test_status_flags_are_polled_only_when_asked_for(hass: HomeAssistant) -> None:
    client = FakeClient(measurements=MEASUREMENT_VALUES)
    coordinator = GreeVersatiCoordinator(hass, client, 30, (STATUS_POLL_KEYS,))

    data = await coordinator._async_update_data()

    assert data == {**MAIN_VALUES, **MEASUREMENT_VALUES, **SETTING_VALUES, **STATUS_VALUES}
    assert client.calls == [
        list(POLL_KEYS),
        list(MEASUREMENT_POLL_KEYS),
        list(SETTING_POLL_KEYS),
        list(STATUS_POLL_KEYS),
    ]


async def test_unsupported_status_flags_keep_measurements(hass: HomeAssistant) -> None:
    client = FakeClient(measurements=MEASUREMENT_VALUES)
    client.status = GreeVersatiClientError("no values")
    coordinator = GreeVersatiCoordinator(hass, client, 30, (STATUS_POLL_KEYS,))

    await coordinator._async_update_data()
    data = await coordinator._async_update_data()

    # The status request backs off on its own; measurements carry on.
    assert data == {**MAIN_VALUES, **MEASUREMENT_VALUES, **SETTING_VALUES}
    assert client.calls.count(list(STATUS_POLL_KEYS)) == 1
    assert _measurement_calls(client) == 2


async def test_unsupported_priority_keeps_measurements(hass: HomeAssistant) -> None:
    client = FakeClient(measurements=MEASUREMENT_VALUES)
    client.settings = GreeVersatiClientError("no values")
    coordinator = GreeVersatiCoordinator(hass, client, 30)

    data = await coordinator._async_update_data()

    assert data == {**MAIN_VALUES, **MEASUREMENT_VALUES}
