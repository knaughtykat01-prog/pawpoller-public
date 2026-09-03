"""4.0.11 — the safety half of docs/specs/publish_flow.md, plus its small fixes.

Three things here are guards in the literal sense: they exist so a UI
regression cannot fire a real, publicly-visible post. The story endpoints have
carried a `confirm_live` check since they were written, with a docstring saying
exactly that; the artwork, posts and masterpiece-sync endpoints never did, and
every one of their frontend callers was unconfirmed too. This file holds both
halves together — the server refuses without the flag, and every caller sends it
— because either half alone is a regression waiting to happen.
"""
from __future__ import annotations

import re

import pytest


# ── Server-side guard ─────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import dashboard
    return TestClient(dashboard.app)


@pytest.mark.parametrize("path,body", [
    ("/api/artwork/publish", {"artwork_name": "Sample", "platforms": ["fa"]}),
    ("/api/posts/1/publish", {"platforms": ["bsky"]}),
    ("/api/masterpieces/Sample/sync", {}),
])
def test_live_endpoints_refuse_without_confirm_live(client, path, body):
    """Must 400 on the guard BEFORE touching anything: no network, no disk.

    The artwork and posts bodies are otherwise valid so the only reason to
    refuse is the missing flag. Masterpiece sync 404s on an unknown name
    before its guard, so an absent piece is acceptable there too — what is
    NOT acceptable is a 200 or a 500, either of which means the request got
    past the guard.
    """
    r = client.post(path, json=body)
    assert r.status_code in (400, 404), f"{path} -> {r.status_code}: {r.text[:120]}"
    if r.status_code == 400:
        assert "confirm_live" in r.text, (
            f"{path} refused for some other reason, so the guard was never reached")


def test_the_guard_message_names_the_flag(client):
    """The error has to tell a developer what to send, or the next person
    to hit it will assume the endpoint is broken."""
    r = client.post("/api/artwork/publish",
                    json={"artwork_name": "Sample", "platforms": ["fa"]})
    assert r.status_code == 400
    assert "confirm_live=true" in r.json()["detail"]


# ── Client-side: every caller sends it ────────────────────────

def _calls(src: str, method: str) -> list[str]:
    """Every `API.<method>(...)` call, captured through to its closing paren."""
    out = []
    for m in re.finditer(rf"API\.{method}\(", src):
        depth, i = 0, m.end() - 1
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append(src[m.start():i + 1])
    return out


@pytest.mark.parametrize("path,method,expected", [
    ("frontend/js/artwork.js", "publishArtwork", 3),
    ("frontend/js/masterpieces.js", "publishArtwork", 1),
    ("frontend/js/masterpieces.js", "syncMasterpiece", 1),
    ("frontend/js/posts.js", "publishPost", 1),
])
def test_every_publish_caller_sends_confirm_live(path, method, expected):
    """The guard is only as good as its callers. A call site that forgets the
    flag turns a working button into a 400 — and a call site is exactly where
    the next copy-paste happens."""
    src = open(path, encoding="utf-8").read()
    calls = _calls(src, method)
    assert len(calls) == expected, (
        f"{path}: found {len(calls)} {method} call(s), expected {expected} — "
        f"a new one was added; make sure it sends confirm_live and update this count")
    for c in calls:
        assert "confirm_live" in c, f"{path}: {method} call without confirm_live:\n{c[:200]}"


# ── The story Telegram blurb ──────────────────────────────────

class TestStoryTelegramBlurb:
    """Telegram is a broadcast surface and wants the short blurb, as Bluesky
    does. The cascade had no `tg` branch, so Telegram fell to the final `else`
    and received the FULL description — which on any story over ~1,000
    characters made the poster refuse the announcement outright. 4.0.5 claimed
    "one blurb now serves both"; that was true of artwork only."""

    def _story(self, story_archive):
        import config
        from posting import story_reader
        config.save_settings({"posting_story_archive_path": str(story_archive)})
        return story_reader.load_story("Test_Story")

    def test_a_long_description_is_never_sent_whole(self, story_archive):
        from posting import story_reader
        story = self._story(story_archive)
        story.descriptions = {}
        story.chapter_descriptions = {}
        story.description = "word " * 400          # ~2,000 chars
        pkg = story_reader.build_package(story, 0, "tg")
        assert len(pkg.description) <= story_reader._TG_BLURB_LIMIT + 200, (
            "the fallback must be a truncated blurb, not the whole description "
            "(+200 allows for the attribution line appended after the cascade)")

    def test_the_announcement_slot_is_shared_with_bluesky(self, story_archive):
        from posting import story_reader
        story = self._story(story_archive)
        story.chapter_descriptions = {}
        story.descriptions = {"announcement": "Chapter 3 is up."}
        story.description = "word " * 400
        assert story_reader.build_package(story, 0, "tg").description.startswith("Chapter 3 is up.")
        assert story_reader.build_package(story, 0, "bsky").description.startswith("Chapter 3 is up.")

    def test_a_telegram_specific_blurb_wins(self, story_archive):
        """`descriptions['tg']` is what a per-post override will write to."""
        from posting import story_reader
        story = self._story(story_archive)
        story.chapter_descriptions = {}
        story.descriptions = {"announcement": "Generic.", "tg": "For the channel."}
        assert story_reader.build_package(story, 0, "tg").description.startswith("For the channel.")
        assert story_reader.build_package(story, 0, "bsky").description.startswith("Generic.")

    def test_the_limit_leaves_room_for_the_rest_of_the_caption(self):
        """CAPTION_LIMIT applies to the WHOLE caption — title, blurb, links and
        hashtags together — so the blurb floor must sit well under it."""
        from posting import story_reader
        from posting.platforms import telegram
        assert story_reader._TG_BLURB_LIMIT < telegram.CAPTION_LIMIT - 100


# ── The small ones ────────────────────────────────────────────

def test_masterpiece_persona_map_is_keyed_on_persona_id():
    """/api/personas returns rows straight from the table, whose PK is
    persona_id. Keying on `p.id` put every persona under "undefined" and the
    chips never rendered. bookshelf.js keys on `p.id` legitimately — it reads
    /api/works, which re-keys — so this test is deliberately scoped to the
    one file that reads /api/personas directly."""
    src = open("frontend/js/masterpieces.js", encoding="utf-8").read()
    assert "this._personas[p.persona_id]" in src
    assert "this._personas[p.id]" not in src


def test_preview_is_offered_on_both_telegram_surfaces():
    """The one option the backend supported and neither UI offered. The
    parity test that should have caught it carried an unexplained exemption
    for exactly this key; that exemption is gone, so this is belt-and-braces
    for the STORY side, which the parity test never read."""
    for path in ("frontend/js/artwork.js", "frontend/js/metadata_editor.js"):
        src = open(path, encoding="utf-8").read()
        assert "['preview'," in src, f"{path} does not offer the preview option"


def test_the_parity_test_carries_no_exemptions():
    """A guard that names a key and then looks away is worse than no guard —
    it reads as coverage. Keep this test honest by keeping that one honest."""
    src = open("tests/test_tg_per_item_options.py", encoding="utf-8").read()
    i = src.index("def test_every_backend_option_has_a_control")
    block = src[i:i + 1500]
    assert 'k != "preview"' not in block
