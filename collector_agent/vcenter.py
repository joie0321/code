"""vCenter inventory adapter reused from the working standalone collector."""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any

from pydantic import SecretStr


@dataclass(frozen=True)
class VCenterCredentials:
    host: str
    username: str
    password: SecretStr
    verify_tls: bool


@dataclass(frozen=True)
class VmRecord:
    vm_uuid: str
    name: str
    hostname: str | None
    ips: list[str]
    cluster: str | None
    folder: str | None
    os_name: str | None
    power_state: str | None


@dataclass(frozen=True)
class ExporterCandidate:
    """A reviewed ESXi management-interface candidate for IPFIX exports."""

    host_name: str
    cluster: str | None
    vmk0_ips: list[str]


def reported_ips(vm: Any, guest_summary: Any) -> list[str]:
    addresses: set[str] = set()
    for nic in getattr(getattr(vm, "guest", None), "net", None) or []:
        for entry in getattr(getattr(nic, "ipConfig", None), "ipAddress", None) or []:
            value = getattr(entry, "ipAddress", entry)
            if isinstance(value, str):
                try:
                    addresses.add(str(ip_address(value)))
                except ValueError:
                    continue
    primary = getattr(guest_summary, "ipAddress", None)
    if isinstance(primary, str):
        try:
            addresses.add(str(ip_address(primary)))
        except ValueError:
            pass
    return sorted(addresses)


def fetch_vms(credentials: VCenterCredentials) -> list[VmRecord]:
    """Fetch active vCenter inventory without persisting its credentials."""

    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim

    context = ssl.create_default_context()
    if not credentials.verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    service_instance = SmartConnect(
        host=credentials.host,
        user=credentials.username,
        pwd=credentials.password.get_secret_value(),
        sslContext=context,
    )
    try:
        view = service_instance.content.viewManager.CreateContainerView(
            service_instance.content.rootFolder, [vim.VirtualMachine], True
        )
        records: list[VmRecord] = []
        try:
            for vm in view.view:
                summary = vm.summary
                if not summary.config or not summary.config.uuid:
                    continue
                guest = summary.guest
                guest_info = getattr(vm, "guest", None)
                host = getattr(getattr(vm, "runtime", None), "host", None)
                records.append(
                    VmRecord(
                        vm_uuid=summary.config.uuid,
                        name=vm.name,
                        hostname=getattr(guest_info, "hostName", None) or guest.hostName,
                        ips=reported_ips(vm, guest),
                        cluster=getattr(getattr(host, "parent", None), "name", None),
                        folder=getattr(getattr(vm, "parent", None), "name", None),
                        os_name=summary.config.guestFullName,
                        power_state=str(getattr(getattr(vm, "runtime", None), "powerState", None)),
                    )
                )
        finally:
            view.Destroy()
        return records
    finally:
        Disconnect(service_instance)


def fetch_exporter_candidates(credentials: VCenterCredentials) -> list[ExporterCandidate]:
    """Return ESXi vmk0 addresses for operator review before opening IPFIX intake.

    vmk0 is a useful default candidate, not a guarantee that an environment exports
    IPFIX from that address. The caller must still explicitly select the candidates.
    """

    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim

    context = ssl.create_default_context()
    if not credentials.verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    service_instance = SmartConnect(
        host=credentials.host,
        user=credentials.username,
        pwd=credentials.password.get_secret_value(),
        sslContext=context,
    )
    try:
        view = service_instance.content.viewManager.CreateContainerView(
            service_instance.content.rootFolder, [vim.HostSystem], True
        )
        candidates: list[ExporterCandidate] = []
        try:
            for host in view.view:
                addresses: set[str] = set()
                network = getattr(getattr(host, "config", None), "network", None)
                for vnic in getattr(network, "vnic", None) or []:
                    if getattr(vnic, "device", None) != "vmk0":
                        continue
                    value = getattr(getattr(vnic, "spec", None), "ip", None)
                    address = getattr(value, "ipAddress", None)
                    if isinstance(address, str):
                        try:
                            addresses.add(str(ip_address(address)))
                        except ValueError:
                            continue
                if addresses:
                    candidates.append(
                        ExporterCandidate(
                            host_name=host.name,
                            cluster=getattr(getattr(host, "parent", None), "name", None),
                            vmk0_ips=sorted(addresses),
                        )
                    )
        finally:
            view.Destroy()
        return sorted(
            candidates, key=lambda candidate: (candidate.cluster or "", candidate.host_name)
        )
    finally:
        Disconnect(service_instance)
