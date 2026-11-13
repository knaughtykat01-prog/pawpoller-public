"""Spec 084 — Patreon: the client, the poster, sign-in and routes, against a stand-in Patreon (no network).

The stand-in answers from hand-written JSON:API dicts shaped like the live answers mapped on 13 Nov 2026 (research
R1–R6, R11). Names are stand-ins only (Inkwolf / SecondFur / Sample Story).
"""
from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import config
from clients.pat import account as pat_account
from clients.pat import client as patc
from clients.pat import editor_json
from database import accounts as adb
from database.db import get_connection
from posting.platforms import patreon as patp
from posting.platforms.base import StoryUploadPackage

PLANTED_COOKIE = "session_id=PLANTED_SESSION_VALUE_123"
CSRF = "PLANTED_CSRF_TOKEN_456"
PAYWALL = editor_json.paywall_node("USD", "777")


def run(coro):
    return asyncio.run(coro)


async def _no_sleep(*_a, **_k):
    return None


class FakePatreon:
    """A stand-in Patreon. ``requests`` records (method, host, path, query, headers, json body)."""

    def __init__(self, vanity="inkwolf", campaign="111", nsfw=False):
        self.vanity, self.campaign, self.nsfw = vanity, campaign, nsfw
        self.requests: list[dict] = []
        self.post = None              # the one draft: {edited_at, published_at, rule_ids, content_json_string}
        self.keep_old_rules = False   # the save answers 200 but keeps the default audience
        self.extra_read_rules = []    # rule ids Patreon adds to the read-back (any shape it likes)
        self.after_publish_rules = None
        self.outdated_once = False
        self.lose_publish = False
        self.media_state = "ready"
        self.media_n = 0
        self.tiers_gone = False
        self.collections = [{"type": "collection", "id": "900", "attributes": {"title": "Sample Story", "num_posts": 2}}]
        self.members_pages = None
        self.status_override = None   # (path, status, body, headers)
        self.teaser_leak = False      # Patreon keeps some of the body as a free preview despite the marker
        self.teaser_leak_on_publish = False
        self.omit_teaser = False      # the read-back has no teaser_text_json_string at all
        self.media_free_stuck = None  # the "Free image" switch reads back this, whatever is sent
        self.media_free_on_publish = None   # ... or this, only once published
        self.keep_times = True        # scheduled_for / change_visibility_at are stored as sent
        self.media_host = "c10.patreonusercontent.com"
        self.media_scheme = "https"
        self.lose_schedule = False    # the schedule save is stored but its answer is lost
        self.hide_new_collection = False    # a made collection doesn't show in the list read after
        self.after_schedule_rules = None
        # GET /api/posts pages by cursor ("" = first): (posts, next cursor); comments by (post id, cursor): (data, included, next)
        self.post_pages = {"": ([{"type": "post", "id": "777", "attributes": {
            "title": "Sample Story", "like_count": 4, "comment_count": 2, "published_at": "2026-11-12T00:00:00Z",
            "post_type": "image_file", "url": "https://evil.example/elsewhere"}}], None)}
        self.comment_pages = {("777", ""): (
            [{"type": "comment", "id": "61", "attributes": {"body": "<b>Lovely</b> work", "created": "2026-11-12T01:00:00Z",
                                                           "is_by_creator": False},
              "relationships": {"commenter": {"data": {"type": "user", "id": "u1"}}}}],
            [{"type": "comment", "id": "62", "attributes": {"body": "Thank you", "created": "2026-11-12T02:00:00Z",
                                                           "is_by_creator": True},
              "relationships": {"commenter": {"data": {"type": "user", "id": "u9"}}}},
             {"type": "user", "id": "u1", "attributes": {"full_name": "SecondFur", "email": "owner@example.com"}},
             {"type": "user", "id": "u9", "attributes": {"full_name": "Inkwolf"}}], None)}

    # ── answers ──────────────────────────────────────────────────────────────
    def rules(self):
        inc = [
            {"type": "access-rule", "id": "1", "attributes": {"access_rule_type": "public"}},
            {"type": "access-rule", "id": "2", "attributes": {"access_rule_type": "patrons"}},
            {"type": "access-rule", "id": "3", "attributes": {"access_rule_type": "tier"},
             "relationships": {"tier": {"data": {"type": "reward", "id": "30"}}}},
            {"type": "access-rule", "id": "4", "attributes": {"access_rule_type": "tier"},
             "relationships": {"tier": {"data": {"type": "reward", "id": "40"}}}},
            {"type": "access-rule", "id": "5", "attributes": {"access_rule_type": "tier"},
             "relationships": {"tier": {"data": {"type": "reward", "id": "50"}}}},
            {"type": "access-rule", "id": "6", "attributes": {"access_rule_type": "non_member"}},
            {"type": "access-rule", "id": "7", "attributes": {"access_rule_type": "tier"},
             "relationships": {"tier": {"data": {"type": "reward", "id": "-1"}}}},
            {"type": "access-rule", "id": "8", "attributes": {"access_rule_type": "min_cents_pledged"}},
            {"type": "access-rule", "id": "9", "attributes": {"access_rule_type": "made_up_kind"}},
            {"type": "reward", "id": "30", "attributes": {"title": "Free", "amount_cents": 0}},
            {"type": "reward", "id": "-1", "attributes": {"title": "Everyone", "amount_cents": 0}},
        ]
        if not self.tiers_gone:
            inc += [{"type": "reward", "id": "40", "attributes": {"title": "<b>Sketch</b> club", "amount_cents": 500}},
                    {"type": "reward", "id": "50", "attributes": {"title": "Inner circle", "amount_cents": 1000}}]
        return inc

    def handler(self, request: httpx.Request) -> httpx.Response:
        u = urlsplit(str(request.url))
        body = None
        if request.content and request.headers.get("content-type", "").startswith("application/vnd.api+json"):
            body = json.loads(request.content)
        self.requests.append({"method": request.method, "host": u.hostname, "path": u.path,
                              "query": parse_qs(u.query), "headers": dict(request.headers), "json": body,
                              "raw": request.content})
        if self.status_override and (u.path == self.status_override[0] or (
                self.status_override[0].startswith("*") and u.path.endswith(self.status_override[0][1:]))):
            _, status, raw, headers = self.status_override
            return httpx.Response(status, content=raw, headers=headers)
        if u.hostname == "patreon-media.s3-accelerate.amazonaws.com":
            return httpx.Response(204)
        p, m = u.path, request.method
        if p == "/home":
            return httpx.Response(200, text=f'<html><meta name="csrf-token" content="{CSRF}"></html>')
        if p == "/api/current_user":
            return httpx.Response(200, json={
                "data": {"id": "9", "type": "user", "relationships": {"campaign": {"data": {"type": "campaign", "id": self.campaign}}}},
                "included": [{"type": "campaign", "id": self.campaign, "attributes": {
                    "vanity": self.vanity, "is_nsfw": self.nsfw, "currency": "USD", "name": "Sample Page"}}]})
        if p == f"/api/campaigns/{self.campaign}":
            q = parse_qs(u.query)
            if "access_rules" in (q.get("include") or [""])[0]:
                return httpx.Response(200, json={"data": {"id": self.campaign, "type": "campaign"}, "included": self.rules()})
            return httpx.Response(200, json={"data": {"id": self.campaign, "type": "campaign", "attributes": {
                "patron_count": 7, "paid_member_count": 4, "pledge_sum": 2500, "pledge_sum_currency": "USD",
                "currency": "USD", "is_monthly": True, "is_nsfw": self.nsfw}},
                "included": [{"type": "reward", "id": "40", "attributes": {"title": "Sketch club", "amount_cents": 500, "patron_count": 3}},
                             {"type": "reward", "id": "50", "attributes": {"title": "Inner circle", "amount_cents": 1000, "patron_count": 1}}]})
        if p == "/api/posts" and m == "GET":
            data, nxt = self.post_pages[(parse_qs(u.query).get("page[cursor]") or [""])[0]]
            return httpx.Response(200, json={"data": data, "meta": {"pagination": {"cursors": {"next": nxt}}}})
        if p.startswith("/api/posts/") and p.endswith("/comments") and m == "GET":
            key = (p.split("/")[3], (parse_qs(u.query).get("page[cursor]") or [""])[0])
            data, inc, nxt = self.comment_pages[key]
            return httpx.Response(200, json={"data": data, "included": inc, "meta": {"pagination": {"cursors": {"next": nxt}}}})
        if p == "/api/posts" and m == "POST":
            # Live, 13 Nov: a draft made through the API has an empty body and no paywall node.
            self.post = {"edited_at": "t0", "published_at": None, "rule_ids": ["2"],
                         "content_json_string": json.dumps({"type": "doc", "content": []}), "teaser": None,
                         "media_free": False, "scheduled_for": None, "public_at": None}
            return httpx.Response(201, json={"data": {"type": "post", "id": "777"}})
        if p == "/api/posts/777" and m == "GET":
            s = self.post
            return httpx.Response(200, json={"data": {"type": "post", "id": "777", "attributes": {
                "edited_at": s["edited_at"], "published_at": s["published_at"], "content_json_string": s["content_json_string"],
                # Live, 13 Nov: Patreon answers times with milliseconds and an offset
                "scheduled_for": s["scheduled_for"] and s["scheduled_for"] + ".000+00:00",
                "change_visibility_at": s["public_at"] and s["public_at"] + ".000+00:00",
                **({} if self.omit_teaser else {"teaser_text_json_string": s["teaser"]}),
                "is_header_media_free": (self.media_free_on_publish if self.media_free_on_publish is not None and s["published_at"]
                                         else s["media_free"] if self.media_free_stuck is None else self.media_free_stuck)},
                "relationships": {"access_rules": {"data": [{"type": "access-rule", "id": r}
                                                            for r in s["rule_ids"] + self.extra_read_rules]}}}})
        if p == "/api/posts/777" and m == "PATCH":
            a = body["data"]["attributes"]
            if self.outdated_once:
                self.outdated_once = False
                self.post["edited_at"] = "t-other"
                return httpx.Response(400, json={"errors": [{"code_name": "POST_CONTENT_OUTDATED", "detail": "outdated"}]})
            if a.get("edited_at") != self.post["edited_at"]:
                return httpx.Response(400, json={"errors": [{"code_name": "POST_CONTENT_OUTDATED", "detail": "outdated"}]})
            publish = a["tags"]["publish"]
            if not self.keep_old_rules:
                self.post["rule_ids"] = [r["id"] for r in body["data"]["relationships"]["access_rules"]["data"]]
            if a.get("content_json_string"):
                # Live, 13 Nov: Patreon splits the body at the paywall node and stores neither the node nor, as
                # content, what sat above it: that becomes the free preview.
                nodes = json.loads(a["content_json_string"])["content"]
                cut = next((i for i, n in enumerate(nodes) if n.get("type") == "paywallBreakpoint"), None)
                above, below = (nodes[:cut], nodes[cut + 1:]) if cut is not None else ([], nodes)
                if self.teaser_leak or (self.teaser_leak_on_publish and (a["tags"]["publish"] or a.get("scheduled_for"))):
                    above, below = below[:1], below[1:]
                self.post["content_json_string"] = json.dumps({"type": "doc", "content": below})
                self.post["teaser"] = json.dumps({"type": "doc", "content": above})
            if "is_header_media_free" in a:
                self.post["media_free"] = a["is_header_media_free"]
            if self.keep_times:
                for k, mine in (("scheduled_for", "scheduled_for"), ("change_visibility_at", "public_at")):
                    if k in a:
                        self.post[mine] = a[k]
            self.post["edited_at"] = self.post["edited_at"] + "+"
            if a.get("scheduled_for"):
                if self.after_schedule_rules is not None:
                    self.post["rule_ids"] = self.after_schedule_rules
                if self.lose_schedule:
                    raise httpx.ReadTimeout("lost", request=request)
            if publish:
                self.post["published_at"] = "2026-11-13T01:00:00+00:00"
                if self.after_publish_rules is not None:
                    self.post["rule_ids"] = self.after_publish_rules
                if self.lose_publish:
                    raise httpx.ReadTimeout("lost", request=request)
            return httpx.Response(200, json={"data": {"type": "post", "id": "777"}})
        if p == "/api/media" and m == "POST":
            self.media_n += 1
            return httpx.Response(201, json={"data": {"type": "media", "id": str(500 + self.media_n), "attributes": {
                "upload_url": "https://patreon-media.s3-accelerate.amazonaws.com/",
                "upload_parameters": {"key": "k", "X-Amz-Signature": "PLANTED_SIG"}}}})
        if p.startswith("/api/media/"):
            mid = p.rsplit("/", 1)[1]
            return httpx.Response(200, json={"data": {"type": "media", "id": mid, "attributes": {
                "state": self.media_state,
                "display": {"url": f"{self.media_scheme}://{self.media_host}/p/post/777/{mid}/1.png?token-time=1&token-hash=x",
                            "width": 800, "height": 600}}}})
        if p == "/api/collection" and m == "POST":
            if not self.hide_new_collection:
                self.collections.append({"type": "collection", "id": "901", "attributes": {
                    "title": body["data"]["attributes"]["title"], "num_posts": 0}})
            return httpx.Response(201, json={"data": {"type": "collection", "id": "901"}})
        if p == "/api/collection":
            return httpx.Response(200, json={"data": self.collections})
        if p == "/api/members":
            off = int((parse_qs(u.query).get("page[offset]") or ["0"])[0])
            pages = self.members_pages or {0: ([], None)}
            if off not in pages:
                return httpx.Response(500, json={"errors": [{"detail": "boom"}]})
            data, nxt = pages[off]
            return httpx.Response(200, json={"data": data, "meta": {"pagination": {"offsets": {"next": nxt}}}})
        return httpx.Response(404, json={"errors": [{"detail": "not found"}]})

    def transport(self):
        return httpx.MockTransport(self.handler)

    def client(self, cookie=PLANTED_COOKIE):
        return patc.PatClient(cookie, transport=self.transport(), upload_transport=self.transport(), sleep=_no_sleep)

    def calls(self, method=None, path=None):
        return [r for r in self.requests if (method is None or r["method"] == method) and (path is None or r["path"] == path)]


