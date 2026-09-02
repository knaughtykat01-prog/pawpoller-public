"""Resolve a pasted submission URL back to ``(platform, submission_id)``.

The codebase has always had the forward direction — ``PLATFORM_TABLES[p]
["url_template"]`` turns an id into a link, in one table, for all 18 platforms.
It never had the inverse, so "I have the link to this post" was not a thing you
could act on: the only way to attach a site upload to a Masterpiece by hand was
to pick it out of the *discovered* list, and a post that is already recorded as
a publication is (correctly) not in that list.

**This module is derived from those same templates rather than restating them.**
A second hand-written list of URL shapes is the failure this session already hit
three times — one fact, several declarations, no check (3.12.1, 3.12.2, 3.13.0).
Adding a platform to ``PLATFORM_TABLES`` gives it URL parsing for free here, and
a test asserts every platform in that table is parseable.

**Resolution is deliberately not trusted.** ``parse_submission_url`` returns
*candidates*, because some patterns genuinely overlap and because a few
platforms' public URLs do not contain the id the poller stores:

  * Bluesky stores an ``at://`` URI as the submission id, while a bsky.app link
    carries only the record key.
  * DeviantArt's publish API returns a UUID while the poller stores the numeric
    id from the public URL (the long-standing DAID mismatch, see BACKLOG).

So the caller resolves, then **verifies the id actually exists** in that
platform's submissions table, and only then offers to link. A dangling member
row pointing at a submission nobody has is worse than a clear "I can't find
that post" — it would pool no stats and quietly look linked.
"""
from __future__ import annotations

import re

from posting.sync import PLATFORM_TABLES

__all__ = ["parse_submission_url", "candidates_for", "SUPPORTED_PLATFORMS"]

# Alternate public URL shapes, each justified rather than guessed:
#
#   fa   /full/  — FurAffinity's own alternate view of the same submission id.
#   sf   /view/  — appears in this codebase alongside /s/ (clients/sf).
#   da   /{user}/art/{slug}-{digits} — the URL a person actually copies from
#        DeviantArt. The trailing digits ARE the numeric id the poller stores,
#        which is the one `da_submissions` keys on.
#   tw   /{user}/status/{id} on both twitter.com and x.com — the real form; the
#        template's /i/status/ is a fallback the pollers rarely emit.
#   bsky /profile/{handle}/post/{rkey} — resolves to the record key only; see
#        the module docstring on why that may not match the stored id.
_EXTRA_PATTERNS: list[tuple[str, str]] = [
    ("fa",   r"^https?://(?:www\.)?furaffinity\.net/full/(\d+)/?$"),
    ("sf",   r"^https?://(?:www\.)?sofurry\.com/view/(\d+)/?$"),
    ("da",   r"^https?://(?:www\.)?deviantart\.com/[^/]+/art/[^/]*?(\d+)/?$"),
    ("tw",   r"^https?://(?:www\.)?(?:twitter|x)\.com/[^/]+/status/(\d+)/?$"),
    ("bsky", r"^https?://(?:www\.)?bsky\.app/profile/[^/]+/post/([A-Za-z0-9]+)/?$"),
    ("e621", r"^https?://(?:www\.)?e621\.net/post/show/(\d+)/?$"),
]

# Some templates carry a literal `_` standing in for a user handle the poller
# does not know (bsky `profile/_/post/{id}`, tumblr `blog/view/_/{id}`). A real
# URL has the handle there, so that segment must match anything.
_HANDLE_PLACEHOLDER = "_"


def _template_to_regex(template: str) -> str:
    """Invert one ``url_template`` into a matching pattern.

    Escapes the literal text, opens up the id placeholder, tolerates an absent
    ``www.``/scheme difference and a trailing slash, and widens any `_` segment
    that stands in for a handle.
    """
    head, _sep, tail = template.partition("{id}")
    # Strip the scheme and any literal `www.` from the template BEFORE escaping,
    # then put back one optional group. Doing it the other way round is a trap:
    # inserting `(?:www\.)?` and *then* replacing the literal `www.` eats the
    # `www.` inside the group you just added, and the pattern stops matching a
    # bare `furaffinity.net/...`. That is exactly what the first version did.
    m = re.match(r"^https?://(?:www\.)?", head)
    rest = head[m.end():] if m else head
    pattern = (r"https?://(?:www\.)?" + re.escape(rest)
               + r"([^/?#]+)" + re.escape(tail))
    pattern = pattern.replace("/" + _HANDLE_PLACEHOLDER + "/", r"/[^/]+/")
    return "^" + pattern.rstrip("/") + r"/?$"


_TEMPLATE_PATTERNS: list[tuple[str, str]] = [
    (plat, _template_to_regex(cfg["url_template"]))
    for plat, cfg in PLATFORM_TABLES.items()
    if "{id}" in (cfg.get("url_template") or "")
]

SUPPORTED_PLATFORMS = tuple(sorted({p for p, _ in _TEMPLATE_PATTERNS}))


def candidates_for(url: str) -> list[tuple[str, str]]:
    """Every ``(platform, submission_id)`` a URL could plausibly mean.

    Order is stable: exact alternates first (they are narrower), then the
    generic template inversions. Duplicates are collapsed, preserving order.
    """
    u = (url or "").strip()
    if not u:
        return []
    # A bare id is a legitimate paste too — someone copying from a poller log
    # or an existing link row. It cannot name its own platform, so it yields
    # nothing here; the caller decides whether to offer a platform picker.
    out: list[tuple[str, str]] = []
    for plat, pattern in _EXTRA_PATTERNS + _TEMPLATE_PATTERNS:
        m = re.match(pattern, u, re.I)
        if m:
            sid = m.group(1).strip()
            if sid and (plat, sid) not in out:
                out.append((plat, sid))
    return out


def parse_submission_url(url: str) -> tuple[str, str] | None:
    """The single best ``(platform, submission_id)``, or None.

    Convenience over :func:`candidates_for` for the common unambiguous case.
    When a URL is genuinely ambiguous the caller should use the candidate list
    and let the user choose rather than silently taking the first.
    """
    c = candidates_for(url)
    return c[0] if c else None


def build_url(platform: str, submission_id: str) -> str:
    """``(platform, id)`` → public URL, or ``""`` when one cannot be built.

    The forward direction of `parse_submission_url`, from the same
    ``url_template`` table, so the two stay in step by construction.

    Returns empty rather than guessing in two cases: an unknown platform, and a
    template whose path carries a `_` standing in for a user handle we do not
    have (bsky, tumblr). A URL with a literal `_` in the handle segment does not
    resolve, and handing back a dead link is worse than handing back nothing —
    callers that hold the real link (a `publications` row keeps `external_url`)
    should prefer theirs anyway.
    """
    if not submission_id:
        return ""
    cfg = PLATFORM_TABLES.get(platform) or {}
    template = cfg.get("url_template") or ""
    if "{id}" not in template:
        return ""
    if "/" + _HANDLE_PLACEHOLDER + "/" in template:
        return ""
    return template.format(id=submission_id)
