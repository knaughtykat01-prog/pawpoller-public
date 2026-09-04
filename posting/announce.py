"""The shared parts of an announcement post — Telegram, X, Bluesky (4.3.7).

Three platforms carry a short broadcast rather than a gallery upload: a
caption, hashtags, and links to wherever the piece already lives. Telegram
grew each of those first (4.0.10 the options, 4.3.0 the text box and the link
picker) and kept them as private helpers in ``platforms/telegram.py``. When X
joined the artwork picker the choice was to copy them a second and then a third
time, or to move them. They moved. ``telegram.py`` imports them back under its
old names, so its tests and its docstrings still read true.

Nothing here touches a network. Each poster decides what fits its own limit —
Telegram 1,024 in a caption, X 280 *weighted*, Bluesky 300 graphemes — by
handing :func:`compose` its own ``measure``; and decides its own defaults
through :func:`flag`.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Callable

from posting.platforms.base import StoryUploadPackage

logger = logging.getLogger(__name__)

# The platforms that announce rather than host. The manager posts these LAST so
# the links they carry can include what the same publish just created; the
# artwork reader queries a piece's live links only for them; and the artwork
# UI shows the per-piece options panel on exactly these rows.
ANNOUNCERS = ("tg", "tw", "bsky")

IMAGE_TYPES = ("png", "jpg", "jpeg", "gif", "webp")

# Ratings that get a blur / sensitive flag / content label unless the piece
# says otherwise. One vocabulary, so one rating field drives all three sites.
SPOILER_RATINGS = ("adult", "explicit", "nsfw", "mature", "questionable")

LINK_MODES = ("auto", "first", "all", "pick", "none")


def flag(value, default: bool) -> bool:
    """Read a tri-state option: unset falls back, anything else is coerced.

    Per-artwork options arrive as JSON, so a value may be a real bool or one of
    the strings a human typed into art.json. Bare bool() treats "false" as TRUE,
    which would silently invert the setting — and for a blur or a sensitive
    flag that is the wrong way round on a live post.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def hashtags(tags: list[str]) -> str:
    """Tags as hashtags — alnum/underscore, deduped, order preserved.

    ⚠ No 30-tag cap. That is Instagram's rule; copying it elsewhere would
    silently drop tags for no reason. Length is the caller's budget, which
    :func:`compose` applies by dropping the *whole* hashtag block first — a
    half-list of hashtags reads worse than none.
    """
    seen: set[str] = set()
    out: list[str] = []
    for t in tags or []:
        h = "".join(ch for ch in str(t) if ch.isalnum() or ch == "_")
        if not h or h.lower() in seen:
            continue
        seen.add(h.lower())
        out.append("#" + h)
    return " ".join(out)


