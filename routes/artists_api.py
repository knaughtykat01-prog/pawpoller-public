"""Artist registry API — the lookup made live (3.10.0).

The August 2026 research (44 artists, 134 verified handles across fa/e621/da/
tw/bsky/ib/ws/sf/fn/ik, each graded and with the rejections written down) was
applied once as a migration payload and then sat in a workspace JSON file the
app could not read. These routes put it behind the Masterpiece editor, so
typing a name that is already known fills in every handle that was verified for
them — and surfaces the flags for the artists whose terms restrict reposting,
which is the one thing you must not find out *after* posting.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from database.db import get_connection
from database import artist_queries as aq

logger = logging.getLogger(__name__)

artists_router = APIRouter(prefix="/api/artists", tags=["artists"])


def _works_by_artist_key() -> dict[str, list[str]]:
    """``{artist_key: [work names]}`` across the whole archive.

    The credit is stored INLINE on each `masterpiece.json`, not as a foreign key
    — that is what makes a rename a two-part job — so answering "how many pieces
    does this artist have" and "what would a rename touch" both mean reading the
    folders. 162 of them; cheap enough to do per request and always correct,
    which a cached count would not be.
    """
    from posting import artwork_reader

    out: dict[str, list[str]] = {}
    for w in artwork_reader.list_artworks():
        art = w.get("artist") or {}
        name = (art.get("name") or "").strip() if isinstance(art, dict) else str(art).strip()
        if not name:
            continue
        out.setdefault(aq.artist_key(name), []).append(w.get("name", ""))
    return out


@artists_router.get("")
def list_artists(q: str = "", limit: int = 500, with_counts: bool = False):
    """Every known artist, or those matching ``q`` by name, alias or handle.

    ``with_counts=true`` adds ``works`` per artist. Off by default because it
    reads every folder in the archive, and the picker — which opens on a
    keystroke — does not need it.
    """
    conn = get_connection()
    try:
        # `count()` returns its own "artists" key (a number). Spreading it here
        # replaced the LIST with that integer — nested, not merged.
        artists = aq.list_artists(conn, q, limit)
        if with_counts:
            by_key = _works_by_artist_key()
            for a in artists:
                a["works"] = len(by_key.get(a["key"], []))
        return {"artists": artists,
                "totals": aq.count(conn),
                "platforms": list(aq.KNOWN_PLATFORMS)}
    finally:
        conn.close()


@artists_router.post("/{key}/rename")
def rename_artist(key: str, body: dict):
    """Rename an artist. ``{new_name, apply?: bool}``.

    **Without ``apply`` this only previews.** A rename has to reach the works —
    `masterpiece.json` stores the artist's name inline, so renaming in the
    registry alone leaves every piece still crediting the old spelling — and
    rewriting artwork metadata is never done without first showing exactly which
    pieces change.
    """
    new_name = str((body or {}).get("new_name") or "").strip()
    if not new_name:
        raise HTTPException(400, detail="new_name is required")

    conn = get_connection()
    try:
        current = aq.get_artist(conn, key)
        if not current:
            raise HTTPException(404, detail="Artist not found")
        new_key = aq.artist_key(new_name)
        if not new_key:
            raise HTTPException(400, detail="new_name must contain letters or digits")
        clash = new_key != key and aq.get_artist(conn, new_key) is not None
        works = _works_by_artist_key().get(key, [])

        if not (body or {}).get("apply"):
            return {"status": "preview", "from": current["name"], "to": new_name,
                    "new_key": new_key, "rekeyed": new_key != key,
                    "works": sorted(works), "conflict": clash}
        if clash:
            raise HTTPException(409, detail=(
                f"An artist called {new_name!r} already exists. Renaming onto them "
                f"would have to merge two sets of handles and flags — move the "
                f"handles across deliberately instead."))

        result = aq.rename_artist(conn, key, new_name)
        conn.commit()
    finally:
        conn.close()

    # Now the works. Done after the registry commit so a file failure cannot
    # leave the registry half-renamed; a work that fails is reported, not hidden.
    from posting import artwork_reader

    updated, failed = [], []
    for name in sorted(works):
        try:
            raw = artwork_reader.read_raw_metadata(name)
            art = dict(raw.get("artist") or {})
            art["name"] = new_name
            artwork_reader.save_artwork_metadata(name, {"artist": art})
            updated.append(name)
        except Exception as e:
            logger.warning("Rename: could not update %s: %s", name, e)
            failed.append(name)
    return {"status": "renamed", **result,
            "works_updated": updated, "works_failed": failed}


@artists_router.get("/resolve")
def resolve_artist(name: str):
    """Resolve a typed name to a registry entry, or ``null``.

    Separate from the list route because the editor asks this on every name
    change: the answer is what auto-fills the handles, and it has to tolerate
    the spelling the user actually typed rather than the canonical one.
    """
    conn = get_connection()
    try:
        return {"artist": aq.find_by_name(conn, name)}
    finally:
        conn.close()


@artists_router.get("/{key}")
def get_artist(key: str):
    conn = get_connection()
    try:
        artist = aq.get_artist(conn, key)
        if not artist:
            raise HTTPException(404, detail="Artist not found")
        return artist
    finally:
        conn.close()


@artists_router.post("")
def upsert_artist(body: dict):
    """Create or update an artist.

    ``{name, handles?: {platform: handle}, aliases?, flags?, notes?,
    replace_handles?}``. Handles MERGE by default — see
    ``artist_queries.upsert_artist``: a handle added by hand for a platform the
    lookup never resolved must survive the next import.
    """
    name = str((body or {}).get("name") or "").strip()
    if not name:
        raise HTTPException(400, detail="name is required")
    handles = body.get("handles")
    if handles is not None and not isinstance(handles, dict):
        raise HTTPException(400, detail="handles must be an object")
    conn = get_connection()
    try:
        key = aq.upsert_artist(
            conn, name,
            handles=handles or {},
            aliases=body.get("aliases") if isinstance(body.get("aliases"), list) else None,
            flags=body.get("flags") if isinstance(body.get("flags"), list) else None,
            notes=str(body.get("notes") or ""),
            replace_handles=bool(body.get("replace_handles")))
        conn.commit()
        return {"status": "saved", **(aq.get_artist(conn, key) or {})}
    finally:
        conn.close()


@artists_router.delete("/{key}/handles/{platform}")
def delete_handle(key: str, platform: str):
    """Drop one platform handle. The artist itself is never deleted here —
    losing a credit is worse than carrying a stale handle."""
    conn = get_connection()
    try:
        if not aq.get_artist(conn, key):
            raise HTTPException(404, detail="Artist not found")
        aq.remove_handle(conn, key, platform)
        conn.commit()
        return {"status": "removed", **(aq.get_artist(conn, key) or {})}
    finally:
        conn.close()