OFFERED = [  # what audience_rules() returns from the stand-in, as saved in pat_tiers
    {"rule_id": "1", "kind": "public", "tier_id": "", "title": "Everyone (public)", "amount_cents": 0},
    {"rule_id": "2", "kind": "patrons", "tier_id": "", "title": "All paying members", "amount_cents": 0},
    {"rule_id": "3", "kind": "tier", "tier_id": "30", "title": "Free", "amount_cents": 0},
    {"rule_id": "4", "kind": "tier", "tier_id": "40", "title": "Sketch club", "amount_cents": 500},
    {"rule_id": "5", "kind": "tier", "tier_id": "50", "title": "Inner circle", "amount_cents": 1000},
]


def make_account(campaign="111", nsfw=False, vanity="inkwolf", tiers=OFFERED) -> int:
    conn = get_connection()
    try:
        aid = adb.get_default_account_id(conn, "pat", create=True)
    finally:
        conn.close()
    config.save_settings({"pat_cookie": PLANTED_COOKIE, "pat_username": vanity})
    if campaign:
        pat_account.record_campaign(aid, {"campaign_id": campaign, "is_nsfw": nsfw}, tiers)
    return aid


@pytest.fixture
def piece(tmp_path):
    """A piece folder with a main image, a General and a Mature render, and story files."""
    for n in ("main.png", "sfw.png", "mature.png", "cover.png"):
        (tmp_path / n).write_bytes(b"\x89PNG fake")
    return tmp_path


def pkg(piece, rating="general", extra=None, pictures=None, cover=None, files=None, problems=None, media="image"):
    main = str(piece / "main.png")
    return StoryUploadPackage(
        story_name="Sample Story", chapter_index=0, chapter_title="", platform="pat", title="Sample Story",
        description="<p>Hello <em>there</em></p>", tags=["sample"], rating=rating,
        file_path=main if media else "", file_type="png" if media else "", media_kind=media or "",
        extra={"audience": "tiers", "tier_ids": ["40"], **(extra or {})},
        pat_files={"piece_dir": str(piece), "pictures": pictures if pictures is not None else ([(main, rating)] if media else []),
                   "cover": cover, "files": files or [], "problems": problems or []})


def poster(fake: FakePatreon, aid: int | None = None) -> patp.PatreonPoster:
    p = patp.PatreonPoster()
    p.account_id = aid
    p._transport = fake.transport()
    p._upload_transport = fake.transport()
    p._sleep = staticmethod(_no_sleep)
    return p


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(patc, "MIN_GAP_S", 0)
    pat_account._checked.clear()


# ── the client (FR-SEC-005, FR-SEC-012) ─────────────────────────────────────

@pytest.mark.parametrize("url", ["http://www.patreon.com/api/x", "https://patreon.com/api/x", "https://evilpatreon.com/x",
                                 "https://www.patreon.com.evil.net/x", "https://www.patreon.com:8443/x"])
def test_pat_client_refuses_foreign_host(url):
    assert not patc.PatClient._host_ok(url)
    assert patc.PatClient._host_ok("https://www.patreon.com/api/x")


@pytest.mark.parametrize("loc", ["https://evilpatreon.com/x", "http://www.patreon.com/x", "https://www.patreon.com:8443/x"])
def test_pat_redirect_off_site_not_followed(loc):
    fake = FakePatreon()
    fake.status_override = ("/api/current_user", 302, b"", {"location": loc})
    with pytest.raises(patc.PatUnavailable):
        run(fake.client().current_campaign())
    assert all(r["host"] == "www.patreon.com" for r in fake.requests)


def test_pat_cookie_jar_scoped_to_www():
    fake = FakePatreon()
    c = fake.client()
    jar = c._http().cookies.jar
    assert {ck.domain for ck in jar} == {"www.patreon.com"}
    run(c.close())


def test_pat_answer_size_cap(monkeypatch):
    monkeypatch.setattr(patc, "MAX_BODY", 1000)
    fake = FakePatreon()
    fake.status_override = ("/api/current_user", 200, b"x", {"content-length": "5000000"})
    with pytest.raises(patc.PatUnavailable, match="too large"):
        run(fake.client().current_campaign())

    def streamed(request):
        return httpx.Response(200, stream=httpx.ByteStream(b"y" * 5000))    # no length header
    c = patc.PatClient(PLANTED_COOKIE, transport=httpx.MockTransport(streamed), sleep=_no_sleep)
    with pytest.raises(patc.PatUnavailable, match="too large"):
        run(c.current_campaign())


def test_pat_challenge_is_not_signed_out():
    for status, headers in ((403, {"cf-mitigated": "challenge"}), (429, {})):
        fake = FakePatreon()
        fake.status_override = ("/api/current_user", status, b"Just a moment", headers)
        with pytest.raises(patc.PatChallenged):
            run(fake.client().current_campaign())


def test_pat_upload_client_sends_no_cookie_or_csrf(piece):
    fake = FakePatreon()
    make_account()
    run(fake.client().create_draft("image_file"))
    mid = run(fake.client().create_media("777", str(piece / "main.png"), "main", "image"))
    assert mid == "501"
    up = [r for r in fake.requests if r["host"] == "patreon-media.s3-accelerate.amazonaws.com"]
    assert len(up) == 1
    hdrs = {k.lower() for k in up[0]["headers"]}
    assert "cookie" not in hdrs and "x-csrf-signature" not in hdrs
    writes = [r for r in fake.requests if r["method"] in ("POST", "PATCH") and r["host"] == "www.patreon.com"]
    assert all(r["headers"].get("x-csrf-signature") == CSRF for r in writes)


@pytest.mark.parametrize("url", ["http://patreon-media.s3-accelerate.amazonaws.com/", "https://evil.example.com/",
                                 "https://patreon-media.s3-accelerate.amazonaws.com:444/", "https://www.patreon.com/"])
def test_pat_upload_host_allowlist(url, piece):
    assert not patc.UploadClient.host_ok(url)

    async def go():
        async with patc.UploadClient(transport=httpx.MockTransport(lambda r: httpx.Response(204))) as up:
            await up.upload(url, {}, str(piece / "main.png"))
    with pytest.raises(patc.PatUnavailable):
        run(go())


def test_pat_upload_follows_no_redirect(piece):
    async def go():
        t = httpx.MockTransport(lambda r: httpx.Response(307, headers={"location": "https://evil.example.com/"}))
        async with patc.UploadClient(transport=t) as up:
            await up.upload("https://patreon-media.s3-accelerate.amazonaws.com/", {}, str(piece / "main.png"))
    with pytest.raises(patc.PatUnavailable, match="HTTP 307"):
        run(go())


def test_pat_errors_carry_no_cookie_or_token(caplog):
    caplog.set_level(logging.DEBUG)
    for status, raw in ((401, b"{}"), (500, json.dumps({"errors": [{"detail": "y" * 2000}]}).encode())):
        fake = FakePatreon()
        fake.status_override = ("/api/posts", status, raw, {})
        with pytest.raises(Exception) as ei:
            run(fake.client().create_draft("text_only"))
        msg = str(ei.value)
        assert "PLANTED" not in msg and CSRF not in msg
        assert len(msg) < 400
    assert "PLANTED_SESSION" not in caplog.text and CSRF not in caplog.text


def test_pat_csrf_never_stored():
    make_account()
    fake = FakePatreon()
    run(fake.client().create_draft("text_only"))
    on_disk = config.SETTINGS_PATH.read_text(encoding="utf-8") + (
        config.VAULT_PATH.read_text(encoding="utf-8") if config.VAULT_PATH.exists() else "")
    assert CSRF not in on_disk and CSRF not in json.dumps(config.get_settings())


def test_pat_unknown_rule_kind_never_offered():
    rules = run(FakePatreon().client().audience_rules("111"))
    assert [r["kind"] for r in rules] == ["public", "patrons", "tier", "tier", "tier"]
    assert {r["rule_id"] for r in rules} == {"1", "2", "3", "4", "5"}
    assert next(r for r in rules if r["tier_id"] == "40")["title"] == "Sketch club"    # tags dropped: text only


def test_pat_members_partial_is_a_failed_read():
    fake = FakePatreon()
    m = {"type": "member", "id": "m1", "attributes": {"patron_status": "active_patron", "full_name": "SecondFur",
                                                       "email": "owner@example.com"}}
    fake.members_pages = {0: ([m], 500)}         # page 2 answers 500
    with pytest.raises(patc.PatPartial):
        run(fake.client().members("111"))
    fake.members_pages = {0: ([m], None)}
    got = run(fake.client().members("111"))
    assert got and "full_name" not in got[0] and "email" not in got[0] and "id" not in got[0]


def test_pat_link_built_from_id():
    assert patc.post_link("777") == "https://www.patreon.com/posts/777"
    assert patc.post_link("../x") == "" and patc.post_link("https://evil.example.com") == ""


# ── editor JSON (T049) ──────────────────────────────────────────────────────

def test_pat_editor_json_rules():
    doc = json.loads(editor_json.to_editor_json(
        "<p>One <em>two</em> <strong>three</strong></p><hr><p>Go <a href='https://example.com'>here</a></p>"
        "<p>a<br>b</p><span>plain</span><script>x</script>", paywall=PAYWALL))
    c = doc["content"]
    assert c[0] == PAYWALL and set(PAYWALL["attrs"]["paywallBreakpointCtaProps"]) == {
        "currencyCode", "isMonetized", "isPaidAccessSelected", "isPaidMembersSelected", "showCtas", "postId"}
    assert c[1]["content"] == [{"type": "text", "text": "One "}, {"type": "text", "text": "two", "marks": [{"type": "italic"}]},
                               {"type": "text", "text": " "}, {"type": "text", "text": "three", "marks": [{"type": "bold"}]}]
    assert c[2] == {"type": "horizontalRule"}
    assert c[3]["content"] == [{"type": "text", "text": "Go "}, {"type": "text", "text": "here", "marks": [
        {"type": "link", "attrs": {"href": "https://example.com", "target": "_blank", "auto": False}}]}]
    assert [n["content"][0]["text"] for n in c[4:7]] == ["a", "b", "plain"]
    assert len(c) == 7
    same = editor_json.to_editor_json("<p>x</p>", paywall=None)
    assert same == editor_json.to_editor_json("<p>x</p>", paywall=None)
    assert "paywallBreakpoint" not in same
    assert editor_json.paywall_node("eur", "9")["attrs"]["paywallBreakpointCtaProps"]["currencyCode"] == "EUR"
    assert editor_json.paywall_node("", "9")["attrs"]["paywallBreakpointCtaProps"]["postId"] == "9"


def test_pat_is_empty_doc():
    e = editor_json.is_empty_doc
    assert e('{"type":"doc","content":[]}') and e('{"type":"doc","content":[{"type":"paragraph"}]}')
    assert not e('{"type":"doc","content":[{"type":"paragraph","content":[{"type":"text","text":"hi"}]}]}')
    assert not e('{"type":"doc","content":[{"type":"image"}]}')
    for bad in (None, "", "not json", "[]", '{"content":[]}'):
        assert not e(bad)


# ── refusal: no network (T028–T032, T045) ───────────────────────────────────

