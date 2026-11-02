"""Toyhouse platform (spec 062): parsers, the client's safety rails, the poster's rules, the poll's stand-down.

The page snippets below copy the markup Toyhouse served on 29 Oct 2026 (research.md) with made-up names,
ids and no tokens. Nothing here reaches the network: the client gets an httpx MockTransport.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

import config
from clients.th import client as thc
from clients.th.client import THChallenged, THClient
from polling import fa_standdown
from posting.platforms import toyhouse as thp
from posting.platforms.base import StoryUploadPackage

HEADER = ('<nav class="navbar navbar-toggleable-sm navbar-inverse header" role="navigation" id="header">'
          '<li class="nav-item"><a class="nav-link" href="https://toyhou.se/SecondFur">\n'
          '  <span class="fa fa-home"></span> Profile</a></li></nav>')


def art_card(iid, char_id, slug, name, faves, comments):
    return f'''<div class="gallery-thumb thumb"><div class="thumb-image">
<a href="https://f2.toyhou.se/file/f2-toyhou-se/images/{iid}_x.png" class="magnific-item th img-thumbnail" data-id="{iid}">
<img src="https://f2.toyhou.se/file/f2-toyhou-se/thumbnails/{iid}_x.png"></a></div>
<div class="magnific-caption hide"><div class="image-controls pull-left">
<a href="#" class="btn btn-sm btn-default mr-1 " data-id="image-{iid}" data-url="https://toyhou.se/~images/{iid}/favorite" th-favorite="1">
 <i class="fa fa-star fa-regular sidebar-icon"></i> <span class="th-favorite-count">{faves}</span> </a></div>
<div class="image-credits "> <h2> Credits </h2> <div class="mb-1">28 Oct 2026, 6:33:22 pm</div></div>
<div class="image-characters "><div class="mb-1">
<a href="/{char_id}.{slug}" class="btn btn-sm btn-primary character-name-badge">{name}</a></div></div>
<div class="image-comments-content" image-comments-count="{comments}" image-comments-lazy="https://toyhou.se/{char_id}.-/{iid}/comments"></div>
</div></div>'''


def char_card(cid, slug, name, faves, images, comments):
    return f'''<div class="text-center gallery-thumb character-thumb gallery-item"><div class="thumb-image">
<a href="/{cid}.{slug}" class="img-thumbnail"><img src="x"></a></div><div class="thumb-caption">
<span class="thumb-character-name"><a href="/{cid}.{slug}" class="btn btn-sm btn-primary character-name-badge"><i class="fa fa-unlock-alt"></i>&nbsp;{name}</a></span>
<a data-id="character-{cid}" href="#"><span class="thumb-character-stat favorites" title="Favorites"><i class="fi-star"></i><span class="th-favorite-count">{faves}</span></span></a>
<a class="thumb-character-stat images" href="https://toyhou.se/{cid}.{slug}/gallery" title="Images"> <i class="fi-photo"></i>{images} </a>
<a class="thumb-character-stat comments" href="https://toyhou.se/{cid}.{slug}/comments" title="Comments"> <i class="fi-comment"></i>{comments} </a>
</div></div>'''


def comment(cid, author, body_html):
    return f'''<a id="comment-{cid}"></a><div class="row forum-post-post"><div class=" comment-header-{cid}">
<span class=" forum-post-user-badge"><a href="https://toyhou.se/{author}" class="btn btn-sm btn-default user-name-badge "><i class="fi-torso user-name-icon"></i>{author}</a></span></div>
<div class="card forum-post-content"><div class="card-block bg-faded user-content comment-body-{cid}">
{body_html}
</div><div class="card-block bg-faded forum-post-footer"><span class="forum-post-time small"><abbr class="tooltipster datetime" title="" data-original-title="29 Sep 2026, 4:17:07 pm">a month ago</abbr></span>
<a href="https://evil.example/~comments/{cid}" class="small forum-post-permalink"></a></div></div></div>'''


ART = HEADER + art_card("1001", "501", "sample-one", "Sample One", 3, 2) + art_card("1002", "502", "-", "Inkwolf &amp; Co", 0, 0)
CHARS = HEADER + char_card("501", "sample-one", "Sample One", 7, 4, 45) + char_card("502", "-", "Penwright", 0, 1, 0)
UPLOAD = HEADER + '<form><input type="hidden" name="_token" value="FAKE_TOKEN_FOR_TESTS"></form>'
CHALLENGE = "<html><title>Just a moment...</title></html>"


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(thc, "PAGE_PAUSE_S", 0)

    async def _quiet(*a, **k):
        return True
    monkeypatch.setattr("polling.telegram.send_telegram", _quiet)


def fake_client(routes: dict, seen: list | None = None) -> THClient:
    """A client whose HTTP goes to `routes` ({url: (status, body, headers)}); unknown URLs are 404."""
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(str(request.url))
        status, body, headers = routes.get(str(request.url), (404, "", {}))
        return httpx.Response(status, text=body, headers=headers)
    c = THClient(username="SecondFur", cookie="sess=abc")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return c


def run(coro):
    return asyncio.run(coro)


# ── parsers ─────────────────────────────────────────────────────────────────

def test_art_page_parse_builds_links_from_ids():
    imgs = thc.parse_art_page(ART)
    assert [i["submission_id"] for i in imgs] == ["1001", "1002"]
    a = imgs[0]
    assert (a["favorites_count"], a["comments_count"], a["character_ids"]) == (3, 2, ["501"])
    assert a["link"] == "https://toyhou.se/501.sample-one/1001"
    assert a["thumbnail_url"].startswith("https://f2.toyhou.se/")
    assert a["posted_at"] == "28 Oct 2026, 6:33:22 pm"
    assert imgs[1]["title"] == "Inkwolf & Co"


def test_characters_page_parse():
    chars = thc.parse_characters_page(CHARS)
    assert [(c["character_id"], c["name"], c["favorites_count"], c["images_count"], c["comments_count"])
            for c in chars] == [("501", "Sample One", 7, 4, 45), ("502", "Penwright", 0, 1, 0)]


def test_th_comment_parse_plain_text_and_built_link():
    """FR-SEC-004: the body is plain text, the permalink is built from the id (the page's own is ignored)."""
    html = comment("77", "ThirdFur", '<p>hi <b>there</b><script>alert(1)</script></p><p>line two</p>')
    [c] = thc.parse_comments(html)
    assert c["comment_id"] == "77" and c["author"] == "ThirdFur"
    assert "<" not in c["body"] and "hi there" in c["body"] and "line two" in c["body"]
    assert c["permalink"] == "https://toyhou.se/~comments/77"
    assert c["commented_at"] == "29 Sep 2026, 4:17:07 pm"


def test_th_whoami_ignores_link_outside_navbar():
    """FR-SEC-001: a Profile-looking link in user-written HTML further down the page doesn't count."""
    forged = ('<nav id="header"></nav><div class="user-content"><a class="nav-link" href="https://toyhou.se/SecondFur">'
              '<span class="fa fa-home"></span> Profile</a></div>')
    assert thc.logged_in_username(forged) == ""
    assert thc.logged_in_username(HEADER) == "SecondFur"


# ── client safety rails ─────────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["https://eviltoyhou.se/x", "http://toyhou.se/x", "https://toyhou.se.evil.com/",
                                 "https://toyhou.se:8443/", "https://f2.toyhou.se/x"])
