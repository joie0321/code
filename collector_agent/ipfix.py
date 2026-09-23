"""Bounded, allowlisted IPFIX v10 receiver for the collector appliance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import ip_address
from socket import AF_INET, SOCK_DGRAM, socket
from struct import unpack_from
from threading import Event, Lock, Thread

_IPFIX_VERSION = 10
_HEADER_LENGTH = 16
_TEMPLATE_SET_ID = 2
_MIN_DATA_SET_ID = 256
_MAX_DATAGRAM_BYTES = 65_535
_MAX_TEMPLATE_FIELDS = 128
_SOURCE_IPV4, _DESTINATION_IPV4, _DESTINATION_PORT, _PROTOCOL = 8, 12, 11, 4
_FLOW_END_SECONDS, _FLOW_END_MILLISECONDS = 151, 153


@dataclass(frozen=True)
class IpfixFlow:
    source_ip: str
    destination_ip: str
    destination_port: int
    protocol: str
    observed_at: datetime


@dataclass(frozen=True)
class _TemplateField:
    element_id: int
    length: int
    enterprise_id: int | None


class IpfixParser:
    """Parse bounded TCP and UDP IPFIX flow metadata for dependency reporting."""

    def __init__(self) -> None:
        self._templates: dict[tuple[str, int, int], tuple[_TemplateField, ...]] = {}

    def parse(self, payload: bytes, exporter_ip: str) -> list[IpfixFlow]:
        if len(payload) < _HEADER_LENGTH:
            return []
        version, message_length, export_time, _, domain_id = unpack_from("!HHIII", payload)
        if version != _IPFIX_VERSION or message_length != len(payload):
            return []
        flows: list[IpfixFlow] = []
        offset = _HEADER_LENGTH
        while offset < message_length:
            if message_length - offset < 4:
                return []
            set_id, set_length = unpack_from("!HH", payload, offset)
            if set_length < 4 or offset + set_length > message_length:
                return []
            set_payload = payload[offset + 4 : offset + set_length]
            if set_id == _TEMPLATE_SET_ID:
                self._parse_templates(set_payload, exporter_ip, domain_id)
            elif set_id >= _MIN_DATA_SET_ID:
                flows.extend(
                    self._parse_data(set_payload, exporter_ip, domain_id, set_id, export_time)
                )
            offset += set_length
        return flows

    def _parse_templates(self, payload: bytes, exporter_ip: str, domain_id: int) -> None:
        offset = 0
        while offset < len(payload):
            if len(payload) - offset < 4:
                return
            template_id, field_count = unpack_from("!HH", payload, offset)
            offset += 4
            if template_id < _MIN_DATA_SET_ID or not 0 < field_count <= _MAX_TEMPLATE_FIELDS:
                return
            fields: list[_TemplateField] = []
            for _ in range(field_count):
                if len(payload) - offset < 4:
                    return
                raw_id, length = unpack_from("!HH", payload, offset)
                offset += 4
                enterprise_id = None
                if raw_id & 0x8000:
                    if len(payload) - offset < 4:
                        return
                    enterprise_id = unpack_from("!I", payload, offset)[0]
                    offset += 4
                if length == 0:
                    return
                fields.append(_TemplateField(raw_id & 0x7FFF, length, enterprise_id))
            self._templates[(exporter_ip, domain_id, template_id)] = tuple(fields)

    def _parse_data(
        self, payload: bytes, exporter_ip: str, domain_id: int, template_id: int, export_time: int
    ) -> list[IpfixFlow]:
        fields = self._templates.get((exporter_ip, domain_id, template_id))
        if not fields:
            return []
        offset, flows = 0, []
        while offset < len(payload):
            record, next_offset = self._parse_record(payload, offset, fields)
            if record is None:
                return flows
            offset = next_offset
            flow = _flow_from_record(record, export_time)
            if flow:
                flows.append(flow)
        return flows

    @staticmethod
    def _parse_record(
        payload: bytes, offset: int, fields: tuple[_TemplateField, ...]
    ) -> tuple[dict[int, bytes] | None, int]:
        record: dict[int, bytes] = {}
        for field in fields:
            length = field.length
            if length == 65_535:
                if offset >= len(payload):
                    return None, offset
                length = payload[offset]
                offset += 1
                if length == 255:
                    if len(payload) - offset < 2:
                        return None, offset
                    length = unpack_from("!H", payload, offset)[0]
                    offset += 2
            if length == 0 or len(payload) - offset < length:
                return None, offset
            if field.enterprise_id is None:
                record[field.element_id] = payload[offset : offset + length]
            offset += length
        return record, offset


def _flow_from_record(record: dict[int, bytes], export_time: int) -> IpfixFlow | None:
    source_ip = _ipv4(record.get(_SOURCE_IPV4))
    destination_ip = _ipv4(record.get(_DESTINATION_IPV4))
    destination_port = _uint(record.get(_DESTINATION_PORT))
    protocol_number = _uint(record.get(_PROTOCOL))
    protocol = {6: "tcp", 17: "udp"}.get(protocol_number)
    if not protocol or not source_ip or not destination_ip or not destination_port:
        return None
    milliseconds = _uint(record.get(_FLOW_END_MILLISECONDS))
    seconds = _uint(record.get(_FLOW_END_SECONDS))
    observed_at = datetime.fromtimestamp(
        (milliseconds / 1000) if milliseconds else (seconds or export_time), UTC
    )
    return IpfixFlow(source_ip, destination_ip, destination_port, protocol, observed_at)


def _uint(value: bytes | None) -> int | None:
    return int.from_bytes(value, "big") if value is not None and 1 <= len(value) <= 8 else None


def _ipv4(value: bytes | None) -> str | None:
    return str(ip_address(value)) if value is not None and len(value) == 4 else None


class IpfixListener:
    """A single in-process UDP listener with explicit individual-IP allowlisting."""

    def __init__(self, on_flows: Callable[[list[IpfixFlow]], int]) -> None:
        self._on_flows, self._lock, self._parser = on_flows, Lock(), IpfixParser()
        self._socket: socket | None = None
        self._stop_event: Event | None = None
        self._thread: Thread | None = None
        self._allowed_exporters: frozenset[str] = frozenset()
        self._status: dict[str, object] = self._new_status()

    @staticmethod
    def _new_status() -> dict[str, object]:
        return {
            "status": "stopped",
            "bind_host": None,
            "port": None,
            "allowed_exporters": [],
            "datagrams_received": 0,
            "datagrams_rejected": 0,
            "flows_received": 0,
            "observations_stored": 0,
            "last_received_at": None,
            "last_error": None,
        }

    def start(
        self, allowed_exporters: list[str], bind_host: str = "0.0.0.0", port: int = 4739
    ) -> dict[str, object]:
        exporters = frozenset(str(ip_address(value)) for value in allowed_exporters)
        if not exporters:
            raise ValueError("Select at least one IPFIX exporter address")
        with self._lock:
            if self._socket is not None:
                raise ValueError("The IPFIX collector is already running")
            listener_socket = socket(AF_INET, SOCK_DGRAM)
            try:
                listener_socket.bind((bind_host, port))
                listener_socket.settimeout(1.0)
            except OSError:
                listener_socket.close()
                raise ValueError("Cannot bind the IPFIX listener to UDP port 4739") from None
            self._socket, self._allowed_exporters, self._stop_event, self._parser = (
                listener_socket,
                exporters,
                Event(),
                IpfixParser(),
            )
            self._status = self._new_status() | {
                "status": "running",
                "bind_host": bind_host,
                "port": port,
                "allowed_exporters": sorted(exporters),
            }
            self._thread = Thread(target=self._run, daemon=True, name="ipfix-listener")
            self._thread.start()
            return dict(self._status)

    def stop(self) -> dict[str, object]:
        with self._lock:
            if self._stop_event:
                self._stop_event.set()
            if self._socket:
                self._socket.close()
            self._socket, self._status["status"] = None, "stopped"
            return dict(self._status)

    def status(self) -> dict[str, object]:
        with self._lock:
            return dict(self._status)

    def _run(self) -> None:
        while True:
            with self._lock:
                listener_socket, stop_event = self._socket, self._stop_event
            if listener_socket is None or stop_event is None or stop_event.is_set():
                return
            try:
                payload, address = listener_socket.recvfrom(_MAX_DATAGRAM_BYTES)
            except TimeoutError:
                continue
            except OSError:
                return
            if address[0] not in self._allowed_exporters:
                self._increment("datagrams_rejected")
                continue
            flows = self._parser.parse(payload, address[0])
            self._increment("datagrams_received")
            self._increment("flows_received", len(flows))
            self._set("last_received_at", datetime.now(UTC).isoformat())
            if flows:
                try:
                    self._increment("observations_stored", self._on_flows(flows))
                except Exception:
                    self._set("last_error", "Could not upload received IPFIX flow records.")

    def _increment(self, key: str, increment: int = 1) -> None:
        with self._lock:
            self._status[key] = int(self._status[key]) + increment

    def _set(self, key: str, value: object) -> None:
        with self._lock:
            self._status[key] = value
