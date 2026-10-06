"""Explicitly enabled, credential-free GET requests to public market APIs."""
from urllib.parse import urlsplit

import requests as _requests
from core.config import Config

_HOSTS = frozenset({
    "gamma-api.polymarket.com", "clob.polymarket.com",
    "api.binance.com", "api.coingecko.com",
})


def get(url, *, params=None, timeout=10):
    if not Config.ALLOW_PUBLIC_DATA:
        raise RuntimeError("Public data access is disabled. Use the scan command explicitly.")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in _HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("Unsupported public data endpoint")
    # Do not inherit proxy credentials, .netrc, redirects or wallet authentication.
    with _requests.Session() as session:
        session.trust_env = False
        response = session.get(url, params=params, timeout=timeout, allow_redirects=False)
        if 300 <= response.status_code < 400:
            raise RuntimeError("Public data redirects are not followed")
        return response