class _NoNetwork(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        raise AssertionError(f"refusal made a request: {request.url}")


@pytest.mark.parametrize("rating", ["", None, "e", "s", "nonsense", "adult", "explicit"])
def test_pat_unrated_counts_as_adult(rating, piece):
    make_account()
    p = patp.PatreonPoster()
    p._transport = _NoNetwork()
    assert "Patreon only takes General and Mature work" in (p.refusal(pkg(piece, rating=rating)) or "")


@pytest.mark.parametrize("rating", ["g", "general", "safe", "mature"])
def test_pat_general_and_mature_pass_the_rating_check(rating, piece):
    make_account()
    pictures = [(str(piece / "main.png"), rating)]
    assert patp.PatreonPoster().refusal(pkg(piece, rating=rating, extra={"tier_ids": ["40"]}, pictures=pictures)) is None


def test_pat_mature_offered_paid_tiers_only(piece):
    make_account()
    p = patp.PatreonPoster()
    for extra in ({"audience": "public"}, {"audience": "patrons"}, {"audience": "tiers", "tier_ids": ["30"]},
                  {"audience": "tiers", "tier_ids": ["40", "30"]}):
        assert p.refusal(pkg(piece, rating="mature", extra=extra)) == patp.MATURE_PAID
    assert patp.all_paid_tier_ids(OFFERED) == ["40", "50"]


def test_pat_refusal_sentences(piece):
    p = patp.PatreonPoster()
    make_account()
    assert p.refusal(pkg(piece, extra={"audience": ""})) == patp.NO_AUDIENCE
    assert p.refusal(pkg(piece, extra={"audience": "tiers", "tier_ids": []})) == patp.NO_AUDIENCE
    assert "no longer exists" in p.refusal(pkg(piece, extra={"tier_ids": ["99"]}))
    assert "up to 50" in p.refusal(pkg(piece, extra={"tier_ids": [str(i) for i in range(51)]}))
    assert p.refusal(pkg(piece, problems=["This story has no EPUB file to attach."])) == "This story has no EPUB file to attach."
    config.save_settings({"pat_campaign_id": ""})
    assert p.refusal(pkg(piece)) == patp.NO_CAMPAIGN


def test_pat_validate_needs_no_tag(piece):
    make_account()
    p = pkg(piece)
    p.tags = []
    assert patp.PatreonPoster().validate(p) == []


def test_pat_under18_refused_on_adult_page(piece, monkeypatch):
    import age_gate
    make_account(nsfw=True)
    monkeypatch.setattr(age_gate, "is_under_18", lambda *a, **k: True)
    assert "18+" in patp.PatreonPoster().refusal(pkg(piece))
    make_account(nsfw=False)
    assert patp.PatreonPoster().refusal(pkg(piece)) is None
    # the age check gets the effective rating: "e" is adult, not general
    assert patp.PatreonPoster().refusal(pkg(piece, rating="e")) is not None


def test_pat_cover_rated_no_higher_than_post(piece, monkeypatch):
    make_account()
    p = patp.PatreonPoster()
    mature_cover = (str(piece / "mature.png"), "mature")
    assert p.refusal(pkg(piece, cover=(str(piece / "mature.png"), "adult"), rating="mature")) == patp.COVER_HIGHER
    assert p.refusal(pkg(piece, cover=mature_cover)) == patp.COVER_HIGHER
    assert p.refusal(pkg(piece, rating="mature", cover=mature_cover,
                         pictures=[(str(piece / "main.png"), "mature")])) == patp.COVER_GENERAL
    monkeypatch.setattr(patp, "BLURRED_COVER_PROVED", True)
    assert p.refusal(pkg(piece, rating="mature", cover=mature_cover, pictures=[(str(piece / "main.png"), "mature")])) is None


def test_pat_picture_rated_above_post_refused(piece):
    make_account()
    pics = [(str(piece / "main.png"), "general"), (str(piece / "mature.png"), "mature")]
    assert patp.PatreonPoster().refusal(pkg(piece, pictures=pics)) == patp.PICTURE_HIGHER


# ── the audience matcher (T033) ─────────────────────────────────────────────

@pytest.mark.parametrize("chosen,seen,ok", [
    (["4"], ["4"], True),
    (["4"], ["4", "5"], True),            # an extra higher-priced tier only
    (["4"], [], False),
    (["4"], ["4", "1"], False),           # an extra public
    (["4"], ["4", "2"], False),           # an extra patrons
    (["4"], ["4", "3"], False),           # an extra free tier
    (["5"], ["5", "4"], False),           # an extra lower-priced tier
    (["4"], ["4", "6"], False),           # non_member (never in the offered list)
    (["4"], ["4", "8"], False),           # min_cents_pledged
    (["4"], ["4", "12345"], False),       # an id not in the fresh list
    (["1"], ["1", "5"], False),           # public chosen: no extras at all
])
def test_pat_audience_match_cases(chosen, seen, ok):
    assert patp.audience_matches(chosen, seen, OFFERED) is ok


# ── post(): order, read-back, outcomes (T034–T048, T050) ────────────────────

def test_pat_publishes_only_after_audience_matches(piece):
    aid = make_account()
    fake = FakePatreon()
    res = run(poster(fake, aid).post(pkg(piece)))
    assert res.success, res.error
    assert res.external_url == "https://www.patreon.com/posts/777" and res.external_id == "777"
    patches = fake.calls("PATCH", "/api/posts/777")
    assert [r["json"]["data"]["attributes"]["tags"]["publish"] for r in patches] == [False, True]
    seq = [(r["method"], r["path"]) for r in fake.requests if r["host"] == "www.patreon.com" and r["path"] != "/home"]
    i_draft = seq.index(("POST", "/api/posts"))
    i_media = seq.index(("POST", "/api/media"))
    i_save = seq.index(("PATCH", "/api/posts/777"))
    assert i_draft < i_media < i_save
    assert seq[i_save + 1] == ("GET", "/api/posts/777")             # read back before the publish
    assert seq[-1] == ("GET", "/api/posts/777")                     # and after it


def test_pat_audience_sent_in_included(piece):
    aid = make_account()
    fake = FakePatreon()
    run(poster(fake, aid).post(pkg(piece)))
    for r in fake.calls("PATCH", "/api/posts/777"):
        body = r["json"]
        assert body["data"]["relationships"]["access_rules"]["data"] == [{"type": "access-rule", "id": "4"}]
        assert {"type": "access-rule", "id": "4", "attributes": {}} in body["included"]


def test_pat_mismatch_stays_draft(piece):
    aid = make_account()
    fake = FakePatreon()
    fake.keep_old_rules = True
    res = run(poster(fake, aid).post(pkg(piece)))
    assert not res.success and patp.LEFT_DRAFT in res.error and "All paying members" in res.error
    assert all(r["json"]["data"]["attributes"]["tags"]["publish"] is False for r in fake.calls("PATCH", "/api/posts/777"))
    assert fake.post["published_at"] is None


def test_pat_mismatch_after_publish_says_live(piece):
    aid = make_account()
    fake = FakePatreon()
    fake.after_publish_rules = ["1"]
    res = run(poster(fake, aid).post(pkg(piece)))
    # Live is live: success with a warning, so the manager never retries it into a second post.
    assert res.success and "live on Patreon" in res.needs_attention and "not the tiers picked" in res.needs_attention
    assert res.external_url == "https://www.patreon.com/posts/777"
    assert not fake.calls("DELETE")


def test_pat_send_time_reread_refuses_changed_tiers(piece):
    aid = make_account()
    fake = FakePatreon()
    fake.tiers_gone = True                          # tier 40 deleted since the form was filled
    res = run(poster(fake, aid).post(pkg(piece)))
    assert not res.success and "no longer exists" in res.error
    assert not fake.calls("POST", "/api/posts")
    assert {t["tier_id"] for t in pat_account.values(aid)["tiers"]} == {"", "30"}   # pat_tiers refreshed


def test_pat_save_sends_edited_at(piece):
    aid = make_account()
    fake = FakePatreon()
    fake.outdated_once = True
    res = run(poster(fake, aid).post(pkg(piece)))
    assert res.success, res.error
    patches = fake.calls("PATCH", "/api/posts/777")
    assert len(patches) == 3                         # the outdated one, its single retry, the publish
    assert patches[1]["json"]["data"]["attributes"]["edited_at"] == "t-other"


def test_pat_save_outdated_twice_fails_naming_step(piece):
    aid = make_account()
    fake = FakePatreon()
    real = fake.handler

    def always_outdated(request):
        if request.method == "PATCH":
            fake.outdated_once = True
        return real(request)
    p = poster(fake, aid)
    p._transport = httpx.MockTransport(always_outdated)
    res = run(p.post(pkg(piece)))
    assert not res.success and "saving the draft" in res.error and patp.LEFT_DRAFT in res.error


def test_pat_lost_publish_answer_checks_draft(piece):
    aid = make_account()
    fake = FakePatreon()
    fake.lose_publish = True
    res = run(poster(fake, aid).post(pkg(piece)))
    assert res.success, res.error                    # published and matching
    assert len(fake.calls("POST", "/api/posts")) == 1


def test_pat_lost_publish_unreadable_is_unconfirmed(piece):
    aid = make_account()
    fake = FakePatreon()
    fake.lose_publish = True
    real = fake.handler
    state = {"published": False}

    def h(request):
        if request.method == "GET" and request.url.path == "/api/posts/777" and state["published"]:
            return httpx.Response(500, json={})
        if request.method == "PATCH" and json.loads(request.content)["data"]["attributes"]["tags"]["publish"]:
            state["published"] = True
        return real(request)
    p = poster(fake, aid)
    p._transport = httpx.MockTransport(h)
    res = run(p.post(pkg(piece)))
    assert not res.success and res.unconfirmed


def test_pat_locked_payload_has_no_teaser(piece):
    aid = make_account()
    fake = FakePatreon()
    run(poster(fake, aid).post(pkg(piece, rating="mature", pictures=[(str(piece / "main.png"), "mature")])))
    for r in fake.calls("PATCH", "/api/posts/777"):
        a = r["json"]["data"]["attributes"]
        assert json.loads(a["content_json_string"])["content"][0] == PAYWALL
        assert a["teaser_text"] == "" and a["is_paid"] is False and a["allow_preview_in_rss"] is False


def test_pat_free_preview_left_stays_draft(piece):
    """A locked post publishes only when Patreon's stored free preview is empty."""
    aid = make_account()
    fake = FakePatreon()
    fake.teaser_leak = True
    res = run(poster(fake, aid).post(pkg(piece)))
    assert not res.success and patp.LEFT_DRAFT in res.error and "free preview" in res.error
    assert all(not c["json"]["data"]["attributes"]["tags"]["publish"] for c in fake.calls("PATCH", "/api/posts/777"))
    assert fake.post["published_at"] is None


def test_pat_locked_post_stores_empty_preview(piece):
    aid = make_account()
    fake = FakePatreon()
    res = run(poster(fake, aid).post(pkg(piece)))
    assert res.success, res.error
    assert editor_json.is_empty_doc(fake.post["teaser"]) and "Hello" in fake.post["content_json_string"]
    a = fake.calls("PATCH", "/api/posts/777")[0]["json"]["data"]["attributes"]
    assert a["is_header_media_free"] is False
    q = fake.calls("GET", "/api/posts/777")[0]["query"]["fields[post]"][0]
    assert "teaser_text_json_string" in q


def test_pat_public_post_sends_no_marker(piece):
    aid = make_account()
    fake = FakePatreon()
    res = run(poster(fake, aid).post(pkg(piece, extra={"audience": "public", "tier_ids": []})))
    assert res.success, res.error
    a = fake.calls("PATCH", "/api/posts/777")[0]["json"]["data"]["attributes"]
    assert "paywallBreakpoint" not in a["content_json_string"] and "is_header_media_free" not in a


def test_pat_unblurred_only_with_general_cover(piece):
    cover = (str(piece / "cover.png"), "general")
    for nsfw, blurred in ((False, False), (True, True)):
        aid = make_account(nsfw=nsfw)
        fake = FakePatreon(nsfw=nsfw)
        res = run(poster(fake, aid).post(pkg(piece, cover=cover)))
        assert res.success, res.error
        a = fake.calls("PATCH", "/api/posts/777")[0]["json"]["data"]["attributes"]
        assert a["is_preview_blurred"] is blurred and a["custom_preview_media_id"]


def test_pat_post_refused_when_campaign_changed(piece):
    aid = make_account(campaign="222")
    fake = FakePatreon(campaign="111", vanity="secondfur")
    res = run(poster(fake, aid).post(pkg(piece)))
    assert not res.success and "secondfur" in res.error and "222" in res.error
    assert not fake.calls("POST", "/api/posts")


def test_pat_never_post_account_refused(monkeypatch):
    from posting import manager
    aid = make_account()
    monkeypatch.setattr(adb, "never_post_ids", lambda *a, **k: {aid})
    with pytest.raises(ValueError):
        manager._get_poster("pat", aid)
    with pytest.raises(ValueError):
        manager._get_poster("pat", None)


def test_pat_media_upload_flow(piece):
    aid = make_account()
    fake = FakePatreon()
    res = run(poster(fake, aid).post(pkg(piece, cover=(str(piece / "cover.png"), "general"),
                                         files=[str(piece / "main.png")])))
    assert res.success, res.error
    made = [r["json"]["data"]["attributes"] for r in fake.calls("POST", "/api/media")]
    assert [(m["owner_relationship"], m["media_type"]) for m in made] == [
        ("main", "image"), ("custom-thumbnail", "image"), ("attachment", "file")]
    assert all(m["state"] == "pending_upload" and m["owner_id"] == "777" and m["owner_type"] == "post"
               and m["size_bytes"] > 0 for m in made)


def test_pat_media_failed_leaves_draft(piece):
    aid = make_account()
    fake = FakePatreon()
    fake.media_state = "failed"
    res = run(poster(fake, aid).post(pkg(piece)))
    assert not res.success and patp.LEFT_DRAFT in res.error and "upload" in res.error


def test_pat_several_pictures_in_order(piece):
    aid = make_account()
    fake = FakePatreon()
    pics = [(str(piece / "main.png"), "general"), (str(piece / "sfw.png"), "general")]
    res = run(poster(fake, aid).post(pkg(piece, pictures=pics)))
    assert res.success, res.error
    pm = fake.calls("PATCH", "/api/posts/777")[0]["json"]["data"]["attributes"]["post_metadata"]
    assert pm["image_order"] == ["501", "502"]


def test_pat_notify_switch(piece):
    for notify, want in ((False, False), (True, True), (None, True)):
        aid = make_account()
        fake = FakePatreon()
        extra = {} if notify is None else {"notify": notify}
        run(poster(fake, aid).post(pkg(piece, extra=extra)))
        pub = fake.calls("PATCH", "/api/posts/777")[-1]["json"]
        assert pub["meta"]["send_notifications"] is want
        assert pub["data"]["attributes"]["post_metadata"]["send_notifications"] is want


def test_pat_collection_choice(piece):
    aid = make_account()
    fake = FakePatreon()
    res = run(poster(fake, aid).post(pkg(piece, extra={"collection": "900"})))
    assert res.success, res.error
    assert fake.calls("PATCH", "/api/posts/777")[0]["json"]["data"]["relationships"]["collections"]["data"] == [
        {"type": "collection", "id": "900"}]
    fake = FakePatreon()
    res = run(poster(fake, aid).post(pkg(piece, extra={"collection": "901"})))
    assert not res.success and "no longer exists" in res.error and not fake.calls("POST", "/api/posts")
    fake = FakePatreon()
    run(poster(fake, aid).post(pkg(piece, extra={"collection": "none"})))
    assert "collections" not in fake.calls("PATCH", "/api/posts/777")[0]["json"]["data"]["relationships"]


def test_pat_story_collection_uses_saved_id_only(piece):
    aid = make_account()
    pat_account.save(aid, story_collections={"Sample Story": "900"})
    fake = FakePatreon()
    res = run(poster(fake, aid).post(pkg(piece, extra={"collection": "story"})))
    assert res.success
    assert fake.calls("PATCH", "/api/posts/777")[0]["json"]["data"]["relationships"]["collections"]["data"][0]["id"] == "900"
    pat_account.save(aid, story_collections={})
    fake = FakePatreon()
    res = run(poster(fake, aid).post(pkg(piece, extra={"collection": "story"})))
    assert res.success and "without a collection" in res.extra.get("note", "")


def test_pat_paths_rechecked_inside_piece(piece, tmp_path_factory):
    aid = make_account()
    other = tmp_path_factory.mktemp("other") / "settings.json"
    other.write_text("{}")
    fake = FakePatreon()
    res = run(poster(fake, aid).post(pkg(piece, files=[str(other)])))
    assert not res.success and "inside" in res.error and not fake.requests


def test_pat_story_chapter_body_from_file(piece):
    aid = make_account()
    chapter = piece / "Chapters" / "SoFurry_HTML"
    chapter.mkdir(parents=True)
    (chapter / "c1.html").write_text("<p>Chapter <b>one</b></p>", encoding="utf-8")
    p = pkg(piece, media=None)
    p.file_path = str(chapter / "c1.html")
    fake = FakePatreon()
    res = run(poster(fake, aid).post(p))
    assert res.success, res.error
    a = fake.calls("PATCH", "/api/posts/777")[0]["json"]["data"]["attributes"]
    assert a["post_type"] == "text_only" and "Chapter " in a["content_json_string"]
    assert fake.calls("POST", "/api/posts")[0]["json"]["data"]["attributes"]["post_type"] == "text_only"


# ── the readers: seven keys, resolved paths (T039, T043, T044, T046) ─────────

class _Art:
    def __init__(self, root, variants):
        self.path, self.variants, self.rating = str(root), variants, "adult"


def test_pat_choice_keys_resolve_inside_piece(piece):
    from posting import pat_choices
    saved = {"audience": "tiers", "tier_ids": ["40"], "pat_file_paths": ["/data/settings.json"], "evil": 1, "notify": False}
    assert pat_choices.pick(saved) == {"audience": "tiers", "tier_ids": ["40"], "notify": False}
    art = _Art(piece, [{"key": "sfw", "image": "sfw.png", "rating": "general"},
                       {"key": "up", "image": "../x.png", "rating": "general"}])
    out = pat_choices.for_artwork(art, str(piece / "main.png"), "general",
                                  {"variant_keys": ["sfw", "up", "nope"], "cover": "variant:../x"})
    assert [p for p, _ in out["pictures"]] == [str(piece / "main.png"), str((piece / "sfw.png").resolve())]
    assert len(out["problems"]) == 3                 # missing render, unknown key, unknown cover
    out = pat_choices.for_artwork(art, str(piece / "main.png"), "general", {"cover": "/etc/passwd"})
    assert out["cover"] is None and out["problems"]


def test_pat_cover_main_is_the_render_sent(piece):
    from posting import pat_choices
    art = _Art(piece, [{"key": "sfw", "image": "sfw.png", "rating": "general"}])
    out = pat_choices.for_artwork(art, str(piece / "sfw.png"), "general", {"cover": "main"})
    assert out["cover"] == (str(piece / "sfw.png"), "general")


def test_pat_story_files_one_per_format(piece):
    from posting import pat_choices

    class S:
        path = str(piece)
    (piece / "PDF").mkdir()
    (piece / "Chapters" / "PDF").mkdir(parents=True)
    (piece / "Chapters" / "PDF" / "ch1.pdf").write_bytes(b"%PDF")
    out = pat_choices.for_story(S, None, "general", {"files": ["pdf", "epub", "docx"]})
    assert "no PDF file" in out["problems"][0] and "no EPUB file" in out["problems"][1] and "docx" in out["problems"][2]
    (piece / "PDF" / "Sample_Story.pdf").write_bytes(b"%PDF")
    out = pat_choices.for_story(S, None, "general", {"files": ["pdf"]})
    assert out["files"] == [str((piece / "PDF" / "Sample_Story.pdf").resolve())] and not out["problems"]
    (piece / "PDF" / "Other.pdf").write_bytes(b"%PDF")
    out = pat_choices.for_story(S, None, "general", {"files": ["pdf"]})
    assert "more than one PDF" in out["problems"][0] and not out["files"]


def test_pat_story_cover_is_the_story_cover(piece):
    from posting import pat_choices

    class S:
        path = str(piece)
    out = pat_choices.for_story(S, "cover.png", "mature", {"cover": "story"})
    assert out["cover"] == (str((piece / "cover.png").resolve()), "mature")
    assert pat_choices.for_story(S, "../evil.png", "general", {"cover": "story"})["problems"]


# ── sign-in on every save path (T010, T017) ─────────────────────────────────

@pytest.fixture
def fake_net(monkeypatch):
    fake = FakePatreon(vanity="secondfur")
    real_init = patc.PatClient.__init__

    def init(self, cookie="", **kw):
        kw.setdefault("transport", fake.transport())
        kw.setdefault("upload_transport", fake.transport())
        kw.setdefault("sleep", _no_sleep)
        real_init(self, cookie, **kw)
    monkeypatch.setattr(patc.PatClient, "__init__", init)
    return fake


def test_pat_who_refuses_other_page(fake_net):
    from routes import signin_helper_api as sh
    ok, who, why = run(sh._who("pat", {"pat_cookie": PLANTED_COOKIE}, "inkwolf"))
    assert not ok and who == "secondfur" and "inkwolf" in why and "secondfur" in why
    assert not pat_account.was_checked(PLANTED_COOKIE)
    ok, who, _ = run(sh._who("pat", {"pat_cookie": PLANTED_COOKIE}, "SecondFur"))      # case-insensitive
    assert ok and pat_account.was_checked(PLANTED_COOKIE)


def test_pat_direct_signin_checks_page_name(fake_net):
    from auth import browser_login as bl
    why = run(bl.whose_session_problem("pat", {"pat_cookie": PLANTED_COOKIE, "pat_username": "inkwolf"}, None))
    assert "secondfur" in why and "inkwolf" in why
    with pytest.raises(ValueError):           # the save refuses a cookie that wasn't checked
        bl._save_browser_creds("pat", {"pat_cookie": PLANTED_COOKIE, "pat_username": "inkwolf"}, None)
    assert not config.get_settings().get("pat_cookie")
    creds = {"pat_cookie": PLANTED_COOKIE, "pat_username": "secondfur"}
    assert run(bl.whose_session_problem("pat", creds, None)) == ""
    bl._save_browser_creds("pat", creds, None)
    s = config.get_settings()
    assert s.get("pat_cookie") == PLANTED_COOKIE and s.get("pat_campaign_id") == "111"
    assert json.loads(s["pat_tiers"])[0]["kind"] == "public"


def test_pat_accounts_form_checks_page_name(fake_net):
    from fastapi.testclient import TestClient
    import dashboard
    c = TestClient(dashboard.app, client=("127.0.0.1", 50000))
    before = len([a for a in adb.list_accounts(get_connection(), platform="pat")])
    r = c.post("/api/accounts", json={"platform": "pat", "label": "Mine",
                                      "credentials": {"pat_cookie": PLANTED_COOKIE, "pat_username": "inkwolf"}})
    assert r.status_code == 409 and "secondfur" in r.json()["detail"]
    assert len(adb.list_accounts(get_connection(), platform="pat")) == before     # no account row left behind
    r = c.post("/api/accounts", json={"platform": "pat", "label": "Mine",
                                      "credentials": {"pat_cookie": PLANTED_COOKIE, "pat_username": "secondfur"}})
    assert r.status_code == 200, r.text
    aid = r.json()["account"]["account_id"]
    assert pat_account.values(aid)["campaign_id"] == "111"


def test_pat_campaign_ids_from_server_only(fake_net):
    """A sign-in body carrying its own campaign id / tiers never writes them (FR-SEC-006)."""
    from auth import browser_login as bl
    creds = {"pat_cookie": PLANTED_COOKIE, "pat_username": "secondfur"}
    assert set(bl.PLATFORM_LOGIN["pat"]["extract"]({"session_id": "v"}, "https://www.patreon.com/home")) == {"pat_cookie"}
    assert "pat_campaign_id" not in config.PLATFORM_CREDENTIAL_FIELDS["pat"]
    run(bl.whose_session_problem("pat", creds, None))
    bl._save_browser_creds("pat", creds, None)
    assert config.get_settings()["pat_campaign_id"] == "111"
    with pytest.raises(ValueError):
        pat_account.save(make_account(campaign=""), cookie="x")


# ── routes (T016) ───────────────────────────────────────────────────────────

def _client():
    from fastapi.testclient import TestClient
    import dashboard
    return TestClient(dashboard.app, client=("127.0.0.1", 50000))


def test_pat_routes_refuse_other_platform_account():
    conn = get_connection()
    try:
        th = adb.get_default_account_id(conn, "th", create=True)
    finally:
        conn.close()
    c = _client()
    for method, url in (("get", f"/api/pat/auth/status?account_id={th}"), ("get", f"/api/pat/tiers?account_id={th}"),
                        ("post", f"/api/pat/auth/disconnect?account_id={th}")):
        assert getattr(c, method)(url).status_code == 404
    assert c.put("/api/pat/default-audience", json={"account_id": th, "audience": "public"}).status_code == 404


def test_pat_default_audience_checks_tiers(monkeypatch):
    aid = make_account()
    c = _client()
    assert c.put("/api/pat/default-audience", json={"account_id": aid, "audience": "tiers", "tier_ids": ["99"]}).status_code == 400
    assert c.put("/api/pat/default-audience", json={"account_id": aid, "audience": "tiers", "tier_ids": ["40"],
                                                    "notify": False}).status_code == 200
    assert pat_account.values(aid)["default_audience"] == {"audience": "tiers", "tier_ids": ["40"], "notify": False}
    monkeypatch.setattr(adb, "never_post_ids", lambda *a, **k: {aid})
    assert c.put("/api/pat/default-audience", json={"account_id": aid, "audience": "public"}).status_code == 404


def test_pat_tiers_route_marks_free(fake_net):
    aid = make_account(vanity="secondfur")
    r = _client().get(f"/api/pat/tiers?account_id={aid}")
    assert r.status_code == 200, r.text
    rules = r.json()["rules"]
    assert [x["kind"] for x in rules] == ["public", "patrons", "tier", "tier", "tier"]
    assert [x["free"] for x in rules if x["kind"] == "tier"] == [True, False, False]


def test_pat_disconnect_clears_all_keys():
    aid = make_account()
    pat_account.save(aid, default_audience={"audience": "public"}, story_collections={"Sample Story": "900"})
    r = _client().post(f"/api/pat/auth/disconnect?account_id={aid}")
    assert r.status_code == 200
    s = config.get_settings()
    assert not [k for k in s if k.startswith("pat_") or "_pat_" in k]


def test_pat_open_instance_locks_routes():
    import dashboard
    assert "/api/pat/" in dashboard._SENSITIVE_WHEN_OPEN_PREFIXES
    assert "/api/supporters/" in dashboard._SENSITIVE_WHEN_OPEN_PREFIXES


# ── registry rows (T008, T009, T014) ────────────────────────────────────────

def test_pat_registry_rows():
    import age_gate
    from auth import browser_login as bl
    from polling import fa_standdown
    # SubscribeStar ("ssa", adult only) and Ko-fi rows come with their own phases: the table lists registered sites only.
    assert age_gate.SITE_AGES["pat"]["adult_only"] is False and age_gate.SITE_AGES["pat"]["checked"] is False
    assert fa_standdown._SITES["pat"] == "Patreon"
    assert "pat_cookie" in config.CREDENTIAL_FIELDS and config.PLATFORM_CREDENTIAL_FIELDS["pat"] == ["pat_username", "pat_cookie"]
    assert bl.identity_keys("pat") == {"pat_username"}
    for k in ("pat_campaign_id", "pat_is_nsfw", "pat_tiers"):
        assert k not in config.PLATFORM_CREDENTIAL_FIELDS["pat"]


def test_pat_poll_challenge_starts_standdown(monkeypatch):
    from polling import fa_standdown, pat_poller
    make_account()
    fake = FakePatreon()
    fake.status_override = ("/api/current_user", 429, b"", {})
    monkeypatch.setattr(pat_poller, "_transport", fake.transport())
    with pytest.raises(patc.PatChallenged):
        run(pat_poller.run_pat_poll_cycle())
    assert fa_standdown.remaining("pat") > 0


def test_pat_default_public_never_carries_mature(piece):
    """FR-012 / FR-SEC-003: a piece with no audience of its own takes the account's default, and the rating rules
    still apply: a default of public on a piece re-rated Mature is refused."""
    aid = make_account()
    pat_account.save(aid, default_audience={"audience": "public", "tier_ids": [], "notify": False})
    p = patp.PatreonPoster()
    p.account_id = aid
    no_choice = pkg(piece, extra={"audience": "", "tier_ids": []})
    assert p.refusal(no_choice) is None
    assert patp.choices_for(no_choice, pat_account.values(aid))["notify"] is False
    assert p.refusal(pkg(piece, rating="mature", extra={"audience": "", "tier_ids": []},
                         pictures=[(str(piece / "main.png"), "mature")])) == patp.MATURE_PAID


def test_pat_scheduled_post_reads_choices_from_piece(tmp_path):
    """The send-time read takes the Patreon keys from the piece itself (artwork categories["pat"])."""
    import json as _json
    from posting import artwork_reader
    folder = tmp_path / "Sample Piece"
    folder.mkdir()
    (folder / "main.png").write_bytes(b"\x89PNG fake")
    (folder / "artwork.json").write_text(_json.dumps({
        "title": "Sample Piece", "rating": "general", "image": "main.png", "tags": {"core": ["sample"]},
        "categories": {"pat": {"audience": "tiers", "tier_ids": ["40"], "pat_file_paths": ["/data/settings.json"]}},
    }), encoding="utf-8")
    config.save_settings({"artwork_archive_path": str(tmp_path)})
    art = artwork_reader.load_artwork("Sample Piece")
    package = artwork_reader.build_artwork_package(art, "pat")
    assert package.extra.get("audience") == "tiers" and package.extra.get("tier_ids") == ["40"]
    assert "pat_file_paths" not in package.extra
    assert package.pat_files["piece_dir"] and package.pat_files["pictures"]


# ── gaps named in tasks.md (T009, T010, T030, T038, T039, T044, T045, T070, T080–T083) ──

def test_pat_refusal_makes_no_request(piece, monkeypatch):
    """refusal() and validate() read only the package and the saved values: any client built fails the test."""
    aid = make_account()

    def boom(*_a, **_k):
        raise AssertionError("refusal/validate must not reach Patreon")
    monkeypatch.setattr(patc.PatClient, "__init__", boom)
    p = patp.PatreonPoster()
    p.account_id = aid
    for package in (pkg(piece), pkg(piece, rating="adult"), pkg(piece, rating="mature", extra={"audience": "public"}),
                    pkg(piece, extra={"tier_ids": ["99"]})):
        p.refusal(package)
        p.validate(package)


def test_pat_retry_rewrites_audience(piece):
    """A save Patreon calls outdated is re-sent with the audience again, and the audience is read back after it."""
    aid = make_account()
    fake = FakePatreon()
    fake.outdated_once = True
    res = run(poster(fake, aid).post(pkg(piece)))
    assert res.success, res.error
    first, second = fake.calls("PATCH", "/api/posts/777")[:2]
    for call in (first, second):
        assert call["json"]["data"]["relationships"]["access_rules"]["data"] == [{"type": "access-rule", "id": "4"}]
        assert any(i.get("type") == "access-rule" and i.get("id") == "4" for i in call["json"].get("included") or [])
    order = [(r["method"], r["path"]) for r in fake.requests]
    second_patch = order.index(("PATCH", "/api/posts/777")) + 1
    assert ("GET", "/api/posts/777") in order[second_patch:]


def test_pat_refusal_on_schedule_and_retry(piece):
    """The manager's live, scheduled and retry paths all call poster.refusal() (test_rating_gate pins the call
    sites); for Patreon that is PatreonPoster's own, so its rules hold on every path."""
    from posting.manager import _get_poster
    p = _get_poster("pat")
    assert type(p).refusal is patp.PatreonPoster.refusal
    assert p.refusal(pkg(piece, rating="adult")) == patp.ADULT_REFUSED.format(r="adult")


def test_pat_story_files_attached(piece):
    aid = make_account()
    (piece / "PDF").mkdir()
    pdf = piece / "PDF" / "Sample_Story.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    fake = FakePatreon()
    res = run(poster(fake, aid).post(pkg(piece, files=[str(pdf)])))
    assert res.success, res.error
    made = [c["json"]["data"]["attributes"] for c in fake.calls("POST", "/api/media")]
    assert ("attachment", "file") in [(m["owner_relationship"], m["media_type"]) for m in made]


def test_pat_missing_story_file_refused(piece):
    aid = make_account()
    fake = FakePatreon()
    res = run(poster(fake, aid).post(pkg(piece, problems=["This story has no EPUB file to attach."])))
    assert not res.success and "no EPUB file" in res.error and not fake.requests


def test_pat_cover_sets_custom_preview(piece):
    aid = make_account()
    fake = FakePatreon()
    run(poster(fake, aid).post(pkg(piece, cover=(str(piece / "cover.png"), "general"))))
    thumb = [c for c in fake.calls("POST", "/api/media")
             if c["json"]["data"]["attributes"]["owner_relationship"] == "custom-thumbnail"]
    assert len(thumb) == 1
    assert fake.calls("PATCH", "/api/posts/777")[0]["json"]["data"]["attributes"]["custom_preview_media_id"]
    fake = FakePatreon()
    run(poster(fake, aid).post(pkg(piece, cover=None)))
    assert "custom_preview_media_id" not in fake.calls("PATCH", "/api/posts/777")[0]["json"]["data"]["attributes"]


def test_pat_secrets_in_vault():
    import datamap
    from auth import browser_login
    assert "pat_cookie" in config.PLATFORM_CREDENTIAL_FIELDS["pat"]
    assert "pat_cookie" in {e.name for e in datamap.ENTRIES if e.kind == "setting" and e.vault}
    assert browser_login.identity_keys("pat") == {"pat_username"}
    assert browser_login.saved_keys("pat") == {"pat_cookie"}
    extracted = browser_login.PLATFORM_LOGIN["pat"]["extract"]({"session_id": "x"}, "https://www.patreon.com/home")
    for k in ("pat_campaign_id", "pat_is_nsfw", "pat_tiers"):
        assert k not in config.PLATFORM_CREDENTIAL_FIELDS["pat"] and k not in extracted


def test_pat_handover_body_cannot_set_campaign():
    """A hand-over or browser-login body may carry only the cookie and the page name (FR-SEC-006)."""
    from auth import browser_login
    browser_login.check_creds("pat", {"pat_cookie": "session_id=x", "pat_username": "inkwolf"})
    for extra in ({"pat_campaign_id": "111"}, {"pat_tiers": "[]"}, {"pat_is_nsfw": "true"}):
        with pytest.raises(ValueError):
            browser_login.check_creds("pat", {"pat_cookie": "session_id=x", **extra})


async def _with(fake, fn):
    async with fake.client() as c:
        return await fn(c)


def test_pat_post_counts():
    fake = FakePatreon()
    fake.post_pages = {"": ([{"type": "post", "id": "777", "attributes": {"title": "<i>Sample</i>", "like_count": 4,
                                                                         "comment_count": 2, "url": "https://evil.example"}}], "c2"),
                       "c2": ([{"type": "post", "id": "778", "attributes": {"like_count": -3}}], "c2")}   # cursor stuck
    out = run(_with(fake, lambda c: c.posts("111")))
    assert [p["submission_id"] for p in out] == ["777", "778"]
    assert out[0]["link"] == "https://www.patreon.com/posts/777" and out[0]["title"] == "Sample"
    assert out[1]["favorites_count"] == 0
    q = fake.calls("GET", "/api/posts")[0]["query"]
    assert q["filter[campaign_id]"] == ["111"] and q["filter[is_draft]"] == ["false"]
    assert q["fields[post]"] == ["title,like_count,comment_count,published_at,post_type"]
    assert len(fake.calls("GET", "/api/posts")) == 2                    # the stuck cursor stopped it


def test_pat_post_page_cap(monkeypatch):
    monkeypatch.setattr(patc, "MAX_POST_PAGES", 3)
    fake = FakePatreon()
    fake.post_pages = {(f"c{i}" if i else ""): ([{"type": "post", "id": str(800 + i), "attributes": {}}], f"c{i + 1}")
                       for i in range(10)}
    out = run(_with(fake, lambda c: c.posts("111")))
    assert len(out) == 3 and len(fake.calls("GET", "/api/posts")) == 3


def test_pat_comment_keeps_only_name_text_ids():
    fake = FakePatreon()
    fake.comment_pages[("777", "")][0][0]["attributes"]["body"] = "<p>Hi</p>" + "x" * 9000
    out = run(_with(fake, lambda c: c.comments("777")))
    first = out[0]
    assert set(first) == {"comment_id", "author", "body", "commented_at", "is_by_creator", "permalink"}
    assert first["author"] == "SecondFur" and first["body"].startswith("Hi") and len(first["body"]) <= patc.COMMENT_MAX
    assert "owner@example.com" not in json.dumps(out)
    q = fake.calls("GET", "/api/posts/777/comments")[0]["query"]
    assert q["fields[comment]"] == ["body,created,is_by_creator"] and q["fields[user]"] == ["full_name"]


def test_pat_comment_link_built_from_ids():
    fake = FakePatreon()
    out = run(_with(fake, lambda c: c.comments("777")))
    assert {c["permalink"] for c in out} == {"https://www.patreon.com/posts/777"}


def test_pat_comment_page_cap(monkeypatch):
    monkeypatch.setattr(patc, "MAX_COMMENT_PAGES", 2)
    fake = FakePatreon()
    fake.comment_pages = {("777", (f"c{i}" if i else "")): (
        [{"type": "comment", "id": str(70 + i), "attributes": {"body": "hi"}}], [], f"c{i + 1}") for i in range(6)}
    assert len(run(_with(fake, lambda c: c.comments("777")))) == 2
    fake.comment_pages = {("777", ""): ([{"type": "comment", "id": "70", "attributes": {}}], [], "c1"),
                          ("777", "c1"): ([{"type": "comment", "id": "71", "attributes": {}}], [], "c1")}
    assert len(run(_with(fake, lambda c: c.comments("777")))) == 2   # the stuck cursor stopped it


def _poll_with(monkeypatch, fake, aid):
    from polling import pat_poller
    monkeypatch.setattr(pat_poller, "_transport", fake.transport())
    return run(pat_poller.run_pat_poll_cycle(aid))


def test_pat_poller_sends_only_gets(monkeypatch):
    aid = make_account()
    fake = FakePatreon()
    _poll_with(monkeypatch, fake, aid)
    paths = {r["path"] for r in fake.requests}
    assert {"/api/current_user", "/api/campaigns/111", "/api/members", "/api/posts", "/api/posts/777/comments"} <= paths
    assert {r["method"] for r in fake.requests} == {"GET"}


def test_pat_comments_include_replies(monkeypatch):
    """Top-level plus replies reach the Inbox, the creator's own reply is marked as theirs, and a second poll adds
    nothing; a comment gone from Patreon stays in the Inbox."""
    aid = make_account()
    _poll_with(monkeypatch, FakePatreon(), aid)
    conn = get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM platform_comments WHERE platform = 'pat' ORDER BY comment_id")]
    finally:
        conn.close()
    assert [r["comment_id"] for r in rows] == ["61", "62"]
    assert [bool(r["is_own"]) for r in rows] == [False, True]
    gone = FakePatreon()
    gone.comment_pages = {("777", ""): ([], [], None)}               # both gone from Patreon now
    _poll_with(monkeypatch, gone, aid)
    conn = get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM platform_comments WHERE platform = 'pat'").fetchone()[0] == 2
    finally:
        conn.close()


# ── hidden details out (spec 071) for every picture Patreon sends ───────────

def test_pat_uploads_the_cleaned_copies(piece, tmp_path_factory):
    aid = make_account()
    clean_dir = tmp_path_factory.mktemp("pp_clean")
    copy = clean_dir / "main.png"
    copy.write_bytes(b"CLEANED-COPY-BYTES")
    p = pkg(piece, cover=(str(piece / "cover.png"), "general"))
    p.pat_files["upload"] = {str(piece / "main.png"): str(copy), str(piece / "cover.png"): str(copy)}
    fake = FakePatreon()
    res = run(poster(fake, aid).post(p))
    assert res.success, res.error
    sent = [r["raw"] for r in fake.requests if r["host"] == "patreon-media.s3-accelerate.amazonaws.com"]
    assert len(sent) == 2 and all(b"CLEANED-COPY-BYTES" in b and b"PNG fake" not in b for b in sent)


def test_pat_clean_run_cleans_every_picture_and_the_cover(tmp_path):
    from posting import metadata_strip as ms
    from tests import _metadata_fixtures as fx
    for n in ("main.jpg", "extra.jpg", "cover.jpg"):
        (tmp_path / n).write_bytes(fx.gps_jpeg())
    assert any(v in (tmp_path / "main.jpg").read_bytes() for v in fx.FAKE_VALUES)
    p = pkg(tmp_path, pictures=[(str(tmp_path / "main.jpg"), "general"), (str(tmp_path / "extra.jpg"), "general")],
            cover=(str(tmp_path / "cover.jpg"), "general"))
    p.file_path = str(tmp_path / "main.jpg")
    with ms.CleanRun({"metadata_strip_enabled": True}) as r:
        r.clean_package(p)
        up = p.pat_files["upload"]
        assert set(up) == {str(tmp_path / n) for n in ("main.jpg", "extra.jpg", "cover.jpg")}
        for src, dst in up.items():
            data = open(dst, "rb").read()
            assert dst != src and not any(v in data for v in fx.FAKE_VALUES)
        assert up[str(tmp_path / "main.jpg")] == p.file_path
    assert p.pat_files["pictures"][0][0] == str(tmp_path / "main.jpg")     # library paths kept for the re-check


def test_pat_routes_never_adopt_an_unsaved_page(fake_net):
    """With a sign-in but no saved page, the tier route refuses instead of saving whichever page the cookie is for."""
    from fastapi.testclient import TestClient
    import dashboard
    aid = make_account(campaign="")
    r = TestClient(dashboard.app, client=("127.0.0.1", 50000)).get("/api/pat/tiers", params={"account_id": aid})
    assert r.status_code == 409 and not pat_account.values(aid)["campaign_id"] and not fake_net.requests


# ── lean review (13 Nov) ─────────────────────────────────────────────────────

def test_pat_chapter_without_file_refused(piece):
    """A chapter's body is its built file; without one it is refused, never posted as the blurb."""
    make_account()
    p = pkg(piece, media=None)
    errs = patp.PatreonPoster().validate(p)
    assert any("its file wasn't found" in e for e in errs)
    from posting.manager import _PERMANENT_ERROR_MARKERS
    assert any(m in errs[-1].lower() for m in _PERMANENT_ERROR_MARKERS)


def test_pat_chapter_too_long_refused(piece, monkeypatch):
    make_account()
    big = piece / "c1.html"
    big.write_text("<p>" + "x" * 50 + "</p>", encoding="utf-8")
    monkeypatch.setattr(patp, "_STORY_TEXT_MAX", 20)
    p = pkg(piece, media=None)
    p.file_path = str(big)
    assert any("too long" in e for e in patp.PatreonPoster().validate(p))


def test_pat_slow_down_not_retried(piece):
    """A slow-down during a post isn't retried in minutes, and only the scheduled poller starts the stand-down."""
    from polling import fa_standdown
    from posting.manager import _PERMANENT_ERROR_MARKERS
    aid = make_account()
    fake = FakePatreon()
    fake.status_override = ("/api/posts", 429, b"", {})
    res = run(poster(fake, aid).post(pkg(piece)))
    assert not res.success and any(m in res.error.lower() for m in _PERMANENT_ERROR_MARKERS)
    assert fa_standdown.remaining("pat") == 0


def test_pat_comment_429_stands_down_after_one_request(monkeypatch):
    from polling import fa_standdown
    aid = make_account()
    fake = FakePatreon()
    fake.post_pages = {"": ([{"type": "post", "id": str(770 + i), "attributes": {"comment_count": 1}} for i in range(5)], None)}
    fake.status_override = ("*/comments", 429, b"", {})
    with pytest.raises(patc.PatChallenged):
        _poll_with(monkeypatch, fake, aid)
    assert len([r for r in fake.requests if r["path"].endswith("/comments")]) == 1
    assert fa_standdown.remaining("pat") > 0
    run(fa_standdown.reset("pat"))


def test_pat_poll_lock_released_when_setup_fails(monkeypatch):
    from polling import pat_poller
    aid = make_account()

    def boom(*_a, **_k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(pat_poller.pat_queries, "start_pat_poll_log", boom)
    with pytest.raises(RuntimeError):
        _poll_with(monkeypatch, FakePatreon(), aid)
    assert not pat_poller._lock.locked()


def test_pat_account_check_outage_says_so(monkeypatch):
    from clients.pat import account as pat_acc

    class Down:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            raise patc.PatUnavailable("Patreon answered 500.")

        async def __aexit__(self, *_a):
            return False
    monkeypatch.setattr(patc, "PatClient", Down)
    ok, _, why = run(pat_acc.check_session("session_id=x", "inkwolf"))
    assert not ok and "didn't answer the account check" in why


def test_pat_route_errors_keep_their_reason(fake_net):
    """Status codes under 500, so the shared handler doesn't blank the reason."""
    from fastapi.testclient import TestClient
    import dashboard
    from polling import fa_standdown
    aid = make_account()
    c = TestClient(dashboard.app, client=("127.0.0.1", 50000))
    fake_net.status_override = ("/api/current_user", 500, b"", {})
    r = c.get("/api/pat/tiers", params={"account_id": aid})
    assert r.status_code == 424 and r.json().get("detail") not in (None, "", "Internal server error")
    fake_net.status_override = ("/api/current_user", 429, b"", {})
    r = c.get("/api/pat/tiers", params={"account_id": aid})
    assert r.status_code == 429 and "slow down" in r.json()["detail"]
    assert fa_standdown.remaining("pat") == 0              # opening the panel never starts or lengthens it


# ── second security pass, gap pass, home-box connection (13 Nov) ─────────────

def test_pat_missing_preview_field_stays_draft(piece):
    """A read-back without the preview field is not taken as "no preview"."""
    aid = make_account()
    fake = FakePatreon()
    fake.omit_teaser = True
    res = run(poster(fake, aid).post(pkg(piece)))
    assert not res.success and "free preview" in res.error and fake.post["published_at"] is None


def test_pat_free_image_switch_read_back(piece):
    aid = make_account()
    fake = FakePatreon()
    fake.media_free_stuck = True
    res = run(poster(fake, aid).post(pkg(piece)))
    assert not res.success and "members only" in res.error and fake.post["published_at"] is None


def test_pat_preview_after_publish_needs_attention(piece):
    aid = make_account()
    fake = FakePatreon()
    fake.teaser_leak_on_publish = True
    res = run(poster(fake, aid).post(pkg(piece)))
    assert res.success and "free preview" in res.needs_attention


def test_pat_left_draft_not_retried(piece):
    from posting.manager import _PERMANENT_ERROR_MARKERS
    aid = make_account()
    fake = FakePatreon()
    fake.teaser_leak = True
    res = run(poster(fake, aid).post(pkg(piece)))
    assert any(m in res.error.lower() for m in _PERMANENT_ERROR_MARKERS)


def test_pat_bot_check_said_apart_from_slow_down(piece):
    from polling import fa_standdown
    from posting.manager import _PERMANENT_ERROR_MARKERS
    aid = make_account()
    fake = FakePatreon()
    fake.status_override = ("/api/current_user", 403, b"<html>Just a moment...</html>", {"cf-mitigated": "challenge"})
    res = run(poster(fake, aid).post(pkg(piece)))
    assert not res.success and res.error.startswith(patc.BOT_CHECK) and "slow down" not in res.error
    assert any(m in res.error.lower() for m in _PERMANENT_ERROR_MARKERS)
    fa_standdown.note_challenge("pat")                                  # as the scheduled poller would
    again = run(poster(FakePatreon(), aid).post(pkg(piece)))
    assert "turned PawPoller away" in again.error and any(m in again.error.lower() for m in _PERMANENT_ERROR_MARKERS)
    run(fa_standdown.reset("pat"))


def test_pat_connects_like_chrome():
    """Patreon's API goes over curl presenting as Chrome, with Chrome's own headers (research R8); a test transport
    still wins."""
    c = patc.PatClient("session_id=x")
    http = c._http()
    assert type(http._transport).__name__ == "AsyncCurlTransport"
    # Never follows a redirect itself: PatClient checks each hop's host by hand (security, third pass).
    assert http.follow_redirects is False and http._transport._session.allow_redirects is False
    assert http.headers.get("user-agent", "") != patc.USER_AGENT
    fake = FakePatreon()
    assert patc.PatClient("session_id=x", transport=fake.transport())._http().headers["user-agent"] == patc.USER_AGENT


def test_pat_comments_read_past_the_newest_twenty(monkeypatch):
    """A comment on an older post reaches the Inbox: the shared capture's own cap picks posts, not a slice."""
    aid = make_account()
    fake = FakePatreon()
    fake.post_pages = {"": ([{"type": "post", "id": str(800 + i), "attributes": {"comment_count": 0}} for i in range(24)]
                            + [{"type": "post", "id": "777", "attributes": {"comment_count": 2}}], None)}
    _poll_with(monkeypatch, fake, aid)
    assert fake.calls("GET", "/api/posts/777/comments")


def test_pat_long_tags_left_out_and_said(piece):
    aid = make_account()
    fake = FakePatreon()
    p = pkg(piece)
    p.tags = ["short", "x" * 26]
    res = run(poster(fake, aid).post(p))
    assert res.success, res.error
    sent = fake.calls("PATCH", "/api/posts/777")[0]["json"]["data"]["relationships"]["user_defined_tags"]["data"]
    assert [t["id"] for t in sent] == ["user_defined;short"] and res.extra["tags_left_out"] == [{"tag": "x" * 26, "rule": "longer than 25 characters"}]


def test_pat_picture_names_neutral_files_keep_theirs(piece):
    aid = make_account()
    (piece / "PDF").mkdir()
    pdf = piece / "PDF" / "Sample_Story.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    fake = FakePatreon()
    res = run(poster(fake, aid).post(pkg(piece, files=[str(pdf)])))
    assert res.success, res.error
    names = {m["json"]["data"]["attributes"]["media_type"]: m["json"]["data"]["attributes"]["file_name"]
             for m in fake.calls("POST", "/api/media")}
    assert names == {"image": "picture.png", "file": "Sample_Story.pdf"}
    assert all(b"main.png" not in r["raw"] for r in fake.requests if r["host"] != "www.patreon.com")


def test_pat_collections_include_draft_only():
    fake = FakePatreon()
    run(_with(fake, lambda c: c.collections("111")))
    q = fake.calls("GET", "/api/collection")[0]["query"]
    assert q["filter[must_contain_at_least_one_published_post]"] == ["false"]


def test_pat_refuses_without_chrome_connection(monkeypatch):
    """No silent fall-back to a plain connection the bot check turns away (as Toyhouse)."""
    monkeypatch.setattr(patc, "AsyncCurlTransport", None)
    with pytest.raises(patc.PatUnavailable, match="curl_cffi"):
        patc.PatClient("session_id=x")._http()


def test_pat_free_image_after_publish_needs_attention(piece):
    aid = make_account()
    fake = FakePatreon()
    fake.media_free_on_publish = True
    res = run(poster(fake, aid).post(pkg(piece)))
    assert res.success and "members only" in res.needs_attention


def test_pat_markers_leave_toyhouse_alone():
    from posting.manager import _PERMANENT_ERROR_MARKERS
    th = "Toyhouse's bot check turned PawPoller away."
    assert not any(m in th.lower() for m in _PERMANENT_ERROR_MARKERS if "patreon" in m)
    for msg in (patc.BOT_CHECK, patc.PAUSED.format(when="10:00")):
        assert any(m in msg.lower() for m in _PERMANENT_ERROR_MARKERS)


def test_pat_account_check_says_missing_connection(monkeypatch):
    from clients.pat import account as pat_acc
    monkeypatch.setattr(patc, "AsyncCurlTransport", None)
    ok, _, why = run(pat_acc.check_session("session_id=x", "inkwolf"))
    assert not ok and "curl_cffi" in why


# ── Phase A2: the rest of Patreon's editor (FR-075 to FR-080) ────────────────

def test_pat_editor_json_lists_quotes_marks():
    c = json.loads(editor_json.to_editor_json(
        "<ul><li>a<ul><li>b</li></ul></li><li>c</li></ul><ol><li>one</li></ol><blockquote><p>q</p></blockquote>"
        "<p><u>u</u><s>s</s><del>d</del></p><h2>Head</h2><p style='text-align: center'>mid</p><center>c2</center>",
        paywall=None))["content"]
    items = lambda n: [i["content"][0]["content"][0]["text"] for i in n["content"]]   # noqa: E731
    assert c[0]["type"] == "bulletList" and items(c[0]) == ["a", "b", "c"]          # a nested list joins its parent
    assert c[0]["content"][0]["attrs"] == {"closed": False, "nested": False}
    assert c[1] == {"type": "orderedList", "attrs": {"order": 1}, "content": [{"type": "listItem", "attrs": {
        "closed": False, "nested": False}, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]}]}
    assert c[2] == {"type": "blockquote", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "q"}]}]}
    assert [n["marks"][0]["type"] for n in c[3]["content"]] == ["underline", "strike"]
    assert c[3]["content"][1]["text"] == "sd"
    assert c[4]["content"][0]["marks"] == [{"type": "bold"}]
    assert c[5]["attrs"]["nodeTextAlignment"] == "center" and c[6]["attrs"]["nodeTextAlignment"] == "center"
    h = editor_json.paragraphs('<h1 style="text-align: center;">Title</h1><p>after</p>')    # a chapter's title
    assert h[0]["attrs"]["nodeTextAlignment"] == "center" and h[0]["content"][0]["marks"] == [{"type": "bold"}]
    assert "attrs" not in h[1]
    assert editor_json.paragraphs("<ul></ul><blockquote></blockquote>") == [
        {"type": "blockquote", "content": [{"type": "paragraph"}]}]
    assert editor_json.paragraphs("<b>x</ul></b>y") == [{"type": "paragraph", "content": [
        {"type": "text", "text": "x", "marks": [{"type": "bold"}]}, {"type": "text", "text": "y"}]}]


