"""Bounded reader for customer-owned AWS VPC Flow Log objects in Amazon S3."""

from __future__ import annotations

import gzip
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any

from botocore.exceptions import ClientError

from collector_agent.aws import AwsCredentials, aws_session

_MAX_COMPRESSED_OBJECT_BYTES = 16 * 1024 * 1024
_MAX_UNCOMPRESSED_OBJECT_BYTES = 64 * 1024 * 1024
_MAX_OBJECTS_PER_SYNC = 250


@dataclass(frozen=True)
class AwsFlowLogSettings:
    """Non-secret location of VPC Flow Logs for one AWS source."""

    bucket_name: str
    prefix: str


@dataclass(frozen=True)
class AwsFlowObservation:
    source_ip: str
    destination_ip: str
    destination_port: int
    protocol: str
    observed_at: datetime


@dataclass(frozen=True)
class AwsFlowLogReadResult:
    observations: list[AwsFlowObservation]
    processed_object_keys: set[str]
    objects_found: int


class AwsFlowLogError(ValueError):
    """Sanitized S3 flow-log retrieval failure safe for the local operator UI."""


def _safe_s3_error(error: ClientError) -> AwsFlowLogError:
    error_data = error.response.get("Error", {})
    code = error_data.get("Code") if isinstance(error_data, dict) else None
    messages = {
        "AccessDenied": (
            "AWS denied S3 read access. Verify s3:ListBucket and s3:GetObject for the "
            "configured bucket and prefix."
        ),
        "NoSuchBucket": (
            "The configured S3 bucket was not found. Verify the bucket name and AWS Region."
        ),
        "InvalidAccessKeyId": (
            "AWS rejected the configured access key. Store valid AWS credentials again."
        ),
        "ExpiredToken": "AWS credentials have expired. Store fresh AWS credentials again.",
    }
    return AwsFlowLogError(
        messages.get(code, "AWS could not read the configured S3 VPC Flow Log location.")
    )


def parse_flow_log_lines(lines: Iterable[str]) -> list[AwsFlowObservation]:
    """Parse the documented custom VPC Flow Log field order, rejecting bad rows.

    Expected order: version, account ID, VPC ID, subnet ID, instance ID, interface
    ID, source/destination address and port, protocol, packets, bytes, start/end,
    action, TCP flags, and log status.
    """

    observations: list[AwsFlowObservation] = []
    for line in lines:
        fields = line.split()
        if len(fields) != 18 or fields[15] != "ACCEPT" or fields[17] != "OK":
            continue
        try:
            source_ip = str(ip_address(fields[6]))
            destination_ip = str(ip_address(fields[7]))
            destination_port = int(fields[9])
            observed_at = datetime.fromtimestamp(int(fields[14]), UTC)
        except (OSError, ValueError, OverflowError):
            continue
        protocol = {"6": "tcp", "17": "udp"}.get(fields[10])
        if not protocol or not 1 <= destination_port <= 65535:
            continue
        observations.append(
            AwsFlowObservation(
                source_ip=source_ip,
                destination_ip=destination_ip,
                destination_port=destination_port,
                protocol=protocol,
                observed_at=observed_at,
            )
        )
    return observations


def _object_text(body: Any, content_length: object) -> str:
    if not isinstance(content_length, int) or content_length < 0:
        raise ValueError("AWS flow-log object has an invalid content length")
    if content_length > _MAX_COMPRESSED_OBJECT_BYTES:
        raise ValueError("AWS flow-log object exceeds the compressed size limit")
    with gzip.GzipFile(fileobj=body, mode="rb") as stream:
        data = stream.read(_MAX_UNCOMPRESSED_OBJECT_BYTES + 1)
    if len(data) > _MAX_UNCOMPRESSED_OBJECT_BYTES:
        raise ValueError("AWS flow-log object exceeds the uncompressed size limit")
    return data.decode("utf-8", errors="strict")


def fetch_flow_log_observations(
    credentials: AwsCredentials,
    settings: AwsFlowLogSettings,
    known_object_keys: set[str],
) -> AwsFlowLogReadResult:
    """Read unseen, bounded VPC flow-log objects from the configured S3 prefix."""

    try:
        client = aws_session(credentials).client("s3")
        objects: list[dict[str, object]] = []
        for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=settings.bucket_name,
            Prefix=settings.prefix,
            PaginationConfig={"MaxItems": _MAX_OBJECTS_PER_SYNC},
        ):
            contents = page.get("Contents", [])
            if isinstance(contents, list):
                objects.extend(item for item in contents if isinstance(item, dict))
        candidates = sorted(
            (
                item
                for item in objects
                if isinstance(item.get("Key"), str)
                and str(item["Key"]).endswith(".log.gz")
                and str(item["Key"]) not in known_object_keys
            ),
            key=lambda item: str(item.get("LastModified", "")),
        )
        observations: list[AwsFlowObservation] = []
        processed_keys: set[str] = set()
        for item in candidates:
            key = str(item["Key"])
            response = client.get_object(Bucket=settings.bucket_name, Key=key)
            body = response.get("Body")
            if body is None or not hasattr(body, "read"):
                raise AwsFlowLogError("AWS flow-log object has no readable body")
            text = _object_text(body, response.get("ContentLength"))
            observations.extend(parse_flow_log_lines(text.splitlines()))
            processed_keys.add(key)
        return AwsFlowLogReadResult(observations, processed_keys, len(candidates))
    except ClientError as error:
        raise _safe_s3_error(error) from error