def test_th_client_refuses_foreign_host(url):
    assert not THClient._host_ok(url)


def test_th_client_allows_its_two_hosts():
    assert THClient._host_ok("https://toyhou.se/x") and THClient._host_ok("https://upload.toyhou.se/~images/upload")


def test_th_redirect_off_site_not_followed():
    """FR-SEC-003: a redirect off Toyhouse is refused, and the off-site URL is never requested."""
    seen = []
    c = fake_client({"https://toyhou.se/SecondFur/art": (302, "", {"location": "https://evil.example/steal"})}, seen)
    with pytest.raises(ValueError):
        run(c._get_text("/SecondFur/art"))
    assert seen == ["https://toyhou.se/SecondFur/art"]


def test_th_cookie_jar_scoped_to_toyhouse():
    c = THClient(username="SecondFur", cookie="sess=abc; other=1")
    jar = c._http().cookies.jar
    assert {ck.domain for ck in jar} == {".toyhou.se"} and len(list(jar)) == 2   # one copy per cookie
    run(c.close())


def test_th_challenge_is_not_signed_out():
    """FR-SEC-008: the bot check raises THChallenged; it is never an expired sign-in."""
    c = fake_client({"https://toyhou.se/~images/upload": (403, CHALLENGE, {"cf-mitigated": "challenge"})})
    with pytest.raises(THChallenged):
        run(c.validate_session())


def test_th_validate_session_names_the_wrong_account():
    c = fake_client({"https://toyhou.se/~images/upload": (200, UPLOAD, {})})
    c.username = "Penwright"
    r = run(c.validate_session())
    assert r["logged_in"] and not r["ok"] and r["username"] == "SecondFur"


def test_th_no_curl_is_not_signed_out(monkeypatch):
    monkeypatch.setattr(thc, "AsyncCurlTransport", None)
    with pytest.raises(THChallenged):
        THClient(username="SecondFur", cookie="sess=abc")._http()


def test_th_listing_page_cap(monkeypatch):
    """FR-SEC-010: a site that always says "next page" is read at most MAX_PAGES times."""
    monkeypatch.setattr(thc, "MAX_PAGES", 3)
    seen = []
    c = fake_client({}, seen)

    async def _page(path):
        seen.append(path)
        n = len(seen)
        return art_card(str(2000 + n), "501", "s", "S", 0, 0) + f'href="https://toyhou.se/SecondFur/art?page={n + 1}"'
    c._get_text = _page
    assert len(run(c.get_all_images())) == 3 and len(seen) == 3


