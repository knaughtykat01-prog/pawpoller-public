"""DeviantArt strips off-site links out of a description (3.23.0).

Measured, not inferred. Deviation 1371636392 ("Sitting Serious") was sent three
paragraphs on 2026-08-22::

    "Why so…seriousss?~"
    Art by CircuitSlime - https://www.deviantart.com/circuitslime
    🐾 Posted via PawPoller — pawpoller.pages.dev

and DA's own `deviation/metadata` reported back two:

    <p>&quot;Why so…seriousss?~&quot;</p><p> </p>
    <p>Art by CircuitSlime - <a href="https://www.deviantart.com/circuitslime">…</a></p>

The deviantart.com link came back wrapped in a real anchor. The paragraph
carrying `pawpoller.pages.dev` was gone — not shortened, gone, taking its
paragraph with it. The operator reproduced the same boundary by hand in DA's editor:
it refused the external link and accepted the DA one.

**Why this is more than a cosmetic loss.** The artist credit is rendered per
platform, and same-site handles win — which is the only reason CircuitSlime's
credit survived. An artist with no DA handle rendered as::

    Art by Kegeti - https://www.furaffinity.net/user/kegeti

which is an off-site link in exactly the position DA deletes. So the credit
didn't arrive degraded; it didn't arrive. **17 of the catalogue's 45 artists
have no DA handle** (20 do, 8 have no handle at all and were already plain), so
this was silently dropping roughly a third of all credits on DeviantArt — and
"credit should always be there" is a rule that doesn't bend here.

`artist_credit.py` already carried the principle needed to fix it: *"Crediting
the artist matters more than the link being clickable."* It just didn't know DA
ate off-site URLs.
"""
from __future__ import annotations

import pytest

from posting import artist_credit, attribution

DA_ARTIST = {"name": "CircuitSlime", "handles": {"da": "circuitslime"}}
FA_ARTIST = {"name": "Kegeti", "handles": {"fa": "kegeti"}}
TW_ARTIST = {"name": "Ariryu", "handles": {"tw": "ariryu"}}
WS_ARTIST = {"name": "LindseyVi", "handles": {"ws": "lindseyvi"}}
NO_HANDLE = {"name": "Azzieworks", "handles": {}}
MULTI = {"name": "Someone", "handles": {"fa": "someone", "da": "someone_da"}}


# ── the credit survives, with or without a link ──────────────────────

@pytest.mark.parametrize("artist", [FA_ARTIST, TW_ARTIST, WS_ARTIST])
def test_an_off_site_credit_keeps_the_name_and_drops_the_url(artist):
    """THE regression. The name is the part that must reach the page."""
    line = artist_credit.render(artist, "da")
    assert artist["name"] in line
    assert "http" not in line, (
        f"off-site URL in a DA credit — DA deletes the paragraph it sits in, "
        f"so this credit would vanish entirely: {line!r}")


def test_a_deviantart_credit_keeps_its_link():
    """Same-site links survive, and are worth more than plain text — the fix
    must not flatten the 20 artists who ARE on DA."""
    line = artist_credit.render(DA_ARTIST, "da")
    assert line == "Art by CircuitSlime - https://www.deviantart.com/circuitslime"


def test_an_artist_on_both_prefers_their_deviantart_page_on_da():
    line = artist_credit.render(MULTI, "da")
    assert "deviantart.com/someone_da" in line
    assert "furaffinity" not in line


def test_an_artist_with_no_handle_is_unchanged():
    assert artist_credit.render(NO_HANDLE, "da") == "Art by Azzieworks"


def test_no_credit_on_da_can_ever_carry_an_off_site_url():
    """The general form, over every handle key the registry uses. A new
    platform added to PROFILE_URL must not quietly reopen this."""
    for key in artist_credit.PROFILE_URL:
        line = artist_credit.render({"name": "X", "handles": {key: "handle"}}, "da")
        if key == "da":
            assert "deviantart.com" in line
        else:
            assert "http" not in line, f"handle key {key!r} leaked a URL: {line!r}"
        assert "X" in line, f"handle key {key!r} lost the artist's name"


# ── other platforms are untouched ────────────────────────────────────

def test_furaffinity_still_gets_its_icon_code():
    assert artist_credit.render(FA_ARTIST, "fa") == "Art by :iconkegeti:"


def test_other_platforms_still_link_off_site():
    """Only DA was measured to strip links. Nothing else changes — this repo's
    own rule is that unverified behaviour is not emitted OR assumed."""
    # An X handle is OFF-site on SoFurry, where the plain "Name - url" form
    # carries it. (On X itself it is a native @mention since 4.6.1.)
    assert "twitter.com/ariryu" in artist_credit.render(TW_ARTIST, "sf")
    assert artist_credit.render(TW_ARTIST, "tw") == "Art by @ariryu"
    assert "furaffinity" in artist_credit.render(FA_ARTIST, "e621")
    # InkBunny cross-links FA natively rather than with a URL, so the thing to
    # check there is that the native tag is still emitted.
    assert artist_credit.render(FA_ARTIST, "ib") == "Art by [fa]kegeti[/fa]"


# ── the PawPoller line ───────────────────────────────────────────────

def test_the_attribution_line_drops_its_domain_on_da():
    out = attribution.maybe_append("blurb", "da", {})
    assert "Posted via PawPoller" in out
    assert "pawpoller.pages.dev" not in out, \
        "DA deletes the paragraph carrying this URL, so the whole line is lost"


def test_the_attribution_line_keeps_its_domain_everywhere_else():
    for platform in ("fa", "ib", "ws", "sf", "ao3"):
        assert "pawpoller.pages.dev" in attribution.maybe_append("blurb", platform, {})


def test_the_da_line_is_still_idempotent():
    """The marker has to match BOTH forms, or an edit re-append would stack a
    second credit line onto every DA description."""
    once = attribution.maybe_append("blurb", "da", {})
    assert attribution.maybe_append(once, "da", {}) == once
    # and the full-URL form is still recognised, for descriptions written
    # before this change or synced from another install
    withurl = "blurb\n\n" + attribution.ATTRIBUTION_LINE
    assert attribution.maybe_append(withurl, "da", {}) == withurl


def test_the_toggle_still_wins():
    assert attribution.maybe_append("blurb", "da",
                                    {"pawpoller_attribution": False}) == "blurb"


# ── end to end: what actually reaches DA ─────────────────────────────

def test_a_full_da_description_carries_no_off_site_url():
    """The two fixes together, over the shape that failed live: a blurb, an
    off-site artist, and the PawPoller line."""
    desc = artist_credit.append_to('"Why so…seriousss?~"', FA_ARTIST, "da")
    desc = attribution.maybe_append(desc, "da", {})
    assert "Kegeti" in desc
    assert "Posted via PawPoller" in desc
    assert "http" not in desc and "pages.dev" not in desc
