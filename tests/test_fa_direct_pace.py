"""FurAffinity direct polling asks for far fewer pages (spec 092).

A direct poll used to read every piece's page: about 350 requests per poll for a 342-piece gallery. These pin
which pieces a scheduled direct poll reads (new, the newest 48, a capped slice of older ones), that every other
gallery piece keeps a snapshot at the poll's time so totals never dip, and that nothing crosses accounts.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import config
from clients.fa.client import FAClient, _check_challenge
from database import fa_queries
from database.db import get_connection
from polling import fa_poller
from polling.fa_poller import _direct_ids_to_read

FMT = "%Y-%m-%d %H:%M:%S"
NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
GALLERY = list(range(1342, 1000, -1))          # 342 pieces, newest first, as FA lists them
FRESH = GALLERY[:48]
OLDER = GALLERY[48:]                           # 294


def _ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).strftime(FMT)


def _stored(hours_ago: float, ids=GALLERY, views: int = 5) -> dict:
    return {i: (_ago(hours_ago), views) for i in ids}


# ── Which pieces a poll reads (US1) ──────────────────────────

def test_nothing_older_due_reads_only_the_newest_48():
    assert _direct_ids_to_read(GALLERY, _stored(4), NOW, False) == FRESH


def test_older_due_reads_newest_48_plus_a_sixth_oldest_first():
    stored = _stored(21)
    longest_ago = OLDER[100:149]                 # 49 pieces read even longer ago
    for i in longest_ago:
        stored[i] = (_ago(30), 5)
    got = _direct_ids_to_read(GALLERY, stored, NOW, False)
    assert len(got) == 48 + 49                   # ceil(294 / 6) = 49
    assert set(got) == set(FRESH) | set(longest_ago)
    assert got == [i for i in GALLERY if i in set(got)]      # gallery order kept


def test_a_piece_not_stored_yet_is_read_whatever_its_age():
    stored = _stored(4)
    del stored[1001]                             # the oldest piece in the gallery
    assert 1001 in _direct_ids_to_read(GALLERY, stored, NOW, False)


def test_a_stored_piece_with_no_views_counts_as_never_read():
    stored = _stored(4)
    stored[1001] = (_ago(1), 0)
    assert 1001 in _direct_ids_to_read(GALLERY, stored, NOW, False)


def test_force_full_reads_everything():
    assert _direct_ids_to_read(GALLERY, _stored(1), NOW, True) == GALLERY


def test_small_gallery_is_read_whole():
    small = GALLERY[:40]
    assert _direct_ids_to_read(small, _stored(1, small), NOW, False) == small


def test_19_hours_is_not_due_and_20_is():
    assert len(_direct_ids_to_read(GALLERY, _stored(19), NOW, False)) == 48
    assert len(_direct_ids_to_read(GALLERY, _stored(20), NOW, False)) == 97


def test_a_missing_last_read_time_is_due_and_goes_first():
    stored = _stored(21)
    stored[OLDER[200]] = (None, 5)
    stored[OLDER[201]] = ("", 5)
    got = _direct_ids_to_read(GALLERY, stored, NOW, False)
    assert len(got) == 97 and OLDER[200] in got and OLDER[201] in got


@pytest.mark.parametrize("interval_hours", [1, 4])
def test_every_older_piece_is_read_within_a_day(interval_hours):
    """SC-002: after the first sweep, no older piece waits more than a day (plus one poll) between reads."""
    stored = _stored(0)
    last = {i: NOW for i in OLDER}
    worst = timedelta(0)
    now = NOW
    for n in range(1, 30 * 24 // interval_hours // 6):           # five days of polls
        now = NOW + timedelta(hours=interval_hours * n)
        ids = _direct_ids_to_read(GALLERY, stored, now, False)
        assert len(ids) <= 97                                    # SC-001, every poll
        for i in ids:
            stored[i] = (now.strftime(FMT), 5)
            if i in last:
                if now - NOW > timedelta(hours=44):              # past the first sweep after an update
                    worst = max(worst, now - last[i])
                last[i] = now
    assert all(now - t <= timedelta(hours=24 + interval_hours) for t in last.values())
    assert timedelta(0) < worst <= timedelta(hours=24 + interval_hours)


# ── Carry-forward (US2) ──────────────────────────────────────

def _piece(sid: int, views: int = 100) -> dict:
    return {"submission_id": sid, "title": f"Sample Piece {sid}", "username": "SecondFur",
            "views": views, "favorites_count": 3, "comments_count": 1}


def _two_accounts(conn):
    from database import accounts as adb
    a = adb.get_default_account_id(conn, "fa", create=True)
    b = adb.create_account(conn, "fa", "second", handle="ThirdFur")
    return a, b


def test_carry_forward_writes_the_last_numbers_for_pieces_without_a_row():
    conn = get_connection()
    try:
        a, _ = _two_accounts(conn)
        for sid in (1, 2, 3):
            fa_queries.upsert_fa_submission(conn, _piece(sid, 100 + sid), a)
        fa_queries.insert_fa_snapshot(conn, a, 1, 999, 9, 9, polled_at="2026-11-18 12:00:00")   # really read
        before = conn.execute("SELECT submission_id, updated_at FROM fa_submissions ORDER BY 1").fetchall()
        n = fa_queries.carry_forward_fa_snapshots(conn, a, [1, 2, 3], "2026-11-18 12:00:00")
        rows = conn.execute("SELECT submission_id, account_id, views, favorites_count, comments_count "
                            "FROM fa_snapshots WHERE polled_at = '2026-11-18 12:00:00' ORDER BY 1").fetchall()
        after = conn.execute("SELECT submission_id, updated_at FROM fa_submissions ORDER BY 1").fetchall()
    finally:
        conn.close()
    assert n == 2
    assert [tuple(r) for r in rows] == [(1, a, 999, 9, 9), (2, a, 102, 3, 1), (3, a, 103, 3, 1)]
    assert [tuple(r) for r in before] == [tuple(r) for r in after]           # FR-006: last-read time untouched


def test_carry_forward_stays_inside_the_account():
    """FR-SEC-001: one account's poll writes nothing for another account's pieces, even if handed their ids."""
    conn = get_connection()
    try:
        a, b = _two_accounts(conn)
        fa_queries.upsert_fa_submission(conn, _piece(1), a)
        fa_queries.upsert_fa_submission(conn, _piece(2), b)
        n = fa_queries.carry_forward_fa_snapshots(conn, a, [1, 2, 3], "2026-11-18 12:00:00")
        rows = conn.execute("SELECT submission_id, account_id FROM fa_snapshots").fetchall()
    finally:
        conn.close()
    assert n == 1 and [tuple(r) for r in rows] == [(1, a)]


def test_carry_forward_is_one_transaction():
    """FR-SEC-003: more ids than one statement takes, still all-or-nothing: the function never commits."""
    conn = get_connection()
    try:
        a, _ = _two_accounts(conn)
        ids = list(range(1, 1301))
        for sid in ids:
            fa_queries.upsert_fa_submission(conn, _piece(sid), a)
        conn.commit()
        assert fa_queries.carry_forward_fa_snapshots(conn, a, ids, "2026-11-18 12:00:00") == 1300
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM fa_snapshots").fetchone()[0] == 0
    finally:
        conn.close()


# ── A whole poll (page loads, totals, soft bot check) ────────

def _page(views: int) -> str:
    return ('<html><head><title>Sample Piece by SecondFur</title></head><body>'
            '<div class="submission-title"><h2>Sample Piece</h2></div><div class="submission-page-stats">'
            f'<div title="Views"><div>{views}</div></div><div title="Comments"><div>1</div></div>'
            '<div title="Favorites"><div><a href="/favslist/1/">3</a></div></div></div></body></html>')


@pytest.fixture
def fa_site(tmp_path, monkeypatch):
    """A stand-in FurAffinity: one gallery page of 342 pieces, a page per piece. Records the piece pages read."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "FA_REQUEST_DELAY_SECONDS", 0)
    monkeypatch.setattr("polling.last_check._skips", {})
    monkeypatch.setattr(fa_poller, "_fa_first_poll_done", set())

    async def _send(text):
        return True
    monkeypatch.setattr("polling.telegram.send_telegram", _send)
    config.save_settings({"fa_username": "SecondFur", "fa_cookie_a": "a", "fa_cookie_b": "b",
                          "fa_direct_polling": True, "telegram_error_alerts": False})
    site = {"read": [], "views": {i: 100 for i in GALLERY}, "zero": set()}

    def _h(req):
        path = req.url.path
        html = {"content-type": "text/html; charset=UTF-8"}
        if path.startswith("/controls/submissions"):
            return httpx.Response(200, text='<a href="/logout/?key=1">Log Out</a>', headers=html)
        if path.startswith("/gallery/"):
            first = path.rstrip("/").endswith("/1")
            body = "".join(f'<figure id="sid-{i}"></figure>' for i in GALLERY) if first else ""
            return httpx.Response(200, text=body, headers=html)
        m = re.match(r"/view/(\d+)/", path)
        sid = int(m.group(1))
        site["read"].append(sid)
        return httpx.Response(200, text=_page(0 if sid in site["zero"] else site["views"][sid]), headers=html)

    async def _get(self):
        if self._fa_http is None:
            self._fa_http = httpx.AsyncClient(transport=httpx.MockTransport(_h), base_url=config.FA_BASE,
                                              event_hooks={"response": [_check_challenge]})
        return self._fa_http
    monkeypatch.setattr(FAClient, "_get_fa_http", _get)
    return site


