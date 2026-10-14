"""Registry of the configured Govee lamps and their four identities.

Each lamp is known by four different things, and every transport needs a different one:

    HA entity   light.left_lamp          Home Assistant state and ordinary colour commands
    device id   AA:BB:CC:DD:EE:FF:00:11  Govee cloud Platform API (DreamView toggle)
    BLE MAC     CC:DD:EE:FF:00:11        BlueZ palette writes - the last 6 bytes of the device id
    LAN IP      discovered               local UDP colorwc / ptReal

The first three come from the add-on options (see settings.py). The LAN IP is DISCOVERED, never
configured: a DHCP lease change moves a lamp to a new address and would silently break every local
command otherwise.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import time
from dataclasses import dataclass, field

import settings

MCAST = "239.255.255.250"
SCAN_PORT = 4001
RECV_PORT = 4002
# Any routable address works: connecting a UDP socket sends nothing, it only asks the routing table
# which local interface would be used. TEST-NET-3 (RFC 5737) is reserved and never answers.
ROUTE_PROBE = os.environ.get("GOVEE_ROUTE_PROBE", "203.0.113.1")

# The interface-specific bind is mandatory on the multi-homed Windows host: 0.0.0.0:4002 receives
# nothing at all there. Rather than hardcode that host's address, work out the address of whichever
# interface actually routes outward - which keeps this correct on the Pi too. If that is the wrong
# interface (a VPN, for example), set GOVEE_LOCAL_IP, or GOVEE_ROUTE_PROBE to your router's address.
def default_local_ip() -> str:
    """IP of the interface that routes to the LAN, without sending anything."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((ROUTE_PROBE, 1))
        return probe.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        probe.close()


DEFAULT_LOCAL_IP = default_local_ip()


@dataclass
class Lamp:
    name: str
    entity_id: str
    device_id: str
    ip: str | None = field(default=None)

    @property
    def ble_mac(self) -> str:
        """Pi-side BlueZ address: the last six bytes of the Govee device id."""
        return ":".join(self.device_id.split(":")[2:])


SETTINGS = settings.load()

LAMPS: list[Lamp] = [
    Lamp(lamp.name, lamp.entity_id, lamp.device_id) for lamp in SETTINGS.lamps
]

BY_ENTITY: dict[str, Lamp] = {lamp.entity_id: lamp for lamp in LAMPS}


def scan(local_ip: str = DEFAULT_LOCAL_IP, timeout: float = 4.0) -> dict[str, dict]:
    """Multicast scan returning {device_id: scan_data} for every lamp that answers."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((local_ip, RECV_PORT))
    except OSError:
        # another local process already holds :4002
        sock.close()
        return {}
    try:
        sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
            struct.pack("4s4s", socket.inet_aton(MCAST), socket.inet_aton(local_ip)))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                        socket.inet_aton(local_ip))
    except OSError:
        pass
    sock.settimeout(1.0)

    request = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}},
                         separators=(",", ":")).encode()
    found: dict[str, dict] = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            sock.sendto(request, (MCAST, SCAN_PORT))
        except OSError:
            break
        try:
            while True:
                raw, _ = sock.recvfrom(8192)
                try:
                    data = json.loads(raw.decode())["msg"]["data"]
                except (ValueError, KeyError):
                    continue
                if data.get("device"):
                    found[data["device"]] = data
        except socket.timeout:
            pass
        if len(found) >= len(LAMPS):
            break
    sock.close()
    return found


def resolve_ips(local_ip: str = DEFAULT_LOCAL_IP) -> list[Lamp]:
    """Fill in each lamp's current LAN IP. Lamps that do not answer keep ip=None."""
    found = scan(local_ip)
    for lamp in LAMPS:
        entry = found.get(lamp.device_id)
        if entry and entry.get("ip"):
            lamp.ip = entry["ip"]
    return LAMPS


if __name__ == "__main__":
    SETTINGS.require_lamps()
    for lamp in resolve_ips():
        print(f"{lamp.name:<14} {lamp.entity_id:<16} ip={lamp.ip or '(no reply)':<15} "
              f"ble={lamp.ble_mac}  device={lamp.device_id}")