def test_pat_editor_json_links_only_http():
    """FR-SEC-017: only an absolute http(s) address becomes a link; anything else keeps just its text."""
    for href in ("javascript:alert(1)", "JAVASCRIPT:alert(1)", "\x01javascript:x", "&#106;avascript:x", "vbscript:x",
                 "data:text/html,x", "/relative", "//evil.example", "https://a b", "", "HTTPS:/one-slash"):
        doc = editor_json.paragraphs(f'<p><a href="{href}">txt</a></p>')
        assert doc == [{"type": "paragraph", "content": [{"type": "text", "text": "txt"}]}], href
    ok = editor_json.paragraphs('<a href="http://example.com/x?y=1">t</a>')[0]["content"][0]["marks"][0]
    assert ok["attrs"]["href"] == "http://example.com/x?y=1"


def test_pat_caption_alt_plain_and_capped(piece):
    aid = make_account()
    fake = FakePatreon()
    p = pkg(piece, extra={"caption": "<b>Hi</b> there"}, pictures=[(str(piece / "main.png"), "general"),
                                                                 (str(piece / "sfw.png"), "general")])
    p.extra["alt_text"] = "A fox <i>reading</i>" + "x" * 2000
    res = run(poster(fake, aid).post(p))
    assert res.success, res.error
    metas = [r["json"]["data"]["attributes"]["metadata"] for r in fake.requests
             if r["method"] == "PATCH" and r["path"].startswith("/api/media/")]
    assert metas[0]["caption"] == "Hi there" and metas[1]["caption"] == ""
    assert all(m["alt_text"].startswith("A fox reading") and len(m["alt_text"]) == 1000 for m in metas)
    long = pkg(piece, extra={"caption": "x" * 141})
    assert patp.PatreonPoster().refusal(long) == patp.LONG_CAPTION


