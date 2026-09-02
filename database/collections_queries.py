"""Collections — CRUD + the live rollup that resolves a collection's polymorphic
members (works / submissions / posts) into pooled analytics, locations, merged
tags and the persona(s) spanned. See docs/specs/collections.md.

The per-platform stat-column normalisation mirrors
analytics_queries.get_link_combined_stats (the unify-master pooling), so a
Collection and a Master pool stats the same way — both now read the same
registry rather than each carrying a copy.
"""
from __future__ import annotations

import json
import sqlite3

from database import platform_metrics
from database import posting_queries

# Platform table + metric columns come from the canonical registry
# (database/platform_metrics.py). The local copies this replaces were missing
# FurryNetwork and Furbooru entirely, and mapped e621's `score` into the VIEWS
# slot — summing a net up−down total (which can be negative) as page views.
_TABLE_MAP = {code: platform_metrics.table_for(code)
              for code in platform_metrics.ALL_CODES}


# ── CRUD ─────────────────────────────────────────────────────────

def create_collection(conn: sqlite3.Connection, name: str, cover_kind: str = "",
                      cover_ref: str = "", notes: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO collections (name, cover_kind, cover_ref, notes) VALUES (?, ?, ?, ?)",
        (name or "Untitled collection", cover_kind or "", cover_ref or "", notes or ""))
    return cur.lastrowid


def list_collections(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM collections ORDER BY updated_at DESC, id DESC").fetchall()]


def get_collection(conn: sqlite3.Connection, cid: int) -> dict | None:
    row = conn.execute("SELECT * FROM collections WHERE id = ?", (cid,)).fetchone()
    return dict(row) if row else None


def update_collection(conn: sqlite3.Connection, cid: int, **fields) -> None:
    allowed = {"name", "cover_kind", "cover_ref", "notes"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k} = ?")
            params.append(v)
    if not sets:
        return
    sets.append("updated_at = datetime('now')")
    params.append(cid)
    conn.execute(f"UPDATE collections SET {', '.join(sets)} WHERE id = ?", params)


def delete_collection(conn: sqlite3.Connection, cid: int) -> None:
    conn.execute("DELETE FROM collection_members WHERE collection_id = ?", (cid,))
    conn.execute("DELETE FROM collections WHERE id = ?", (cid,))


def get_members(conn: sqlite3.Connection, cid: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT member_type, member_ref, role, added_at FROM collection_members "
        "WHERE collection_id = ? ORDER BY added_at", (cid,)).fetchall()]


def add_member(conn: sqlite3.Connection, cid: int, member_type: str,
               member_ref: str, role: str = "") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO collection_members (collection_id, member_type, member_ref, role) "
        "VALUES (?, ?, ?, ?)", (cid, member_type, member_ref, role or ""))
    conn.execute("UPDATE collections SET updated_at = datetime('now') WHERE id = ?", (cid,))


def remove_member(conn: sqlite3.Connection, cid: int, member_type: str, member_ref: str) -> None:
    conn.execute(
        "DELETE FROM collection_members WHERE collection_id = ? AND member_type = ? AND member_ref = ?",
        (cid, member_type, member_ref))
    conn.execute("UPDATE collections SET updated_at = datetime('now') WHERE id = ?", (cid,))


# ── Rollup ───────────────────────────────────────────────────────

def _acct_to_persona(conn: sqlite3.Connection) -> dict:
    try:
        return {r["account_id"]: r["persona_id"]
                for r in conn.execute("SELECT account_id, persona_id FROM accounts")
                if r["persona_id"]}
    except Exception:
        return {}


def _parse_tags(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw if str(t).strip()]
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [str(t) for t in v if str(t).strip()]
    except Exception:
        pass
    return [t.strip() for t in str(raw).split(",") if t.strip()]


def _submission_row(conn: sqlite3.Connection, platform: str, sid: str) -> dict | None:
    tbl = _TABLE_MAP.get(platform)
    if not tbl:
        return None
    try:
        r = conn.execute(f"SELECT * FROM {tbl} WHERE submission_id = ?", (str(sid),)).fetchone()
    except Exception:
        return None
    return dict(r) if r else None


def _stats_from_row(platform: str, row: dict) -> dict:
    """Canonical {views, favorites, comments, score} for one submission row.

    None (not 0) means "this platform doesn't report that metric" — Itaku and
    Mastodon have no view count, e621 has a score instead. Keeping them apart
    stops a pooled total implying a platform scored zero when it simply doesn't
    measure that thing.
    """
    spec = platform_metrics.get(platform)
    if not spec:
        return {"views": None, "favorites": None, "comments": None, "score": None}
    v, f, c = spec.views, spec.faves, spec.comments
    return {
        "views": (row.get(v, 0) or 0) if v else None,
        "favorites": (row.get(f, 0) or 0) if f else None,
        "comments": (row.get(c, 0) or 0) if c else None,
        "score": (row.get(spec.score, 0) or 0) if spec.score else None,
    }


