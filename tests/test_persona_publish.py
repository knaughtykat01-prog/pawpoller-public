"""Persona-first publishing refuses instead of defaulting (4.2.0).

docs/specs/publish_flow.md §3, §7.7, §10 Q1/Q2. The dangerous default: a
publish that omitted a platform from ``account_ids`` fell through to that
platform's global default account — which can belong to a DIFFERENT persona —
and ``create=True`` would invent an account rather than error. In a
persona-first publish that is the one thing that must never happen, so the
resolver refuses, the refusal is a result row (the results panel shows it),
and the schedule routes check at schedule time rather than at 3am.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest


@pytest.fixture()
def conn(monkeypatch):
    import config
    from database import db as dbm
    monkeypatch.setattr(config, "DB_PATH", os.path.join(tempfile.mkdtemp(), "pp.db"))
    dbm.init_db()
    c = dbm.get_connection()
    yield c
    c.close()


def _seed(conn):
    """Two personas. Inkwolf holds two FA accounts (one default) and one IB;
    Penwright holds one FA. Nobody holds Weasyl."""
    from database import accounts, personas
    ink = personas.create_persona(conn, "Inkwolf")
    pen = personas.create_persona(conn, "Penwright")
    fa1 = accounts.create_account(conn, "fa", "FA main", is_default=True)
    fa2 = accounts.create_account(conn, "fa", "FA alt")
    ib1 = accounts.create_account(conn, "ib", "IB")
    fa3 = accounts.create_account(conn, "fa", "FA other")
    for aid in (fa1, fa2, ib1):
        personas.assign_account_persona(conn, aid, ink)
    personas.assign_account_persona(conn, fa3, pen)
    conn.commit()
    return dict(ink=ink, pen=pen, fa1=fa1, fa2=fa2, ib1=ib1, fa3=fa3)


class TestAccountsForPersona:
    def test_returns_a_list_per_platform_default_first(self, conn):
        """A LIST, so two-accounts-one-platform is visible, not guessed."""
        from database import personas
        s = _seed(conn)
        got = personas.accounts_for_persona(conn, s["ink"])
        assert got["fa"] == [s["fa1"], s["fa2"]]
        assert got["ib"] == [s["ib1"]]
        assert "ws" not in got

    def test_platform_filter_and_disabled_excluded(self, conn):
        from database import accounts, personas
        s = _seed(conn)
        accounts.update_account(conn, s["fa2"], enabled=0)
        conn.commit()
        assert personas.accounts_for_persona(conn, s["ink"], platforms=["fa"]) == {"fa": [s["fa1"]]}


class TestPersonaAccountError:
    def test_no_account_is_refused_and_names_the_persona(self, conn):
        from database import personas
        s = _seed(conn)
        err = personas.persona_account_error(conn, "ws", None, s["ink"])
        assert err and "Inkwolf" in err and "ws" in err and "default" in err

    def test_another_personas_account_is_refused(self, conn):
        """The friend's-account case: an id that exists, on the right platform,
        belonging to somebody else."""
        from database import personas
        s = _seed(conn)
        err = personas.persona_account_error(conn, "fa", s["fa3"], s["ink"])
        assert err and "does not belong to Inkwolf" in err

    def test_wrong_platform_and_disabled(self, conn):
        from database import accounts, personas
        s = _seed(conn)
        assert "not a fa account" in personas.persona_account_error(conn, "fa", s["ib1"], s["ink"])
        accounts.update_account(conn, s["fa2"], enabled=0)
        conn.commit()
        assert "disabled" in personas.persona_account_error(conn, "fa", s["fa2"], s["ink"])

    def test_own_enabled_account_passes(self, conn):
        from database import personas
        s = _seed(conn)
        assert personas.persona_account_error(conn, "fa", s["fa2"], s["ink"]) is None


class TestResolver:
    def test_persona_path_never_reaches_the_default(self, conn, monkeypatch):
        """Even where a default account EXISTS on the platform."""
        from posting import manager
        s = _seed(conn)
        conn.close()
        with pytest.raises(ValueError) as ei:
            manager._resolve_account_id("fa", None, persona_id=s["ink"])
        assert "no account on fa" in str(ei.value)

    def test_persona_path_returns_the_verified_id(self, conn):
        from posting import manager
        s = _seed(conn)
        conn.close()
        assert manager._resolve_account_id("fa", s["fa2"], persona_id=s["ink"]) == s["fa2"]

    def test_non_persona_path_is_unchanged(self, conn):
        """Explicit id wins; no persona means the old default behaviour."""
        from posting import manager
        s = _seed(conn)
        conn.close()
        assert manager._resolve_account_id("fa", s["fa3"]) == s["fa3"]
        assert manager._resolve_account_id("fa", None) == s["fa1"]   # the platform default


class TestManagerRefusalIsAResultRow:
    def test_post_artwork_reports_the_refusal_and_continues(self, conn, monkeypatch):
        """A refused platform must not abort the publish or vanish — it is a
        row with refused=True, so the results panel names it."""
        from posting import manager, artwork_reader
        s = _seed(conn)
        conn.close()
        monkeypatch.setattr(artwork_reader, "load_artwork", lambda name: object())
        calls = []

        def no_poster(plat, aid):
            calls.append((plat, aid))
            raise RuntimeError("a poster was built for a refused platform")
        monkeypatch.setattr(manager, "_get_poster", no_poster)
        res = asyncio.run(manager.post_artwork("Sample_Piece", ["ws"], account_ids={}, persona_id=s["ink"]))
        assert res == [{"platform": "ws", "success": False, "url": "", "refused": True,
                        "error": res[0]["error"]}]
        assert "Inkwolf has no account on ws" in res[0]["error"]
        assert calls == [], "no poster may be built for a refused platform"


class TestScheduleRoutesCheckNow:
    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient
        import dashboard
        return TestClient(dashboard.app)

    def test_artwork_schedule_refuses_before_touching_the_archive(self, client, conn):
        """400 with the reason, not a 404 for the artwork and not a queued row."""
        s = _seed(conn)
        conn.close()
        r = client.post("/api/artwork/schedule", json={
            "artwork_name": "Nope", "platform": "ws", "scheduled_at": "2099-01-01T00:00:00Z",
            "persona_id": s["ink"]})
        assert r.status_code == 400, r.text
        assert "Inkwolf has no account on ws" in r.json()["detail"]


class TestFrontendWiring:
    """Source-order checks, as tests/test_publish_confirm.py does for 4.1.0."""

    def _src(self, p):
        return open(p, encoding="utf-8").read()

    def test_the_shared_picker_exists_and_sends_ids_even_for_one_account(self):
        js = self._src("frontend/js/components.js")
        i = js.index("async personaPicker(o) {")
        block = js[i:i + 9000]
        assert 'type="hidden"' in block, (
            "one account under a persona must still travel as an explicit id — "
            "the platform default may be another persona's")
        assert "acct-choice-mark" in block, "two accounts on one platform must be MARKED, not collapsed"
        assert "acct-missing" in block, "a platform the persona lacks is shown disabled with a reason"

    def test_every_publish_surface_sends_persona_id(self):
        art = self._src("frontend/js/artwork.js")
        assert art.count("persona_id: this._personaId(") >= 3, "new form, detail publish, detail schedule"
        assert "persona_id: this._qpPersonaId(" in art
        posts = self._src("frontend/js/posts.js")
        assert posts.count("persona_id: this._personaId()") >= 2, "publish and schedule"
        mp = self._src("frontend/js/masterpieces.js")
        assert "persona_id: personaId" in mp
        pc = self._src("frontend/js/publish_check.js")
        assert "persona_id:" in pc

    def test_quick_publish_no_longer_collapses_two_accounts(self):
        art = self._src("frontend/js/artwork.js")
        i = art.index("_qpBuildMap(accounts) {")
        block = art[i:i + 2500]
        assert "multi" in block, "_qpBuildMap must report platforms where a persona has >1 account"
        assert "qp-acct" in art, "and Quick Publish must render a choice for them"

    def test_posting_page_lost_its_unpicked_publish_buttons(self):
        posting = self._src("frontend/js/posting.js")
        assert "upload-to" not in posting
        assert "_uploadTo(" not in posting
        app = self._src("frontend/js/app.js")
        assert "'upload-to'" not in app

    def test_sync_confirms_with_the_shared_dialog(self):
        mp = self._src("frontend/js/masterpieces.js")
        i = mp.index("async _syncAll(btn) {")
        block = mp[i:i + 2500]
        assert "window.confirm(" not in block
        assert "Components.confirmPublish(" in block and "verb: 'Sync'" in block
        assert "showPublishResults(" in block

    def test_telegram_disclosure_is_named(self):
        art = self._src("frontend/js/artwork.js")
        assert "Telegram options" in art
        assert "<summary>Override</summary>" not in art
