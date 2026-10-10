"""Spec 027: journals to FurAffinity, Weasyl and DeviantArt — against stand-ins for each site."""
from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import config
from clients.da.client import DAClient
from clients.fa.client import FAClient
from clients.weasyl.client import WeasylClient
from database import posts_queries
from database.db import get_connection
from posting import journals


class Sites:
    """Stand-ins: FA's /controls/journal/ form, Weasyl's journal forms, DeviantArt's journal API."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.ws_refuse = ""
        self.da_error: dict | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        u = urlparse(str(request.url))
        form = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(request.content.decode()).items()} \
            if request.method == "POST" else {}
        self.calls.append((request.method, f"{u.netloc}{u.path}", form))
        host, path = u.netloc, u.path
        if "furaffinity" in host:
            if request.method == "GET" and path.startswith("/controls/journal/"):
                return httpx.Response(200, text='<a href="/logout/">Log out</a><form id="journal-form" name="MsgForm" '
                                      'method="post" action="/controls/journal/"><input type="hidden" name="key" '
                                      'value="KEY1"/></form>')
            if request.method == "POST":
                jid = form.get("id") if form.get("id") not in (None, "0") else "555"
                return httpx.Response(302, headers={"location": f"https://www.furaffinity.net/journal/{jid}/"})
            return httpx.Response(200, text="journal page")
        if "weasyl" in host:
            if path == "/submit/journal":
                if self.ws_refuse:
                    return httpx.Response(403, text=f"<div id='error_content'>{self.ws_refuse}</div>")
                return httpx.Response(303, headers={"location": "https://www.weasyl.com/journal/77/sample-story"})
            if path == "/edit/journal" and request.method == "GET":
                return httpx.Response(200, text='<form action="/edit/journal"><input name="journalid" value="77">'
                                      '<input name="title" value="old"><textarea name="content">old</textarea>'
                                      '<select name="rating"><option value="10" selected>G</option></select></form>')
            if path.startswith("/journal/") and request.method == "GET":
                return httpx.Response(200, text='<form action="/submit/tags"><input name="journalid" value="77">'
                                      '<textarea name="tags">old</textarea></form>')
            if request.method == "POST":
                return httpx.Response(303, headers={"location": "https://www.weasyl.com/journal/77/sample-story"})
            return httpx.Response(200, text="ok")
        if "deviantart" in host:
            if self.da_error:
                return httpx.Response(403, json=self.da_error)
            if path.endswith("/deviation/journal/create"):
                return httpx.Response(200, json={"deviationid": "ABC-1"})
            if "/deviation/journal/update/" in path:
                return httpx.Response(200, json={"success": True})
            if path.endswith("/deviation/ABC-1"):
                return httpx.Response(200, json={"url": "https://www.deviantart.com/secondfur/journal/sample-1"})
        return httpx.Response(404)

    def posted(self, host_part: str, path_part: str) -> list[dict]:
        return [f for m, p, f in self.calls if m == "POST" and host_part in p and path_part in p]


@pytest.fixture
def sites(monkeypatch):
    s = Sites()
    mk = lambda: httpx.AsyncClient(transport=httpx.MockTransport(s), follow_redirects=True)  # noqa: E731

    async def fa(_acct):
        c = FAClient("secondfur", "a", "b")
        c._fa_http = mk()
        return c

    async def ws(_acct):
        c = WeasylClient(api_key="K")
        c._http = mk()
        return c

    async def da(_acct):
        c = DAClient(target_user="secondfur")
        c._http = mk()
        return c, "TOKEN"
    monkeypatch.setattr(journals, "_fa", fa)
    monkeypatch.setattr(journals, "_ws", ws)
    monkeypatch.setattr(journals, "_da", da)
    return s


def _journal(**kw):
    post = {"title": "Commissions open", "body": "Three **slots**. Prices [here](https://example.com/p).",
            "rating": "general", "tags": "commissions open_slots", "featured": 1, "kind": "journal"}
    post.update(kw)
    return post


def _publish(post, site):
    return asyncio.run(journals.publish_journal(post, site, None, config.get_settings()))


# ── Format converters ───────────────────────────────────────────────────────

def test_markdown_becomes_bbcode_and_html():
    md = "# News\n\nThree **slots**, *maybe* more: [prices](https://example.com/p) or https://example.com/q\n\n- one\n- two"
    bb = journals.to_bbcode(md)
    assert "[b]News[/b]" in bb and "[b]slots[/b]" in bb and "[i]maybe[/i]" in bb
    assert "[url=https://example.com/p]prices[/url]" in bb and "[url]https://example.com/q[/url]" in bb
    assert "• one\n• two" in bb
    h = journals.to_html(md + "\n\n<script>x</script>")
    assert "<h3>News</h3>" in h and '<a href="https://example.com/p">prices</a>' in h
    assert "<ul><li>one</li><li>two</li></ul>" in h and "<script>" not in h and "&lt;script&gt;" in h


# ── US1: one journal, three sites ───────────────────────────────────────────

def test_fa_journals_are_copied_not_posted_because_of_its_captcha(sites):
    """Live 2026-10-10: FA's journal form demands a CAPTCHA. PawPoller never tries; it hands over the FA version."""
    r = _publish(_journal(), "fa")
    assert not r["success"] and "CAPTCHA" in r["error"] and sites.calls == []
    c = journals.copy_text("fa", "x" * 70, "Three **slots**. [p](https://example.com/p)")
    assert c["title"] == "x" * 60 and c["title_cut"] and "[b]slots[/b]" in c["text"]
    assert c["open_url"].endswith("/controls/journal/")
    with pytest.raises(ValueError):
        journals.copy_text("ws", "t", "b")


