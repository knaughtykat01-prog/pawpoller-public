"""Clock-anchored poll slots for hosted seats (4.72.0, spec 037).

A seat polls at its own start minute, then every interval after that, counted on the UTC clock from the Unix epoch.
Before this a seat slept a whole interval after each poll *finished*, so seats drifted into step and ended up
polling the same sites at the same moment from the box's one home address.

Only seats get a start minute (``homebox.sh`` sets ``PAWPOLLER_POLL_OFFSET_MINUTES``). Without one, an install keeps
the old interval-after-finish schedule.
"""
from __future__ import annotations

import math
import os


def offset_minutes() -> int | None:
    """The seat's start minute (0-59), or None when this isn't a seat."""
    raw = (os.environ.get("PAWPOLLER_POLL_OFFSET_MINUTES") or "").strip()
    try:
        n = int(raw)
    except ValueError:
        return None
    return n if 0 <= n <= 59 else None


def slot_mode() -> bool:
    return offset_minutes() is not None


def next_slot(now: float, offset_min: int, interval_s: int) -> float:
    """The first slot strictly later than ``now``: offset + k x interval seconds after the epoch.

    Strictly later, so an overrun or a restart rolls to the next slot: missed slots are never caught up.
    """
    interval_s = max(int(interval_s), 60)
    base = offset_min * 60
    k = math.floor((now - base) / interval_s) + 1
    return base + k * interval_s
