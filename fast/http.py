"""Shared HTTP plumbing for the fast fetchers."""
from __future__ import annotations

import threading

from curl_cffi import requests

_local = threading.local()


def session() -> requests.Session:
    """One curl_cffi session per thread, impersonating desktop Chrome.

    Carrier bot checks reject Python's own TLS handshake outright (plain
    requests/curl get a 403 from Aramex); curl_cffi presents Chrome's. Sessions
    are not shared across threads, so each worker keeps its own.
    """
    s = getattr(_local, "s", None)
    if s is None:
        s = _local.s = requests.Session(impersonate="chrome")
    return s


class TransientError(Exception):
    """The carrier didn't answer usefully (blocked, timeout, 5xx). The number
    itself may be fine — a caller should retry it another way."""