def _location_from_row(platform: str, sid: str, row: dict | None, *, url: str = "",
                       account_id=None, source: str = "submission") -> dict | None:
    """Build a location dict from an ALREADY-FETCHED submission row (or None).

    Pure — no DB access — so callers that bulk-fetch rows (``_submission_rows_bulk``)
    can resolve every location in Python instead of one query per member. This is
    the split that lets the Masterpiece/Collection/Works rollups batch.
    """
    if not row:
        # Still surface the location even if the poller hasn't stored it (link only).
        if not url:
            return None
        return {"platform": platform, "submission_id": str(sid), "url": url,
                "title": "", "account_id": account_id, "stats": {"views": None, "favorites": None, "comments": None},
                "keywords": [], "source": source}
    return {
        "platform": platform,
        "submission_id": str(sid),
        "url": url or row.get("link") or "",
        "title": row.get("title") or row.get("full_text") or "",
        "account_id": account_id if account_id is not None else row.get("account_id"),
        "stats": _stats_from_row(platform, row),
        "keywords": _parse_tags(row.get("keywords")),
        # Cover image so Collections can SHOW the art at each location, not just
        # name it (the "Collections is missing the artwork" gap). Raw CDN URL;
        # the frontend routes FA/IB/Pixiv through their thumbnail relays.
        "thumbnail_url": row.get("thumbnail_url") or "",
        "source": source,
    }


def _location_from_submission(conn, platform: str, sid: str, *, url: str = "",
                              account_id=None, source: str = "submission") -> dict | None:
    """Resolve one platform submission into a location dict (stats + link + tags).

    Single-row convenience wrapper. Hot paths that resolve many members should
    bulk-fetch with ``_submission_rows_bulk`` and call ``_location_from_row``.
    """
    return _location_from_row(
        platform, sid, _submission_row(conn, platform, sid),
        url=url, account_id=account_id, source=source)


# SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 999 (older builds); chunk well
# under it so a large IN(...) never trips a "too many variables" error.
_IN_CHUNK = 900


def _submission_rows_bulk(conn, pairs) -> dict:
    """Bulk-fetch submission rows for many ``(platform, submission_id)`` pairs.

    ONE query per platform table (chunked under SQLite's variable limit) instead
    of one per pair. Returns ``{(platform, str(sid)): row_dict}``; pairs with no
    stored row are simply absent. This is the batching that keeps the Masterpiece
    / Collection / Works rollups O(platforms) rather than O(members) — the
    per-member ``_location_from_submission`` fan-out was the "live rollup × N"
    cost that made large libraries crawl.
    """
    by_platform: dict[str, set] = {}
    for platform, sid in pairs:
        if platform in _TABLE_MAP:
            by_platform.setdefault(platform, set()).add(str(sid))
    out: dict[tuple, dict] = {}
    for platform, sids in by_platform.items():
        tbl = _TABLE_MAP[platform]
        sid_list = list(sids)
        for i in range(0, len(sid_list), _IN_CHUNK):
            chunk = sid_list[i:i + _IN_CHUNK]
            ph = ",".join("?" * len(chunk))
            try:
                rows = conn.execute(
                    f"SELECT * FROM {tbl} WHERE submission_id IN ({ph})", chunk).fetchall()
            except Exception:
                continue
            for r in rows:
                out[(platform, str(r["submission_id"]))] = dict(r)
    return out


