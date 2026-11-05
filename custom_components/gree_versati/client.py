"""Local UDP protocol client for Gree Versati devices."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import socket
from collections.abc import Callable, Sequence
from typing import Any, Literal, NamedTuple, Protocol

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from homeassistant.exceptions import HomeAssistantError

from .constants import DEFAULT_MAX_DATAGRAM_BYTES

ENCRYPTION_ECB = "ECB"
ENCRYPTION_GCM = "GCM"

# Values observed in working open-source protocol implementations for GCM devices.
GCM_NONCE = b"\x54\x40\x78\x44\x49\x67\x5a\x51\x6c\x5e\x63\x13"
GCM_AAD = b"qualcomm-test"

_LOGGER = logging.getLogger(__name__)

Address = tuple[str, int]

_Operation = Literal["read", "write"]


class _Combination(NamedTuple):
    """One way of asking the pump: an encryption and a request format."""

    encryption: str
    format_index: int


class GreeVersatiClientError(HomeAssistantError):
    """Raised when protocol communication with the device fails."""


class _NoReply(GreeVersatiClientError):
    """The pump never answered: every attempt timed out or failed to send."""


class DatagramSocket(Protocol):
    """One request's UDP socket: the client's only door to the network."""

    async def sendto(self, data: bytes, address: Address) -> None:
        """Send one datagram to ``address``."""

    async def recvfrom(self) -> tuple[bytes, Address]:
        """Wait for the next datagram from anyone; return it and its sender."""

    def close(self) -> None:
        """Release the socket."""


class _UdpSocket:
    """A real, non-blocking IPv4 UDP socket."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)

    async def sendto(self, data: bytes, address: Address) -> None:
        await asyncio.get_running_loop().sock_sendto(self._sock, data, address)

    async def recvfrom(self) -> tuple[bytes, Address]:
        return await asyncio.get_running_loop().sock_recvfrom(
            self._sock, DEFAULT_MAX_DATAGRAM_BYTES
        )

    def close(self) -> None:
        self._sock.close()