def resolve_links(package: StoryUploadPackage) -> list[str]:
    """Which of the work's URLs the post carries, and in what order (4.3.0).

    Inputs ride in ``package.extra`` (all optional):

    * ``links_by_platform`` — ``[(platform, url), …]`` of EXISTING posted
      publications (story_reader._story_extra / artwork_reader._artwork_links).
      ``links`` is the older bare-URL list, used when the pairs are absent.
    * ``run_links`` — ``[(platform, url), …]`` posted SO FAR IN THIS PUBLISH.
      The manager sets it, and sorts announcing platforms last so it exists.
    * ``link_mode`` (one of LINK_MODES) and ``link_platforms`` (an ordered
      list of codes) — per-piece options from ``categories.<code>`` (artwork)
      or ``platform_options.<code>`` (story). ⚠ Read RAW, never through
      flag(): a list coerced to True was exactly the bug publish_flow spec §6
      named.

    Modes:
      auto  — the existing links, ordered by link_platforms where given; and
              when there are none yet, the first link THIS publish produced.
              That is "wherever it lands first" (spec §10 Q3), and it is the
              default so a never-posted piece sent to FA + Telegram in one go
              links to FA without anyone configuring anything.
      first — only the first successful link of this publish, else the first
              existing one.
      all   — every existing link plus this publish's, ordered.
      pick  — only the platforms in link_platforms, in that order.
      none  — no links.

    The first link matters most: it is the one Telegram and X preview.
    """
    x = package.extra or {}
    mode = str(x.get("link_mode") or "auto").strip().lower()
    if mode not in LINK_MODES:
        mode = "auto"
    if mode == "none":
        return []
    raw_order = x.get("link_platforms")
    order = [str(c) for c in raw_order] if isinstance(raw_order, (list, tuple)) else []

    def pairs(key: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for item in x.get(key) or []:
            if isinstance(item, (list, tuple)) and len(item) == 2 and item[1]:
                out.append((str(item[0]), str(item[1])))
        return out

    existing = pairs("links_by_platform") or [("", str(u)) for u in (x.get("links") or []) if u]
    run = pairs("run_links")

    if mode == "first":
        pool = run or existing
        return [pool[0][1]] if pool else []
    if mode == "auto" and not existing:
        return [run[0][1]] if run else []

    seen: set[str] = set()
    pool: list[tuple[str, str]] = []
    for p, u in existing + run:
        if u not in seen:
            seen.add(u)
            pool.append((p, u))
    if mode == "pick":
        pool = [(p, u) for p, u in pool if p in order]
    if order:
        rank = {c: i for i, c in enumerate(order)}
        # Stable: listed platforms in the user's order, unlisted after them.
        pool.sort(key=lambda pu: rank.get(pu[0], len(order)))
    return [u for _, u in pool]


# ── Text budgets ─────────────────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# X counts a tweet in "weighted" characters: any URL is 23 whatever its length,
# characters in these ranges weigh 1, and everything else (CJK, emoji, most
# symbols) weighs 2. That is X's own twitter-text v3 configuration, reproduced
# here rather than approximated with len(), because len() over-counts a URL and
# under-counts emoji — in opposite directions, so it cannot even be biased safe.
_WEIGHT_ONE = (
    (0x0000, 0x10FF), (0x2000, 0x200D), (0x2010, 0x201F), (0x2032, 0x2037),
)
TWEET_LIMIT = 280
TCO_LENGTH = 23


def tweet_length(text: str) -> int:
    """X's weighted length of *text*."""
    total = 0
    pos = 0
    for m in _URL_RE.finditer(text or ""):
        total += _weigh(text[pos:m.start()])
        total += TCO_LENGTH
        pos = m.end()
    total += _weigh(text[pos:])
    return total


def _weigh(segment: str) -> int:
    n = 0
    for ch in segment:
        cp = ord(ch)
        n += 1 if any(lo <= cp <= hi for lo, hi in _WEIGHT_ONE) else 2
    return n


BSKY_LIMIT = 300


def graphemes(text: str) -> int:
    """Approximate grapheme count: code points minus combining marks.

    Bluesky's limit is 300 *graphemes*. Python's stdlib has no grapheme
    segmenter; dropping combining marks (Mn/Me) is the right correction for
    accents and most emoji modifiers and is never *under* the true count for
    plain text, so a post that passes here does not bounce at the server.
    """
    return sum(1 for ch in (text or "") if unicodedata.category(ch) not in ("Mn", "Me"))


def body_text(package: StoryUploadPackage, *, is_art: bool) -> str:
    """The prose part of an announcement: description or title for art; title
    plus blurb for a story. The BODY of a story is never included."""
    if is_art:
        return (package.description or package.title or "").strip()
    parts = []
    title = (package.chapter_title or package.title or package.story_name or "").strip()
    if title:
        parts.append(title)
    blurb = (package.description or "").strip()
    if blurb:
        parts.append(blurb)
    return "\n\n".join(parts)


def compose(package: StoryUploadPackage, *, is_art: bool, with_tags: bool,
            limit: int, measure: Callable[[str], int], with_links: bool = True) -> str:
    """Body + links + hashtags, fitted to *limit* under *measure*.

    Fitting order, most disposable first:

      1. the hashtag block goes as a whole — half a hashtag list reads worse
         than none;
      2. the body is trimmed to an ellipsis — it is a gallery description being
         asked to be a caption, and the per-platform text box exists for
         anyone who wants to write the short version themselves;
      3. links are kept whole, and if even they overflow, only the first is
         kept — the announcement's whole job is to point somewhere.

    A trimmed body is logged at INFO with the platform, so a caption that looks
    cut on the live site has a line in the log saying why.
    """
    body = body_text(package, is_art=is_art)
    links = "\n".join(resolve_links(package)) if with_links else ""
    tags = hashtags(package.tags) if with_tags else ""

    def join(*parts: str) -> str:
        return "\n\n".join(p for p in parts if p)

    text = join(body, links, tags)
    if measure(text) <= limit:
        return text
    text = join(body, links)                         # 1. drop the hashtags
    if measure(text) <= limit:
        return text
    if links and measure(join("", links)) > limit:    # 3. even the links overflow
        links = links.split("\n", 1)[0]
    ellipsis = "…"
    # 2. trim the body, keeping room for the ellipsis AS THE PLATFORM WEIGHS IT
    # — X counts "…" as 2, which an off-by-one here turned into a 281.
    room = limit - (measure(links) + (measure("\n\n") if links else 0)) - measure(ellipsis)
    if room <= 0:
        return links
    cut = body
    while cut and measure(cut) > room:
        cut = cut[: max(0, len(cut) - max(1, (measure(cut) - room) // 2 or 1))]
    cut = cut.rstrip()
    if cut != body:
        logger.info("%s: caption trimmed to fit %d (%s) — the per-platform text box can "
                    "carry a short version instead", package.platform, limit,
                    getattr(measure, "__name__", "measure"))
    out = join((cut + ellipsis) if cut else "", links)
    # The step above is arithmetic on a heuristic; this is the guarantee.
    while cut and measure(out) > limit:
        cut = cut[:-1].rstrip()
        out = join((cut + ellipsis) if cut else "", links)
    return out
