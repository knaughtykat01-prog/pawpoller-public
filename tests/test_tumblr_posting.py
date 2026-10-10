"""Tumblr media posting (tum) — spec 024 (4.60.0).

A stand-in Tumblr answers every call: NPF create (multipart), read-back, edit, user info and the
OAuth 2 token endpoint. It can keep or drop the Mature label, which is the question the live proof
settles (research R3). Names are placeholders ("SecondFur", "Sample Story").
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from clients.tum import writer as w
from posting.platforms.base import StoryUploadPackage
from posting.platforms import tumblr as tp


class FakeTumblr:
    def __init__(self, *, keep_label=True, readback_error=False, create_error=None, user="secondfur",
                 blogs=("secondfur",), legacy=False):
        self.keep_label, self.readback_error, self.create_error = keep_label, readback_error, create_error
        self.user, self.blogs, self.legacy = user, list(blogs), legacy
        self.posts: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.uploads: list[str] = []
        self.token_calls: list[dict] = []
        self.n = 0

    def _store(self, pid, params):
        label = bool(params.get("has_community_label")) and self.keep_label
        self.posts[pid] = {"id_string": pid, "content": params.get("content") or self.posts.get(pid, {}).get("content"),
                           "tags": (params.get("tags") or "").split(",") if params.get("tags") else
                           self.posts.get(pid, {}).get("tags", []),
                           "state": params.get("state") or self.posts.get(pid, {}).get("state", "published"),
                           "community_labels": {"has_community_label": label,
                                                "categories": params.get("community_label_categories") or []}}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append((req.method, path))
        if path.endswith("/oauth2/token"):
            from urllib.parse import parse_qs
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            self.token_calls.append(form)
            return httpx.Response(200, json={"access_token": f"AT{len(self.token_calls)}", "expires_in": 2520,
                                             "refresh_token": f"RT{len(self.token_calls)}"})
        if path.endswith("/user/info"):
            return httpx.Response(200, json={"response": {"user": {"name": self.user,
                                                                   "blogs": [{"name": b} for b in self.blogs]}}})
        if req.method == "POST" and path.endswith("/posts"):
            if self.create_error:
                return httpx.Response(self.create_error[0], json={"errors": [{"code": self.create_error[1]}]})
            ctype = req.headers.get("content-type", "")
            body = req.content
            boundary = ctype.split("boundary=")[1].encode()
            params = {}
            for part in body.split(b"--" + boundary):
                if b'name="json"' in part:
                    params = json.loads(part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0])
                elif b'filename="' in part:
                    self.uploads.append(part.split(b'name="', 1)[1].split(b'"', 1)[0].decode())
            self.n += 1
            pid = str(1000 + self.n)
            self._store(pid, params)
            if any(b.get("type") == "video" for b in params.get("content") or []):
                # Live 2026-10-09: a transcoding video answers with a TEMPORARY id; the post gets another.
                return httpx.Response(201, json={"response": {"id": "temp" + pid, "state": "transcoding"}})
            return httpx.Response(201, json={"response": {"id": pid, "state": params.get("state")}})
        if req.method == "GET" and path.endswith("/posts"):
            import time as _t
            if getattr(self, "hide_list", False):
                return httpx.Response(200, json={"response": {"posts": []}})
            return httpx.Response(200, json={"response": {"posts": [
                {"id_string": k, "timestamp": int(_t.time()), **v} for k, v in reversed(self.posts.items())]}})
        if "/posts/" in path:
            pid = path.rsplit("/", 1)[1]
            if req.method == "GET":
                if self.readback_error:
                    return httpx.Response(500, json={})
                if self.legacy:
                    return httpx.Response(200, json={"response": {"id_string": pid, "type": "photo"}})
                return httpx.Response(200, json={"response": self.posts.get(pid, {})})
            if req.method == "PUT":
                if b"multipart" in req.headers.get("content-type", "").encode():
                    self.uploads.append("replace")
                    params = json.loads(req.content.split(b'name="json"', 1)[1].split(b"\r\n\r\n", 1)[1]
                                        .split(b"\r\n--", 1)[0])
                else:
                    params = json.loads(req.content)
                self._store(pid, params)
                return httpx.Response(200, json={"response": {"id": pid, "state": params.get("state")}})
        return httpx.Response(404, json={"errors": [{"title": "not found"}]})


@pytest.fixture
def fake(monkeypatch):
    f = FakeTumblr()
    real_writer, real_token = w.TumWriter.__init__, w._token

    def writer_init(self, blog, **kw):
        kw["transport"] = httpx.MockTransport(f)
        real_writer(self, blog, **kw)

    async def token(data, transport=None):
        return await real_token(data, httpx.MockTransport(f))
    monkeypatch.setattr(w.TumWriter, "__init__", writer_init)
    monkeypatch.setattr(w, "_token", token)
    config.save_settings({"tum_api_key": "CK", "tum_consumer_secret": "CS", "tum_blog": "secondfur",
                          "tum_oauth2_access_token": "AT0", "tum_oauth2_refresh_token": "RT0",
                          "tum_oauth2_expires_at": str(10 ** 10), "tum_oauth2_user": "secondfur"})
    return f


def _png(tmp_path, name="a.png", size=1000):
    p = tmp_path / name
    p.write_bytes(b"\x89PNG" + b"0" * size)
    return p


def _pkg(tmp_path, *, rating="general", desc="A caption with **bold** and a [link](https://example.com).",
         name="a.png", kind="image", extra=None, size=1000):
    p = _png(tmp_path, name, size)
    return StoryUploadPackage(story_name="Sample Story", chapter_index=0, chapter_title="", platform="tum",
                              title="Sample Story", description=desc, tags=["fox", "sample_tag"], rating=rating,
                              file_path=str(p), file_type=name.rsplit(".", 1)[1], media_kind=kind,
                              extra={"alt_text": "A fox", **(extra or {})})


def _post(pkg):
    return asyncio.run(tp.TumblrPoster().post(pkg))


# ── US1: a piece reaches Tumblr ──────────────────────────────────────────────

def test_a_general_piece_is_published_with_image_title_caption_alt_and_tags(fake, tmp_path):
    r = _post(_pkg(tmp_path))
    assert r.success and r.external_id == "1001" and r.external_url == "https://secondfur.tumblr.com/post/1001"
    assert not r.needs_attention
    post = fake.posts["1001"]
    img, title, caption = post["content"][:3]
    assert img["type"] == "image" and img["alt_text"] == "A fox" and img["media"][0]["identifier"] == "f0"
    assert title == {"type": "text", "text": "Sample Story", "formatting": [{"start": 0, "end": 12, "type": "bold"}]}
    assert caption["text"] == "A caption with bold and a link."
    assert {"start": 15, "end": 19, "type": "bold"} in caption["formatting"]
    assert {"start": 26, "end": 30, "type": "link", "url": "https://example.com"} in caption["formatting"]
    assert post["tags"] == ["fox", "sample tag"] and post["state"] == "published"
    assert not post["community_labels"]["has_community_label"]
    assert fake.uploads == ["f0"]


def test_a_long_caption_is_split_never_cut():
    para = ("Word " * 1000).strip() + ". " + ("Next " * 900).strip() + "."
    blocks = tp.npf_text_blocks("", para)
    assert len(blocks) == 3 and all(len(b["text"]) <= 4096 for b in blocks)
    assert " ".join(b["text"] for b in blocks).split() == para.split()


def test_oversize_files_are_refused_before_any_call(fake, tmp_path):
    p = tp.TumblrPoster()
    big = _pkg(tmp_path, size=21 * 1024 * 1024)
    gif = _pkg(tmp_path, name="b.gif", size=12 * 1024 * 1024)
    assert any("up to 20 MB" in e for e in p.validate(big))
    assert any("GIFs up to 10 MB" in e for e in p.validate(gif))
    assert fake.calls == []


def test_tumblr_refusals_are_in_plain_words(fake, tmp_path):
    fake.create_error = (403, 8004)
    r = _post(_pkg(tmp_path))
    assert not r.success and "daily image upload limit" in r.error and "try again tomorrow" in r.error
    from posting.manager import _PERMANENT_ERROR_MARKERS
    assert any(m in r.error.lower() for m in _PERMANENT_ERROR_MARKERS)     # not retried in minutes


def test_tumblr_is_a_publish_target_everywhere_it_must_be():
    from posting import manager, post_publisher, tag_budget
    assert "tum" in manager.WORK_POSTERS
    assert "tum" not in post_publisher._TEXT_ONLY
    assert tag_budget.budget_for("tum") == {"count": 30}
    assert "tum_oauth2_refresh_token" in config.CREDENTIAL_FIELDS
    assert tp.TumblrPoster.max_rating == "mature"


# ── US2: Mature labelled or not at all ───────────────────────────────────────

def test_mature_with_the_label_kept_is_published_labelled(fake, tmp_path):
    r = _post(_pkg(tmp_path, rating="mature"))
    assert r.success and not r.needs_attention
    post = fake.posts["1001"]
    assert post["state"] == "published" and post["community_labels"] == {"has_community_label": True,
                                                                         "categories": ["sexual_themes"]}


def test_mature_with_the_label_dropped_stays_a_draft(fake, tmp_path):
    fake.keep_label = False
    r = _post(_pkg(tmp_path, rating="mature", extra={"tum_label_categories": ["violence"]}))
    assert r.success and r.needs_attention == tp.DRAFT_SENTENCE
    assert fake.posts["1001"]["state"] == "draft"
    assert ("PUT", "/v2/blog/secondfur/posts/1001") not in fake.calls       # never published


def test_a_failed_read_back_counts_as_unlabelled(fake, tmp_path):
    fake.readback_error = True
    r = _post(_pkg(tmp_path, rating="mature"))
    assert r.needs_attention and fake.posts["1001"]["state"] == "draft"


def test_adult_work_is_refused_before_anything_is_sent(fake, tmp_path):
    errors = tp.TumblrPoster().validate(_pkg(tmp_path, rating="adult"))
    assert any("doesn't take adult work" in e for e in errors) and fake.calls == []


def test_the_manager_records_a_draft_and_the_bell_lists_it(fake, tmp_path, monkeypatch):
    """needs_attention → publication 'draft', log 'needs_attention', result `attention`, bell wording."""
    from database import posting_queries
    from database.db import get_connection
    from routes.api import _format_post_summary
    conn = get_connection()
    try:
        pub = posting_queries.upsert_publication(conn, "Sample Story", 0, "tum", account_id=0,
                                                 content_type="artwork", external_id="1001",
                                                 status="draft")
        posting_queries.log_posting_action(conn, "tum", "Sample Story", 0, action="post", status="needs_attention",
                                           content_type="artwork", pub_id=pub, error_message=tp.DRAFT_SENTENCE)
        row = conn.execute("SELECT status FROM publications WHERE pub_id = ?", (pub,)).fetchone()
        log = posting_queries.get_posting_log(conn, None, 1, None)[0]
    finally:
        conn.close()
    assert row["status"] == "draft"
    assert _format_post_summary(log, "Sample Story") == "Needs you: Sample Story is waiting as a draft"


# ── US3: the Posts page ──────────────────────────────────────────────────────

def test_posts_page_images_reach_tumblr(fake, tmp_path):
    from posting import post_publisher
    a, b = _png(tmp_path, "one.png"), _png(tmp_path, "two.png")
    post = {"body": "Two pictures", "rating": "general",
            "media": [{"path": str(a), "alt": "one"}, {"path": str(b), "alt": "two"}]}
    res = asyncio.run(post_publisher._publish_one(post, "tum", None, config.get_settings()))
    assert res["success"], res
    content = fake.posts[res["external_id"]]["content"]
    assert [c["type"] for c in content] == ["image", "image", "text"] and fake.uploads == ["f0", "f1"]
    prev = post_publisher.preview("x", ["tum"], image_count=2)
    assert "Text only" not in json.dumps(prev)                       # no "images will be left off"


def test_a_sensitive_posts_page_post_takes_the_draft_path(fake, tmp_path):
    from posting import post_publisher
    fake.keep_label = False
    res = asyncio.run(post_publisher._publish_one({"body": "Spicy", "rating": "mature"}, "tum", None,
                                                  config.get_settings()))
    assert res["success"] and res["attention"] == tp.DRAFT_SENTENCE
    assert fake.posts[res["external_id"]]["state"] == "draft"


# ── US4: Connect ─────────────────────────────────────────────────────────────

def _api():
    from routes.tum_api import tum_router
    app = FastAPI()
    app.include_router(tum_router)
    return TestClient(app)


def test_connect_hands_back_tumblrs_page_and_the_return_address(fake):
    r = _api().post("/api/tum/auth/posting/connect", json={})
    assert r.status_code == 200
    assert r.json()["url"].startswith("https://www.tumblr.com/oauth2/authorize?client_id=CK")
    assert r.json()["redirect_uri"].endswith("/api/tum/auth/callback")


def _state_from(url):
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(url).query)["state"][0]


def test_the_blogs_owner_is_kept_and_anyone_else_is_refused(fake):
    api = _api()
    config.delete_settings_keys(["tum_oauth2_access_token", "tum_oauth2_refresh_token", "tum_oauth2_user"])
    fake.user, fake.blogs = "someoneelse", ["otherblog"]
    st = _state_from(api.post("/api/tum/auth/posting/connect", json={}).json()["url"])
    page = api.get(f"/api/tum/auth/callback?code=C&state={st}")
    assert page.status_code == 400 and "own secondfur" in page.text
    assert not config.get_settings().get("tum_oauth2_refresh_token")
    fake.user, fake.blogs = "secondfur", ["secondfur"]
    st = _state_from(api.post("/api/tum/auth/posting/connect", json={}).json()["url"])
    page = api.get(f"/api/tum/auth/callback?code=C&state={st}")
    assert page.status_code == 200 and "Connected as secondfur" in page.text
    assert config.get_settings()["tum_oauth2_refresh_token"].startswith("RT")
    assert api.get(f"/api/tum/auth/callback?code=C&state={st}").status_code == 400     # state is single use


def test_the_error_page_is_escaped(fake):
    page = _api().get("/api/tum/auth/callback?error=x&error_description=<script>alert(1)</script>")
    assert "<script>" not in page.text and "&lt;script&gt;" in page.text


def test_an_expiring_token_is_refreshed_and_the_rotated_pair_stored(fake, tmp_path):
    config.save_settings({"tum_oauth2_expires_at": "0"})
    assert _post(_pkg(tmp_path)).success
    assert fake.token_calls[0]["grant_type"] == "refresh_token" and fake.token_calls[0]["refresh_token"] == "RT0"
    assert config.get_settings()["tum_oauth2_refresh_token"] == "RT1"


def test_pasted_oauth1_keys_still_post(fake, tmp_path):
    config.delete_settings_keys(["tum_oauth2_access_token", "tum_oauth2_refresh_token", "tum_oauth2_expires_at"])
    config.save_settings({"tum_oauth_token": "OT", "tum_oauth_token_secret": "OTS"})
    assert _post(_pkg(tmp_path)).success


# ── US5: edit in place ───────────────────────────────────────────────────────

def test_an_edit_keeps_the_media_and_uploads_nothing(fake, tmp_path):
    _post(_pkg(tmp_path))
    fake.uploads.clear()
    pkg = _pkg(tmp_path, desc="New caption")
    pkg.tags = ["wolf"]
    r = asyncio.run(tp.TumblrPoster().edit("1001", pkg))
    assert r.success and fake.uploads == []
    post = fake.posts["1001"]
    assert post["content"][0]["type"] == "image" and post["content"][-1]["text"] == "New caption"
    assert post["tags"] == ["wolf"]


def test_now_mature_without_the_label_is_made_private(fake, tmp_path):
    _post(_pkg(tmp_path))
    fake.keep_label = False
    r = asyncio.run(tp.TumblrPoster().edit("1001", _pkg(tmp_path, rating="mature")))
    assert r.success and r.needs_attention == tp.PRIVATE_SENTENCE and fake.posts["1001"]["state"] == "private"


def test_an_old_format_post_cant_be_edited(fake, tmp_path):
    fake.legacy = True
    r = asyncio.run(tp.TumblrPoster().edit("55", _pkg(tmp_path)))
    assert not r.success and "old format" in r.error


def test_replacing_the_file_uploads_once(fake, tmp_path):
    _post(_pkg(tmp_path))
    fake.uploads.clear()
    r = asyncio.run(tp.TumblrPoster().replace_file("1001", str(_png(tmp_path, "new.png"))))
    assert r.success and fake.uploads == ["replace"]


# ── US6: video and audio ─────────────────────────────────────────────────────

def test_video_posts_and_audio_is_refused_before_sending(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(tp.TumblrPoster, "SETTLE_WAIT", 0)
    rv = _post(_pkg(tmp_path, name="clip.mp4", kind="video"))
    # The temporary id from the create call is swapped for the finished post's own id.
    assert rv.success and rv.external_id == "1001" and rv.external_url.endswith("/post/1001")
    assert fake.posts[rv.external_id]["content"][0]["type"] == "video"
    assert "still processing" in (rv.error or "")
    # Tumblr's public API refuses audio uploads (live, 2026-10-09): greyed out, never sent.
    errs = tp.TumblrPoster().validate(_pkg(tmp_path, name="song.mp3", kind="audio"))
    assert any("Tumblr" in e and "audio" in e for e in errs)


def test_a_video_not_yet_on_the_blog_links_the_blog_not_a_dead_id(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(tp.TumblrPoster, "SETTLE_WAIT", 0)
    monkeypatch.setattr(tp.TumblrPoster, "SETTLE_TRIES", 1)
    pkg = _pkg(tmp_path, name="clip.mp4", kind="video")
    pkg.title = "Something else entirely"
    fake.hide_list = True                      # Tumblr hasn't finished it yet
    r = asyncio.run(tp.TumblrPoster().post(pkg))
    assert r.success and r.external_id == "" and r.external_url == "https://secondfur.tumblr.com/"
    assert "appear on your blog shortly" in r.error


def test_the_daily_video_limit_says_try_tomorrow(fake, tmp_path):
    fake.create_error = (403, 8011)
    r = _post(_pkg(tmp_path, name="clip.mp4", kind="video"))
    assert not r.success and "20 videos or 60 minutes" in r.error
