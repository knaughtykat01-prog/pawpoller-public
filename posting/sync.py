"""Retroactive sync — claim existing platform submissions into the publications registry.

Scans each platform's submissions table in PawPoller's database, matches them
to stories in the local archive by title, and populates the publications table
so that future /update commands can push revisions to already-live submissions.

Matching strategy:
  1. Normalize both story folder names and submission titles (lowercase, strip
     punctuation, collapse whitespace)
  2. Full-story match: "Example Story" ↔ Example_Story
  3. Chapter match: "Example Story - Chapter 1: The Opening" ↔ Example_Story ch1
  4. Multi-part match: "Another Story (Part One)" ↔ Another_Story ch1-4
  5. Skip test submissions (titles starting with [TEST])
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timezone

from database.db import get_connection
from database import posting_queries
from posting import story_reader

logger = logging.getLogger(__name__)

# Platform submission table configs: (table, id_col, title_col, url_template)
PLATFORM_TABLES = {
    "ib": {
        "table": "submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://inkbunny.net/s/{id}",
    },
    "fa": {
        "table": "fa_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://www.furaffinity.net/view/{id}/",
    },
    "ws": {
        "table": "ws_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://www.weasyl.com/submission/{id}",
    },
    "sf": {
        "table": "sf_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://sofurry.com/s/{id}",
    },
    "sqw": {
        "table": "sqw_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://squidgeworld.org/works/{id}",
    },
    "ao3": {
        "table": "ao3_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://archiveofourown.org/works/{id}",
    },
    "wp": {
        "table": "wp_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://www.wattpad.com/story/{id}",
    },
    "bsky": {
        "table": "bsky_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://bsky.app/profile/_/post/{id}",
    },
    "da": {
        "table": "da_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://www.deviantart.com/deviation/{id}",
    },
    "ik": {
        "table": "ik_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://itaku.ee/images/{id}",
    },
    # Newer platforms (2.69.0): registered so their polled submissions are
    # discoverable + importable too. Each stores a `link` permalink (preferred
    # for the external URL) + a `thumbnail_url` image. Pixiv & Instagram are
    # image-first; the microblogs carry images on some posts. url_template is a
    # best-effort fallback only (the stored `link` wins in build_discovered).
    "mast": {
        "table": "mast_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://mastodon.social/web/statuses/{id}",
    },
    "tum": {
        "table": "tum_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://www.tumblr.com/blog/view/_/{id}",
    },
    "pix": {
        "table": "pix_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://www.pixiv.net/artworks/{id}",
    },
    "thr": {
        "table": "thr_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://www.threads.net/t/{id}",
    },
    "ig": {
        "table": "ig_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://www.instagram.com/p/{id}/",
    },
    "tw": {
        "table": "tw_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://twitter.com/i/status/{id}",
    },
    "e621": {
        "table": "e621_submissions",
        "id_col": "submission_id",
        "title_col": "title",
        "url_template": "https://e621.net/posts/{id}",
    },
}

# Word-to-number mapping for "Part One", "Part Two" etc.
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _normalize(text: str) -> str:
    """Normalize a title for fuzzy matching."""
    text = text.lower().strip()
    text = re.sub(r'\[test\]', '', text)
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _story_name_to_title(name: str) -> str:
    """Convert folder name to normalized title.

    Handles compound names: My_Story/Alt_Version → my story alt version
    """
    return _normalize(name.replace("_", " ").replace("/", " "))


def claim_existing_submissions(
    platforms: list[str] | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Scan platform tables and match submissions to archive stories.

    Args:
        platforms: Which platforms to scan (None = all with data).
        dry_run: If True, return matches without writing to publications.

    Returns:
        List of match dicts: {platform, story_name, chapter_index, external_id, title, url, status}
    """
    # Load all stories from archive
    stories = {}
    try:
        for s in story_reader.list_stories():
            try:
                info = story_reader.load_story(s["name"])
                stories[s["name"]] = info
            except Exception as e:
                logger.warning("Could not load story %s: %s", s["name"], e)
    except Exception as e:
        logger.error("Could not list stories: %s", e)
        return [{"error": f"Could not list stories: {e}"}]

    # Build lookup: normalized title → (story_name, chapter_index, chapter_title)
    title_map: list[tuple[str, str, int, str]] = []
    for name, info in stories.items():
        # Full story match
        norm = _story_name_to_title(name)
        title_map.append((norm, name, 0, ""))

        # Chapter matches
        for ch in info.chapters:
            # "Chapter 1: The Arrangement" → multiple match patterns
            ch_title_norm = _normalize(ch.title)
            # Pattern: "story - chapter title"
            title_map.append((f"{norm} {ch_title_norm}", name, ch.index, ch.title))
            # Pattern: "story chapter N"
            title_map.append((f"{norm} chapter {ch.index}", name, ch.index, ch.title))
            # Pattern: "story part N"
            title_map.append((f"{norm} part {ch.index}", name, ch.index, ch.title))

    conn = get_connection()
    results = []

    try:
        scan_platforms = platforms or list(PLATFORM_TABLES.keys())

        for platform in scan_platforms:
            cfg = PLATFORM_TABLES.get(platform)
            if not cfg:
                continue

            try:
                rows = conn.execute(
                    f"SELECT {cfg['id_col']}, {cfg['title_col']} FROM {cfg['table']}"
                ).fetchall()
            except Exception:
                continue  # Table doesn't exist or is empty

            if not rows:
                continue

            for row in rows:
                ext_id = str(row[0])
                title = row[1] or ""
                norm_title = _normalize(title)

                # Skip test submissions
                if "[test]" in title.lower():
                    continue

                # Try to match
                match = _find_match(norm_title, title_map, stories)
                if not match:
                    results.append({
                        "platform": platform,
                        "external_id": ext_id,
                        "title": title,
                        "status": "unmatched",
                        "story_name": None,
                        "chapter_index": None,
                    })
                    continue

                story_name, chapter_index, chapter_title = match
                url = cfg["url_template"].format(id=ext_id)

                # Check if already claimed
                existing = posting_queries.get_publication_by_story(
                    conn, story_name, chapter_index, platform
                )
                if existing:
                    results.append({
                        "platform": platform,
                        "external_id": ext_id,
                        "title": title,
                        "status": "already_claimed",
                        "story_name": story_name,
                        "chapter_index": chapter_index,
                    })
                    continue

                if not dry_run:
                    posting_queries.upsert_publication(
                        conn, story_name, chapter_index, platform,
                        external_id=ext_id,
                        external_url=url,
                        title_used=title,
                        status="posted",
                    )

                results.append({
                    "platform": platform,
                    "external_id": ext_id,
                    "title": title,
                    "url": url,
                    "status": "claimed",
                    "story_name": story_name,
                    "chapter_index": chapter_index,
                    "chapter_title": chapter_title,
                })

    finally:
        conn.close()

    claimed = sum(1 for r in results if r["status"] == "claimed")
    already = sum(1 for r in results if r["status"] == "already_claimed")
    unmatched = sum(1 for r in results if r["status"] == "unmatched")
    logger.info(
        "Claim sync: %d claimed, %d already claimed, %d unmatched",
        claimed, already, unmatched,
    )
    return results


