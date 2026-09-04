"""REST API endpoints for the posting module.

Provides endpoints for uploading stories to platforms, managing the posting
queue, viewing publication history, and browsing the story archive.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import sqlite3
import tarfile

from mirror import core as mirror_core
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse

import config
from database.db import get_connection
from database import platform_metrics
from database import posting_queries

logger = logging.getLogger(__name__)

posting_router = APIRouter(prefix="/api/posting")


# ── Story Archive ─────────────────────────────────────────────

@posting_router.get("/stories")
def list_stories():
    """List all available stories with publication status per platform."""
    from posting import story_reader
    try:
        stories = story_reader.list_stories()

        # Enrich with publication status
        conn = get_connection()
        try:
            pubs = posting_queries.get_publications(conn)
        finally:
            conn.close()

        # Group publications by story
        pub_map: dict[str, list[dict]] = {}
        for p in pubs:
            sn = p["story_name"]
            if sn not in pub_map:
                pub_map[sn] = []
            pub_map[sn].append(p)

        for story in stories:
            name = story["name"]
            story_pubs = pub_map.get(name, [])
            story["published_platforms"] = sorted(set(
                p["platform"] for p in story_pubs if p["status"] == "posted"
            ))
            story["publication_count"] = len(story_pubs)

        return {"stories": stories}
    except Exception as e:
        logger.error("Error listing stories: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@posting_router.get("/stories/{story_name:path}")
def get_story_detail(story_name: str):
    """Get full story detail including publications, stats, and metadata."""
    from posting import story_reader
    import json as _json
    try:
        story = story_reader.load_story(story_name)
        story_path = story.path

        # Read story.json for full metadata
        story_json_data = {}
        sjp = story_path / "story.json"
        if sjp.is_file():
            story_json_data = _json.loads(sjp.read_text(encoding="utf-8"))

        # Get publications with stats + ancillary per-story data (recent log,
        # pending queue items, and per-IB-pub top fans). All fetched in the
        # same connection scope so the detail page is a single round-trip.
        conn = get_connection()
        try:
            pubs = posting_queries.get_publications_with_stats(conn, story_name=story_name)

            # Last 5 posting actions for this story (success or failure).
            recent_log = posting_queries.get_posting_log(
                conn, story_name=story_name, limit=5,
            )

            # Pending / processing queue items for this story (callout card).
            pending_queue = posting_queries.get_queue(
                conn, include_completed=False, story_name=story_name,
            )

            # Top fans for IB publications. Each IB pub gets a `top_fans` list
            # of {username, first_seen_at} populated from faving_users for that
            # submission_id. Capped at 5 most recent. Other platforms get an
            # empty list. Skipped silently if the table doesn't exist (e.g.
            # fresh install before first IB poll).
            for pub in pubs:
                pub["top_fans"] = []
                if pub["platform"] != "ib" or not pub.get("external_id"):
                    continue
                try:
                    rows = conn.execute(
                        "SELECT username, first_seen_at FROM faving_users "
                        "WHERE submission_id = ? "
                        "ORDER BY first_seen_at DESC LIMIT 5",
                        (int(pub["external_id"]),),
                    ).fetchall()
                    pub["top_fans"] = [dict(r) for r in rows]
                except (sqlite3.OperationalError, ValueError):
                    pass  # Table missing or non-numeric ID — leave as []

            # Per-publication snapshots (last 30 days, capped at 60 points) for
            # the sparkline + comparison chart in batch 3. Each platform's
            # snapshots table has a different name and column set, so the
            # canonical registry (database/platform_metrics.py) resolves
            # platform → (table, id_col, primary metric column). The frontend
            # renders whichever value is present without caring about the
            # source column.
            #
            # The local map this replaces covered 11 platforms and asked
            # AO3/SquidgeWorld for a `hits` column that doesn't exist (they
            # store plain `views`), so those sparklines were always empty —
            # the `sqlite3.OperationalError` below swallowed it. Score-model
            # platforms chart their score; engagement-only ones chart faves.
            def _snap_metric(code):
                spec = platform_metrics.get(code)
                if not spec:
                    return None
                col = spec.views or spec.score or spec.faves
                return (spec.snapshots, spec.id_col, col) if col else None

            for pub in pubs:
                pub["snapshots"] = []
                cfg = _snap_metric(pub["platform"])
                if not cfg or not pub.get("external_id"):
                    continue
                table, id_col, value_col = cfg
                try:
                    # IDs are stored as TEXT for BSKY/TW/SF (URI-shaped or
                    # 64-bit), INTEGER for others. Try int first then fall
                    # back to the raw string.
                    raw_id = pub["external_id"]
                    try_id = int(raw_id) if raw_id.isdigit() else raw_id
                    rows = conn.execute(
                        f"SELECT polled_at, {value_col} as value FROM {table} "
                        f"WHERE {id_col} = ? "
                        f"AND polled_at >= datetime('now', '-30 days') "
                        f"ORDER BY polled_at DESC LIMIT 60",
                        (try_id,),
                    ).fetchall()
                    # Reverse for chronological order (oldest → newest) so
                    # the sparkline draws left-to-right naturally.
                    pub["snapshots"] = [
                        {"t": r["polled_at"], "v": r["value"] or 0}
                        for r in reversed(rows)
                    ]
                except (sqlite3.OperationalError, AttributeError, ValueError):
                    pass  # Table missing, ID type mismatch — leave as []
        finally:
            conn.close()

        # Per-publication change detection. Hashes the current local file for
        # each pub and compares against the stored file_hash. Filtered to this
        # story so we don't pay the cost of hashing every other story's files
        # just to render one detail page. Result is merged onto each pub by
        # (chapter_index, platform) — the unique key.
        try:
            from posting import sync as posting_sync
            change_rows = posting_sync.detect_changes(story_name=story_name)
            change_map = {
                (row["chapter_index"], row["platform"]): row
                for row in change_rows
            }
            for pub in pubs:
                key = (pub["chapter_index"], pub["platform"])
                row = change_map.get(key)
                if row:
                    pub["change_status"] = row["status"]   # changed/unchanged/file_missing/no_hash
                    pub["change_detected"] = bool(row["changed"])
                else:
                    pub["change_status"] = None
                    pub["change_detected"] = False
        except Exception as e:
            logger.warning("Change detection failed for %s: %s", story_name, e)
            for pub in pubs:
                pub.setdefault("change_status", None)
                pub.setdefault("change_detected", False)

        # Enrich images with auto-detected cover when story.json doesn't declare
        # one — same fallback the listing endpoint applies via _story_entry().
        # Without this the detail page would show no cover for stories whose
        # thumbnail file lives in the folder root but isn't recorded in
        # story.json.images.cover (the common case in this archive).
        images = dict(story_json_data.get("images", {}))
        if not images.get("cover"):
            detected = story_reader.detect_cover_relative(story_path)
            if detected:
                images["cover"] = detected

        # Resolve format files: turns the {bbcode: true, html: true} flag dict
        # from story.json into a richer structure with per-file size + mtime
        # so the frontend can show "bbcode (24 KB, 2 days ago)" badges and
        # link them to the /api/posting/file download endpoint.
        formats_raw = story_json_data.get("formats", {})
        formats_enriched = story_reader.get_format_files(story_path, formats_raw)

        published_platforms = sorted(set(p["platform"] for p in pubs if p["status"] == "posted"))
        all_platforms = list(story_json_data.get("platforms", {}).keys())
        # Map platform names to IDs
        plat_map = {"inkbunny": "ib", "furaffinity": "fa", "weasyl": "ws",
                    "sofurry": "sf", "squidgeworld": "sqw", "bluesky": "bsky", "wattpad": "wp"}
        all_platform_ids = [plat_map.get(p, p) for p in all_platforms]
        unpublished = [p for p in all_platform_ids if p not in published_platforms]

        from routes.submissions_api import first_posted_for
        first_posted, first_posted_source = first_posted_for("story", story.name, pubs)
        return {
            "name": story.name,
            "title": story_json_data.get("title", story.name.replace("_", " ")),
            "author": story.author,
            "description": story.description,
            "summary": story.summary,
            "rating": story_json_data.get("rating", ""),
            "category": story_json_data.get("category", ""),
            "warnings": story_json_data.get("warnings", []),
            "fandom": story_json_data.get("fandom", ""),
            "series": story_json_data.get("series", ""),                       # gap-wave-5 §2
            "series_index": int(story_json_data.get("series_index", 0) or 0),
            "characters": story_json_data.get("characters", []),
            "relationships": story_json_data.get("relationships", []),
            "total_chapters": story.total_chapters,
            "total_words": story.total_words,
            "chapters": [
                {
                    "index": ch.index,
                    "title": ch.title,
                    "word_count": ch.word_count,
                    "description": story.chapter_descriptions.get(ch.index, ""),
                }
                for ch in story.chapters
            ],
            "tags_by_platform": story.tags_by_platform,
            "formats": formats_enriched,
            "images": images,
            "platforms": story_json_data.get("platforms", {}),
            "published_platforms": published_platforms,
            "unpublished_platforms": unpublished,
            "publications": pubs,
            "recent_log": recent_log,
            "pending_queue": pending_queue,
            "first_posted": first_posted,               # what the Library sorts by (4.3.1)
            "first_posted_source": first_posted_source,
        }
    except FileNotFoundError:
        raise HTTPException(404, detail=f"Story not found: {story_name}")
    except Exception as e:
        logger.error("Error loading story %s: %s", story_name, e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@posting_router.post("/stories/{story_name:path}/link-url")
def link_story_by_url(story_name: str, body: dict):
    """Record a copy of this story that was posted by hand, from its URL (4.5.0).

    The story twin of ``masterpieces_api.link_member_by_url``, deliberately the
    same contract: **previews by default**, ``confirm: true`` performs the link.
    The preview says what the URL resolved to, whether the poller has seen it,
    and what it is already attached to — an artwork member or another story's
    publication — because linking silently over either is the mistake the art
    route was written to avoid.

    ⚠ ``chapter_index`` is REQUIRED on confirm and has no safe default: a story
    publishes per chapter, ``0`` means "the whole work", and
    ``upsert_publication`` would happily file chapter 7 as the whole story.
    Re-linking a (platform, chapter) that already exists is refused rather than
    updated — an upsert there reads as success and changes nothing asked for.

    Body: ``{url, confirm?: bool, chapter_index?: int, platform?, submission_id?}``.
    """
    from posting import submission_urls, story_reader
    from database import posting_queries, collections_queries

    try:
        story = story_reader.load_story(story_name)
    except FileNotFoundError:
        raise HTTPException(404, detail=f"Story not found: {story_name}")

    url = str(body.get("url") or "").strip()
    forced_p = str(body.get("platform") or "").strip()
    forced_s = str(body.get("submission_id") or "").strip()
    if forced_p and forced_s:
        cands = [(forced_p, forced_s)]
    elif url:
        cands = submission_urls.candidates_for(url)
    else:
        raise HTTPException(400, detail="url (or platform + submission_id) is required")
    if not cands:
        raise HTTPException(422, detail=(
            "That does not look like a submission link I recognise. Supported: "
            + ", ".join(submission_urls.SUPPORTED_PLATFORMS)))

    conn = get_connection()
    try:
        resolved = []
        for plat, sid in cands:
            row = collections_queries._submission_row(conn, plat, str(sid)) or {}
            owner = conn.execute(
                "SELECT masterpiece_name FROM masterpiece_members "
                "WHERE platform = ? AND submission_id = ?", (plat, str(sid))).fetchone()
            pub = conn.execute(
                "SELECT story_name, chapter_index FROM publications "
                "WHERE platform = ? AND external_id = ? LIMIT 1", (plat, str(sid))).fetchone()
            resolved.append({
                "platform": plat,
                "submission_id": str(sid),
                "known": bool(row),
                "title": row.get("title", "") if row else "",
                "account_id": row.get("account_id") if row else None,
                "linked_to": owner["masterpiece_name"] if owner else "",
                "publication_of": pub["story_name"] if pub else "",
                "publication_chapter": pub["chapter_index"] if pub else None,
            })

        if not body.get("confirm"):
            return {"status": "preview", "total_chapters": int(story.total_chapters or 0),
                    "candidates": resolved}

        if len(cands) != 1:
            raise HTTPException(422, detail="That link matched more than one site — pass platform and submission_id")
        if body.get("chapter_index") is None:
            raise HTTPException(400, detail=(
                "chapter_index is required: 0 for the whole story, otherwise the chapter number"))
        try:
            chapter_index = int(body.get("chapter_index"))
        except (TypeError, ValueError):
            raise HTTPException(400, detail="chapter_index must be a number")
        total = int(story.total_chapters or 0)
        if chapter_index < 0 or chapter_index > total:
            raise HTTPException(400, detail=f"chapter_index must be between 0 and {total}")

        plat, sid = cands[0]
        existing = conn.execute(
            "SELECT pub_id FROM publications WHERE story_name = ? AND platform = ? "
            "AND chapter_index = ? AND content_type = 'story'",
            (story_name, plat, chapter_index)).fetchone()
        if existing:
            raise HTTPException(409, detail=(
                f"This story already has a {plat} publication for "
                f"{'the whole story' if chapter_index == 0 else 'chapter ' + str(chapter_index)} — "
                "linking again would silently overwrite it. Forget that one first if it is wrong."))
        row = resolved[0]
        # What they posted by hand is, by every reasonable reading, the file on
        # disk now — record its hash so the row does not start life "drifted"
        # (detect_changes treats a missing hash as changed, deliberately).
        file_hash, format_file = "", ""
        try:
            from posting import sync as posting_sync
            fp, fmt = story_reader._resolve_format_file(story, chapter_index, plat)
            if fp and os.path.isfile(fp):
                file_hash, format_file = posting_sync.hash_file(fp), str(fmt or "")
        except Exception:  # a fake or format-less story: no hash, same as a claim
            pass
        pub_id = posting_queries.upsert_publication(
            conn, story_name, chapter_index, plat,
            account_id=row.get("account_id"),
            external_id=str(sid),
            external_url=url or submission_urls.build_url(plat, str(sid)),
            title_used=row.get("title") or "",
            file_hash=file_hash, format_file=format_file,
            status="posted", content_type="story")
        conn.commit()
        return {"status": "linked", "pub_id": pub_id, "platform": plat,
                "submission_id": str(sid), "chapter_index": chapter_index}
    finally:
        conn.close()


@posting_router.get("/image")
def get_story_image(story: str = Query(...), file: str = Query(...)):
    """Serve a cover/thumbnail image from a story folder.

    Query params (not path segments) so sub-stories like
    ``My_Story/Nice_Version`` and nested files like
    ``Images/cover.png`` round-trip cleanly through ``encodeURIComponent``.

    Hardened against path traversal: the resolved file MUST live under the
    resolved story directory, and only known image extensions are served.
    """
    from posting import story_reader

    if not story or not file:
        raise HTTPException(400, detail="story and file query params are required")

    try:
        story_obj = story_reader.load_story(story)
    except FileNotFoundError:
        raise HTTPException(404, detail=f"Story not found: {story}")
    except Exception as e:
        logger.error("Error loading story %s for image fetch: %s", story, e)
        raise HTTPException(500, detail="Failed to load story")

    story_root = story_obj.path.resolve()
    requested = (story_root / file).resolve()

    # Path traversal guard: requested file must be inside the story dir.
    try:
        requested.relative_to(story_root)
    except ValueError:
        raise HTTPException(403, detail="Path escapes story directory")

    if not requested.is_file():
        raise HTTPException(404, detail="Image not found")

    if requested.suffix.lower() not in story_reader.COVER_EXTENSIONS:
        raise HTTPException(415, detail="Unsupported image type")

    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    return FileResponse(
        path=str(requested),
        media_type=media_types[requested.suffix.lower()],
        headers={"Cache-Control": "public, max-age=3600"},
    )


# Allowlist for /api/posting/file. Wider than the cover allowlist (we want
# the format-badge download to work for the actual story files: BBCode,
# HTML, Markdown, PDF, plus the chapter splits and styled HTML used by
# the converters). Anything outside this set returns 415 — no random
# binaries from the story folder, no Python scripts.
_DOWNLOAD_EXTENSIONS = frozenset({
    ".txt",      # BBCode + chapter BBCode
    ".html",     # SoFurry HTML / SquidgeWorld HTML / styled HTML / clean HTML
    ".htm",
    ".md",       # MASTER.md and chapter Markdown
    ".pdf",      # FA-format PDFs
    ".json",     # story.json + split_manifest.json
    ".epub",     # EPUB 3.0 produced by editor/epub_generator.py
})

# Map extensions to media types for the download response. Browsers use
# Content-Disposition: attachment regardless, but a correct Content-Type
# helps tools that key off MIME (and is honest).
_DOWNLOAD_MEDIA_TYPES = {
    ".txt":  "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm":  "text/html; charset=utf-8",
    ".md":   "text/markdown; charset=utf-8",
    ".pdf":  "application/pdf",
    ".json": "application/json",
    ".epub": "application/epub+zip",
}

# Subdirectories excluded from the whole-story zip — Backups/ in particular
# can be many MB of revision history that the user almost never wants in a
# "send myself this story" download.
_ARCHIVE_EXCLUDED_DIRS = frozenset({"Backups", "__pycache__", ".git"})


@posting_router.get("/file")
def get_story_file(story: str = Query(...), file: str = Query(...)):
    """Serve a format file from a story folder as a download.

    Same security model as /api/posting/image: query params, traversal
    guard, extension allowlist. Sends Content-Disposition: attachment so
    browsers download rather than render — the format-badge download UX
    on the story detail page is the only intended caller.
    """
    from posting import story_reader

    if not story or not file:
        raise HTTPException(400, detail="story and file query params are required")

    try:
        story_obj = story_reader.load_story(story)
    except FileNotFoundError:
        raise HTTPException(404, detail=f"Story not found: {story}")
    except Exception as e:
        logger.error("Error loading story %s for file fetch: %s", story, e)
        raise HTTPException(500, detail="Failed to load story")

    story_root = story_obj.path.resolve()
    requested = (story_root / file).resolve()

    try:
        requested.relative_to(story_root)
    except ValueError:
        raise HTTPException(403, detail="Path escapes story directory")

    if not requested.is_file():
        raise HTTPException(404, detail="File not found")

    suffix = requested.suffix.lower()
    if suffix not in _DOWNLOAD_EXTENSIONS:
        raise HTTPException(415, detail=f"File type not allowed: {suffix}")

    return FileResponse(
        path=str(requested),
        media_type=_DOWNLOAD_MEDIA_TYPES.get(suffix, "application/octet-stream"),
        filename=requested.name,
        headers={
            "Content-Disposition": f'attachment; filename="{requested.name}"',
            "Cache-Control": "no-cache",
        },
    )


@posting_router.get("/archive")
def get_story_archive(story: str = Query(...)):
    """Stream a zip of the entire story folder as a single download.

    Built so that on a phone you can grab `Story.zip` once and have
    every format (BBCode/HTML/PDF/EPUB/Styled HTML/SquidgeWorld + the
    canonical Markdown/MASTER.md + cover image + chapter splits) in one
    file. `Backups/` is excluded — it can be many MB of revision
    history that nobody wants in a "send myself this story" download.

    The zip includes the story folder name as the top-level prefix so
    extracting it produces `Example_Story/Markdown/MASTER.md` rather
    than dumping `Markdown/MASTER.md` into the user's downloads.
    """
    import io
    import os
    import zipfile
    from posting import story_reader

    if not story:
        raise HTTPException(400, detail="story query param is required")

    try:
        story_obj = story_reader.load_story(story)
    except FileNotFoundError:
        raise HTTPException(404, detail=f"Story not found: {story}")
    except Exception as e:
        logger.error("Error loading story %s for archive: %s", story, e)
        raise HTTPException(500, detail="Failed to load story")

    story_root = story_obj.path.resolve()
    if not story_root.is_dir():
        raise HTTPException(404, detail="Story folder not found")

    folder_name = story_root.name

    # Build zip in memory. Typical story is well under 50MB after
    # compression — even a ~70K-word, 9-section story with every
    # format generated came to ~28MB across a whole archive of ~13
    # stories. One story alone fits comfortably in RAM.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(story_root):
            # Skip excluded subdirectories in-place so os.walk doesn't
            # descend into them.
            dirs[:] = [d for d in dirs if d not in _ARCHIVE_EXCLUDED_DIRS]
            for fname in files:
                fpath = (Path(root) / fname).resolve()
                # Defensive: never let a symlink leak files from outside
                # the story folder.
                try:
                    fpath.relative_to(story_root)
                except ValueError:
                    continue
                arcname = f"{folder_name}/{fpath.relative_to(story_root).as_posix()}"
                zf.write(fpath, arcname=arcname)

    buf.seek(0)
    size = buf.getbuffer().nbytes

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{folder_name}.zip"',
            "Content-Length": str(size),
            "Cache-Control": "no-cache",
        },
    )


# ── Post / Upload ─────────────────────────────────────────────

@posting_router.post("/post")
async def post_story(body: dict):
    """Post a story to one or more platforms immediately.

    Body: {
        "story_name": "Example_Story",
        "platforms": ["ib", "bsky"],
        "chapters": [1, 2, 3],         // optional, null = all
        "account_ids": {"ib": 5}       // optional, {platform: account_id}; absent → default account
    }
    """
    from posting import manager

    story_name = body.get("story_name")
    platforms = body.get("platforms", [])
    chapters = body.get("chapters")
    account_ids = body.get("account_ids")
    persona_id = body.get("persona_id")   # persona-first: refuse, never default (4.2.0)
    description_overrides = body.get("description_overrides") or None   # this post only (4.3.0)

    if not story_name:
        raise HTTPException(400, detail="story_name is required")
    if not platforms:
        raise HTTPException(400, detail="platforms list is required")
    # Live-publish safety guard — mirrors the editor's publish endpoint so a UI
    # regression can't fire a real, publicly-visible post without an explicit
    # acknowledgement (the front-end sets this after its confirm() dialog).
    if not body.get("confirm_live"):
        raise HTTPException(
            400, detail="post requires confirm_live=true (live-publish safety guard)")

    try:
        results = await manager.post_story(story_name, platforms, chapters,
                                           account_ids=account_ids, persona_id=persona_id,
                                           description_overrides=description_overrides)
        successes = sum(1 for r in results if r.get("success"))
        return {
            "status": "completed",
            "total": len(results),
            "successes": successes,
            "failures": len(results) - successes,
            "results": results,
        }
    except FileNotFoundError as e:
        raise HTTPException(404, detail=str(e))
    except Exception as e:
        logger.error("Post failed: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@posting_router.post("/update")
async def update_story(body: dict):
    """Push updates to already-posted submissions.

    Body: {
        "story_name": "Example_Story",
        "platforms": ["ib"],        // optional, null = all
        "chapters": [3],            // optional, null = all
        "account_id": 5             // optional — only update this account's pubs
    }
    """
    from posting import manager

    story_name = body.get("story_name")
    platforms = body.get("platforms")
    chapters = body.get("chapters")
    account_filter = body.get("account_id")

    if not story_name:
        raise HTTPException(400, detail="story_name is required")
    # Live-publish safety guard (see post_story) — an update pushes to a live,
    # publicly-visible submission, so it needs the same explicit acknowledgement.
    if not body.get("confirm_live"):
        raise HTTPException(
            400, detail="update requires confirm_live=true (live-publish safety guard)")

    try:
        results = await manager.update_story(story_name, platforms, chapters,
                                             account_filter=account_filter)
        successes = sum(1 for r in results if r.get("success"))
        return {
            "status": "completed",
            "total": len(results),
            "successes": successes,
            "failures": len(results) - successes,
            "results": results,
        }
    except Exception as e:
        logger.error("Update failed: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# ── Publications ──────────────────────────────────────────────

@posting_router.get("/publications")
def get_publications(
    story_name: str = Query(None),
    platform: str = Query(None),
):
    """List all publications (what's been posted where)."""
    conn = get_connection()
    try:
        pubs = posting_queries.get_publications(conn, story_name=story_name, platform=platform)
        return {"publications": pubs}
    finally:
        conn.close()


@posting_router.get("/publications/stats")
def get_publications_with_stats(story_name: str = Query(None)):
    """List publications with live stats from polling tables."""
    conn = get_connection()
    try:
        pubs = posting_queries.get_publications_with_stats(conn, story_name=story_name)
        return {"publications": pubs}
    finally:
        conn.close()


@posting_router.get("/preview-file")
def preview_publication_file(
    story_name: str = Query(...),
    platform: str = Query(...),
    chapter_index: int = Query(0),
    max_lines: int = Query(120),
):
    """Drift preview — what's in the local file, vs what was uploaded.

    Backs the "Preview before update" affordance in the publish-check
    cell drawer. Resolves the format file PawPoller would push for
    (story, chapter, platform), hashes it, and compares against the
    publication's stored file_hash. Returns a head excerpt of the
    local file plus drift status — enough for the user to sanity-
    check what they'd be uploading without firing a blind update.

    No upstream fetch — that's expensive (auth + per-platform parse)
    and most of the value of this endpoint is "what would I push?",
    which only requires the local side.
    """
    from posting import story_reader, sync as posting_sync
    conn = get_connection()
    try:
        pubs = posting_queries.get_publications(
            conn, story_name=story_name, platform=platform,
        )
        # Find the matching publication for this chapter
        pub = next(
            (p for p in pubs if int(p.get("chapter_index") or 0) == chapter_index),
            None,
        )
        try:
            story = story_reader.load_story(story_name)
        except Exception as e:
            raise HTTPException(404, detail=f"story not found: {e}")

        file_path, _format_key = story_reader._resolve_format_file(
            story, chapter_index, platform,
        )
        if not file_path or not os.path.isfile(file_path):
            raise HTTPException(404, detail=f"no file resolved for ({story_name}, ch{chapter_index}, {platform})")

        current_hash = posting_sync.hash_file(file_path)
        posted_hash = (pub or {}).get("file_hash") or ""
        size = os.path.getsize(file_path)
        modified_at = None
        try:
            from datetime import datetime, timezone
            modified_at = datetime.fromtimestamp(
                os.path.getmtime(file_path), timezone.utc,
            ).isoformat()
        except Exception:
            pass

        # Read a head excerpt — enough to spot-check chapter
        # boundaries / opening lines without dumping a 50KB body
        # into the editor's HTTP response.
        excerpt_lines: list[str] = []
        truncated = False
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i >= max_lines:
                        truncated = True
                        break
                    excerpt_lines.append(line.rstrip("\n"))
        except Exception as e:
            logger.debug("preview-file: excerpt read failed for %s: %s", file_path, e)

        return {
            "story_name": story_name,
            "chapter_index": chapter_index,
            "platform": platform,
            "file_path": str(Path(file_path).relative_to(Path(file_path).anchor)) if Path(file_path).is_absolute() else file_path,
            "file_size": size,
            "modified_at": modified_at,
            "current_hash": current_hash,
            "posted_hash": posted_hash,
            "drifted": bool(posted_hash and current_hash and posted_hash != current_hash),
            "ever_posted": bool(pub),
            "posted_at": (pub or {}).get("last_updated_at") or (pub or {}).get("first_posted_at"),
            "excerpt": "\n".join(excerpt_lines),
            "excerpt_truncated": truncated,
            "excerpt_lines": len(excerpt_lines),
        }
    finally:
        conn.close()


@posting_router.get("/publications/{pub_id}")
def get_publication(pub_id: int):
    """Get a single publication by ID."""
    conn = get_connection()
    try:
        pub = posting_queries.get_publication(conn, pub_id)
        if not pub:
            raise HTTPException(404, detail="Publication not found")
        return pub
    finally:
        conn.close()


# ── Queue ─────────────────────────────────────────────────────

@posting_router.post("/queue")
def add_to_queue(body: dict):
    """Add items to the posting queue.

    Body: {
        "story_name": "Example_Story",
        "platforms": ["ib", "sf"],
        "chapters": [1, 2],        // optional
        "action": "post",          // "post" or "update"
        "scheduled_at": null       // ISO datetime or null for immediate
    }
    """
    story_name = body.get("story_name")
    platforms = body.get("platforms", [])
    chapters = body.get("chapters", [0])
    action = body.get("action", "post")
    scheduled_at = body.get("scheduled_at")

    if not story_name:
        raise HTTPException(400, detail="story_name is required")
    if not platforms:
        raise HTTPException(400, detail="platforms list is required")

    conn = get_connection()
    try:
        from posting.manager import get_platform_requires
        queue_ids = []
        for platform in platforms:
            requires = get_platform_requires(platform)
            for ch_idx in chapters:
                qid = posting_queries.add_to_queue(
                    conn, story_name, ch_idx, platform, action,
                    scheduled_at=scheduled_at,
                    requires=requires,
                )
                queue_ids.append(qid)
        return {"status": "queued", "queue_ids": queue_ids}
    except Exception as e:
        logger.error("Queue add failed: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@posting_router.get("/queue")
def get_queue(include_completed: bool = Query(False),
              content_type: str = Query(None)):
    """List posting queue items.

    content_type=None (the default) returns every type — stories AND artwork
    — so the Queue & Schedule page and the Pending-queue widget show all
    pending work, not just stories. Pass 'story'/'artwork' to scope.
    """
    conn = get_connection()
    try:
        items = posting_queries.get_queue(
            conn, include_completed=include_completed, content_type=content_type)
        return {"queue": items}
    finally:
        conn.close()


@posting_router.get("/queue/clearable")
def queue_clearable():
    """How many finished queue rows could be cleared, per status and platform.

    Declared BEFORE `/queue/{queue_id}` — FastAPI matches in definition order,
    and "clearable" would otherwise be parsed as a queue_id and 422 on the int.
    """
    conn = get_connection()
    try:
        return posting_queries.count_queue_by_status(conn)
    finally:
        conn.close()


@posting_router.post("/queue/clear")
def queue_clear(body: dict = None):
    """Delete finished queue rows (failed / cancelled / completed).

    Live work is unreachable from here: the DB helper refuses any status
    outside CLEARABLE_STATUSES, so a bad request is a 400 rather than a
    deleted schedule.
    """
    statuses = (body or {}).get("statuses") or ["failed"]
    conn = get_connection()
    try:
        try:
            n = posting_queries.clear_queue_rows(conn, statuses)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
    finally:
        conn.close()
    logger.info("Queue: cleared %d finished rows (%s)", n, ", ".join(statuses))
    return {"cleared": n, "statuses": statuses}


@posting_router.delete("/queue/{queue_id}")
def cancel_queue_item(queue_id: int):
    """Cancel a pending queue item."""
    conn = get_connection()
    try:
        if posting_queries.cancel_queue_item(conn, queue_id):
            return {"status": "cancelled", "queue_id": queue_id}
        raise HTTPException(404, detail="Queue item not found or not pending")
    finally:
        conn.close()


@posting_router.delete("/drip/{drip_group}")
def cancel_drip(drip_group: str):
    """Cancel a whole drip campaign (gap G1) — every non-terminal row that
    shares the drip_group id. Returns the cancelled count."""
    conn = get_connection()
    try:
        n = posting_queries.cancel_all_for(conn, drip_group=drip_group)
    finally:
        conn.close()
    if n == 0:
        raise HTTPException(404, detail="No pending items for that drip")
    return {"status": "cancelled", "drip_group": drip_group, "cancelled": n}


@posting_router.post("/queue/{queue_id}/reschedule")
def reschedule_queue_item(queue_id: int, body: dict):
    """Move a scheduled item to a new time. Body: {"scheduled_at": ISO8601}.

    Works for any content type (story or artwork) — the queue row is
    addressed by id. Mirrors the schedule endpoints' timezone handling:
    parse the ISO string, treat naive as UTC, store a UTC
    'YYYY-MM-DD HH:MM:SS' string matching SQLite datetime('now').
    """
    from datetime import datetime, timezone

    raw = (body or {}).get("scheduled_at")
    if not raw:
        raise HTTPException(400, detail="scheduled_at is required")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, detail="Invalid datetime format — use ISO 8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if (dt - datetime.now(timezone.utc)).total_seconds() < -30:
        raise HTTPException(400, detail="Scheduled time must be in the future")
    scheduled_str = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection()
    try:
        ok = posting_queries.reschedule_queue_item(conn, queue_id, scheduled_str)
    finally:
        conn.close()
    if not ok:
        raise HTTPException(404, detail="Queue item not found or not pending")
    return {"status": "rescheduled", "queue_id": queue_id, "scheduled_at": scheduled_str}


# ── Log ───────────────────────────────────────────────────────

@posting_router.get("/log")
def get_log(
    story_name: str = Query(None),
    limit: int = Query(50),
):
    """Get posting audit log."""
    conn = get_connection()
    try:
        entries = posting_queries.get_posting_log(conn, story_name=story_name, limit=limit)
        return {"log": entries}
    finally:
        conn.close()


# ── Settings ──────────────────────────────────────────────────

@posting_router.get("/settings")
def get_posting_settings():
    """Get posting-related settings."""
    settings = config.get_settings()
    return {
        "posting_enabled": settings.get("posting_enabled", False),
        "posting_story_archive_path": settings.get("posting_story_archive_path", ""),
        "posting_default_platforms": settings.get("posting_default_platforms", []),
        "posting_default_rating": settings.get("posting_default_rating", "adult"),
        # "Posted via PawPoller" credit line on story/artwork descriptions
        # (gap-wave-2 §1). Absent = ON by design.
        "pawpoller_attribution": settings.get("pawpoller_attribution", True),
    }


@posting_router.post("/settings")
def save_posting_settings(body: dict):
    """Save posting-related settings."""
    allowed_keys = {
        "posting_enabled", "posting_story_archive_path",
        "posting_default_platforms", "posting_default_rating",
        "posting_server_url", "posting_server_api_key",
        "pawpoller_attribution",
    }
    filtered = {k: v for k, v in body.items() if k in allowed_keys}
    config.save_settings(filtered)
    return {"status": "saved"}


# ── Claim: Retroactive sync ───────────────────────────────────

@posting_router.post("/claim")
def claim_submissions(body: dict = {}):
    """Claim existing platform submissions into the publications registry.

    Scans platform submission tables, matches to archive stories, and creates
    publication records so /update can push revisions to them.

    Body (all optional): {
        "platforms": ["ib", "fa"],  // null = all with data
        "dry_run": false            // true = preview matches without writing
    }
    """
    from posting.sync import claim_existing_submissions

    platforms = body.get("platforms")
    dry_run = body.get("dry_run", False)

    try:
        results = claim_existing_submissions(platforms=platforms, dry_run=dry_run)
        claimed = [r for r in results if r["status"] == "claimed"]
        already = [r for r in results if r["status"] == "already_claimed"]
        unmatched = [r for r in results if r["status"] == "unmatched"]

        return {
            "status": "dry_run" if dry_run else "synced",
            "claimed": len(claimed),
            "already_claimed": len(already),
            "unmatched": len(unmatched),
            "results": results,
        }
    except Exception as e:
        logger.error("Claim failed: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# ── Change Detection ───────────────────────────────────────────

@posting_router.get("/changes")
def get_changes():
    """Detect which publications have changed files since last post/update."""
    from posting.sync import detect_changes
    try:
        changes = detect_changes()
        changed = [c for c in changes if c["changed"]]
        unchanged = [c for c in changes if not c["changed"]]
        return {
            "total": len(changes),
            "changed": len(changed),
            "unchanged": len(unchanged),
            "items": changes,
        }
    except Exception as e:
        logger.error("Change detection failed: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# ── Sync: Server receives archive uploads ─────────────────────

@posting_router.post("/sync/upload")
async def sync_upload(file: UploadFile = File(...)):
    """Receive a .tar.gz archive and extract to the story archive directory.

    Called by the desktop instance to push updated story files to the server.
    The archive is extracted in-place, overwriting existing files.
    """
    from posting.story_reader import get_archive_path

    archive_path = get_archive_path()
    if not archive_path.is_dir():
        try:
            archive_path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(500, detail=f"Cannot create archive directory: {e}")

    try:
        contents = await file.read()
        buf = io.BytesIO(contents)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            # Security: reject any member that could escape the archive dir.
            # Link members (sym/hard) are rejected outright — a link followed by
            # a write through it escapes even without ".." in the names. Every
            # other member's resolved path must stay under the archive root.
            #
            # Delegated to `mirror.core.safe_extract` rather than restated here
            # (3.17.4). This endpoint used to carry its own copy of that loop,
            # and when the resolve()-only check turned out to be
            # PLATFORM-DEPENDENT — `C:\Windows\evil.dll` is absolute on
            # Windows and an ordinary filename on Linux — the fix reached the
            # two mirror call sites and left this one behind. Three copies of a
            # security check is three places for it to drift; the one that
            # drifts is the one nobody remembers exists.
            try:
                mirror_core.safe_extract(tar, archive_path)
            except mirror_core.MirrorSecurityError as e:
                raise HTTPException(400, detail=str(e))

        # Count what was extracted
        story_dirs = [d for d in archive_path.iterdir() if d.is_dir() and not d.name.startswith(".")]
        return {
            "status": "synced",
            "archive_path": str(archive_path),
            "stories": len(story_dirs),
            "bytes_received": len(contents),
        }
    except tarfile.TarError as e:
        raise HTTPException(400, detail=f"Invalid tar.gz archive: {e}")
    except Exception as e:
        logger.error("Sync upload failed: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@posting_router.post("/sync/push")
async def sync_push(body: dict):
    """Push the local story archive to a remote PawPoller server.

    Called from the desktop instance. Tars the local archive and POSTs it
    to the remote server's /api/posting/sync/upload endpoint.

    Body: {
        "server_url": "http://34.xx.xx.xx:8420",  // optional, uses setting if omitted
        "api_key": "pp_xxxx",                      // optional, uses setting if omitted
        "story_name": "Example_Story"               // optional, sync one story only
    }
    """
    import httpx as _httpx
    from posting.story_reader import get_archive_path

    settings = config.get_settings()
    server_url = body.get("server_url") or settings.get("posting_server_url", "")
    api_key = body.get("api_key") or settings.get("posting_server_api_key", "")

    if not server_url:
        raise HTTPException(400, detail="No server URL configured. Set posting_server_url in settings.")

    archive_path = get_archive_path()
    if not archive_path.is_dir():
        raise HTTPException(404, detail=f"Local archive not found at {archive_path}")

    story_filter = body.get("story_name")

    try:
        # Create tarball in memory
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            if story_filter:
                story_path = archive_path / story_filter
                if not story_path.is_dir():
                    raise HTTPException(404, detail=f"Story not found: {story_filter}")
                tar.add(str(story_path), arcname=story_filter)
            else:
                for entry in sorted(archive_path.iterdir()):
                    if entry.is_dir() and not entry.name.startswith("."):
                        tar.add(str(entry), arcname=entry.name)
        buf.seek(0)
        tar_bytes = buf.getvalue()

        # POST to remote server
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        async with _httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{server_url.rstrip('/')}/api/posting/sync/upload",
                files={"file": ("story-archive.tar.gz", tar_bytes, "application/gzip")},
                headers=headers,
            )

        if resp.status_code != 200:
            raise HTTPException(502, detail=f"Remote server returned {resp.status_code}: {resp.text[:200]}")

        result = resp.json()
        result["bytes_sent"] = len(tar_bytes)
        result["synced_from"] = str(archive_path)
        if story_filter:
            result["story_filter"] = story_filter
        return result

    except _httpx.HTTPError as e:
        raise HTTPException(502, detail=f"Failed to reach remote server: {e}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Sync push failed: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# ── Change Detection ──────────────────────────────────────────

def _hash_file(path: str) -> str:
    """SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:16]  # Short hash for display


def _hash_story(story_path: Path) -> dict:
    """Hash all posting-relevant files in a story folder."""
    hashes = {}
    for pattern in ["Markdown/MASTER.md", "Tags/tags_upload.txt", "Chapters/split_manifest.json"]:
        f = story_path / pattern
        if f.is_file():
            hashes[pattern] = _hash_file(str(f))
    # Hash chapter format files
    for subdir in ["Chapters/BBCode", "Chapters/SoFurry_HTML", "BBCode", "PDF"]:
        d = story_path / subdir
        if d.is_dir():
            for f in sorted(d.iterdir()):
                if f.is_file():
                    rel = f"{subdir}/{f.name}"
                    hashes[rel] = _hash_file(str(f))
    return hashes


@posting_router.get("/sync/status")
def get_sync_status():
    """Check which stories have changed since they were last posted.

    Compares current file hashes against the format_file hash stored
    in publications when the story was last posted/updated.
    """
    from posting.story_reader import get_archive_path

    archive_path = get_archive_path()
    if not archive_path.is_dir():
        return {"stories": [], "archive_path": str(archive_path), "error": "Archive not found"}

    conn = get_connection()
    try:
        pubs = posting_queries.get_publications(conn, status="posted")
    finally:
        conn.close()

    # Group publications by story
    pub_by_story: dict[str, list] = {}
    for p in pubs:
        pub_by_story.setdefault(p["story_name"], []).append(p)

    stories = []
    for entry in sorted(archive_path.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name == "Reference_Guides":
            continue

        story_name = entry.name
        current_hashes = _hash_story(entry)
        master_hash = current_hashes.get("Markdown/MASTER.md", "")

        story_pubs = pub_by_story.get(story_name, [])
        published_platforms = [p["platform"] for p in story_pubs]
        last_posted = max((p["first_posted_at"] or "" for p in story_pubs), default="")
        last_updated = max((p["last_updated_at"] or "" for p in story_pubs), default="")

        # Detect changes: compare current MASTER hash against what was last posted
        # We store the hash of the format file used, but MASTER.md is the source of truth
        changed = False
        if story_pubs:
            # Check if any posted format file has changed
            for p in story_pubs:
                fmt_file = p.get("format_file", "")
                if fmt_file:
                    # Get relative path within story folder
                    try:
                        full_path = entry / fmt_file if not os.path.isabs(fmt_file) else Path(fmt_file)
                        if full_path.is_file():
                            current = _hash_file(str(full_path))
                            # We don't have the old hash stored yet, so compare against pub timestamp
                            # If the file's mtime is newer than last_updated, it's changed
                            import datetime
                            mtime = datetime.datetime.fromtimestamp(full_path.stat().st_mtime)
                            if last_updated:
                                try:
                                    posted_dt = datetime.datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
                                    if mtime.replace(tzinfo=None) > posted_dt.replace(tzinfo=None):
                                        changed = True
                                except (ValueError, TypeError):
                                    pass
                    except (OSError, ValueError):
                        pass

        stories.append({
            "name": story_name,
            "master_hash": master_hash,
            "file_count": len(current_hashes),
            "published_to": published_platforms,
            "last_posted": last_posted,
            "last_updated": last_updated,
            "changed": changed,
            "status": "changed" if changed else ("published" if story_pubs else "not published"),
        })

    return {"stories": stories, "archive_path": str(archive_path)}
