"""Coordinator for Gree Versati integration."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
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
    SETTING_POLL_KEYS,
)

_LOGGER = logging.getLogger(__name__)


class GreeVersatiCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch data from Gree Versati device periodically."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: GreeVersatiProtocolClient,
        scan_interval_seconds: int,
        extra_poll_groups: Sequence[tuple[str, ...]] = (),
    ) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval_seconds),
        )
        self.client = client
        # Each group is its own request, so keys one firmware doesn't report
        # can't break the others.
        self._optional_groups = (MEASUREMENT_POLL_KEYS, SETTING_POLL_KEYS, *extra_poll_groups)
        self._retry_at = dict.fromkeys(self._optional_groups, 0.0)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = dict(await self.client.async_get(list(POLL_KEYS)))
        except GreeVersatiClientError as err:
            raise UpdateFailed(str(err)) from err

        for keys in self._optional_groups:
            data.update(await self._async_fetch_optional(keys))
        return data

    async def _async_fetch_optional(self, keys: tuple[str, ...]) -> dict[str, Any]:
        """Fetch values some firmware may not report, without failing the update.

        If the device does not answer this request (older firmware, unknown
        keys), the entities using these values become unavailable and the
        request is retried after a back-off, so a slow failure cannot stall
        every poll.
        """
        now = time.monotonic()
        if now < self._retry_at[keys]:
            return {}

        try:
            return await self.client.async_get(list(keys))
        except GreeVersatiClientError as err:
            self._retry_at[keys] = now + MEASUREMENT_RETRY_BACKOFF_SECONDS
            _LOGGER.debug(
                "Values %s not available, retrying in %s s: %s",
                keys,
                MEASUREMENT_RETRY_BACKOFF_SECONDS,
                err,
            )
            return {}