def test_pat_pictures_in_text_after_marker(piece):
    aid = make_account()
    fake = FakePatreon()
    p = pkg(piece, extra={"picture_place": "text", "caption": "Cap"},
            pictures=[(str(piece / "main.png"), "general"), (str(piece / "sfw.png"), "general")])
    p.extra["alt_text"] = "Alt"
    res = run(poster(fake, aid).post(p))
    assert res.success, res.error
    made = [r["json"]["data"]["attributes"] for r in fake.calls("POST", "/api/media")]
    assert [m["owner_relationship"] for m in made] == ["inline", "inline"]
    save = fake.calls("PATCH", "/api/posts/777")[0]["json"]["data"]["attributes"]
    nodes = json.loads(save["content_json_string"])["content"]
    assert nodes[0]["type"] == "paywallBreakpoint"
    assert [n["type"] for n in nodes[1:3]] == ["customImage", "customImage"]
    assert nodes[1]["attrs"]["src"].startswith("https://c10.patreonusercontent.com/") and nodes[1]["attrs"]["caption"] == "Cap"
    assert nodes[2]["attrs"]["caption"] == "" and nodes[1]["attrs"]["alt"] == "Alt"
    assert nodes[1]["attrs"]["data-media-id"] == "501" and nodes[1]["attrs"]["postId"] == "777"
    assert save["post_type"] == "text_only" and "is_header_media_free" not in save
    assert fake.calls("POST", "/api/posts")[0]["json"]["data"]["attributes"]["post_type"] == "text_only"