def test_fa_client_form_and_its_captcha_refusal(sites):
    """The FA client still speaks the form (for if FA drops the CAPTCHA) and names the CAPTCHA when it's there."""
    async def run():
        cli = await journals._fa(2)
        return await cli.submit_journal("Commissions open", "[b]slots[/b]", "2", featured=True)
    r = asyncio.run(run())
    assert r["id"] == "555"
    f = sites.posted("furaffinity", "/controls/journal/")[0]
    assert (f["key"], f["id"], f["do"], f["rating"], f["make_featured"]) == ("KEY1", "0", "update", "2", "on")
    assert f["subject"] == "Commissions open" and "[b]slots[/b]" in f["message"]


def test_weasyl_gets_markdown_rating_and_tags(sites):
    r = _publish(_journal(rating="adult"), "ws")
    assert r["success"] and r["external_id"] == "77"
    f = sites.posted("weasyl", "/submit/journal")[0]
    assert f["rating"] == "40" and f["content"].startswith("Three **slots**") and f["tags"] == "commissions open_slots"


def test_deviantart_gets_html_maturity_and_its_own_link(sites):
    r = _publish(_journal(rating="mature"), "da")
    assert r["success"] and r["external_id"] == "ABC-1"
    assert r["external_url"] == "https://www.deviantart.com/secondfur/journal/sample-1"
    f = sites.posted("deviantart", "/journal/create")[0]
    assert f["is_mature"] == "1" and f["mature_level"] == "moderate" and "<strong>slots</strong>" in f["body"]
    assert f["tags[0]"] == "commissions" and f["access_token"] == "TOKEN"


def test_story_ratings_never_fall_to_general():
    assert journals._rating({"rating": "explicit"}) == "adult"
    assert journals._rating({"rating": "teen"}) == "general"
    assert journals._rating({"rating": "weird"}) == "adult"


def test_title_limits_and_refusals_in_plain_words(sites):
    r = _publish(_journal(title="x" * 51), "da")
    assert not r["success"] and "at most 50" in r["error"] and sites.calls == []
    sites.ws_refuse = "vouchRequired"
    r = _publish(_journal(), "ws")
    assert not r["success"] and "verified" in r["error"]
    sites.da_error = {"error": "insufficient_scope", "error_description": "scope user.manage"}
    r = _publish(_journal(), "da")
    assert not r["success"] and "Authorise posting" in r["error"]


def test_never_post_accounts_are_refused(sites, monkeypatch):
    from database import accounts as accounts_db
    monkeypatch.setattr(accounts_db, "never_post_ids", lambda s=None: {0})
    r = _publish(_journal(), "ws")
    assert not r["success"] and "Never post" in r["error"] and sites.calls == []


