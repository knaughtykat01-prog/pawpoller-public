"""CHARTTHIN: every site's /aggregate series is cut to about 300 polls for its chart."""
from __future__ import annotations

import re
from pathlib import Path

from database.queries import CHART_POINTS, thin_series

ROOT = Path(__file__).resolve().parents[1]


def test_a_long_series_keeps_its_ends_and_order():
    snaps = [{"polled_at": f"t{i:05d}", "views": i} for i in range(3244)]
    out = thin_series(snaps)
    assert len(out) == CHART_POINTS
    assert out[0] == snaps[0] and out[-1] == snaps[-1]
    assert [s["views"] for s in out] == sorted({s["views"] for s in out})     # in order, no repeats


def test_a_short_series_is_untouched():
    snaps = [{"views": i} for i in range(CHART_POINTS)]
    assert thin_series(snaps) is snaps and thin_series([]) == []


def test_every_aggregate_route_thins():
    files = [f for f in (ROOT / "routes").glob("*.py") if '.get("/aggregate")' in f.read_text(encoding="utf-8")]
    assert len(files) >= 27
    for f in files:
        src = f.read_text(encoding="utf-8")
        body = src[src.index('.get("/aggregate")'):]
        assert re.search(r'return \{"snapshots": (queries\.)?thin_series\(', body), f
