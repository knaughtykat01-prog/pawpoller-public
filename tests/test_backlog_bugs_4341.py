"""The open bug rows, squashed (4.34.1).

FADATES, DAID, PUBDESCWIPE and PWRESET. Two of the seven turned out to be already
fixed in code with only historical rows left to repair, and one (FACOOKIE) is the
operator's to do — cookies are pasted by the owner and never pass through here.
"""
from __future__ import annotations

import inspect
import sqlite3

import pytest

from database import posting_queries
from database.platform_metrics import normalize_posted


class TestFADatesSortAsTime:
    """FADATES. FA renders the posted date as prose in the popup_date title attribute
    and it was stored verbatim, alongside ISO rows written later. `posted_at` is TEXT, so
    a text sort put every month name before every "2026-…" — MIN/MAX and every
    date-windowed query over FA was wrong wherever a prose row was involved.

    `normalize_posted` already knew all of these shapes; it was only ever applied on the
    way OUT, so raw SQL never saw it.
    """

    def test_fa_prose_is_normalised_on_the_way_in(self):
        from database.fa_queries import _sortable_date
        assert _sortable_date("August 7, 2019 11:57:56 PM") == "2019-08-07 23:57:56"

    def test_iso_normalises_to_the_same_shape(self):
        """Both shapes have to land on ONE form or the column still mixes."""
        from database.fa_queries import _sortable_date
        assert _sortable_date("2026-03-10T05:06:35Z") == "2026-03-10 05:06:35"

    def test_the_two_shapes_now_order_correctly(self):
        from database.fa_queries import _sortable_date
        old = _sortable_date("August 7, 2019 11:57:56 PM")
        new = _sortable_date("2026-03-10T05:06:35Z")
        assert old < new, "a 2019 piece must sort before a 2026 one"
        # and the bug, for contrast: raw, it did not
        assert not ("August 7, 2019 11:57:56 PM" < "2026-03-10T05:06:35Z")

    def test_an_unreadable_date_keeps_its_text(self):
        """Dropping it would destroy the only record of when something was posted —
        a date nobody can sort still beats no date at all."""
        from database.fa_queries import _sortable_date
        assert _sortable_date("sometime last winter") == "sometime last winter"

    def test_empty_stays_empty(self):
        from database.fa_queries import _sortable_date
        assert _sortable_date(None) == "" and _sortable_date("") == ""

    def test_the_upsert_uses_it(self):
        from database import fa_queries
        src = inspect.getsource(fa_queries.upsert_fa_submission)
        assert "_sortable_date(sub.get(\"posted_at\"))" in src

    def test_the_backfill_never_blanks_a_row(self):
        from database import db as db_module
        src = inspect.getsource(db_module)
        i = src.index("FADATES")
        block = src[i:i + 1600]
        assert "if norm and norm != raw:" in block, \
            "an unparseable value must be left alone, not written as ''"


class TestDeviantArtIdsJoin:
    """DAID. DA has two ids for one deviation: the API GUID and the integer in the public
    URL. Everything in PawPoller speaks the integer. Posting used to store the GUID, so a
    post made THROUGH PawPoller joined to nothing it had ever polled — DA views and faves
    never pooled into the work's totals.

    The posting path was fixed to derive the integer from the returned URL; the rows
    written before that are still GUIDs, and are repairable with no API call because the
    URL in the same row carries the integer.
    """

    def test_the_poster_derives_the_integer(self):
        from posting.platforms import deviantart
        src = inspect.getsource(deviantart)
        assert "_int_id_from_url(url)" in src

    def test_the_url_is_where_the_integer_comes_from(self):
        from clients.da.client import _int_id_from_url
        assert _int_id_from_url(
            "https://www.deviantart.com/user/art/Some-Title-1351251437") == 1351251437
        assert _int_id_from_url("https://www.deviantart.com/user/art/No-Id") is None

    def test_the_backfill_only_touches_guid_rows(self):
        from database import db as db_module
        src = inspect.getsource(db_module)
        i = src.index("4.34.1, DAID")
        block = src[i:i + 1800]
        assert "platform = 'da' AND external_id LIKE '%-%'" in block
        assert "if num is None:" in block, "a row with no URL must be left alone"


