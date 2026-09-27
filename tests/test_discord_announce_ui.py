"""The Discord options on the artwork pages (4.41.0, spec 008) — source contracts.

The panel is the shared announcer panel; these pin the wiring that makes the
per-piece choices and the per-publish tick actually reach the server.
"""
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "frontend" / "js"
ART = (JS / "artwork.js").read_text(encoding="utf-8")
MP = (JS / "masterpieces.js").read_text(encoding="utf-8")


def test_discord_is_an_announcer_with_its_own_options():
    assert "_ANNOUNCERS: ['tg', 'tw', 'bsky', 'discord']" in ART
    assert "discord: 'Discord options'" in ART
    assert "code === 'discord' ? this._DISCORD_OPTS" in ART
    opts = ART.split("_DISCORD_OPTS: [", 1)[1].split("\n    ],", 1)[0]
    for key in ("'image'", "'spoiler'", "'silent'", "'caption'", "'tags'"):
        assert key in opts


def test_discord_row_only_when_a_webhook_is_set():
    body = ART.split("async _fillDiscordRow(", 1)[1].split("\n    },", 1)[0]
    assert "fetch('/api/discord')" in body
    assert "!st.configured" in body
    assert "announce_on_publish ? ' checked'" in body
    # no per-site text box: Discord uses the piece's description
    assert "desc" not in body.split("_tgOptRows(", 1)[1].split(")}", 1)[0]


def test_every_publish_sends_the_discord_tick():
    assert ART.count("discord: this._discordChoice()") == 2
    assert "discord: window.Artwork ? window.Artwork._discordChoice() : undefined" in MP


def test_new_artwork_page_saves_announcer_options():
    meta = ART.split("_collectMetadata() {", 1)[1].split("\n    },", 1)[0]
    assert "this._collectCategories()" in meta
    assert "{ categories }" in meta


def test_discord_default_links_are_not_saved_as_a_choice():
    body = ART.split("_collectPlatOpts(code) {", 1)[1].split("\n    },", 1)[0]
    assert "code === 'discord' ? 'all' : 'auto'" in body
    assert "modeEl.value !== dflt" in body
