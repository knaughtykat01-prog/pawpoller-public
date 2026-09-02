"""Tests for the desktop↔server work handoff (mirroring Stage 2).

The weight is on three things, because those are where a bug costs something
real rather than merely failing:

* a job must not be executable twice (a live platform gets two posts),
* a result must not be recorded twice (a publication is double-counted),
* an account must never be resolved by a raw id across the boundary (the
  2026-08-12 corruption).
"""
from __future__ import annotations

import sqlite3

import pytest

from database import accounts as accounts_db
from database import posting_queries
from mirror import handoff


@pytest.fixture
def conn(tmp_path, monkeypatch):
    import config
    from database import db as db_mod
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "pawpoller.db")
    db_mod.init_db()
    c = db_mod.get_connection()
    yield c
    c.close()


def _account(conn, platform, handle, *, default=False, label=None):
    cur = conn.execute(
        "INSERT INTO accounts (platform, label, handle, enabled, is_default, sort_order) "
        "VALUES (?, ?, ?, 1, ?, 0)",
        (platform, label or handle, handle, 1 if default else 0),
    )
    conn.commit()
    return cur.lastrowid


def _queue_desktop_job(conn, *, story="Chosen", chapter=1, platform="fa", account_id=None):
    qid = posting_queries.add_to_queue(
        conn, story, chapter, platform, "post",
        account_id=account_id, requires="desktop",
    )
    conn.commit()
    return qid


# ── Server half: describing jobs ──────────────────────────────

class TestDescribeJobs:
    def test_pending_desktop_jobs_are_offered(self, conn):
        acct = _account(conn, "fa", "KnaughtyKat", default=True)
        _queue_desktop_job(conn, account_id=acct)
        jobs = handoff.describe_jobs(conn)
        assert len(jobs) == 1
        assert jobs[0]["story_name"] == "Chosen"
        assert jobs[0]["platform"] == "fa"

    def test_the_account_travels_as_an_identity_not_an_id(self, conn):
        """account_id means a different account on each install."""
        acct = _account(conn, "fa", "KnaughtyKat", default=True)
        _queue_desktop_job(conn, account_id=acct)
        job = handoff.describe_jobs(conn)[0]
        assert job["account"] == {"platform": "fa", "handle": "KnaughtyKat"}
        assert "account_id" not in job

    def test_already_claimed_jobs_are_not_re_offered(self, conn):
        """Re-offering a processing row is how the same post goes out twice."""
        acct = _account(conn, "fa", "KnaughtyKat", default=True)
        qid = _queue_desktop_job(conn, account_id=acct)
        posting_queries.claim_queue_item(conn, qid, "desktop")
        assert handoff.describe_jobs(conn) == []

    def test_requires_any_jobs_are_not_offered(self, conn):
        _account(conn, "ao3", "KnaughtyKat", default=True)
        posting_queries.add_to_queue(conn, "Other", 1, "ao3", "post")
        conn.commit()
        assert handoff.describe_jobs(conn) == []


# ── Desktop half: importing ───────────────────────────────────

class TestImportJob:
    def _job(self, qid=99, handle="KnaughtyKat"):
        return {"origin_queue_id": qid, "action": "post", "content_type": "story",
                "story_name": "Chosen", "chapter_index": 1, "platform": "fa",
                "account": {"platform": "fa", "handle": handle}, "overrides": {}}

    def test_import_creates_a_local_queue_row(self, conn):
        _account(conn, "fa", "KnaughtyKat", default=True)
        result = handoff.import_job(conn, self._job(), "https://srv")
        assert result["imported"] is True
        row = conn.execute("SELECT * FROM posting_queue WHERE queue_id = ?",
                           (result["queue_id"],)).fetchone()
        assert row["story_name"] == "Chosen"
        assert row["origin_queue_id"] == 99
        assert row["origin_server"] == "https://srv"

    def test_imported_row_is_runnable_here(self, conn):
        """It arrived as requires='desktop' from the server's point of view;
        on the worker it is ordinary work, or the local scheduler skips it."""
        _account(conn, "fa", "KnaughtyKat", default=True)
        result = handoff.import_job(conn, self._job(), "https://srv")
        row = conn.execute("SELECT requires, status FROM posting_queue WHERE queue_id = ?",
                           (result["queue_id"],)).fetchone()
        assert row["requires"] == "any"
        assert row["status"] == "pending"

    def test_the_account_is_resolved_locally_by_handle(self, conn):
        """The local id differs from the server's; the handle is the key."""
        _account(conn, "ao3", "someone", default=True)      # takes id 1 locally
        fa = _account(conn, "fa", "KnaughtyKat", default=True)
        result = handoff.import_job(conn, self._job(), "https://srv")
        assert result["account_id"] == fa

    def test_an_unknown_platform_is_refused_not_guessed(self, conn):
        """Posting as the wrong account is worse than not posting."""
        with pytest.raises(LookupError):
            handoff.import_job(conn, self._job(), "https://srv")

    def test_reimporting_the_same_job_is_refused(self, conn):
        """Otherwise the same post goes out twice."""
        _account(conn, "fa", "KnaughtyKat", default=True)
        first = handoff.import_job(conn, self._job(), "https://srv")
        second = handoff.import_job(conn, self._job(), "https://srv")
        assert second["imported"] is False
        assert second["queue_id"] == first["queue_id"]
        assert conn.execute("SELECT COUNT(*) FROM posting_queue").fetchone()[0] == 1

    def test_the_same_id_from_a_different_server_is_a_different_job(self, conn):
        _account(conn, "fa", "KnaughtyKat", default=True)
        handoff.import_job(conn, self._job(), "https://one")
        assert handoff.import_job(conn, self._job(), "https://two")["imported"] is True


