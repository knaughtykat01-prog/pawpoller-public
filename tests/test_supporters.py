"""Spec 084 — supporter totals: how each figure is counted, what's stored (totals only), and the poll's
all-or-nothing rule for a member read. Stand-ins only; no network."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

import config
from database import accounts as adb
from database import supporter_queries as sq
from database.db import get_connection

CAMPAIGN = {"patron_count": 9, "pledge_sum": 4200, "pledge_sum_currency": "USD", "currency": "USD", "is_monthly": True,
            "tiers": [{"tier_id": "30", "title": "Free", "amount_cents": 0},
                      {"tier_id": "40", "title": "Sketch club", "amount_cents": 500},
                      {"tier_id": "50", "title": "Inner circle", "amount_cents": 1000}]}


def m(status="active_patron", tier="40", cents=500, trial=False, free=False, gifted=False, start="", end=""):
    return {"patron_status": status, "tier_id": tier, "campaign_pledge_amount_cents": cents, "is_free_trial": trial,
            "is_free_member": free, "is_gifted": gifted, "pledge_relationship_start": start, "pledge_relationship_end": end}


def account(platform="pat") -> int:
    conn = get_connection()
    try:
        return adb.get_default_account_id(conn, platform, create=True)
    finally:
        conn.close()


def test_figures_count_each_kind():
    members = [m(), m(cents=700), m(tier="50", cents=1000, gifted=True),            # three paying (gifted counts)
               m(trial=True),                                                       # a trial, apart
               m(status=None, tier="30", cents=0, free=True),                       # a free member
               m(status="former_patron", tier="30", cents=0, free=True, end="2026-11-12T10:00:00Z"),
               m(start="2026-11-12T09:00:00Z")]                                     # new since the last poll
    f = sq.figures(members, CAMPAIGN, "2026-11-11 00:00:00")
    assert (f["paid_count"], f["trial_count"], f["free_count"]) == (4, 1, 2)
    assert f["total_count"] == 9 and f["monthly_cents"] == 4200 and f["currency"] == "USD"
    assert (f["new_count"], f["lost_count"]) == (1, 1)
    tiers = {t["tier_id"]: t for t in f["tiers"]}
    assert tiers["40"]["patron_count"] == 4 and tiers["40"]["monthly_cents"] == 500 + 700 + 500 + 500
    assert tiers["50"]["patron_count"] == 1 and tiers["30"]["patron_count"] == 2


def test_first_poll_records_no_new_or_lost():
    f = sq.figures([m(start="2026-11-12T09:00:00Z"), m(status="former_patron", end="2026-11-12T10:00:00Z")], CAMPAIGN, None)
    assert (f["new_count"], f["lost_count"]) == (0, 0)


def test_record_snapshot_validates():
    aid = account()
    conn = get_connection()
    try:
        base = dict(platform="pat", account_id=aid, paid_count=1, trial_count=0, free_count=0, total_count=1,
                    monthly_cents=500, currency="USD", is_monthly=True, new_count=0, lost_count=0)
        for bad in ({"paid_count": -1}, {"monthly_cents": "lots"}, {"currency": "usd"}, {"currency": "US"},
                    {"platform": "fa"}, {"tiers": [{"tier_id": "40", "patron_count": -2}]}):
            with pytest.raises(ValueError):
                sq.record_snapshot(conn, **{**base, **bad})
        assert conn.execute("SELECT COUNT(*) FROM supporter_snapshots").fetchone()[0] == 0
        sid = sq.record_snapshot(conn, **base, tiers=[{"tier_id": "40", "title": "x" * 500, "amount_cents": 500,
                                                       "patron_count": 1, "monthly_cents": 500}])
        conn.commit()
        title = conn.execute("SELECT title FROM supporter_tier_snapshots WHERE snapshot_id = ?", (sid,)).fetchone()[0]
        assert len(title) == 200
    finally:
        conn.close()


def test_stored_columns_are_totals_only():
    conn = get_connection()
    try:
        cols = {r[1] for t in ("supporter_snapshots", "supporter_tier_snapshots")
                for r in conn.execute(f"PRAGMA table_info({t})")}
    finally:
        conn.close()
    for personal in ("name", "full_name", "email", "member_id", "user_id", "patron_id"):
        assert personal not in cols


def _snap(conn, aid, polled_at, paid, cents, currency="USD", new=0, lost=0):
    return sq.record_snapshot(conn, platform="pat", account_id=aid, paid_count=paid, trial_count=0, free_count=0,
                              total_count=paid, monthly_cents=cents, currency=currency, is_monthly=True,
                              new_count=new, lost_count=lost, polled_at=polled_at)


def test_summary_change_history_period_and_removed_accounts():
    aid = account()
    conn = get_connection()
    try:
        _snap(conn, aid, "2026-11-10 00:00:00", 3, 1500, new=1)
        _snap(conn, aid, "2026-11-12 00:00:00", 5, 2500, new=2, lost=1)
        _snap(conn, 99999, "2026-11-12 00:00:00", 50, 99999)          # an account that no longer exists
        conn.commit()
        rows = sq.summary(conn)
        assert [r["account_id"] for r in rows] == [aid]
        assert rows[0]["paid_count"] == 5 and rows[0]["change"] == {"paid_count": 2, "free_count": 0, "monthly_cents": 1000}
        assert sq.period(conn, aid, "2026-11-01", "2026-11-12") == {"new": 3, "lost": 1, "polls": 2}
        with pytest.raises(ValueError):
            sq.history(conn, aid, 731)
        with pytest.raises(ValueError):
            sq.period(conn, aid, "2026-11-12", "2026-11-01")
        _snap(conn, aid, "2026-11-13 00:00:00", 5, 2500, currency="EUR")
        conn.commit()
        assert sq.summary(conn)[0]["change"] is None                     # never compared across currencies
    finally:
        conn.close()


def test_account_removal_removes_supporter_rows():
    from fastapi.testclient import TestClient
    import dashboard
    conn = get_connection()
    try:
        account()                                        # the default Patreon account
        extra = adb.create_account(conn, "pat", "Second page", handle="penwright")
        _snap(conn, extra, "2026-11-12 00:00:00", 2, 1000)
        conn.commit()
    finally:
        conn.close()
    config.save_settings({f"acct_{extra}_pat_tiers": "[]", f"acct_{extra}_pat_campaign_id": "222"})
    r = TestClient(dashboard.app, client=("127.0.0.1", 50000)).delete(f"/api/accounts/{extra}")
    assert r.status_code == 200, r.text
    conn = get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM supporter_snapshots WHERE account_id = ?", (extra,)).fetchone()[0] == 0
    finally:
        conn.close()
    assert not [k for k in config.get_settings() if k.startswith(f"acct_{extra}_pat_")]


# ── the poll: all or nothing for the member read ─────────────────────────────

def _poll(monkeypatch, members_pages):
    from tests.test_patreon import FakePatreon, make_account
    from clients.pat import client as patc
    from polling import pat_poller
    monkeypatch.setattr(config, "DATA_DIR", config.SETTINGS_PATH.parent)
    monkeypatch.setattr(patc, "MIN_GAP_S", 0)
    aid = make_account()
    fake = FakePatreon()
    fake.members_pages = members_pages
    real = fake.handler

    def h(request):
        if request.url.path == "/api/posts" and request.method == "GET":
            import httpx
            return httpx.Response(200, json={"data": [{"type": "post", "id": "777", "attributes": {
                "title": "Sample Story", "like_count": 4, "comment_count": 0, "published_at": "2026-11-12T00:00:00Z"}}],
                "meta": {"pagination": {"cursors": {"next": None}}}})
        return real(request)
    import httpx
    monkeypatch.setattr(pat_poller, "_transport", httpx.MockTransport(h))
    stats = asyncio.run(pat_poller.run_pat_poll_cycle(aid))
    return aid, stats


def _member(status="active_patron", cents=500, name="SecondFur"):
    return {"type": "member", "id": "m-x", "attributes": {"patron_status": status, "campaign_pledge_amount_cents": cents,
                                                         "full_name": name, "email": "owner@example.com"},
            "relationships": {"reward": {"data": {"type": "reward", "id": "40"}}}}


def test_poll_partial_member_read_writes_no_supporter_numbers(monkeypatch):
    aid, stats = _poll(monkeypatch, {0: ([_member()], 500)})       # page 2 fails
    conn = get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM supporter_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT likes FROM (SELECT favorites_count AS likes FROM pat_submissions)").fetchone()[0] == 4
        log = conn.execute("SELECT status, error_message FROM pat_poll_log ORDER BY id DESC").fetchone()
        assert log["status"] == "partial" and "part-way" in log["error_message"]
    finally:
        conn.close()


def test_poll_full_read_stores_totals_not_people(monkeypatch):
    aid, stats = _poll(monkeypatch, {0: ([_member(), _member(cents=700)], None)})
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM supporter_snapshots").fetchone()
        assert row["paid_count"] == 2 and row["monthly_cents"] == 2500 and row["total_count"] == 7
        dump = "\n".join(str(tuple(r)) for t in ("supporter_snapshots", "supporter_tier_snapshots", "pat_submissions")
                         for r in conn.execute(f"SELECT * FROM {t}"))
        assert "SecondFur" not in dump and "owner@example.com" not in dump and "m-x" not in dump
    finally:
        conn.close()


# ── gaps named in tasks.md (T060, T061, T064, T066, T067, T068) ──────────────

def _member_read(monkeypatch, pages):
    from tests.test_patreon import FakePatreon, _with
    from clients.pat import client as patc
    monkeypatch.setattr(patc, "MIN_GAP_S", 0)
    fake = FakePatreon()
    fake.members_pages = pages
    return fake, (lambda: asyncio.run(_with(fake, lambda c: c.members("111"))))


def test_pat_member_read_asks_only_needed_fields(monkeypatch):
    fake, read = _member_read(monkeypatch, {0: ([_member()], 500), 500: ([_member()], None)})
    read()
    calls = fake.calls("GET", "/api/members")
    assert len(calls) == 2
    for c in calls:
        q = c["query"]
        assert q["fields[member]"] == ["patron_status,is_free_member,is_free_trial,is_gifted,"
                                       "campaign_pledge_amount_cents,pledge_relationship_start,pledge_relationship_end"]
        assert q["include"] == ["reward"] and q["fields[reward]"] == ["amount_cents"]
        assert q["json-api-use-default-includes"] == ["false"]
        flat = " ".join(" ".join(v) for v in q.values())
        for never in ("email", "full_name", "note", "address", "user_id", "discord_vanity"):
            assert never not in flat


def test_pat_member_parse_drops_email_and_name(monkeypatch):
    _, read = _member_read(monkeypatch, {0: ([_member()], None)})
    out = read()
    assert "SecondFur" not in repr(out) and "owner@example.com" not in repr(out) and "m-x" not in repr(out)


def test_pat_member_page_cap(monkeypatch):
    from clients.pat import client as patc
    monkeypatch.setattr(patc, "MAX_MEMBER_PAGES", 2)
    _, read = _member_read(monkeypatch, {0: ([_member()], 500), 500: ([_member()], 1000), 1000: ([_member()], None)})
    with pytest.raises(patc.PatPartial):
        read()
    _, read = _member_read(monkeypatch, {0: ([_member()], 0)})          # a cursor that doesn't move
    with pytest.raises(patc.PatPartial):
        read()


def test_pat_poll_refused_without_campaign(monkeypatch):
    """The poll half of test_pat_post_and_poll_refused_without_campaign: no saved page, no request, a plain reason."""
    from tests.test_patreon import FakePatreon, make_account
    from polling import pat_poller
    aid = make_account(campaign="")
    fake = FakePatreon()
    monkeypatch.setattr(pat_poller, "_transport", fake.transport())
    out = asyncio.run(pat_poller.run_pat_poll_cycle(aid))
    assert "doesn't know which page" in out["skipped"] and not fake.requests


def test_supporter_values_never_reach_logs_or_reports(monkeypatch, caplog):
    import logging
    import techcentre
    before = dict(techcentre.CHECKIN_FIELDS)
    caplog.set_level(logging.DEBUG)
    _poll(monkeypatch, {0: ([_member(cents=4321)], None)})                # a full read
    _poll(monkeypatch, {0: ([_member(cents=4321)], 500)})                 # and one that fails part-way
    conn = get_connection()
    try:
        logs = "\n".join(str(tuple(r)) for r in conn.execute("SELECT * FROM pat_poll_log"))
    finally:
        conn.close()
    for secret in ("SecondFur", "owner@example.com", "m-x", "4321"):
        assert secret not in caplog.text and secret not in logs
    assert techcentre.CHECKIN_FIELDS == before


def test_supporter_routes_refuse_bad_ranges_and_hide_removed_accounts():
    from fastapi.testclient import TestClient
    import dashboard
    c = TestClient(dashboard.app, client=("127.0.0.1", 50000))
    aid = account()
    assert c.get("/api/supporters/history", params={"account_id": aid, "days": 731}).status_code == 400
    assert c.get("/api/supporters/period", params={"account_id": aid, "from": "2026-11-12", "to": "2026-11-01"}).status_code == 400
    conn = get_connection()
    try:
        _snap(conn, 99999, "2026-11-12 00:00:00", 50, 99999)              # an account that no longer exists
        conn.commit()
    finally:
        conn.close()
    r = c.get("/api/supporters/history", params={"account_id": 99999, "days": 30})
    assert r.status_code == 200 and r.json()["snapshots"] == []
    assert all(a["account_id"] != 99999 for a in c.get("/api/supporters/summary").json()["accounts"])


def test_datamap_supporter_group_confidential():
    import datamap
    group = datamap.GROUPS["Supporters and money"]
    assert group[1] == "confidential"
    tables = {e.name for e in datamap.ENTRIES if e.kind == "table" and e.group == "Supporters and money"}
    # kofi_payments joins with Ko-fi (Phase D); until then only Patreon's two tables exist.
    assert tables == {"supporter_snapshots", "supporter_tier_snapshots"}


def test_malformed_member_record_raises_a_fixed_sentence(monkeypatch):
    """A record Patreon sends malformed stops the read with PawPoller's own words, never the record's values."""
    from clients.pat import client as patc
    bad = _member()
    bad["attributes"]["campaign_pledge_amount_cents"] = "4321-not-a-number"
    _, read = _member_read(monkeypatch, {0: ([bad], None)})
    with pytest.raises(patc.PatPartial) as e:
        read()
    assert "4321" not in str(e.value) and "SecondFur" not in str(e.value)


def test_summary_tier_change_since_the_poll_before():
    """FR-031: each tier carries its change since the poll before; a tier new this poll has none."""
    aid = account()
    conn = get_connection()
    try:
        sq.record_snapshot(conn, platform="pat", account_id=aid, paid_count=3, trial_count=0, free_count=0, total_count=3,
                           monthly_cents=1500, currency="USD", is_monthly=True, new_count=0, lost_count=0,
                           polled_at="2026-11-10 00:00:00",
                           tiers=[{"tier_id": "40", "title": "Sketch club", "amount_cents": 500, "patron_count": 3, "monthly_cents": 1500}])
        sq.record_snapshot(conn, platform="pat", account_id=aid, paid_count=5, trial_count=0, free_count=0, total_count=5,
                           monthly_cents=2500, currency="USD", is_monthly=True, new_count=2, lost_count=0,
                           polled_at="2026-11-11 00:00:00",
                           tiers=[{"tier_id": "40", "title": "Sketch club", "amount_cents": 500, "patron_count": 4, "monthly_cents": 2000},
                                  {"tier_id": "50", "title": "Inner circle", "amount_cents": 1000, "patron_count": 1, "monthly_cents": 1000}])
        conn.commit()
        row = next(r for r in sq.summary(conn) if r["account_id"] == aid)
    finally:
        conn.close()
    assert {t["tier_id"]: t["change"] for t in row["tiers"]} == {"40": 1, "50": None}

