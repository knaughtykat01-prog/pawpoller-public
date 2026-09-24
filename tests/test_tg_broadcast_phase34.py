"""TGBROADCAST phases 3 and 4 — linked mode, polls, digest routing.

Phase 3 is the only part of that spec that adds a CONCEPT rather than a method:
a post that uploads nothing and points at work published elsewhere. Most of what
follows pins the four consequences the spec named before it was built, because
each of them is a way the feature could look finished and be wrong.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from posting import tg_linked


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE publications (
        pub_id INTEGER PRIMARY KEY AUTOINCREMENT, content_type TEXT, story_name TEXT,
        chapter_index INTEGER, platform TEXT, account_id INTEGER,
        variant_key TEXT NOT NULL DEFAULT '', external_id TEXT, external_url TEXT,
        status TEXT)""")
    return c


def _pub(c, platform, url, status="posted", story="Sample Story", ci=0,
         content_type="story"):
    c.execute("INSERT INTO publications (content_type, story_name, chapter_index,"
              " platform, account_id, external_url, status) VALUES (?,?,?,?,1,?,?)",
              (content_type, story, ci, platform, url, status))


class TestLinkedModeReadsWhereTheWorkActuallyIs:
    """Consequence 2: the data source is `publications`, not an upload package."""

    def test_it_finds_live_publications(self, conn):
        _pub(conn, "fa", "https://example.com/fa/1")
        _pub(conn, "ws", "https://example.com/ws/2")
        got = tg_linked.find_live_links(conn, story_name="Sample Story")
        assert ("fa", "https://example.com/fa/1") in got
        assert len(got) == 2

    def test_a_row_that_is_not_posted_is_not_a_link(self, conn):
        _pub(conn, "fa", "https://example.com/fa/1", status="failed")
        assert tg_linked.find_live_links(conn, story_name="Sample Story") == []

    def test_a_posted_row_with_no_url_cannot_be_linked_to(self, conn):
        """It may well be live; we simply cannot point at it."""
        _pub(conn, "fa", "")
        assert tg_linked.find_live_links(conn, story_name="Sample Story") == []

    def test_telegram_is_excluded_so_it_cannot_link_to_itself(self, conn):
        _pub(conn, "tg", "https://t.me/example/5")
        _pub(conn, "fa", "https://example.com/fa/1")
        got = tg_linked.find_live_links(conn, story_name="Sample Story")
        assert [p for p, _ in got] == ["fa"]

    def test_a_chapter_filter_is_respected(self, conn):
        _pub(conn, "fa", "https://example.com/fa/1", ci=1)
        _pub(conn, "fa", "https://example.com/fa/2", ci=2)
        got = tg_linked.find_live_links(conn, story_name="Sample Story", chapter_index=2)
        assert got == [("fa", "https://example.com/fa/2")]


class TestNoLinkYetIsNotAFailure:
    """Consequence 3. Reporting 'not yet' as an error teaches people to ignore
    errors, which is how the real ones get missed."""

    def test_nothing_published_yet_reports_not_yet(self, conn):
        r = _run(tg_linked.announce_existing(conn, story_name="Sample Story"))
        assert r["status"] == "not_yet"
        assert "error" not in r

    def test_it_says_what_to_do_about_it(self, conn):
        r = _run(tg_linked.announce_existing(conn, story_name="Sample Story"))
        assert "not live on any" in r["message"]

    def test_it_sends_nothing(self, conn, monkeypatch):
        """The check must come BEFORE any client is built, or a misconfigured
        Telegram would turn 'not yet' into a credentials error."""
        def _boom(*a, **k):
            raise AssertionError("no client should be constructed")
        import clients.tg.client as tgc
        monkeypatch.setattr(tgc, "TgClient", _boom)
        assert _run(tg_linked.announce_existing(
            conn, story_name="Sample Story"))["status"] == "not_yet"


