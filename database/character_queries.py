"""The character registry (4.33.0, backlog CHARREG).

Before this, a character was a string typed into a comma-separated box on each piece
(`masterpiece.json: characters[]`). That meant:

  * no canonical spelling — "Kii", "kii" and "Kii " were three different characters;
  * no dedup across works, so a rename meant editing every piece by hand;
  * no owner link except inside one piece's `people[]` row, repeated per piece;
  * and no route to a post at all, while the *same* character already existed on the
    tag side as a `name_(owner)` booru tag that reaches every post and sits at tier 3
    in `scripts/reorder_tags.py`.

This is the people registry (`database/artist_queries.py`) minus handles: a key derived
from the name, an optional owner pointing at `artists.artist_key`, and the booru tag that
closes the gap to the tag side. The shape is deliberately the same so the page, the
picker and the rename-with-preview all read like their People counterparts.

⚠ Works are NOT foreign-keyed to this table. A piece still stores the character's NAME
inline, exactly as it stores the artist's, because the archive has to stay readable on
its own. That is what makes a rename a two-part job (registry, then the files) and why
:func:`rename_character` only does the first half — the route rewrites the works, after
showing which ones it would touch.
"""
from __future__ import annotations

import json
import re
import sqlite3


class CharacterExists(Exception):
    """A rename would land on a character that already exists."""


def character_key(name: str) -> str:
    """The identity of a character, from its name.

    Case, spaces and punctuation are stripped, so "Kii", "kii" and "K I I" are one
    character rather than three rows. Same rule as `artist_queries.artist_key`, so a
    reader who knows one knows the other.
    """
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _loads(raw, fallback):
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
        return val if isinstance(val, type(fallback)) else fallback
    except (TypeError, ValueError):
        return fallback


def _row(r: sqlite3.Row) -> dict:
    return {
        "key": r["character_key"],
        "name": r["name"],
        "owner_key": r["owner_key"] or "",
        # None = not one of the operator's. An int = "this one is mine", and
        # WHICH of their personas it belongs to.
        "persona_id": (r["persona_id"] if "persona_id" in r.keys() else None),
        "booru_tag": r["booru_tag"] or "",
        "species": r["species"] or "",
        "notes": r["notes"] or "",
        "aliases": _loads(r["aliases"], []),
    }


def list_characters(conn: sqlite3.Connection, q: str = "", limit: int = 500) -> list[dict]:
    """Every character, or those matching *q* by name, alias or booru tag."""
    sql = ("SELECT character_key, name, owner_key, persona_id, booru_tag, species, notes, aliases "
           "FROM characters")
    params: list = []
    if q:
        sql += " WHERE name LIKE ? OR aliases LIKE ? OR booru_tag LIKE ?"
        like = f"%{q}%"
        params += [like, like, like]
    sql += " ORDER BY name COLLATE NOCASE LIMIT ?"
    params.append(limit)
    return [_row(r) for r in conn.execute(sql, params)]


def get_character(conn: sqlite3.Connection, key: str) -> dict | None:
    r = conn.execute(
        "SELECT character_key, name, owner_key, persona_id, booru_tag, species, notes, aliases "
        "FROM characters WHERE character_key = ?", (key,)).fetchone()
    return _row(r) if r else None


def find_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    """Resolve a typed spelling. The point of the key: whatever the case or spacing,
    a name that has been seen before comes back as the row it belongs to."""
    return get_character(conn, character_key(name))


# A field left out of an upsert must not be cleared — the picker sends a name and an
# owner, the registry page sends notes and a species, and neither should wipe the
# other's work. Same sentinel as artist_queries.
KEEP = object()


