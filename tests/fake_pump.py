"""A fake Versati pump on the far side of the client's UDP socket.

It speaks the wire protocol with its own crypto, so client tests check what
actually goes over the network rather than the client's own view of it.
Plug it in with ``GreeVersatiProtocolClient(..., socket_factory=pump.socket)``.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

ECB = "ECB"
GCM = "GCM"
GCM_NONCE = b"\x54\x40\x78\x44\x49\x67\x5a\x51\x6c\x5e\x63\x13"
GCM_AAD = b"qualcomm-test"

PUMP_IP = "192.0.2.10"
PUMP_PORT = 7000
FOREIGN_IP = "192.0.2.99"
DEVICE_ID = "502cc62fc0be"
KEY = "1234567890abcdef"

Address = tuple[str, int]
Datagram = tuple[bytes, Address]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _gcm() -> Any:
    gcm = AES.new(KEY.encode(), AES.MODE_GCM, nonce=GCM_NONCE)
    gcm.update(GCM_AAD)
    return gcm


def _ecb() -> Any:
    return AES.new(KEY.encode(), AES.MODE_ECB)


def seal(payload: dict[str, Any], *, encryption: str = ECB) -> bytes:
    """Return a datagram carrying ``payload`` encrypted the way a pump does it."""
    raw = json.dumps(payload).encode()
    envelope: dict[str, Any] = {"t": "pack", "i": 1, "uid": 0, "cid": DEVICE_ID, "tcid": ""}
    if encryption == GCM:
        packed, tag = _gcm().encrypt_and_digest(raw)
        envelope["pack"] = _b64(packed)
        envelope["tag"] = _b64(tag)
    else:
        envelope["pack"] = _b64(_ecb().encrypt(pad(raw, AES.block_size)))
    return json.dumps(envelope).encode()


def plaintext(payload: dict[str, Any]) -> bytes:
    """Return a datagram carrying ``payload`` with no encryption at all."""
    return json.dumps(payload).encode()


def status_reply(values: dict[str, Any]) -> dict[str, Any]:
    """Return the decrypted body of a pump's answer to a status request."""
    return {
        "t": "dat",
        "mac": DEVICE_ID,
        "r": 200,
        "cols": list(values),
        "dat": list(values.values()),
    }


def write_reply(params: dict[str, Any], status: int = 200) -> dict[str, Any]:
    """Return the decrypted body of a pump's answer to a write."""
    return {
        "t": "res",
        "mac": DEVICE_ID,
        "r": status,
        "opt": list(params),
        "p": list(params.values()),
        "val": list(params.values()),
    }


def bad_tag(datagram: bytes) -> bytes:
    """Swap a GCM reply's tag for one the device key didn't make."""
    envelope = json.loads(datagram)
    envelope["tag"] = _b64(bytes(16))
    return json.dumps(envelope).encode()


def garbled(datagram: bytes) -> bytes:
    """Swap a reply's encrypted payload for bytes that aren't one."""
    envelope = json.loads(datagram)
    envelope["pack"] = _b64(bytes(range(32)))
    return json.dumps(envelope).encode()


def not_base64(datagram: bytes) -> bytes:
    """Swap a reply's encrypted payload for text that isn't even base64."""
    envelope = json.loads(datagram)
    envelope["pack"] = "not base64!"
    return json.dumps(envelope).encode()


class WirePump:
    """Answers the client's requests like a pump at ``ip`` would.

    Scenario knobs:
    - ``silent``: the pump never answers.
    - ``write_status``: the ``r`` code the pump answers writes with.
    - ``ahead``: datagrams that reach the client ahead of every reply, as if
      someone else on the network raced the pump.
    - ``chatter``: a datagram that keeps arriving, forever, while the client waits.
    - ``tamper``: rewrites each reply datagram the pump sends.
    """

    def __init__(
        self,
        *,
        encryption: str = ECB,
        values: dict[str, Any] | None = None,
        ip: str = PUMP_IP,
    ) -> None:
        self.encryption = encryption
        self.values: dict[str, Any] = dict(values or {})
        self.ip = ip
        self.silent = False
        self.write_status = 200
        self.ahead: list[Datagram] = []
        self.chatter: Datagram | None = None
        self.tamper: Callable[[bytes], bytes] | None = None

    @property
    def address(self) -> Address:
        return (self.ip, PUMP_PORT)

    def socket(self) -> FakeSocket:
        return FakeSocket(self)

    def datagrams_after_send(self, data: bytes, address: Address) -> list[Datagram]:
        """Return what reaches the client after it sends ``data`` to ``address``."""
        datagrams = list(self.ahead)
        request = self._open(data) if address == self.address else None
        if request is not None and not self.silent:
            reply = seal(self._respond(request), encryption=self.encryption)
            if self.tamper is not None:
                reply = self.tamper(reply)
            datagrams.append((reply, self.address))
        return datagrams

    def _open(self, data: bytes) -> dict[str, Any] | None:
        """Decrypt a request; like a real pump, ignore what it can't read."""
        try:
            envelope = json.loads(data)
            packed = base64.b64decode(envelope["pack"])
            if self.encryption == GCM:
                tag = base64.b64decode(envelope["tag"])
                raw = _gcm().decrypt_and_verify(packed, tag)
            else:
                raw = unpad(_ecb().decrypt(packed), AES.block_size)
            return json.loads(raw)
        except (KeyError, TypeError, ValueError):
            return None

    def _respond(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("t") == "cmd":
            params = dict(zip(request["opt"], request["p"], strict=True))
            if self.write_status == 200:
                self.values.update(params)
            return write_reply(params, self.write_status)
        keys = request.get("cols") or request.get("opt") or []
        return status_reply({key: self.values[key] for key in keys if key in self.values})


class FakeSocket:
    """The client's end of the network, wired to a fake pump."""

    def __init__(self, pump: WirePump) -> None:
        self._pump = pump
        self._inbox: asyncio.Queue[Datagram] = asyncio.Queue()

    async def sendto(self, data: bytes, address: Address) -> None:
        for datagram in self._pump.datagrams_after_send(data, address):
            self._inbox.put_nowait(datagram)

    async def recvfrom(self) -> Datagram:
        chatter = self._pump.chatter
        if chatter is None or not self._inbox.empty():
            return await self._inbox.get()
        await asyncio.sleep(0.005)
        return chatter

    def close(self) -> None:
        pass