def test_pat_in_text_picture_address_checked(piece):
    aid = make_account()
    for host, scheme in (("evil.example", "https"), ("patreon-media.s3-accelerate.amazonaws.com", "https"),
                         ("c10.patreonusercontent.com.evil.example", "https"), ("evilpatreonusercontent.com", "https"),
                         ("c10.patreonusercontent.com", "http"), ("www.patreon.com", "https")):
        fake = FakePatreon()
        fake.media_host, fake.media_scheme = host, scheme
        res = run(poster(fake, aid).post(pkg(piece, extra={"picture_place": "text"})))
        assert not res.success and "picture address" in res.error and fake.post["published_at"] is None, host
    from log_redaction import scrub
    assert "SECRETHASH" not in scrub("GET https://c10.patreonusercontent.com/p/1.png?token-time=1&token-hash=SECRETHASH")


def _iso(minutes):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_pat_schedule_on_patreon(piece):
    aid = make_account()
    fake = FakePatreon()
    when = _iso(60 * 24)
    res = run(poster(fake, aid).post(pkg(piece, extra={"publish_at": when})))
    assert res.success, res.error
    patches = fake.calls("PATCH", "/api/posts/777")
    assert all(r["json"]["data"]["attributes"]["tags"]["publish"] is False for r in patches)
    assert "scheduled_for" not in patches[0]["json"]["data"]["attributes"]          # only after every check
    final = patches[-1]["json"]["data"]["attributes"]
    assert final["scheduled_for"] == when[:-1] and final["post_metadata"]["send_notifications"] is True
    assert fake.post["published_at"] is None and res.extra["scheduled_for"] == when[:-1]
    assert "Patreon will publish it" in res.extra["note"]
    fake = FakePatreon()
    fake.keep_times = False
    res = run(poster(fake, aid).post(pkg(piece, extra={"publish_at": when})))
    assert not res.success and patp.LEFT_DRAFT in res.error