def _find_match(
    norm_title: str,
    title_map: list[tuple[str, str, int, str]],
    stories: dict,
) -> tuple[str, int, str] | None:
    """Find the best story/chapter match for a normalized submission title.

    Returns (story_name, chapter_index, chapter_title) or None.
    """
    # Direct exact match
    for pattern, story_name, ch_idx, ch_title in title_map:
        if norm_title == pattern:
            return (story_name, ch_idx, ch_title)

    # Contains match — submission title contains the story name
    # Sort by longest pattern first (prefer more specific matches)
    sorted_patterns = sorted(title_map, key=lambda t: len(t[0]), reverse=True)
    for pattern, story_name, ch_idx, ch_title in sorted_patterns:
        if pattern in norm_title and len(pattern) > 5:
            # Check if there's chapter info in the title we can extract
            if ch_idx == 0:
                # Full story pattern matched — check for chapter number in title
                ch_num = _extract_chapter_number(norm_title, story_name)
                if ch_num and story_name in stories:
                    info = stories[story_name]
                    for ch in info.chapters:
                        if ch.index == ch_num:
                            return (story_name, ch_num, ch.title)
                # No chapter found — treat as full story
                return (story_name, 0, "")
            else:
                return (story_name, ch_idx, ch_title)

    # "Part One/Two" matching for split uploads
    for story_name_norm, story_name, _, _ in title_map:
        if story_name_norm in norm_title:
            part_match = re.search(r'part\s+(\w+)', norm_title)
            if part_match:
                part_word = part_match.group(1)
                part_num = _WORD_NUMBERS.get(part_word)
                if part_num is None:
                    try:
                        part_num = int(part_word)
                    except ValueError:
                        pass
                if part_num:
                    return (story_name, part_num, f"Part {part_num}")

    return None


