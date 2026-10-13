"""Sites skipped in their last round (spec 038, research R1).

A skipped turn (spec 037) writes no poll-log row, so ``/api/health/details`` would
show the site's older result as if it were the latest. The skip is kept here, in
memory: a restart losing one is harmless, the next round writes a real row.
"""
from __future__ import annotations

from datetime import datetime, timezone

_skips: dict[str, tuple[str, str]] = {}


def record_skip(code: str, reason: str) -> None:
    _skips[code] = (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), reason)


def skips() -> dict[str, tuple[str, str]]:
    return dict(_skips)
