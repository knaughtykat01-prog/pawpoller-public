"""Story importer — downloads content from platforms and creates local archive folders.

Supports importing existing published submissions from Inkbunny and SoFurry into
the local story archive. The importer:
  1. Fetches full submission metadata from the platform client
  2. Downloads the story content (BBCode from IB, HTML from SF)
  3. Creates the standard folder structure with story.json + MASTER.md
  4. Optionally downloads the cover/thumbnail image

This is a first-version importer — stories are imported as single-chapter works
with whatever format the platform provides, plus a basic MASTER.md created by
stripping formatting from the source content.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx

import config
from posting.story_reader import get_archive_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Folder name sanitisation
# ---------------------------------------------------------------------------

def _sanitize_folder_name(title: str) -> str:
    """Convert a submission title to a safe folder name.

    Strips non-alphanumeric characters (except underscores), collapses
    whitespace to single underscores, and truncates to 80 characters.
    """
    # Replace common separators with underscores
    name = re.sub(r'[\s\-–—]+', '_', title.strip())
    # Keep only letters, digits, underscores
    name = re.sub(r'[^A-Za-z0-9_]', '', name)
    # Collapse multiple underscores
    name = re.sub(r'_+', '_', name).strip('_')
    # Truncate
    if len(name) > 80:
        name = name[:80].rstrip('_')
    return name or 'Imported_Story'


# ---------------------------------------------------------------------------
# Duplicate detection — match by (platform, submission_id) in story.json
# ---------------------------------------------------------------------------

def _find_existing_import(platform: str, submission_id: str) -> str | None:
    """Return the folder name of an existing import for (platform, submission_id), or None.

    Scans every `<archive>/<story>/story.json` for an `import_source` block
    matching the given platform + submission id. Used by the importer to
    short-circuit when the same submission is imported twice — the old
    behaviour appended `_2`, `_3` suffixes which produced byte-identical
    duplicate folders. The list endpoint also dedupes against this same
    field so a submission disappears from the picker once imported, but
    the manual "Import by URL or ID" path bypasses that filter and is
    where the dupes actually came from.
    """
    if not platform or not submission_id:
        return None
    archive = get_archive_path()
    if not archive.is_dir():
        return None
    sid = str(submission_id)
    for entry in archive.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        sj = entry / "story.json"
        if not sj.is_file():
            continue
        try:
            data = json.loads(sj.read_text(encoding="utf-8"))
        except Exception:
            continue
        src = data.get("import_source") or {}
        if src.get("platform") == platform and str(src.get("submission_id", "")) == sid:
            return entry.name
    return None


def _existing_import_response(existing_name: str, platform: str, submission_id: str) -> dict:
    """Build the standard importer return dict for an already-imported work."""
    archive = get_archive_path()
    title = existing_name
    is_draft = False
    sj = archive / existing_name / "story.json"
    if sj.is_file():
        try:
            data = json.loads(sj.read_text(encoding="utf-8"))
            title = data.get("title", existing_name)
        except Exception:
            pass
    logger.info(
        "Import skipped — %s/%s already imported as %s",
        platform, submission_id, existing_name,
    )
    return {
        "story_name": existing_name,
        "title": title,
        "is_draft": is_draft,
        "already_imported": True,
    }


# ---------------------------------------------------------------------------
# Content stripping — produce basic markdown from BBCode / HTML
# ---------------------------------------------------------------------------

def _bbcode_to_markdown(bbcode: str) -> str:
    """Convert BBCode to basic markdown for MASTER.md.

    Handles the most common BBCode tags used in Inkbunny stories. This is
    intentionally simple — the goal is a readable MASTER.md, not a perfect
    round-trip conversion.
    """
    text = bbcode

    # Bold: [b]...[/b] → **...**
    text = re.sub(r'\[b\](.*?)\[/b\]', r'**\1**', text, flags=re.IGNORECASE | re.DOTALL)
    # Italic: [i]...[/i] → *...*
    text = re.sub(r'\[i\](.*?)\[/i\]', r'*\1*', text, flags=re.IGNORECASE | re.DOTALL)
    # Underline: [u]...[/u] → just the text (no markdown equivalent)
    text = re.sub(r'\[u\](.*?)\[/u\]', r'\1', text, flags=re.IGNORECASE | re.DOTALL)
    # Strikethrough: [s]...[/s] → ~~...~~
    text = re.sub(r'\[s\](.*?)\[/s\]', r'~~\1~~', text, flags=re.IGNORECASE | re.DOTALL)
    # URLs: [url=X]text[/url] → [text](X)
    text = re.sub(r'\[url=(.*?)\](.*?)\[/url\]', r'[\2](\1)', text, flags=re.IGNORECASE | re.DOTALL)
    # Plain URLs: [url]X[/url] → X
    text = re.sub(r'\[url\](.*?)\[/url\]', r'\1', text, flags=re.IGNORECASE | re.DOTALL)
    # Horizontal rule: [hr] → ---
    text = re.sub(r'\[hr\]', '\n---\n', text, flags=re.IGNORECASE)
    # Center/left/right alignment — just strip the tags
    text = re.sub(r'\[/?(?:center|left|right)\]', '', text, flags=re.IGNORECASE)
    # Color/size/font tags — strip
    text = re.sub(r'\[(?:color|size|font)=[^\]]*\]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[/(?:color|size|font)\]', '', text, flags=re.IGNORECASE)
    # Quote: [quote]...[/quote] → > blockquote
    def _quote_block(m):
        lines = m.group(1).strip().split('\n')
        return '\n'.join(f'> {line}' for line in lines)
    text = re.sub(r'\[quote(?:=[^\]]*)?\](.*?)\[/quote\]', _quote_block, text, flags=re.IGNORECASE | re.DOTALL)
    # Strip remaining BBCode tags
    text = re.sub(r'\[/?[a-z]+(?:=[^\]]*?)?\]', '', text, flags=re.IGNORECASE)

    # Normalise line endings
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    return text.strip()


def _html_to_markdown(html_content: str) -> str:
    """Convert HTML to basic markdown for MASTER.md.

    Handles common HTML elements found in SoFurry story content. Like the
    BBCode converter, this is intentionally simple.
    """
    text = html_content

    # Remove script/style blocks
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.IGNORECASE | re.DOTALL)

    # Headings: <h1>-<h6> → # markers
    for i in range(1, 7):
        text = re.sub(rf'<h{i}[^>]*>(.*?)</h{i}>', rf'\n{"#" * i} \1\n', text, flags=re.IGNORECASE | re.DOTALL)

    # Bold: <b>, <strong> → **...**
    text = re.sub(r'<(?:b|strong)[^>]*>(.*?)</(?:b|strong)>', r'**\1**', text, flags=re.IGNORECASE | re.DOTALL)
    # Italic: <i>, <em> → *...*
    text = re.sub(r'<(?:i|em)[^>]*>(.*?)</(?:i|em)>', r'*\1*', text, flags=re.IGNORECASE | re.DOTALL)

    # Line breaks: <br> → newline
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    # Paragraphs: <p>...</p> → double newline
    text = re.sub(r'<p[^>]*>(.*?)</p>', r'\n\n\1\n\n', text, flags=re.IGNORECASE | re.DOTALL)
    # Horizontal rules: <hr> → ---
    text = re.sub(r'<hr[^>]*/?>', '\n---\n', text, flags=re.IGNORECASE)

    # Links: <a href="X">text</a> → [text](X)
    text = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r'[\2](\1)', text, flags=re.IGNORECASE | re.DOTALL)

    # Strip all remaining HTML tags
    text = re.sub(r'<[^>]+>', '', text)

    # Decode HTML entities
    import html
    text = html.unescape(text)

    # Normalise whitespace
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # Collapse excessive blank lines (3+ → 2)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


# ---------------------------------------------------------------------------
# IB rating map
# ---------------------------------------------------------------------------

_IB_RATING_MAP = {
    "0": "general",
    "1": "mature",
    "2": "explicit",
}


# ---------------------------------------------------------------------------
# Story folder creation
# ---------------------------------------------------------------------------

def _create_story_folder(
    name: str,
    title: str,
    author: str,
    description: str,
    tags: list[str],
    rating: str,
    content: str,
    content_format: str,
    cover_url: str = "",
    platform: str = "",
    submission_id: str = "",
    source_url: str = "",
) -> str:
    """Create the full story folder structure and return the story name.

    Args:
        name: Sanitised folder name.
        title: Display title.
        author: Author name.
        description: Story description/summary.
        tags: List of tag strings.
        rating: Rating string (general, mature, explicit).
        content: The story content text.
        content_format: Format of content — "bbcode", "html", or "markdown".
        cover_url: URL to a cover image (optional, downloaded if provided).
        platform: Source platform code (e.g. "ib", "sf").
        submission_id: Original submission ID on the platform.
        source_url: Direct URL to the submission.

    Returns:
        The story folder name (same as `name`).
    """
    archive = get_archive_path()
    story_dir = archive / name

    if story_dir.exists():
        # Append a suffix to avoid collisions
        i = 2
        while (archive / f"{name}_{i}").exists():
            i += 1
        name = f"{name}_{i}"
        story_dir = archive / name

    # Create directory structure
    dirs = [
        story_dir / "Markdown",
        story_dir / "BBCode",
        story_dir / "HTML",
        story_dir / "Images",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    # Save original format content
    if content_format == "bbcode":
        (story_dir / "BBCode" / f"{name}_bbcode.txt").write_text(content, encoding="utf-8")
    elif content_format == "html":
        (story_dir / "HTML" / f"{name}_SoFurry.html").write_text(content, encoding="utf-8")

    # Create MASTER.md from content
    if content_format == "bbcode":
        body = _bbcode_to_markdown(content)
    elif content_format == "html":
        body = _html_to_markdown(content)
    else:
        body = content

    # Count words in the converted body
    word_count = len(body.split())

    master_content = f"""<!-- @title -->