def test_pat_schedule_refuses_past_or_far(piece):
    make_account()
    p = patp.PatreonPoster()
    for when, msg in ((_iso(-5), "has passed"), (_iso(60 * 24 * 400), "more than a year"), ("soon", "can read")):
        assert msg in (p.refusal(pkg(piece, extra={"publish_at": when})) or ""), when


def test_pat_early_access_general_only(piece):
    make_account()
    p = patp.PatreonPoster()
    later = _iso(60 * 24)
    assert p.refusal(pkg(piece, extra={"public_at": later})) is None
    assert p.refusal(pkg(piece, rating="mature", extra={"public_at": later},
                         pictures=[(str(piece / "mature.png"), "mature")])) == patp.EARLY_GENERAL
    assert p.refusal(pkg(piece, extra={"public_at": later},
                         pictures=[(str(piece / "main.png"), "general"), (str(piece / "sfw.png"), "mature")])) in (
        patp.EARLY_GENERAL, patp.PICTURE_HIGHER)
    assert p.refusal(pkg(piece, extra={"public_at": later, "audience": "public", "tier_ids": []})) == patp.EARLY_LOCKED
    assert p.refusal(pkg(piece, extra={"public_at": _iso(60), "publish_at": _iso(120)})) == patp.EARLY_AFTER


def test_pat_early_access_read_back(piece):
    aid = make_account()
    later = _iso(60 * 24)
    fake = FakePatreon()
    res = run(poster(fake, aid).post(pkg(piece, extra={"public_at": later})))
    assert res.success, res.error
    assert fake.calls("PATCH", "/api/posts/777")[0]["json"]["data"]["attributes"]["change_visibility_at"] == later[:-1]
    fake = FakePatreon()
    fake.keep_times = False
    res = run(poster(fake, aid).post(pkg(piece, extra={"public_at": later})))
    assert not res.success and "goes public" in res.error and fake.post["published_at"] is None


def test_pat_posts_spaced(piece):
    """FR-080: a second post waits out the 90 s after the first; a refused one doesn't wait."""
    aid = make_account()
    waits = []

    async def rec(s):
        waits.append(s)
    p = poster(FakePatreon(), aid)
    p._sleep = staticmethod(rec)
    run(p.post(pkg(piece)))
    run(p.post(pkg(piece)))
    assert any(w > 80 for w in waits)
    waits.clear()
    run(p.post(pkg(piece, rating="adult")))
    assert not any(w > 80 for w in waits)


def test_pat_story_collection_create_waits_on_proof(piece, monkeypatch):
    aid = make_account()
    fake = FakePatreon()
    fake.collections = []
    res = run(poster(fake, aid).post(pkg(piece, extra={"collection": "story"})))
    assert res.success and not fake.calls("POST", "/api/collection")
    monkeypatch.setattr(patp, "COLLECTION_CREATE_PROVED", True)
    fake = FakePatreon()
    fake.collections = []
    res = run(poster(fake, aid).post(pkg(piece, extra={"collection": "story"})))
    assert res.success, res.error
    made = fake.calls("POST", "/api/collection")
    assert len(made) == 1 and made[0]["json"]["data"]["relationships"]["campaign"]["data"]["id"] == "111"
    assert fake.calls("PATCH", "/api/posts/777")[0]["json"]["data"]["relationships"]["collections"]["data"] == [
        {"type": "collection", "id": "901"}]
    assert pat_account.values(aid)["story_collections"] == {"Sample Story": "901"}
    run(poster(fake, aid).post(pkg(piece, extra={"collection": "story"})))       # the next chapter reuses it
    assert len(fake.calls("POST", "/api/collection")) == 1


def test_pat_text_pictures_mature_refused_until_proved(piece, monkeypatch):
    make_account()
    p = pkg(piece, rating="mature", extra={"picture_place": "text"}, pictures=[(str(piece / "mature.png"), "mature")])
    assert patp.PatreonPoster().refusal(p) == patp.TEXT_MATURE
    monkeypatch.setattr(patp, "INLINE_PICTURES_PROVED", True)
    assert patp.PatreonPoster().refusal(p) is None


