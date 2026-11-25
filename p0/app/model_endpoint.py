"""Outbound model endpoint policy for private deployments.

F75: public mode keeps the existing endpoints working.
F76: public IPs and deceptive hostnames must fail in local-only mode.
F77: an explicitly named internal service may be used by Docker deployments.
F78: malformed URLs must fail closed before a request is built.
F79: every model transport must check this policy before sending content.
F80: redirects and environment proxies must not bypass local-only routing.
F81: production mode enforces local-only routing even when the optional flag is unset or explicitly false.
"""
from __future__ import annotations

import ipaddress
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit


_LOCAL_RANGES = tuple(ipaddress.ip_network(value) for value in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "::1/128", "fc00::/7",
))


def local_only() -> bool:
    # Production can process customer data, so a deployment typo or an
    # explicit opt-out must not silently restore public model routing.
    if os.getenv("P0_ENV", "").strip().lower() == "production":
        return True
    value = os.getenv("P0_LOCAL_MODEL_ONLY", "").strip().lower()
    if not value:
        return False
    # Only explicit false values disable the guard. A typo must fail closed.
    return value not in {"0", "false", "no", "off"}


def is_local_endpoint(base_url: str) -> bool:
    """Accept local IPs, localhost, or exact operator-approved service names."""
    try:
        parsed = _parse_endpoint(base_url)
    except (AttributeError, ValueError):
        return False
    if parsed is None:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        allowed = {
            item.strip().lower().rstrip(".")
            for item in os.getenv("P0_LOCAL_MODEL_HOSTS", "").split(",") if item.strip()
        }
        # Only single-label names are supported here. An FQDN may resolve
        # outside the private network and must be blocked at the app boundary.
        return "." not in host and host in allowed
    return any(address in network for network in _LOCAL_RANGES)


def _parse_endpoint(base_url: str):
    try:
        parsed = urlsplit(base_url.strip())
        host = parsed.hostname
        _ = parsed.port  # Invalid ports raise ValueError.
    except (AttributeError, ValueError):
        return None
    if parsed.scheme not in {"http", "https"} or not host:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    return parsed


def is_valid_endpoint(base_url: str) -> bool:
    return _parse_endpoint(base_url) is not None


def endpoint_allowed(base_url: str) -> bool:
    return is_valid_endpoint(base_url) and (not local_only() or is_local_endpoint(base_url))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        return None


def open_model_request(request: urllib.request.Request | str, timeout: float):
    """In local-only mode, bypass proxies and reject all HTTP redirects."""
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    if not endpoint_allowed(url):
        raise urllib.error.URLError("model endpoint is outside the local-only allowlist")
    if not local_only():
        return urllib.request.urlopen(request, timeout=timeout)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(request, timeout=timeout)
