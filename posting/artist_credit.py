"""Render an artwork's artist credit in each platform's own markup.

The catalogue is mostly art drawn by OTHER people — commissions and gifts of
the user's characters — so almost every piece carries a credit. Before 3.5.0
that credit was free text baked into the description, which meant one string
went to every site. The visible failure: a raw
``https://www.furaffinity.net/user/inkwolf`` was posted **to FurAffinity
itself** as plain text, where ``:iconinkwolf:`` would have been a real user
link that shows their avatar. InkBunny had the same problem, and boorus never
got an artist tag at all.

So the artist is now a structured field (``artwork_reader.ArtworkInfo.artist``)
and this module turns it into the right markup per platform::

    {"name": "Inkwolf", "handles": {"fa": "inkwolf"}}

    fa    ->  Art by :iconinkwolf:
    ib    ->  Art by [fa]inkwolf[/fa]
    ws    ->  Art by <fa:inkwolf>
    e621  ->  Art by "Inkwolf":https://www.furaffinity.net/user/inkwolf
    bsky  ->  Art by Inkwolf
    (none)->  Art by Inkwolf - https://www.furaffinity.net/user/inkwolf

**Design rule: never lose the credit, never ship unverified markup.** Every
platform falls back to a plain "Art by <name>" line, so a missing handle or an
unknown platform degrades to correct-but-plain rather than to nothing — and a
markup form we could not confirm against the platform's own documentation is
deliberately NOT emitted, because a wrong tag renders as literal junk on a
live post. Crediting the artist matters more than the link being clickable.

Syntax sources (each verified against the platform's own docs or shipped
frontend, 2026-08-12):
  fa    furaffinity.net/help  — "Icon Embedding"
  ib    posting/references/inkbunny_bbcode_guide.md
  ws    weasyl.com/help/markdown + the Weasyl API docs' login-name definition
  e621  e621.net/help/dtext + the e621ng DText parser source
  fn    FurryNetwork's own app.js mention transform
  ik    Itaku's shipped frontend bundle (they publish no markup docs)
  bsky  atproto app.bsky.feed.post / richtext.facet lexicons
Deliberately plain, see the notes on each below: da, sf.

No model is involved — this is a lookup and a format string.
"""
from __future__ import annotations

import re

# Public profile URL per handle key.
PROFILE_URL = {
    "fa": "https://www.furaffinity.net/user/{h}",
    "ib": "https://inkbunny.net/{h}",
    "sf": "https://sofurry.com/u/{h}",
    "ws": "https://www.weasyl.com/~{h}",
    "da": "https://www.deviantart.com/{h}",
    "e621": "https://e621.net/users/{h}",
    "tw": "https://twitter.com/{h}",
    "bsky": "https://bsky.app/profile/{h}",
    "ik": "https://itaku.ee/profile/{h}",
    "fn": "https://furrynetwork.com/{h}",
    "ig": "https://www.instagram.com/{h}",
}

# Which handle to reach for, in order, when linking on a given platform.
# Same-site first (a native user link is always best), then the sites most
# likely to be an artist's main presence.
#
# `ig` sits second-from-last deliberately. Instagram is a large platform with
# a small furry-art presence, so an artist who has both an Instagram and any
# of the sites above almost certainly posts the work there — and a credit is
# only useful if it lands where the art is. It still outranks `e621`, whose
# user pages are booru accounts rather than portfolios.
_PREFERENCE = ("fa", "tw", "bsky", "ib", "ws", "da", "sf", "ik", "fn",
               "ig", "e621")


def _fa_slug(handle: str) -> str:
    """FurAffinity's 'stripped' username — the form its own URLs use.

    FA lowercases and removes underscores, spaces and other punctuation:
    ``Long_Eared_Hare`` is ``longearedhare`` in every URL and icon code. Verified
    live — ``/user/long_eared_hare`` returns **400** while ``/user/longearedhare``
    returns 200 — so emitting the unstripped form yields a dead credit link.
    """
    return re.sub(r"[^a-z0-9]", "", (handle or "").lower())


def _weasyl_login(handle: str) -> str:
    """Weasyl's login name: lowercase, alphanumeric ASCII only.

    Weasyl's API docs define it exactly that way, and it is the form in the
    ``/~name`` profile URL.
    """
    return re.sub(r"[^a-z0-9]", "", (handle or "").lower())


