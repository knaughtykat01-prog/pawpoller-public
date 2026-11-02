"""OTW Archive forms (AO3, SquidgeWorld): pick the form a request posts to.

The archive uses per-form CSRF tokens: each form's ``authenticity_token`` is only valid for that form's own action.
A page carries several (the site-banner dismiss form, the skin switchers, the work form), so "the first token on the
page" is the wrong one whenever a banner is up, and AO3 answers the POST with ``/auth_error`` (4.78.1, AO3AUTHERR).

One forward pass with ``str.find`` (SECLOW4781): a regex over the whole page went quadratic on a page of unclosed
``<form`` tags (~50 s on 360 KB), and this runs inside the event loop.
"""
from __future__ import annotations

import re
from html import unescape

_ACTION_RE = re.compile(r'\saction="([^"]*)"', re.I)
_MAX_TAG = 4096          # a real <form ...> tag is a few hundred characters
# Site chrome that appears on every page: never the form a caller wants.
_CHROME_RE = re.compile(r"/users/[^/]+/end_banner|/skins/(?:\d+/set|unset)")


def _forms(page: str):
    """(start, end, action) of each complete ``<form>…</form>``, in order. Linear: every find moves forward."""
    low = page.lower()
    i = 0
    while True:
        s = low.find("<form", i)
        if s < 0:
            return
        e = low.find(">", s)
        end = low.find("</form>", e) if e >= 0 else -1
        if end < 0:
            return
        end += len("</form>")
        m = _ACTION_RE.search(page, s, e + 1) if e - s <= _MAX_TAG else None
        yield s, end, unescape(m.group(1)) if m else ""
        i = end


def otw_form(page: str, action: str) -> str:
    """The ``<form>`` on *page* whose action path fully matches the regex *action*, e.g. ``r"/works/\\d+"``.

    No match: the page without its chrome forms, so a token search still can't land on the banner's or a skin's.
    """
    chrome = []
    for s, end, act in _forms(page):
        path = re.sub(r"^https?://[^/]+", "", act.split("?", 1)[0].split("#", 1)[0]).rstrip("/") or "/"
        if re.fullmatch(action, path):
            return page[s:end]
        if _CHROME_RE.search(act):
            chrome.append((s, end))
    out, i = [], 0
    for s, end in chrome:
        out.append(page[i:s])
        i = end
    out.append(page[i:])
    return "".join(out)


# ── Ratings (AO3RATING) ────────────────────────────────────────────────────
# The archive's own five rating words, ranked by how much they warn. Not Rated sits between Mature and
# Explicit: it promises nothing, so a rated work must never drop to it, and only Explicit is above it.
OTW_RATING_RANK = {
    "General Audiences": 0.0,
    "Teen And Up Audiences": 1.0,
    "Mature": 2.0,
    "Not Rated": 2.5,
    "Explicit": 3.0,
}


class RatingUnreadable(RuntimeError):
    """The work's live rating couldn't be read from its edit form, so an update can't know what to keep."""


def raise_only_rating(current_fields: list[tuple[str, str]], wanted: str, site: str) -> str:
    """The rating to send on an update: whichever is higher, the work's live rating or ``wanted``.

    A later chapter must never lower a live work's rating (R21). The live value is read from the edit form
    (``work[rating_string]``); missing, empty or not one of the five words stops the update — it never guesses.
    """
    live = next((v for n, v in current_fields if n == "work[rating_string]"), None)
    if not live or live.strip() not in OTW_RATING_RANK:
        raise RatingUnreadable(f"Couldn't read this work's rating on {site} — check it on the site")
    live = live.strip()
    if wanted not in OTW_RATING_RANK:
        return live
    return wanted if OTW_RATING_RANK[wanted] > OTW_RATING_RANK[live] else live


def to_otw_rating(rating: str | None) -> str:
    """A story's rating word as one of the archive's five. Their own words (and ``teen``) map exactly;
    anything else goes through ``effective_rating``, so an unknown word is Explicit, never General."""
    from posting.platforms.base import effective_rating
    r = (rating or "").strip().lower()
    for word in OTW_RATING_RANK:
        if r == word.lower():
            return word
    if r == "teen":
        return "Teen And Up Audiences"
    return {"general": "General Audiences", "mature": "Mature"}.get(effective_rating(r), "Explicit")
