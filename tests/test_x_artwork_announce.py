"""X joins the artwork picker; the announcer options panel is shared (4.3.7).

The masterpiece page's "Publish to more" listed eleven sites and X was not
one of them — ``Artwork._PLATFORMS`` was hand-written and stopped before it,
the same shape as the poller list 4.3.2 replaced. There was no X poster to
list: X could be polled, and posted to from the Posts hub, but not from a
piece.

Telegram's row had grown a per-piece panel (blur, hashtags, caption, links,
a text box); X and Bluesky are the same kind of surface — a short post that
points at where the piece lives — and had nothing. The panel is now rendered
per announcer from one option table per platform, and the parts of an
announcement that are the same everywhere (hashtags, the link picker, the
tri-state read) moved to ``posting/announce.py``.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from posting import announce
from posting.platforms.base import StoryUploadPackage


def _pkg(**kw) -> StoryUploadPackage:
    base = dict(story_name="Sample_Piece", chapter_index=0, chapter_title="",
                platform="tw", title="Sample Piece", description="A quiet piece.",
                tags=["anthro", "wolf"], rating="adult", file_path="/tmp/x.png",
                file_type="png", extra={})
    base.update(kw)
    return StoryUploadPackage(**base)


# ── the shared module ────────────────────────────────────────────────────────

class TestAnnounceHelpers:
    def test_telegram_still_reads_its_old_names(self):
        """The helpers moved; telegram.py imports them back so nothing that
        reads that module — tests included — changed."""
        from posting.platforms import telegram as tg
        assert tg._resolve_links is announce.resolve_links
        assert tg._hashtags is announce.hashtags
        assert tg._flag is announce.flag
        assert tg._LINK_MODES is announce.LINK_MODES

    def test_the_announcer_list_is_read_by_everyone(self):
        from posting import manager, artwork_reader
        assert manager._ANNOUNCES_LAST is announce.ANNOUNCERS
        assert artwork_reader._ANNOUNCERS is announce.ANNOUNCERS
        js = open("frontend/js/artwork.js", encoding="utf-8").read()
        assert "_ANNOUNCERS: ['tg', 'tw', 'bsky']" in js
        assert set(announce.ANNOUNCERS) == {"tg", "tw", "bsky"}

    def test_announcers_still_go_last(self):
        from posting.manager import _announcers_last
        assert _announcers_last(["tw", "fa", "bsky", "ib"]) == ["fa", "ib", "tw", "bsky"]

    def test_hashtags_strip_and_dedupe(self):
        assert announce.hashtags(["anthro", "Anthro", "male/male", "soft lighting"]) == "#anthro #malemale #softlighting"

    def test_tweet_length_weights_urls_and_wide_characters(self):
        assert announce.tweet_length("https://example.com/a/very/long/path/that/goes/on") == 23
        assert announce.tweet_length("abc") == 3
        assert announce.tweet_length("日本") == 4          # CJK weighs 2
        assert announce.tweet_length("a https://x.co/1 b") == 1 + 1 + 23 + 1 + 1

    def test_graphemes_ignore_combining_marks(self):
        assert announce.graphemes("é") == 1
        assert announce.graphemes("abc") == 3

    def test_compose_fits_everything_when_it_fits(self):
        p = _pkg(description="Short.", extra={"links": ["https://example.com/p"]})
        out = announce.compose(p, is_art=True, with_tags=True, limit=280, measure=announce.tweet_length)
        assert out == "Short.\n\nhttps://example.com/p\n\n#anthro #wolf"

    def test_compose_drops_hashtags_first(self):
        p = _pkg(description="x" * 250, extra={"links": ["https://example.com/p"]})
        out = announce.compose(p, is_art=True, with_tags=True, limit=280, measure=announce.tweet_length)
        assert "#" not in out and out.startswith("x" * 250) and "https://example.com/p" in out

    def test_compose_trims_the_body_and_keeps_the_link(self):
        p = _pkg(description="y" * 400, extra={"links": ["https://example.com/p"]})
        out = announce.compose(p, is_art=True, with_tags=True, limit=280, measure=announce.tweet_length)
        assert out.endswith("https://example.com/p")
        assert "…" in out
        assert announce.tweet_length(out) <= 280

    def test_compose_keeps_only_the_first_link_when_even_links_overflow(self):
        links = [f"https://example.com/{i}" for i in range(20)]
        p = _pkg(description="body", extra={"links": links})
        out = announce.compose(p, is_art=True, with_tags=False, limit=60, measure=announce.tweet_length)
        assert out.count("https://") == 1 and announce.tweet_length(out) <= 60

    def test_a_story_announcement_uses_title_and_blurb_never_the_body(self):
        p = _pkg(platform="tw", file_path=None, file_type="bbcode", title="Sample Story",
                 description="A blurb.", chapter_title="")
        assert announce.body_text(p, is_art=False) == "Sample Story\n\nA blurb."


# ── the X poster ─────────────────────────────────────────────────────────────

class _FakeTW:
    def __init__(self, **kw):
        self.kw = kw
        self.last_error = ""
        self.calls = []
        self.fail_upload = False
        self.fail_tweet = False

    async def upload_media(self, path):
        self.calls.append(("upload", path))
        if self.fail_upload:
            self.last_error = "X is rate-limiting this session (HTTP 429)"
            return None
        return "m1"

    async def set_media_alt(self, mid, text):
        self.calls.append(("alt", mid, text))
        return True

    async def create_tweet(self, text, media_ids=None, *, sensitive=False):
        self.calls.append(("tweet", text, media_ids, sensitive))
        if self.fail_tweet:
            self.last_error = "X blocked this post as automated activity (error 226)."
            return None
        return {"id": "1", "url": "https://x.com/sample/status/1"}

    async def close(self):
        self.calls.append(("close",))


@pytest.fixture()
def tw(monkeypatch, tmp_path):
    from posting.platforms import twitter
    import clients.tw.client as twc
    made = []

    def _factory(**kw):
        c = _FakeTW(**kw)
        made.append(c)
        return c

    monkeypatch.setattr(twc, "TWClient", _factory)
    poster = twitter.TwitterPoster()
    monkeypatch.setattr(poster, "_creds", lambda settings=None: ("tok", "ct0", "sample"))
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG" + b"0" * 100)
    return poster, made, str(img)


class TestXPoster:
    def test_registered_with_the_manager(self):
        from posting.manager import _get_poster
        assert type(_get_poster("tw")).__name__ == "TwitterPoster"

    def test_offered_in_the_artwork_picker_and_post_only_for_sync(self):
        js = open("frontend/js/artwork.js", encoding="utf-8").read()
        line = next(l for l in js.splitlines() if "_PLATFORMS:" in l)
        assert "'tw'" in line, "X missing from the artwork picker"
        mp = open("frontend/js/masterpieces.js", encoding="utf-8").read()
        line = next(l for l in mp.splitlines() if "_POST_ONLY:" in l)
        assert "'tw'" in line, "a tweet cannot be edited; sync must skip it, not fail it"
        pl = open("frontend/js/platforms.js", encoding="utf-8").read()
        line = next(l for l in pl.splitlines() if "code: 'tw'" in l)
        assert "pollOnly: false" in line, "the hub would still badge X 'poll only'"

    def test_posts_image_then_tweet_with_the_sensitive_flag_from_the_rating(self, tw):
        poster, made, img = tw
        r = asyncio.run(poster.post(_pkg(file_path=img, rating="adult",
                                         extra={"alt_text": "a wolf"})))
        assert r.success and r.external_url.endswith("/status/1")
        c = made[0]
        kinds = [x[0] for x in c.calls]
        assert kinds == ["upload", "alt", "tweet", "close"]
        tweet = next(x for x in c.calls if x[0] == "tweet")
        assert tweet[2] == ["m1"] and tweet[3] is True, "adult rating ⇒ possibly_sensitive"
        assert "#anthro" in tweet[1]

    def test_a_general_piece_is_not_flagged_and_the_piece_can_override(self, tw):
        poster, made, img = tw
        asyncio.run(poster.post(_pkg(file_path=img, rating="general")))
        assert next(x for x in made[0].calls if x[0] == "tweet")[3] is False
        asyncio.run(poster.post(_pkg(file_path=img, rating="general", extra={"sensitive": True})))
        assert next(x for x in made[1].calls if x[0] == "tweet")[3] is True

    def test_options_switch_off_tags_caption_and_alt(self, tw):
        poster, made, img = tw
        asyncio.run(poster.post(_pkg(file_path=img, extra={"tags": False, "alt": False,
                                                          "alt_text": "a wolf"})))
        c = made[0]
        assert not any(x[0] == "alt" for x in c.calls)
        assert "#" not in next(x for x in c.calls if x[0] == "tweet")[1]
        asyncio.run(poster.post(_pkg(file_path=img, extra={"caption": False})))
        assert next(x for x in made[1].calls if x[0] == "tweet")[1] == ""

    def test_a_failed_upload_reports_what_x_said_and_sends_no_tweet(self, tw):
        poster, made, img = tw
        poster_client_flag = {"fail_upload": True}
        import clients.tw.client as twc
        orig = twc.TWClient

        def _factory(**kw):
            c = orig(**kw)
            c.fail_upload = True
            return c
        twc.TWClient = _factory
        try:
            r = asyncio.run(poster.post(_pkg(file_path=img)))
        finally:
            twc.TWClient = orig
        assert not r.success and "429" in r.error
        assert not any(x[0] == "tweet" for x in made[-1].calls)

    def test_a_refused_tweet_carries_xs_own_reason(self, tw):
        poster, made, img = tw
        import clients.tw.client as twc
        orig = twc.TWClient

        def _factory(**kw):
            c = orig(**kw)
            c.fail_tweet = True
            return c
        twc.TWClient = _factory
        try:
            r = asyncio.run(poster.post(_pkg(file_path=img)))
        finally:
            twc.TWClient = orig
        assert not r.success and "226" in r.error

    def test_not_connected_is_said_plainly(self, monkeypatch, tw):
        poster, _, img = tw
        monkeypatch.setattr(poster, "_creds", lambda settings=None: ("", "", ""))
        r = asyncio.run(poster.post(_pkg(file_path=img)))
        assert not r.success and "isn't connected" in r.error

    def test_validate_refuses_the_wrong_things_and_not_length(self, tw, tmp_path):
        poster, _, img = tw
        assert poster.validate(_pkg(file_path=img, description="z" * 5000)) == [], (
            "length is fitted by compose, never a validation error")
        assert any("not found" in e.lower() for e in poster.validate(_pkg(file_path=str(tmp_path / "nope.png"))))
        big = tmp_path / "big.png"
        big.write_bytes(b"0" * (5 * 1024 * 1024 + 1))
        assert any("5 MB" in e for e in poster.validate(_pkg(file_path=str(big))))
        assert any("images" in e for e in poster.validate(_pkg(file_path=img, file_type="pdf")))

    def test_the_client_grew_the_flag_and_alt_text(self):
        src = open("clients/tw/client.py", encoding="utf-8").read()
        assert "sensitive: bool = False" in src
        assert '"possibly_sensitive": bool(sensitive)' in src
        assert "async def set_media_alt" in src and "media/metadata/create.json" in src

    def test_the_poster_names_x_error_226_honestly(self):
        """It inherits 4.3.5's limit and must say so, not promise a fix."""
        src = open("posting/platforms/twitter.py", encoding="utf-8").read()
        assert "226" in src and "TWAUTO" in src