def rollup_collection(conn: sqlite3.Connection, cid: int) -> dict | None:
    """Full detail for one collection: metadata + resolved locations + pooled
    totals + merged tags + persona(s) spanned + the companion story (if any)."""
    coll = get_collection(conn, cid)
    if not coll:
        return None
    members = get_members(conn, cid)
    a2p = _acct_to_persona(conn)

    locations: list[dict] = []
    story = None

    for m in members:
        mtype, mref, role = m["member_type"], m["member_ref"], m.get("role", "")
        if mtype == "submission":
            platform, _, sid = mref.partition(":")
            loc = _location_from_submission(conn, platform, sid, source="submission")
            if loc:
                locations.append(loc)
        elif mtype == "work":
            ct, _, name = mref.partition(":")
            if ct == "story" and story is None:
                story = {"name": name}
            for p in posting_queries.get_publications(conn, story_name=name, content_type=ct):
                if p.get("status") not in (None, "", "posted"):
                    continue
                loc = _location_from_submission(
                    conn, p["platform"], p.get("external_id", ""),
                    url=p.get("external_url", ""), account_id=p.get("account_id"),
                    source=f"work:{ct}")
                if loc:
                    loc["work_name"] = name
                    loc["work_type"] = ct
                    locations.append(loc)
        elif mtype == "masterpiece":
            # A Masterpiece contributes its WHOLE set of site-uploads to the
            # Collection (spec §7) — resolved live from masterpiece_members so the
            # pooled stats/tags/personas stay current. Lazy import avoids a cycle
            # (masterpiece_queries imports this module at load time).
            from database import masterpiece_queries
            for mm in masterpiece_queries.get_members(conn, mref):
                loc = _location_from_submission(
                    conn, mm["platform"], mm["submission_id"],
                    account_id=mm.get("account_id"), source="masterpiece")
                if loc:
                    loc["masterpiece_name"] = mref
                    locations.append(loc)
        # 'post' members: resolved lightly for now (no per-platform stats table);
        # surfaced by the curation UI. Left out of stats rollup intentionally.

    # Pool: sum non-None metrics; merge tags; collect personas + platforms.
    tot = {"views": 0, "favorites": 0, "comments": 0}
    tags: set[str] = set()
    persona_ids: set[int] = set()
    platforms: set[str] = set()
    for loc in locations:
        for k in tot:
            val = loc["stats"].get(k)
            if val:
                tot[k] += val
        tags.update(loc.get("keywords") or [])
        platforms.add(loc["platform"])
        aid = loc.get("account_id")
        if aid in a2p:
            persona_ids.add(a2p[aid])

    return {
        **coll,
        "members": members,
        "locations": locations,
        "totals": {**tot, "platforms": len(platforms), "locations": len(locations)},
        "tags": sorted(tags),
        "persona_ids": sorted(persona_ids),
        "story": story,
    }


def collection_member_pairs(conn: sqlite3.Connection, cid: int) -> list[tuple]:
    """`(platform, submission_id)` pairs for a collection's submission + work
    members (posts excluded — no per-platform stats table). Feeds the combined
    snapshot chart via analytics_queries.get_combined_snapshots."""
    pairs: list[tuple] = []
    for m in get_members(conn, cid):
        mt, mref = m["member_type"], m["member_ref"]
        if mt == "submission":
            platform, _, sid = mref.partition(":")
            if platform and sid:
                pairs.append((platform, sid))
        elif mt == "work":
            ct, _, name = mref.partition(":")
            for p in posting_queries.get_publications(conn, story_name=name, content_type=ct):
                if p.get("status") not in (None, "", "posted"):
                    continue
                ext = p.get("external_id")
                if p.get("platform") and ext:
                    pairs.append((p["platform"], str(ext)))
        elif mt == "masterpiece":
            from database import masterpiece_queries
            pairs.extend(masterpiece_queries.member_pairs(conn, mref))
    return pairs


def _collected_pairs(conn: sqlite3.Connection) -> set:
    """All `(platform, str(submission_id))` pairs already inside ANY collection —
    the exclusion set for suggestions, so we never re-propose a merged piece."""
    existing: set = set()
    try:
        rows = conn.execute(
            "SELECT member_type, member_ref FROM collection_members").fetchall()
    except Exception:
        return existing
    for r in rows:
        mt, mref = r["member_type"], r["member_ref"]
        if mt == "submission":
            platform, _, sid = mref.partition(":")
            if platform and sid:
                existing.add((platform, str(sid)))
        elif mt == "work":
            ct, _, name = mref.partition(":")
            try:
                for p in posting_queries.get_publications(conn, story_name=name, content_type=ct):
                    ext = p.get("external_id")
                    if p.get("platform") and ext:
                        existing.add((p["platform"], str(ext)))
            except Exception:
                pass
        elif mt == "masterpiece":
            try:
                from database import masterpiece_queries
                for plat, sid in masterpiece_queries.member_pairs(conn, mref):
                    existing.add((plat, str(sid)))
            except Exception:
                pass
    return existing


