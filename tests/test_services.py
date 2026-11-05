"""Tests for the raw parameter actions, set_param and get_params."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized

from custom_components.gree_versati.client import GreeVersatiProtocolClient
from custom_components.gree_versati.constants import DOMAIN

DEVICE_ID = "502cc62fc0be"


@pytest.fixture
def writes() -> Iterator[list[dict[str, Any]]]:
    """Every write sent to the pump, in order."""
    values: dict[str, Any] = {
        "HeWatOutTemSet": 35,
        "Pow": 1,
        "Mod": 4,
        "AllErr": 0,
        "TemUn": 0,
    }
    sent: list[dict[str, Any]] = []

    async def get(self: GreeVersatiProtocolClient, keys: list[str]) -> dict[str, Any]:
        return {key: values[key] for key in keys if key in values}

    async def set_(self: GreeVersatiProtocolClient, params: dict[str, Any]) -> None:
        sent.append(dict(params))
        values.update(params)

    with (
        patch.object(GreeVersatiProtocolClient, "async_get", get),
        patch.object(GreeVersatiProtocolClient, "async_set", set_),
    ):
        yield sent


@pytest.fixture
async def setup_pump(hass: HomeAssistant, writes: list[dict[str, Any]]) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        data={
            "host": "192.0.2.10",
            "port": 7000,
            "device_id": DEVICE_ID,
            "key": "1234567890abcdef",
            "timeout": 5,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def non_admin_user(hass: HomeAssistant) -> MockUser:
    """A logged-in user outside the admin group."""
    user = MockUser().add_to_hass(hass)
    assert not user.is_admin
    return user


async def _set_param(hass: HomeAssistant, value: Any, context: Context | None = None) -> None:
    await hass.services.async_call(
        DOMAIN,
        "set_param",
        {"key": "HeWatOutTemSet", "value": value},
        blocking=True,
        context=context,
    )


@pytest.mark.usefixtures("setup_pump")
async def test_admin_can_set_a_parameter(
    hass: HomeAssistant, writes: list[dict[str, Any]], hass_admin_user: MockUser
) -> None:
    await _set_param(hass, "40", Context(user_id=hass_admin_user.id))

    assert writes == [{"HeWatOutTemSet": 40}]


@pytest.mark.usefixtures("setup_pump")
async def test_automation_without_a_user_can_set_a_parameter(
    hass: HomeAssistant, writes: list[dict[str, Any]]
) -> None:
    await _set_param(hass, "40", Context())

    assert writes == [{"HeWatOutTemSet": 40}]


@pytest.mark.usefixtures("setup_pump")
async def test_non_admin_cannot_set_a_parameter(
    hass: HomeAssistant, writes: list[dict[str, Any]], non_admin_user: MockUser
) -> None:
    with pytest.raises(Unauthorized):
        await _set_param(hass, "40", Context(user_id=non_admin_user.id))

    assert writes == []


@pytest.mark.usefixtures("setup_pump")
async def test_non_admin_can_read_parameters(
    hass: HomeAssistant, non_admin_user: MockUser
) -> None:
    response = await hass.services.async_call(
        DOMAIN,
        "get_params",
        {"keys": ["HeWatOutTemSet"]},
        blocking=True,
        context=Context(user_id=non_admin_user.id),
        return_response=True,
    )

    assert response == {"devices": {DEVICE_ID: {"HeWatOutTemSet": 35}}}


@pytest.mark.usefixtures("setup_pump")
@pytest.mark.parametrize(
    ("text", "sent"),
    [
        ("35.0", 35),
        (" 35.0 ", 35),
        ("1.5", 1.5),
        ("-0.5", -0.5),
        ("35", 35),
        (" 35 ", 35),
        ("true", True),
        ("FALSE", False),
        ("auto", "auto"),
        ("nan", "nan"),
        ("inf", "inf"),
        ("1e3", "1e3"),
    ],
)
async def test_text_values_are_sent_as_their_type(
    hass: HomeAssistant, writes: list[dict[str, Any]], text: str, sent: Any
) -> None:
    await _set_param(hass, text)

    [written] = writes
    value = written["HeWatOutTemSet"]
    assert value == sent
    assert type(value) is type(sent)