# {title}

<!-- @subtitle -->
*Imported from {platform.upper()}*

<!-- @byline -->
by {author or 'Unknown'}

<!-- @body -->

{body}

<!-- @story-end -->
"""
    (story_dir / "Markdown" / "MASTER.md").write_text(master_content, encoding="utf-8")

    # Generate story.json
    story_json = {
        "title": title,
        "author": author,
        "description": description,
        "summary": "",
        "rating": rating,
        "category": "",
        "fandom": "Original Work",
        "genre": "",
        "warnings": [],
        "characters": [],
        "relationships": [],
        "word_count": word_count,
        "chapters": 1,
        "tags": {"default": tags},
        "chapter_info": [],
        "formats": {"bbcode": True, "html": True, "markdown": True},
        "images": {"cover": ""},
        "import_source": {
            "platform": platform,
            "submission_id": submission_id,
            "url": source_url,
        },
    }
    (story_dir / "story.json").write_text(
        json.dumps(story_json, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    logger.info("Created imported story folder: %s (%d words from %s)", name, word_count, platform)
    return name


# ---------------------------------------------------------------------------
# Platform importers
# ---------------------------------------------------------------------------

async def import_from_inkbunny(submission_id: str) -> dict:
    """Download an IB submission and create a local story folder.

    Uses the Inkbunny API to fetch submission details with show_writing=yes
    to get the file URL, then downloads the BBCode content and creates the
    local folder structure.

    Returns:
        Dict with 'story_name' and 'title'.
    """
    existing = _find_existing_import("ib", submission_id)
    if existing:
        return _existing_import_response(existing, "ib", submission_id)

    from clients.ib.client import InkbunnyClient
    from database.db import get_connection
    from database import queries

    settings = config.get_settings()
    username = settings.get("username", "")
    password = settings.get("password", "")
    if not username or not password:
        raise RuntimeError("Inkbunny credentials not configured — set up in Settings")

    # Look up the submission in the local DB first for basic metadata
    conn = get_connection()
    try:
        db_sub = queries.get_submission(conn, int(submission_id))
    finally:
        conn.close()

    async def _ib_fetch(client) -> dict:
        # Reuse the cached session ID (SID) the poller writes to the DB
        # after each successful login. Imports use the default IB account.
        conn = get_connection()
        try:
            from database import accounts as _accts
            _ib_acct = _accts.get_default_account_id(conn, "ib", create=True)
            cached = queries.get_cached_session(conn, _ib_acct)
        finally:
            conn.close()
        cached_sid = cached["sid"] if cached else None
        cached_uid = cached.get("user_id", 0) if cached else 0
        if cached_uid:
            client.user_id = cached_uid
        await client.ensure_session(cached_sid)

        resp = await client._http.post(
            f"{config.INKBUNNY_API_BASE}/api_submissions.php",
            data={
                "sid": client.sid,
                "submission_ids": str(submission_id),
                "show_description": "yes",
                "show_writing": "yes",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if "error_code" in data:
            raise RuntimeError(f"IB API error: {data.get('error_message', data)}")
        submissions = data.get("submissions", [])
        if not submissions:
            raise RuntimeError(f"Submission {submission_id} not found on Inkbunny")
        sub = submissions[0]

        # Get the file URL — Inkbunny stores files in a "files" array.
        files = sub.get("files", [])
        content = ""
        if files:
            file_info = files[0]
            file_url = file_info.get("file_url_full", "") or file_info.get("file_url_screen", "")
            if file_url:
                async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as dl:
                    file_resp = await dl.get(file_url)
                    file_resp.raise_for_status()
                    content = file_resp.text
            else:
                content = file_info.get("writing_text", "")
        if not content:
            content = sub.get("description", "") or "(No content available)"
            logger.warning("IB import %s: no file content found, using description", submission_id)
        return {"sub": sub, "content": content}

    from polling.cf_proxy import (
        proxy_kwargs,
        proxy_kwargs_fallback as _proxy_fb,
        is_blocking_failure as _is_blk,
    )

    direct_client = InkbunnyClient(username=username, password=password,
                                   **proxy_kwargs(settings, "ib"))
    try:
        try:
            fetched = await _ib_fetch(direct_client)
        except Exception as e:
            if not _is_blk(e):
                raise
            creds = _proxy_fb(settings, "ib")
            if not creds:
                raise
            logger.warning("IB import direct call failed (%s); retrying via CF Worker proxy", e)
            await direct_client.close()
            direct_client = InkbunnyClient(username=username, password=password, **creds)
            fetched = await _ib_fetch(direct_client)
    finally:
        await direct_client.close()

    sub = fetched["sub"]
    content = fetched["content"]
    title = sub.get("title", f"IB_{submission_id}")
    author = sub.get("username", "")
    description = sub.get("description", "")
    keywords = [k.get("keyword_name", "") for k in sub.get("keywords", []) if k.get("keyword_name")]
    rating = _IB_RATING_MAP.get(str(sub.get("rating_id", "0")), "general")
    is_draft = str(sub.get("public", "yes")).lower() == "no"
    url = f"https://inkbunny.net/s/{submission_id}"
    cover_url = (
        sub.get("thumbnail_url_huge", "")
        or sub.get("thumbnail_url_large", "")
        or sub.get("thumbnail_url_medium", "")
        or sub.get("thumbnail_url_medium_noncustom", "")
    )

    folder_name = _sanitize_folder_name(title)
    story_name = _create_story_folder(
        name=folder_name,
        title=title,
        author=author,
        description=description,
        tags=keywords,
        rating=rating,
        content=content,
        content_format="bbcode",
        cover_url=cover_url,
        platform="ib",
        submission_id=str(submission_id),
        source_url=url,
    )

    return {"story_name": story_name, "title": title, "is_draft": is_draft}


async def import_from_sofurry(submission_id: str) -> dict:
    """Download an SF submission and create a local story folder.

    Fetches the submission metadata from the SF JSON API, then scrapes
    the submission page for the story content HTML.

    Returns:
        Dict with 'story_name' and 'title'.
    """
    existing = _find_existing_import("sf", submission_id)
    if existing:
        return _existing_import_response(existing, "sf", submission_id)

    from clients.sf.client import SoFurryClient, SOFURRY_BASE

    settings = config.get_settings()
    sf_token = settings.get("sf_api_token", "")
    sf_display = settings.get("sf_display_name", "")

    proxy_url = settings.get("cf_worker_url", "")
    proxy_key = settings.get("cf_worker_key", "")

    if not sf_token:
        raise RuntimeError("SoFurry is not connected — add a Personal Access Token in Settings")

    client = SoFurryClient(
        api_token=sf_token,
        display_name=sf_display,
        proxy_url=proxy_url,
        proxy_key=proxy_key,
    )

    try:
        # Metadata via the official API, which returns the owner's private works
        # too — so importing an unpublished draft still resolves.
        data = await client.get_submission(submission_id)
        if not data:
            raise RuntimeError(f"SF submission {submission_id} not found")
        title = data.get("title", f"SF_{submission_id}")
        author_val = data.get("author")
        if isinstance(author_val, dict):
            author = author_val.get("handle") or author_val.get("username") or (sf_display or "")
        else:
            author = author_val or sf_display or ""
        description = data.get("description", "")
        tags = data.get("tags", []) or []
        sf_rating = data.get("rating", 0)
        if sf_rating >= 20:
            rating = "explicit"
        elif sf_rating >= 10:
            rating = "mature"
        else:
            rating = "general"

        cover_url = data.get("coverUrl", "") or data.get("thumbUrl", "")
        url = f"{SOFURRY_BASE}/s/{submission_id}"

        # SF drafts have publishedAt unset (null/empty/"0000-00-00...") or
        # set to a future ISO date. The /ui/submission JSON endpoint
        # returns owner drafts the same way it returns published works.
        published_at = (data.get("publishedAt") or "").strip()
        is_draft = (
            not published_at
            or published_at.startswith("0000")
            or published_at == "0"
        )
        if not is_draft:
            try:
                from datetime import datetime, timezone
                pub_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                if pub_dt > datetime.now(tz=timezone.utc):
                    is_draft = True
            except (ValueError, TypeError):
                pass

        import asyncio
        await asyncio.sleep(0.5)

        # /s/{id} renders for owner-viewed drafts but we tolerate non-200
        # rather than failing the whole import — the description from JSON
        # remains a usable fallback so at least metadata + summary land.
        page_resp = await client.get_page(f"{SOFURRY_BASE}/s/{submission_id}")
        if page_resp.status_code != 200:
            if is_draft:
                logger.warning(
                    "SF draft %s page returned %s — falling back to description",
                    submission_id, page_resp.status_code,
                )
                page_html = ""
            else:
                raise RuntimeError(f"Could not fetch SF submission page (status {page_resp.status_code})")
        else:
            page_html = page_resp.text

        content = ""

        # SF renders story text after a chapter divider line inside
        # the story-content-holder div. Extract everything between the
        # divider and the next major page section (comments/footer).
        divider_idx = page_html.find('background-color: #575757')
        if divider_idx > 0:
            # Skip past the closing </div> of the divider
            start = page_html.find('>', divider_idx + 30)
            if start > 0:
                start += 1
                # Find the end — look for comment section or footer
                end_markers = ['id="comments"', 'class="comment-', '<footer', 'id="submission-actions"']
                end = len(page_html)
                for marker in end_markers:
                    m_idx = page_html.find(marker, start)
                    if m_idx > 0 and m_idx < end:
                        end = m_idx
                # Walk back to find the enclosing tag start
                content = page_html[start:end].strip()
                # Strip trailing closing divs
                while content.endswith('</div>'):
                    content = content[:-6].strip()

        if not content:
            content = description or "(No content available)"
            logger.warning("SF import %s: no story content found, using description", submission_id)

    finally:
        await client.close()

    folder_name = _sanitize_folder_name(title)
    story_name = _create_story_folder(
        name=folder_name,
        title=title,
        author=author,
        description=description,
        tags=tags,
        rating=rating,
        content=content,
        content_format="html",
        cover_url=cover_url,
        platform="sf",
        submission_id=str(submission_id),
        source_url=url,
    )

    return {"story_name": story_name, "title": title, "is_draft": is_draft}


async def import_from_furaffinity(submission_id: str) -> dict:
    """Import a story from FurAffinity via FAExport API.

    Downloads the story file (TXT/PDF/DOC) from the download URL,
    extracts text content, and creates a local story folder.
    """
    existing = _find_existing_import("fa", submission_id)
    if existing:
        return _existing_import_response(existing, "fa", submission_id)

    from clients.fa.client import FAClient

    settings = config.get_settings()
    fa_username = settings.get("fa_username", "")
    cookie_a = settings.get("fa_cookie_a", "")
    cookie_b = settings.get("fa_cookie_b", "")

    if not cookie_a or not cookie_b:
        raise RuntimeError("FA cookies not configured — set up in Settings")

    from polling.cf_proxy import (
        proxy_kwargs as _fa_proxy_kwargs,
        proxy_kwargs_fallback as _fa_proxy_fb,
        is_blocking_failure as _fa_is_blk,
    )

    async def _fa_get(creds):
        c = FAClient(username=fa_username, cookie_a=cookie_a, cookie_b=cookie_b, **creds)
        try:
            return await c.get_submission_detail(int(submission_id))
        finally:
            await c.close()

    try:
        detail = await _fa_get(_fa_proxy_kwargs(settings, "fa"))
    except Exception as e:
        if _fa_is_blk(e):
            creds = _fa_proxy_fb(settings, "fa")
            if creds:
                logger.warning("FA import direct call failed (%s); retrying via CF Worker proxy", e)
                try:
                    detail = await _fa_get(creds)
                except Exception as e2:
                    raise RuntimeError(f"Could not fetch FA submission {submission_id} (proxy retry also failed): {e2}")
            else:
                raise RuntimeError(f"Could not fetch FA submission {submission_id}: {e}")
        else:
            raise RuntimeError(f"Could not fetch FA submission {submission_id}: {e}")

    title = detail.get("title", f"FA_{submission_id}")
    author = detail.get("username", "")
    description = detail.get("description", "")
    tags = detail.get("keywords", [])
    fa_rating = detail.get("rating", "").lower()
    if "adult" in fa_rating:
        rating = "explicit"
    elif "mature" in fa_rating:
        rating = "mature"
    else:
        rating = "general"

    download_url = detail.get("download_url", "")
    cover_url = detail.get("thumbnail_url", "")
    url = detail.get("link", f"https://www.furaffinity.net/view/{submission_id}/")

    content = ""
    if download_url:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as dl:
                file_resp = await dl.get(download_url)
                file_resp.raise_for_status()
                if download_url.lower().endswith(".txt"):
                    content = file_resp.text
                elif download_url.lower().endswith(".pdf"):
                    content = f"(PDF file downloaded — manual conversion needed)\n\nDescription:\n{description}"
                    logger.info("FA import %s: PDF file — saving raw, needs manual conversion", submission_id)
                else:
                    content = file_resp.text
        except Exception as e:
            logger.warning("FA import %s: file download failed: %s", submission_id, e)

    if not content:
        content = description or "(No content available)"
        logger.warning("FA import %s: no downloadable file, using description", submission_id)

    folder_name = _sanitize_folder_name(title)
    story_name = _create_story_folder(
        name=folder_name,
        title=title,
        author=author,
        description=description,
        tags=tags,
        rating=rating,
        content=content,
        content_format="text",
        cover_url=cover_url,
        platform="fa",
        submission_id=str(submission_id),
        source_url=url,
    )

    return {"story_name": story_name, "title": title}


# ---------------------------------------------------------------------------
# OTW Archive (AO3 / SqW) — shared work-page parsing
# ---------------------------------------------------------------------------

def _parse_otw_work_page(html_text: str) -> dict:
    """Pull title/author/summary/rating/tags/chapters out of an OTW work page.

    Both AO3 and SqW run the same Rails app, so the markup structure is
    identical. Selector-based parsing kept simple — falls back to empty
    strings when a field isn't found rather than failing the whole import.
    Chapter splitting respects ``?view_full_work=true`` mode where every
    chapter renders inside ``<div id="chapter-N" class="chapter">``.
    """
    out = {
        "title": "",
        "author": "",
        "summary": "",
        "rating": "general",
        "tags": [],
        "chapters": [],  # list of dict(title=..., html=...)
    }

    title_m = re.search(r'<h2[^>]*class="title heading"[^>]*>(.*?)</h2>', html_text, re.DOTALL)
    if title_m:
        out["title"] = re.sub(r'<[^>]+>', '', title_m.group(1)).strip()

    author_m = re.search(r'<h3[^>]*class="byline heading"[^>]*>(.*?)</h3>', html_text, re.DOTALL)
    if author_m:
        out["author"] = re.sub(r'<[^>]+>', '', author_m.group(1)).strip()

    summary_m = re.search(r'<div[^>]*class="summary module"[^>]*>(.*?)</div>\s*</div>', html_text, re.DOTALL)
    if summary_m:
        block = summary_m.group(1)
        ub = re.search(r'<blockquote[^>]*>(.*?)</blockquote>', block, re.DOTALL)
        if ub:
            out["summary"] = _html_to_markdown(ub.group(1))

    rating_m = re.search(r'<dd[^>]*class="rating tags"[^>]*>(.*?)</dd>', html_text, re.DOTALL)
    if rating_m:
        rtxt = re.sub(r'<[^>]+>', ' ', rating_m.group(1)).strip().lower()
        if "explicit" in rtxt:
            out["rating"] = "explicit"
        elif "mature" in rtxt:
            out["rating"] = "mature"
        elif "teen" in rtxt:
            out["rating"] = "teen"
        elif "general" in rtxt:
            out["rating"] = "general"

    # Freeform tags — only the actual <a class="tag"> links count;
    # OTW emits "Show additional tags" / "Hide additional tags"
    # toggles inside the same <dd> that we don't want to capture.
    free_m = re.search(r'<dd[^>]*class="freeform tags"[^>]*>(.*?)</dd>', html_text, re.DOTALL)
    if free_m:
        out["tags"] = re.findall(r'<a[^>]*class="tag"[^>]*>([^<]+)</a>', free_m.group(1))

    # Chapters — when fetched via ?view_full_work=true OTW renders each
    # chapter inside <div id="chapter-N" class="chapter">.
    chapter_blocks = re.findall(
        r'<div[^>]*id="chapter-\d+"[^>]*class="[^"]*chapter[^"]*"[^>]*>(.*?)</div>\s*(?=<div[^>]*id="chapter-|<div[^>]*id="work_endnotes|$)',
        html_text,
        re.DOTALL,
    )
    if chapter_blocks:
        for blk in chapter_blocks:
            ch_title_m = re.search(r'<h3[^>]*class="title"[^>]*>(.*?)</h3>', blk, re.DOTALL)
            ch_title = re.sub(r'<[^>]+>', '', ch_title_m.group(1)).strip() if ch_title_m else ""
            body_m = re.search(
                r'<div[^>]*class="[^"]*userstuff[^"]*"[^>]*>(.*?)</div>\s*(?=<div|$)',
                blk,
                re.DOTALL,
            )
            ch_body = body_m.group(1) if body_m else blk
            out["chapters"].append({"title": ch_title, "html": ch_body})
    else:
        # Single-chapter work — content lives in a top-level
        # <div class="userstuff">. OTW also wraps it in
        # <div id="chapters"> but the inner div is the canonical body.
        body_m = re.search(
            r'<div[^>]*class="[^"]*userstuff[^"]*"[^>]*>(.*?)</div>\s*<!--/content-->',
            html_text,
            re.DOTALL,
        )
        if not body_m:
            # Fallback — grab the userstuff div even without the closing marker.
            body_m = re.search(
                r'<div[^>]*class="[^"]*userstuff[^"]*"[^>]*>(.*?)(?=<div\s+id="work_endnotes|<div\s+id="series-nav|<div\s+id="feedback|</div>\s*</div>\s*<div\s+id=)',
                html_text,
                re.DOTALL,
            )
        if body_m:
            out["chapters"] = [{"title": "", "html": body_m.group(1)}]

    return out


async def _fetch_ao3_work(client, submission_id: str, ao3_username: str) -> tuple[str, bool]:
    """Fetch the AO3 work HTML using *client*. Returns (html, is_draft).

    Raises RuntimeError on auth failure, missing-content, or wrong-owner.
    The fallback wrapper catches block-like RuntimeErrors and retries
    with a proxy-equipped client.
    """
    if not await client.ensure_logged_in():
        raise RuntimeError(
            "Could not log in to AO3 (likely rate-limited — try again in 10 min)"
        )
    public_url = f"https://archiveofourown.org/works/{submission_id}?view_full_work=true&view_adult=true"
    preview_url = f"https://archiveofourown.org/works/{submission_id}/preview?view_full_work=true&view_adult=true"
    is_draft = False
    resp = await client._http.get(public_url, follow_redirects=True)
    if resp.status_code == 404:
        is_draft = True
        resp = await client._http.get(preview_url, follow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(f"AO3 returned {resp.status_code} for work {submission_id}")
    if 'class="title heading"' not in resp.text or 'userstuff' not in resp.text:
        if is_draft:
            raise RuntimeError(
                f"AO3 work {submission_id} appears to be a draft, but the configured "
                f"AO3 account ('{ao3_username}') doesn't own it. Drafts are owner-only — "
                f"check that ao3_username in Settings matches the draft's author."
            )
        raise RuntimeError(f"AO3 work {submission_id}: response did not contain a parseable work page")
    return resp.text, is_draft


async def import_from_ao3(submission_id: str) -> dict:
    """Download an AO3 work and create a local story folder.

    Reuses the persistent AO3Client singleton from the poller module so
    we share session cookies and avoid hitting AO3's per-IP login rate
    limiter every time someone imports. (Each fresh AO3Client instance
    has _logged_in=False and triggers a full POST /users/login round
    trip; AO3 throttles those aggressively, returning 429 "Retry later"
    for 5–10 minutes after the first hit.) Fetches the work via
    ``?view_full_work=true`` so all chapters arrive in one response,
    then converts each chapter's userstuff block to markdown and
    concatenates them into MASTER.md.
    """
    existing = _find_existing_import("ao3", submission_id)
    if existing:
        return _existing_import_response(existing, "ao3", submission_id)

    from polling.ao3_poller import _get_or_create_client as _get_ao3_client

    settings = config.get_settings()
    ao3_username = settings.get("ao3_username", "")
    ao3_password = settings.get("ao3_password", "")
    ao3_cookie = settings.get("ao3_session_cookie", "")

    # Either a username + password OR a pasted session cookie is enough.
    # Cookie mode lets the user skip the rate-limited /users/login form
    # by pasting `_otwarchive_session` from their already-authenticated
    # browser; no username is strictly required (the importer will use
    # `ao3_target_user` for owner-of-draft sanity checks instead).
    if not ao3_cookie and (not ao3_username or not ao3_password):
        raise RuntimeError("AO3 credentials not configured — set up in Settings")

    # AO3 is in PROXY_REQUIRED — the singleton already runs through the
    # CF Worker when one is configured. Fallback wrapper is included for
    # symmetry with the optional platforms; in practice it'd only fire if
    # the worker itself is having a bad day.
    from polling.cf_proxy import (
        is_blocking_failure as _is_blk,
        proxy_kwargs_fallback as _proxy_fb,
    )
    from clients.ao3.client import AO3Client

    direct_client = _get_ao3_client(settings)
    try:
        html_text, is_draft = await _fetch_ao3_work(direct_client, submission_id, ao3_username or settings.get("ao3_target_user", ""))
    except Exception as e:
        if not _is_blk(e):
            raise
        creds = _proxy_fb(settings, "ao3")
        if not creds:
            raise
        logger.warning("AO3 import direct call failed (%s); retrying via CF Worker proxy", e)
        proxy_client = AO3Client(
            username=ao3_username,
            password=ao3_password,
            target_user=settings.get("ao3_target_user", "") or ao3_username,
            session_cookie=settings.get("ao3_session_cookie", ""),
            **creds,
        )
        try:
            html_text, is_draft = await _fetch_ao3_work(proxy_client, submission_id, ao3_username or settings.get("ao3_target_user", ""))
        finally:
            await proxy_client.close()
    parsed = _parse_otw_work_page(html_text)

    title = parsed["title"] or f"AO3_{submission_id}"
    author = parsed["author"] or ao3_username
    chapters = parsed["chapters"] or [{"title": "", "html": "(No content extracted)"}]

    # Build MASTER.md content — one chapter per heading.
    parts = []
    for i, ch in enumerate(chapters, 1):
        head = ch["title"] or f"Chapter {i}"
        parts.append(f"# {head}\n\n{_html_to_markdown(ch['html'])}")
    content = ("\n\n---\n\n".join(parts)) if parts else "(No content)"

    folder_name = _sanitize_folder_name(title)
    story_name = _create_story_folder(
        name=folder_name,
        title=title,
        author=author,
        description=parsed["summary"],
        tags=parsed["tags"],
        rating=parsed["rating"],
        content=content,
        content_format="text",  # already converted to markdown above
        cover_url="",
        platform="ao3",
        submission_id=str(submission_id),
        source_url=f"https://archiveofourown.org/works/{submission_id}",
    )
    return {
        "story_name": story_name,
        "title": title,
        "chapter_count": len(chapters),
        "is_draft": is_draft,
    }


async def _fetch_sqw_work(client, submission_id: str) -> tuple[str, bool]:
    """Fetch the SqW work HTML using *client*. Returns (html, is_draft).

    Raises RuntimeError on auth failure, missing-content, or wrong-owner.
    The fallback wrapper catches block-like RuntimeErrors and retries
    with a proxy-equipped client.
    """
    if not await client.ensure_logged_in():
        raise RuntimeError("Could not log in to SqW")
    public_url = f"https://squidgeworld.org/works/{submission_id}?view_full_work=true&view_adult=true"
    preview_url = f"https://squidgeworld.org/works/{submission_id}/preview?view_full_work=true&view_adult=true"
    is_draft = False
    html_text = await client._get_page(public_url)
    looks_published = bool(html_text) and (
        'class="title heading"' in html_text and 'userstuff' in html_text
    )
    if not looks_published:
        is_draft = True
        html_text = await client._get_page(preview_url)
    if not html_text:
        raise RuntimeError(f"SqW returned no body for work {submission_id}")
    if 'class="title heading"' not in html_text or 'userstuff' not in html_text:
        if is_draft:
            raise RuntimeError(
                f"SqW work {submission_id} appears to be a draft, but the configured "
                f"SqW account doesn't own it. Drafts are owner-only — "
                f"check that sqw_username in Settings matches the draft's author."
            )
        raise RuntimeError(f"SqW work {submission_id}: response did not contain a parseable work page")
    return html_text, is_draft


async def import_from_squidgeworld(submission_id: str) -> dict:
    """Download a SqW work — same OTW Rails layout as AO3, different host.

    Reuses the persistent SquidgeWorldClient singleton from the poller
    module so we share Anubis tokens + session cookies across import
    calls (and across imports + poll cycles). Re-solving the Anubis
    proof-of-work challenge for every import would be wasteful and
    could trip the same kind of rate limiter that bites AO3 logins.
    """
    existing = _find_existing_import("sqw", submission_id)
    if existing:
        return _existing_import_response(existing, "sqw", submission_id)

    from polling.sqw_poller import _get_or_create_client as _get_sqw_client

    settings = config.get_settings()
    sqw_username = settings.get("sqw_username", "")
    sqw_password = settings.get("sqw_password", "")

    if not sqw_username or not sqw_password:
        raise RuntimeError("SqW credentials not configured — set up in Settings")

    # Direct attempt via the shared singleton. On a block-like failure
    # (Anubis stuck, Cloudflare 403, persistent timeouts) AND the
    # sqw_use_cf_proxy fallback toggle is on, retry once through a
    # fresh proxy-equipped client.
    from polling.cf_proxy import (
        is_blocking_failure as _is_blk,
        proxy_kwargs_fallback as _proxy_fb,
    )
    from clients.sqw.client import SquidgeWorldClient

    direct_client = _get_sqw_client(settings)
    try:
        html_text, is_draft = await _fetch_sqw_work(direct_client, submission_id)
    except Exception as e:
        if not _is_blk(e):
            raise
        creds = _proxy_fb(settings, "sqw")
        if not creds:
            raise
        logger.warning("SqW import direct call failed (%s); retrying via CF Worker proxy", e)
        proxy_client = SquidgeWorldClient(
            username=sqw_username,
            password=sqw_password,
            target_user=settings.get("sqw_target_user", "") or sqw_username,
            **creds,
        )
        try:
            html_text, is_draft = await _fetch_sqw_work(proxy_client, submission_id)
        finally:
            await proxy_client.close()
    parsed = _parse_otw_work_page(html_text)

    title = parsed["title"] or f"SqW_{submission_id}"
    author = parsed["author"] or sqw_username
    chapters = parsed["chapters"] or [{"title": "", "html": "(No content extracted)"}]

    parts = []
    for i, ch in enumerate(chapters, 1):
        head = ch["title"] or f"Chapter {i}"
        parts.append(f"# {head}\n\n{_html_to_markdown(ch['html'])}")
    content = ("\n\n---\n\n".join(parts)) if parts else "(No content)"

    folder_name = _sanitize_folder_name(title)
    story_name = _create_story_folder(
        name=folder_name,
        title=title,
        author=author,
        description=parsed["summary"],
        tags=parsed["tags"],
        rating=parsed["rating"],
        content=content,
        content_format="text",
        cover_url="",
        platform="sqw",
        submission_id=str(submission_id),
        source_url=f"https://squidgeworld.org/works/{submission_id}",
    )
    return {
        "story_name": story_name,
        "title": title,
        "chapter_count": len(chapters),
        "is_draft": is_draft,
    }