# ── Sanitising ───────────────────────────────────────────────────────
# The name and handle come from masterpiece.json and are interpolated into six
# different markup languages, so a delimiter that survives breaks out of the
# credit line. This is NOT hypothetical and needs no attacker: a credit written
# `Art by "Inkwolf"` leaves the quotes on the name, and `"{name}":{url}` then
# ships malformed DText to a live e621 post. Booru handles of the common
# `name_(artist)` shape terminate a Markdown link at their first bracket.
#
# Everything is stripped rather than escaped. These are names and handles —
# a bracket in one is a transcription artefact, never meaningful — and stripping
# cannot itself produce a new delimiter the way a half-applied escape can.
_WHITESPACE = re.compile(r"\s+")
# Per-target: the characters that would end the construct we interpolate into.
_UNSAFE = {
    "bbcode": "[]",          # [url=…]name[/url], [fa]handle[/fa]
    "markdown": "[]()",      # [name](url)
    "dtext": '"',            # "name":url
    "angle": "<>",           # <da:handle>, and DA/SF swallow raw HTML
}


def _flat(text: str) -> str:
    """Collapse all whitespace to single spaces.

    A newline inside a name would otherwise append arbitrary extra lines to the
    description on every platform, since the credit is joined to the blurb as
    text. `.strip()` alone does not catch an interior newline.
    """
    return _WHITESPACE.sub(" ", (text or "").strip())


def _clean(text: str, kind: str) -> str:
    """Drop the delimiters that would break `kind`'s syntax."""
    out = _flat(text).translate({ord(c): None for c in _UNSAFE.get(kind, "")})
    return _WHITESPACE.sub(" ", out).strip()


def _url_safe(url: str) -> str:
    """Percent-encode the characters that terminate a link early.

    Brackets and parentheses are legal in a URL path but end a Markdown target
    and a BBCode attribute, so they have to be encoded rather than stripped —
    stripping would point the link at a different profile.
    """
    for ch, enc in (("(", "%28"), (")", "%29"), ("[", "%5B"), ("]", "%5D"),
                    (" ", "%20"), ('"', "%22")):
        url = url.replace(ch, enc)
    return url


def _pick_handle(artist: dict, platform: str) -> tuple[str, str]:
    """-> (handle_key, handle) for the best account to link, or ("", "")."""
    handles = (artist or {}).get("handles") or {}
    if not handles:
        return "", ""
    if platform in handles:                       # same-site account wins
        return platform, handles[platform]
    for key in _PREFERENCE:
        if key in handles:
            return key, handles[key]
    key = next(iter(handles))
    return key, handles[key]


def profile_url(artist: dict | None, platform: str = "") -> str:
    """The single best public profile URL for this artist, or ''."""
    key, handle = _pick_handle(artist or {}, platform)
    if not handle:
        return ""
    if key == "fa":
        handle = _fa_slug(handle)
    elif key == "ws":
        handle = _weasyl_login(handle)
    return PROFILE_URL.get(key, "").format(h=handle) if handle else ""