class TestAPartialWriteNoLongerErasesTheRecord:
    """PUBDESCWIPE, mechanism found.

    `upsert_publication`'s UPDATE wrote EVERY column unconditionally, so a caller that
    passed only an id and a title — `posting/sync.py`'s "Verify posted" does exactly
    that — silently erased `description_used`, `tags_used` and `rating_used`.

    Those columns are the only local record of what was actually SENT to a platform, and
    a "what did we post?" audit reads them. The observed symptom was a publication read
    twice minutes apart with the description going from 129 chars to empty.
    """

    @pytest.fixture
    def conn(self, tmp_path):
        c = sqlite3.connect(tmp_path / "p.db")
        c.row_factory = sqlite3.Row
        c.execute("""CREATE TABLE publications (
            pub_id INTEGER PRIMARY KEY AUTOINCREMENT, content_type TEXT DEFAULT 'story',
            story_name TEXT, chapter_index INTEGER DEFAULT 0, chapter_title TEXT DEFAULT '',
            platform TEXT, account_id INTEGER DEFAULT 0, variant_key TEXT NOT NULL DEFAULT '',
            external_id TEXT DEFAULT '', external_url TEXT DEFAULT '',
            format_file TEXT DEFAULT '', file_hash TEXT DEFAULT '',
            tags_used TEXT DEFAULT '[]', title_used TEXT DEFAULT '',
            description_used TEXT DEFAULT '', rating_used TEXT DEFAULT '',
            status TEXT DEFAULT 'draft', first_posted_at TEXT, last_updated_at TEXT,
            update_count INTEGER DEFAULT 0, last_error TEXT,
            created_at TEXT, word_count INTEGER DEFAULT 0,
            UNIQUE(content_type, story_name, chapter_index, platform, account_id, variant_key))""")
        c.commit()
        return c

    def _row(self, conn):
        return dict(conn.execute("SELECT * FROM publications").fetchone())

    def test_a_partial_update_keeps_what_it_did_not_supply(self, conn):
        posting_queries.upsert_publication(
            conn, "Piece", 0, "da", account_id=1, content_type="artwork",
            external_id="GUID-1", description_used="the real description we sent",
            tags_used=["a", "b"], rating_used="mature", title_used="Piece")
        # what "Verify posted" does: an id, a url, a title, a status — nothing else
        posting_queries.upsert_publication(
            conn, "Piece", 0, "da", account_id=1, content_type="artwork",
            external_id="1371636392", external_url="https://x/1371636392",
            title_used="Piece", status="posted")
        r = self._row(conn)
        assert r["external_id"] == "1371636392"        # the id DID move — that is the point
        assert r["description_used"] == "the real description we sent"
        assert r["tags_used"] == '["a", "b"]'
        assert r["rating_used"] == "mature"

    def test_an_explicit_empty_still_clears(self, conn):
        """None means "not supplied"; "" has to keep meaning "deliberately empty"."""
        posting_queries.upsert_publication(
            conn, "Piece", 0, "da", account_id=1, content_type="artwork",
            description_used="something")
        posting_queries.upsert_publication(
            conn, "Piece", 0, "da", account_id=1, content_type="artwork",
            description_used="")
        assert self._row(conn)["description_used"] == ""

    def test_a_new_row_still_gets_empty_defaults(self, conn):
        """A brand-new row has nothing to preserve, so None must not reach the INSERT."""
        posting_queries.upsert_publication(
            conn, "Fresh", 0, "fa", account_id=1, content_type="artwork")
        r = self._row(conn)
        assert r["description_used"] == "" and r["tags_used"] == "[]"
        assert r["word_count"] == 0

    def test_the_update_count_still_climbs(self, conn):
        posting_queries.upsert_publication(conn, "P", 0, "fa", account_id=1)
        posting_queries.upsert_publication(conn, "P", 0, "fa", account_id=1)
        assert self._row(conn)["update_count"] == 1


class TestThereIsAWayBackFromAForgottenPassword:
    """PWRESET. A packaged install had none: the old guides promised a function that does
    not exist, and a .env value that is ignored once a hash is set."""

    def test_the_server_takes_the_flag(self):
        import server
        src = inspect.getsource(server.main)
        assert '"--reset-password"' in src
        assert "_reset_password()" in src

    def test_the_desktop_takes_it_too(self):
        src = open("main.py", encoding="utf-8").read()
        assert '"--reset-password" in sys.argv' in src

    def test_the_desktop_checks_before_the_update_gate(self):
        """So it still works on an install whose updates are failing."""
        src = open("main.py", encoding="utf-8").read()
        i = src.index("def main():")
        block = src[i:i + 1400]
        assert block.index("--reset-password") < block.index("update_gate")

    def test_the_password_is_never_an_argument(self):
        """An argument lands in shell history and in the process list, where any other
        local user can read it."""
        import server
        src = inspect.getsource(server._reset_password)
        assert "getpass" in src
        assert "add_argument" not in src

    def test_it_confirms_and_sets_a_floor(self):
        import server
        src = inspect.getsource(server._reset_password)
        assert "Type it again" in src
        assert "len(pw) < 8" in src

    def test_it_writes_through_the_vault_safe_path(self):
        """Not a raw settings.json poke — save_settings keeps the vault consistent."""
        import server
        src = inspect.getsource(server._reset_password)
        assert "config.save_settings" in src and "config.hash_password" in src


