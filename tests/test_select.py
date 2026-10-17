"""Tests for the heat + hot water priority choice."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.select import (
    ATTR_OPTION,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.gree_versati.client import GreeVersatiProtocolClient
from custom_components.gree_versati.constants import DOMAIN

DEVICE_ID = "502cc62fc0be"


@pytest.fixture
def pump() -> Iterator[dict[str, Any]]:
    """The pump's parameters; writes land here too."""
    values: dict[str, Any] = {
        "HeWatOutTemSet": 35,
        "Pow": 1,
        "Mod": 4,
        "AllErr": 0,
        "TemUn": 0,
        "HetHtWter": 1,
    }

    async def get(self: GreeVersatiProtocolClient, keys: list[str]) -> dict[str, Any]:
        return {key: values[key] for key in keys if key in values}

    async def set_(self: GreeVersatiProtocolClient, params: dict[str, Any]) -> None:
        values.update(params)

    with (
        patch.object(GreeVersatiProtocolClient, "async_get", get),
        patch.object(GreeVersatiProtocolClient, "async_set", set_),
    ):
        yield values


async def _setup(hass: HomeAssistant) -> str:
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
    entity_id = er.async_get(hass).async_get_entity_id("select", DOMAIN, f"{DEVICE_ID}_HetHtWter")
    assert entity_id is not None
    return entity_id


async def test_shows_the_priority(hass: HomeAssistant, pump: dict[str, Any]) -> None:
    entity_id = await _setup(hass)

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "hot_water"
    assert state.attributes["options"] == ["heating", "hot_water"]
    registry_entry = er.async_get(hass).async_get(entity_id)
    assert registry_entry is not None
    assert registry_entry.entity_category is EntityCategory.CONFIG


async def test_changing_the_priority_sets_the_pump(
    hass: HomeAssistant, pump: dict[str, Any]
) -> None:
    entity_id = await _setup(hass)

    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: "heating"},
        blocking=True,
    )

    assert pump["HetHtWter"] == 0
    assert hass.states.get(entity_id).state == "heating"


@pytest.mark.parametrize("reported", [None, "", 2])
async def test_unavailable_without_a_known_priority(
    hass: HomeAssistant, pump: dict[str, Any], reported: Any
) -> None:
    if reported is None:
        del pump["HetHtWter"]
    else:
        pump["HetHtWter"] = reported

    entity_id = await _setup(hass)

    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