def test_th_errors_carry_no_cookie_or_token(caplog, tmp_path):
    """FR-SEC-005: a refused upload's error and the logs hold neither the cookie nor the form token."""
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG")
    c = fake_client({"https://toyhou.se/~images/upload": (200, UPLOAD, {}),
                     "https://upload.toyhou.se/~images/upload": (422, "boom FAKE_TOKEN_FOR_TESTS", {})})
    with caplog.at_level(logging.DEBUG):
        r = run(c.upload_image(file_path=str(img), caption="", character_ids=["501"], flags={}, credits=[]))
    assert not r["sent"]
    for text in (r["error"], caplog.text):
        assert "FAKE_TOKEN_FOR_TESTS" not in text and "sess=abc" not in text


# ── poster rules ────────────────────────────────────────────────────────────

def test_th_flags_floor():
    """FR-SEC-006: the rating and tags set a floor; the piece's options can only raise it."""
    assert thp.th_flags("adult", [], {"sexual": 0})["is_sexual"] == 2
    assert thp.th_flags("mature", [])["is_sexual"] == 1
    assert thp.th_flags("general", [], {"sexual": 2})["is_sexual"] == 2
    f = thp.th_flags("general", ["Nude", "blood"], {"gore": False})
    assert f["is_nudity"] and f["is_gore"]


def pkg(rating="general", tags=None, extra=None, path="x.png"):
    return StoryUploadPackage(story_name="Sample Story", chapter_index=0, chapter_title="", platform="th",
                              title="Sample Story", description="", tags=tags or [], rating=rating,
                              file_path=path, file_type="png", media_kind="image", extra=extra or {})


def test_th_underage_refused():
    """Site rule 2.2, in refusal() — the gate the manager runs for live, scheduled and retried posts alike."""
    p = thp.ToyhousePoster()
    assert "underage" in (p.refusal(pkg("adult", ["cub"])) or "")
    assert "underage" in (p.refusal(pkg("mature", ["Young"])) or "")
    assert p.refusal(pkg("general", ["cub"])) is None


class FakeTH:
    username = "SecondFur"

    def __init__(self, images_after, sent=True):
        self.calls = 0
        self.images_after = images_after
        self.sent = sent
        self.uploaded = None

    async def get_characters(self):
        return [{"character_id": "501"}, {"character_id": "502"}]

    async def get_all_images(self, max_pages=thc.MAX_PAGES):
        self.calls += 1
        self.pages_asked = getattr(self, "pages_asked", []) + [max_pages]
        return [{"submission_id": "1000", "character_ids": ["501"], "link": "l0"}] + (
            self.images_after if self.calls > 1 else [])

    async def upload_image(self, **kw):
        self.uploaded = kw
        return {"sent": True} if self.sent else {"sent": False, "error": "nope"}

    async def close(self):
        pass


def poster_with(monkeypatch, fake):
    p = thp.ToyhousePoster()

    async def _c():
        return fake
    monkeypatch.setattr(p, "_client", _c)
    return p


def test_th_refuses_character_not_owned(monkeypatch):
    """FR-SEC-002: only ids in the account's own list are posted."""
    fake = FakeTH([])
    r = run(poster_with(monkeypatch, fake).post(pkg(extra={"character_ids": ["999"]})))
    assert not r.success and fake.uploaded is None


def test_th_needs_a_character(monkeypatch):
    r = run(poster_with(monkeypatch, FakeTH([])).post(pkg()))
    assert not r.success and "character" in r.error


def test_th_unknown_result_checks_gallery(monkeypatch):
    """FR-SEC-009: the new image is found in the gallery under the characters we sent; a gift posted
    meanwhile under someone else's character doesn't count, and nothing found is unconfirmed (never retried)."""
    gift = {"submission_id": "1005", "character_ids": ["777"], "link": "gift"}
    ours = {"submission_id": "1006", "character_ids": ["501"], "link": "https://toyhou.se/501.-/1006"}
    r = run(poster_with(monkeypatch, FakeTH([gift, ours])).post(pkg(extra={"character_ids": ["501"]})))
    assert r.success and r.external_id == "1006"
    r = run(poster_with(monkeypatch, FakeTH([gift])).post(pkg(extra={"character_ids": ["501"]})))
    assert not r.success and r.unconfirmed


def test_th_upload_carries_flags_and_owner_credit(monkeypatch):
    fake = FakeTH([{"submission_id": "1006", "character_ids": ["501"], "link": "x"}])
    run(poster_with(monkeypatch, fake).post(pkg("adult", extra={"character_ids": ["501"]})))
    assert fake.uploaded["flags"]["is_sexual"] == 2
    assert fake.uploaded["credits"] == [{"type": "onsite", "username": "SecondFur"}]


