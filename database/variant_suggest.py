"""Suggest Masterpiece variant families by TITLE (2.160.0).

The operator imported a ~165-image collection and got ~200 separate Masterpieces, then:
*"i thought we implemented them into variants … it didnt merge the variants."*

It didn't, and it couldn't: the de-dup finder (2.144.0) groups by **perceptual
hash** — the same *image* cross-posted to several sites. But a rough sketch and
the finished colour piece, or an SFW and an NSFW edit, are **different images**,
so hash-matching correctly never groups them. The signal that ties those together
is the **title**: ``Midnight Snack`` + ``Midnight Snack (Rough)``,
``Ki — Reference Sheet (SFW)`` + ``(NSFW)``.

This module finds those families by stripping stage/edit qualifiers from titles.
It only *suggests* — the caller reviews each family and merges via the existing
``POST /merge-as-variant``. A title heuristic is fuzzy (it won't catch a final
that was retitled, e.g. ``… for the Night``), so review, never auto-merge.
"""
from __future__ import annotations

import re
import sqlite3

# Trailing qualifier in ()/[] that marks a render STAGE or EDIT of one piece,
# not a different piece. Anchored to the end and applied repeatedly so
# "Foo (Rough) (SFW)" reduces fully. Kept deliberately conservative — a word not
# on this list (e.g. "for the Night") is treated as part of the real title, so
# two genuinely different pieces don't get merged on a coincidental suffix.
_QUALIFIER_WORDS = (
    "early rough", "rough", "sketch", "sketchy", "lines", "lineart", "line art",
    "pencil", "pencils", "inks", "inked", "wip", "draft", "final", "finished",
    "clean", "colou?red", "colou?r", "flat", "flats", "shaded", "render",
    "nsfw", "sfw", "censored", "uncensored", "clothed", "nude", "alt",
    "alternate", "variant", "v[0-9]+", "ych", "painted ych", "commission",
)
_QUALIFIER_RE = re.compile(
    r"\s*[\(\[]\s*(?:" + "|".join(_QUALIFIER_WORDS) + r")\b[^\)\]]*[\)\]]\s*$",
    re.IGNORECASE,
)

# The qualifier text itself (for deriving a variant key + label).
_QUALIFIER_CAPTURE = re.compile(r"[\(\[]\s*([^\)\]]+?)\s*[\)\]]\s*$")


def base_title(title: str) -> str:
    """The piece's title with any trailing stage/edit qualifiers peeled off.

    ``"Midnight Snack (Rough)"`` -> ``"midnight snack"``. Idempotent; returns the
    lower-cased, whitespace-collapsed base so it can key a family.
    """
    t = (title or "").strip()
    prev = None
    while prev != t:
        prev = t
        t = _QUALIFIER_RE.sub("", t).strip()
    return re.sub(r"\s+", " ", t).lower()


def _qualifier_of(title: str) -> str:
    """The raw qualifier text, e.g. ``"Rough"`` from ``"Foo (Rough)"`` (""=none)."""
    # Only report it as a qualifier if stripping it actually changes the base —
    # otherwise a title that simply ends in "(YCH)"-but-not-listed stays whole.
    if base_title(title) == (title or "").strip().lower():
        return ""
    m = _QUALIFIER_CAPTURE.search((title or "").strip())
    return m.group(1).strip() if m else ""


def variant_key(qualifier: str) -> str:
    """Slugify a qualifier into a ``_VARIANT_KEY_RE``-safe key (a-z0-9_-)."""
    k = re.sub(r"[^a-z0-9_-]+", "-", (qualifier or "").lower()).strip("-")
    return k or "variant"


