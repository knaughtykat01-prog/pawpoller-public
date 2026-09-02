"""Tests for per-persona Telegram digest segmentation (Phase 3).

Seeds accounts on three platforms under two personas + one Unassigned, then
drives ``send_digest_report`` with a captured ``send_telegram`` and asserts ONE
message per persona (plus Unassigned), each carrying only its own accounts.
"""

import asyncio
import sqlite3

import pytest

import config
from database import accounts, personas
from database import queries, fa_queries, ws_queries
import polling.telegram as tg


# Child tables first (FK-safe order); cleared with foreign_keys OFF so leftover
# rows can't block a parent delete and contaminate the NEXT test file.
_CLEAR_TABLES = (
    "snapshots", "fa_snapshots", "ws_snapshots",
    "submissions", "fa_submissions", "ws_submissions",
    "accounts", "personas",
)


def _clear_all(c):
    c.execute("PRAGMA foreign_keys=OFF")
    for t in _CLEAR_TABLES:
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.execute("PRAGMA foreign_keys=ON")
    c.commit()


@pytest.fixture
def conn():
    """Fresh DB with personas/accounts/platform tables empty + telegram enabled.

    Clears on BOTH setup and teardown so this file leaves the shared dev DB as
    pristine as it found it (otherwise leftover ws_/fa_ rows with account_id FKs
    break a later file's ``DELETE FROM accounts``).
    """
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    _clear_all(c)
    # Telegram must look "enabled + digest on" for send_digest_report to run.
    config.save_settings({
        "telegram_enabled": True,
        "telegram_digest": True,
        "telegram_bot_token": "x", "telegram_chat_id": "y",
    })
    yield c
    _clear_all(c)
    c.close()


def _seed(conn):
    """Two personas (Alpha→IB, Beta→FA) + one Unassigned WS account, each with
    a submission so every digest unit has data."""
    alpha = personas.create_persona(conn, "Alpha")
    beta = personas.create_persona(conn, "Beta")
    ib = accounts.create_account(conn, "ib", "IB-Main")
    fa = accounts.create_account(conn, "fa", "FA-Main")
    ws = accounts.create_account(conn, "ws", "WS-Loose")
    personas.assign_account_persona(conn, ib, alpha)
    personas.assign_account_persona(conn, fa, beta)
    # ws stays Unassigned
    queries.upsert_submission(conn, {"submission_id": 1, "title": "Alpha IB Story", "views": 500, "favorites_count": 20, "comments_count": 3}, ib)
    fa_queries.upsert_fa_submission(conn, {"submission_id": 9001, "title": "Beta FA Story", "views": 800, "favorites_count": 40, "comments_count": 5}, fa)
    ws_queries.upsert_ws_submission(conn, {"submission_id": 7001, "title": "Loose WS Story", "views": 120, "favorites_count": 6, "comments_count": 1}, ws)
    conn.commit()
    return alpha, beta, ib, fa, ws


def _run_digest(monkeypatch):
    """Capture every send_telegram call from a digest run; return the messages."""
    captured = []

    async def _capture(text):
        captured.append(text)
        return True

    monkeypatch.setattr(tg, "send_telegram", _capture)
    # Neutralise the FA-watcher-digest piggyback so it can't add noise.
    import polling.fa_poller as fap
    async def _noop():
        return None
    monkeypatch.setattr(fap, "send_fa_watcher_digest", _noop)

    asyncio.run(tg.send_digest_report())
    return captured


def test_digest_sends_one_message_per_persona(conn, monkeypatch):
    _seed(conn)
    msgs = _run_digest(monkeypatch)

    # Exactly three units: Alpha, Beta, Unassigned.
    assert len(msgs) == 3, msgs
    alpha_msg = next(m for m in msgs if "Alpha" in m)
    beta_msg = next(m for m in msgs if "Beta" in m)
    unassigned_msg = next(m for m in msgs if "Unassigned" in m)

    # Each message carries ONLY its own account(s) — strict segmentation.
    assert "IB-Main" in alpha_msg and "FA-Main" not in alpha_msg and "WS-Loose" not in alpha_msg
    assert "FA-Main" in beta_msg and "IB-Main" not in beta_msg
    assert "WS-Loose" in unassigned_msg and "IB-Main" not in unassigned_msg

    # Each has a persona-combined totals block.
    assert all("Combined" in m for m in msgs)
    # Alpha's combined views == its single IB account's 500 (not lumped with others).
    assert "500" in alpha_msg


def test_no_personas_falls_back_to_single_digest(conn, monkeypatch):
    """With no personas defined, one combined digest goes out (back-compat)."""
    ib = accounts.create_account(conn, "ib", "IB-Main")
    queries.upsert_submission(conn, {"submission_id": 1, "title": "S", "views": 10, "favorites_count": 1, "comments_count": 0}, ib)
    conn.commit()

    msgs = _run_digest(monkeypatch)
    assert len(msgs) == 1
    assert "PawPoller" in msgs[0] and "Unassigned" not in msgs[0]


