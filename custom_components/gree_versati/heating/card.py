"""Serves the floor heating dashboard card from the integration itself."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers.start import async_at_started
from homeassistant.loader import async_get_integration

from ..constants import DOMAIN

CARD_URL: Final = f"/{DOMAIN}/floor-heating-card.js"
CARD_PATH: Final = Path(__file__).parent / "www" / "floor-heating-card.js"
_REGISTERED: Final = f"{DOMAIN}_floor_heating_card"

_LOGGER = logging.getLogger(__name__)


async def async_register_card(hass: HomeAssistant) -> None:
    """Load the card on every dashboard, from as early as possible."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    if _can_serve(hass):
        # Home Assistant may still be starting, but its web server already
        # serves dashboards, and they must find the card.
        await _async_register(hass)
    else:
        async_at_started(hass, _async_register)


def _can_serve(hass: HomeAssistant) -> bool:
    return hass.http is not None and "frontend" in hass.config.components


async def _async_register(hass: HomeAssistant) -> None:
    # Without the web server or the frontend there is nothing to serve.
    if not _can_serve(hass):
        return
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(CARD_PATH), cache_headers=False)]
        )
        integration = await async_get_integration(hass, DOMAIN)
        # Each version gets its own URL, so browsers don't keep an old card.
        add_extra_js_url(hass, f"{CARD_URL}?v={integration.version}")
    except Exception:  # A missing card must never stop floor heating control.
        _LOGGER.exception("Could not load the floor heating dashboard card")
