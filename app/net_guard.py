"""Static-IP guard: LIVE order placement is blocked unless the public IP matches GROWW_STATIC_IP.
(Paper mode never needs it.)"""
from __future__ import annotations
import ipaddress
import urllib.request


def public_ip(fetch=None, timeout: float = 5.0) -> str:
    if fetch is None:
        def fetch():
            with urllib.request.urlopen("https://api.ipify.org", timeout=timeout) as r:
                return r.read().decode().strip()
    ip = fetch().strip()
    ipaddress.IPv4Address(ip)          # raises on garbage
    return ip


def check_static_ip(expected: str | None, fetch=None) -> tuple[bool, str]:
    if not expected:
        return False, "GROWW_STATIC_IP not set in .env"
    try:
        actual = public_ip(fetch)
    except Exception as e:
        return False, f"could not determine public IP ({type(e).__name__}); blocking live orders"
    if actual != expected.strip():
        return False, f"public IP {actual} != whitelisted {expected}; blocking live orders"
    return True, "public IP matches whitelist"