class TestAnAnnouncementIsNeverRecordedAsAPublication:
    """Consequence 4, and the one that would quietly corrupt data rather than
    visibly break. A publications row would assert Telegram HOLDS the work."""

    def test_a_dry_run_writes_nothing(self, conn):
        _pub(conn, "fa", "https://example.com/fa/1")
        before = conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
        r = _run(tg_linked.announce_existing(conn, story_name="Sample Story",
                                             dry_run=True))
        assert r["status"] == "preview"
        assert conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == before

    def test_the_module_says_so_in_writing(self):
        doc = tg_linked.__doc__ or ""
        assert "never be recorded as a publication" in doc

    def test_nothing_in_the_module_writes_publications(self):
        src = open("posting/tg_linked.py", encoding="utf-8").read()
        assert "upsert_publication" not in src
        assert "INSERT INTO publications" not in src

    def test_it_is_recorded_as_an_announcement_not_a_post(self):
        """tg_submissions still gets a row -- reactions need something to attach to --
        but its content_type distinguishes it from a direct post carrying the art."""
        src = open("posting/tg_linked.py", encoding="utf-8").read()
        assert 'content_type="announcement"' in src


class TestTheAnnouncementText:

    def test_it_carries_the_links(self):
        text = tg_linked.build_announcement(
            title="Sample Story", blurb="A blurb.",
            links=[("fa", "https://example.com/fa/1")])
        assert "https://example.com/fa/1" in text
        assert "Sample Story" in text and "A blurb." in text

    def test_link_order_follows_the_pieces_own_preference(self):
        """Reuses announce.resolve_links rather than reimplementing ordering, so the
        announcement and the direct poster can never disagree about it."""
        text = tg_linked.build_announcement(
            title="T", blurb="", link_mode="pick", link_platforms=["ws"],
            links=[("fa", "https://example.com/fa/1"),
                   ("ws", "https://example.com/ws/2")])
        assert "ws/2" in text and "fa/1" not in text

    def test_tags_can_be_left_off(self):
        text = tg_linked.build_announcement(
            title="T", blurb="", links=[("fa", "https://e.example/1")],
            tags=["one"], with_tags=False)
        assert "#one" not in text

    def test_a_dry_run_returns_the_exact_text_that_would_be_sent(self, conn):
        _pub(conn, "fa", "https://example.com/fa/1")
        r = _run(tg_linked.announce_existing(conn, story_name="Sample Story",
                                             title="Sample Story", dry_run=True))
        assert "https://example.com/fa/1" in r["text"]


