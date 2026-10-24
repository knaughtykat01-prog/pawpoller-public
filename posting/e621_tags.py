"""e621's tag format and rules — one place, shared by post, edit and the pre-check (055, E621TAGS).

e621 refuses a whole upload over one tag it doesn't accept, and the old poster sent the
library's tags as written: ``Red Fox`` arrived as two tags, ``tail__fluff`` was refused.
``prepare`` formats each tag the way e621 itself normalises names, then checks what is
left against e621's own validator, so a tag e621 would refuse is left out *with a reason*
rather than sinking the post. Posting, editing and the pre-check all call it, so the three
can't disagree.

The rules mirror e621ng ``app/logical/tag_name_validator.rb`` and the reserved prefixes
its ``TagQuery::METATAGS`` / ``TagCategory::MAPPING`` constants (fetched 2026-10-24).
Plain string rules, nothing learned.
"""
from __future__ import annotations

import re
import unicodedata

# TagQuery::METATAGS (md5 order limit child randseed ratinglocked notelocked statuslocked
# + NEGATABLE_METATAGS incl. the CATEGORY_METATAG_MAP "<short>tags" keys + COUNT + BOOLEAN),
# frozen 2026-10-24.
METATAGS = frozenset("""
md5 order limit child randseed ratinglocked notelocked statuslocked
id filetype type rating description parent user user_id approver flagger deletedby delreason
source status pool set search fav favoritedby note locked
upvote votedup upvoted voteup downvote voteddown downvoted votedown voted vote
width height mpixels ratio filesize duration score favcount date age change tagcount
commenter comm noter noteupdater flagreason flagnote flaggedby
gentags arttags conttags copytags chartags spectags invtags metatags lortags
comment_count
hassource hasdescription isparent ischild hasparent haschild haschildren inpool
pending_replacements artverified
""".split())

# TagCategory::MAPPING keys, frozen 2026-10-24.
CATEGORIES = frozenset("""
general gen artist art contributor contrib cont copyright copy co
character char ch oc species spec invalid inv meta lore lor
""".split())

# Our own upload syntax — e621 reads these as "file this tag under that category".
_OWN_PREFIXES = ("artist:", "character:")

_BAD_START = "-~_+`(){}[]/:"
_BAD_ANYWHERE = "*,#%$\\"


def format_tag(t) -> str:
    """e621's own normalisation plus the fixes it would otherwise refuse:
    trim, lower case, whitespace → ``_``, collapse ``_`` runs, strip edge ``_``."""
    t = unicodedata.normalize("NFC", str(t or "")).strip().lower()
    t = re.sub(r"\s+", "_", t)
    t = re.sub(r"_+", "_", t)
    return t.strip("_")


def rule_broken(t: str) -> str | None:
    """The plain-words reason e621 would refuse an already-formatted tag, or None."""
    if not t.strip("_"):
        return "empty"
    if t[0] in _BAD_START:
        return f"can't start with {t[0]}"
    for c in _BAD_ANYWHERE:
        if c in t:
            return f"contains {c}"
    for c in "_-~":
        if c * 2 in t:
            return f"has a double {c}"
    if any(unicodedata.category(c) in ("Zs", "Cf", "Cc", "Co", "Cn") for c in t):
        return "has a hidden character"
    if not t.isascii():
        return "has accents or symbols e621 doesn't allow"
    head, sep, rest = t.partition(":")
    if sep and rest and (head in METATAGS or head in CATEGORIES):
        return f"starts with {head}:, which e621 reserves"
    return None


def prepare(tags) -> tuple[list[str], list[dict]]:
    """``(sent, left_out)``: tags formatted for e621, de-duplicated (first wins), and
    every tag it would refuse as ``{"tag": <as written>, "rule": <plain words>}``.

    An ``artist:`` / ``character:`` tag is formatted and checked on the name part, then
    re-prefixed."""
    sent: list[str] = []
    left_out: list[dict] = []
    seen: set[str] = set()
    for raw in tags or []:
        if not str(raw or "").strip():
            continue
        low = str(raw).strip().lower()
        prefix = next((p for p in _OWN_PREFIXES if low.startswith(p)), "")
        name = format_tag(str(raw).strip()[len(prefix):])
        why = rule_broken(name)
        if why:
            left_out.append({"tag": str(raw), "rule": why})
            continue
        tag = prefix + name
        # A bare tag and its prefixed copy are one tag to e621.
        key = name
        if key in seen:
            continue
        seen.add(key)
        sent.append(tag)
    return sent, left_out