def _poll(site, **kw) -> dict:
    site["read"].clear()
    return asyncio.run(fa_poller.run_fa_poll_cycle(**kw))


def _hours_pass(hours: int) -> None:
    """Move everything stored back in time: the same as *hours* passing before the next poll."""
    conn = get_connection()
    try:
        conn.execute("UPDATE fa_submissions SET updated_at = datetime(updated_at, ?)", (f"-{hours} hours",))
        conn.execute("UPDATE fa_snapshots SET polled_at = datetime(polled_at, ?)", (f"-{hours} hours",))
        conn.commit()
    finally:
        conn.close()


def _totals() -> list[tuple]:
    """Per poll, oldest first: (pieces with a row, summed views)."""
    conn = get_connection()
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT COUNT(*), SUM(views) FROM fa_snapshots GROUP BY polled_at ORDER BY polled_at")]
    finally:
        conn.close()


def test_scheduled_poll_page_loads(fa_site, caplog):
    """SC-001 / FR-SEC-006: 342 on the first poll (nothing stored), then 48, then 97 once older pieces are due."""
    _poll(fa_site)
    assert len(fa_site["read"]) == 342           # a new account: every piece is new

    _hours_pass(4)
    with caplog.at_level(logging.INFO, logger="polling.fa_poller"):
        stats = _poll(fa_site)                   # "refresh now" and the scheduler both call it like this
    assert sorted(fa_site["read"]) == sorted(FRESH)
    assert stats["snapshots_inserted"] == 342 and stats["submissions_found"] == 342    # rows written
    assert "read 48 of 342 pieces (48 asked for), carried 294 forward" in caplog.text

    _hours_pass(17)                              # the older pieces were last read 21 hours ago
    _poll(fa_site)
    assert len(fa_site["read"]) == 97 and set(FRESH) <= set(fa_site["read"])

    _poll(fa_site, force_full=True)
    assert len(fa_site["read"]) == 342


