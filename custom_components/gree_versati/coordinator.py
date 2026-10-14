"""Coordinator for Gree Versati integration."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import GreeVersatiClientError, GreeVersatiProtocolClient
from .constants import (
    DOMAIN,
    MEASUREMENT_POLL_KEYS,
    MEASUREMENT_RETRY_BACKOFF_SECONDS,
    POLL_KEYS,
)

_LOGGER = logging.getLogger(__name__)


class GreeVersatiCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch data from Gree Versati device periodically."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: GreeVersatiProtocolClient,
        scan_interval_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval_seconds),
        )
        self.client = client
        self._measurements_retry_at = 0.0

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = dict(await self.client.async_get(list(POLL_KEYS)))
        except GreeVersatiClientError as err:
            raise UpdateFailed(str(err)) from err

        data.update(await self._async_fetch_measurements())
        return data

    async def _async_fetch_measurements(self) -> dict[str, Any]:
        """Fetch measured temperatures without ever failing the whole update.

        If the device does not answer this request (older firmware, unknown
        keys), the measurement sensors become unavailable and the request is
        retried after a back-off, so a slow failure cannot stall every poll.
        """
        now = time.monotonic()
        if now < self._measurements_retry_at:
            return {}

        try:
            return await self.client.async_get(list(MEASUREMENT_POLL_KEYS))
        except GreeVersatiClientError as err:
            self._measurements_retry_at = now + MEASUREMENT_RETRY_BACKOFF_SECONDS
            _LOGGER.debug(
                "Measured temperatures not available, retrying in %s s: %s",
                MEASUREMENT_RETRY_BACKOFF_SECONDS,
                err,
            )
            return {}