# ── Server half: applying results ─────────────────────────────

class TestApplyResult:
    def _setup(self, conn):
        acct = _account(conn, "fa", "KnaughtyKat", default=True)
        return acct, _queue_desktop_job(conn, account_id=acct)

    def _result(self, qid, **kw):
        base = {"origin_queue_id": qid, "platform": "fa", "story_name": "Chosen",
                "chapter_index": 1, "content_type": "story", "success": True,
                "external_id": "12345678",
                "external_url": "https://furaffinity.net/view/12345678",
                "account": {"platform": "fa", "handle": "KnaughtyKat"}}
        base.update(kw)
        return base

    def test_success_records_a_publication_and_completes_the_row(self, conn):
        acct, qid = self._setup(conn)
        out = handoff.apply_result(conn, self._result(qid))
        assert out["applied"] is True and out["pub_id"]
        pub = posting_queries.get_publication_by_story(conn, "Chosen", 1, "fa", acct)
        assert pub["external_id"] == "12345678"
        row = conn.execute("SELECT status FROM posting_queue WHERE queue_id = ?",
                           (qid,)).fetchone()
        assert row["status"] == "completed"

    def test_the_server_allocates_its_own_pub_id(self, conn):
        """Nothing in the payload dictates a surrogate id here."""
        _, qid = self._setup(conn)
        out = handoff.apply_result(conn, self._result(qid, pub_id=999999))
        assert out["pub_id"] != 999999

    def test_failure_records_no_publication(self, conn):
        acct, qid = self._setup(conn)
        handoff.apply_result(conn, self._result(qid, success=False, error="FA 503"))
        assert posting_queries.get_publication_by_story(conn, "Chosen", 1, "fa", acct) is None
        row = conn.execute("SELECT status, last_error FROM posting_queue WHERE queue_id = ?",
                           (qid,)).fetchone()
        assert row["status"] == "failed" and "503" in row["last_error"]

    def test_a_replayed_result_is_ignored(self, conn):
        """Delivery is at-least-once, so the far side must be idempotent —
        otherwise a retried report double-counts the publication."""
        _, qid = self._setup(conn)
        handoff.apply_result(conn, self._result(qid))
        again = handoff.apply_result(conn, self._result(qid))
        assert again["applied"] is False and again["reason"] == "already completed"
        assert conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 1

    def test_a_cancelled_job_outranks_a_late_result(self, conn):
        _, qid = self._setup(conn)
        posting_queries.update_queue_status(conn, qid, "cancelled")
        out = handoff.apply_result(conn, self._result(qid))
        assert out["applied"] is False and out["reason"] == "cancelled"

    def test_mismatched_natural_keys_are_rejected(self, conn):
        """origin_queue_id is opaque, so it cannot detect a wrong row on its
        own — the natural keys are what prove the result belongs here."""
        _, qid = self._setup(conn)
        with pytest.raises(ValueError, match="does not match"):
            handoff.apply_result(conn, self._result(qid, story_name="Something_Else"))

    def test_an_unknown_queue_id_is_an_error(self, conn):
        self._setup(conn)
        with pytest.raises(LookupError):
            handoff.apply_result(conn, self._result(4242))

    def test_missing_fields_are_rejected(self, conn):
        _, qid = self._setup(conn)
        with pytest.raises(ValueError, match="missing"):
            handoff.apply_result(conn, {"origin_queue_id": qid, "platform": "fa"})

    def test_the_account_is_resolved_by_handle_not_position(self, conn):
        """The offset that corrupted prod: the same ordinal means different
        platforms on each install, so only the handle can be trusted."""
        _account(conn, "ws", "KnaughtyKat", default=True)     # local id 1
        fa = _account(conn, "fa", "KnaughtyKat", default=True)  # local id 2
        qid = _queue_desktop_job(conn, account_id=fa)
        out = handoff.apply_result(conn, self._result(qid))
        assert out["account_id"] == fa

    def test_an_unknown_handle_falls_back_to_the_platform_default(self, conn):
        """A seeded default carries no handle until the platform is connected,
        so the handle key cannot see it — same rule as apply_manifest."""
        fa = _account(conn, "fa", "", default=True)
        qid = _queue_desktop_job(conn, account_id=fa)
        out = handoff.apply_result(conn, self._result(qid))
        assert out["account_id"] == fa