def test_totals_never_dip_between_polls(fa_site):
    """SC-003: every poll has a row for every piece, and the summed views only go up."""
    _poll(fa_site)
    for _ in range(3):
        _hours_pass(4)
        for i in GALLERY:
            fa_site["views"][i] += 1
        _poll(fa_site)
    totals = _totals()
    assert [n for n, _ in totals] == [342] * 4
    views = [v for _, v in totals]
    assert views == sorted(views) and views[-1] > views[0]


def test_zero_page_is_carried_and_stays_due(fa_site, caplog):
    """FR-SEC-002: a bot-check page served as a normal page (all zeros) is not a read."""
    _poll(fa_site)
    _hours_pass(21)
    fa_site["zero"] = set(OLDER)
    with caplog.at_level(logging.INFO, logger="polling.fa_poller"):
        stats = _poll(fa_site)
    assert stats["snapshots_inserted"] == 342    # rows written; the 49 older pieces asked for came back empty
    line = next(r for r in caplog.records if "FA direct: read" in r.getMessage())
    assert "read 48 of 342 pieces (97 asked for), carried 294 forward" in line.getMessage()
    assert line.levelno == logging.WARNING       # fewer read than asked for is said out loud
    conn = get_connection()
    try:
        newest = conn.execute("SELECT MAX(polled_at) FROM fa_snapshots").fetchone()[0]
        rows = conn.execute("SELECT COUNT(*), MIN(views) FROM fa_snapshots WHERE polled_at = ?",
                            (newest,)).fetchone()
        still_due = conn.execute("SELECT COUNT(*) FROM fa_submissions WHERE submission_id <= ? "
                                 "AND updated_at <= datetime('now', '-20 hours')", (OLDER[0],)).fetchone()[0]
    finally:
        conn.close()
    assert tuple(rows) == (342, 100)             # carried with their last real numbers, never a zero
    assert still_due == 294                      # last-read time unchanged: first in line next poll


def test_failed_carry_forward_leaves_no_rows(fa_site, monkeypatch, caplog):
    """FR-SEC-003: a carry-forward that fails part-way is rolled back before anything else can commit it."""
    _poll(fa_site)
    _hours_pass(4)
    real = fa_queries.carry_forward_fa_snapshots

    def _half(conn, account_id, ids, polled_at):
        real(conn, account_id, ids[:100], polled_at)
        raise RuntimeError("disk trouble")
    monkeypatch.setattr(fa_queries, "carry_forward_fa_snapshots", _half)
    with caplog.at_level(logging.WARNING, logger="polling.fa_poller"):
        stats = _poll(fa_site)                   # the poll itself still finishes
    assert stats["snapshots_inserted"] == 48
    assert _totals()[-1][0] == 48                # only the pieces really read; none of the half-written set
    assert "carry-forward failed" in caplog.text