# ── Bluesky's options ────────────────────────────────────────────────────────

class TestBlueskyOptions:
    def _opts(self, **kw):
        from posting.platforms.bluesky import _resolve_options
        return _resolve_options(_pkg(platform="bsky", **kw))

    def test_labels_follow_the_rating_by_default(self):
        assert self._opts(rating="adult")["labels"] == ["sexual"]
        assert self._opts(rating="mature")["labels"] == ["nudity"]
        assert self._opts(rating="general")["labels"] is None

    def test_the_piece_can_set_or_clear_the_label(self):
        assert self._opts(rating="adult", extra={"label": "porn"})["labels"] == ["porn"]
        assert self._opts(rating="adult", extra={"label": "none"})["labels"] is None
        assert self._opts(rating="adult", extra={"label": "bogus"})["labels"] == ["sexual"]

    def test_hashtags_stay_off_unless_asked(self):
        """A post that carried no tags before 4.3.7 must not grow them on upgrade."""
        assert self._opts()["tags"] is False
        assert self._opts(extra={"tags": True})["tags"] is True

    def test_length_is_no_longer_a_validation_error(self):
        from posting.platforms.bluesky import BlueskyPoster
        assert BlueskyPoster().validate(_pkg(platform="bsky", description="z" * 2000)) == []


