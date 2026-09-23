"""Character registry API (4.33.0, backlog CHARREG).

The people registry's shape, for characters: list / resolve / upsert / rename-with-
preview / delete. Mirrors `routes/artists_api.py` deliberately — a reader who knows one
knows the other, and the picker on the front end is the same picker with a different
noun.

The one rule worth restating here: a piece stores a character's NAME inline, the same
way it stores the artist's, so the archive stays readable on its own. That makes a
rename two jobs — the registry, then the files — and the second only ever runs after the
first has shown which pieces it would rewrite.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from database import artist_queries as aq
from database import character_queries as cq
from database.db import get_connection

logger = logging.getLogger(__name__)

characters_router = APIRouter(prefix="/api/characters", tags=["characters"])


def _works_by_character_key() -> dict[str, list[str]]:
    """``{character_key: [work names]}`` across the archive.

    Characters live inline on each `masterpiece.json`, so "how many pieces feature
    her" and "what would a rename touch" both mean reading the folders — the same
    trade-off `artists_api._works_by_artist_key` makes, and cheap enough at this size.
    """
    from posting import artwork_reader

    out: dict[str, list[str]] = {}
    for w in artwork_reader.list_artworks():
        for name in (w.get("characters") or []):
            key = cq.character_key(name)
            if key:
                out.setdefault(key, []).append(w.get("name", ""))
    return out


@characters_router.get("")
def list_characters(q: str = "", limit: int = 500, with_counts: bool = False):
    """Every character, with their owners resolved for display.

    ``with_counts`` reads every artwork folder, so the registry page asks for it and
    the picker does not.
    """
    conn = get_connection()
    try:
        rows = cq.list_characters(conn, q=q, limit=limit)
        owners = aq.people_for(conn, [r["owner_key"] for r in rows if r["owner_key"]])
        counts = _works_by_character_key() if with_counts else {}
        for r in rows:
            owner = owners.get(r["owner_key"]) if r["owner_key"] else None
            r["owner"] = {"key": owner["key"], "name": owner["name"]} if owner else None
            if with_counts:
                r["works"] = len(counts.get(r["key"], []))
        return {"characters": rows, "totals": cq.count(conn)}
    finally:
        conn.close()


@characters_router.get("/resolve")
def resolve_character(name: str):
    """Resolve a typed spelling to a row, or ``null`` — what the picker asks on every
    keystroke so a known character fills in its owner and tag."""
    conn = get_connection()
    try:
        return {"character": cq.find_by_name(conn, name)}
    finally:
        conn.close()


@characters_router.post("")
def upsert_character(body: dict):
    """Create or update one character.

    ``{name, owner_key?, booru_tag?, species?, notes?, aliases?}``. A field left out is
    left alone: the picker sends a name and an owner, the registry page sends notes and
    a species, and neither should wipe the other's work.
    """
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, detail="A character needs a name")
    fields = {k: body[k] for k in ("owner_key", "booru_tag", "species", "notes", "aliases")
              if k in body}
    conn = get_connection()
    try:
        if fields.get("owner_key") and not aq.get_artist(conn, str(fields["owner_key"])):
            raise HTTPException(400, detail="That owner is not in People")
        row = cq.upsert_character(conn, name, **fields)
        conn.commit()
        return {"character": row}
    finally:
        conn.close()


@characters_router.post("/{key}/rename")
def rename_character(key: str, body: dict):
    """Preview a rename, or apply it to the registry AND the pieces.

    Without ``apply: true`` this only reports what would change, including the works
    that name this character — rewriting artwork metadata is never done blind.
    """
    new_name = str(body.get("new_name", "")).strip()
    if not new_name:
        raise HTTPException(400, detail="A character needs a name")
    apply = bool(body.get("apply"))

    conn = get_connection()
    try:
        existing = cq.get_character(conn, key)
        if not existing:
            raise HTTPException(404, detail="Character not found")
        works = sorted(_works_by_character_key().get(key, []))
        new_key = cq.character_key(new_name)
        conflict = new_key != key and cq.get_character(conn, new_key) is not None

        if not apply:
            return {"status": "preview", "from": existing["name"], "to": new_name,
                    "new_key": new_key, "rekeyed": new_key != key,
                    "works": works, "conflict": conflict}
        if conflict:
            raise HTTPException(409, detail=f"A character called {new_name!r} already exists")

        result = cq.rename_character(conn, key, new_name)
        conn.commit()
    finally:
        conn.close()

    updated, failed = [], []
    from posting import artwork_reader
    for work in works:
        try:
            art = artwork_reader.load_artwork(work)
            names = [new_name if cq.character_key(n) == key else n
                     for n in (art.characters or [])]
            people = []
            for p in (art.people or []):
                if cq.character_key(p.get("character", "")) == key:
                    p = {**p, "character": new_name}
                people.append(p)
            artwork_reader.save_artwork_metadata(work, {"characters": names, "people": people})
            updated.append(work)
        except Exception as e:                       # one bad file must not strand the rest
            logger.warning("Character rename: could not update %s: %s", work, e)
            failed.append(work)

    return {"status": "renamed", **result, "works_updated": updated, "works_failed": failed}


@characters_router.get("/{key}")
def get_character(key: str):
    conn = get_connection()
    try:
        row = cq.get_character(conn, key)
        if not row:
            raise HTTPException(404, detail="Character not found")
        return row
    finally:
        conn.close()


@characters_router.delete("/{key}")
def delete_character(key: str, confirm: bool = False):
    """Remove a character from the registry.

    The pieces keep the name they have written down — they simply stop resolving it to
    an owner or a booru tag. Still a change worth seeing coming, so an unconfirmed
    delete of a character that appears on any piece answers 409 with the count.
    """
    conn = get_connection()
    try:
        row = cq.get_character(conn, key)
        if not row:
            raise HTTPException(404, detail="Character not found")
        works = _works_by_character_key().get(key, [])
        if works and not confirm:
            raise HTTPException(409, detail={
                "message": (f"{row['name']} appears on {len(works)} piece"
                            f"{'' if len(works) == 1 else 's'}. Removing them from the "
                            f"registry keeps the name on every piece — only the owner "
                            f"link and the booru tag go."),
                "works": sorted(works)[:20],
                "work_count": len(works),
            })
        cq.delete_character(conn, key)
        conn.commit()
        logger.info("Characters: removed %s (on %d works)", key, len(works))
        return {"status": "deleted", "key": key, "name": row["name"], "work_count": len(works)}
    finally:
        conn.close()