def _persona_or_none(value):
    """An int persona id, or None for "not mine". Accepts the empty string and 0 that
    a <select> sends for its blank option, so the UI does not have to special-case."""
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def upsert_character(conn: sqlite3.Connection, name: str, *,
                     owner_key=KEEP, persona_id=KEEP, booru_tag=KEEP, species=KEEP,
                     notes=KEEP, aliases=KEEP) -> dict:
    """Create or update one character, leaving unsupplied fields alone.

    ⚠ ``owner_key`` and ``persona_id`` are two answers to one question and cannot
    both be true. A character belongs to a People row (someone else) OR to one of
    the operator's personas ("mine"). Supplying either one CLEARS the other, so a
    character reassigned from a friend to yourself does not keep claiming both —
    which would make the badge and the booru tag disagree about whose it is.
    """
    disp = str(name or "").strip()
    key = character_key(disp)
    if not key:
        raise ValueError("character name cannot be empty")

    existing = get_character(conn, key)
    if existing is None:
        conn.execute(
            "INSERT INTO characters (character_key, name, owner_key, persona_id, "
            "booru_tag, species, notes, aliases) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, disp,
             "" if owner_key is KEEP else str(owner_key or ""),
             None if persona_id is KEEP else _persona_or_none(persona_id),
             "" if booru_tag is KEEP else str(booru_tag or ""),
             "" if species is KEEP else str(species or ""),
             "" if notes is KEEP else str(notes or ""),
             json.dumps([] if aliases is KEEP else list(aliases or []))))
        return get_character(conn, key)

    sets, params = ["name = ?", "updated_at = datetime('now')"], [disp]
    # Whichever side of the ownership question was answered, clear the other.
    if owner_key is not KEEP and str(owner_key or ""):
        sets.append("persona_id = NULL")
    if persona_id is not KEEP:
        pid = _persona_or_none(persona_id)
        sets.append("persona_id = ?")
        params.append(pid)
        if pid is not None:
            sets.append("owner_key = ''")
    for field, val in (("owner_key", owner_key), ("booru_tag", booru_tag),
                       ("species", species), ("notes", notes)):
        if val is not KEEP:
            sets.append(f"{field} = ?")
            params.append(str(val or ""))
    if aliases is not KEEP:
        sets.append("aliases = ?")
        params.append(json.dumps(list(aliases or [])))
    params.append(key)
    conn.execute(f"UPDATE characters SET {', '.join(sets)} WHERE character_key = ?", params)
    return get_character(conn, key)


def rename_character(conn: sqlite3.Connection, key: str, new_name: str) -> dict:
    """Rename in the registry, keeping the old spelling as an alias.

    ⚠ Does NOT touch the works: each `masterpiece.json` holds the character's name
    inline, so the pieces still say the old spelling until they are rewritten too. That
    is the route's job, after it has listed which pieces it would change — rewriting
    artwork metadata is never done blind (the same rule as the artist rename).
    """
    old = get_character(conn, key)
    if not old:
        raise KeyError(key)
    disp = str(new_name or "").strip()
    new_key = character_key(disp)
    if not new_key:
        raise ValueError("character name cannot be empty")

    aliases = list(old.get("aliases") or [])
    if old["name"] != disp and old["name"] not in aliases:
        aliases.append(old["name"])

    if new_key == key:
        conn.execute("UPDATE characters SET name = ?, aliases = ?, updated_at = datetime('now') "
                     "WHERE character_key = ?", (disp, json.dumps(aliases), key))
        return {"from": old["name"], "to": disp, "key": key, "rekeyed": False}

    if get_character(conn, new_key) is not None:
        raise CharacterExists(new_key)

    conn.execute(
        "INSERT INTO characters (character_key, name, owner_key, booru_tag, species, notes, aliases) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (new_key, disp, old["owner_key"], old["booru_tag"], old["species"], old["notes"],
         json.dumps(aliases)))
    conn.execute("DELETE FROM characters WHERE character_key = ?", (key,))
    return {"from": old["name"], "to": disp, "key": new_key, "rekeyed": True}


def delete_character(conn: sqlite3.Connection, key: str) -> dict:
    """Remove a character from the registry.

    Works keep the name they have written down, exactly as deleting a person leaves
    every credit in place — the piece simply stops resolving it to an owner or a tag.
    """
    existing = get_character(conn, key)
    if not existing:
        raise KeyError(key)
    conn.execute("DELETE FROM characters WHERE character_key = ?", (key,))
    return existing


def characters_for(conn: sqlite3.Connection, names) -> dict[str, dict]:
    """``{typed name: row}`` for the names a piece lists, resolving each by key.

    The piece keeps whatever spelling it was given; this is how a renderer turns those
    strings into rows without the piece having to store keys.
    """
    out: dict[str, dict] = {}
    for name in (names or []):
        row = find_by_name(conn, name)
        if row:
            out[name] = row
    return out


def booru_tags_for(conn: sqlite3.Connection, names) -> list[str]:
    """The booru tags for a piece's characters, in the order the piece lists them.

    A character with no tag contributes nothing — an invented tag is worse than a
    missing one, since a booru tag that matches nothing is noise on every post.
    """
    seen, tags = set(), []
    for row in characters_for(conn, names).values():
        tag = (row.get("booru_tag") or "").strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def count(conn: sqlite3.Connection) -> dict:
    return {
        "characters": conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0],
        "with_owner": conn.execute(
            "SELECT COUNT(*) FROM characters WHERE owner_key != ''").fetchone()[0],
        "with_tag": conn.execute(
            "SELECT COUNT(*) FROM characters WHERE booru_tag != ''").fetchone()[0],
    }