def test_a_journal_post_publishes_through_the_posts_module(sites):
    from posting import post_publisher
    conn = get_connection()
    try:
        pid = posts_queries.create_post(conn, body="Hello **there**", kind="journal", title="Sample Story news",
                                        tags="news", now="2026-10-10 00:00:00")
    finally:
        conn.close()
    res = asyncio.run(post_publisher.publish_post(pid, ["fa", "ws", "bsky"]))
    assert [r["success"] for r in res] == [False, True, False] and "isn't wired" in res[2]["error"]
    assert "CAPTCHA" in res[0]["error"]
    conn = get_connection()
    try:
        pubs = {p["platform"]: p for p in posts_queries.get_post_publications(conn, pid)}
        listed = posts_queries.list_posts(conn, kind="journal")
    finally:
        conn.close()
    assert pubs["fa"]["status"] == "failed" and pubs["ws"]["external_id"] == "77"
    assert [p["post_id"] for p in listed] == [pid] and listed[0]["title"] == "Sample Story news"


def test_preview_blocks_an_over_long_title():
    rows = journals.preview("y" * 55, ["da", "ws"])
    assert not any("at most" in w["text"] for w in rows["ws"]["warnings"])
    assert any(w["level"] == "block" and "at most 50" in w["text"] for w in rows["da"]["warnings"])


# ── US2: announcing a piece ─────────────────────────────────────────────────

def test_an_announcement_waits_for_the_publish_then_links_it(sites, monkeypatch):
    from posting import paired_comment
    monkeypatch.setattr(paired_comment, "piece_context",
                        lambda kind, name, plat, ch=0, acct=None: {"extra": {}, "title": "Sample Story", "artist": None})
    pid = journals.store_piece_journal("artwork", "Sample_Story",
                                       {"sites": ["ws"], "title": "New art: {title}", "text": "Up now: {links}"},
                                       "general")
    asyncio.run(journals.journal_pass("artwork", "Sample_Story", [{"platform": "ib", "success": False}]))
    assert sites.calls == []                                   # nothing landed: the journal waits
    results = [{"platform": "fa", "success": True, "external_url": "https://www.furaffinity.net/view/1/"},
               {"platform": "ib", "success": True, "external_url": "https://inkbunny.net/s/2"}]
    asyncio.run(journals.journal_pass("artwork", "Sample_Story", results))
    f = sites.posted("weasyl", "/submit/journal")[0]
    assert f["title"] == "New art: Sample Story"
    assert "https://www.furaffinity.net/view/1/" in f["content"] and "https://inkbunny.net/s/2" in f["content"]
    assert results[-1]["journal"] and results[-1]["success"]
    conn = get_connection()
    try:
        post = posts_queries.get_post(conn, pid)
        pubs = posts_queries.get_post_publications(conn, pid)
    finally:
        conn.close()
    assert post["body"].startswith("Up now: https://") and [p["status"] for p in pubs] == ["posted"]
    asyncio.run(journals.journal_pass("artwork", "Sample_Story", results[:2]))
    assert len(sites.posted("weasyl", "/submit/journal")) == 1          # posted once, never again


def test_an_empty_link_is_left_out_and_said(sites, monkeypatch):
    from posting import paired_comment
    monkeypatch.setattr(paired_comment, "piece_context",
                        lambda kind, name, plat, ch=0, acct=None: {"extra": {}, "title": "Sample Story", "artist": None})
    journals.store_piece_journal("story", "Sample_Story",
                                 {"sites": ["ws"], "title": "{title}: chapter {chapter}",
                                  "text": "Chapter {chapter}: {chapter_title}. FA: {site:fa}"}, "explicit")
    results = [{"platform": "ib", "success": True, "chapter_index": 3, "chapter_title": "The Den",
                "external_url": "https://inkbunny.net/s/9"}]
    asyncio.run(journals.journal_pass("story", "Sample_Story", results))
    f = sites.posted("weasyl", "/submit/journal")[0]
    assert f["title"] == "Sample Story: chapter 3" and f["content"].startswith("Chapter 3: The Den.")
    assert "{site:fa}" not in f["content"] and f["rating"] == "40"
    assert "Left out {site:fa}" in results[-1]["error"]


