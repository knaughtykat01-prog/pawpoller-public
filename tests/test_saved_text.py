"""Spec 060 US3: saved descriptions and tag groups, and bringing PostyBirb's in.

Covers the server-side caps (plan: Settings caps — an over-cap PUT is a 400), the name clash
rule (" (PostyBirb)"), no double import, HTML → Markdown, and the upload path (saved text comes
in with the library import, before the upload is deleted). Stand-in data only (pb_fixture).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import pb_import
from routes import library_api
from tests import pb_fixture


@pytest.fixture
def client():
    a = FastAPI()
    a.include_router(library_api.library_router)
    return TestClient(a)


@pytest.fixture
def pb(tmp_path, monkeypatch):
    docs, app = pb_fixture.build(tmp_path)
    monkeypatch.setenv("PAWPOLLER_PB_DIR", str(docs))
    monkeypatch.setenv("PAWPOLLER_PB_APPDATA", str(app))
    return docs


def test_save_and_read_back(client):
    r = client.put("/api/library/saved-descriptions", json={"items": [{"name": " Bio ", "text": "Hi."}]})
    assert r.status_code == 200
    got = client.get("/api/library/saved-descriptions").json()["items"]
    assert [(i["name"], i["text"]) for i in got] == [("Bio", "Hi.")] and got[0]["id"]
    r = client.put("/api/library/tag-groups", json={"items": [{"name": "W", "tags": ["wolf", "Wolf", " ", "canine"]}]})
    assert r.json()["items"][0]["tags"] == ["wolf", "canine"]          # no duplicates, no blanks


@pytest.mark.parametrize("path,items", [
    ("saved-descriptions", [{"name": "x", "text": "a"}] * 201),
    ("saved-descriptions", [{"name": "x", "text": "a" * 20_001}]),
    ("saved-descriptions", [{"name": "n" * 81, "text": "a"}]),
    ("saved-descriptions", [{"name": "", "text": "a"}]),
    ("tag-groups", [{"name": "x", "tags": [f"t{i}" for i in range(301)]}]),
    ("tag-groups", [{"name": "x", "tags": ["t" * 201]}]),
    ("tag-groups", "not a list"),
])
def test_over_cap_is_refused_and_nothing_saved(client, path, items):
    r = client.put(f"/api/library/{path}", json={"items": items})
    assert r.status_code == 400
    assert not config.get_settings().get(library_api.KEYS["descriptions" if "desc" in path else "groups"])


def test_postybirb_extras_come_in_once_with_clashes_renamed(pb):
    library_api.save("descriptions", [{"name": "Thanks", "text": "mine"}])
    first = pb_import.apply_extras()
    assert first["descriptions"]["added"] == 2 and first["tag_groups"]["added"] == 2
    descs = {d["name"]: d["text"] for d in library_api.load("descriptions")}
    assert descs["Thanks"] == "mine" and descs["Thanks (PostyBirb)"] == "Thanks for looking!"
    md = descs["Commission info"]
    assert "<" not in md and "**open**" in md and "Ask & see." in md      # HTML → Markdown
    assert "Sneaky" not in descs                                           # an id unfit for use is skipped
    groups = {g["name"]: g["tags"] for g in library_api.load("groups")}
    assert groups == {"Wolves": ["wolf", "canine"], "Moon": ["moon", "night"]}
    again = pb_import.apply_extras()                                       # a second import adds nothing
    assert again["descriptions"] == {"added": 0, "skipped": 2, "full": 0}
    assert len(library_api.load("groups")) == 2


def test_a_full_list_leaves_the_rest_out(pb):
    library_api.save("groups", [{"name": f"g{i}", "tags": []} for i in range(200)])
    assert pb_import.apply_extras()["tag_groups"] == {"added": 0, "skipped": 0, "full": 2}


def test_the_scan_counts_them(pb):
    assert pb_import.scan_submissions()["extras"] == {"descriptions": 2, "tag_groups": 2}


def test_imported_piece_descriptions_are_markdown(pb, tmp_path):
    from tests.pb_fixture import _nedb, _part
    _nedb(pb / "data" / "submission-part.db", [_part("p1", "s1", "default", "default", desc="<p>Hi <em>there</em></p>")])
    parts = {"s1": pb_import._records(str(tmp_path), "submission-part")}
    assert pb_import._describe({"_id": "s1", "title": "T"}, parts["s1"], set())["description"] == "Hi *there*"


def test_upload_brings_saved_text_in_before_the_upload_goes(tmp_path, monkeypatch):
    from tests import test_pb_upload as up
    env = up.env.__wrapped__(tmp_path, monkeypatch)
    c, uid = up._ready(env, logins=False)
    r = c.post(f"/api/pb-import/uploads/{uid}/submissions/apply", json={"ids": [], "extras": True})
    assert r.status_code == 200, r.text
    st = up._wait()
    assert st["state"] == "done" and st["extras"]["descriptions"]["added"] == 2 and st["results"] == []
    assert up._sessions(env) == []                                         # deleted afterwards, as before
    assert c.post(f"/api/pb-import/uploads/{uid}/submissions/apply", json={"ids": []}).status_code in (400, 404)


def test_nothing_ticked_and_no_extras_is_refused(tmp_path, monkeypatch):
    from tests import test_pb_upload as up
    env = up.env.__wrapped__(tmp_path, monkeypatch)
    c, uid = up._ready(env, logins=False)
    assert c.post(f"/api/pb-import/uploads/{uid}/submissions/apply", json={"ids": []}).status_code == 400


def test_a_bad_stored_item_drops_out_alone():
    config.save_settings({"saved_descriptions": [{"name": "Keep", "text": "k"}, {"name": "", "text": 5}]})
    assert [d["name"] for d in library_api.load("descriptions")] == ["Keep"]


@pytest.mark.parametrize("crafted", ['<a href="" ' * 4000, "<b>" * 20000, "<strong" * 20000, "<p>" * 40000],
                         ids=["links", "bold", "unclosed", "paragraphs"])
def test_a_crafted_description_converts_quickly(crafted):
    import time
    t = time.perf_counter()
    pb_import._markdown(crafted)
    assert time.perf_counter() - t < 1.0


@pytest.mark.parametrize("crafted", ['<a href="" ' * 4000, "<b>" * 20000, "<h1>" * 40000],
                         ids=["links", "bold", "headings"])
def test_story_import_html_converts_quickly(crafted):
    # SEC060: the story importer used a regex chain that went quadratic on unclosed tags.
    import time
    from posting.importer import _html_to_markdown
    t = time.perf_counter()
    _html_to_markdown(crafted)
    assert time.perf_counter() - t < 1.0


def test_story_import_keeps_headings_and_bare_angles():
    from posting.importer import _html_to_markdown
    assert _html_to_markdown("<h2>Ch 1</h2><p>a <em>b</em> &amp; x < y</p>") == "## Ch 1\n\na *b* & x < y"


def test_markdown_shapes():
    assert pb_import._markdown('<p>A <a href="https://example.com">link</a></p><ul><li>one</li></ul><script>x</script>') \
        == "A [link](https://example.com)\n\n- one"