def test_th_offsite_credit_link_scheme(monkeypatch):
    """FR-SEC-012: an off-site credit link is http(s) or nothing."""
    monkeypatch.setattr("posting.artist_credit.profile_url", lambda a, p="": "javascript:alert(1)")
    row = thp.credit_row({"name": "Inkwolf", "handles": {"fa": "inkwolf"}}, False, "SecondFur")
    assert row["type"] == "offsite" and row["url"] == ""
    row = thp.credit_row({"name": "Inkwolf", "handles": {"th": "Inkwolf"}}, False, "SecondFur")
    assert row == {"type": "onsite", "username": "Inkwolf"}


@pytest.mark.parametrize("status", [429, 503, 403])
def test_th_rate_limit_is_not_signed_out(status):
    """FR-002: a busy, rate-limited or blocking Toyhouse is never reported as an expired sign-in."""
    c = fake_client({"https://toyhou.se/~images/upload": (status, "busy", {})})
    with pytest.raises(thc.THUnavailable):
        run(c.validate_session())


def test_th_signed_out_page_is_signed_out():
    c = fake_client({"https://toyhou.se/~images/upload": (200, "<nav id=\"header\"></nav>", {})})
    r = run(c.validate_session())
    assert not r["logged_in"] and "expired" in r["detail"]


def test_th_5xx_after_upload_checks_gallery(tmp_path):
    """FR-SEC-009: a gateway error after the upload counts as maybe-sent, so the gallery is checked."""
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG")
    c = fake_client({"https://toyhou.se/~images/upload": (200, UPLOAD, {}),
                     "https://upload.toyhou.se/~images/upload": (524, "timeout", {})})
    r = run(c.upload_image(file_path=str(img), caption="", character_ids=["501"], flags={}, credits=[]))
    assert r["sent"]


def test_th_art_list_error_before_upload_refuses(monkeypatch):
    """An error page while reading the gallery before the upload stops the post: never an empty list that
    would let the gallery check confirm an old image as the new one."""
    c = fake_client({"https://toyhou.se/~images/upload": (200, UPLOAD, {}),
                     "https://toyhou.se/SecondFur/characters": (200, CHARS, {}),
                     "https://toyhou.se/SecondFur/art": (503, "busy", {})})
    p = thp.ToyhousePoster()

    async def _c():
        return c
    monkeypatch.setattr(p, "_client", _c)
    sent = []

    async def _upload(**kw):
        sent.append(kw)
        return {"sent": True}
    monkeypatch.setattr(c, "upload_image", _upload)
    r = run(p.post(pkg(extra={"character_ids": ["501"]})))
    assert not r.success and not r.unconfirmed and not sent


def test_th_manager_refuses_never_post_account(monkeypatch):
    """FR-SEC-007 on the posting path itself: FRIENDGUARD refuses a never-post Toyhouse account."""
    from database import accounts as adb
    from database.db import get_connection
    from posting import manager
    conn = get_connection()
    try:
        aid = adb.get_default_account_id(conn, "th", create=True)
    finally:
        conn.close()
    monkeypatch.setattr(adb, "never_post_ids", lambda *a, **k: {aid})
    with pytest.raises(ValueError):
        manager._get_poster("th", aid)
    with pytest.raises(ValueError):
        manager._get_poster("th", None)          # the default account is checked too


def test_th_standdown_blocks_post(monkeypatch):
    fa_standdown.note_challenge("th")
    fake = FakeTH([])
    r = run(poster_with(monkeypatch, fake).post(pkg(extra={"character_ids": ["501"]})))
    assert not r.success and "bot check" in r.error and fake.uploaded is None


def test_th_standdown_characters_from_saved(monkeypatch):
    """While Toyhouse is standing us down, the picker's list comes from what's saved, not the site."""
    from routes import th_api
    fa_standdown.note_challenge("th")
    monkeypatch.setattr(th_api, "_creds", lambda aid: {"th_cookie": "sess=abc", "th_username": "SecondFur"})
    monkeypatch.setattr("polling.th_poller.client_from_creds", lambda c: (_ for _ in ()).throw(AssertionError("asked the site")))
    assert run(th_api.th_characters(None))["from"] == "saved"


def test_th_no_pawpoller_line():
    from posting import attribution
    assert attribution.maybe_append("hello", "th", {"pawpoller_attribution": True}) == "hello"


def test_th_friend_account_refused(monkeypatch):
    """FR-SEC-007: an account on the never-post list gets no character list (and the manager's FRIENDGUARD
    refuses the post itself, as for every site)."""
    from database import accounts as adb
    from database.db import get_connection
    from routes import th_api
    conn = get_connection()
    try:
        aid = adb.get_default_account_id(conn, "th", create=True)
    finally:
        conn.close()
    monkeypatch.setattr(adb, "never_post_ids", lambda *a, **k: {aid})
    with pytest.raises(Exception) as e:
        run(th_api.th_characters(aid))
    assert getattr(e.value, "status_code", None) == 404


# ── stand-down + poll ───────────────────────────────────────────────────────

