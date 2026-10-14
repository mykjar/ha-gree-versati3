"""Serves the floor heating dashboard card from the integration itself."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.start import async_at_started
from homeassistant.loader import async_get_integration

from ..constants import DOMAIN

CARD_URL: Final = f"/{DOMAIN}/floor-heating-card.js"
CARD_PATH: Final = Path(__file__).parent / "www" / "floor-heating-card.js"
_REGISTERED: Final = f"{DOMAIN}_floor_heating_card"


@callback
def async_register_card(hass: HomeAssistant) -> None:
    """Load the card on every dashboard, once Home Assistant has started."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True

    async def _async_register(hass: HomeAssistant) -> None:
        # Without the web server or the frontend there is nothing to serve.
        if hass.http is None or "frontend" not in hass.config.components:
            return
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(CARD_PATH), cache_headers=False)]
        )
        integration = await async_get_integration(hass, DOMAIN)
        # Each version gets its own URL, so browsers don't keep an old card.
        add_extra_js_url(hass, f"{CARD_URL}?v={integration.version}")

    async_at_started(hass, _async_register)