def suggest_families(items: list[dict], dismissed: set | None = None) -> list[dict]:
    """Group artworks into likely variant families by base title.

    ``items``: ``[{name, title, views?}, …]`` (title falls back to name).
    ``dismissed``: a set of unordered ``frozenset({name_a, name_b})`` pairs the
    user has marked "not variants"; a family is dropped if ALL its cross-pairs are
    dismissed, and any fully-dismissed member is excluded first.

    Returns ``[{base, hero, members:[{name, title, key, label, is_hero}]}]`` for
    every family of 2+, hero first. The hero is the member with no qualifier if
    exactly one qualifies, else the most-viewed; its key is ``''`` (primary).
    """
    dismissed = dismissed or set()
    fams: dict[str, list[dict]] = {}
    for it in items:
        title = (it.get("title") or it.get("name") or "").strip()
        base = base_title(title)
        if not base:
            continue
        fams.setdefault(base, []).append({
            "name": it["name"],
            "title": title,
            "views": int(it.get("views") or 0),
            "qualifier": _qualifier_of(title),
        })

    out: list[dict] = []
    for base, members in fams.items():
        if len(members) < 2:
            continue
        members = _drop_fully_dismissed(members, dismissed)
        if len(members) < 2:
            continue

        # Hero: the unqualified member if there's exactly one, else most-viewed.
        unqualified = [m for m in members if not m["qualifier"]]
        hero = unqualified[0] if len(unqualified) == 1 else max(
            members, key=lambda m: (m["views"], m["name"]))

        resolved = []
        for m in members:
            is_hero = m["name"] == hero["name"]
            qual = m["qualifier"] or ("" if is_hero else "alt")
            resolved.append({
                "name": m["name"],
                "title": m["title"],
                "views": m["views"],
                "is_hero": is_hero,
                # Hero keeps the primary key (''); others get a slug from their
                # qualifier (or 'alt' when they somehow share the hero's clean title).
                "key": "" if is_hero else variant_key(qual),
                "label": "Primary" if is_hero else (m["qualifier"] or "Alt").strip(),
            })
        # Hero first, then by descending views for a stable, sensible order.
        resolved.sort(key=lambda m: (not m["is_hero"], -m["views"], m["title"].lower()))
        # A slug can collide (two "(rough)"s); disambiguate so merge keys stay unique.
        _dedupe_keys(resolved)
        out.append({"base": base, "hero": hero["name"], "members": resolved})

    # Biggest / most-viewed families first — the ones worth acting on.
    out.sort(key=lambda f: (-len(f["members"]),
                            -max(m["views"] for m in f["members"])))
    return out


def _drop_fully_dismissed(members: list[dict], dismissed: set) -> list[dict]:
    """Remove members every one of whose pairings has been dismissed."""
    if not dismissed:
        return members
    kept = []
    for m in members:
        others = [o for o in members if o["name"] != m["name"]]
        if others and all(
                frozenset((m["name"], o["name"])) in dismissed for o in others):
            continue
        kept.append(m)
    return kept


def _dedupe_keys(members: list[dict]) -> None:
    """Make non-hero variant keys unique in place (rough, rough -> rough, rough-2)."""
    seen: set[str] = set()
    for m in members:
        if m["is_hero"]:
            continue
        k, n = m["key"], 2
        while k in seen:
            k = f"{m['key']}-{n}"
            n += 1
        m["key"] = k
        seen.add(k)


# ── "Not variants" dismissals ─────────────────────────────────────────────────
# DELIBERATELY separate from the hash finder's `masterpiece_not_duplicate`
# ("not the same image"): an SFW and an NSFW edit ARE different images (so they
# may be dismissed there) yet ARE variants of one piece — conflating the two
# judgments would hide exactly the families this finder exists to surface. Own
# table, created lazily here so this stays out of the central schema/migrations.

def ensure_dismiss_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS masterpiece_not_variant ("
        "  name_a TEXT NOT NULL, name_b TEXT NOT NULL,"
        "  PRIMARY KEY (name_a, name_b))")


def add_not_variant(conn: sqlite3.Connection, names: list[str]) -> int:
    """Remember that these Masterpieces are NOT variants of one piece. Records
    every pair in the family (normalised a<b) so it never regroups. Returns new
    pairs stored."""
    ensure_dismiss_table(conn)
    uniq = sorted({n for n in (names or []) if n})
    added = 0
    for i in range(len(uniq)):
        for j in range(i + 1, len(uniq)):
            cur = conn.execute(
                "INSERT OR IGNORE INTO masterpiece_not_variant (name_a, name_b) "
                "VALUES (?, ?)", (uniq[i], uniq[j]))
            added += cur.rowcount
    conn.commit()
    return added


def not_variant_pairs(conn: sqlite3.Connection) -> set:
    """Every dismissed pair as an unordered ``frozenset({a, b})`` — the shape
    :func:`suggest_families` expects for its ``dismissed`` argument."""
    ensure_dismiss_table(conn)
    return {
        frozenset((r["name_a"], r["name_b"]))
        for r in conn.execute("SELECT name_a, name_b FROM masterpiece_not_variant")
    }