def test_standdown_site_is_a_fixed_set():
    """`site` becomes a file name: an unknown one is never written (and reads as "no stand-down")."""
    with pytest.raises(ValueError):
        fa_standdown.note_challenge("../../etc")
    assert fa_standdown.remaining("../../etc") == 0 and fa_standdown.remaining("th") == 0


def test_th_challenge_starts_standdown_not_signed_out(monkeypatch):
    """FR-SEC-008: a bot check during a poll starts Toyhouse's stand-down and leaves the sign-in alone;
    FA's stand-down is untouched."""
    from polling import th_poller

    class Challenged:
        username = "SecondFur"

        async def validate_session(self):
            raise THChallenged("bot check")
    monkeypatch.setattr(th_poller, "_get_or_create_client", lambda creds: Challenged())
    config.save_settings({"th_cookie": "sess=abc", "th_username": "SecondFur"})
    with pytest.raises(THChallenged):
        run(th_poller.run_th_poll_cycle())
    assert fa_standdown.remaining("th") > 0 and fa_standdown.remaining("fa") == 0
    assert config.get_settings().get("th_cookie") == "sess=abc"
    assert run(th_poller.run_th_poll_cycle()).get("skipped")      # the next tick stands down


def test_th_poll_stores_images_characters_and_comments(monkeypatch):
    from database import inbox_queries, th_queries
    from database.db import get_connection
    from polling import th_poller
    c = fake_client({
        "https://toyhou.se/~images/upload": (200, UPLOAD, {}),
        "https://toyhou.se/SecondFur/art": (200, ART, {}),
        "https://toyhou.se/SecondFur/characters/folder:all": (200, CHARS, {}),
        "https://toyhou.se/501.-": (200, HEADER + r9_sidebar("501", 4, 0, 1, 7), {}),
        "https://toyhou.se/502.-": (200, HEADER + r9_sidebar("502", 1, 0, "", ""), {}),
        "https://toyhou.se/501.-/1001/comments": (200, comment("90", "ThirdFur", "<p>lovely</p>")
                                                  + comment("91", "SecondFur", "<p>thanks</p>"), {}),
        "https://toyhou.se/501.-/comments": (200, comment("92", "ThirdFur", "<p>cute</p>"), {}),
    })
    monkeypatch.setattr(th_poller, "_get_or_create_client", lambda creds: c)
    config.save_settings({"th_cookie": "sess=abc", "th_username": "SecondFur"})
    stats = run(th_poller.run_th_poll_cycle())
    assert stats["submissions_found"] == 2
    conn = get_connection()
    try:
        assert th_queries.get_th_submission(conn, "1001")["favorites_count"] == 3
        assert [r["name"] for r in th_queries.get_th_characters(conn)] == ["Penwright", "Sample One"]
        assert inbox_queries.count_for_submission(conn, "th", "1001") == 2
        assert inbox_queries.count_for_submission(conn, "th", "c501") == 1
    finally:
        conn.close()


def test_th_bare_cookie_value_is_the_session():
    """The guide says to copy the laravel_session value; pasted alone it must still sign in."""
    from clients.th.client import THClient
    assert THClient(cookie="FAKE_TOKEN_FOR_TESTS")._cookies() == {"laravel_session": "FAKE_TOKEN_FOR_TESTS"}
    assert THClient(cookie="laravel_session=a; XSRF-TOKEN=b")._cookies() == {"laravel_session": "a", "XSRF-TOKEN": "b"}
    assert THClient(cookie="")._cookies() == {}


# ── how much of the gallery a poll reads (a 144-page gallery was cut at 40 pages) ──────────────

def _paged_client(seen, pages=10):
    """A gallery of `pages` art pages, each one image, each linking to the next."""
    c = fake_client({}, seen)

    async def _page(path):
        seen.append(path)
        if "/characters" in path:
            return CHARS
        n = int(path.split("page=")[1]) if "page=" in path else 1
        nxt = f'href="https://toyhou.se/SecondFur/art?page={n + 1}"' if n < pages else ""
        return HEADER + art_card(str(3000 + n), "501", "s", "S", 0, 0) + nxt

    async def _ok():
        return {"ok": True}
    c._get_text = _page
    c.validate_session = _ok
    return c


def test_th_max_pages_stops_early_and_never_passes_the_cap(monkeypatch):
    seen = []
    c = _paged_client(seen, pages=10)
    assert len(run(c.get_all_images(3))) == 3
    seen.clear()
    monkeypatch.setattr(thc, "MAX_PAGES", 4)
    assert len(run(c.get_all_images(50))) == 4          # asking for more than the cap still stops at it


def _poll(monkeypatch, c, **kw):
    from polling import th_poller
    monkeypatch.setattr(th_poller, "_get_or_create_client", lambda creds: c)
    config.save_settings({"th_cookie": "sess=abc", "th_username": "SecondFur"})
    return run(th_poller.run_th_poll_cycle(**kw))


