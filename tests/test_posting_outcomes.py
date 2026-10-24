"""Every site reports back (055, IKSILENT).

One final posting_log row per site per post; a "success" with no id and no link is
unconfirmed (never retried); retries used up leave one `gave_up` row; a job cut off by a
restart leaves `not_sent`. Stand-in posters, no network.
"""
import pytest

from database.db import init_db, get_connection
from database import posting_queries
from posting import artwork_reader, manager
from posting.platforms.base import PostResult


class _Stub:
    requires_mode = "any"

    def __init__(self, code, result):
        self.platform_id = code
        self.platform_name = {"ik": "Itaku"}.get(code, code)
        self._result = result
        self.posted = 0

    def validate(self, package):
        return []

    async def post(self, package):
        self.posted += 1
        return PostResult(**self._result)

    async def _rate_limit(self):
        pass


OK = {"success": True, "external_id": "1", "external_url": "https://example.com/1"}


@pytest.fixture(autouse=True)
def archive(tmp_path, monkeypatch):
    arch = tmp_path / "Artwork"
    arch.mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: arch)
    init_db()
    conn = get_connection()
    for t in ("posting_log", "posting_queue", "publications"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    return arch


def _piece():
    return artwork_reader.create_artwork(
        title="Sample Story", image_filename="pic.png", image_bytes=b"\x89PNG fake",
        rating="general", tags={"default": ["a", "b", "c", "d", "e"]})


def _stubs(monkeypatch, results):
    stubs = {code: _Stub(code, r) for code, r in results.items()}
    monkeypatch.setattr(manager, "_get_poster", lambda p, account_id=None: stubs[p])
    return stubs


def _log(name):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT platform, status, error_message FROM posting_log WHERE story_name = ? "
            "ORDER BY log_id", (name,)).fetchall()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_four_sites_one_failing_leave_four_final_rows(monkeypatch):
    name = _piece()
    _stubs(monkeypatch, {"ib": OK, "ws": OK, "sf": OK,
                         "fbr": {"success": False, "error": "HTTP 503 upstream hiccup"}})
    await manager.post_artwork(name, ["ib", "ws", "sf", "fbr"])
    rows = _log(name)
    assert sorted(r["platform"] for r in rows) == ["fbr", "ib", "sf", "ws"]
    assert {r["platform"]: r["status"] for r in rows}["fbr"] == "failed"
    assert not any(r["status"] == "pending" for r in rows)


@pytest.mark.asyncio
async def test_success_with_no_id_is_unconfirmed_and_never_retried(monkeypatch):
    name = _piece()
    _stubs(monkeypatch, {"ik": {"success": True, "external_id": "", "external_url": ""}})
    res = await manager.post_artwork(name, ["ik"])
    assert res[0]["success"] is False and res[0]["unconfirmed"] is True
    assert "Itaku took the upload but sent back no link" in res[0]["error"]
    assert res[0]["retry_queued"] is False
    assert [r["status"] for r in _log(name)] == ["unconfirmed"]
    conn = get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM posting_queue").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_retries_used_up_leave_one_gave_up_row(monkeypatch):
    name = _piece()
    acct = manager._resolve_account_id("ws", None)
    conn = get_connection()
    try:
        for _ in range(3):
            posting_queries.add_to_queue(conn, name, 0, "ws", "post", account_id=acct,
                                         content_type="artwork", priority=-1)
    finally:
        conn.close()
    _stubs(monkeypatch, {"ws": {"success": False, "error": "HTTP 503 upstream hiccup"}})
    res = await manager.post_artwork(name, ["ws"])
    rows = _log(name)
    assert [r["status"] for r in rows] == ["gave_up"]
    assert rows[0]["error_message"].startswith("gave up after 4 tries — HTTP 503")
    assert res[0]["retry_queued"] is False


@pytest.mark.asyncio
async def test_a_refused_site_still_gets_its_row(monkeypatch):
    name = _piece()
    _stubs(monkeypatch, {"ib": OK})
    await manager.post_artwork(name, ["ib"], variant_overrides={"ib": "no_such_render"})
    rows = _log(name)
    assert [r["status"] for r in rows] == ["failed"]
    assert "no render called" in rows[0]["error_message"]


def test_restart_marks_pending_not_sent_and_posts_nothing(monkeypatch):
    name = _piece()
    stubs = _stubs(monkeypatch, {"ib": OK, "ws": OK})
    manager.write_pending(name, ["ib", "ws"])
    conn = get_connection()
    try:   # rows from "the last process": created before this start
        conn.execute("UPDATE posting_log SET created_at = datetime('now', '-1 minute')")
        conn.commit()
    finally:
        conn.close()
    assert manager.mark_restart_leftovers() == 2
    rows = _log(name)
    assert [r["status"] for r in rows] == ["not_sent", "not_sent"]
    assert all(s.posted == 0 for s in stubs.values())