# ── the artwork reader feeds every announcer ─────────────────────────────────

class TestReader:
    def test_links_and_the_announcement_blurb_reach_all_three(self):
        src = open("posting/artwork_reader.py", encoding="utf-8").read()
        assert "if platform in _ANNOUNCERS else {}" in src
        assert 'platform == "tg"' not in src.split("def _artwork_links")[0].split("extra={")[1], (
            "the live-links query was Telegram-only")


# ── the UI: one panel per announcer, read back per platform ─────────────────

class TestPanelUI:
    js = open("frontend/js/artwork.js", encoding="utf-8").read()

    @pytest.mark.parametrize("code, table, module", [
        ("tw", "_TW_OPTS", "posting/platforms/twitter.py"),
        ("bsky", "_BSKY_OPTS", "posting/platforms/bluesky.py"),
    ])
    def test_each_panel_offers_exactly_what_its_poster_reads(self, code, table, module):
        """The Telegram parity test, applied to the two new panels: every
        option the poster resolves has a control, and no control is a lie."""
        py = open(module, encoding="utf-8").read()
        block = py[py.index("def _resolve_options"):]
        block = block[:block.index("\n\n\n")] if "\n\n\n" in block else block
        backend = {k for k in ("sensitive", "tags", "caption", "alt", "label")
                   if f'"{k}":' in block or f'x.get("{k}")' in block}
        ui = self.js[self.js.index(f"{table}:"):]
        ui = ui[:ui.index("],\n\n")]
        keys = {k for k in ("sensitive", "tags", "caption", "alt", "label") if f"['{k}'," in ui}
        assert keys == backend, f"{code}: UI {sorted(keys)} vs poster {sorted(backend)}"

    def test_controls_are_scoped_to_their_platform(self):
        assert 'data-platform="${code}"' in self.js
        assert '.art-tg-opt[data-platform="${code}"]' in self.js
        assert '.art-tg-desc[data-platform="${code}"]' in self.js
        assert '.art-tg-linkmode[data-platform="${code}"]:checked' in self.js

    def test_the_choice_control_stores_its_value_and_default_stays_absent(self):
        i = self.js.index("_collectPlatOpts(code) {")
        block = self.js[i:i + 900]
        assert "sel.dataset.kind === 'choice' && sel.value" in block

    def test_the_old_telegram_entry_points_still_exist(self):
        assert "_collectTgOpts() { return this._collectPlatOpts('tg'); }" in self.js
        assert "_collectTgDesc() { return this._collectPlatDesc('tg'); }" in self.js

    def test_the_edit_form_renders_a_panel_per_announcer(self):
        # The live form is the Masterpiece page (4.5.0 deleted the old one):
        # one options panel per announcer, from the registry, one template.
        mp = open("frontend/js/masterpieces.js", encoding="utf-8").read()
        assert "(window.Artwork._ANNOUNCERS || ['tg']).forEach(code => {" in mp
        rows = self.js[self.js.index("_renderPlatformRows(el"):]
        assert rows.count("this._PANEL_TITLES[code]") == 1, "one template, mapped"

    def test_saving_touches_only_panels_on_the_page(self):
        i = self.js.index("async _saveMeta(name, data) {")
        block = self.js[i:i + 2500]
        assert '.art-tg-opt[data-platform="${code}"]' in block
        assert "...(data.categories || {})" in block and "...(data.descriptions || {})" in block

    def test_the_confirm_dialog_shows_a_box_per_announcer(self):
        comp = open("frontend/js/components.js", encoding="utf-8").read()
        assert "o.textBoxes" in comp and 'data-pub-desc="${esc(b.code)}"' in comp
        assert "descriptions," in comp and "tgDescription: descriptions.tg || ''" in comp
        assert self.js.count("_pubTextBoxes(") == 4, "definition + three callers"
        assert self.js.count("_pubDescOverrides(") == 4

    def test_the_masterpiece_page_passes_maps_not_telegram_alone(self):
        mp = open("frontend/js/masterpieces.js", encoding="utf-8").read()
        assert "_renderPlatformRows(host, optsByCode, extraByCode)" in mp
        assert "_applyOverrides(name, overrides)" in mp and "A._collectPlatOpts(code)" in mp

    def test_the_pre_4_3_7_telegram_only_call_shape_is_still_read(self):
        assert "_byCode(x)" in self.js and "{ tg: x }" in self.js