class TestChannelPolls:
    """Phase 4 item 7. Every limit is checked before the request: a poll is visible
    to every subscriber the instant it lands, so a malformed one should not be
    discovered by posting it."""

    @pytest.fixture
    def client(self):
        from clients.tg.client import TgClient
        return TgClient(bot_token="123:abc", channel="@example")

    def test_a_question_is_required(self, client):
        with pytest.raises(ValueError, match="needs a question"):
            _run(client.create_poll("", ["a", "b"]))

    def test_one_option_is_not_a_poll(self, client):
        with pytest.raises(ValueError, match="between 2 and 10"):
            _run(client.create_poll("Q?", ["only"]))

    def test_eleven_options_is_too_many(self, client):
        with pytest.raises(ValueError, match="between 2 and 10"):
            _run(client.create_poll("Q?", [str(i) for i in range(11)]))

    def test_blank_options_do_not_pad_the_count(self, client):
        """Two real options and three blanks is a two-option poll, not a five."""
        with pytest.raises(ValueError, match="between 2 and 10"):
            _run(client.create_poll("Q?", ["a", "", "   ", ""]))

    def test_an_over_long_question_is_refused(self, client):
        with pytest.raises(ValueError, match="Telegram allows 300"):
            _run(client.create_poll("x" * 301, ["a", "b"]))

    def test_an_over_long_option_is_refused(self, client):
        with pytest.raises(ValueError, match="over 100 characters"):
            _run(client.create_poll("Q?", ["a", "x" * 101]))

    def test_a_quiz_answer_must_be_one_of_the_options(self, client):
        with pytest.raises(ValueError, match="not one of the 2 options"):
            _run(client.create_poll("Q?", ["a", "b"], quiz_answer=5))

    def test_a_quiz_cannot_allow_multiple_answers(self, client):
        """The API refuses it; saying which combination is wrong beats a bare 400."""
        with pytest.raises(ValueError, match="cannot allow"):
            _run(client.create_poll("Q?", ["a", "b"], quiz_answer=0, multiple=True))

    def test_a_valid_quiz_answer_is_accepted_by_validation(self, client, monkeypatch):
        """Reaching the HTTP layer is proof the guards passed."""
        sent = {}

        class _Resp:
            def json(self):
                return {"ok": True, "result": {"message_id": 7}}

        class _C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, data=None, **k):
                sent["url"], sent["data"] = url, data
                return _Resp()

        import clients.tg.client as tgc
        monkeypatch.setattr(tgc.httpx, "AsyncClient", lambda **k: _C())
        r = _run(client.create_poll("Q?", ["a", "b"], quiz_answer=1,
                                    explanation="because"))
        assert r["id"] == "7"
        assert sent["url"].endswith("/sendPoll")
        assert sent["data"]["type"] == "quiz"
        assert sent["data"]["correct_option_id"] == "1"
        assert sent["data"]["options"] == '["a", "b"]'


class TestTheChannelDigestReusesTheEmailsNumbers:
    """Phase 4 item 8 is 'routing only'. A second implementation of the week's
    figures that drifted from the email's would be worse than no channel digest."""

    def test_it_calls_the_same_builder(self):
        import inspect
        from polling import email_digest
        src = inspect.getsource(email_digest.send_weekly_digest_to_channel)
        assert "build_weekly_digest_data" in src
        assert "render_weekly_digest_text" in src

    def test_it_is_off_unless_asked_for(self):
        import inspect
        from polling import email_digest
        src = inspect.getsource(email_digest.send_weekly_digest_to_channel)
        assert "tg_channel_digest_enabled" in src

    def test_it_truncates_on_a_line_boundary_with_a_marker(self):
        """Telegram slices at 4096 silently; a digest that stops mid-number reads
        as a bug rather than as a long week."""
        import inspect
        from polling import email_digest
        src = inspect.getsource(email_digest.send_weekly_digest_to_channel)
        assert "MESSAGE_LIMIT" in src
        assert '(truncated)' in src
        assert 'rsplit("\\n", 1)' in src

    def test_the_route_defaults_to_a_dry_run(self):
        """It is the one of the three most likely to be fired by accident."""
        src = open("routes/tg_api.py", encoding="utf-8").read()
        i = src.index("def tg_channel_digest")
        assert 'body.get("dry_run", True)' in src[i:i + 1200]


class TestTheRoutesDoNotCollideWithPolling:

    def test_a_telegram_poll_is_not_a_polling_cycle(self):
        """In this router '/poll/' already means a polling cycle. Sharing the prefix
        would have made /poll/trigger ambiguous forever."""
        src = open("routes/tg_api.py", encoding="utf-8").read()
        assert '@tg_router.post("/channel/poll")' in src
        assert '@tg_router.post("/poll")' not in src

    def test_not_yet_is_not_an_http_error(self):
        src = open("routes/tg_api.py", encoding="utf-8").read()
        i = src.index("def tg_channel_announce")
        block = src[i:i + 2000]
        assert 'result.get("status") == "error"' in block, \
            "only a real error raises; not_yet is a 200"