def test_digest_skips_when_no_data(conn, monkeypatch):
    """Personas with no submissions produce no messages (skip empty)."""
    alpha = personas.create_persona(conn, "Alpha")
    ib = accounts.create_account(conn, "ib", "IB-Main")
    personas.assign_account_persona(conn, ib, alpha)
    # No submissions seeded.
    conn.commit()

    msgs = _run_digest(monkeypatch)
    assert msgs == []


def _run_weekly(monkeypatch):
    """Capture every send_telegram call from a WEEKLY digest run."""
    captured = []

    async def _capture(text):
        captured.append(text)
        return True

    monkeypatch.setattr(tg, "send_telegram", _capture)
    asyncio.run(tg.send_weekly_digest_report())
    return captured


def test_periodic_digest_skips_unchanged_but_weekly_keeps_them(conn, monkeypatch):
    """Concision rule: a persona whose accounts had no new views/faves/comments
    this window is omitted from the PERIODIC digest, but still shown in the
    WEEKLY digest (the always-full exception)."""
    alpha = personas.create_persona(conn, "Alpha")
    beta = personas.create_persona(conn, "Beta")
    ib = accounts.create_account(conn, "ib", "IB-Quiet")
    fa = accounts.create_account(conn, "fa", "FA-Active")
    personas.assign_account_persona(conn, ib, alpha)
    personas.assign_account_persona(conn, fa, beta)

    # Alpha/IB: stats UNCHANGED vs a snapshot from well before the window → 0 delta.
    queries.upsert_submission(conn, {"submission_id": 1, "title": "Quiet", "views": 500, "favorites_count": 20, "comments_count": 3}, ib)
    queries.insert_snapshot(conn, ib, 1, 500, 20, 3, polled_at="2020-01-01 00:00:00")
    # Beta/FA: fresh submission, no prior snapshot → full value is the delta (non-zero).
    fa_queries.upsert_fa_submission(conn, {"submission_id": 9001, "title": "Active", "views": 800, "favorites_count": 40, "comments_count": 5}, fa)
    conn.commit()

    # Periodic digest: Alpha (unchanged) is skipped; only Beta goes out.
    msgs = _run_digest(monkeypatch)
    assert len(msgs) == 1, msgs
    assert "Beta" in msgs[0] and "FA-Active" in msgs[0]
    assert not any(("Alpha" in m) or ("IB-Quiet" in m) for m in msgs)

    # Weekly digest: the always-full exception — Alpha reappears despite 0 delta.
    weekly = _run_weekly(monkeypatch)
    assert any(("Alpha" in m) and ("IB-Quiet" in m) for m in weekly)
    assert any("Beta" in m for m in weekly)


def test_consolidated_summary_groups_by_persona(conn, monkeypatch):
    """One poll cycle → one message with a 👤 sub-section per persona."""
    alpha, beta, ib, fa, ws = _seed(conn)
    captured = []

    async def _cap(text):
        captured.append(text)
        return True

    monkeypatch.setattr(tg, "send_telegram", _cap)
    results = [
        {"platform": "ib", "account_id": ib, "label": "IB-Main", "stats": {"submissions_found": 5, "new_faves_found": 2}},
        {"platform": "fa", "account_id": fa, "label": "FA-Main", "stats": {"submissions_found": 3, "new_comments_found": 1}},
        {"platform": "ws", "account_id": ws, "label": "WS-Loose", "stats": {"submissions_found": 1}},
    ]
    asyncio.run(tg.send_consolidated_poll_summary(results, 12.0))

    assert len(captured) == 1
    msg = captured[0]
    assert msg.count("👤") == 3           # one sub-section per persona/unassigned
    assert "Alpha" in msg and "Beta" in msg and "Unassigned" in msg


def test_milestones_batch_scoped_by_account(conn, monkeypatch):
    """check_milestones_batch(account_id=a1) scans ONLY a1's submissions — the
    fix that prevents multi-account double-firing."""
    a1 = accounts.create_account(conn, "ib", "A1")
    a2 = accounts.create_account(conn, "ib", "A2")
    queries.upsert_submission(conn, {"submission_id": 11, "title": "A1 sub", "views": 300, "favorites_count": 20, "comments_count": 3}, a1)
    queries.upsert_submission(conn, {"submission_id": 22, "title": "A2 sub", "views": 300, "favorites_count": 20, "comments_count": 3}, a2)
    for sid, acc in [(11, a1), (22, a2)]:
        queries.insert_snapshot(conn, acc, sid, 90, 5, 0, polled_at="2026-06-01 00:00:00")
        queries.insert_snapshot(conn, acc, sid, 300, 20, 3, polled_at="2026-06-02 00:00:00")
    conn.commit()
    config.save_settings({"telegram_milestones": True})

    seen = []

    async def _cap_ms(platform, sid, title, *a, **k):
        seen.append(sid)

    monkeypatch.setattr(tg, "check_milestones", _cap_ms)
    asyncio.run(tg.check_milestones_batch("ib", "snapshots", "submissions", account_id=a1))
    assert seen == [11]   # a2's submission (22) is not scanned
