"""Local-only bootstrap and connectivity commands for a collector appliance."""

from __future__ import annotations

import argparse
import json
from os import environ
from sys import exit

from collector_agent.client import ControlPlaneClient, ControlPlaneClientError


def _required_env(name: str) -> str:
    value = environ.get(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migration Discovery collector bootstrap client")
    parser.add_argument("command", choices=("register", "heartbeat"))
    parser.add_argument("--control-plane-url", default=environ.get("CONTROL_PLANE_URL"))
    parser.add_argument("--tenant-id", default=environ.get("COLLECTOR_TENANT_ID"))
    parser.add_argument("--display-name", default=environ.get("COLLECTOR_DISPLAY_NAME"))
    parser.add_argument("--enrollment-code", default=environ.get("ENROLLMENT_CODE"))
    parser.add_argument("--collector-id", default=environ.get("COLLECTOR_ID"))
    parser.add_argument("--agent-token", default=environ.get("AGENT_TOKEN"))
    return parser


def main() -> None:
    args = _parser().parse_args()
    if not args.control_plane_url:
        raise ValueError("CONTROL_PLANE_URL is required")
    client = ControlPlaneClient(args.control_plane_url)
    try:
        if args.command == "register":
            result = client.register(
                {
                    "tenant_id": args.tenant_id or _required_env("COLLECTOR_TENANT_ID"),
                    "enrollment_code": args.enrollment_code or _required_env("ENROLLMENT_CODE"),
                    "display_name": args.display_name or _required_env("COLLECTOR_DISPLAY_NAME"),
                    "software_version": "0.1.0",
                }
            )
            print(json.dumps(result))
            return
        result = client.heartbeat(
            args.collector_id or _required_env("COLLECTOR_ID"),
            args.agent_token or _required_env("AGENT_TOKEN"),
            {"software_version": "0.1.0", "inventory_vm_count": 0},
        )
        print(json.dumps(result))
    except (ControlPlaneClientError, ValueError) as error:
        print(str(error))
        exit(1)


if __name__ == "__main__":
    main()
