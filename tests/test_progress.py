"""Progress everywhere (WORKLEVELS, spec 052; quickstart 1-7, 9-10).

Seeds submissions, snapshots and publications directly and checks levels, audience XP,
medals and performance. No network; names are the shared fixtures (Sample Story, Inkwolf).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
import medals
import progress
import xp
from database import platform_metrics

ART = ("artwork", "Sample Story")
WORKS = {ART: "Sample Story"}
FMT = "%Y-%m-%d %H:%M:%S"


@pytest.fixture(autouse=True)
def _levels_on():
    """Gamification is off until the person says yes (22 Oct); these tests are about it being on."""
    config.save_settings({"levels_enabled": True})


@pytest.fixture
def conn(db_conn):
    for cache in (progress._AT, progress._CACHE, progress._SHARE):
        cache.clear()
    yield db_conn


def _ago(**kw) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(**kw)


def _sub(conn, code, sid, posted: datetime, acct=1, **stats):
    spec = platform_metrics.get(code)
    cols = {getattr(spec, k): v for k, v in stats.items()}
    names = ", ".join(["submission_id", "title", "posted_at", "account_id", *cols])
    conn.execute(f"INSERT OR REPLACE INTO {spec.table} ({names}) VALUES ({', '.join('?' * (4 + len(cols)))})",
                 (sid, "Sample Story", posted.strftime(FMT), acct, *cols.values()))
    conn.commit()


def _snap(conn, code, sid, at: datetime, acct=1, **stats):
    spec = platform_metrics.get(code)
    cols = {getattr(spec, k): v for k, v in stats.items()}
    names = ", ".join(["submission_id", "polled_at", "account_id", *cols])
    conn.execute(f"INSERT INTO {spec.snapshots} ({names}) VALUES ({', '.join('?' * (3 + len(cols)))})",
                 (sid, at.strftime(FMT), acct, *cols.values()))
    conn.commit()


def _pub(conn, code, sid, name="Sample Story", ctype="artwork", acct=1):
    conn.execute("INSERT INTO publications (content_type, story_name, platform, account_id, external_id, status,"
                 " first_posted_at) VALUES (?, ?, ?, ?, ?, 'posted', ?)",
                 (ctype, name, code, acct, str(sid), _ago(days=1).strftime(FMT)))
    conn.commit()


def _level(conn, key=ART):
    return conn.execute("SELECT level, peak_level, paid_level FROM work_levels WHERE content_type = ? AND"
                        " work_name = ?", key).fetchone()


def _audience(conn):
    return xp._sum(conn, "source = 'audience'", ())


# ── T005: the scale ──────────────────────────────────────────────

@pytest.mark.parametrize("s,lvl", [(0, 1), (9, 1), (10, 1), (19, 1), (20, 2), (39, 2), (40, 3), (905, 7),
                                   (100_000, 14)])
def test_scale(s, lvl):
    assert progress.level_for_score(s) == lvl


def test_every_platform_maps():
    full = {"views": 2, "faves": 3, "comments": 1, "score": 4}
    for code in platform_metrics.ALL_CODES:
        assert progress.score(code, full) >= 0, code
    assert progress.score("fa", full) == 2 + 15 + 20
    assert progress.score("e621", full) == 5 * (3 + 4) + 20          # the net score sits with faves
    assert progress.score("e621", {**full, "score": -9}) == 15 + 20  # a negative score adds nothing


# ── US1: levels and peaks ─────────────────────────────────────────

def test_level_and_peak_never_drops(conn):
    _sub(conn, "fa", 101, _ago(days=9), views=40)
    _pub(conn, "fa", 101)
    progress.sync(conn, WORKS)
    assert _level(conn)[:2] == (3, 3)
    conn.execute("UPDATE fa_submissions SET views = 90 WHERE submission_id = 101")
    progress.sync(conn, WORKS)
    assert _level(conn)[:2] == (4, 4)
    conn.execute("UPDATE fa_submissions SET views = 40 WHERE submission_id = 101")   # a recount
    progress.sync(conn, WORKS)
    assert _level(conn)[:2] == (3, 4)


def test_no_view_site_counts_likes_and_comments(conn):
    _sub(conn, "bsky", "at://x/1", _ago(days=3), faves=4, comments=1)
    _pub(conn, "bsky", "at://x/1")
    synced = progress.sync(conn, WORKS)
    assert _level(conn)[0] == 3
    assert synced["scores"][ART]["no_view_sites"] == ["bsky"]


def test_members_pool_with_publications_once(conn):
    _sub(conn, "fa", 101, _ago(days=9), views=40)
    _sub(conn, "ws", 7, _ago(days=9), views=40)
    _pub(conn, "fa", 101)
    conn.execute("INSERT INTO masterpiece_members (masterpiece_name, platform, submission_id, account_id)"
                 " VALUES ('Sample Story', 'fa', '101', 1), ('Sample Story', 'ws', '7', 1)")
    assert progress.work_scores(conn, WORKS)[ART]["score"] == 80


def test_works_rows_carry_level():
    from routes.submissions_api import assemble_works
    out = assemble_works(stories=[], artworks=[{"name": "Sample Story"}], pubs=[], acct_to_persona={},
                         personas={}, levels={ART: 6})
    assert out["works"][0]["level"] == 6
    assert assemble_works(stories=[], artworks=[{"name": "Sample Story"}], pubs=[], acct_to_persona={},
                          personas={})["works"][0]["level"] == 1


# ── US3: audience XP ──────────────────────────────────────────────

def test_backpay_then_each_level_once(conn):
    _sub(conn, "fa", 101, _ago(days=9), views=40)                    # level 3
    _pub(conn, "fa", 101)
    progress.sync(conn, WORKS)
    assert _audience(conn) == 10 + 20 + 30                            # back-pay (R4)
    assert int(xp._state(conn, "audience_backpay_pending")) == 60
    conn.execute("UPDATE fa_submissions SET views = 320 WHERE submission_id = 101")   # 3 → 6
    progress.sync(conn, WORKS)
    assert _audience(conn) == 60 + 40 + 50 + 60
    progress.sync(conn, WORKS)                                        # a re-poll adds nothing
    assert _audience(conn) == 210
    conn.execute("UPDATE fa_submissions SET views = 640 WHERE submission_id = 101")   # 6 → 7
    progress.sync(conn, WORKS)
    assert _audience(conn) == 280
    t = xp.totals(conn)
    assert t["today"]["xp"] == 0 and t["week"]["xp"] == 0             # outside caps and the week
    assert t["audience_xp"] == 280


def test_levels_after_the_baseline_are_queued(conn):
    _sub(conn, "fa", 101, _ago(days=9), views=40)
    _pub(conn, "fa", 101)
    synced = progress.sync(conn, WORKS)
    assert medals.pending(conn, synced["titles"])["works"] == []      # the back-pay is one catch-up line
    conn.execute("UPDATE fa_submissions SET views = 320 WHERE submission_id = 101")
    synced = progress.sync(conn, WORKS)
    works = medals.pending(conn, synced["titles"])["works"]
    assert works == [{"type": "artwork", "name": "Sample Story", "title": "Sample Story",
                      "from": 3, "to": 6, "xp": 150}]
    medals.announce(conn, {"works": [{"type": "artwork", "name": "Sample Story", "level": 6}]})
    assert medals.pending(conn, synced["titles"])["works"] == []


def test_never_post_works_pay_nothing(conn):
    config.save_settings({"never_post_account_ids": [2]})
    _sub(conn, "fa", 101, _ago(days=9), acct=2, views=640)
    _pub(conn, "fa", 101, acct=2)
    progress.sync(conn, WORKS)
    assert _level(conn)[0] == 7 and _audience(conn) == 0


def test_merge_never_pays_a_level_twice(conn):
    other = ("artwork", "Sample Story Copy")
    _sub(conn, "fa", 101, _ago(days=9), views=40)
    _sub(conn, "ws", 7, _ago(days=9), views=320)
    _pub(conn, "fa", 101)
    _pub(conn, "ws", 7, name=other[1])
    works = {**WORKS, other: other[1]}
    progress.sync(conn, works)
    paid = _audience(conn)
    from database import masterpiece_queries
    masterpiece_queries.merge_masterpieces(conn, ART[1], other[1])
    conn.execute("UPDATE publications SET story_name = ? WHERE story_name = ?", (ART[1], other[1]))
    progress.sync(conn, WORKS)                         # 360 → level 6, already paid via the copy
    assert _level(conn)[1:] == (6, 6)
    assert _audience(conn) == paid


# ── US2: medals ───────────────────────────────────────────────────

def _earned(conn):
    return {(r[0], r[1]): r[2:] for r in conn.execute(
        "SELECT medal_id, subject, source, announced_at, dated_exactly FROM earned_medals")}


def test_catchup_then_live_medal_once(conn):
    conn.execute("INSERT INTO accounts (account_id, platform, label) VALUES (1, 'fa', 'Inkwolf')")
    _sub(conn, "fa", 101, _ago(days=9), views=150)
    _pub(conn, "fa", 101)
    medals.evaluate(conn, progress.sync(conn, WORKS))
    rows = _earned(conn)
    assert ("views-100", "install") in rows and ("w_views_100", "work:artwork:Sample Story") in rows
    assert all(src == "catchup" and ann and not exact for src, ann, exact in rows.values())
    assert medals.pending(conn, WORKS)["catchup_count"] == sum(1 for (_, s) in rows if s == "install")
    medals.announce(conn, {"catchup": True})
    assert medals.pending(conn, WORKS)["catchup_count"] == 0

    conn.execute("UPDATE fa_submissions SET views = 260 WHERE submission_id = 101")
    medals.evaluate(conn, progress.sync(conn, WORKS))
    queued = medals.pending(conn, WORKS)["medals"]
    assert [m["id"] for m in queued] == ["views-250"]
    medals.announce(conn, {"medals": [{"id": "views-250", "subject": "install"}]})
    medals.announce(conn, {"medals": [{"id": "views-250", "subject": "install"}]})   # idempotent
    assert medals.pending(conn, WORKS)["medals"] == []

    conn.execute("UPDATE fa_submissions SET views = 5 WHERE submission_id = 101")    # totals drop
    medals.evaluate(conn, progress.sync(conn, WORKS))
    listed = {m["id"]: m for m in medals.install_list(conn, progress.work_scores(conn, WORKS))}
    assert listed["views-250"]["earned"] and listed["views-250"]["earned_at"]


def test_work_medal_boundaries():
    c = config.PROGRESS_TABLE["medals"]
    assert "w_first_comment" not in medals.work_checks({"comments": 0})
    assert "w_first_comment" in medals.work_checks({"comments": 1})
    assert "w_sites_3" not in medals.work_checks({"platforms": {"fa", "ws"}})
    assert "w_sites_3" in medals.work_checks({"platforms": {"fa", "ws", "ib"}})
    assert "w_views_1k" not in medals.work_checks({"views": 999})
    assert "w_views_1k" in medals.work_checks({"views": 1000})

    def site(code, ratio, value=10):
        return {"code": code, "ratio": ratio, "value": value}
    two = lambda r: {48: {"blended": r, "sites": [site("fa", r), site("ws", r)]}}
    assert "w_breakout" not in medals.perf_checks(two(c["breakout_ratio"] - 0.01), {})
    assert "w_breakout" in medals.perf_checks(two(c["breakout_ratio"]), {})
    assert "w_breakout" not in medals.perf_checks({48: {"blended": 9, "sites": [site("fa", 9)]}}, {})

    burn = lambda v60: {720: {"sites": [site("fa", None, 100)]}, 1440: {"sites": [site("fa", None, v60)]}}
    assert "w_slow_burn" not in medals.perf_checks(burn(124), {})
    assert "w_slow_burn" in medals.perf_checks(burn(125), {})

    week = lambda depth: {168: {"blended": 0.5, "depth": depth, "usual_depth": 0.05, "sites": []}}
    assert "w_found_people" not in medals.perf_checks(week(0.099), {"faves": 5})
    assert "w_found_people" in medals.perf_checks(week(0.10), {"faves": 5})
    assert "w_found_people" not in medals.perf_checks(week(0.10), {"faves": 4})


# ── US4: performance ──────────────────────────────────────────────

def _history(conn, values, code="fa", start=100, views_at=48):
    """Earlier pieces on one site, each with one snapshot at ``views_at`` hours."""
    for i, v in enumerate(values):
        posted = _ago(days=20 + i)
        _sub(conn, code, start + i, posted, views=v)
        _snap(conn, code, start + i, posted + timedelta(hours=views_at), views=v)


def _new_piece(conn, views, code="fa", sid=1):
    posted = _ago(days=3)
    _sub(conn, code, sid, posted, views=views)
    _snap(conn, code, sid, posted + timedelta(hours=48), views=views)
    _pub(conn, code, sid)


def test_three_point_one_times_your_usual(conn):
    _history(conn, [100] * 5)
    _new_piece(conn, 310)
    _sub(conn, "bsky", "at://x/1", _ago(days=3), faves=4)
    _snap(conn, "bsky", "at://x/1", _ago(days=1), faves=4)
    _pub(conn, "bsky", "at://x/1")
    p = progress.performance(conn, *ART)
    assert p["at_age_h"] == 48 and p["blended"] == 3.1
    rows = {r["code"]: r for r in p["sites"]}
    assert rows["fa"]["usual"] == 100 and rows["fa"]["ratio"] == 3.1
    assert rows["bsky"]["ratio"] is None and rows["bsky"]["reason"] == "not enough history yet"
    assert p["top_pct"] == 17


def test_four_pieces_is_not_enough_history(conn):
    _history(conn, [100] * 4)
    _new_piece(conn, 310)
    p = progress.performance(conn, *ART)
    assert p["blended"] is None and p["sites"][0]["reason"] == "not enough history yet"


def test_median_resists_one_outlier(conn):
    _history(conn, [100, 100, 100, 100, 10_000])
    _new_piece(conn, 200)
    assert progress.performance(conn, *ART)["blended"] == 2.0


def test_blend_follows_audience_share(conn):
    rows = [{"code": "fa", "ratio": 4.0}, {"code": "ws", "ratio": 1.0}]
    assert progress._blend(rows, {"fa": 3, "ws": 1}) == 3.25
    assert progress._blend(rows, {}) == 2.5


def test_top_pct_left_out_at_fortieth_percentile(conn):
    _history(conn, [50, 80, 120, 150, 200, 300, 400, 500, 600])
    _new_piece(conn, 310)                              # 4 of 9 above it: top 50%
    p = progress.performance(conn, *ART)
    assert p["blended"] is not None and "top_pct" not in p


def test_young_piece_has_no_performance(conn):
    _sub(conn, "fa", 1, _ago(hours=20), views=10)
    _pub(conn, "fa", 1)
    assert progress.performance(conn, *ART) is None


# ── US6: account and persona levels ───────────────────────────────

def test_account_levels_follow_the_work_scale(conn, monkeypatch):
    from database import accounts as accounts_db
    from routes import progress_api
    monkeypatch.setattr(progress, "_catalogue", lambda: WORKS)
    conn.execute("INSERT INTO personas (persona_id, name) VALUES (1, 'Inkwolf')")
    conn.execute("INSERT INTO accounts (account_id, platform, label, persona_id) VALUES (1, 'fa', 'Inkwolf', 1)")
    conn.execute("INSERT INTO accounts (account_id, platform, label, persona_id) VALUES (2, 'ib', 'Penwright', 1)")
    config.save_settings({"never_post_account_ids": [2]})          # a never-post account still gets a level
    stats = {1: {"views": 300, "favorites": 4, "comments": 1}, 2: {"views": 80, "favorites": 0, "comments": 0}}
    monkeypatch.setattr(accounts_db, "account_stats", lambda c, aid, plat: stats.get(aid))
    conn.commit()                                                  # the route opens its own connection
    body = progress_api.get_progress()
    got = {a["subject"]: a["level"] for a in body["accounts"]}
    assert got == {"acct:1": progress.level_for_score(340), "acct:2": progress.level_for_score(80)}
    assert body["personas"] == [{"subject": "persona:1", "level": progress.level_for_score(420), "score": 420}]
    assert _audience(conn) == 0                                    # accounts give levels, never XP
    conn.execute("DELETE FROM accounts WHERE account_id = 2")      # a removed account isn't listed
    conn.commit()
    assert [a["subject"] for a in progress_api.get_progress()["accounts"]] == ["acct:1"]


# ── US5: the Overnight sheet ──────────────────────────────────────

def _window():
    now = datetime.now(timezone.utc)
    return now - timedelta(hours=8), now + timedelta(minutes=1)


def test_overnight_progress_lists_what_moved(conn, monkeypatch):
    from polling import overnight
    monkeypatch.setattr(progress, "_catalogue", lambda: WORKS)
    _sub(conn, "fa", 101, _ago(days=9), views=40)
    _pub(conn, "fa", 101)
    medals.evaluate(conn, progress.sync(conn, WORKS))       # the baseline: back-pay and catch-up
    start, end = _window()
    conn.execute("UPDATE xp_ledger SET earned_at = ?", ((start - timedelta(hours=1)).strftime(FMT),))
    conn.execute("UPDATE xp_state SET value = ? WHERE key = 'progress_baselined_at'",
                 ((start - timedelta(hours=1)).strftime(FMT),))
    conn.execute("UPDATE earned_medals SET earned_at = ?", ((start - timedelta(hours=1)).strftime(FMT),))
    assert overnight.progress_block(conn, start, end) is None          # nothing progressed
    conn.execute("UPDATE fa_submissions SET views = 320 WHERE submission_id = 101")
    medals.evaluate(conn, progress.sync(conn, WORKS))
    p = overnight.progress_block(conn, start, end)
    assert p["xp"] == 150
    assert p["work_levelups"] == [{"title": "Sample Story", "level": 6}]
    assert {m["name"] for m in p["medals"]} >= {"100 views"}


def test_levels_off_still_records_but_announces_nothing(conn, monkeypatch):
    from polling import overnight
    from routes import progress_api
    monkeypatch.setattr(progress, "_catalogue", lambda: WORKS)
    config.save_settings({"levels_enabled": False})
    _sub(conn, "fa", 101, _ago(days=9), views=40)
    _pub(conn, "fa", 101)
    body = progress_api.get_progress()
    assert body["enabled"] is False and body["announce"]["medals"] == [] and body["announce"]["works"] == []
    assert _level(conn)[0] == 3 and _audience(conn) == 60
    start, end = _window()
    assert overnight.progress_block(conn, start, end) is None


def test_junked_work_keeps_its_level_but_pays_and_announces_nothing(conn):
    _sub(conn, "fa", 101, _ago(days=9), views=40)
    _pub(conn, "fa", 101)
    conn.execute("INSERT INTO masterpieces (name, status) VALUES ('Sample Story', 'junk')")
    synced = progress.sync(conn, WORKS)
    conn.execute("UPDATE fa_submissions SET views = 320 WHERE submission_id = 101")
    synced = progress.sync(conn, WORKS)
    assert _level(conn)[:2] == (6, 6) and _audience(conn) == 0
    assert medals.pending(conn, synced["titles"])["works"] == []
    conn.execute("UPDATE masterpieces SET status = '' WHERE name = 'Sample Story'")   # unjunked
    progress.sync(conn, WORKS)
    assert _audience(conn) == 10 + 20 + 30 + 40 + 50 + 60


# ── SC-004: no failure framing ────────────────────────────────────

def test_no_failure_language():
    """The progress screens never call a piece bad (FR-010): banned words in the new UI
    strings, and no warning or danger colours in the new styles."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    texts = [root / "progress.py", root / "medals.py", root / "frontend/js/progress.js"]
    banned = re.compile(r"\b(underperform\w*|poor(ly)?|bad|failed|failing|worse)\b", re.I)
    for f in texts:
        for line in f.read_text(encoding="utf-8").splitlines():
            for s in re.findall(r"[\"'`]([^\"'`]{3,})[\"'`]", line):
                assert not banned.search(s), f"{f.name}: {s}"
    css = (root / "frontend/css/levels.css").read_text(encoding="utf-8")
    block = css[css.index("/* spec 052"):] if "/* spec 052" in css else ""
    assert block, "levels.css should carry the spec 052 styles"
    assert not re.search(r"--(danger|error|warn\w*)|#(c0453b|e53e3e|dc2626)\b|\bred\b", block, re.I)
