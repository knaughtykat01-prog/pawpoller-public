"""Weasyl edit + reupload (4.54.4).

Every Weasyl edit failed: it loaded ``/edit/submission/N`` (a 404). Weasyl's form is
``/edit/submission?submitid=N`` and needs its current values posted back; tags are their own
form; and the file, cover and thumbnail CAN be replaced (its own Reupload links) — the poster
said they couldn't. Form shapes read off the live forms with the stored key on 2026-10-03.
"""
from __future__ import annotations

import asyncio

import pytest

from clients.weasyl import client as ws
from clients.weasyl.client import WeasylClient

EDIT_PAGE = """
<form action="/search" method="get"><input name="q" value=""></form>
<form action="/edit/submission" method="post">
  <input type="hidden" name="submitid" value="2620579">
  <input type="text" name="title" value="Old &amp; Title">
  <select name="folderid"><option value="">None</option><option value="9">Stories</option></select>
  <select name="subtype"><option value="2010" selected>Story</option><option value="2020">Poetry</option></select>
  <select name="rating"><option value="10">General</option><option value="40" selected>Explicit</option></select>
  <textarea name="content">Old &lt;b&gt;words&lt;/b&gt;</textarea>
  <input type="checkbox" name="friends">
  <input type="checkbox" name="critique" checked>
  <button type="submit">Save</button>
</form>"""


def test_form_values_are_what_a_browser_would_post():
    v = ws._form_values(EDIT_PAGE, "/edit/submission")
    assert v == {"submitid": "2620579", "title": "Old & Title", "folderid": "", "subtype": "2010",
                 "rating": "40", "content": "Old <b>words</b>", "critique": "on"}
    assert ws._form_values(EDIT_PAGE, "/nope") == {}


class _Resp:
    def __init__(self, status=200, text="", url="https://www.weasyl.com/"):
        self.status_code, self.text, self.url = status, text, url


def _client(post_status=200, post_text=""):
    calls = []

    class _HTTP:
        async def get(self, url, follow_redirects=False):
            calls.append(("GET", url))
            action = {"edit": "/edit/submission", "submission/": "/submit/tags", "reupload/submission":
                      "/reupload/submission", "reupload/cover": "/reupload/cover",
                      "thumbnail": "/manage/thumbnail"}
            if url.endswith("cover.png"):            # the cover image the crop is measured on
                import io
                from PIL import Image
                buf = io.BytesIO()
                Image.new("RGB", (300, 240)).save(buf, "PNG")
                r = _Resp()
                r.content = buf.getvalue()
                return r
            act = next(a for k, a in action.items() if k in url)
            page = EDIT_PAGE if act == "/edit/submission" else (
                f'<form action="{act}"><input type="hidden" name="redirect" value="/x"></form>')
            if act == "/manage/thumbnail":
                page += '<img src="https://cdn.weasyl.com/static/media/cover.png" id="imageselect">'
            return _Resp(text=page)

        async def post(self, url, data=None, files=None, timeout=None, follow_redirects=False):
            calls.append(("POST", url, dict(data or {}), sorted((files or {}).keys())))
            return _Resp(post_status, post_text)

    c = WeasylClient.__new__(WeasylClient)
    c._http = _HTTP()
    return c, calls


def test_edit_posts_the_current_values_with_only_the_changes_then_tags():
    c, calls = _client()
    asyncio.run(c.edit_submission("2620579", title="New", description="", tags="a b", rating=40))
    assert calls[0] == ("GET", "https://www.weasyl.com/edit/submission?submitid=2620579")
    _, url, data, _ = calls[1]
    assert url == "https://www.weasyl.com/edit/submission"
    assert data["title"] == "New" and data["subtype"] == "2010" and data["content"] == "Old <b>words</b>"
    assert calls[3][1:3] == ("https://www.weasyl.com/submit/tags", {"redirect": "/x", "submitid": "2620579",
                                                                   "tags": "a b"})


def test_cover_replaces_cover_then_crops_the_thumbnail(tmp_path, monkeypatch):
    img = tmp_path / "c.png"
    img.write_bytes(b"\x89PNG")
    c, calls = _client()
    # WSKEYHOST (4.56.2): the cover image is fetched by a PLAIN client, never the keyed one.
    plain = []

    class _Plain:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def get(self, url):
            plain.append(url)
            import io
            from PIL import Image
            buf = io.BytesIO()
            Image.new("RGB", (300, 240)).save(buf, "PNG")
            r = _Resp()
            r.content = buf.getvalue()
            return r

        def stream(self, method, url):
            # WSPLAINPROXY (4.58.0): the cover is read as a stream with a size cap.
            outer = self

            class _S:
                async def __aenter__(self):
                    r = await outer.get(url)
                    self.status_code = 200

                    async def chunks():
                        yield r.content
                    self.aiter_bytes = chunks
                    return self

                async def __aexit__(self, *a):
                    return False
            return _S()
    import clients.weasyl.client as wsmod
    monkeypatch.setattr(wsmod.httpx, "AsyncClient", _Plain)
    asyncio.run(c.reupload_cover("5", str(img)))
    assert plain == ["https://cdn.weasyl.com/static/media/cover.png"]
    assert not any(x[0] == "GET" and x[1].endswith("cover.png") for x in calls), "the keyed client fetched the image"
    posts = [x for x in calls if x[0] == "POST"]
    assert [(p[1], p[3]) for p in posts] == [("https://www.weasyl.com/reupload/cover", ["coverfile"]),
                                            ("https://www.weasyl.com/manage/thumbnail", [])]
    # the thumbnail is a CROP of the whole cover, in the cover's own pixels (an upload alone stays default)
    crop = posts[1][2]
    assert (crop["x1"], crop["y1"], crop["x2"], crop["y2"]) == ("0", "0", "300", "240")


def test_an_identical_file_counts_as_done_any_other_refusal_raises(tmp_path):
    f = tmp_path / "s.md"
    f.write_text("# S\n", encoding="utf-8")
    c, _ = _client(422, '<div id="error_content"><p>You have already made a submission with this submission file.</p></div>')
    asyncio.run(c.reupload_file("5", str(f)))
    c, _ = _client(422, '<div id="error_content"><p>That file type is not allowed.</p></div>')
    with pytest.raises(RuntimeError, match="That file type is not allowed"):
        asyncio.run(c.reupload_file("5", str(f)))


def test_the_poster_edit_replaces_metadata_file_and_cover(tmp_path, monkeypatch):
    from posting.platforms.weasyl import WeasylPoster
    from posting.platforms.base import StoryUploadPackage
    story, cover = tmp_path / "Chapter_1.md", tmp_path / "cover.png"
    story.write_text("<!-- @title -->\n# T\n", encoding="utf-8")
    cover.write_bytes(b"\x89PNG")
    did = []

    class _C:
        async def edit_submission(self, sid, **kw):
            did.append("edit")
            return {"url": f"https://www.weasyl.com/submission/{sid}"}

        async def reupload_file(self, sid, path):
            did.append(("file", open(path, encoding="utf-8").read()))

        async def reupload_cover(self, sid, path):
            did.append("cover")

    p = WeasylPoster()
    p._client = _C()
    pkg = StoryUploadPackage.__new__(StoryUploadPackage)
    pkg.__dict__.update(title="T", description="d", tags=["a", "b"], rating="adult", file_path=str(story),
                        file_type="markdown", thumbnail_path=str(cover), extra={})
    r = asyncio.run(p.edit("9", pkg))
    assert r.success and did == ["edit", ("file", "# T\n"), "cover"]
    assert WeasylPoster.supports_file_replace is True