def auto_suggest_collections(conn: sqlite3.Connection) -> list[dict]:
    """Suggest un-collected cross-platform lookalikes to fold into a Collection.

    Merges two native (no-AI) signals, excluding anything already collected:
      • **title** similarity (Jaccard, analytics_queries._auto_suggest), and
      • **image** similarity (perceptual dHash, image_hash.image_suggestions).
    Pairs found by both are marked reason='both' and take the higher score.
    Imported lazily to avoid any import cycle.
    """
    from database import analytics_queries, image_hash
    existing = _collected_pairs(conn)
    title = analytics_queries._auto_suggest(conn, existing)
    for t in title:
        t.setdefault("reason", "title")
    image = image_hash.image_suggestions(conn, existing)

    # Merge on the unordered pair of members so title+image dedupe.
    def _key(s):
        return frozenset((m["platform"], str(m["submission_id"])) for m in s["submissions"])

    merged: dict = {}
    for s in title + image:
        k = _key(s)
        cur = merged.get(k)
        if cur is None:
            merged[k] = s
        else:
            # Same pair from both signals → reason 'both', keep the richer titles
            # and the higher confidence.
            cur["reason"] = "both"
            if s.get("similarity", 0) > cur.get("similarity", 0):
                cur["similarity"] = s["similarity"]
            # Prefer whichever copy has non-empty titles.
            if any(not m.get("title") for m in cur["submissions"]) and \
               all(m.get("title") for m in s["submissions"]):
                cur["submissions"] = s["submissions"]

    out = list(merged.values())
    # Route each suggestion (spec §7, Masterpieces Phase 7): a same-IMAGE match
    # (perceptual hash) is the SAME picture across sites → propose a **Masterpiece**;
    # a same-PIECE title match (a gallery upload + a microblog post about it) →
    # propose a **Collection**. 'both' leans Masterpiece. Additive field; the
    # suggestions UI routes on it.
    for s in out:
        s["target"] = "masterpiece" if s.get("reason") in ("image", "both") else "collection"
    out.sort(key=lambda x: x.get("similarity", 0), reverse=True)
    return out[:20]


def migrate_links_to_collections(conn: sqlite3.Connection) -> int:
    """One-time, idempotent: fold each Cross-Platform submission_link into a
    Collection (submission members). The original submission_links rows are left
    INTACT so the operation is fully reversible; idempotency + provenance are
    tracked via collections.source_link_id. Returns the number newly created.
    """
    try:
        link_rows = conn.execute("SELECT link_id FROM submission_links").fetchall()
    except Exception:
        return 0  # link tables absent on this DB — nothing to migrate
    try:
        migrated = {r["source_link_id"] for r in conn.execute(
            "SELECT source_link_id FROM collections WHERE source_link_id IS NOT NULL")}
    except Exception:
        return 0  # source_link_id column not present yet — migration hook adds it first
    n = 0
    for lr in link_rows:
        lid = lr["link_id"]
        if lid in migrated:
            continue
        members = conn.execute(
            "SELECT platform, submission_id FROM submission_link_members WHERE link_id = ?",
            (lid,)).fetchall()
        if len(members) < 2:
            continue  # a link needs 2+ members to be meaningful
        # Name the collection from the first member that resolves to a title.
        name = ""
        for m in members:
            row = _submission_row(conn, m["platform"], str(m["submission_id"]))
            if row and (row.get("title") or row.get("full_text")):
                name = (row.get("title") or row.get("full_text") or "").strip()[:120]
                break
        if not name:
            name = f"Linked piece #{lid}"
        cur = conn.execute(
            "INSERT INTO collections (name, notes, source_link_id) VALUES (?, ?, ?)",
            (name, "Migrated from a Cross-Platform link.", lid))
        cid = cur.lastrowid
        for m in members:
            conn.execute(
                "INSERT OR IGNORE INTO collection_members (collection_id, member_type, member_ref) "
                "VALUES (?, 'submission', ?)",
                (cid, f"{m['platform']}:{m['submission_id']}"))
        n += 1
    return n


def list_collections_with_summary(conn: sqlite3.Connection) -> list[dict]:
    """Every collection + a light rollup (totals, platforms, personas, cover) for
    the hub grid. Reuses rollup_collection but trims the heavy per-location list."""
    out = []
    for c in list_collections(conn):
        roll = rollup_collection(conn, c["id"]) or {}
        locs = roll.get("locations", [])
        # Auto-cover: first location that actually has an image, so a collection
        # card shows the piece even when no explicit cover was set.
        cover_thumb, cover_platform = "", ""
        for l in locs:
            if l.get("thumbnail_url"):
                cover_thumb, cover_platform = l["thumbnail_url"], l["platform"]
                break
        out.append({
            **c,
            "totals": roll.get("totals", {"views": 0, "favorites": 0, "comments": 0,
                                          "platforms": 0, "locations": 0}),
            "persona_ids": roll.get("persona_ids", []),
            "member_count": len(roll.get("members", [])),
            "platforms": sorted({l["platform"] for l in locs}),
            "cover_thumb": cover_thumb,
            "cover_platform": cover_platform,
        })
    return out