class TestWhatsNewReadsAsDotPoints:
    """The popup summary was a wall of paragraphs. Two causes, one in each layer.

    `_summarize` did `" ".join()` on the blockquote, so a summary written as dot points
    arrived as one run-on line *however it was authored* — the format could not be fixed
    by writing it differently. The popup already renders bullets (`_mdLite`), so keeping
    the line structure was the whole fix on that side; the rest is the convention, now in
    CLAUDE.md: one short line per change, no build-up, no cause.
    """

    # NOTE: the two checks that read the real CHANGELOG.md live in test_public_copy.py.
    # It is excluded from the public copy by name, and this file is not — a shipped test
    # that reads a file the public copy does not contain cannot run in a public checkout,
    # which `make_public.py` refuses outright (it caught exactly that here).

    def test_the_popup_renders_bullets(self):
        """`_mdLite` is what turns "- x" into a list; without it the dashes are literal."""
        src = open("frontend/js/app.js", encoding="utf-8").read()
        i = src.index("_showWhatsNewModal(data) {")      # the definition, not a call site
        assert "this._mdLite(e.summary" in src[i:i + 900]
        assert "html += '<ul>'" in src


class TestARotatedTokenReachesTheOtherMachine:
    """DATOKENROTSYNC. The same defect class as the 2026-08-19 incident — a rotated
    secret not written back to where it was read from — but across MACHINES, so writing
    the right local key does not reach it.

    Refresh tokens are single-use and DA's `requires_mode` is "any", so either install
    may post. Settings auto-sync is pull-only. So a desktop post rotated the token
    locally, the server never learned, and the server's copy was spent — and the next
    pull then overwrote the desktop's fresh one with the server's dead one, killing the
    account on both machines from a single post.
    """

    def test_saving_a_credential_pushes_it(self):
        from posting.platforms.base import PlatformPoster
        src = inspect.getsource(PlatformPoster._save_creds)
        assert "_push_rotated_creds" in src
        assert src.index("config.save_settings") < src.index("_push_rotated_creds"), \
            "the local write must land first — the push is best-effort on top of it"

    def test_the_push_cannot_break_a_post(self):
        """The credential is already saved locally by the time this runs. A server that
        is down must not fail, stall or roll back the post that just succeeded."""
        from posting.platforms.base import PlatformPoster
        src = inspect.getsource(PlatformPoster._push_rotated_creds)
        assert "except Exception" in src
        assert "daemon=True" in src, "synchronous HTTP on an async posting path"

    def test_it_self_gates_when_unpaired(self):
        """An unpaired install and the server itself have no sync target, so this has to
        be a no-op there rather than an error every time a token rotates."""
        import auto_sync
        assert auto_sync.push_now() == -1        # nothing paired in the test environment

    def test_it_does_not_log_the_credential(self):
        from posting.platforms.base import PlatformPoster
        src = inspect.getsource(PlatformPoster._push_rotated_creds)
        for line in src.splitlines():
            if "logger." in line:
                assert "values" not in line and "token" not in line.split("#")[0].lower() \
                    or "rotated credential" in line, "a token must never reach a log line"


class TestTheServiceWorkerCannotGoStale:
    """SWCACHE. `dashboard.py` serves /sw.js with `Cache-Control: no-cache` and
    **Cloudflare overrides it** — the public response carries `max-age=14400`. A service
    worker cached for four hours means a phone keeps serving the OLD app after a deploy,
    which is what happened after 3.26–3.32.

    A Cloudflare cache rule is still worth adding, but the fix should not depend on a
    dashboard setting nobody remembers to check.
    """

    def test_the_registration_bypasses_the_http_cache(self):
        src = open("frontend/js/pwa.js", encoding="utf-8").read()
        assert "updateViaCache: 'none'" in src, \
            "the spec's answer to an edge cache holding a stale worker"

    def test_the_worker_url_carries_the_app_version(self):
        """Belt and braces: even a browser ignoring the hint sees a new URL per release."""
        src = open("frontend/js/pwa.js", encoding="utf-8").read()
        assert "'/sw.js' + (_appVer" in src

    def test_the_version_is_read_where_currentScript_is_valid(self):
        """Inside the load handler `document.currentScript` is null, so reading it there
        would silently yield no version and quietly drop half the fix."""
        src = open("frontend/js/pwa.js", encoding="utf-8").read()
        assert src.index("document.currentScript") < src.index("addEventListener('load'")

    def test_the_server_still_sends_no_cache(self):
        """The origin header stays correct even though Cloudflare overrides it — an
        install not behind Cloudflare relies on it."""
        src = open("dashboard.py", encoding="utf-8").read()
        i = src.index('@app.get("/sw.js"')
        assert "no-cache" in src[i:i + 700]