def test_unknown_placeholders_are_refused_when_stored():
    with pytest.raises(ValueError, match="doesn't know"):
        journals.store_piece_journal("artwork", "x", {"sites": ["ws"], "title": "t", "text": "{oops}"})
    assert journals.store_piece_journal("artwork", "x", {"sites": [], "title": "t", "text": "b"}) is None


# ── US3: fix it everywhere ──────────────────────────────────────────────────

def test_edits_resend_the_whole_journal(sites):
    post = _journal(title="Fixed title", body="New text")
    r = asyncio.run(journals.edit_journal(post, {"platform": "da", "external_id": "ABC-1", "account_id": 0,
                                                 "external_url": "https://www.deviantart.com/secondfur/journal/sample-1"}))
    assert r["success"] and r["external_url"].endswith("sample-1")
    f = sites.posted("deviantart", "/journal/update/ABC-1")[0]
    assert f["title"] == "Fixed title" and f["body"] == "<p>New text</p>" and f["is_mature"] == "0" and f["tags[1]"]
    r = asyncio.run(journals.edit_journal(post, {"platform": "fa", "external_id": "555", "account_id": 0}))
    assert r.get("skipped") and "CAPTCHA" in r["reason"] and sites.posted("furaffinity", "/controls") == []
    asyncio.run(journals.edit_journal(post, {"platform": "ws", "external_id": "77", "account_id": 0}))
    assert sites.posted("weasyl", "/edit/journal")[0]["title"] == "Fixed title"
    assert sites.posted("weasyl", "/submit/tags")[0]["tags"] == "commissions open_slots"


def test_remove_takes_it_off_weasyl_and_says_so_elsewhere(sites):
    r = asyncio.run(journals.remove_journal({"platform": "ws", "external_id": "77", "account_id": 0}))
    assert r["success"] and sites.posted("weasyl", "/remove/journal")[0]["journalid"] == "77"
    r = asyncio.run(journals.remove_journal({"platform": "fa", "external_id": "555", "account_id": 0}))
    assert r.get("skipped") and "Remove it on FurAffinity" in r["error"]


def test_routes_create_edit_and_remove(sites):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.posts_api import posts_router
    app = FastAPI()
    app.include_router(posts_router)
    c = TestClient(app)
    assert c.post("/api/posts", data={"kind": "journal", "body": "x"}).status_code == 400   # no title
    pid = c.post("/api/posts", data={"kind": "journal", "title": "Sample Story news", "body": "Hi",
                                     "tags": "news, art", "featured": "1"}).json()["post_id"]
    assert c.post(f"/api/posts/{pid}/publish", json={"platforms": ["ws"], "confirm_live": True}).json()["successes"] == 1
    out = c.patch(f"/api/posts/{pid}", json={"body": "Hi again", "tags": "news"}).json()
    assert out["results"][0]["success"] and sites.posted("weasyl", "/edit/journal")[0]["content"] == "Hi again"
    assert c.post(f"/api/posts/{pid}/remove-from-sites", json={}).status_code == 400
    assert c.post(f"/api/posts/{pid}/remove-from-sites", json={"confirm": True}).json()["results"][0]["success"]
    rules = c.get("/api/posts/rules").json()
    assert rules["journal_sites"] == ["ws", "da"] and rules["journal_title_limits"]["fa"] == 60
    copied = c.post("/api/posts/journal-copy", json={"site": "fa", "title": "T", "body": "**b**"}).json()
    assert copied["text"] == "[b]b[/b]"


def test_journal_templates_save_and_fall_back():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.posts_api import posts_router
    app = FastAPI()
    app.include_router(posts_router)
    c = TestClient(app)
    assert c.get("/api/posts/journal-templates").json()["templates"]["art"] == journals.DEFAULT_TEMPLATES["art"]
    out = c.put("/api/posts/journal-templates", json={"templates": {"art": "Fresh: {links}", "chapter": ""}}).json()
    assert out["templates"]["art"] == "Fresh: {links}"
    assert out["templates"]["chapter"] == journals.DEFAULT_TEMPLATES["chapter"]     # blank → the default
    assert c.put("/api/posts/journal-templates", json={"templates": {"art": "{nope}"}}).status_code == 400
    assert c.get("/api/posts/rules").json()["journal_templates"]["art"] == "Fresh: {links}"