# ── Reporting sweep ───────────────────────────────────────────

class TestReporting:
    def _imported(self, conn, status="completed"):
        _account(conn, "fa", "KnaughtyKat", default=True)
        r = handoff.import_job(conn, {
            "origin_queue_id": 77, "action": "post", "content_type": "story",
            "story_name": "Chosen", "chapter_index": 1, "platform": "fa",
            "account": {"platform": "fa", "handle": "KnaughtyKat"}, "overrides": {}},
            "https://srv")
        posting_queries.update_queue_status(conn, r["queue_id"], status)
        return r["queue_id"]

    def test_finished_jobs_are_pending_report(self, conn):
        qid = self._imported(conn)
        assert [r["queue_id"] for r in handoff.pending_reports(conn)] == [qid]

    def test_unfinished_jobs_are_not_reported(self, conn):
        self._imported(conn, status="processing")
        assert handoff.pending_reports(conn) == []

    def test_locally_queued_jobs_are_never_reported(self, conn):
        _account(conn, "ao3", "KnaughtyKat", default=True)
        qid = posting_queries.add_to_queue(conn, "Mine", 1, "ao3", "post")
        posting_queries.update_queue_status(conn, qid, "completed")
        conn.commit()
        assert handoff.pending_reports(conn) == []

    def test_marking_reported_removes_it_from_the_sweep(self, conn):
        qid = self._imported(conn)
        handoff.mark_reported(conn, qid)
        assert handoff.pending_reports(conn) == []

    def test_a_report_is_scoped_to_its_origin_server(self, conn):
        self._imported(conn)
        assert handoff.pending_reports(conn, "https://other") == []
        assert len(handoff.pending_reports(conn, "https://srv")) == 1

    def test_the_report_carries_natural_keys_and_the_opaque_handle(self, conn):
        qid = self._imported(conn)
        row = handoff.pending_reports(conn)[0]
        payload = handoff.build_report(conn, row)
        assert payload["origin_queue_id"] == 77
        assert payload["story_name"] == "Chosen" and payload["platform"] == "fa"
        assert payload["account"] == {"platform": "fa", "handle": "KnaughtyKat"}
        assert payload["success"] is True
        assert "account_id" not in payload

    def test_a_failed_job_reports_as_failure(self, conn):
        qid = self._imported(conn, status="failed")
        payload = handoff.build_report(conn, handoff.pending_reports(conn)[0])
        assert payload["success"] is False


# ── The account identity resolver ─────────────────────────────

class TestResolveIdentity:
    def test_exact_handle_wins(self, conn):
        _account(conn, "fa", "KnaughtyKat", default=True)
        other = _account(conn, "fa", "SecondFur")
        assert accounts_db.resolve_account_by_identity(conn, "fa", "SecondFur") == other

    def test_handle_match_is_case_insensitive(self, conn):
        acct = _account(conn, "fa", "KnaughtyKat", default=True)
        assert accounts_db.resolve_account_by_identity(conn, "fa", "knaughtykat") == acct

    def test_falls_back_to_the_platform_default(self, conn):
        acct = _account(conn, "fa", "", default=True)
        assert accounts_db.resolve_account_by_identity(conn, "fa", "unknown") == acct

    def test_unknown_platform_returns_none(self, conn):
        assert accounts_db.resolve_account_by_identity(conn, "fa", "x") is None

    def test_never_resolves_across_platforms(self, conn):
        """The whole point: the same handle exists on several platforms."""
        _account(conn, "ib", "KnaughtyKat", default=True)
        fa = _account(conn, "fa", "KnaughtyKat", default=True)
        assert accounts_db.resolve_account_by_identity(conn, "fa", "KnaughtyKat") == fa
