"""The artist registry: who drew a piece, and where to find them.

3.5.0 made ``artist`` a structured field on ``masterpiece.json`` and taught
``posting/artist_credit.py`` to render it in each platform's own markup
(``:iconinkwolf:`` on FA, ``[fa]inkwolf[/fa]`` on Inkbunny, a DText link on
e621…). The August 2026 lookup then researched **44 artists / 134 handles**
across eleven platforms, graded each handle confirmed/likely, recorded the
rejections with reasons, and flagged the artists whose terms restrict reposting.

That research was applied **once**, as a migration payload, and then went
dormant in a workspace JSON file the app could not read. The visible cost: a
piece with no artist could not be fixed from the dashboard at all — the UI said
"add the artist to masterpiece.json" — and had it been fixable, it would have
meant hand-retyping handles that were already researched and verified, with no
warning for the artists who carry repost prohibitions.

This module is that registry made live. ``artist_handles`` is a real table
rather than a JSON column because its entire purpose is answering "where is this
artist on <platform>", which a blob cannot be indexed for.
"""
from __future__ import annotations

import json
import re
import sqlite3

# The platforms the registry actually carries handles for, most-covered first
# (fa 30 · e621 25 · da 20 · tw 20 · bsky 18 · ib 8 · ws 6 · sf 3 · fn 2 · ik 2).
# Not a whitelist — an unknown platform is still stored — just the display order.
# Instagram (3.13.0) is last because it is the newest and has no coverage yet,
# not because it matters least. No migration was needed to add it: `platform`
# is a free TEXT column with no CHECK constraint and `upsert_artist` never
# filtered against this tuple, so storage already accepted any code — this
# list is what the UI offers, not what the table permits.
KNOWN_PLATFORMS = ("fa", "e621", "da", "tw", "bsky", "ib", "ws", "sf", "fn",
                   "ik", "ig")

_KEY_STRIP = re.compile(r"[^a-z0-9]+")


def artist_key(name: str) -> str:
    """Stable identity for an artist name.

    Case, spaces and punctuation are stripped because the catalogue credits the
    same person several ways. The lookup hit exactly this: it had to record a
    ``corrected_name`` for "Dan Cresent Wolf" → "Dan Crescent Wolf", and the
    handles are spelled ``DanCrescentWolf``. Collapsing them is what stops one
    artist becoming three rows that each know a third of their handles.
    """
    return _KEY_STRIP.sub("", (name or "").lower())


def _loads(raw, fallback):
    try:
        v = json.loads(raw or "")
    except Exception:
        return fallback
    return v if isinstance(v, type(fallback)) else fallback


# Phrases that make a lookup note a WARNING rather than context (3.10.0).
#
# The lookup wrote 90 notes across the 44 artists, so treating every one as an
# alarm flagged 44 of 44 — which is the same as flagging none. Most are useful
# context ("alias 'Tavi', they/them", "Weasyl dormant since 2015"). A minority
# describe a way to get the credit WRONG: a dead account, a rebrand, a
# look-alike handle, a typo in the recorded one, an explicit prohibition.
#
# Deterministic and explicit by house rule — no model decides this. Matching is
# substring, case-insensitive; a note that matches nothing still shows, just
# without the alarm.
_WARNING_MARKERS = (
    "do not", "don't", "dead", "disabled", "deleted", "rebrand", "not on ",
    "user-not-found", "404", "typo", "prohibit", "no repost", "do-not",
    "different handle", "not the artist", "placeholder", "wrong",
)


def classify_flags(flags) -> tuple[list[str], list[str]]:
    """Split lookup notes into ``(warnings, context)``.

    Both are shown when crediting an artist — the research is only useful if it
    is visible at the moment you are typing a handle — but only the warnings are
    styled as an alarm, so the alarm keeps meaning something.
    """
    warnings, context = [], []
    for raw in flags or []:
        # `str(None)` is "None", which is truthy — a null in the source JSON
        # would otherwise show up in the editor as a note reading "None".
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        (warnings if any(m in text.lower() for m in _WARNING_MARKERS) else context).append(text)
    return warnings, context


def _row_to_artist(row: sqlite3.Row, handles: dict) -> dict:
    flags = _loads(row["flags"], [])
    warnings, context = classify_flags(flags)
    return {
        "key": row["artist_key"],
        "name": row["name"],
        "aliases": _loads(row["aliases"], []),
        "flags": flags,
        "warnings": warnings,
        "context": context,
        "notes": row["notes"] or "",
        "handles": handles,
    }


def _handles_for(conn: sqlite3.Connection, keys: list[str]) -> dict[str, dict]:
    """``{artist_key: {platform: handle}}`` for many artists in ONE query."""
    out: dict[str, dict] = {k: {} for k in keys}
    if not keys:
        return out
    ph = ",".join("?" * len(keys))
    for r in conn.execute(
            "SELECT artist_key, platform, handle FROM artist_handles "
            "WHERE artist_key IN (" + ph + ")", keys):
        out.setdefault(r["artist_key"], {})[r["platform"]] = r["handle"]
    return out