class TestThePostedPathEndToEnd:
    """The dry run proves the text. This proves what a real send leaves behind --
    which is where consequence 4 would actually be violated."""

    @pytest.fixture
    def db(self, conn):
        conn.executescript("""
            CREATE TABLE tg_submissions (
                submission_id TEXT PRIMARY KEY, account_id INTEGER NOT NULL DEFAULT 0,
                chat_id TEXT NOT NULL DEFAULT '', message_id INTEGER NOT NULL DEFAULT 0,
                title TEXT DEFAULT '', posted_at TEXT DEFAULT '', link TEXT DEFAULT '',
                content_type TEXT DEFAULT 'artwork', reactions_count INTEGER DEFAULT 0,
                reactions_json TEXT DEFAULT '', reactions_at TEXT,
                updated_at TEXT DEFAULT (datetime('now')))""")
        return conn

    @pytest.fixture
    def sent(self, monkeypatch):
        """A TgClient that records instead of calling Telegram."""
        box = {}

        class _Fake:
            channel = "@example"
            last_error = ""

            def __init__(self, bot_token="", channel=""):
                box["token"], box["channel"] = bot_token, channel

            async def create_post(self, text, image_paths=None, **kw):
                box["text"], box["images"], box["kw"] = text, image_paths, kw
                return {"id": "42", "url": "https://t.me/example/42"}

        import clients.tg.client as tgc
        monkeypatch.setattr(tgc, "TgClient", _Fake)

        import posting.platforms.telegram as tgp
        monkeypatch.setattr(tgp.TelegramPoster, "_resolve_creds",
                            lambda self, code, s: {"tg_bot_token": "123:abc",
                                                   "tg_channel": "@example"})
        return box

    def test_it_posts_and_reports_the_message(self, db, sent):
        _pub(db, "fa", "https://example.com/fa/1")
        r = _run(tg_linked.announce_existing(db, story_name="Sample Story",
                                             title="Sample Story"))
        assert r["status"] == "posted"
        assert r["message_id"] == "42"
        assert "https://example.com/fa/1" in sent["text"]

    def test_it_writes_no_publication_row(self, db, sent):
        """The whole point. Telegram announced the work; it does not hold it."""
        _pub(db, "fa", "https://example.com/fa/1")
        before = db.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
        _run(tg_linked.announce_existing(db, story_name="Sample Story"))
        assert db.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == before
        assert db.execute("SELECT COUNT(*) FROM publications WHERE platform='tg'"
                          ).fetchone()[0] == 0

    def test_it_records_the_message_as_an_announcement(self, db, sent):
        _pub(db, "fa", "https://example.com/fa/1")
        _run(tg_linked.announce_existing(db, story_name="Sample Story"))
        row = db.execute("SELECT content_type, message_id, link FROM tg_submissions"
                         ).fetchone()
        assert row["content_type"] == "announcement"
        assert row["message_id"] == 42

    def test_it_uploads_nothing_when_there_is_no_cover(self, db, sent):
        """A linked post references; it does not upload the artefact."""
        _pub(db, "fa", "https://example.com/fa/1")
        _run(tg_linked.announce_existing(db, story_name="Sample Story"))
        assert sent["images"] == []

    def test_a_refusal_is_reported_without_a_record(self, db, monkeypatch):
        class _Fake:
            channel = "@example"
            last_error = "not enough rights"

            def __init__(self, **k):
                pass

            async def create_post(self, *a, **k):
                return None

        import clients.tg.client as tgc
        monkeypatch.setattr(tgc, "TgClient", _Fake)
        import posting.platforms.telegram as tgp
        monkeypatch.setattr(tgp.TelegramPoster, "_resolve_creds",
                            lambda self, code, s: {"tg_bot_token": "1", "tg_channel": "@e"})
        _pub(db, "fa", "https://example.com/fa/1")
        r = _run(tg_linked.announce_existing(db, story_name="Sample Story"))
        assert r["status"] == "error" and "not enough rights" in r["error"]
        assert db.execute("SELECT COUNT(*) FROM tg_submissions").fetchone()[0] == 0

    def test_bookkeeping_failure_never_turns_a_sent_post_into_a_failure(self, conn, sent):
        """No tg_submissions table here at all -- the post still went out."""
        _pub(conn, "fa", "https://example.com/fa/1")
        r = _run(tg_linked.announce_existing(conn, story_name="Sample Story"))
        assert r["status"] == "posted"