def _render_link(artist: dict, platform: str, name: str) -> str:
    """The artist's name rendered as a link in `platform`'s markup.

    Falls back to the bare name whenever we hold no handle, and to a plain
    "Name - url" whenever the platform has no verified user-link syntax.
    """
    key, handle = _pick_handle(artist, platform)
    if not handle:
        return _clean(name, "angle")
    url = _url_safe(profile_url(artist, platform))

    # --- FurAffinity: BBCode. :iconslug: renders the avatar plus a linked name.
    if platform == "fa":
        if key == "fa":
            slug = _fa_slug(handle)
            return f":icon{slug}:" if slug else _clean(name, "bbcode")
        nm = _clean(name, "bbcode")
        return f"[url={url}]{nm}[/url]" if url else nm

    # --- InkBunny: BBCode, with dedicated tags for the big external sites.
    if platform == "ib":
        nm = _clean(name, "bbcode")
        if key == "ib":
            return f"[name]{_clean(handle, 'bbcode')}[/name]"
        if key == "fa":
            return f"[fa]{_fa_slug(handle)}[/fa]"
        if key in ("da", "sf"):
            return f"[{key}]{_clean(handle, 'bbcode')}[/{key}]"
        if key == "ws":
            return f"[w]{_weasyl_login(handle)}[/w]"
        return f"[url={url}]{nm}[/url]" if url else nm

    # --- Weasyl: Markdown, plus <site:name> cross-site links.
    if platform == "ws":
        if key == "ws":
            login = _weasyl_login(handle)
            return f"<!~{login}>" if login else _clean(name, "markdown")
        if key == "fa":
            return f"<fa:{_fa_slug(handle)}>"
        if key in ("da", "ib", "sf"):
            return f"<{key}:{_clean(handle, 'angle')}>"
        nm = _clean(name, "markdown")
        return f"[{nm}]({url})" if url else nm

    # --- e621: DText. There is NO @mention syntax (confirmed against e621's
    # own parser source), so a titled link is the only option — and the real
    # attribution mechanism there is the artist TAG, which build_artwork_package
    # injects separately.
    if platform == "e621":
        nm = _clean(name, "dtext")
        return f'"{nm}":{url}' if url else nm

    # --- FurryNetwork / Itaku: Markdown with an @mention transform. FN's
    # regex only fires at a line start or straight after whitespace, which the
    # "Art by " prefix satisfies.
    if platform in ("fn", "ik"):
        if key == platform:
            return f"@{_clean(handle, 'markdown')}"
        nm = _clean(name, "markdown")
        return f"[{nm}]({url})" if url else nm

    # --- Bluesky: plain text; the client builds link/mention facets itself and
    # its mention regex needs a full dotted handle. No URL appended — the post
    # text is capped at 300 graphemes and the credit should not eat the blurb.
    if platform == "bsky":
        if key == "bsky" and "." in handle:
            return f"@{_flat(handle)}"
        return _flat(name)

    # --- DeviantArt: a link survives only if it points AT DeviantArt.
    #
    # DA strips off-site URLs out of a description and takes the paragraph they
    # sat in with them, so a credit reading "Art by Kegeti -
    # https://www.furaffinity.net/user/kegeti" does not arrive degraded — it
    # arrives ABSENT, and the post looks perfectly fine without it.
    #
    # Measured on a live post (2026-08-22, deviation 1371636392): three
    # paragraphs were sent and DA kept two, dropping exactly the one carrying
    # `pawpoller.pages.dev`, while CircuitSlime's deviantart.com link came back
    # wrapped in a real <a>. The operator reproduced the same boundary by hand in DA's
    # own editor — it refused the external link and accepted the DA one.
    #
    # So off-site credits degrade to the bare name, which is this module's
    # first rule: crediting the artist matters more than the link being
    # clickable. Not an edge case — 17 of the catalogue's 45 artists have no
    # DA handle, so this is the path most DA credits take.
    if platform == "da":
        nm = _clean(name, "angle")
        return f"{nm} - {url}" if (key == "da" and url) else nm

    # --- SoFurry renders HTML but was never confirmed to accept markup pushed
    # through the API rather than typed in its editor (its API docs don't state
    # a format at all). Emitting an anchor that the field escapes would put a
    # literal "<a href=..." on a live post, so it stays plain until a live test
    # says otherwise.
    #
    # --- Everything else (tw, tum, mast, thr, ig, …) is plain text anyway.
    # DA's artist_comments IS an HTML field, so angle brackets in a name would
    # be swallowed rather than shown — hence "angle" here, not no cleaning.
    nm = _clean(name, "angle")
    return f"{nm} - {url}" if url else nm


def render(artist: dict | None, platform: str, *, prefix: str = "Art by") -> str:
    """The credit line for one artwork on one platform, or '' if no artist.

    `prefix` is settable because a few pieces are gifts rather than
    commissions, and the archive used "Done by" for those.
    """
    if not artist:
        return ""
    name = (artist.get("name") or "").strip()
    if not name:
        # A handle with no name: the handle is the only identity we have.
        _, handle = _pick_handle(artist, platform)
        if not handle:
            return ""
        name = handle
    return f"{prefix} {_render_link(artist, platform, name)}"


def append_to(description: str, artist: dict | None, platform: str) -> str:
    """Description with the credit appended, separated by a blank line.

    Idempotent on the artist's NAME: if the text already credits them — a
    hand-written credit that predates the structured field, or a second call on
    the same string — nothing is added, so migration and manual edits can't
    double up.

    The match is on WORD BOUNDARIES, not a bare substring. A substring test
    silently drops the credit whenever a short name happens to appear inside
    another word — an artist called "art" gets no credit on a description
    reading "drawn on an ART tablet" — which breaks this module's own first
    rule (never lose the credit) and fails invisibly, so nobody notices until
    the artist does. The catalogue has several names short enough to collide.
    """
    line = render(artist, platform)
    if not line:
        return description
    desc = description or ""
    # Two separate guards. The first catches a re-run on our OWN output, which
    # the name check alone cannot: the rendered form is often not the plain
    # name (`:iconinkwolf:` contains no standalone "Inkwolf"), so a
    # word-boundary test on the name would happily append a second copy.
    if line in desc:
        return description
    name = (artist.get("name") or "").strip()
    if name and re.search(rf"\b{re.escape(name)}\b", desc, re.IGNORECASE):
        return description
    if not (description or "").strip():
        return line
    return f"{description.rstrip()}\n\n{line}"


def artist_tag(artist: dict | None) -> str:
    """The artist's name as a booru tag, or ''.

    Boorus key on the artist tag harder than on anything else — it is how a
    reader finds everything by that artist — and it is tier 1 in the
    catalogue's own tag priority (scripts/reorder_tags.py). Spaces become
    underscores, the convention on every booru-style site.
    """
    if not artist:
        return ""
    name = (artist.get("name") or "").strip()
    if not name:
        return ""
    return "_".join(name.lower().split())
