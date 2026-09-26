"""Where a dropped item lands, in Trello's own terms (research R6).

Trello orders cards, lists, checklists and check items by a positive float
``pos``. A drop between two neighbours takes their midpoint; at an end, one step
beyond the neighbour. Whether Trello renumbers positions is undocumented, so the
mirror never trusts its own numbers to survive — every board re-read overwrites
``pos`` from Trello and ties break on id.
"""
from __future__ import annotations

STEP = 65536.0          # Trello's own spacing for new items
MIN_GAP = 1e-6


def between(before: float | None, after: float | None) -> float:
    """``before`` is the item above/left of the drop, ``after`` the one below/right.
    ``None`` means an end. Returns the new ``pos``."""
    if before is None and after is None:
        return STEP
    if before is None:
        return after / 2 if after > MIN_GAP * 2 else after - MIN_GAP
    if after is None:
        return before + STEP
    return (before + after) / 2


def crowded(before: float | None, after: float | None) -> bool:
    """True when the midpoint would no longer separate the neighbours — the rare
    case a caller answers by re-spacing the whole list."""
    return before is not None and after is not None and (after - before) < MIN_GAP * 2


def respace(count: int) -> list[float]:
    """Fresh, evenly spaced positions for ``count`` items in their current order."""
    return [STEP * (i + 1) for i in range(count)]


if __name__ == "__main__":                                      # pragma: no cover
    assert between(None, None) == STEP
    assert between(STEP, None) == 2 * STEP
    assert between(None, STEP) == STEP / 2
    assert between(STEP, 2 * STEP) == 1.5 * STEP
    assert crowded(1.0, 1.0 + 1e-7) and not crowded(1.0, 2.0)
    print("ok")
