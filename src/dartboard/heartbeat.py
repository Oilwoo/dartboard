"""Heartbeat to an outside monitor (healthchecks.io style URL): when the pings stop, the monitor alerts the owner.

The ping URL works like a password for the check, so it is never logged.
"""
from __future__ import annotations

import logging
import urllib.request
from typing import Callable

log = logging.getLogger(__name__)

Opener = Callable[..., object]


def ping(url: str, ok: bool = True, opener: Opener = urllib.request.urlopen, timeout: float = 10) -> bool:
    """A success ping, or `<url>/fail` after a failed run. Never raises: a monitor outage must not stop the bot."""
    if not url:
        return False
    target = url.rstrip("/") + ("" if ok else "/fail")
    try:
        with opener(urllib.request.Request(target, method="HEAD"), timeout=timeout):
            return True
    except Exception as e:
        log.warning("heartbeat ping failed (%s)", type(e).__name__)
        return False