class TestASlowMachineCanStillStart:
    """LOGSWEEP (a). A tester's log had the desktop launcher give up on its own server
    TWICE -- "SERVER DID NOT START within 15s after 13 attempts!" -- and then start fine.

    The attempt count is the tell. A 200ms sleep between tries reads as ~75 attempts in
    15 seconds; it got thirteen. A refused connection returns instantly, but where the
    port is not yet bound the connect BLOCKS, so each try cost most of its 1.0s timeout.
    The wait was a third of what the code looked like it was.

    Three things follow: attempts have to be cheap for the deadline to mean anything, the
    deadline has to cover a cold start (schema files, the migration chain, the vault), and
    "slow" must not be treated as "dead" -- the exit killed a working app.
    """

    def _wait_block(self) -> str:
        src = open("main.py", encoding="utf-8").read()
        i = src.index("Waiting for server at")
        # rindex: the comment above the loop quotes the log line this anchors on.
        return src[i:src.rindex("SERVER DID NOT START") + 400]

    def test_an_attempt_is_cheap_enough_that_the_deadline_holds(self):
        block = self._wait_block()
        assert "timeout=1.0" not in block, "a 1s blocking connect starves the retry count"
        assert "timeout=0.35" in block

    def test_the_budget_covers_a_cold_start(self):
        block = self._wait_block()
        assert "started + 60" in block, "15s did not cover a cold start on a slow disk"

    def test_a_dead_thread_exits_immediately_rather_than_waiting_out_the_clock(self):
        """The real failure should be reported when it happens, not a minute later."""
        block = self._wait_block()
        i = block.index("except OSError")
        assert "not server_thread.is_alive()" in block[i:], \
            "a thread that died is the actual failure and should not wait for the deadline"
        # inside the loop, not after the deadline has already passed
        assert block.index("not server_thread.is_alive()") < block.index("if not ready:")

    def test_being_slow_is_no_longer_fatal(self):
        """The exit is reached only by exhausting the deadline, never by one slow attempt."""
        block = self._wait_block()
        assert "if not ready:" in block

    def test_the_elapsed_time_is_measured_not_recomputed(self):
        """It used to derive elapsed from the deadline and a repeated literal 15, so
        changing the budget in one place silently made the log lie."""
        block = self._wait_block()
        assert "(deadline - 15)" not in block
        assert "time.time() - started" in block


class TestInstagramNoLongerStoresAHandleAsAnId:
    """LOGSWEEP (b). The same log had `IG: auth error (400) ... Object with ID
    '<username>' does not exist` on 2026-09-03: a handle sat in the box that wants the
    numeric Graph user id, went into the URL path, and 400d every poll -- reported as an
    auth error against a token that was fine.

    Already fixed, one day after that line, by 4.3.6. Kept here because the backlog row
    proposed REJECTING a handle and that would have been the worse fix: the token already
    knows the numeric id, so the right move is to ignore the handle, resolve the id from
    /me, keep the handle in the username field, and say so.
    """

    def test_a_handle_never_becomes_an_id(self):
        from clients.ig.client import IgClient
        from clients.thr.client import ThrClient
        for cls in (IgClient, ThrClient):
            c = cls(access_token="tok", user_id="sample_handle")
            assert c.user_id == "", f"{cls.__name__} would put a handle in the URL path"
            assert c.ignored_user_id == "sample_handle", "and it must not be thrown away"

    def test_a_real_id_is_kept(self):
        from clients.ig.client import IgClient
        assert IgClient(access_token="tok", user_id="17841400000000000").user_id \
            == "17841400000000000"

    def test_the_connect_route_says_what_it_did(self):
        """Silently storing something other than what was typed is how this stayed
        invisible for a month."""
        src = open("routes/ig_api.py", encoding="utf-8").read()
        i = src.index("def ig_connect")
        assert "client.ignored_user_id" in src[i:i + 1800]