def test_bell_names_site_and_piece():
    from routes.api import _format_post_summary, _POST_STATUS_FOR_BELL
    row = {"platform": "ik", "story_name": "Sample_Story", "action": "post", "status": "unconfirmed"}
    assert _format_post_summary(row).startswith("Itaku: took the upload of Sample Story")
    assert _POST_STATUS_FOR_BELL["unconfirmed"] == "warn"
    assert _POST_STATUS_FOR_BELL["gave_up"] == "error"
    row = dict(row, status="not_sent")
    assert "not sent (PawPoller restarted)" in _format_post_summary(row)


# ── US2: the pre-check (055, FR-006) ─────────────────────────────────────────

def _piece_with(tags):
    return artwork_reader.create_artwork(
        title="Sample Story", image_filename="pic.png", image_bytes=b"\x89PNG fake",
        rating="adult", tags={"default": tags})


class _NoSend:
    """The real poster's checks, but post() must never be reached."""
    def __init__(self, real):
        self._real = real
        self.posted = 0

    def __getattr__(self, k):
        return getattr(self._real, k)

    async def post(self, package):
        self.posted += 1
        raise AssertionError("the check must not send")


def _real_posters(monkeypatch):
    from posting.platforms.e621 import E621Poster
    from posting.platforms.itaku import ItakuPoster
    made = {"ik": _NoSend(ItakuPoster()), "e621": _NoSend(E621Poster())}
    monkeypatch.setattr(manager, "_get_poster", lambda p, account_id=None: made[p])
    return made


def test_check_flags_tag_minimums_without_sending_or_logging(monkeypatch):
    name = _piece_with(["solo", "Red Fox", "anthro", "café"])
    made = _real_posters(monkeypatch)
    r = manager.publish_check(name, ["ik", "e621"])
    assert any("Itaku needs at least 5 tags; this piece has 4." in p for p in r["problems"]["ik"])
    assert any("this piece has 3 it will accept" in p for p in r["problems"]["e621"])
    assert {"tag": "café", "rule": "has accents or symbols e621 doesn't allow"} in r["left_out"]["e621"]
    assert r["tag_counts"]["e621"] == 3
    assert all(p.posted == 0 for p in made.values())
    assert _log(name) == []


@pytest.mark.asyncio
async def test_check_sentence_equals_the_posts_refusal(monkeypatch):
    name = _piece_with(["solo", "anthro", "fox"])
    _real_posters(monkeypatch)
    said = manager.publish_check(name, ["ik"])["problems"]["ik"]
    res = await manager.post_artwork(name, ["ik"])
    assert res[0]["error"] == "; ".join(said)


# ── FR-010: every dropped tag is named ───────────────────────────────────────

@pytest.mark.asyncio
async def test_itaku_names_a_tag_over_59_characters(monkeypatch):
    long_tag = "x" * 60
    name = _piece_with(["a", "b", "c", "d", "e", long_tag])
    _stubs(monkeypatch, {"ik": OK})
    res = await manager.post_artwork(name, ["ik"])
    assert res[0]["tags_left_out"] == [{"tag": long_tag, "rule": "longer than 59 characters"}]


def test_fa_names_the_tags_its_500_character_cut_drops():
    from posting import tag_budget
    tags = [f"tag{i:03d}_" + "y" * 40 for i in range(20)]
    kept, dropped = tag_budget.fit_report(tags, "fa")
    assert len(" ".join(kept)) <= 500
    assert [d["tag"] for d in dropped] == tags[len(kept):]
    assert all(d["rule"] == "over the 500-character total" for d in dropped)
    assert tag_budget.fit(tags, "fa") == kept


def test_unlimited_sites_tags_unchanged():
    from posting import tag_budget
    tags = ["Red Fox", "x" * 80]
    assert tag_budget.fit_report(tags, "ib") == (tags, [])


def test_check_writes_no_account_row():
    """A check writes nothing — not even a default account (release review, Low)."""
    name = _piece()
    conn = get_connection()
    try:
        before = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    finally:
        conn.close()
    r = manager.publish_check(name, ["zzz", "ib"])
    assert r["problems"]["zzz"] == ["Unknown platform: zzz"]
    conn = get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == before
    finally:
        conn.close()
