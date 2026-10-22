"""Experience points and levels (XPLEVELS, spec 050 Part 1; quickstart 1-9).

The sweep reads posting_log, so these tests seed posting_log rows directly and
check the ledger. No network, no personal data.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

import config
import xp

T = config.XP_TABLE
DAY = "2026-10-20 03:00:00"          # a Tuesday, UTC


@pytest.fixture(autouse=True)
def _levels_on():
    """Gamification is off until the person says yes (22 Oct); these tests are about it being on."""
    config.save_settings({"levels_enabled": True})


@pytest.fixture
def conn(db_conn):
    for t in ("xp_ledger", "xp_state"):
        db_conn.execute(f"DELETE FROM {t}")
    db_conn.commit()
    yield db_conn
    for t in ("xp_ledger", "xp_state"):
        db_conn.execute(f"DELETE FROM {t}")
    db_conn.commit()


def _log(conn, name, platform, *, at=DAY, status="success", action="post", ctype="artwork",
         account_id=1, chapter=0):
    conn.execute(
        "INSERT INTO posting_log (platform, story_name, chapter_index, action, status, account_id,"
        " content_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (platform, name, chapter, action, status, account_id, ctype, at))
    conn.commit()


def _live(conn):
    """Skip the one-off backfill, so what follows is live (capped) XP."""
    xp.sync(conn)


def _total(conn):
    return xp.totals(conn)["xp"]


# ── 1. The table ──────────────────────────────────────────────────

def test_one_work_four_sites_then_a_retry(conn):
    _live(conn)
    for p in ("fa", "ws", "bsky", "ib"):
        _log(conn, "Sample Piece", p)
    _log(conn, "Sample Piece", "da", status="failed")
    xp.sync(conn)
    assert _total(conn) == T["work"] + 4 * T["site"] + T["streak_week"]
    _log(conn, "Sample Piece", "da")                      # the retry succeeds
    xp.sync(conn)
    assert _total(conn) == T["work"] + 5 * T["site"] + T["streak_week"]


# ── 2. Once per work per site ─────────────────────────────────────

def test_fifty_reposts_earn_what_one_does(conn):
    _live(conn)
    _log(conn, "Sample Piece", "fa")
    xp.sync(conn)
    once = _total(conn)
    for _ in range(50):
        _log(conn, "Sample Piece", "fa")
    xp.sync(conn)
    assert _total(conn) == once


# ── 3. Caps ───────────────────────────────────────────────────────

def test_site_cap_clamps_and_flags(conn, monkeypatch):
    monkeypatch.setattr(xp, "datetime", _FrozenDT)
    _live(conn)
    for i in range(8):
        _log(conn, f"Piece {i}", "fa")
    xp.sync(conn)
    site = conn.execute("SELECT SUM(points) FROM xp_ledger WHERE kind = 'site' AND site = 'fa'").fetchone()[0]
    assert site == T["cap_site_day"]
    assert conn.execute("SELECT COUNT(*) FROM xp_ledger WHERE capped = 1").fetchone()[0] >= 1
    assert xp.totals(conn)["today"]["sites_capped"] == ["fa"]


def test_daily_cap_overall(conn):
    _live(conn)
    for i in range(40):
        _log(conn, f"Piece {i}", ("fa", "ws", "bsky", "ib", "da", "e621")[i % 6])
    xp.sync(conn)
    assert _total(conn) == T["cap_day"]


# ── 4. Backfill ───────────────────────────────────────────────────

def test_backfill_is_uncapped_idempotent_and_not_this_week(conn):
    for i in range(10):
        _log(conn, f"Piece {i}", "fa")
    xp.sync(conn)
    expected = 10 * (T["work"] + T["site"]) + T["streak_week"]
    assert _total(conn) == expected                               # no caps on history
    assert conn.execute("SELECT COUNT(*) FROM xp_ledger WHERE source != 'backfill'").fetchone()[0] == 0
    xp.sync(conn)
    assert _total(conn) == expected                               # re-run is a no-op
    t = xp.totals(conn)
    assert t["week"]["xp"] == 0 and t["backfilled_xp"] == expected


# ── 5. Words ──────────────────────────────────────────────────────

def test_words_once_per_chapter(conn):
    _live(conn)
    conn.execute("INSERT INTO publications (story_name, chapter_index, platform, word_count)"
                 " VALUES ('Sample Story', 1, 'ao3', 3400)")
    conn.commit()
    _log(conn, "Sample Story", "ao3", ctype="story", chapter=1)
    _log(conn, "Sample Story", "sf", ctype="story", chapter=1)
    xp.sync(conn)
    words = conn.execute("SELECT SUM(points) FROM xp_ledger WHERE kind = 'words'").fetchone()[0]
    assert words == 3 * T["words_per_1000"]


# ── 6. Streak weeks in the operator's zone ────────────────────────

def test_streak_week_follows_display_zone(conn, monkeypatch):
    monkeypatch.setattr(config, "display_zone", lambda: ZoneInfo("Australia/Sydney"))
    _live(conn)
    _log(conn, "A", "fa", at="2026-10-18 12:00:00")   # Sun 23:00 in Sydney: week 42
    _log(conn, "B", "fa", at="2026-10-17 01:00:00")   # Sat in Sydney: week 42 too
    _log(conn, "C", "fa", at="2026-10-18 13:30:00")   # Mon 00:30 in Sydney: week 43
    xp.sync(conn)
    keys = {r[0] for r in conn.execute("SELECT award_key FROM xp_ledger WHERE kind = 'streak'")}
    assert keys == {"week:2026-W42", "week:2026-W43"}


# ── 7. What earns nothing ─────────────────────────────────────────

def test_exclusions(conn):
    s = config.get_settings()
    s["never_post_account_ids"] = [9]
    config.save_settings(s)
    _live(conn)
    _log(conn, "Friend Piece", "fa", account_id=9)
    _log(conn, "Edited", "fa", action="update")
    _log(conn, "A social post", "bsky", ctype="post")
    _log(conn, "Pending", "fa", status="queued_desktop")
    xp.sync(conn)
    assert _total(conn) == 0


# ── 8. The level curve ────────────────────────────────────────────

def test_level_curve():
    assert [xp.level_start(n) for n in (1, 2, 3, 10)] == [0, 50, 150, 2250]
    assert xp.level_for(49) == 1 and xp.level_for(50) == 2 and xp.level_for(2250) == 10


def test_new_user_reaches_level_two(conn):
    _live(conn)
    _log(conn, "First Piece", "fa")
    _log(conn, "First Piece", "bsky")
    xp.sync(conn)
    assert xp.totals(conn)["level"] == 2                         # SC-001


# ── 9. Reply and tidy ─────────────────────────────────────────────

def test_reply_awards_once(conn):
    assert xp.award(conn, "reply", "reply:bsky:c1", T["reply"], site="bsky") == T["reply"]
    assert xp.award(conn, "reply", "reply:bsky:c1", T["reply"], site="bsky") == 0


def test_tidy_alt_text_once(conn):
    from routes.masterpieces_api import _award_tidy
    _award_tidy("Sample Piece", {}, {"alt_text": "A fox reading"})
    _award_tidy("Sample Piece", {"alt_text": ""}, {"alt_text": "Again"})     # cleared, refilled
    _award_tidy("Sample Piece", {}, {"tags": {"default": ["fox"]}})
    _award_tidy("Sample Piece", {"alt_text": "x"}, {"alt_text": "y"})        # already filled
    rows = sorted(r[0] for r in conn.execute("SELECT award_key FROM xp_ledger"))
    assert rows == ["tidy:alt:artwork:Sample Piece", "tidy:tags:artwork:Sample Piece"]


# ── The endpoint ──────────────────────────────────────────────────

def test_api_shape(conn):
    from routes.xp_api import get_xp
    body = get_xp()
    assert set(body) == {"enabled", "xp", "level", "level_start", "next_level_at", "today", "week",
                         "backfilled_xp", "audience_xp", "rewards"}
    assert body["enabled"] is True and body["level"] == 1 and body["next_level_at"] == 50
    assert set(body["week"]["by_kind"]) == {"posting", "writing", "community", "care"}


class _FrozenDT(xp.datetime):
    """`today` in totals() must be the seeded DAY for the per-site check."""
    @classmethod
    def now(cls, tz=None):
        return xp.datetime(2026, 10, 20, 3, 0, 0, tzinfo=tz or xp.timezone.utc)


# ── levels.js: the moment fires once (US1 #2, US2) ─────────────────

import json as _json
import shutil as _shutil
import subprocess as _subprocess
from pathlib import Path as _Path

_LEVELS_JS = (_Path(__file__).resolve().parent.parent / "frontend" / "js" / "levels.js").read_text(encoding="utf-8")
_node = pytest.mark.skipif(_shutil.which("node") is None, reason="node is not installed")


def _levels(states: list, store: dict | None = None, visible: bool = True, replay: bool = False) -> dict:
    """Feed /api/xp bodies to Levels.refresh() in turn; report sounds and toasts."""
    harness = """
    const store = %s, states = %s, sounds = [], toasts = [];
    global.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } };
    global.window = { addEventListener() {} };
    global.document = { visibilityState: %s, getElementById: () => null };
    let i = 0;
    global.API = { get: async () => states[i++] };
    global.Sound = { play: n => sounds.push(n) }; window.Sound = Sound;
    global.toast = { success: m => toasts.push(m) }; window.toast = toast;
    %s
    (async () => { for (const _ of states) await window.Levels.refresh();
      if (%s) await window.Levels.replay();
      console.log(JSON.stringify({ sounds, toasts, store })); })();
    """ % (_json.dumps(store or {}), _json.dumps(states), "'visible'" if visible else "'hidden'", _LEVELS_JS, "true" if replay else "false")
    r = _subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return _json.loads(r.stdout.strip().splitlines()[-1])


def _st(level, enabled=True):
    return {"enabled": enabled, "xp": xp.level_start(level), "level": level, "level_start": xp.level_start(level),
            "next_level_at": xp.level_start(level + 1), "today": {"xp": 0, "cap": 300, "sites_capped": []}}


@_node
def test_first_open_is_one_quiet_note():
    out = _levels([_st(7), _st(7)])
    assert out["sounds"] == [] and len(out["toasts"]) == 1 and "level 7" in out["toasts"][0]
    assert out["store"]["pp-xp-seen-level"] == "7"


@_node
def test_level_up_once_and_not_in_a_hidden_tab():
    out = _levels([_st(3), _st(4), _st(4)], store={"pp-xp-seen-level": "3"})
    assert out["sounds"] == ["level_up"] and out["store"]["pp-xp-seen-level"] == "4"
    hidden = _levels([_st(4)], store={"pp-xp-seen-level": "3"}, visible=False)
    assert hidden["sounds"] == [] and hidden["store"]["pp-xp-seen-level"] == "3"


@_node
def test_levels_off_never_celebrates():
    out = _levels([_st(5, enabled=False)], store={"pp-xp-seen-level": "2"})
    assert out["sounds"] == [] and out["toasts"] == []


@_node
def test_replay_plays_without_moving_the_seen_level():
    out = _levels([_st(18)], store={"pp-xp-seen-level": "18"}, replay=True)
    assert out["sounds"] == ["level_up"] and out["store"]["pp-xp-seen-level"] == "18"

def test_gamification_is_off_until_the_person_says_yes(conn):
    """22 Oct: levels, medals and rewards are a choice. Unanswered = off, and the app asks."""
    from routes import api
    config.delete_settings_keys(["levels_enabled"])
    assert xp.enabled() is False and xp.totals(conn)["enabled"] is False
    p = api.get_preferences()
    assert p["levels_enabled"] is False and p["gamification_asked"] is False
    api.save_preferences({"levels_enabled": False})            # "No thanks" is an answer too
    p = api.get_preferences()
    assert p["levels_enabled"] is False and p["gamification_asked"] is True
    api.save_preferences({"levels_enabled": True})
    assert xp.enabled() is True and api.get_preferences()["levels_enabled"] is True
