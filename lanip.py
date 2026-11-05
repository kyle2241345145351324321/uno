"""Pick this computer's *real* LAN address (standard library only).

The usual trick - ask the OS which source address it would use to reach the
internet - returns the VPN's address while a VPN is connected, which nobody on
the Wi-Fi can reach.  This module instead lists the network interfaces and skips
VPN / tunnel / virtual ones, then ranks what is left (Wi-Fi/Ethernet, private
ranges first).
"""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys

# interface names that are tunnels, VPNs or virtual switches rather than the LAN
_VIRTUAL = re.compile(
    r"^(lo\d*|utun\d*|tun\d*|tap\d*|ppp\d*|wg\d*|ipsec\d*|gpd\d*|zt\w*|tailscale\w*|ts\d+|"
    r"vpn\w*|ham\w*|awdl\d*|llw\d*|anpi\d*|ap\d+|bridge\d*|vmnet\d*|vboxnet\d*|docker\w*|"
    r"veth\w*|br-\w*|virbr\d*|vnic\d*|vEthernet.*|utap\d*|nordlynx|proton\d*|cscotun\d*|"
    r"feth\d*|gif\d*|stf\d*|XHC\d*)$", re.I)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")        # Tailscale and some carrier networks


def _usable(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version != 4 or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        return False
    return addr not in _CGNAT


def _parse_ifconfig(text: str) -> list[tuple[str, str, str | None]]:
    """macOS / BSD ``ifconfig`` -> [(iface, ip, broadcast)]."""
    found: list[tuple[str, str, str | None]] = []
    iface = ""
    for line in text.splitlines():
        head = re.match(r"^([A-Za-z0-9_.-]+):\s+flags=", line)
        if head:
            iface = head.group(1)
            continue
        m = re.match(r"^\s+inet\s+(\d+\.\d+\.\d+\.\d+)\s+(?:-->\s+\S+\s+)?netmask\s+(\S+)(?:\s+broadcast\s+(\S+))?", line)
        if m and iface:
            found.append((iface, m.group(1), m.group(3)))
    return found


def _parse_ip_addr(text: str) -> list[tuple[str, str, str | None]]:
    """Linux ``ip -o -4 addr show`` -> [(iface, ip, broadcast)]."""
    found: list[tuple[str, str, str | None]] = []
    for line in text.splitlines():
        m = re.match(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)(?:\s+brd\s+(\S+))?", line)
        if m:
            found.append((m.group(1).rstrip(":"), m.group(2), m.group(4)))
    return found


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=4).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _interfaces() -> list[tuple[str, str, str | None]]:
    if sys.platform == "darwin" or sys.platform.startswith(("freebsd", "openbsd", "netbsd")):
        exe = shutil.which("ifconfig") or "/sbin/ifconfig"
        return _parse_ifconfig(_run([exe]))
    if sys.platform.startswith("linux"):
        exe = shutil.which("ip")
        if exe:
            found = _parse_ip_addr(_run([exe, "-o", "-4", "addr", "show"]))
            if found:
                return found
        exe = shutil.which("ifconfig")
        if exe:
            return _parse_ifconfig(_run([exe]))
    return []


def _rank(iface: str, ip: str) -> tuple:
    addr = ipaddress.ip_address(ip)
    wifi_or_eth = bool(re.match(r"^(en\d+|eth\d+|enp\w+|ens\w+|eno\w+|wl\w+|wlan\d+|Wi-?Fi.*|Ethernet.*)$", iface, re.I))
    private = addr.is_private
    # best first: real adapter, private range, then the usual home-router /24s
    home = ip.startswith("192.168.")
    return (not wifi_or_eth, not private, not home, iface, ip)


_CACHE: dict = {"t": 0.0, "v": None}
_TTL = 60.0


def lan_addresses(refresh: bool = False) -> list[tuple[str, str, str | None]]:
    """[(iface, ip, broadcast)] for real LAN adapters, best first (never VPN/virtual).

    The result is cached: asking the OS means running a command, which must not happen
    over and over (and never from a background thread).  ``refresh=True`` recomputes.
    """
    import time as _time

    now = _time.monotonic()
    if not refresh and _CACHE["v"] is not None and now - _CACHE["t"] < _TTL:
        return _CACHE["v"]
    value = _lan_addresses_uncached()
    _CACHE["v"], _CACHE["t"] = value, now
    return value


def cached_broadcasts() -> list[str]:
    """Directed broadcasts from the cache only (safe for background threads)."""
    if _CACHE["v"] is None:
        return []
    out: list[str] = []
    for _i, ip, brd in _CACHE["v"]:
        guess = brd or ".".join(ip.split(".")[:3] + ["255"])
        if guess not in out:
            out.append(guess)
    return out


def _lan_addresses_uncached() -> list[tuple[str, str, str | None]]:
    items = [(i, ip, brd) for i, ip, brd in _interfaces() if not _VIRTUAL.match(i) and _usable(ip)]
    items.sort(key=lambda it: _rank(it[0], it[1]))
    seen: set[str] = set()
    out = []
    for item in items:
        if item[1] not in seen:
            seen.add(item[1])
            out.append(item)
    if out:
        return out
    # fallback when the interface list is unavailable (e.g. Windows): ask the OS
    guesses: list[str] = []
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            guesses.append(ip)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))
        guesses.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    ranked = sorted({g for g in guesses if _usable(g)},
                    key=lambda g: (not g.startswith("192.168."), not ipaddress.ip_address(g).is_private, g))
    return [("?", ip, None) for ip in ranked]


def lan_ips(refresh: bool = False) -> list[str]:
    return [ip for _i, ip, _b in lan_addresses(refresh)]


def best_lan_ip(refresh: bool = False) -> str:
    ips = lan_ips(refresh)
    return ips[0] if ips else "127.0.0.1"


def broadcast_addresses() -> list[str]:
    """Directed broadcast addresses of the real LAN adapters (for room discovery)."""
    out: list[str] = []
    for _i, ip, brd in lan_addresses():
        if brd:
            if brd not in out:
                out.append(brd)
        else:
            parts = ip.split(".")
            guess = ".".join(parts[:3] + ["255"])
            if guess not in out:
                out.append(guess)
    return out


if __name__ == "__main__":
    for iface, ip, brd in lan_addresses():
        print(f"{iface:10s} {ip:16s} {brd or ''}")
    print("best:", best_lan_ip())
