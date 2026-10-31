"""Polite HTTP: one shared session, a clear User-Agent, a delay per host."""
from __future__ import annotations

import time
from urllib.parse import urlparse

import requests

from .config import REQUEST_DELAY_S, REQUEST_TIMEOUT_S, USER_AGENT

_session: requests.Session | None = None
_last_hit: dict[str, float] = {}


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": USER_AGENT})
    return _session


def get(url: str, *, delay: float = REQUEST_DELAY_S, **kwargs) -> requests.Response:
    """GET with a minimum gap between requests to the same host."""
    host = urlparse(url).netloc
    wait = _last_hit.get(host, 0) + delay - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    try:
        return session().get(url, timeout=kwargs.pop("timeout", REQUEST_TIMEOUT_S), **kwargs)
    finally:
        _last_hit[host] = time.monotonic()
