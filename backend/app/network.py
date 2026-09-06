"""Local interface discovery; never advertise the Internet/VPN default route."""
from __future__ import annotations

import ipaddress
import socket

import psutil


_VIRTUAL_NAMES = (
    "virtual", "vmware", "vethernet", "wsl", "docker", "clash", "tun", "tap",
    "vpn", "wireguard", "tailscale", "zerotier", "loopback", "回环", "虚拟",
)
_NON_LAN_RANGES = tuple(ipaddress.ip_network(value) for value in (
    "198.18.0.0/15", "100.64.0.0/10",
))


def local_interfaces() -> list[dict[str, str | bool]]:
    try:
        stats = psutil.net_if_stats()
        addresses = psutil.net_if_addrs()
    except OSError:
        return []
    interfaces = []
    for name, entries in addresses.items():
        if name not in stats or not stats[name].isup:
            continue
        virtual = any(part in name.casefold() for part in _VIRTUAL_NAMES)
        for entry in entries:
            if entry.family != socket.AF_INET:
                continue
            address = ipaddress.IPv4Address(entry.address)
            if address.is_loopback or address.is_unspecified or address.is_multicast:
                continue
            recommended = not (virtual or address.is_link_local or any(address in net for net in _NON_LAN_RANGES))
            interfaces.append({"name": name, "ip": str(address), "recommended": recommended})
    return sorted(interfaces, key=lambda item: (not item["recommended"], str(item["name"]), str(item["ip"])))


def advertised_ip(configured: str = "", *, interfaces: list[dict] | None = None) -> str:
    if configured:
        return str(ipaddress.IPv4Address(configured))
    candidates = local_interfaces() if interfaces is None else interfaces
    return next((str(item["ip"]) for item in candidates if item["recommended"]), "127.0.0.1")