def test_bot_check_part_way_carries_nothing(fa_site):
    """FR-007: a poll a bot check stopped writes no carried rows."""
    from clients.fa.client import FAChallenged
    _poll(fa_site)
    _hours_pass(4)

    async def _challenged(self, ids):
        raise FAChallenged()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(FAClient, "get_submission_details_batch_direct", _challenged)
        with pytest.raises(FAChallenged):
            _poll(fa_site)
    assert len(_totals()) == 1                   # only the first poll has rows


def test_pieces_under_another_account_are_read_not_carried(fa_site, caplog):
    """An account removed and added again: its old rows belong to the old id. Read as before, never carried."""
    _poll(fa_site)
    conn = get_connection()
    try:
        _, other = _two_accounts(conn)
        conn.execute("UPDATE fa_submissions SET account_id = ? WHERE submission_id <= ?", (other, OLDER[0]))
        conn.commit()
    finally:
        conn.close()
    _hours_pass(4)
    with caplog.at_level(logging.WARNING, logger="polling.fa_poller"):
        _poll(fa_site)
    assert len(fa_site["read"]) == 342           # the 294 look new to this account
    assert "294 gallery pieces are stored under another account" in caplog.text
    conn = get_connection()
    try:
        accounts = {r[0] for r in conn.execute(
            "SELECT DISTINCT account_id FROM fa_snapshots WHERE polled_at = (SELECT MAX(polled_at) FROM fa_snapshots)")}
    finally:
        conn.close()
    assert other not in accounts


def _no_data_service(monkeypatch):
    """The third-party data service answers 500 to everything, as it has since 16 Oct 2026. Offline."""
    real_init = FAClient.__init__

    def _init(self, *a, **kw):
        real_init(self, *a, **kw)
        self._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="{}")))
    monkeypatch.setattr(FAClient, "__init__", _init)


def test_fallback_after_the_data_service_fails_is_paced(fa_site, monkeypatch, caplog):
    """The path a real install takes: the data service fails, the poll falls back to direct, and is paced."""
    config.save_settings({"fa_direct_polling": False})
    _no_data_service(monkeypatch)
    _poll(fa_site)
    _hours_pass(4)
    with caplog.at_level(logging.INFO, logger="polling.fa_poller"):
        _poll(fa_site)
    assert "falling back to direct FA scraping" in caplog.text
    assert sorted(fa_site["read"]) == sorted(FRESH)
    assert _totals()[-1][0] == 342


def test_through_the_data_service_nothing_changes(fa_site, monkeypatch, caplog):
    """FR-004: when the data service answers, every piece is fetched through it and nothing is carried."""
    config.save_settings({"fa_direct_polling": False})
    _no_data_service(monkeypatch)                # everything but the two calls below (watchers, profile) fails softly
    asked = []

    async def _gallery(self):
        return [{"submission_id": i} for i in GALLERY]

    async def _details(self, ids):
        asked.append(len(ids))
        return [_piece(i) | {"comments_count": 0} for i in ids]
    monkeypatch.setattr(FAClient, "get_all_gallery_ids", _gallery)
    monkeypatch.setattr(FAClient, "get_submission_details_batch", _details)
    _poll(fa_site)
    _hours_pass(4)
    with caplog.at_level(logging.INFO, logger="polling.fa_poller"):
        stats = _poll(fa_site)
    assert asked == [342, 342] and fa_site["read"] == []
    assert stats["snapshots_inserted"] == 342 and "FA direct:" not in caplog.text


# ── Gallery ids and the declined-retry log line ──────────────

def test_overlong_gallery_id_is_dropped():
    """FR-SEC-004."""
    html = '<figure id="sid-12345"></figure><figure id="sid-' + "9" * 40 + '"></figure>'
    assert FAClient._GALLERY_SID_RE.findall(html) == ["12345"]


def test_every_permanent_marker_still_declines():
    """FR-SEC-005: the wording change leaves the decision alone, for every marker."""
    from posting.manager import _PERMANENT_ERROR_MARKERS, _schedule_retry
    for marker in _PERMANENT_ERROR_MARKERS:
        assert _schedule_retry("Sample Story", 0, "fa", "post", f"x {marker} y") is False


def test_bot_check_log_wording(caplog):
    from clients.fa.client import FAChallenged
    from posting.manager import _schedule_retry
    with caplog.at_level(logging.WARNING, logger="posting.manager"):
        _schedule_retry("Sample Story", 0, "fa", "post", str(FAChallenged()))
    line = caplog.text
    assert "bot check" in line and "NOT retrying" in line
    assert "credential" not in line and "Settings" not in line

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="posting.manager"):
        _schedule_retry("Sample Story", 0, "fa", "post", "FA: not logged in")
    assert "credential" in caplog.text and "Settings" in caplog.text