class TestTheAnnounceRouteCannotReadArbitraryFiles:
    """A 4.34.4 security review found this as a High, in code written the same day.

    The first draft took `image_path` from the request body and opened it, uploading
    the bytes as a photo. On a server instance that is a remote file-read gadget
    WITH EGRESS: the file leaves the machine and lands in a channel real subscribers
    can read. `routes/artwork_api.py` already treats the same shape as a gadget and
    gates it to desktop-only.

    ⚠ The fix deletes the parameter rather than gating it, because linked mode is
    DEFINED as not uploading the artefact — the parameter contradicted the contract
    it was written under. These tests pin the absence, not a guard, since there is
    now no code path from caller input to `open()`.
    """

    def test_announce_takes_no_path_from_its_caller(self):
        import inspect
        sig = inspect.signature(tg_linked.announce_existing)
        assert "image_path" not in sig.parameters, \
            "a caller-supplied path is a file-read gadget with egress"

    def test_the_route_does_not_read_one_from_the_body(self):
        src = open("routes/tg_api.py", encoding="utf-8").read()
        i = src.index("def tg_channel_announce")
        block = src[i:i + 2500]
        assert 'body.get("image_path")' not in block

    def test_nothing_in_the_module_opens_a_file(self):
        src = open("posting/tg_linked.py", encoding="utf-8").read()
        code = src[src.index("def find_live_links"):]
        assert "os.path.isfile" not in code
        assert "open(" not in code

    def test_it_sends_an_empty_image_list_explicitly(self):
        src = open("posting/tg_linked.py", encoding="utf-8").read()
        assert "image_paths=[]" in src

    def test_the_reason_is_recorded_where_the_code_is(self):
        src = open("posting/tg_linked.py", encoding="utf-8").read()
        assert "gadget" in src, "the next person needs to know why this is absent"


class TestEveryBroadcastRouteCanBePreviewed:
    """The module comment claims all three support dry_run. It was not true of the
    poll — and a comment asserting a safety property the code lacks is worse than no
    comment, because the next author trusts it instead of reading."""

    @pytest.fixture
    def client(self):
        from clients.tg.client import TgClient
        return TgClient(bot_token="123:abc", channel="@example")

    def test_a_dry_run_poll_validates_but_does_not_send(self, client, monkeypatch):
        def _boom(**k):
            raise AssertionError("dry run must not reach the network")
        import clients.tg.client as tgc
        monkeypatch.setattr(tgc.httpx, "AsyncClient", _boom)
        r = _run(client.create_poll("Which next?", ["A", "B"], dry_run=True))
        assert r["dry_run"] is True
        assert r["payload"]["question"] == "Which next?"

    def test_a_dry_run_still_refuses_an_invalid_poll(self, client):
        """Validation before the send is the whole point; skipping it on a preview
        would let a bad poll pass review and fail live."""
        with pytest.raises(ValueError):
            _run(client.create_poll("Q?", ["only one"], dry_run=True))

    def test_the_poll_route_passes_it_through(self):
        src = open("routes/tg_api.py", encoding="utf-8").read()
        i = src.index("def tg_channel_poll")
        assert 'dry_run=bool(body.get("dry_run", False))' in src[i:i + 1600]

    def test_the_digest_does_not_bypass_the_operators_toggle(self):
        """force=True skips tg_channel_digest_enabled AND skips writing the
        last-sent timestamp, so there is no cadence record to dedupe against."""
        src = open("routes/tg_api.py", encoding="utf-8").read()
        i = src.index("def tg_channel_digest")
        assert 'body.get("force", False)' in src[i:i + 900]