def _extract_chapter_number(norm_title: str, story_name: str) -> int | None:
    """Try to extract a chapter number from a submission title."""
    patterns = [
        r'chapter\s*(\d+)',
        r'ch\s*(\d+)',
        r'part\s*(\d+)',
    ]
    for pattern in patterns:
        m = re.search(pattern, norm_title)
        if m:
            return int(m.group(1))
    return None


# ── Change Detection ──────────────────────────────────────────


def hash_file(file_path: str) -> str:
    """Compute SHA-256 hash of a file. Returns hex digest or empty string if file missing."""
    if not file_path or not os.path.isfile(file_path):
        return ""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_changes(story_name: str | None = None) -> list[dict]:
    """Compare current archive files against what was last posted.

    For each publication, resolves the current format file, hashes it, and
    compares against the stored file_hash. Returns a list of change records.

    Args:
        story_name: If provided, only check publications for this story.
            Used by the story detail page to avoid hashing every story's
            files just to render one page.

    Returns:
        List of dicts: {story_name, chapter_index, platform, external_id, status,
                        current_hash, posted_hash, changed, file_path}
        Where status is: 'changed', 'unchanged', 'file_missing', 'no_hash' (never posted via PawPoller)
    """
    conn = get_connection()
    results = []

    try:
        pubs = posting_queries.get_publications(conn, story_name=story_name, status="posted")
        if not pubs:
            return []

        # Group by story to avoid loading the same story repeatedly
        by_story: dict[str, list[dict]] = {}
        for pub in pubs:
            sn = pub["story_name"]
            if sn not in by_story:
                by_story[sn] = []
            by_story[sn].append(pub)

        for story_name, story_pubs in by_story.items():
            try:
                story = story_reader.load_story(story_name)
            except Exception:
                for pub in story_pubs:
                    results.append({
                        "story_name": story_name,
                        "chapter_index": pub["chapter_index"],
                        "platform": pub["platform"],
                        "account_id": pub["account_id"],
                        "external_id": pub["external_id"],
                        "status": "file_missing",
                        "changed": False,
                        "current_hash": "",
                        "posted_hash": pub.get("file_hash", ""),
                    })
                continue

            for pub in story_pubs:
                ch_idx = pub["chapter_index"]
                platform = pub["platform"]
                posted_hash = pub.get("file_hash", "")

                # Resolve current file
                file_path, _ = story_reader._resolve_format_file(story, ch_idx, platform)
                current_hash = hash_file(file_path) if file_path else ""

                if not posted_hash:
                    # Never posted through PawPoller (claimed from existing, no hash stored)
                    status = "no_hash"
                    changed = True  # Conservative: treat as needing update
                elif not current_hash:
                    status = "file_missing"
                    changed = False
                elif current_hash != posted_hash:
                    status = "changed"
                    changed = True
                else:
                    status = "unchanged"
                    changed = False

                results.append({
                    "story_name": story_name,
                    "chapter_index": ch_idx,
                    "platform": platform,
                    "account_id": pub["account_id"],
                    "external_id": pub["external_id"],
                    "status": status,
                    "changed": changed,
                    "current_hash": current_hash,
                    "posted_hash": posted_hash,
                    "file_path": file_path or "",
                })
    finally:
        conn.close()

    return results


def get_changed_stories() -> dict[str, list[dict]]:
    """Get stories with changes, grouped by story name.

    Convenience wrapper around detect_changes() that filters to only changed items
    and groups them by story.
    """
    changes = detect_changes()
    changed_only = [c for c in changes if c["changed"]]

    by_story: dict[str, list[dict]] = {}
    for c in changed_only:
        sn = c["story_name"]
        if sn not in by_story:
            by_story[sn] = []
        by_story[sn].append(c)

    return by_story


def get_sync_status_summary() -> list[dict]:
    """Get per-story sync status for the dashboard.

    Returns a list of story summaries with change counts per platform.
    """
    all_changes = detect_changes()

    # Group by story
    by_story: dict[str, list[dict]] = {}
    for c in all_changes:
        sn = c["story_name"]
        if sn not in by_story:
            by_story[sn] = []
        by_story[sn].append(c)

    summaries = []
    for story_name, items in sorted(by_story.items()):
        changed_count = sum(1 for i in items if i["changed"])
        total = len(items)
        platforms = sorted(set(i["platform"] for i in items))
        changed_platforms = sorted(set(i["platform"] for i in items if i["changed"]))

        summaries.append({
            "name": story_name,
            "total_publications": total,
            "changed_count": changed_count,
            "changed": changed_count > 0,
            "platforms": platforms,
            "changed_platforms": changed_platforms,
            "status": "needs update" if changed_count > 0 else "up to date",
        })

    return summaries
