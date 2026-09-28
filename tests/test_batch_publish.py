"""Post several artworks at once (4.43.0, spec 010).

A batch is planned without side effects and queued only as planned. The site checks
themselves are ``manager.post_artwork``'s, so here the poster is a fake whose answers
are set per test; one test runs the real ``_site_check`` wiring against it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import posting_queries
from database.db import get_connection
from posting import artwork_reader, batch

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def arch(tmp_path, monkeypatch):
    a = tmp_path / "Artwork"
    a.mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: a)
    return a


@pytest.fixture
def ok_sites(monkeypatch):
    """Every site accepts unless the test says otherwise: {(piece, site): reason}."""
    refusals: dict = {}

    def fake(art, plat, acct, persona, renders=(batch.AUTO,)):
        if (art.name, plat) in refusals:
            return [{"row_key": "", "resolved": "", "label": "The original", "requires": "any",
                     "interval": 5, "reason": refusals[(art.name, plat)]}]
        return [{"row_key": "" if k == batch.AUTO else k, "resolved": "" if k in (batch.AUTO, batch.PRIMARY) else k,
                 "label": k, "requires": "any", "interval": 70 if plat == "fa" else 5, "reason": None}
                for k in renders]

    monkeypatch.setattr(batch, "_site_check", fake)
    return refusals


def _piece(title, date="", rating="general"):
    return artwork_reader.create_artwork(title=title, image_filename="a.png", image_bytes=b"x",
                                         rating=rating, original_posted_at=date)


def _plan(names, **kw):
    kw.setdefault("platforms", ["ib", "fa"])
    kw.setdefault("now", NOW)
    kw.setdefault("start", "2026-10-02T09:00:00+00:00")
    return batch.plan(names, **kw)


def _persona(conn, name="Main"):
    cur = conn.execute("INSERT INTO personas(name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


# ── ordering + slots ─────────────────────────────────────────────────────────

def test_oldest_first_and_six_hour_slots(arch, ok_sites):
    b = _piece("B", "2025-03-01")
    a = _piece("A", "2025-01-01")
    c = _piece("C", "2025-06-01")
    p = _plan([b, a, c], gap_minutes=360)
    assert [x["name"] for x in p["pieces"]] == [a, b, c]
    assert [x["slot"] for x in p["pieces"]] == ["2026-10-02 09:00:00", "2026-10-02 15:00:00", "2026-10-02 21:00:00"]
    assert p["totals"] == {"pieces": 3, "posts": 6, "skipped_pieces": 0}


def test_newest_first_and_as_selected(arch, ok_sites):
    a, b = _piece("A", "2025-01-01"), _piece("B", "2025-02-01")
    assert [x["name"] for x in _plan([a, b], order="newest")["pieces"]] == [b, a]
    assert [x["name"] for x in _plan([b, a], order="selected")["pieces"]] == [b, a]


def test_all_now_gives_every_piece_the_start(arch, ok_sites):
    a, b = _piece("A"), _piece("B")
    p = _plan([a, b], gap_minutes=0, start=None)
    assert {x["slot"] for x in p["pieces"]} == {"2026-10-01 09:00:00"}


def test_a_fully_skipped_piece_takes_no_slot(arch, ok_sites):
    a, b, c = _piece("A", "2025-01-01"), _piece("B", "2025-02-01"), _piece("C", "2025-03-01")
    ok_sites[(b, "ib")] = ok_sites[(b, "fa")] = "rating not allowed"
    p = _plan([a, b, c], gap_minutes=60)
    slots = {x["name"]: x["slot"] for x in p["pieces"]}
    assert slots[b] is None and slots[c] == "2026-10-02 10:00:00"
    assert p["totals"]["skipped_pieces"] == 1


# ── skips ────────────────────────────────────────────────────────────────────

def test_already_posted_site_is_skipped(arch, ok_sites):
    a = _piece("A")
    conn = get_connection()
    try:
        posting_queries.upsert_publication(conn, a, 0, "ib", content_type="artwork",
                                           external_url="https://example.com/1", status="posted")
    finally:
        conn.close()
    x = _plan([a])["pieces"][0]
    assert [s["platform"] for s in x["posts"]] == ["fa"]
    assert x["skips"] == [{"platform": "ib", "reason": "already posted there"}]


def test_site_refusal_is_reported_in_its_own_words(arch, ok_sites):
    a = _piece("A", rating="adult")
    ok_sites[(a, "fa")] = "FurAffinity doesn't take that"
    x = _plan([a])["pieces"][0]
    assert {"platform": "fa", "version": "", "reason": "FurAffinity doesn't take that"} in x["skips"]


def test_another_personas_piece_is_skipped_whole(arch, ok_sites, monkeypatch):
    a = _piece("A")
    conn = get_connection()
    try:
        mine, theirs = _persona(conn, "Mine"), _persona(conn, "Theirs")
    finally:
        conn.close()
    monkeypatch.setattr(batch, "_owner_personas", lambda conn, art: {theirs})
    x = _plan([a], persona_id=mine)["pieces"][0]
    assert x["skip"] == "belongs to another persona" and x["posts"] == []
    monkeypatch.setattr(batch, "_owner_personas", lambda conn, art: {mine})
    assert _plan([a], persona_id=mine)["pieces"][0]["posts"]


def test_persona_required_once_accounts_belong_to_personas(arch, ok_sites):
    a = _piece("A")
    conn = get_connection()
    try:
        pid = _persona(conn)
        _plan([a])                          # personas, but no account is anyone's: nothing to pick
        conn.execute("INSERT INTO accounts(platform, label, handle, enabled, persona_id) VALUES ('fa','x','x',1,?)", (pid,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(batch.BatchError, match="posted as one persona"):
        _plan([a])


def test_missing_piece_is_reported_not_queued(arch, ok_sites):
    p = _plan(["No_Such_Piece"])
    assert p["pieces"][0]["skip"] == "not found" and p["totals"]["posts"] == 0


@pytest.mark.parametrize("kw,msg", [
    ({"gap_minutes": 5}, "15 minutes"),
    ({"gap_minutes": 60 * 24 * 31}, "30 days"),
    ({"start": "2020-01-01T00:00:00+00:00"}, "now or later"),
    ({"order": "random"}, "order"),
    ({"platforms": []}, "at least one site"),
    ({"account_ids": {"fa": "x"}}, "account number"),
])
def test_refused_requests(arch, ok_sites, kw, msg):
    a = _piece("A")
    with pytest.raises(batch.BatchError, match=msg):
        _plan([a], **kw)


def test_over_200_pieces_refused(arch):
    with pytest.raises(batch.BatchError, match="at most 200"):
        batch.plan([f"p{i}" for i in range(201)], platforms=["ib"], now=NOW)


def test_plan_writes_nothing(arch, ok_sites):
    a = _piece("A")
    _plan([a])
    conn = get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM posting_queue").fetchone()[0] == 0
    finally:
        conn.close()


# ── the real site check, against a fake poster ───────────────────────────────

class FakePoster:
    requires_mode = "desktop"
    max_rating = "general"

    def __init__(self, refusal=None, errors=None):
        self._r, self._e = refusal, errors or []

    def refusal(self, package):
        return self._r

    def validate(self, package):
        return self._e


def test_site_check_runs_refusal_then_validate(arch, monkeypatch):
    from posting import manager
    a = artwork_reader.load_artwork(_piece("A"))
    monkeypatch.setattr(manager, "_get_poster", lambda plat, acct=None: FakePoster(refusal="too spicy"))
    r = batch._site_check(a, "fa", None, None)
    assert [x["reason"] for x in r] == ["too spicy"] and r[0]["requires"] == "desktop"
    monkeypatch.setattr(manager, "_get_poster", lambda plat, acct=None: FakePoster(errors=["no title", "no tags"]))
    assert batch._site_check(a, "fa", None, None)[0]["reason"] == "no title; no tags"
    monkeypatch.setattr(manager, "_get_poster", lambda plat, acct=None: FakePoster())
    r = batch._site_check(a, "fa", None, None)
    assert r[0]["reason"] is None and r[0]["row_key"] == "" and r[0]["label"] == "The original"


# ── queue ────────────────────────────────────────────────────────────────────

def _rows():
    conn = get_connection()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM posting_queue ORDER BY queue_id")]
    finally:
        conn.close()


def test_queue_rows_follow_the_plan(arch, ok_sites):
    a, b = _piece("A", "2025-01-01"), _piece("B", "2025-02-01")
    p = _plan([a, b], platforms=["bsky", "ib"], gap_minutes=120)
    out = batch.queue(p)
    rows = _rows()
    assert out["rows"] == 4 and out["pieces"] == 2 and len(rows) == 4
    assert {r["drip_group"] for r in rows} == {out["drip_group"]}
    assert [(r["story_name"], r["platform"]) for r in rows] == [(a, "ib"), (a, "bsky"), (b, "ib"), (b, "bsky")]
    assert rows[0]["title_override"] == "🎨 batch 1/2" and rows[2]["scheduled_at"] == "2026-10-02 11:00:00"
    assert {r["content_type"] for r in rows} == {"artwork"} and {r["announce"] for r in rows} == {0}


def test_announce_is_carried(arch, ok_sites):
    a = _piece("A")
    batch.queue(_plan([a], announce=True))
    assert {r["announce"] for r in _rows()} == {1}


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from routes.artwork_api import artwork_router
    app = FastAPI()
    app.include_router(artwork_router)
    return TestClient(app)


def _body(names, **kw):
    return {"names": names, "platforms": ["ib"], "start": "2099-01-01T09:00:00+00:00", "gap_minutes": 60, **kw}


def test_route_plan_then_queue(arch, ok_sites, client):
    a = _piece("A")
    plan = client.post("/api/artwork/batch/plan", json=_body([a])).json()
    assert plan["totals"]["posts"] == 1
    expect = batch.post_set(plan)
    assert client.post("/api/artwork/batch", json=_body([a], expect=expect)).status_code == 400   # no confirm_live
    r = client.post("/api/artwork/batch", json=_body([a], expect=expect, confirm_live=True))
    assert r.status_code == 200 and r.json()["rows"] == 1


def test_route_refuses_a_changed_plan(arch, ok_sites, client):
    a = _piece("A")
    r = client.post("/api/artwork/batch", json=_body([a], expect=[[a, "fa", ""]], confirm_live=True))
    assert r.status_code == 409 and r.json()["plan"]["totals"]["posts"] == 1
    assert _rows() == []


def test_route_bad_request_is_400(arch, client):
    assert client.post("/api/artwork/batch/plan", json=_body([], )).status_code == 400


def test_real_owner_lookup_on_an_unposted_piece(arch):
    conn = get_connection()
    try:
        assert batch._owner_personas(conn, artwork_reader.load_artwork(_piece("A"))) == set()
    finally:
        conn.close()


# ── scheduler: announce column ───────────────────────────────────────────────

def _queue_row(name, plat, group, announce):
    conn = get_connection()
    try:
        qid = posting_queries.add_to_queue(conn, name, 0, plat, content_type="artwork",
                                           drip_group=group, announce=announce)
        return dict(conn.execute("SELECT * FROM posting_queue WHERE queue_id = ?", (qid,)).fetchone())
    finally:
        conn.close()


class _Row(dict):
    def keys(self):
        return list(super().keys())


@pytest.fixture
def sched(monkeypatch):
    import asyncio
    from posting import manager, scheduler
    calls = {"post": [], "announce": []}

    async def fake_post(name, platforms, **kw):
        calls["post"].append(kw.get("announce_discord", "missing"))
        return [{"platform": platforms[0], "success": True}]

    async def fake_announce(name, **kw):
        calls["announce"].append(name)

    async def quiet(*a, **k):
        return None

    monkeypatch.setattr(manager, "post_artwork", fake_post)
    monkeypatch.setattr(manager, "announce_artwork", fake_announce)
    monkeypatch.setattr(scheduler, "_notify_completion", quiet)
    run = lambda row: asyncio.run(scheduler._process_queue_item(_Row(row)))   # noqa: E731
    return calls, run


def test_batch_rows_never_announce_alone_and_the_piece_announces_once(sched):
    calls, run = sched
    r1, r2 = _queue_row("A", "ib", "g1", 1), _queue_row("A", "fa", "g1", 1)
    run(r1)
    assert calls["post"] == [False] and calls["announce"] == []        # a sibling is still pending
    run(r2)
    assert calls["post"] == [False, False] and calls["announce"] == ["A"]


def test_quiet_batch_rows_do_not_announce(sched):
    calls, run = sched
    run(_queue_row("A", "ib", "g2", 0))
    assert calls["post"] == [False] and calls["announce"] == []


def test_legacy_rows_still_follow_the_switch(sched):
    calls, run = sched
    run(_queue_row("A", "ib", None, None))
    assert calls["post"] == [None] and calls["announce"] == []


def test_upgrade_adds_the_announce_column(tmp_path, monkeypatch):
    """A posting_queue from before 4.43.0 (no ``announce``) boots clean and gains it."""
    import sqlite3
    import config
    from database import db
    schema = (config.resource_path("database/posting_schema.sql")).read_text(encoding="utf-8")
    old = "\n".join(line for line in schema.splitlines() if not line.strip().startswith("announce "))
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript(old)
    c.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    db.init_db()
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(posting_queue)")}
    assert "announce" in cols


# ── UI wiring (source contracts) ─────────────────────────────────────────────

def _src(rel):
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / rel).read_text(encoding="utf-8")


def test_library_select_mode_and_batch_dialog_are_wired():
    js = _src("frontend/js/masterpieces.js")
    assert "data-mp-select" in js and "_toggleSel(card)" in js and "e.preventDefault();" in js
    dlg = js.split("async _openBatch() {", 1)[1].split("\n    },", 1)[0]
    assert "API.batchPlan(body())" in dlg and "'/api/artwork/batch'" in dlg
    assert "confirm_live: true" in dlg and "expect" in dlg and "resp.status === 409" in dlg
    assert "Components.personaPicker" in dlg
    # quiet by default: the announce box and the announcer rows start unticked
    assert '<input type="checkbox" id="bp-announce">' in dlg
    assert "ann.map(c => row(c, false))" in dlg


def test_queue_page_labels_and_cancels_batches():
    js = _src("frontend/js/posting.js")
    assert "🎨 Cancel batch" in js
    art = js.split("} else if (isArt) {", 1)[1].split("} else {", 1)[0]
    assert "item.drip_group && item.title_override" in art


# ── versions, per piece ──────────────────────────────────────────────────────

def _with_versions(title="V", rating="adult"):
    name = _piece(title, rating=rating)
    d = artwork_reader.get_artwork_archive_path() / name
    (d / "sfw.png").write_bytes(b"s")
    (d / "alt.png").write_bytes(b"t")
    artwork_reader.save_artwork_metadata(name, {"variants": [
        {"key": "sfw", "label": "SFW", "image": "sfw.png", "rating": "general"},
        {"key": "alt", "label": "Alt colours", "image": "alt.png", "rating": rating},
    ]})
    return name


def test_a_piece_with_versions_offers_the_single_publish_choices(arch, ok_sites):
    x = _plan([_with_versions()])["pieces"][0]
    assert [v["key"] for v in x["versions"]] == [batch.AUTO, batch.PRIMARY, "sfw", "alt"]
    assert x["chosen"] == [batch.AUTO] and len(x["posts"]) == 2          # default: one per site
    assert _plan([_piece("Plain")])["pieces"][0]["versions"] == []


def test_chosen_versions_each_become_a_post_per_site(arch, ok_sites):
    n = _with_versions()
    x = _plan([n], renders={n: [batch.PRIMARY, "alt"]})["pieces"][0]
    assert sorted((p["platform"], p["variant_key"]) for p in x["posts"]) == [
        ("fa", "__primary__"), ("fa", "alt"), ("ib", "__primary__"), ("ib", "alt")]
    rows = batch.queue(_plan([n], renders={n: [batch.PRIMARY, "alt"]}))
    assert rows["rows"] == 4
    assert sorted(r["variant_key"] for r in _rows()) == ["__primary__", "__primary__", "alt", "alt"]


def test_no_version_chosen_skips_the_piece(arch, ok_sites):
    n = _with_versions()
    assert _plan([n], renders={n: []})["pieces"][0]["skip"] == "no version chosen"


def test_a_version_already_on_a_site_is_skipped_but_another_version_is_not(arch, ok_sites):
    n = _with_versions()
    conn = get_connection()
    try:
        posting_queries.upsert_publication(conn, n, 0, "ib", content_type="artwork", variant_key="alt",
                                           external_url="https://example.com/2", status="posted")
    finally:
        conn.close()
    x = _plan([n], renders={n: ["alt", "sfw"]}, platforms=["ib"])["pieces"][0]
    assert [p["variant_key"] for p in x["posts"]] == ["sfw"]
    assert x["skips"] == [{"platform": "ib", "version": "alt", "reason": "already posted there"}]
    # Automatic alone keeps the site-level rule: something is already on Inkbunny.
    assert _plan([n], platforms=["ib"])["pieces"][0]["skips"][0]["reason"] == "already posted there"


def test_bad_renders_are_refused(arch, ok_sites):
    n = _piece("A")
    for bad, msg in (("x", "map a piece"), ({n: "sfw"}, "list of version"), ({n: ["k"] * 33}, "at most 32")):
        with pytest.raises(batch.BatchError, match=msg):
            _plan([n], renders=bad)


def test_real_site_check_resolves_versions(arch, monkeypatch):
    from posting import manager
    art = artwork_reader.load_artwork(_with_versions())
    monkeypatch.setattr(manager, "_get_poster", lambda plat, acct=None: FakePoster())   # max_rating general
    r = batch._site_check(art, "fa", None, None, (batch.AUTO, "sfw", batch.PRIMARY, "nope"))
    # Automatic on a general-only site picks "sfw"; ticking "sfw" too is the same submission.
    assert [(x["row_key"], x["resolved"]) for x in r] == [("", "sfw"), (batch.PRIMARY, ""), ("nope", "nope")]
    assert r[-1]["reason"] == "no version called 'nope' on this piece"


# ── one site, several rows: spaced by the site's own gap ─────────────────────

def test_all_now_spaces_rows_for_the_same_site(arch, ok_sites):
    a, b = _piece("A", "2025-01-01"), _piece("B", "2025-02-01")
    batch.queue(_plan([a, b], platforms=["fa", "ib"], gap_minutes=0, start=None))
    at = {(r["story_name"], r["platform"]): r["scheduled_at"] for r in _rows()}
    assert at[(a, "fa")] == at[(a, "ib")] == "2026-10-01 09:00:00"     # different sites: no wait between them
    assert at[(b, "ib")] == "2026-10-01 09:00:35"                        # Inkbunny again: 5 s + 30 s
    assert at[(b, "fa")] == "2026-10-01 09:01:40"                        # FurAffinity again: 70 s + 30 s


def test_spread_batches_are_never_pushed_later(arch, ok_sites):
    a, b = _piece("A", "2025-01-01"), _piece("B", "2025-02-01")
    batch.queue(_plan([a, b], platforms=["fa"], gap_minutes=60))
    assert [r["scheduled_at"] for r in _rows()] == ["2026-10-02 09:00:00", "2026-10-02 10:00:00"]


def test_dialog_offers_per_piece_versions_and_sends_them():
    js = _src("frontend/js/masterpieces.js")
    dlg = js.split("async _openBatch() {", 1)[1].split("\n    },", 1)[0]
    assert 'class="bp-ver"' in dlg and "renders[box.dataset.name]" in dlg
    assert "renders,\n" in dlg.replace("\r\n", "\n")                     # sent with every plan / queue
    assert "s.variant_key || ''" in dlg                                   # the confirm compares versions too


# ── 4.43.0 pre-release review fixes ──────────────────────────────────────────

def test_only_the_exact_piece_name_counts(arch, ok_sites):
    a = _piece("A")
    p = _plan([f"./{a}", f"x/../{a}", a, a])
    assert sorted((x["name"], x["skip"]) for x in p["pieces"] if x["name"] != a) == [
        (f"./{a}", "not found"), (f"x/../{a}", "not found")]                # variant spellings never plan
    assert sum(1 for x in p["pieces"] if x["name"] == a) == 1              # duplicates collapse
    assert p["totals"]["pieces"] == 1


def test_a_bad_persona_id_is_a_clear_refusal(arch, ok_sites):
    with pytest.raises(batch.BatchError, match="persona number"):
        _plan([_piece("A")], persona_id="abc")


def test_no_announcement_when_every_post_failed(sched, monkeypatch):
    import asyncio
    from posting import manager, scheduler
    calls, _ = sched

    async def failing(name, platforms, **kw):
        calls["post"].append(kw.get("announce_discord"))
        return [{"platform": platforms[0], "success": False, "error": "nope", "retry_queued": True}]

    monkeypatch.setattr(manager, "post_artwork", failing)
    for row in (_queue_row("A", "ib", "g3", 1), _queue_row("A", "fa", "g3", 1)):
        asyncio.run(scheduler._process_queue_item(_Row(row)))
    assert calls["announce"] == []