def test_th_first_poll_reads_everything_then_the_newest_pages_until_a_week_passes(monkeypatch):
    from database.db import get_connection
    seen = []
    c = _paged_client(seen, pages=10)
    assert _poll(monkeypatch, c)["submissions_found"] == 10          # first poll: every page
    seen.clear()
    assert _poll(monkeypatch, c)["submissions_found"] == thc.RECENT_PAGES
    conn = get_connection()
    try:
        conn.execute("UPDATE th_poll_log SET started_at = datetime('now', '-8 days') WHERE full_sweep = 1")
        conn.commit()
    finally:
        conn.close()
    assert _poll(monkeypatch, c)["submissions_found"] == 10          # a week on: every page again
    assert _poll(monkeypatch, c)["submissions_found"] == thc.RECENT_PAGES


def test_th_full_resync_reads_everything(monkeypatch):
    seen = []
    c = _paged_client(seen, pages=10)
    _poll(monkeypatch, c)
    assert _poll(monkeypatch, c, force_full=True)["submissions_found"] == 10


def test_th_failed_full_read_is_tried_again_next_poll(monkeypatch):
    from database import th_queries
    from database.db import get_connection
    conn = get_connection()
    try:
        lid = th_queries.start_th_poll_log(conn, 1)
        th_queries.finish_th_poll_log(conn, lid, "error", full_sweep=True)
        assert th_queries.full_sweep_due(conn, 1)                     # only a successful full read counts
        lid = th_queries.start_th_poll_log(conn, 1)
        th_queries.finish_th_poll_log(conn, lid, "success", full_sweep=True)
        assert not th_queries.full_sweep_due(conn, 1) and th_queries.full_sweep_due(conn, 2)
    finally:
        conn.close()


def test_th_poll_log_gains_full_sweep_on_an_older_table(tmp_path):
    import sqlite3
    from database import th_queries
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE th_poll_log (id INTEGER PRIMARY KEY, account_id INTEGER, started_at TEXT, "
                 "finished_at TEXT, status TEXT, submissions_found INTEGER, snapshots_inserted INTEGER, "
                 "error_message TEXT, duration_seconds REAL)")
    th_queries.ensure_th_columns(conn)
    th_queries.ensure_th_columns(conn)                                # twice is harmless
    assert "full_sweep" in {r[1] for r in conn.execute("PRAGMA table_info(th_poll_log)")}
    conn.close()


def test_th_poster_reads_only_the_newest_pages(monkeypatch):
    fake = FakeTH([{"submission_id": "1006", "character_ids": ["501"], "link": "x"}])
    run(poster_with(monkeypatch, fake).post(pkg(extra={"character_ids": ["501"]})))
    assert fake.pages_asked == [thc.RECENT_PAGES, thc.RECENT_PAGES]


# ── A populated account's pages (research R9, Amendment B): counts live on characters ────────────

def r9_art_card(iid, char_id, name):
    """The R9 art card: id, data-json, credits and character badge, and no counts at all."""
    return f'''<div class="gallery-thumb thumb"><div class="thumb-image">
<a href="https://f2.toyhou.se/file/f2-toyhou-se/images/{iid}_x.png" class="magnific-item th img-thumbnail" data-id="{iid}" data-json="{{&quot;id&quot;:{iid}}}">
<img src="https://f2.toyhou.se/file/f2-toyhou-se/thumbnails/{iid}_x.png"></a></div>
<div class="magnific-caption hide"><div class="row image-bottom-bar"><div class="image-details col-lg-4 text-left"><h2>Details
<div class="image-controls pull-right"><a href="https://toyhou.se/{char_id}.x/{iid}" class="btn btn-default btn-sm"><i class="fa fa-link"></i></a></div></h2></div>
<div class="image-characters col-lg-4 text-left"><div class="mb-1">
<a href="/{char_id}.x" class="btn btn-sm btn-primary character-name-badge"><i class="fa fa-unlock-alt"></i>{name}</a></div></div></div></div></div>'''


def r9_char_card(cid, name):
    """A card inside a folder, as the owner sees it: link and name, no counts."""
    return f'''<div class="text-center gallery-thumb character-thumb gallery-item"><div class="thumb-image">
<a href="/{cid}.x" class="img-thumbnail"><img src="x"></a></div><div class="thumb-caption">
<span class="thumb-character-name"><a href="/{cid}.x" class="btn btn-sm btn-primary character-name-badge"><i class="fa fa-times-circle"></i>{name}</a></span>
<div class="small thumb-character-stats text-center"><span class="thumb-character-edit hide"><a class="thumb-character-stat edit" href="/~characters/edit/{cid}"><i class="fi-pencil"></i></a></span></div></div></div>'''