def list_artists(conn: sqlite3.Connection, q: str = "", limit: int = 500) -> list[dict]:
    """Every artist, or those whose name / alias / handle matches ``q``.

    The search covers handles as well as names because the credit tail in an old
    description is often the handle rather than the display name — looking up
    "honeyvanillaa" has to find Azzieworks.
    """
    term = (q or "").strip().lower()
    if term:
        like = "%" + term + "%"
        rows = conn.execute(
            "SELECT * FROM artists WHERE lower(name) LIKE ? OR lower(aliases) LIKE ? "
            "OR artist_key IN (SELECT artist_key FROM artist_handles "
            "                  WHERE lower(handle) LIKE ?) "
            "ORDER BY name COLLATE NOCASE LIMIT ?", (like, like, like, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM artists ORDER BY name COLLATE NOCASE LIMIT ?", (limit,)).fetchall()
    handles = _handles_for(conn, [r["artist_key"] for r in rows])
    return [_row_to_artist(r, handles.get(r["artist_key"], {})) for r in rows]


def get_artist(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute("SELECT * FROM artists WHERE artist_key = ?", (key,)).fetchone()
    if not row:
        return None
    return _row_to_artist(row, _handles_for(conn, [key]).get(key, {}))


def find_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    """Resolve a typed name to a registry entry via the normalised key.

    This is what makes the editor auto-fill: type "azzie works" and get
    Azzieworks's verified handles back without knowing the exact spelling.
    """
    return get_artist(conn, artist_key(name))


def upsert_artist(conn: sqlite3.Connection, name: str, *, handles: dict | None = None,
                  aliases: list | None = None, flags: list | None = None,
                  notes: str = "", replace_handles: bool = False) -> str:
    """Create or update one artist; returns the key.

    ``replace_handles=False`` (the default) MERGES. A handle added by hand for a
    platform the lookup never resolved must survive a later re-import, and an
    import must not have to carry every handle just to avoid destroying one.
    Pass True only when the caller genuinely means "these are now all of them".
    """
    key = artist_key(name)
    if not key:
        raise ValueError("artist name cannot be empty")
    disp = (name or "").strip()
    conn.execute(
        "INSERT INTO artists (artist_key, name, aliases, flags, notes) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(artist_key) DO UPDATE SET "
        "  name = excluded.name, "
        # An empty aliases/flags/notes means "not supplied", never "delete what
        # is stored" — otherwise a quick rename from the editor would silently
        # discard the lookup's rejection notes and repost prohibitions.
        "  aliases = CASE WHEN excluded.aliases = '[]' THEN artists.aliases ELSE excluded.aliases END, "
        "  flags   = CASE WHEN excluded.flags   = '[]' THEN artists.flags   ELSE excluded.flags END, "
        "  notes   = CASE WHEN excluded.notes   = ''   THEN artists.notes   ELSE excluded.notes END, "
        "  updated_at = datetime('now')",
        (key, disp, json.dumps(aliases or []), json.dumps(flags or []), notes or ""))

    if replace_handles:
        conn.execute("DELETE FROM artist_handles WHERE artist_key = ?", (key,))
    for platform, handle in (handles or {}).items():
        h = str(handle or "").strip()
        if not h:
            continue
        conn.execute(
            "INSERT INTO artist_handles (artist_key, platform, handle) VALUES (?, ?, ?) "
            "ON CONFLICT(artist_key, platform) DO UPDATE SET handle = excluded.handle",
            (key, str(platform).strip().lower(), h))
    return key


class ArtistExists(Exception):
    """Renaming would land on a key another artist already holds.

    Refused rather than silently merged: a merge decides which name, which
    flags and which of two conflicting handles survive, and guessing that is how
    research gets destroyed. The caller is told, and can rename the other one or
    move the handles across deliberately.
    """


def rename_artist(conn: sqlite3.Connection, key: str, new_name: str) -> dict:
    """Rename an artist in the registry, carrying their handles and research.

    ``artist_key`` is derived from the name, so a real rename changes the key
    and the handle rows have to move with it. A pure re-spelling ("cherry_kid"
    → "Cherry Kid") normalises to the SAME key and is only a display change —
    handled without touching handles at all.

    The old name is kept as an alias. The catalogue's descriptions credit
    whatever spelling was used at the time, so dropping it would break the
    search that finds them.

    ⚠ This does NOT touch the works — `masterpiece.json` stores the artist name
    inline, so the pieces still credit the old spelling until they are rewritten
    too. That is a separate, listable step on purpose: it edits artwork
    metadata, which is never done without showing what will change.
    """
    old = get_artist(conn, key)
    if not old:
        raise KeyError(key)
    disp = (new_name or "").strip()
    new_key = artist_key(disp)
    if not new_key:
        raise ValueError("artist name cannot be empty")

    aliases = list(old.get("aliases") or [])
    if old["name"] != disp and old["name"] not in aliases:
        aliases.append(old["name"])

    if new_key == key:
        conn.execute(
            "UPDATE artists SET name = ?, aliases = ?, updated_at = datetime('now') "
            "WHERE artist_key = ?", (disp, json.dumps(aliases), key))
        return {"from": old["name"], "to": disp, "key": key, "rekeyed": False}

    if get_artist(conn, new_key) is not None:
        raise ArtistExists(new_key)

    conn.execute(
        "INSERT INTO artists (artist_key, name, aliases, flags, notes) VALUES (?, ?, ?, ?, ?)",
        (new_key, disp, json.dumps(aliases), json.dumps(old.get("flags") or []),
         old.get("notes") or ""))
    conn.execute("UPDATE artist_handles SET artist_key = ? WHERE artist_key = ?", (new_key, key))
    conn.execute("DELETE FROM artists WHERE artist_key = ?", (key,))
    return {"from": old["name"], "to": disp, "key": new_key, "rekeyed": True}


def remove_handle(conn: sqlite3.Connection, key: str, platform: str) -> None:
    conn.execute("DELETE FROM artist_handles WHERE artist_key = ? AND platform = ?",
                 (key, str(platform).strip().lower()))


def count(conn: sqlite3.Connection) -> dict:
    return {
        "artists": conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0],
        "handles": conn.execute("SELECT COUNT(*) FROM artist_handles").fetchone()[0],
    }