def test_fa_captcha_page_is_named(sites, monkeypatch):
    real = sites.__call__

    def captcha(request):
        if request.method == "POST" and "furaffinity" in str(request.url):
            return httpx.Response(200, text="<h2>System Message</h2> Error posting a journal. CAPTCHA verification failed.")
        return real(request)
    async def run():
        cli = await journals._fa(2)
        cli._fa_http = httpx.AsyncClient(transport=httpx.MockTransport(captcha), follow_redirects=True)
        return await cli.submit_journal("t", "m", "0")
    with pytest.raises(RuntimeError, match="CAPTCHA"):
        asyncio.run(run())



# ── The desktop's filled-in FA window (4.62.2) ─────────────────────────────

def test_the_fa_window_fills_the_form_from_json_and_spots_the_new_journal():
    from auth import fa_journal_window as w
    js = w.fill_script('Title with "quotes" </script>', "[b]x[/b]\nline", "2")
    assert '"m": "[b]x[/b]\\nline"' in js and '"r": "2"' in js        # the newline travels escaped in the JSON
    assert '"Title with \\"quotes\\" </script>"' in js and 'form[action="/controls/journal/"]' in js
    assert w.posted_journal("https://www.furaffinity.net/journal/1234/") == "1234"
    assert w.posted_journal("https://www.furaffinity.net/controls/journal/") == ""
    assert w.open_fa_journal("t", "m", "0")["ok"] is False        # no desktop GUI loop in tests


def test_a_journal_posted_through_the_fa_window_is_recorded():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.posts_api import posts_router
    app = FastAPI()
    app.include_router(posts_router)
    c = TestClient(app)
    pid = c.post("/api/posts", data={"kind": "journal", "title": "Sample Story news", "body": "Hi"}).json()["post_id"]
    bad = c.post(f"/api/posts/{pid}/journal-record",
                 json={"platform": "fa", "external_id": "77", "external_url": "https://evil.example/journal/77/"})
    assert bad.status_code == 400
    ok = c.post(f"/api/posts/{pid}/journal-record", json={"platform": "fa", "external_id": "77",
                                                           "external_url": "https://www.furaffinity.net/journal/77/"})
    assert ok.json()["ok"]
    conn = get_connection()
    try:
        pubs = posts_queries.get_post_publications(conn, pid)
    finally:
        conn.close()
    assert [(p["platform"], p["status"], p["external_id"]) for p in pubs] == [("fa", "posted", "77")]
    assert c.post("/api/posts/journal-copy", json={"site": "fa", "title": "T", "body": "b", "rating": "explicit"}).json()["rating"] == "1"


def test_fa_window_only_acts_on_fa_itself():
    """SECLOW4622: the host is checked, not a substring anywhere in the address."""
    from auth import fa_journal_window as w
    assert w.posted_journal("https://evil.example/?u=https://www.furaffinity.net/journal/99/") == ""
    assert w.posted_journal("https://furaffinity.net.evil.example/journal/99/") == ""
    assert w.posted_journal("https://www.furaffinity.net/journal/99/") == "99"
    assert w.on_fa("https://www.furaffinity.net/controls/journal/") and not w.on_fa("https://evil.example/controls/journal/")


def test_journal_edit_and_remove_respect_never_post(monkeypatch):
    """SECLOW4622: edit and remove refuse a never-post account, as publish does."""
    import asyncio
    from database import accounts as accounts_db
    monkeypatch.setattr(accounts_db, "never_post_ids", lambda settings=None: {10})
    pub = {"platform": "ws", "account_id": 10, "external_id": "1"}
    post = {"title": "Sample", "body": "x", "rating": "general"}
    assert asyncio.run(journals.edit_journal(post, pub))["error"] == accounts_db.NEVER_POST_ERROR
    assert asyncio.run(journals.remove_journal(pub))["error"] == accounts_db.NEVER_POST_ERROR


def test_result_links_are_scheme_checked():
    """SECLOW4622: a result link only becomes a link when it is http(s)."""
    src = open("frontend/js/components.js", encoding="utf-8").read()
    block = src[src.index("publishResults(results, opts) {"):][:2500]
    assert "Utils.safeUrl(r.external_url || r.url || '')" in block