class GreeVersatiProtocolClient:
    """Minimal async UDP JSON client with AES pack support."""

    def __init__(
        self,
        host: str,
        port: int,
        device_id: str,
        key: str,
        timeout: float,
        retries: int,
        *,
        socket_factory: Callable[[], DatagramSocket] = _UdpSocket,
    ) -> None:
        self._host = host
        self._port = port
        self._device_id = device_id
        self._timeout = timeout
        self._retries = retries
        self._key_bytes = self._validate_key(key)
        # The encryption and request format each operation last worked with.
        self._working: dict[_Operation, _Combination] = {}
        self._open_socket = socket_factory

    @staticmethod
    def validate_key_string(key: str) -> bool:
        """Return True if key is valid for AES-128."""
        return len(key.encode("utf-8")) == 16

    @classmethod
    def _validate_key(cls, key: str) -> bytes:
        if not cls.validate_key_string(key):
            raise GreeVersatiClientError(
                "Invalid AES key length. Key must be exactly 16 bytes."
            )
        return key.encode("utf-8")

    async def async_get(self, keys: list[str]) -> dict[str, Any]:
        """Fetch selected parameters from the device."""
        if not keys:
            return {}

        _LOGGER.debug(
            "Requesting keys from device_id=%s host=%s: %s",
            self._device_id,
            self._host,
            keys,
        )

        # Request format mirrors known working client behavior for Versati:
        # prefer `cols` + `mac`, then fallback to `opt` variants for compatibility.
        formats: tuple[dict[str, Any], ...] = (
            {"t": "status", "mac": self._device_id, "cols": keys},
            {"t": "status", "uid": 0, "mac": self._device_id, "cols": keys},
            {"t": "status", "mac": self._device_id, "opt": keys},
            {"t": "status", "uid": 0, "opt": keys},
        )

        def requested_values(reply: dict[str, Any]) -> dict[str, Any]:
            values = self._extract_values(reply)
            if not values:
                raise GreeVersatiClientError("Device reply held no values")
            return {key: values[key] for key in keys if key in values}

        return await self._async_exchange("read", formats, requested_values)

    async def async_set(self, params: dict[str, Any]) -> None:
        """Set one or more parameters on the device."""
        if not params:
            return

        _LOGGER.debug(
            "Setting keys on device_id=%s host=%s: %s",
            self._device_id,
            self._host,
            list(params.keys()),
        )

        keys = list(params.keys())
        values = [params[key] for key in keys]
        formats: tuple[dict[str, Any], ...] = (
            {"t": "cmd", "opt": keys, "p": values},
            {"t": "cmd", "mac": self._device_id, "opt": keys, "p": values},
            {"t": "cmd", "uid": 0, "mac": self._device_id, "opt": keys, "p": values},
        )

        def confirm(reply: dict[str, Any]) -> None:
            status = reply.get("r")
            if isinstance(status, int) and status not in (0, 200):
                raise GreeVersatiClientError(f"Device returned error code: {status}")

        await self._async_exchange("write", formats, confirm)

    async def _async_exchange[T](
        self,
        operation: _Operation,
        formats: Sequence[dict[str, Any]],
        accept: Callable[[dict[str, Any]], T],
    ) -> T:
        """Send ``formats`` until ``accept`` takes a reply; return what it returns.

        ``accept`` raises GreeVersatiClientError for a reply it can't use. The
        combination of encryption and format that worked is tried first next time.
        """
        working = self._working.get(operation)
        error: GreeVersatiClientError | None = None
        for combination in self._combinations(operation, len(formats)):
            is_working = combination == working
            payload = formats[combination.format_index]
            # Only a combination the pump has answered earns retries: while
            # discovering, a pump may silently ignore what it doesn't understand.
            attempts = self._retries + 1 if is_working else 1
            try:
                request = self._build_outer_message(payload, combination.encryption)
                response = await self._async_request(
                    request, combination.encryption, attempts=attempts
                )
                result = accept(response)
            except GreeVersatiClientError as err:
                silent = isinstance(err, _NoReply)
                # The pump has gone silent: other combinations won't wake it.
                # One that answered may still understand another combination.
                if silent and is_working:
                    raise
                _LOGGER.debug(
                    "%s attempt failed for device_id=%s encryption=%s payload_keys=%s: %s",
                    operation,
                    self._device_id,
                    combination.encryption,
                    list(payload),
                    err,
                )
                # What the pump said beats the silence of combinations it ignores.
                if error is None or not silent:
                    error = err
                continue
            if not is_working:
                _LOGGER.debug(
                    "device_id=%s answers %s with encryption=%s payload_keys=%s",
                    self._device_id,
                    operation,
                    combination.encryption,
                    list(payload),
                )
                self._working[operation] = combination
            return result

        if error is None:
            raise GreeVersatiClientError(f"No request formats to {operation} with")
        raise error

    def _combinations(
        self, operation: _Operation, format_count: int
    ) -> list[_Combination]:
        """Every encryption and format, with what worked before first."""
        working = self._working.get(operation)
        # The pump speaks one encryption: lead with the one that worked, for
        # this operation or else the other.
        lead = working or next(iter(self._working.values()), None)
        encryptions = (ENCRYPTION_ECB, ENCRYPTION_GCM)
        if lead is not None and lead.encryption == ENCRYPTION_GCM:
            encryptions = (ENCRYPTION_GCM, ENCRYPTION_ECB)

        combinations = [
            _Combination(encryption, index)
            for encryption in encryptions
            for index in range(format_count)
        ]
        if working is not None:
            combinations.remove(working)
            combinations.insert(0, working)
        return combinations

    def _build_outer_message(self, pack_payload: dict[str, Any], encryption: str) -> str:
        encrypted_pack, tag = self._encrypt_pack(pack_payload, encryption)
        message = {
            "cid": "app",
            # Keep i=0 for maximum compatibility with known working CLI behavior.
            "i": 0,
            "t": "pack",
            "uid": 0,
            "tcid": self._device_id,
            "pack": encrypted_pack,
        }
        if tag is not None:
            message["tag"] = tag
        return json.dumps(message, separators=(",", ":"))

    def _encrypt_pack(self, payload: dict[str, Any], encryption: str) -> tuple[str, str | None]:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if encryption == ENCRYPTION_GCM:
            cipher = AES.new(self._key_bytes, AES.MODE_GCM, nonce=GCM_NONCE)
            cipher.update(GCM_AAD)
            encrypted, tag = cipher.encrypt_and_digest(raw)
            return (
                base64.b64encode(encrypted).decode("ascii"),
                base64.b64encode(tag).decode("ascii"),
            )

        cipher = AES.new(self._key_bytes, AES.MODE_ECB)
        encrypted = cipher.encrypt(pad(raw, AES.block_size, style="pkcs7"))
        return base64.b64encode(encrypted).decode("ascii"), None

    def _decrypt_pack(
        self,
        packed_value: str,
        tag_value: str | None,
        encryption: str,
    ) -> dict[str, Any]:
        try:
            if encryption == ENCRYPTION_GCM:
                if not tag_value:
                    raise GreeVersatiClientError("Missing GCM tag in response")
                cipher = AES.new(self._key_bytes, AES.MODE_GCM, nonce=GCM_NONCE)
                cipher.update(GCM_AAD)
                decrypted = cipher.decrypt_and_verify(
                    base64.b64decode(packed_value),
                    base64.b64decode(tag_value),
                )
                # Some firmwares append 0xFF padding bytes.
                decoded = decrypted.replace(b"\xff", b"").decode("utf-8")
                return json.loads(decoded)

            encrypted = base64.b64decode(packed_value)
            cipher = AES.new(self._key_bytes, AES.MODE_ECB)
            decrypted_raw = cipher.decrypt(encrypted)
            try:
                decrypted = unpad(decrypted_raw, AES.block_size, style="pkcs7")
            except ValueError:
                # Some firmwares append non-PKCS garbage; trim to last JSON terminator.
                decrypted = decrypted_raw[: decrypted_raw.rfind(b"}") + 1]

            return json.loads(decrypted.decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as err:
            raise GreeVersatiClientError(
                "Failed to decrypt and parse response pack"
            ) from err

    async def _async_request(
        self, message: str, encryption: str, *, attempts: int
    ) -> dict[str, Any]:
        """Send ``message`` until the pump answers; return its decrypted reply.

        Raises ``_NoReply`` once ``attempts`` go unanswered, and gives up at once
        on a reply that won't decrypt: asking again would get the same answer.
        """
        payload = message.encode("utf-8")
        last_error: Exception | None = None

        for _attempt in range(attempts):
            sock = self._open_socket()
            try:
                device_ip = await self._async_resolve_device_ip()
                await sock.sendto(payload, (device_ip, self._port))
                response_bytes = await asyncio.wait_for(
                    self._async_receive_from(sock, device_ip),
                    timeout=self._timeout,
                )
            except (asyncio.TimeoutError, OSError) as err:
                _LOGGER.debug(
                    "UDP attempt %s/%s failed for host=%s port=%s encryption=%s: %r (%s)",
                    _attempt + 1,
                    attempts,
                    self._host,
                    self._port,
                    encryption,
                    err,
                    type(err).__name__,
                )
                last_error = err
                continue
            finally:
                sock.close()
            return self._parse_response(response_bytes, encryption)

        raise _NoReply(
            f"No reply from device after {attempts} attempts"
        ) from last_error

    async def _async_resolve_device_ip(self) -> str:
        """Return the device's IPv4 address: the configured host, resolved if a name."""
        addresses = await asyncio.get_running_loop().getaddrinfo(
            self._host, self._port, family=socket.AF_INET, type=socket.SOCK_DGRAM
        )
        return str(addresses[0][4][0])

    async def _async_receive_from(self, sock: DatagramSocket, device_ip: str) -> bytes:
        """Return the next datagram from the device, skipping anyone else's."""
        while True:
            data, (sender_ip, sender_port) = await sock.recvfrom()
            if sender_ip == device_ip:
                return data
            _LOGGER.debug(
                "Ignoring datagram from %s:%s, waiting for device_id=%s at %s",
                sender_ip,
                sender_port,
                self._device_id,
                device_ip,
            )

    def _parse_response(self, response_bytes: bytes, encryption: str) -> dict[str, Any]:
        try:
            decoded = response_bytes.decode("utf-8", errors="ignore")
            start = decoded.find("{")
            end = decoded.rfind("}")
            if start == -1 or end == -1 or end < start:
                raise GreeVersatiClientError(
                    "Response did not contain valid JSON envelope"
                )
            message = json.loads(decoded[start : end + 1])
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise GreeVersatiClientError("Failed to decode response JSON") from err

        # Only what decrypts with the device key counts: a plaintext reply could
        # come from anyone, so it is never device data or a write's confirmation.
        if not isinstance(message, dict) or not isinstance(message.get("pack"), str):
            raise GreeVersatiClientError("Response has no encrypted payload")

        tag_value = message.get("tag")
        return self._decrypt_pack(
            message["pack"],
            tag_value if isinstance(tag_value, str) else None,
            encryption,
        )

    @staticmethod
    def _extract_values(payload: dict[str, Any]) -> dict[str, Any]:
        if "dat" in payload and isinstance(payload["dat"], dict):
            return payload["dat"]

        if isinstance(payload.get("cols"), list) and isinstance(
            payload.get("dat"), list
        ):
            cols: list[Any] = payload["cols"]
            dat: list[Any] = payload["dat"]
            return {
                key: value
                for key, value in zip(cols, dat, strict=False)
                if isinstance(key, str)
            }

        opts: Sequence[str] | None = None
        if isinstance(payload.get("opt"), list):
            opts = payload["opt"]
        elif isinstance(payload.get("cols"), list):
            opts = payload["cols"]

        raw_values: Sequence[Any] | None = None
        if isinstance(payload.get("p"), list):
            raw_values = payload["p"]
        elif isinstance(payload.get("val"), list):
            raw_values = payload["val"]

        if opts is not None and raw_values is not None:
            return {
                key: value
                for key, value in zip(opts, raw_values, strict=False)
                if isinstance(key, str)
            }

        # Fallback: if values are already flattened in the payload.
        direct: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(key, str) and key not in {
                "t",
                "r",
                "mac",
                "opt",
                "p",
                "dat",
                "cols",
            }:
                direct[key] = value
        if direct:
            return direct

        return {}
