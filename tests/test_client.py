"""Tests for the protocol client."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from custom_components.gree_versati.client import (
    ENCRYPTION_ECB,
    ENCRYPTION_GCM,
    GreeVersatiClientError,
    GreeVersatiProtocolClient,
)

from .fake_pump import (
    DEVICE_ID,
    ECB,
    FOREIGN_IP,
    GCM,
    KEY,
    PUMP_IP,
    PUMP_PORT,
    WirePump,
    bad_tag,
    garbled,
    not_base64,
    plaintext,
    seal,
    status_reply,
)

PUMP_VALUES = {"HeWatOutTemSet": 30, "Pow": 1}


def make_client(
    pump: WirePump, *, host: str = PUMP_IP, timeout: float = 0.05, retries: int = 0
) -> GreeVersatiProtocolClient:
    return GreeVersatiProtocolClient(
        host=host,
        port=PUMP_PORT,
        device_id=DEVICE_ID,
        key=KEY,
        timeout=timeout,
        retries=retries,
        socket_factory=pump.socket,
    )


Operation = Callable[[GreeVersatiProtocolClient], Awaitable[Any]]


async def read(client: GreeVersatiProtocolClient) -> Any:
    return await client.async_get(["HeWatOutTemSet", "Pow"])


async def write(client: GreeVersatiProtocolClient) -> Any:
    return await client.async_set({"HeWatOutTemSet": 28})


READ_AND_WRITE = pytest.mark.parametrize("operation", [read, write], ids=["read", "write"])


def sent(pump: WirePump) -> list[tuple[str, tuple[str, ...]]]:
    """Each request the pump received, as its encryption and the fields it carried."""
    return [(encryption, tuple(sorted(body))) for encryption, body in pump.received]


def test_validate_key_string_accepts_16_bytes() -> None:
    assert GreeVersatiProtocolClient.validate_key_string("1234567890abcdef")


def test_validate_key_string_rejects_non_16_bytes() -> None:
    assert not GreeVersatiProtocolClient.validate_key_string("short")


def test_extract_values_from_opt_and_p() -> None:
    payload = {"opt": ["Pow", "Mod"], "p": [1, 2]}
    values = GreeVersatiProtocolClient._extract_values(payload)
    assert values == {"Pow": 1, "Mod": 2}


def test_extract_values_from_cols_and_dat() -> None:
    payload = {"cols": ["HeWatOutTemSet", "Pow"], "dat": [28, 1]}
    values = GreeVersatiProtocolClient._extract_values(payload)
    assert values == {"HeWatOutTemSet": 28, "Pow": 1}


def test_encrypt_decrypt_roundtrip_ecb() -> None:
    client = GreeVersatiProtocolClient(
        host="127.0.0.1",
        port=7000,
        device_id="502cc62fc0be",
        key="1234567890abcdef",
        timeout=5,
        retries=0,
    )
    payload = {"t": "status", "cols": ["Pow"], "mac": "502cc62fc0be"}
    packed, tag = client._encrypt_pack(payload, ENCRYPTION_ECB)
    unpacked = client._decrypt_pack(packed, tag, ENCRYPTION_ECB)
    assert unpacked == payload


def test_encrypt_decrypt_roundtrip_gcm() -> None:
    client = GreeVersatiProtocolClient(
        host="127.0.0.1",
        port=7000,
        device_id="502cc62fc0be",
        key="1234567890abcdef",
        timeout=5,
        retries=0,
    )
    payload = {"t": "status", "cols": ["Pow"], "mac": "502cc62fc0be"}
    packed, tag = client._encrypt_pack(payload, ENCRYPTION_GCM)
    assert tag is not None
    unpacked = client._decrypt_pack(packed, tag, ENCRYPTION_GCM)
    assert unpacked == payload


@pytest.mark.parametrize("encryption", [ECB, GCM])
async def test_reads_values_from_the_pump(encryption: str) -> None:
    pump = WirePump(encryption=encryption, values=PUMP_VALUES)

    values = await make_client(pump).async_get(["HeWatOutTemSet", "Pow"])

    assert values == {"HeWatOutTemSet": 30, "Pow": 1}


@pytest.mark.parametrize("encryption", [ECB, GCM])
async def test_writes_values_to_the_pump(encryption: str) -> None:
    pump = WirePump(encryption=encryption, values=PUMP_VALUES)

    await make_client(pump).async_set({"HeWatOutTemSet": 28})

    assert pump.values["HeWatOutTemSet"] == 28


async def test_a_write_the_pump_rejects_raises_the_pumps_error() -> None:
    pump = WirePump(values=PUMP_VALUES)
    pump.write_status = 400

    # Not the silence of the encryption the pump doesn't speak, tried after.
    with pytest.raises(GreeVersatiClientError, match="error code: 400"):
        await make_client(pump).async_set({"HeWatOutTemSet": 28})


async def test_ignores_a_reply_from_another_address_and_waits_for_the_pump() -> None:
    pump = WirePump(values=PUMP_VALUES)
    forged = seal(status_reply({"HeWatOutTemSet": 55, "Pow": 0}))
    pump.ahead = [(forged, (FOREIGN_IP, PUMP_PORT))]

    values = await make_client(pump).async_get(["HeWatOutTemSet", "Pow"])

    assert values == {"HeWatOutTemSet": 30, "Pow": 1}


async def test_chatter_from_another_address_does_not_stretch_the_timeout() -> None:
    pump = WirePump(values=PUMP_VALUES)
    pump.silent = True
    pump.chatter = (seal(status_reply({"Pow": 0})), (FOREIGN_IP, PUMP_PORT))

    # 8 requests (2 encryptions x 4 formats) of 0.05 s each: well under 5 s.
    async with asyncio.timeout(5):
        with pytest.raises(GreeVersatiClientError):
            await make_client(pump).async_get(["Pow"])


async def test_a_host_name_is_resolved_to_the_pumps_address() -> None:
    pump = WirePump(values=PUMP_VALUES, ip="127.0.0.1")

    values = await make_client(pump, host="localhost").async_get(["Pow"])

    assert values == {"Pow": 1}


async def test_a_plaintext_status_reply_is_not_used() -> None:
    pump = WirePump(values=PUMP_VALUES)
    pump.silent = True
    pump.ahead = [(plaintext(status_reply({"Pow": 0})), pump.address)]

    with pytest.raises(GreeVersatiClientError):
        await make_client(pump).async_get(["Pow"])


async def test_a_plaintext_success_reply_does_not_confirm_a_write() -> None:
    pump = WirePump(values=PUMP_VALUES)
    pump.silent = True
    pump.ahead = [(plaintext({"t": "res", "r": 200}), pump.address)]

    with pytest.raises(GreeVersatiClientError):
        await make_client(pump).async_set({"HeWatOutTemSet": 28})


UNDECRYPTABLE = pytest.mark.parametrize(
    ("encryption", "tamper"),
    [(GCM, bad_tag), (GCM, garbled), (ECB, garbled), (ECB, not_base64)],
    ids=["gcm-bad-tag", "gcm-garbled", "ecb-garbled", "ecb-not-base64"],
)


@UNDECRYPTABLE
async def test_a_reply_that_fails_to_decrypt_is_not_used_as_pump_data(
    encryption: str, tamper: Callable[[bytes], bytes]
) -> None:
    pump = WirePump(encryption=encryption, values=PUMP_VALUES)
    pump.tamper = tamper

    with pytest.raises(GreeVersatiClientError):
        await make_client(pump).async_get(["Pow"])


@UNDECRYPTABLE
async def test_a_reply_that_fails_to_decrypt_does_not_confirm_a_write(
    encryption: str, tamper: Callable[[bytes], bytes]
) -> None:
    pump = WirePump(encryption=encryption, values=PUMP_VALUES)
    pump.tamper = tamper

    with pytest.raises(GreeVersatiClientError):
        await make_client(pump).async_set({"HeWatOutTemSet": 28})


@pytest.mark.parametrize(
    ("operation", "combinations"),
    [(read, 2 * 4), (write, 2 * 3)],
    ids=["read", "write"],
)
async def test_a_pump_that_never_answers_gets_each_combination_once(
    operation: Operation, combinations: int
) -> None:
    pump = WirePump(values=PUMP_VALUES)
    pump.silent = True

    with pytest.raises(GreeVersatiClientError):
        await operation(make_client(pump, retries=2))

    # Every request to a silent pump waits out one timeout: with the defaults
    # that's 8 x 5 s = 40 s for a read, rather than 3 tries of each (2 minutes).
    assert len(sent(pump)) == combinations
    assert len(set(sent(pump))) == combinations


# The last format of each kind is the only one without ``mac`` (reads) or the
# only one with ``uid`` (writes): a pump that knows nothing else needs every
# fallback the client has.
ONLY_LAST_FORMAT = pytest.mark.parametrize(
    ("operation", "understands"),
    [(read, lambda request: "mac" not in request), (write, lambda request: "uid" in request)],
    ids=["read", "write"],
)


@ONLY_LAST_FORMAT
async def test_the_combination_that_worked_is_tried_first_from_then_on(
    operation: Operation, understands: Callable[[dict[str, Any]], bool]
) -> None:
    pump = WirePump(encryption=GCM, values=PUMP_VALUES)
    pump.understands = understands
    client = make_client(pump)
    await operation(client)
    answered = sent(pump)[-1]
    pump.received.clear()

    await operation(client)

    assert answered[0] == GCM
    assert sent(pump) == [answered]


@READ_AND_WRITE
async def test_a_pump_gone_silent_fails_within_one_round_of_attempts(
    operation: Operation,
) -> None:
    pump = WirePump(values=PUMP_VALUES)
    client = make_client(pump, retries=2)
    await operation(client)
    answered = sent(pump)[-1]
    pump.received.clear()
    pump.silent = True

    with pytest.raises(GreeVersatiClientError):
        await operation(client)

    # The working combination, 1 try + 2 retries: 3 x 5 s = 15 s with the defaults.
    assert sent(pump) == [answered] * 3


async def test_a_reply_with_no_values_moves_on_to_the_other_formats() -> None:
    pump = WirePump(values=PUMP_VALUES)
    client = make_client(pump, retries=2)
    await read(client)
    pump.answers_empty = lambda request: "cols" in request

    values = await read(client)

    assert values == {"HeWatOutTemSet": 30, "Pow": 1}


@READ_AND_WRITE
async def test_a_reply_that_wont_decrypt_moves_on_to_the_other_encryption(
    operation: Operation,
) -> None:
    pump = WirePump(values=PUMP_VALUES)
    client = make_client(pump, retries=2)
    await operation(client)
    # After a firmware update the pump speaks GCM, and answers what it can't
    # read with an error the client can't decrypt.
    pump.encryption = GCM
    pump.answers_unreadable = True
    pump.received.clear()

    await operation(client)

    assert sent(pump)[-1][0] == GCM