def r9_sidebar(cid, gallery, links, comments, faves):
    stat = lambda cls, n: f'<span class="pull-right sidebar-stat {cls}">{n}</span>' if n != "" else ""  # noqa: E731
    fav = f'<span class="pull-right sidebar-stat sidebar-stat-favorites th-favorite-count" data-id="character-{cid}">{faves}</span>'
    return f'''<div class="col-md-3 col-lg-2 sidebar sidebar-offcanvas" id="sidebar"><ul class="side-nav list-unstyled">
<li class=" sidebar-li-gallery"><a href="/{cid}.x/gallery"><i class="far fa-image"></i> Gallery {stat("sidebar-stat-gallery", gallery)}</a></li>
<li class=" sidebar-li-links"><a href="/{cid}.x/links"><i class="fa fa-link"></i> Links {stat("sidebar-stat-links", links)}</a></li>
<li class="  sidebar-li-comments"><a href="/{cid}.x/comments"><i class="fa fa-comment"></i> Comments {stat("sidebar-stat-coments", comments)}</a></li>
<li class="sidebar-li-favorites hide" data-id="character-{cid}" th-favorite="0"><a href="#" class="favorited"><i class="fa fa-star"></i> Favorites {fav}</a></li>
<li class="sidebar-li-favorites " data-id="character-{cid}" th-favorite="1"><a href="#"><i class="far fa-star"></i> Favorites {fav}</a></li>
</ul></div><div class="col-sm-12 col-md-9 col-lg-10 content-main" id="content">'''


R9_ART = HEADER + r9_art_card("2001", "601", "Sample One") + r9_art_card("2002", "602", "Penwright")
R9_ROOT = HEADER + ('<div class="characters-folders-top"><a href="/SecondFur/characters/folder:unsorted">Unsorted</a>'
                    '<a href="/SecondFur/characters/folder:all">All</a><a href="/SecondFur/characters/folder:9">Folder</a></div>')
R9_ALL_1 = HEADER + r9_char_card("601", "Sample One") + '<a href="https://toyhou.se/SecondFur/characters/folder:all?page=2">2</a>'
R9_ALL_2 = HEADER + r9_char_card("602", "Penwright")
R9_CHAR_601 = HEADER + r9_sidebar("601", 12, 3, 24, 185) + "<p>About this character</p>"
R9_CHAR_602 = HEADER + r9_sidebar("602", "", "", "", "")           # zero shows as an empty span or none
# An image page: the sidebar is its CHARACTER's (favourites 185), and nothing outside it counts the image.
R9_IMAGE_PAGE = HEADER + r9_sidebar("601", 12, 3, 24, 185) + '<div class="image-details"><h2>Details</h2></div>'


def test_th_characters_come_from_folder_all_across_pages():
    seen = []
    c = fake_client({
        "https://toyhou.se/SecondFur/characters/folder:all": (200, R9_ALL_1, {}),
        "https://toyhou.se/SecondFur/characters/folder:all?page=2": (200, R9_ALL_2, {}),
        "https://toyhou.se/SecondFur/characters": (200, R9_ROOT, {}),
    }, seen)
    chars = run(c.get_characters())
    assert [(ch["character_id"], ch["name"]) for ch in chars] == [("601", "Sample One"), ("602", "Penwright")]
    assert all(ch["favorites_count"] is None for ch in chars)            # unknown from a card, not 0
    assert not any(u.endswith("/SecondFur/characters") for u in seen)    # folder buttons only there


def test_th_character_counts_come_from_its_page_sidebar():
    s = thc.parse_character_stats(R9_CHAR_601)
    assert s == {"favorites_count": 185, "comments_count": 24, "images_count": 12, "links_count": 3}


def test_th_empty_sidebar_count_is_zero():
    assert thc.parse_character_stats(R9_CHAR_602) == {
        "favorites_count": 0, "comments_count": 0, "images_count": 0, "links_count": 0}
    assert thc.parse_character_stats(HEADER + "<p>Content warning</p>") is None   # no sidebar: unknown


def test_th_image_page_sidebar_is_never_the_images_favourites():
    """FR-009a: an image page's sidebar belongs to its character; reading the image from it would file the
    character's favourites as the image's."""
    assert thc.parse_art_page(R9_IMAGE_PAGE) == []
    assert "parse_character_stats" not in thc.parse_art_page.__code__.co_names


def test_th_art_card_without_counts_stores_none_not_zero():
    imgs = thc.parse_art_page(R9_ART)
    assert [i["submission_id"] for i in imgs] == ["2001", "2002"]
    assert all(i["favorites_count"] is None and i["comments_count"] is None for i in imgs)
    assert imgs[0]["character_ids"] == ["601"]
    assert thc.parse_art_page(ART)[0]["favorites_count"] == 3              # the R5 layout still reads


def _r9_client(seen=None):
    c = fake_client({
        "https://toyhou.se/~images/upload": (200, UPLOAD, {}),
        "https://toyhou.se/SecondFur/art": (200, R9_ART, {}),
        "https://toyhou.se/SecondFur/characters/folder:all": (200, R9_ALL_1, {}),
        "https://toyhou.se/SecondFur/characters/folder:all?page=2": (200, R9_ALL_2, {}),
        "https://toyhou.se/601.-": (200, R9_CHAR_601, {}),
        "https://toyhou.se/602.-": (200, R9_CHAR_602, {}),
        "https://toyhou.se/601.-/comments": (200, comment("95", "ThirdFur", "<p>cute</p>"), {}),
        "https://toyhou.se/602.-/comments": (200, "", {}),
    }, seen)
    return c