def test_pat_schedule_mature_refused_until_proved(piece, monkeypatch):
    make_account()
    p = pkg(piece, rating="mature", extra={"publish_at": _iso(60 * 24)}, pictures=[(str(piece / "mature.png"), "mature")])
    assert patp.PatreonPoster().refusal(p) == patp.SCHEDULE_MATURE
    monkeypatch.setattr(patp, "SCHEDULED_PUBLISH_PROVED", True)
    assert patp.PatreonPoster().refusal(p) is None


def test_pat_schedule_time_needs_a_zone(piece):
    make_account()
    naive = _iso(60 * 24)[:-1]
    assert "can read" in (patp.PatreonPoster().refusal(pkg(piece, extra={"publish_at": naive})) or "")
    assert patp.PatreonPoster().refusal(pkg(piece, extra={"publish_at": naive + "+10:00"})) is None


def test_pat_schedule_lost_answer_reads_back(piece):
    aid = make_account()
    fake = FakePatreon()
    fake.lose_schedule = True
    res = run(poster(fake, aid).post(pkg(piece, extra={"publish_at": _iso(60 * 24)})))
    assert res.success and not res.needs_attention and "Patreon will publish it" in res.extra["note"]
    assert len(fake.calls("POST", "/api/posts")) == 1


def test_pat_schedule_failure_not_retried(piece):
    """A schedule Patreon didn't keep is a draft with the no-retry marker; an unreadable one is unconfirmed."""
    from posting.manager import _PERMANENT_ERROR_MARKERS
    aid = make_account()
    fake = FakePatreon()
    fake.keep_times = False
    res = run(poster(fake, aid).post(pkg(piece, extra={"publish_at": _iso(60 * 24)})))
    assert not res.success and any(m in res.error.lower() for m in _PERMANENT_ERROR_MARKERS)
    fake = FakePatreon()
    fake.lose_schedule = True
    real = fake.handler
    state = {"sent": False}

    def h(request):
        if request.method == "GET" and request.url.path == "/api/posts/777" and state["sent"]:
            return httpx.Response(500, json={})
        if request.method == "PATCH" and json.loads(request.content)["data"]["attributes"].get("scheduled_for"):
            state["sent"] = True
        return real(request)
    p = poster(fake, aid)
    p._transport = httpx.MockTransport(h)
    res = run(p.post(pkg(piece, extra={"publish_at": _iso(60 * 24)})))
    assert res.unconfirmed and not res.success


def test_pat_schedule_read_back_rechecks_audience(piece):
    aid = make_account()
    fake = FakePatreon()
    fake.after_schedule_rules = ["1"]
    res = run(poster(fake, aid).post(pkg(piece, extra={"publish_at": _iso(60 * 24)})))
    assert res.success and "not the tiers picked" in res.needs_attention and "Patreon will publish it" in res.needs_attention
    fake = FakePatreon()
    fake.media_free_stuck = True       # the schedule save sends the pictures again; a free image is caught
    res = run(poster(fake, aid).post(pkg(piece, extra={"publish_at": _iso(60 * 24)})))
    assert not res.success and "members only" in res.error      # caught at the draft check, before the hand-over


def test_pat_schedule_read_back_catches_free_preview(piece):
    aid = make_account()
    fake = FakePatreon()
    fake.teaser_leak_on_publish = True     # the final save (here the schedule save) leaks a free preview
    res = run(poster(fake, aid).post(pkg(piece, extra={"publish_at": _iso(60 * 24)})))
    assert res.success and "free preview" in res.needs_attention and fake.post["published_at"] is None


def _two_at_once(fake, aid, packages, monkeypatch):
    """Post each package from its own thread and event loop, as a live post and the scheduler do."""
    import threading
    import time as _time
    monkeypatch.setattr(patp, "TURN_POLL_S", 0.02)
    real, live = fake.handler, {"now": 0, "most": 0}

    def h(request):
        if request.method == "POST" and request.url.path == "/api/posts":
            live["now"] += 1
            live["most"] = max(live["most"], live["now"])
            _time.sleep(0.2)
        if request.method == "PATCH" and request.url.path == "/api/posts/777" and \
                json.loads(request.content)["data"]["attributes"]["tags"]["publish"]:
            live["now"] -= 1
        return real(request)
    out = []

    def go(p):
        ps = poster(fake, aid)
        ps._transport = httpx.MockTransport(h)
        out.append(asyncio.run(ps.post(p)))
    ts = [threading.Thread(target=go, args=(p,), daemon=True) for p in packages]   # a held lock fails, never hangs
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    return out, live["most"]


def test_pat_posts_one_at_a_time_across_loops(piece, monkeypatch):
    aid = make_account()
    out, most = _two_at_once(FakePatreon(), aid, [pkg(piece), pkg(piece)], monkeypatch)
    assert len(out) == 2 and all(r.success for r in out) and most == 1
    assert not patp._turn_lock.locked()


def test_pat_collection_created_once_per_story(piece, monkeypatch):
    aid = make_account()
    monkeypatch.setattr(patp, "COLLECTION_CREATE_PROVED", True)
    fake = FakePatreon()
    fake.collections = []
    out, _ = _two_at_once(fake, aid, [pkg(piece, extra={"collection": "story"}),
                                      pkg(piece, extra={"collection": "story"})], monkeypatch)
    assert all(r.success for r in out), [r.error for r in out]
    assert len(fake.calls("POST", "/api/collection")) == 1
    # made only once the draft and its media are ready
    order = [(r["method"], r["path"]) for r in fake.requests]
    assert order.index(("POST", "/api/collection")) > order.index(("POST", "/api/media"))


def test_pat_collection_id_checked_against_fresh_list(piece, monkeypatch):
    aid = make_account()
    monkeypatch.setattr(patp, "COLLECTION_CREATE_PROVED", True)
    fake = FakePatreon()
    fake.collections = []
    fake.hide_new_collection = True
    res = run(poster(fake, aid).post(pkg(piece, extra={"collection": "story"})))
    assert res.success and "didn't show the new collection" in res.extra["note"]
    assert pat_account.values(aid)["story_collections"] == {}
    fake = FakePatreon()      # one already named after the story is used, never a second
    fake.collections = [{"type": "collection", "id": "905", "attributes": {"title": "Sample Story", "num_posts": 0}}]
    res = run(poster(fake, aid).post(pkg(piece, extra={"collection": "story"})))
    assert res.success and not fake.calls("POST", "/api/collection")
    assert pat_account.values(aid)["story_collections"] == {"Sample Story": "905"}


def test_pat_schedule_time_rechecked_before_hand_over(piece, monkeypatch):
    """Uploads take minutes: a time that has passed by the final save leaves a draft, never publishes now."""
    aid = make_account()
    fake = FakePatreon()
    real = patp._times

    def late(*a, **k):      # the time passes once the draft has been saved
        saved = fake.post is not None and fake.post["edited_at"].endswith("+")
        return (None, None, patp.PAST_TIME.format(what="time")) if saved else real(*a, **k)
    monkeypatch.setattr(patp, "_times", late)
    res = run(poster(fake, aid).post(pkg(piece, extra={"publish_at": _iso(60 * 24)})))
    assert not res.success and patp.LEFT_DRAFT in res.error and fake.post["published_at"] is None
    assert not any(r["json"]["data"]["attributes"].get("scheduled_for") for r in fake.calls("PATCH", "/api/posts/777"))


def test_pat_schedule_published_now_still_checked(piece):
    aid = make_account()
    fake = FakePatreon()
    real = fake.handler

    def h(request):
        r = real(request)
        if request.method == "PATCH" and json.loads(request.content)["data"]["attributes"].get("scheduled_for"):
            fake.post["published_at"] = "2026-11-13T01:00:00+00:00"
            fake.post["rule_ids"] = ["1"]
        return r
    p = poster(fake, aid)
    p._transport = httpx.MockTransport(h)
    res = run(p.post(pkg(piece, extra={"publish_at": _iso(60 * 24)})))
    assert res.success and "straight away" in res.needs_attention and "not the tiers picked" in res.needs_attention


def test_pat_no_alt_text_on_mature_yet(piece):
    aid = make_account()
    fake = FakePatreon()
    p = pkg(piece, rating="mature", extra={"caption": "Cap"}, pictures=[(str(piece / "mature.png"), "mature")])
    p.extra["alt_text"] = "A description"
    assert run(poster(fake, aid).post(p)).success
    meta = [r["json"]["data"]["attributes"]["metadata"] for r in fake.requests
            if r["method"] == "PATCH" and r["path"].startswith("/api/media/")]
    assert meta == [{"caption": "Cap", "alt_text": ""}]
    assert patc.plain_text("&amp;lt;b&amp;gt; <i>x</i>", 140) == "&lt;b&gt; x"      # text, never markup
    assert patc.parse_time("0001-01-01T00:00:00+14:00") is None


def test_pat_lock_freed_on_failure_or_cancel(piece):
    aid = make_account()
    fake = FakePatreon()
    fake.status_override = ("/api/posts", 500, {}, {})        # the draft can't be made
    assert not run(poster(fake, aid).post(pkg(piece))).success
    assert not patp._turn_lock.locked()

    async def cancelled():
        p = poster(FakePatreon(), aid)
        gate = asyncio.Event()

        async def stuck(_s):
            gate.set()
            await asyncio.sleep(60)
        p._sleep = staticmethod(stuck)
        patp._turn["last"] = __import__("time").monotonic()      # so it waits out the 90 s
        task = asyncio.ensure_future(p.post(pkg(piece)))
        await gate.wait()
        assert patp._turn_lock.locked()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(cancelled())
    assert not patp._turn_lock.locked()


def test_pat_story_chapter_refuses_patreon_dates(piece):
    make_account()
    p = pkg(piece, extra={"publish_at": _iso(60 * 24)})
    p.media_kind = ""
    assert patp.PatreonPoster().refusal(p) == patp.DATES_ART_ONLY


def test_pat_in_text_picture_keeps_caption_alt_on_media(piece):
    aid = make_account()
    fake = FakePatreon()
    p = pkg(piece, extra={"picture_place": "text", "caption": "Cap"})
    p.extra["alt_text"] = "Alt"
    assert run(poster(fake, aid).post(p)).success
    metas = [r["json"]["data"]["attributes"]["metadata"] for r in fake.requests
             if r["method"] == "PATCH" and r["path"].startswith("/api/media/")]
    assert metas == [{"caption": "Cap", "alt_text": "Alt"}]


def test_pat_editor_json_plain_text_with_heart():
    out = editor_json.paragraphs("Commission for a friend <3\n\nThanks for looking!")
    assert [n["content"][0]["text"] for n in out] == ["Commission for a friend <3", "Thanks for looking!"]


def test_pat_early_access_rule_itself():
    """The early-access check stands on its own, not only behind the picture and cover rules."""
    later = _iso(60 * 24)
    assert patp._times({"public_at": later}, {"pictures": [("x.png", "mature")]}, "general")[2] == patp.EARLY_GENERAL
    assert patp._times({"public_at": later}, {"cover": ("c.png", "mature")}, "general")[2] == patp.EARLY_GENERAL


def test_pat_scheduled_note_shown_not_announced():
    from posting import manager
    from posting.platforms.base import PostResult
    r = PostResult(success=True, external_url="https://www.patreon.com/posts/777",
                   extra={"scheduled_for": "2026-11-20T00:00:00", "note": "Patreon will publish it at 20 Nov."})
    assert manager._final_status(r, False, "") == ("success", "Patreon will publish it at 20 Nov.")
    rows = [{"platform": "pat", "success": True, "external_url": "https://www.patreon.com/posts/777",
             "scheduled_for": "2026-11-20T00:00:00"},
            {"platform": "bsky", "success": True, "external_url": "https://bsky.app/x", "scheduled_for": ""}]
    assert manager._run_links(rows) == [("bsky", "https://bsky.app/x")]


@pytest.mark.parametrize("odd", ["-1", "abc", None])
def test_pat_odd_rule_id_in_read_back_is_a_mismatch(piece, odd):
    """4.90.0 release review: an extra audience rule with an id PawPoller wouldn't send (negative, not a number,
    missing) still counts, so the post stays a draft instead of publishing to an audience nobody picked."""
    aid = make_account()
    fake = FakePatreon()
    fake.extra_read_rules = [odd]
    res = run(poster(fake, aid).post(pkg(piece)))
    assert not res.success and patp.LEFT_DRAFT in res.error and "not the tiers picked" in res.error, res.error
    assert [r["json"]["data"]["attributes"]["tags"]["publish"] for r in fake.calls("PATCH", "/api/posts/777")] == [False]

