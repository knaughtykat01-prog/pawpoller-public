"""What each platform will actually accept as tags — one source of truth.

The catalogue's canonical tag set is meant to be **rich**. `core` is a priority
ORDER, not the subset that gets posted: every platform is offered the whole
canonical list (core first, then auxiliary) and trims from the TAIL to whatever
it can take. So a heavily-tagged piece ships everything to Inkbunny and e621,
the first 97 to SoFurry, the first 30 to DeviantArt, and as much as fits 500
characters to FurAffinity — with the tags declared most important surviving
everywhere, because they are at the front.

Before 3.12.0 that only half worked. `_TAG_BUDGET` knew **three** platforms
(fa, ao3, sqw) while three posters capped tags themselves in a second place, and
one of those caps was simply wrong:

    tags=package.tags[:59],  # Max 59 chars per tag      <- itaku.py

59 is Itaku's per-tag **character** limit. Slicing the tag *list* to 59 items
enforces a rule that does not exist, drops tags from any work with more than 59
of them, and still lets a 60-character tag through to be rejected. Both halves
backwards.

Limits are from `posting/references/platform_tag_limits.md`, which is in turn
from each platform's own documentation or a measured probe.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# per platform:
#   count         maximum number of tags
#   chars         maximum length of the joined tag string
#   per_tag_chars maximum length of ONE tag; longer ones are dropped
#   min_count     platform refuses the upload below this many
#
# A platform absent from this table is unlimited and passes through untouched —
# Inkbunny (measured at 108+), e621, Weasyl, FurryNetwork, Furbooru. Bluesky has
# no tag field at all.
BUDGETS: dict[str, dict] = {
    # FA's field is a 500-character keyword string; the tag COUNT is unlimited,
    # so the constraint is length, not number.
    "fa":   {"chars": 500},
    # OTW's total budget is fandom + relationships + characters + freeform <= 75.
    # ao3.py/squidgeworld.py subtract the other three themselves, so this is the
    # ceiling before that subtraction.
    "ao3":  {"count": 75},
    "sqw":  {"count": 75},
    "sf":   {"count": 97},
    "da":   {"count": 30},
    "wp":   {"count": 24},
    "ik":   {"per_tag_chars": 59, "min_count": 5},
}


def budget_for(platform: str) -> dict:
    return BUDGETS.get((platform or "").lower(), {})


def describe(platform: str) -> str:
    """One human line for the UI: what this platform will take."""
    b = budget_for(platform)
    if not b:
        return "no limit"
    bits = []
    if b.get("count"):
        bits.append(f"{b['count']} tags max")
    if b.get("chars"):
        bits.append(f"{b['chars']} characters max")
    if b.get("per_tag_chars"):
        bits.append(f"{b['per_tag_chars']} chars per tag")
    if b.get("min_count"):
        bits.append(f"at least {b['min_count']}")
    return ", ".join(bits)


def fit(tags: list[str], platform: str, *, core_count: int | None = None) -> list[str]:
    """Trim `tags` to what `platform` accepts. Order is preserved.

    Tags arrive core-first, so trimming drops from the TAIL — whatever survives
    is guaranteed to be what was declared most important. That is the entire
    reason the core/auxiliary split exists; without it a trim cuts arbitrarily.

    `core_count` is used only to warn when a platform's budget is tight enough
    to bite into the core set. That is a tagging problem the user needs told
    about, not something to paper over.
    """
    b = budget_for(platform)
    out = [str(t) for t in (tags or [])]
    if not b:
        return out

    # Over-long individual tags go first, wherever they sit. Truncating one
    # would change what it means and could collide with a real tag; dropping it
    # loses exactly the tag the platform was never going to accept.
    per_tag = b.get("per_tag_chars")
    if per_tag:
        too_long = [t for t in out if len(t) > per_tag]
        if too_long:
            logger.warning("%s: dropping %d tag(s) longer than %d characters: %s",
                           platform, len(too_long), per_tag, ", ".join(too_long[:5]))
            out = [t for t in out if len(t) <= per_tag]

    max_count = b.get("count")
    if max_count and len(out) > max_count:
        out = out[:max_count]

    max_chars = b.get("chars")
    if max_chars:
        while out and len(" ".join(out)) > max_chars:
            out.pop()

    if core_count and len(out) < core_count:
        logger.warning(
            "%s tag budget cut into the core set: %d of %d core tags fit (%s)",
            platform, len(out), core_count, describe(platform))

    min_count = b.get("min_count")
    if min_count and 0 < len(out) < min_count:
        # Not padded — inventing tags to satisfy a minimum is worse than the
        # upload failing with a message that says what is wrong.
        logger.warning("%s wants at least %d tags; this piece has %d",
                       platform, min_count, len(out))
    return out


def preview(tags: list[str], platform: str) -> dict:
    """What this platform gets, and what it loses. Feeds the UI."""
    kept = fit(tags, platform)
    kept_set = {t.lower() for t in kept}
    dropped = [t for t in (tags or []) if t.lower() not in kept_set]
    return {
        "platform": platform,
        "limit": describe(platform),
        "sent": len(kept),
        "total": len(tags or []),
        "dropped": dropped,
        "chars": len(" ".join(kept)),
    }
