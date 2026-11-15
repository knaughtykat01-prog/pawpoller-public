"""Spec 087 test helpers: a made-up Patreon page with existing posts.

Stand-in names only; nothing here is from a real page. The stand-in records every request (method, host, headers)
and the tests fail on anything but a GET.
"""
from __future__ import annotations

import io
import json
import socket
from urllib.parse import parse_qs, urlsplit

import httpx
from PIL import Image

from tests.test_patreon import FakePatreon

PIC_HOST = "c10.patreonusercontent.com"


def png(colour=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (24, 24), colour).save(buf, format="PNG")
    return buf.getvalue()


def _post(pid, title, kind, rules, pictures=0, text=""):
    return {"id": pid, "title": title, "kind": kind, "rules": rules, "pictures": pictures, "text": text}


# id → the seven made-up posts (quickstart §3). Rule ids are the stand-in page's own (FakePatreon.rules):
# 1 public, 2 all paying members, 4 the "Sketch club" tier (40), 8 a kind PawPoller doesn't offer.
POSTS = [
    _post("801", "Harbour Lights", "image_file", ["1"], 1, "<p>Out on the <b>water</b>.</p>"),
    _post("802", "Tea on the Roof", "image_file", ["4"], 1, "<p>For the club.</p>"),
    _post("803", "Quiet Carriage", "image_file", ["2"], 1, "<p>Members first.</p>"),
    _post("804", "Winter Market", "image_file", ["8"], 1, "<p>An older tier.</p>"),
    _post("805", "Three Studies", "image_file", ["1"], 3, "<p>Three of them.</p>"),
    _post("806", "A Note", "text_only", ["1"], 0, "<p>Words only.</p>"),
    _post("807", "A Clip", "video_external_file", ["1"], 0, ""),
]


class ImportPatreon(FakePatreon):
    """FakePatreon plus existing posts with pictures and audiences, and the picture host."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.posts = [dict(p) for p in POSTS]
        self.picture_status = 200
        self.picture_body = None            # bytes to serve instead of a PNG
        self.picture_location = None        # answer every picture with a redirect here
        self.post_campaign = None           # the page Patreon says a post belongs to (None = this one; "" = none)
        self.one_post_status = None
        self.bad_pictures = set()           # media ids served as a file that isn't a picture
        self.picture_status_for = {}        # media id → status
        self.fail_first = set()             # media ids that fail once, then arrive
        self.html_content = False           # answer a post's words in `content` (the older shape) as well

    def _media_ids(self, p):
        return [f"{p['id']}{n}" for n in range(1, p["pictures"] + 1)]

    def _rel(self, p):
        return {"access_rules": {"data": [{"type": "access-rule", "id": r} for r in p["rules"]]},
                "images": {"data": [{"type": "media", "id": m} for m in self._media_ids(p)]}}

    def _rule_objects(self, ids):
        return [r for r in self.rules() if r["type"] == "access-rule" and r["id"] in ids]

    def picture_url(self, media_id):
        return f"https://{PIC_HOST}/4/patreon-media/p/post/{media_id}/original.png?token-hash=SIGNED_{media_id}&token-time=1"

    def handler(self, request: httpx.Request) -> httpx.Response:
        u = urlsplit(str(request.url))
        if u.hostname == PIC_HOST:
            self.requests.append({"method": request.method, "host": u.hostname, "path": u.path,
                                  "query": parse_qs(u.query), "headers": dict(request.headers), "json": None,
                                  "raw": b""})
            if self.picture_location:
                return httpx.Response(302, headers={"location": self.picture_location})
            mid = u.path.split("/post/")[-1].split("/")[0]
            if mid in self.fail_first:
                self.fail_first.discard(mid)
                return httpx.Response(500)
            if self.picture_status_for.get(mid):
                return httpx.Response(self.picture_status_for[mid])
            if self.picture_status != 200:
                return httpx.Response(self.picture_status)
            if mid in self.bad_pictures:
                return httpx.Response(200, content=b"<html>not a picture</html>", headers={"content-type": "image/png"})
            return httpx.Response(200, content=self.picture_body if self.picture_body is not None else png(),
                                  headers={"content-type": "text/html"})      # the declared type is never trusted
        if u.path == "/api/posts" and request.method == "GET":
            self.requests.append({"method": "GET", "host": u.hostname, "path": u.path, "query": parse_qs(u.query),
                                  "headers": dict(request.headers), "json": None, "raw": b""})
            data, used = [], set()
            for p in self.posts:
                data.append({"type": "post", "id": p["id"], "relationships": self._rel(p), "attributes": {
                    "title": p["title"], "like_count": 3, "comment_count": 1, "published_at": "2026-10-02T00:00:00Z",
                    "post_type": p["kind"], "content": p["text"]}})
                used.update(p["rules"])
            return httpx.Response(200, json={"data": data, "included": self._rule_objects(used),
                                             "meta": {"pagination": {"cursors": {"next": None}}}})
        one = [p for p in self.posts if u.path == f"/api/posts/{p['id']}"]
        if request.method == "GET" and (one or (u.path.startswith("/api/posts/") and u.path.count("/") == 3
                                                and not u.path.endswith("/comments") and u.path != "/api/posts/777")):
            self.requests.append({"method": "GET", "host": u.hostname, "path": u.path, "query": parse_qs(u.query),
                                  "headers": dict(request.headers), "json": None, "raw": b""})
            if self.one_post_status:
                return httpx.Response(self.one_post_status, json={"errors": [{"detail": "no"}]})
            if not one:
                return httpx.Response(404, json={"errors": [{"detail": "not found"}]})
            p = one[0]
            rel = self._rel(p)
            camp = self.campaign if self.post_campaign is None else self.post_campaign
            if camp:
                rel["campaign"] = {"data": {"type": "campaign", "id": camp}}
            inc = self._rule_objects(p["rules"]) + [
                {"type": "media", "id": m, "attributes": {"media_type": "image", "download_url": self.picture_url(m),
                                                          "image_urls": {"original": self.picture_url(m)}}}
                for m in self._media_ids(p)]
            # As a real page answers: the words are in the editor's own document; `content` is empty unless
            # `html_content` asks the stand-in for the older shape.
            words = [w for w in p["text"].replace("<p>", "").replace("</p>", "\n").replace("<b>", "").replace("</b>", "").split("\n") if w]
            doc = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": w}]} for w in words]
                   + [{"type": "image", "attrs": {"src": self.picture_url("9999")}}]}
            return httpx.Response(200, json={"data": {"type": "post", "id": p["id"], "relationships": rel, "attributes": {
                "title": p["title"], "content": p["text"] if self.html_content else None,
                "content_json_string": json.dumps(doc), "published_at": "2026-10-02T00:00:00Z",
                "post_type": p["kind"]}}, "included": inc})
        return super().handler(request)

    def writes(self):
        return [r for r in self.requests if r["method"] not in ("GET", "HEAD")]

    def picture_calls(self):
        return [r for r in self.requests if r["host"] == PIC_HOST]


def public_dns(monkeypatch):
    """The public-address check resolves the picture host: give it a public address without real DNS."""
    from posting import video_media
    monkeypatch.setattr(video_media, "_resolve",
                        lambda host, port, proto=0: [(socket.AF_INET, socket.SOCK_STREAM, proto, "", ("93.184.216.34", port))])
