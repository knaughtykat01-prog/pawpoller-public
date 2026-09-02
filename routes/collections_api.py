"""Collections API — a user-curated master container for one piece across every
platform it lives on (+ optional companion story). See docs/specs/collections.md.

Members are polymorphic references ('work' | 'submission' | 'post'); the detail
endpoint resolves them live into pooled analytics / locations / tags / personas.
"""
import logging

from fastapi import APIRouter, HTTPException

from database.db import get_connection
from database import collections_queries as cq

logger = logging.getLogger(__name__)

collections_router = APIRouter(prefix="/api/collections", tags=["collections"])

_MEMBER_TYPES = {"work", "submission", "post", "masterpiece"}


@collections_router.get("")
def list_collections():
    """All collections + a light rollup (totals / platforms / personas) for the grid."""
    conn = get_connection()
    try:
        return {"collections": cq.list_collections_with_summary(conn)}
    finally:
        conn.close()


@collections_router.get("/suggestions")
def suggest_collections():
    """Auto-suggest un-collected cross-platform lookalikes to fold into a
    collection (title similarity today; + perceptual-hash image similarity in
    Phase 4). Declared BEFORE /{cid} so the static path wins over the int param.
    """
    conn = get_connection()
    try:
        return {"suggestions": cq.auto_suggest_collections(conn)}
    finally:
        conn.close()


@collections_router.post("/hash-scan")
def hash_scan(limit: int = 200):
    """Populate the perceptual-hash store so image-based suggestions can work:
    hash every local artwork (zero network) + fetch/hash up to `limit` un-hashed
    thumbnails from allowlisted public CDNs. Declared before /{cid}. Server-side
    fetch is https-only, allowlist-guarded and redirect-disabled (no SSRF).
    """
    import httpx
    from database import image_hash
    conn = get_connection()
    try:
        local = image_hash.hash_local_artworks(conn)
        with httpx.Client(timeout=15.0, follow_redirects=False) as client:
            def _fetch(url):
                if not image_hash.is_allowed_thumb_url(url):
                    return None  # defence in depth (missing_thumb_targets already filters)
                try:
                    r = client.get(url)
                    r.raise_for_status()
                    data = r.content
                    return data if len(data) <= image_hash._MAX_IMAGE_BYTES else None
                except Exception:
                    return None
            thumbs = image_hash.hash_scan(conn, _fetch, limit=max(1, min(int(limit), 1000)))
        return {"status": "ok", "local_artwork": local, "thumbnails": thumbs}
    finally:
        conn.close()


@collections_router.post("")
def create_collection(body: dict):
    """Create a collection. Body: {name, cover_kind?, cover_ref?, notes?, members?}.

    `members` (optional) is a list of {member_type, member_ref, role?} added atomically.
    """
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, detail="name is required")
    conn = get_connection()
    try:
        cid = cq.create_collection(
            conn, name=name, cover_kind=body.get("cover_kind", ""),
            cover_ref=body.get("cover_ref", ""), notes=body.get("notes", ""))
        for m in (body.get("members") or []):
            mt = m.get("member_type")
            mr = m.get("member_ref")
            if mt in _MEMBER_TYPES and mr:
                cq.add_member(conn, cid, mt, str(mr), m.get("role", ""))
        conn.commit()
        return {"status": "created", "id": cid}
    finally:
        conn.close()


@collections_router.get("/{cid}")
def get_collection(cid: int):
    """Full detail: metadata + resolved locations + pooled totals + tags + personas + story."""
    conn = get_connection()
    try:
        roll = cq.rollup_collection(conn, cid)
        if not roll:
            raise HTTPException(404, detail="Collection not found")
        return roll
    finally:
        conn.close()


@collections_router.get("/{cid}/snapshots")
def get_collection_snapshots(cid: int):
    """Combined time-series (summed views/faves/comments) across the collection's
    submission + work members — the chart the Cross-Platform screen used to own."""
    conn = get_connection()
    try:
        if not cq.get_collection(conn, cid):
            raise HTTPException(404, detail="Collection not found")
        from database import analytics_queries
        pairs = cq.collection_member_pairs(conn, cid)
        return {"snapshots": analytics_queries.get_combined_snapshots(conn, pairs)}
    finally:
        conn.close()


@collections_router.patch("/{cid}")
def update_collection(cid: int, body: dict):
    """Update name / cover_kind / cover_ref / notes."""
    conn = get_connection()
    try:
        if not cq.get_collection(conn, cid):
            raise HTTPException(404, detail="Collection not found")
        cq.update_collection(
            conn, cid, name=body.get("name"), cover_kind=body.get("cover_kind"),
            cover_ref=body.get("cover_ref"), notes=body.get("notes"))
        conn.commit()
        return {"status": "updated"}
    finally:
        conn.close()


@collections_router.delete("/{cid}")
def delete_collection(cid: int):
    conn = get_connection()
    try:
        cq.delete_collection(conn, cid)
        conn.commit()
        return {"status": "deleted"}
    finally:
        conn.close()


@collections_router.post("/{cid}/members")
def add_member(cid: int, body: dict):
    """Add a member. Body: {member_type: work|submission|post, member_ref, role?}."""
    mt = body.get("member_type")
    mr = body.get("member_ref")
    if mt not in _MEMBER_TYPES or not mr:
        raise HTTPException(400, detail="member_type (work|submission|post) and member_ref are required")
    conn = get_connection()
    try:
        if not cq.get_collection(conn, cid):
            raise HTTPException(404, detail="Collection not found")
        cq.add_member(conn, cid, mt, str(mr), body.get("role", ""))
        conn.commit()
        return {"status": "added"}
    finally:
        conn.close()


@collections_router.delete("/{cid}/members")
def remove_member(cid: int, member_type: str, member_ref: str):
    """Remove a member (query params: member_type, member_ref)."""
    conn = get_connection()
    try:
        cq.remove_member(conn, cid, member_type, member_ref)
        conn.commit()
        return {"status": "removed"}
    finally:
        conn.close()