def test_th_poll_reads_character_pages_and_records_the_totals(monkeypatch):
    from database import inbox_queries, th_queries
    from database.db import get_connection
    from polling import th_poller
    c = _r9_client()
    monkeypatch.setattr(th_poller, "_get_or_create_client", lambda creds: c)
    config.save_settings({"th_cookie": "sess=abc", "th_username": "SecondFur"})
    run(th_poller.run_th_poll_cycle())
    conn = get_connection()
    try:
        rows = {r["character_id"]: r for r in th_queries.get_th_characters(conn)}
        assert (rows["601"]["favorites_count"], rows["601"]["comments_count"], rows["601"]["images_count"]) == (185, 24, 12)
        assert rows["602"]["favorites_count"] == 0 and rows["601"]["read_at"]
        assert th_queries.get_th_submission(conn, "2001")["favorites_count"] is None
        s = th_queries.get_th_summary(conn)
        assert (s["character_favorites"], s["character_comments"], s["characters"]) == (185, 24, 2)
        assert s["image_counts_known"] is False
        assert [t["character_favorites"] for t in th_queries.get_th_character_totals(conn)] == [185]
        assert inbox_queries.count_for_submission(conn, "th", "c601") == 1
    finally:
        conn.close()


def test_th_ordinary_poll_reads_at_most_twenty_characters_longest_unread_first(monkeypatch):
    from database import th_queries
    from database.db import get_connection
    conn = get_connection()
    try:
        for i in range(25):
            th_queries.upsert_th_character(conn, {"character_id": str(700 + i), "name": f"C{i}", "link": ""}, 1)
        conn.execute("UPDATE th_characters SET read_at = datetime('now', '-' || (100 - CAST(character_id AS INT) + 700) || ' minutes')")
        conn.execute("UPDATE th_characters SET read_at = NULL WHERE character_id = '724'")
        conn.commit()
        due = th_queries.characters_to_read(conn, 1, [str(700 + i) for i in range(25)], 20)
        assert len(due) == 20 and due[0] == "724"                          # never read first
        assert due[1:4] == ["700", "701", "702"]                           # then the longest ago
    finally:
        conn.close()
    assert th_poller_limit() == 20


def th_poller_limit():
    from polling import th_poller
    return th_poller.CHARACTERS_PER_POLL


def test_th_dashboard_leads_with_characters_and_is_honest_about_images():
    """FR-009c and Constitution VI: character numbers first; no image 0s when Toyhouse shows none."""
    from pathlib import Path
    src = Path("frontend/js/app.js").read_text(encoding="utf-8")
    dash = src[src.index("async renderTHDashboard() {"):src.index("async renderTHSubmissions() {")]
    cards = [dash.index(f"statCard('{t}'") for t in ("Character Favourites", "Character Comments", "Characters", "Images")]
    assert cards == sorted(cards)
    assert "Image Favourites" not in dash and "getTHCharacterAggregate" in dash
    assert "summary.image_counts_known ?" in dash and "Toyhouse doesn't show favourites" in dash
    assert "Utils.escapeHtml(c.name)" in dash


def test_th_character_totals_route(monkeypatch):
    from database import th_queries
    from database.db import get_connection
    from routes import th_api
    conn = get_connection()
    try:
        th_queries.upsert_th_character(conn, {"character_id": "601", "name": "Sample One"}, 1)
        th_queries.record_character_stats(conn, "601", {"favorites_count": 9, "comments_count": 2, "images_count": 1})
        th_queries.record_character_totals(conn, 1, polled_at="2031-01-06 09:00:00")
        conn.commit()
    finally:
        conn.close()
    out = th_api.get_th_character_aggregate(None, None, None)
    assert out["snapshots"] == [{"polled_at": "2031-01-06 09:00:00", "characters": 1,
                                 "character_favorites": 9, "character_comments": 2}]


def test_connect_with_a_non_numeric_account_is_a_400_not_a_500():
    """SEC4884 Low 1."""
    import asyncio
    from fastapi import HTTPException
    from routes import th_api
    with pytest.raises(HTTPException) as e:
        asyncio.run(th_api.th_connect({"username": "Inkwolf", "cookie": "a=b", "account_id": "abc"}))
    assert e.value.status_code == 400


def test_logs_and_links_carry_no_session_or_script():
    """SEC4884 Lows 2 and 4: no part of an Inkbunny SID in the log; Inbox links go through safeUrl."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert "sid[:8]" not in (root / "clients/ib/client.py").read_text(encoding="utf-8")
    assert "Utils.safeUrl(i.permalink)" in (root / "frontend/js/inbox.js").read_text(encoding="utf-8")
