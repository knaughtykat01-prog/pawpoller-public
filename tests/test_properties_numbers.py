"""Property tests: chart thinning, XP caps and level maths (PROPTESTS)."""
import sqlite3
from datetime import datetime, timedelta

import pytest

pytest.importorskip("hypothesis")
from hypothesis import given, strategies as st  # noqa: E402

import config  # noqa: E402
import xp  # noqa: E402
from database.queries import thin_series  # noqa: E402


@given(st.integers(min_value=0, max_value=5000), st.integers(min_value=2, max_value=400))
def test_thin_series_keeps_ends_order_and_size(length, n):
    snaps = [{"i": i} for i in range(length)]
    out = thin_series(snaps, n)
    if length <= n:
        assert out == snaps
        return
    assert len(out) == n
    assert out[0] is snaps[0] and out[-1] is snaps[-1]
    idx = [s["i"] for s in out]
    assert idx == sorted(set(idx))                              # in order, no repeats


@given(st.integers(min_value=1, max_value=200))
def test_levels_are_monotonic_and_consistent(level):
    assert xp.level_start(level) < xp.level_start(level + 1)
    assert xp.level_for(xp.level_start(level)) == level
    assert xp.level_for(xp.level_start(level + 1) - 1) == level


@given(st.integers(min_value=0, max_value=10**7), st.integers(min_value=0, max_value=10**7))
def test_more_xp_is_never_a_lower_level(a, b):
    lo, hi = sorted((a, b))
    assert xp.level_for(lo) <= xp.level_for(hi)


def _ledger() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE xp_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, award_key TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL, site TEXT, points INTEGER NOT NULL, capped INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL, earned_at TEXT NOT NULL, day TEXT NOT NULL)""")
    return conn


award_step = st.tuples(st.sampled_from(["work", "site", "words", "streak", "reply", "tidy"]),
                       st.sampled_from(["fa", "ib", "e6", None]),
                       st.integers(min_value=0, max_value=120),
                       st.integers(min_value=0, max_value=72),            # hours after the start
                       st.integers(min_value=0, max_value=30))            # key id: repeats on purpose


@given(st.lists(award_step, max_size=60))
def test_live_awards_never_pass_the_daily_caps_and_each_key_pays_once(steps):
    conn = _ledger()
    start = datetime(2026, 10, 1, 0, 0, 0)
    paid: dict[str, int] = {}
    for kind, site, pts, hours, key in steps:
        at = (start + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        if kind == "site":
            site = site or "fa"                                   # _award_post always names the site
        k = f"{kind}:{key}"
        got = xp.award(conn, kind, k, pts, at, site=site)
        assert 0 <= got <= pts
        if k in paid:
            assert got == 0                                       # a key is spent on its first award
        paid.setdefault(k, got)
    t = config.XP_TABLE
    for day, total in conn.execute("SELECT day, SUM(points) FROM xp_ledger WHERE source='live' GROUP BY day"):
        assert total <= t["cap_day"], day
    for day, site, total in conn.execute("SELECT day, site, SUM(points) FROM xp_ledger"
                                         " WHERE source='live' AND kind='site' GROUP BY day, site"):
        assert total <= t["cap_site_day"], (day, site)


@given(st.lists(st.integers(min_value=0, max_value=500), max_size=40))
def test_backfill_is_never_capped(points):
    conn = _ledger()
    for i, p in enumerate(points):
        assert xp.award(conn, "work", f"b:{i}", p, "2026-10-01 01:00:00", source="backfill") == p
