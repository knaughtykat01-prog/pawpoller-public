"""Unified works ("Submissions") hub API.

Read-only aggregation over the local archives + the publications registry that
powers the central Submissions hub: every WORK (story or artwork) the user
manages, grouped per work, with its published platforms and persona.

Note: the per-platform *discovered* submission analytics live at
``/api/submissions`` (analytics). This is the per-WORK view, so it lives at
``/api/works``. Phase 1 of docs/specs/submissions-hub.md (read-only; cards link
to the existing per-work detail views).
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from database.db import get_connection
from database import accounts as accounts_db
from database import personas as personas_db
from database import masterpiece_queries
from database import platform_metrics
from database import posting_queries

logger = logging.getLogger(__name__)
works_router = APIRouter(prefix="/api")


def _submission_account_id(conn, platform: str, submission_id: str):
    """The account_id that owns a polled submission (from {platform}_submissions),
    or None. Lets imports/links attribute the publication to the right account."""
    from posting.sync import PLATFORM_TABLES
    cfg = PLATFORM_TABLES.get(platform)
    if not cfg:
        return None
    try:
        row = conn.execute(
            f"SELECT account_id FROM {cfg['table']} WHERE {cfg['id_col']} = ?",
            (str(submission_id),),
        ).fetchone()
        aid = row[0] if row else None
        return aid if aid else None
    except Exception:
        return None


def _persona_maps(conn):
    """Return (account_id -> persona_id, persona_id -> persona dict)."""
    personas = {p["persona_id"]: p for p in personas_db.list_personas(conn)}
    acct_to_persona = {
        a["account_id"]: a.get("persona_id")
        for a in accounts_db.list_accounts(conn)
        if a.get("persona_id")
    }
    return acct_to_persona, personas


def _posted_dates(conn, pubs: list[dict], artworks: list[dict]) -> dict[tuple, str]:
    """``{(content_type, name): earliest original post date}`` for every work.

    Resolved at read time from the per-platform submission rows rather than
    from a stored field, which is what lets an EXISTING library sort correctly
    without a migration or a re-import. Two sources, batched per platform:

    * every ``posted`` publication's ``external_id`` — covers stories, which
      have no date field of their own, and any artwork that was ever posted;
    * an artwork's ``import_source`` — belt-and-braces for an imported piece
      whose publication link is missing.

    "Earliest" because a piece live on four sites was published once; the
    later ones are reposts, and "when was this made public" is the honest
    reading of "most recent".
    """
    wanted: dict[str, set] = {}
    owners: dict[tuple, list[tuple]] = {}
    for p in pubs:
        if p.get("status") != "posted" or not p.get("external_id"):
            continue
        key = (p.get("content_type", "story"), p["story_name"])
        plat, ext = p.get("platform"), str(p["external_id"])
        wanted.setdefault(plat, set()).add(ext)
        owners.setdefault(key, []).append((plat, ext))
    for a in artworks:
        src = a.get("import_source") or {}
        plat, ext = src.get("platform"), src.get("submission_id")
        if plat and ext:
            wanted.setdefault(plat, set()).add(str(ext))
            owners.setdefault(("artwork", a["name"]), []).append((plat, str(ext)))
    dates = {plat: platform_metrics.read_posted_at(conn, plat, ids)
             for plat, ids in wanted.items()}
    out: dict[tuple, str] = {}
    for key, refs in owners.items():
        found = [dates.get(plat, {}).get(ext, "") for plat, ext in refs]
        found = [d for d in found if d]
        if found:
            out[key] = min(found)
    return out


def assemble_works(
    *,
    stories: list[dict],
    artworks: list[dict],
    pubs: list[dict],
    acct_to_persona: dict,
    personas: dict,
    type: str = "all",
    persona: int | None = None,
    search: str | None = None,
    posted_dates: dict | None = None,
    sort: str = "recent",
    junk: dict[str, str] | None = None,
) -> dict:
    """Pure grouping/filter/sort over already-fetched data (unit-testable).

    Groups publications per (content_type, work name) so each work knows the
    platforms it's posted to and the persona(s) behind those accounts, then
    merges with the local story/artwork archives and applies the filters.
    """
    # Local import for the same reason `list_works` uses one: `posting` pulls in
    # the platform stack, and importing it at module scope here reintroduces a
    # cycle. `_canonical_tag_list` is the flatten the POSTERS use, and search has
    # to agree with them about what a piece is tagged.
    from posting.artwork_reader import _canonical_tag_list

    pub_map: dict[tuple, list] = {}
    for p in pubs:
        pub_map.setdefault((p.get("content_type", "story"), p["story_name"]), []).append(p)

    def enrich(ct: str, name: str):
        wp = pub_map.get((ct, name), [])
        platforms = sorted({p["platform"] for p in wp if p.get("status") == "posted"})
        pids = sorted({
            acct_to_persona[p["account_id"]]
            for p in wp
            if p.get("account_id") in acct_to_persona
        })
        # Pool live stats across every publication of this work (2.147.0), so the
        # Library can sort by performance and the Overview's stat cards can deep-link
        # to a sorted view. Platforms name the same metric differently, so the
        # per-platform column knowledge lives in database/platform_metrics.py and
        # `pooled` resolves it per row before summing. It also keeps the metric
        # FAMILIES apart: e621/Furbooru report a net up−down score (which can be
        # negative), so that lands in its own `score` total instead of being added
        # to views. Publications carry `stats` only when fetched via
        # get_publications_with_stats; without it these stay 0 and sorting simply
        # falls back to a stable order.
        totals = platform_metrics.pooled(
            (p.get("platform"), p.get("stats")) for p in wp)
        return platforms, len(wp), pids, {
            "views": totals["views"], "favorites": totals["faves"],
            "comments": totals["comments"], "score": totals["score"]}

    works: list[dict] = []

    if type in ("all", "story"):
        for s in stories:
            platforms, count, pids, stats = enrich("story", s["name"])
            cover = (s.get("images") or {}).get("cover", "")
            wc = s.get("word_count") or 0
            works.append({
                "content_type": "story",
                "name": s["name"],
                "title": s.get("title") or s["name"].replace("_", " "),
                "rating": s.get("rating", ""),
                # Carried so the merged hub's story cards keep what the retired
                # Stories hub showed (2.155.0): a blurb, a category chip, and the
                # ⚠ warnings tooltip. Folding a hub in must not lose its data.
                "description": s.get("description", ""),
                "category": s.get("category", ""),
                "warnings": s.get("warnings") or [],
                "series": s.get("series", ""),                 # gap-wave-5 §2
                "series_index": s.get("series_index", 0) or 0,
                "platforms": platforms,
                "stats": stats,
                "publication_count": count,
                "persona_ids": pids,
                "persona_names": [personas[i]["name"] for i in pids if i in personas],
                "thumb_url": (
                    f"/api/posting/image?story={quote(s['name'])}&file={quote(cover)}"
                    if cover else ""
                ),
                "detail_route": f"#/posting/story/{quote(s['name'])}",
                "meta": (f"{s.get('chapters', 0) or 0} ch · {wc:,} words" if wc else ""),
                "created_at": "",
                # Stories have no date field on the record; the real one comes
                # from the submission rows via _posted_dates. Before 4.0.12 this
                # was the only value, and "" sorts LAST — so every story sank
                # below every artwork in "Most recent", permanently.
                "original_posted_at": (posted_dates or {}).get(("story", s["name"]), ""),
                # Always False: junk is a Masterpiece concept and stories have no
                # such status. Present anyway so a consumer can read `is_junk`
                # without first checking content_type.
                "is_junk": False,
                # Tags (3.14.0) so `tag:` / `-tag:` search has something to match.
                "tags": list(s.get("tags") or []),
            })

    if type in ("all", "artwork"):
        for a in artworks:
            platforms, count, pids, stats = enrich("artwork", a["name"])
            img = a.get("image", "")
            # Non-primary variants (2.190.1) so the Library can show a tile per
            # render. Each carries a ready thumb_url (same shape as the master's).
            # detail_route carries ?v=<key> (2.193.0) so clicking a variant tile
            # opens the unified detail page with THAT render selected, alongside
            # its siblings. Before this the key was dropped and every variant
            # tile landed you on the hero — the whole complaint.
            variant_tiles = [
                {
                    "key": v.get("key", ""),
                    "label": v.get("label") or v.get("key") or "",
                    "rating": v.get("rating", "") or a.get("rating", ""),
                    "thumb_url": f"/api/artwork/image?name={quote(a['name'])}&file={quote(v.get('image', ''))}",
                    "detail_route": (f"#/artwork/image/{quote(a['name'])}"
                                     f"?v={quote(v.get('key', ''))}"),
                }
                for v in (a.get("variants") or [])
                if v.get("key") and v.get("image")
            ]
            works.append({
                "content_type": "artwork",
                "name": a["name"],
                "title": a.get("title") or a["name"].replace("_", " "),
                "rating": a.get("rating", ""),
                "platforms": platforms,
                "stats": stats,
                "publication_count": count,
                "persona_ids": pids,
                "persona_names": [personas[i]["name"] for i in pids if i in personas],
                "thumb_url": (
                    f"/api/artwork/image?name={quote(a['name'])}&file={quote(img)}"
                    if img else ""
                ),
                "variants": variant_tiles,
                "detail_route": f"#/artwork/image/{quote(a['name'])}",
                "meta": "",
                "created_at": a.get("created_at", ""),
                # Persisted by the importer since 4.0.12; resolved from the
                # submission rows for anything imported before that.
                "original_posted_at": (a.get("original_posted_at")
                                       or (posted_dates or {}).get(("artwork", a["name"]), "")),
                # Attribution (3.5.2). `artist_name` is what the card shows;
                # `needs_artist` is the flag the Library filters and badges on.
                # Stories are never flagged — the author wrote them.
                "artist_name": ((a.get("artist") or {}).get("name") or ""),
                "needs_artist": not (a.get("artist") or {}).get("name"),
                # Junk (3.13.1) — 'kept-but-hidden'. The status lives in the
                # `masterpieces` table, NOT in masterpiece.json, so it has to be
                # joined in here; `list_artworks()` reads only the folder and
                # cannot know. Exposed rather than filtered: see list_works.
                "is_junk": (junk or {}).get(a["name"], "") == "junk",
                # Flattened through the SAME helper the posters use (core, then
                # legacy `default`, then auxiliary, de-duplicated). Searching must
                # see exactly the set that would be posted, or a `tag:` query and
                # a publish would disagree about what a piece is tagged.
                "tags": _canonical_tag_list(a.get("tags") or {}),
            })

    if persona:
        works = [w for w in works if persona in w["persona_ids"]]
    if search:
        q = search.lower()
        works = [w for w in works if q in w["title"].lower() or q in w["name"].lower()]

    if sort == "title":
        works.sort(key=lambda w: w["title"].lower())
    elif sort == "platforms":
        works.sort(key=lambda w: len(w["platforms"]), reverse=True)
    elif sort in ("views", "favorites", "comments", "score"):
        # Performance sorts (2.147.0) — feed the Overview stat cards' deep-links.
        # `score` joins them now that the booru family's metric survives pooling.
        works.sort(key=lambda w: (w.get("stats") or {}).get(sort, 0), reverse=True)
    else:  # recent
        works.sort(key=lambda w: (w.get("original_posted_at") or w.get("created_at") or ""),
                   reverse=True)

    return {
        "works": works,
        "personas": [
            {"id": p["persona_id"], "name": p["name"], "color": p.get("color", "")}
            for p in personas.values()
        ],
    }


# Stable column order for the complete export. The four canonical metrics come
# first — every platform's headline numbers normalised by the registry, so a
# Wattpad row's `reads` and an AO3 row's hits both land in `views`, and the
# booru family's net score lands in `score` instead of being lost. After them,
# one column per platform-specific extra the registry knows about (bookmarks,
# retweets, reach, up/down score …), generated from the registry so a new
# platform's metrics appear in the export without touching this file.
_BASE_EXPORT_COLUMNS = [
    "content_type", "work", "title", "chapter", "platform",
    "account", "external_id", "url", "posted_at", "updated_at", "status", "rating", "words",
    "views", "score", "favorites", "comments",
]

_EXTRA_COLUMNS = sorted({
    col
    for code in platform_metrics.ALL_CODES
    for col in (platform_metrics.get(code).extra if platform_metrics.get(code) else ())
})

_EXPORT_COLUMNS = _BASE_EXPORT_COLUMNS + _EXTRA_COLUMNS


@works_router.get("/works/export.csv")
def export_works_csv():
    """Complete analytics export: one row per publication (work × platform) with
    all its current stats. Complements the Analytics page's summary CSVs (fastest
    / weekly) with the full dataset a spreadsheet user actually wants (gap G5).
    Downloaded via a same-origin navigation so the session cookie rides."""
    conn = get_connection()
    try:
        # content_type=None → every publication (story, artwork, post); stat
        # enrichment maps by platform, so all rows are enriched where a stats
        # table exists (posts simply have no stats and export blank).
        pubs = posting_queries.get_publications_with_stats(conn, content_type=None)
        accounts = {a["account_id"]: (a.get("label") or a["account_id"])
                    for a in accounts_db.list_accounts(conn)}
    finally:
        conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_EXPORT_COLUMNS)
    for pub in pubs:
        stats = pub.get("stats") or {}
        row = {
            "content_type": pub.get("content_type", ""),
            "work": pub.get("story_name", ""),
            "title": pub.get("title_used") or pub.get("story_name", ""),
            "chapter": pub.get("chapter_index", ""),
            "platform": pub.get("platform", ""),
            "account": accounts.get(pub.get("account_id"), pub.get("account_id") or ""),
            "external_id": pub.get("external_id", ""),
            "url": pub.get("external_url", ""),
            "posted_at": pub.get("first_posted_at", ""),
            "updated_at": pub.get("last_updated_at", ""),
            "status": pub.get("status", ""),
            "rating": pub.get("rating_used", ""),
            "words": pub.get("word_count", ""),
            # Canonical metrics — already normalised per platform by the registry.
            "views": stats.get("views"),
            "score": stats.get("score"),
            "favorites": stats.get("faves"),
            "comments": stats.get("comments"),
        }
        for col in _EXTRA_COLUMNS:
            if stats.get(col) is not None:
                row[col] = stats[col]
        writer.writerow(["" if row.get(c) is None else row.get(c, "")
                         for c in _EXPORT_COLUMNS])

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="pawpoller-analytics-{stamp}.csv"'},
    )


@works_router.get("/works")
def list_works(
    type: str = Query("all"),          # all | story | artwork
    persona: int | None = Query(None),
    search: str | None = Query(None),
    sort: str = Query("recent"),       # recent | title | platforms | views | favorites | comments
):
    """Unified per-work list (stories + artwork) for the Submissions hub.

    The frontend caches the full list and filters client-side; these query
    params mirror that so the endpoint is also useful directly / for tests.

    Junked works are RETURNED, carrying ``is_junk``, rather than filtered out
    here. The Library fetches once and filters the cached list client-side, so
    dropping them server-side would make the Junk filter impossible to satisfy
    without a second round trip. Hiding is the client's job; knowing is ours.
    """
    from posting import story_reader, artwork_reader
    try:
        conn = get_connection()
        try:
            # content_type=None returns BOTH stories and artwork. Use the
            # stats-carrying variant so each work gets pooled views/faves/comments
            # (2.147.0) — powers the Library's performance sorts.
            pubs = posting_queries.get_publications_with_stats(conn, content_type=None)
            acct_to_persona, personas = _persona_maps(conn)
            # One query for every junk flag (masterpiece_queries.statuses), not
            # one per work — this list is the whole catalogue.
            junk = masterpiece_queries.statuses(conn)
            # Needs the connection: one batched date query per platform, so
            # the whole catalogue costs at most ~20 queries, not one per work.
            artworks = artwork_reader.list_artworks()
            posted_dates = _posted_dates(conn, pubs, artworks)
        finally:
            conn.close()
        return assemble_works(
            stories=story_reader.list_stories(),
            artworks=artworks,
            pubs=pubs,
            posted_dates=posted_dates,
            junk=junk,
            acct_to_persona=acct_to_persona,
            personas=personas,
            type=type, persona=persona, search=search, sort=sort,
        )
    except Exception as e:
        logger.error("Error listing works: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# ── Discovered (unlinked) bucket + link-to-work (Phase 2) ─────────────────────

# Art vs text classification for discovered submissions. Used by the Artwork
# hub to surface discovered *visual* work (and by any view that wants to split
# a mixed platform's feed). Two platforms are content-pure for this app; the
# rest are classified from the per-platform type string (category / subtype /
# content_type) the poller already stored.
_ART_ONLY_PLATFORMS = frozenset({"da", "ik", "pix", "ig", "e621"})  # image-first platforms
_TEXT_ONLY_PLATFORMS = frozenset({"ao3", "sqw", "wp"})      # literature-only
# Substrings that mark a type string as prose vs visual. Order matters only in
# that a text hint wins over an art hint (a "story illustration" is still text).
_TEXT_TYPE_HINTS = (
    "stor", "writ", "litera", "prose", "poet", "novel", "chapter", "fiction",
)
_ART_TYPE_HINTS = (
    "art", "visual", "image", "illustration", "digital", "drawing", "sketch",
    "paint", "photo", "comic", "animation", "post",
)


def classify_kind(platform: str, type_str: str, has_image: bool | None = None) -> str:
    """Classify a discovered submission as 'art', 'text', or 'unknown'.

    Pure/unit-testable. Content-pure platforms short-circuit; mixed platforms
    (fa/sf/ib/ws/bsky/mast/thr/tw/tum) are read from their stored type string,
    text hints winning over art hints so a "Story illustration" stays text.
    When the type string is inconclusive, ``has_image`` breaks the tie: an
    image-bearing post is importable as art — this is what lets discovered art
    from ANY polled platform be caught, not just the classic art platforms.
    ``has_image=None`` (unknown) preserves the legacy "unknown" result.
    """
    if platform in _ART_ONLY_PLATFORMS:
        return "art"
    if platform in _TEXT_ONLY_PLATFORMS:
        return "text"
    t = (type_str or "").lower()
    if any(h in t for h in _TEXT_TYPE_HINTS):
        return "text"
    if any(h in t for h in _ART_TYPE_HINTS):
        return "art"
    if has_image is True:
        return "art"      # inconclusive type but has an image → importable as art
    if has_image is False:
        return "text"     # inconclusive type, no image → nothing to import
    return "unknown"


def build_discovered(platform_rows: list[tuple], linked: set) -> list[dict]:
    """Normalize per-platform submission rows into the discovered-unlinked list.

    Pure (unit-testable): given a list of ``(platform, cfg, [row_dict, ...])`` and
    the set of already-linked ``(platform, submission_id)`` pairs, return the
    submissions that have NO matching publication, normalized to one shape.
    """
    out: list[dict] = []
    for platform, cfg, rows in platform_rows:
        id_col, title_col = cfg["id_col"], cfg["title_col"]
        for d in rows:
            sid = str(d.get(id_col) or "")
            if not sid or (platform, sid) in linked:
                continue
            stype = (d.get("category") or d.get("content_type") or d.get("subtype")
                     or d.get("type_name") or "")
            thumb = (d.get("thumbnail_url") or d.get("thumb_url") or d.get("download_url")
                     or d.get("media_url") or d.get("file_url") or "")
            out.append({
                "platform": platform,
                "submission_id": sid,
                "title": d.get(title_col) or f"#{sid}",
                "thumbnail_url": d.get("thumbnail_url") or d.get("thumb_url") or "",
                "type": stype,
                "kind": classify_kind(platform, stype, has_image=bool(thumb)),
                # Prefer the poller-stored permalink; the url_template is only a
                # fallback (and can't be right for instance-scoped mast/tum URLs).
                "url": d.get("link") or cfg["url_template"].format(id=sid),
                "views": d.get("views"),
                "favorites": d.get("favorites_count") or d.get("favorites"),
                "comments": d.get("comments_count") or d.get("comments"),
                "posted_at": (d.get("posted_at") or d.get("create_datetime")
                              or d.get("created_at") or ""),
            })
    out.sort(key=lambda x: x.get("posted_at") or "", reverse=True)
    return out


def get_discovered_unlinked(conn, platform_filter: str | None = None) -> list[dict]:
    """Discovered submissions (across platforms) with no publication link.

    Excludes four sets of (platform, submission_id):
      • already published/linked (a real publication exists),
      • Masterpiece members — a piece bundled into a Masterpiece must not reappear
        as a duplicate discovered tile (dedup, 2.140.0),
      • user-ignored tiles (the Ignore list, 2.140.0),
      • posts imported into the Posts module (2.157.0) — `post_publications` is a
        separate registry from `publications` (a post has no title/chapters/file),
        so without this an imported tweet would sit in the queue forever.
    """
    from posting.sync import PLATFORM_TABLES
    from database import masterpiece_queries, ignored_queries
    linked = {
        (r["platform"], str(r["external_id"]))
        for r in conn.execute(
            "SELECT platform, external_id FROM publications WHERE external_id != ''")
    }
    # Fold Masterpiece members + the ignore list into the same exclusion set so
    # both the hub and any other consumer of this list get a clean result.
    linked |= masterpiece_queries.all_member_pairs(conn)
    linked |= ignored_queries.all_ignored_pairs(conn)
    linked |= {
        (r["platform"], str(r["external_id"]))
        for r in conn.execute(
            "SELECT platform, external_id FROM post_publications WHERE external_id != ''")
    }
    platform_rows: list[tuple] = []
    for plat, cfg in PLATFORM_TABLES.items():
        if platform_filter and plat != platform_filter:
            continue
        try:
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM {cfg['table']}").fetchall()]
        except Exception:
            continue  # table may not exist on this install
        platform_rows.append((plat, cfg, rows))
    return build_discovered(platform_rows, linked)


@works_router.get("/works/discovered")
def list_discovered(platform: str | None = Query(None)):
    """Submissions the pollers found that aren't linked to any local work."""
    try:
        conn = get_connection()
        try:
            items = get_discovered_unlinked(conn, platform_filter=platform)
        finally:
            conn.close()
        return {"discovered": items}
    except Exception as e:
        logger.error("Error listing discovered submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# ── Ignore list for discovered tiles (2.140.0) ────────────────────────────────
# Lets the user dismiss discovered artwork they never want in the hub (e.g. images
# scraped from tweets). Reversible via the un-ignore endpoint.

@works_router.post("/works/discovered/ignore")
def ignore_discovered(body: dict):
    """Add a discovered (platform, submission_id) to the Ignore list."""
    from database import ignored_queries
    platform = body.get("platform")
    submission_id = str(body.get("submission_id") or "")
    if not (platform and submission_id):
        raise HTTPException(400, detail="platform and submission_id are required")
    conn = get_connection()
    try:
        ignored_queries.add_ignored(conn, platform, submission_id)
    finally:
        conn.close()
    return {"status": "ignored", "platform": platform, "submission_id": submission_id}


@works_router.delete("/works/discovered/ignore/{platform}/{submission_id:path}")
def unignore_discovered(platform: str, submission_id: str):
    """Remove a (platform, submission_id) from the Ignore list (it reappears)."""
    from database import ignored_queries
    conn = get_connection()
    try:
        ignored_queries.remove_ignored(conn, platform, submission_id)
    finally:
        conn.close()
    return {"status": "unignored", "platform": platform, "submission_id": submission_id}


@works_router.get("/works/discovered/ignored")
def list_ignored_discovered():
    """The Ignore list (for a manage/restore view)."""
    from database import ignored_queries
    conn = get_connection()
    try:
        return {"ignored": ignored_queries.list_ignored(conn)}
    finally:
        conn.close()


@works_router.post("/works/link")
def link_submission(body: dict):
    """Link a discovered platform submission to an existing local work.

    Writes a publication row (`external_id` = the platform submission id) so the
    work shows that platform in the hub and the submission leaves the discovered
    bucket. ``content_type`` should be the target work's type (story | artwork).
    """
    platform = body.get("platform")
    submission_id = str(body.get("submission_id") or "")
    name = body.get("name")
    content_type = body.get("content_type", "story")
    title = body.get("title", "")
    url = body.get("url", "")
    if not (platform and submission_id and name):
        raise HTTPException(400, detail="platform, submission_id and name are required")
    try:
        conn = get_connection()
        try:
            # Attribute the publication to the account that actually owns the
            # submission (from its {platform}_submissions row), not the platform
            # default — so persona/account scoping is correct.
            acct_id = _submission_account_id(conn, platform, submission_id)
            pub_id = posting_queries.upsert_publication(
                conn,
                story_name=name,
                chapter_index=0,
                platform=platform,
                account_id=acct_id,
                content_type=content_type,
                external_id=submission_id,
                external_url=url,
                title_used=title,
                status="posted",
            )
        finally:
            conn.close()
        return {"status": "linked", "pub_id": pub_id}
    except Exception as e:
        logger.error("Link failed: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
