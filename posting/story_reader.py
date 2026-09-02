"""Story archive reader for the posting module.

Reads from the m_x/Archives/Complete_Stories/ directory structure to build
StoryUploadPackage objects for each platform. Handles:
  - split_manifest.json parsing (chapter structure)
  - tags_upload.txt parsing (per-platform tag lists)
  - Format file resolution (BBCode → IB, SoFurry HTML → SF, etc.)
  - Description/summary extraction
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import config
from posting.platforms.base import StoryUploadPackage

logger = logging.getLogger(__name__)

# Platform → preferred format file patterns.
# Each entry is (subdirectory, glob pattern, file_type label).
# Checked in order; first match wins.
PLATFORM_FORMAT_MAP: dict[str, list[tuple[str, str, str]]] = {
    "ib": [
        ("Chapters/BBCode", "*.txt", "bbcode"),
        ("BBCode", "*_bbcode.txt", "bbcode"),
    ],
    "fa": [
        ("PDF", "*.pdf", "pdf"),
        ("Chapters/PDF", "*.pdf", "pdf"),
    ],
    "ws": [
        ("Chapters/BBCode", "*.txt", "bbcode"),
        ("BBCode", "*_bbcode.txt", "bbcode"),
        ("Markdown", "MASTER.md", "markdown"),
    ],
    "sf": [
        ("HTML", "*_SoFurry.html", "html"),       # canonical SF body HTML (p.text-center + strong)
        ("Chapters/SoFurry_HTML", "*.html", "html"),
    ],
    "sqw": [
        ("SquidgeWorld", "*.html", "html"),
        ("Chapters/SoFurry_HTML", "*.html", "html"),
    ],
    "ao3": [
        # AO3 is an OTW Archive site like SquidgeWorld — same chapter
        # markers, warning-glyph, semantic anchors. SqW per-chapter HTML
        # is the right shape. SoFurry HTML is the fallback for archives
        # that pre-date SqW output.
        ("SquidgeWorld", "*.html", "html"),
        ("HTML", "*_SoFurry.html", "html"),
        ("Chapters/SoFurry_HTML", "*.html", "html"),
    ],
    "da": [
        ("Markdown", "MASTER.md", "markdown"),  # DA OAuth accepts plain text
        ("Chapters/Markdown", "*.md", "markdown"),
    ],
    "ik": [],  # Itaku: images uploaded separately, text posts use description
    "bsky": [],  # Bluesky uses text from description, no file upload
}


@dataclass
class StoryInfo:
    """Parsed story metadata from the archive."""
    name: str
    path: Path
    total_chapters: int
    total_words: int
    author: str
    chapters: list[ChapterInfo]
    description: str                                  # short blurb (STORY DESCRIPTION)
    tags_by_platform: dict[str, list[str]]          # platform → tag list
    chapter_tags_by_platform: dict[int, dict[str, list[str]]]  # chapter_index → platform → tags
    chapter_descriptions: dict[int, str]             # chapter_index → description
    title: str = ""                                   # display title from story.json (falls back to name.replace('_', ' '))
    summary: str = ""                                 # detailed summary (SUMMARY section, for SQW/AO3)
    descriptions: dict[str, str] = None               # per-platform overrides: short, announcement
    thumbnail_path: str | None = None                 # full-series thumbnail
    chapter_thumbnails: dict[int, str] = None         # chapter_index → thumbnail path
    # OTW Archive / SQW / AO3 metadata fields (used by SquidgeWorld poster)
    rating: str = ""                                  # explicit, mature, teen, general
    fandom: str = ""                                  # e.g. "Original Work", "Kung Fu Panda"
    category: str = ""                                # legacy single category (e.g. "M/M")
    categories: list[str] = None                      # list of categories
    warnings: list[str] = None                        # list of canonical archive warnings
    characters: list[str] = None                      # list of character tags
    relationships: list[str] = None                   # list of relationship tags
    work_skin_path: Path | None = None                # path to Work_Skin.css if present
    series: str = ""                                  # series name (gap-wave-5 §2), display grouping
    series_index: int = 0                             # position within the series (1-based; 0 = unset)

    def __post_init__(self):
        if self.descriptions is None:
            self.descriptions = {}
        if self.chapter_thumbnails is None:
            self.chapter_thumbnails = {}
        if self.categories is None:
            self.categories = [self.category] if self.category else []
        if self.warnings is None:
            self.warnings = []
        if self.characters is None:
            self.characters = []
        if self.relationships is None:
            self.relationships = []


@dataclass
class ChapterInfo:
    """Single chapter from split_manifest.json."""
    index: int
    title: str
    filename: str
    word_count: int
    files: dict[str, str]   # format_key → relative path


def get_archive_path() -> Path:
    """Get the story archive root, configurable via settings.

    Resolution order:
      1. posting_story_archive_path setting (explicit override)
      2. /app/story-archive (Docker bind mount on GCP server)
      3. ../m_x/Archives/Complete_Stories/ (maintainer's dev checkout — ONLY if it
         actually exists beside the source; never present in a shipped install)
      4. <user data>/story-archive — the generic per-user default, created on first
         use so a fresh install has a real empty archive to sync/drop stories into,
         instead of resolving to a dev path that isn't there.
    """
    settings = config.get_settings()
    custom = settings.get("posting_story_archive_path", "")
    if custom and os.path.isdir(custom):
        return Path(custom)
    # Docker: bind-mounted at /app/story-archive
    docker_mount = Path("/app/story-archive")
    if docker_mount.is_dir() and any(docker_mount.iterdir()):
        return docker_mount
    # Maintainer's dev checkout only — the m_x story pipeline, if it happens to sit
    # beside the source. Guarded by is_dir() so it's skipped on every real install.
    dev = Path(config.resource_path(".")).parent / "m_x" / "Archives" / "Complete_Stories"
    if dev.is_dir():
        return dev
    # Generic per-user default (mirrors the Docker /app/story-archive convention).
    default = config.APPDATA_DIR / "story-archive"
    default.mkdir(parents=True, exist_ok=True)
    return default


def list_stories() -> list[dict]:
    """List all available stories in the archive.

    Returns a list of dicts with story metadata from story.json (if available),
    plus file inventory and format availability.
    """
    archive = get_archive_path()
    if not archive.is_dir():
        logger.warning("Story archive not found at %s", archive)
        return []
    stories = []
    for entry in sorted(archive.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name == "Reference_Guides":
            continue
        # Check if this is a direct story folder
        if (entry / "Markdown" / "MASTER.md").is_file() or (entry / "Tags").is_dir() or (entry / "story.json").is_file():
            stories.append(_story_entry(entry))
        else:
            # Check for sub-stories (e.g. My_Story/Alt_Version/)
            for sub in sorted(entry.iterdir()):
                if sub.is_dir() and (
                    (sub / "Markdown" / "MASTER.md").is_file() or (sub / "story.json").is_file()
                ):
                    stories.append(_story_entry(sub, parent_name=entry.name))
    return stories


def _story_entry(path: Path, parent_name: str = "") -> dict:
    """Build a story list entry from a folder path.

    If story.json exists, uses it for rich metadata. Otherwise falls back
    to basic folder inspection.
    """
    name = f"{parent_name}/{path.name}" if parent_name else path.name
    story_json = path / "story.json"

    entry = {
        "name": name,
        "path": str(path),
        "has_manifest": (path / "Chapters" / "split_manifest.json").is_file(),
        "has_tags": (path / "Tags" / "tags_upload.txt").is_file(),
        "has_master": (path / "Markdown" / "MASTER.md").is_file(),
        "has_story_json": story_json.is_file(),
    }

    if story_json.is_file():
        try:
            data = json.loads(story_json.read_text(encoding="utf-8"))
            entry["title"] = data.get("title", name.replace("_", " "))
            entry["author"] = data.get("author", "")
            entry["description"] = data.get("description", "")
            entry["word_count"] = data.get("word_count", 0)
            entry["chapters"] = data.get("chapters", 0)
            entry["rating"] = data.get("rating", "")
            entry["category"] = data.get("category", "")
            entry["formats"] = data.get("formats", {})
            entry["platforms"] = list(data.get("platforms", {}).keys())
            entry["images"] = data.get("images", {})
            entry["warnings"] = data.get("warnings", [])
            entry["series"] = data.get("series", "")               # gap-wave-5 §2
            entry["series_index"] = int(data.get("series_index", 0) or 0)
        except Exception as e:
            logger.warning("Failed to read story.json for %s: %s", name, e)

    # Auto-detect cover when story.json doesn't declare one. Many stories
    # have a `<name>_thumbnail_full_series.png` in the folder root but no
    # explicit `images.cover` entry; the listing should still show them.
    images = entry.setdefault("images", {})
    if not images.get("cover"):
        detected = detect_cover_relative(path)
        if detected:
            images["cover"] = detected

    return entry


def load_story(story_name: str) -> StoryInfo:
    """Load full story metadata from the archive.

    Reads from story.json if available (preferred), falling back to
    tags_upload.txt + split_manifest.json parsing.

    Security: ``story_name`` arrives from the request URL via FastAPI's
    ``:path`` converter, which means ``../`` segments are passed through
    unchanged. Resolve and re-anchor against the archive root so a
    crafted path can't escape into the host filesystem. Mirrors the
    same check in routes/editor_api.py::_resolve_story_dir.
    """
    archive = get_archive_path().resolve()
    candidate = (archive / story_name).resolve()
    try:
        candidate.relative_to(archive)
    except ValueError:
        raise FileNotFoundError(
            f"Story folder not found: {story_name}"
        ) from None
    if not candidate.is_dir():
        raise FileNotFoundError(f"Story folder not found: {candidate}")
    story_path = candidate

    story_json_path = story_path / "story.json"
    if story_json_path.is_file():
        return _load_from_story_json(story_name, story_path, story_json_path)
    return _load_from_legacy(story_name, story_path)


# Image file extensions accepted as covers/thumbnails. Shared by the auto-detect
# helper and the /api/posting/image route's allowlist.
COVER_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# Glob patterns checked in order. First match wins. Mirrors the convention
# used across the story archive: `<story>_thumbnail_full_series.png` is the
# canonical name, but stories vary.
_COVER_PATTERNS = (
    "*_thumbnail_full_series.*",
    "*_thumbnail.*",
    "*_cover.*",
    "thumbnail.*",
    "cover.*",
)


def detect_cover_relative(story_path: Path) -> str:
    """Find a cover image in the story root and return its name relative to *story_path*.

    Returns an empty string if no cover is found. Used by both the listing
    endpoint (to populate ``images.cover`` when story.json omits it) and
    ``_load_from_story_json`` (which currently does its own glob — kept in sync
    via this helper).
    """
    if not story_path.is_dir():
        return ""
    for pattern in _COVER_PATTERNS:
        for f in story_path.glob(pattern):
            if f.is_file() and f.suffix.lower() in COVER_EXTENSIONS:
                return f.name
    return ""


# Map a `formats` key in story.json to the directory + glob pattern that
# represents that format on disk. Used by get_format_files() to enrich the
# detail page with file size + last-modified info per format key. Distinct
# from PLATFORM_FORMAT_MAP because that one is platform-keyed (resolves the
# file the *poster* uploads), while this is format-key-keyed (the user-facing
# "Available Formats" badge list).
_FORMAT_KEY_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "bbcode": [("BBCode", "*_bbcode.txt")],
    "chapter_bbcode": [("Chapters/BBCode", "*.txt")],
    "html": [("HTML", "*_SoFurry.html"), ("HTML", "*.html")],
    "sofurry_html": [("HTML", "*_SoFurry.html"), ("Chapters/SoFurry_HTML", "*.html")],
    "squidgeworld": [("SquidgeWorld", "*.html")],
    "markdown": [("Markdown", "MASTER.md"), ("Markdown", "*.md")],
    "pdf": [("PDF", "*.pdf"), ("Chapters/PDF", "*.pdf")],
    "styled_html": [("HTML", "*_Styled.html"), ("Chapters/Styled_HTML", "*.html")],
    "epub": [("EPUB", "*.epub")],
}


def get_format_files(story_path: Path, formats: dict) -> dict:
    """Resolve declared format keys to file metadata for the detail page.

    Returns a dict keyed by format name where each value is either:
        {"available": False}                                   — no files matched
        {"available": True, "files": [{"path": str (relative
         to story_path), "size": int, "modified": str (ISO)}]}

    The relative paths are exactly what the /api/posting/file endpoint
    expects in its `file` query param. Multi-file formats (chapter_bbcode,
    squidgeworld, etc.) return all matching files sorted by name. Stories
    using the single-bulk-file convention (most of the archive) return a
    single-element list.
    """
    result = {}
    if not story_path.is_dir():
        return {k: {"available": False} for k in formats}

    for fmt_key, declared in formats.items():
        if not declared:
            result[fmt_key] = {"available": False}
            continue
        patterns = _FORMAT_KEY_PATTERNS.get(fmt_key, [])
        if not patterns:
            # Unknown format key — declared in story.json but we have no
            # pattern for it. Surface as available-but-no-files so the
            # frontend can at least show the badge.
            result[fmt_key] = {"available": True, "files": []}
            continue

        files: list[dict] = []
        seen: set[Path] = set()
        for subdir, pattern in patterns:
            search_dir = story_path / subdir
            if not search_dir.is_dir():
                continue
            for f in sorted(search_dir.glob(pattern)):
                if not f.is_file() or f in seen:
                    continue
                seen.add(f)
                try:
                    stat = f.stat()
                    files.append({
                        "path": str(f.relative_to(story_path)).replace("\\", "/"),
                        "size": stat.st_size,
                        "modified": _iso_mtime(stat.st_mtime),
                    })
                except OSError:
                    continue

        result[fmt_key] = {"available": bool(files), "files": files}

    return result


def _iso_mtime(mtime: float) -> str:
    """Convert a stat mtime float to a UTC ISO 8601 string."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _load_from_story_json(story_name: str, story_path: Path, json_path: Path) -> StoryInfo:
    """Load story metadata from story.json."""
    data = json.loads(json_path.read_text(encoding="utf-8"))

    # Build chapter list from story.json (merge with manifest for file paths).
    # split_manifest.json's chapter entries use ``number``, ``words``, and a
    # nested ``files`` dict (no top-level ``index``/``filename``/``word_count``
    # keys). Pre-2.18.19 the lookup used ``ch.get("index", 0)`` which collapsed
    # every entry to key 0, and ``manifest_ch.get("filename", "")`` always
    # returned ``""`` — feeding the empty-string match in ``_resolve_format_file``
    # that broke per-chapter posting.
    chapters = []
    manifest_chapters = {}
    manifest_path = story_path / "Chapters" / "split_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for ch in manifest.get("chapters", []):
            manifest_chapters[ch.get("number", ch.get("index", 0))] = ch

    for ch_data in data.get("chapter_info", []):
        idx = ch_data.get("index", 0)
        manifest_ch = manifest_chapters.get(idx, {})
        # Derive ``filename`` from any format path in ``files`` — they all
        # share the same stem (e.g. ``Chapter_1_Tip-Off``), which is what
        # the resolver's substring match uses.
        ch_filename = manifest_ch.get("filename", "")
        if not ch_filename:
            for path_str in (manifest_ch.get("files") or {}).values():
                if isinstance(path_str, str) and path_str:
                    ch_filename = Path(path_str).stem
                    break
        chapters.append(ChapterInfo(
            index=idx,
            title=ch_data.get("title", ""),
            filename=ch_filename,
            word_count=ch_data.get("words", manifest_ch.get("words", manifest_ch.get("word_count", 0))),
            files=manifest_ch.get("files", {}),
        ))

    # Build tags_by_platform from story.json tags. The plat_map covers
    # every poster registered in posting.manager — when a story.json key
    # matches one of these, it's stored under the short ID the package
    # builder uses. Anything else passes through unchanged.
    plat_map = {
        "inkbunny": "ib", "furaffinity": "fa", "weasyl": "ws",
        "sofurry": "sf", "squidgeworld": "sqw", "wattpad": "wp",
        "deviantart": "da", "itaku": "ik", "bluesky": "bsky",
        # ao3 already short
    }
    # Every poster ID, used to backfill missing entries from "default".
    all_poster_ids = ["ib", "fa", "ws", "sf", "sqw", "ao3", "da", "ik", "bsky", "wp"]

    tags = data.get("tags", {})
    tags_by_platform = {}
    for plat_key, tag_list in tags.items():
        plat_id = plat_map.get(plat_key, plat_key)
        tags_by_platform[plat_id] = tag_list
    # Cascade default → any platform that wasn't given an explicit list.
    # This mirrors the editor UI's "Default tab cascades to all platforms"
    # behaviour and the legacy tags_upload.txt parser.
    if "default" in tags_by_platform:
        for pid in all_poster_ids:
            if pid not in tags_by_platform:
                tags_by_platform[pid] = list(tags_by_platform["default"])

    # Chapter descriptions and per-chapter tags
    chapter_descriptions = {}
    chapter_tags: dict[int, dict[str, list[str]]] = {}
    for ch_data in data.get("chapter_info", []):
        ch_idx = ch_data.get("index", 0)
        if ch_data.get("description"):
            chapter_descriptions[ch_idx] = ch_data["description"]
        # Per-chapter tags (keyed by platform ID)
        ch_tag_data = ch_data.get("tags", {})
        if ch_tag_data:
            ch_plat_tags = {}
            for plat_key, tag_list in ch_tag_data.items():
                plat_id = plat_map.get(plat_key, plat_key)
                ch_plat_tags[plat_id] = tag_list
            # Same default cascade at the chapter level.
            if "default" in ch_plat_tags:
                for pid in all_poster_ids:
                    if pid not in ch_plat_tags:
                        ch_plat_tags[pid] = list(ch_plat_tags["default"])
            chapter_tags[ch_idx] = ch_plat_tags

    # Images
    images = data.get("images", {})
    thumbnail_path = None
    chapter_thumbnails = {}
    if images.get("cover"):
        thumbnail_path = str(story_path / images["cover"])
    else:
        # Auto-detect thumbnail file in story root using common naming patterns.
        # Shared with the listing endpoint via detect_cover_relative().
        detected = detect_cover_relative(story_path)
        if detected:
            thumbnail_path = str(story_path / detected)
    for ch_idx, ch_path in images.get("chapter_thumbnails", {}).items():
        chapter_thumbnails[int(ch_idx)] = str(story_path / ch_path)

    # OTW Archive metadata fields
    raw_warnings = data.get("warnings", [])
    if isinstance(raw_warnings, str):
        raw_warnings = [raw_warnings]
    raw_characters = data.get("characters", [])
    if isinstance(raw_characters, str):
        raw_characters = [raw_characters]
    raw_relationships = data.get("relationships", [])
    if isinstance(raw_relationships, str):
        raw_relationships = [raw_relationships]
    raw_categories = data.get("categories", [])
    if not raw_categories and data.get("category"):
        raw_categories = [data["category"]]
    if isinstance(raw_categories, str):
        raw_categories = [raw_categories]

    # Detect Work_Skin.css if present
    work_skin = story_path / "SquidgeWorld" / "Work_Skin.css"
    work_skin_path = work_skin if work_skin.is_file() else None

    # `total_chapters` must always match the actual `chapter_info` list
    # length — consumers (publish_check, posting.manager, AO3/SQW posters)
    # iterate `range(1, total_chapters + 1)` and index `story.chapters[i-1]`.
    # `chapters` in story.json can lag (e.g. wizard-created stories and
    # single-piece works save `chapters: N` with an empty `chapter_info`,
    # which used to raise IndexError during publish-check).
    return StoryInfo(
        name=story_name,
        path=story_path,
        total_chapters=len(chapters),
        total_words=data.get("word_count", 0),
        author=data.get("author", config.get_settings().get("default_author", "")),
        chapters=chapters,
        description=data.get("description", ""),
        tags_by_platform=tags_by_platform,
        chapter_tags_by_platform=chapter_tags,
        chapter_descriptions=chapter_descriptions,
        title=data.get("title", ""),
        summary=data.get("summary", ""),
        descriptions=data.get("descriptions", {}),
        thumbnail_path=thumbnail_path,
        chapter_thumbnails=chapter_thumbnails,
        rating=data.get("rating", ""),
        fandom=data.get("fandom", "Original Work"),
        category=data.get("category", ""),
        categories=raw_categories,
        warnings=raw_warnings,
        characters=raw_characters,
        relationships=raw_relationships,
        work_skin_path=work_skin_path,
        series=data.get("series", ""),
        series_index=int(data.get("series_index", 0) or 0),
    )


def _load_from_legacy(story_name: str, story_path: Path) -> StoryInfo:
    """Load story metadata from tags_upload.txt + split_manifest.json (fallback)."""
    # Parse manifest
    chapters = []
    total_chapters = 0
    total_words = 0
    author = ""
    manifest_path = story_path / "Chapters" / "split_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        total_chapters = manifest.get("total_chapters", 0)
        total_words = manifest.get("total_words", 0)
        author = manifest.get("author", "")
        for ch in manifest.get("chapters", []):
            chapters.append(ChapterInfo(
                index=ch.get("index", 0),
                title=ch.get("title", ""),
                filename=ch.get("filename", ""),
                word_count=ch.get("word_count", 0),
                files=ch.get("files", {}),
            ))

    # Parse tags_upload.txt
    description = ""
    summary = ""
    tags_by_platform: dict[str, list[str]] = {}
    chapter_tags: dict[int, dict[str, list[str]]] = {}
    chapter_descriptions: dict[int, str] = {}
    tags_path = story_path / "Tags" / "tags_upload.txt"
    if tags_path.is_file():
        tags_text = tags_path.read_text(encoding="utf-8")
        description, summary, tags_by_platform, chapter_tags, chapter_descriptions = _parse_tags_upload(
            tags_text, total_chapters
        )

    # Discover thumbnails
    thumbnail_path = None
    chapter_thumbnails: dict[int, str] = {}
    for f in story_path.iterdir():
        if not f.is_file() or f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif"):
            continue
        fname = f.name.lower()
        if "thumbnail" in fname and ("full" in fname or "series" in fname or "cover" in fname):
            thumbnail_path = str(f)
        elif "thumbnail" in fname:
            part_match = re.search(r'part[_\s]*(\d+)', fname)
            if part_match:
                chapter_thumbnails[int(part_match.group(1))] = str(f)

    return StoryInfo(
        name=story_name,
        path=story_path,
        total_chapters=total_chapters,
        total_words=total_words,
        author=author,
        chapters=chapters,
        description=description,
        summary=summary,
        tags_by_platform=tags_by_platform,
        chapter_tags_by_platform=chapter_tags,
        chapter_descriptions=chapter_descriptions,
        thumbnail_path=thumbnail_path,
        chapter_thumbnails=chapter_thumbnails,
    )


def build_package(
    story: StoryInfo,
    chapter_index: int,
    platform: str,
    title_override: str | None = None,
    description_override: str | None = None,
    tags_override: list[str] | None = None,
    rating_override: str | None = None,
    file_path_override: str | None = None,
) -> StoryUploadPackage:
    """Build a StoryUploadPackage for a specific chapter + platform combination.

    Args:
        story: Loaded story info.
        chapter_index: 0 = full story, 1+ = specific chapter.
        platform: Platform ID ('ib', 'fa', 'ws', 'sf', 'bsky').
        *_override: Override any auto-resolved field.
    """
    # Resolve chapter info
    chapter_title = ""
    word_count = story.total_words
    if chapter_index > 0 and chapter_index <= len(story.chapters):
        ch = story.chapters[chapter_index - 1]
        chapter_title = ch.title
        word_count = ch.word_count

    # Title — prefer story.json title field over folder-name fallback
    base_title = story.title or story.name.replace("_", " ")
    if title_override:
        title = title_override
    elif chapter_index > 0 and chapter_title:
        title = f"{base_title} — {chapter_title}"
    else:
        title = base_title

    # Description — platform-specific selection with per-platform overrides:
    #   SQW/AO3: use detailed summary (SUMMARY section) for work-level, chapter desc for chapters
    #   IB/SF/FA/WS: use descriptions.short if set, else short blurb (STORY DESCRIPTION)
    #   BSKY: use descriptions.announcement if set, else truncated description (300 chars)
    descs = story.descriptions or {}
    if description_override:
        description = description_override
    elif chapter_index > 0 and chapter_index in story.chapter_descriptions:
        description = story.chapter_descriptions[chapter_index]
    elif platform in ("sqw", "ao3") and story.summary:
        description = story.summary
    elif platform == "bsky":
        announcement = descs.get("announcement", "").strip()
        if announcement:
            description = announcement
        else:
            description = (story.description or "")[:300]
    elif platform in ("ib", "sf", "fa", "ws"):
        short = descs.get("short", "").strip()
        description = short if short else story.description
    else:
        description = story.description

    # FA chapter navigation prefix: "Chapter X of N. <description>" or
    # "Part X of N. <description>" depending on what the chapter is called.
    # This is a useful nav signal for readers landing on a per-chapter page.
    # Only applies when we're posting a specific chapter (not a full-story
    # package) and only for FA (the platform that uses per-chapter
    # submissions). Other platforms use single-bulk-file or chaptered works
    # that already display chapter context in their UI.
    #
    # We detect "Part" vs "Chapter" from the chapter title itself so that
    # stories that use "Part 1" / "Part 2" instead of chapters get the
    # matching word, while normal stories use "Chapter".
    if (
        platform == "fa"
        and chapter_index > 0
        and story.total_chapters > 0
        and not description_override
        and description
        and not description.lstrip().lower().startswith(("chapter ", "part "))
    ):
        # Prefix word matches the chapter's own title prefix
        ch_title_lower = (chapter_title or "").strip().lower()
        if ch_title_lower.startswith("part "):
            prefix_word = "Part"
        else:
            prefix_word = "Chapter"
        description = f"{prefix_word} {chapter_index} of {story.total_chapters}. {description.lstrip()}"

    # "Posted via PawPoller" credit line (gap-wave-2 §1) — appended here, the
    # choke point every story posting path flows through (post/edit/update/
    # retry/scheduler). Self-gates on the pawpoller_attribution setting, skips
    # bsky (announcement post, 295-char truncation), never double-appends.
    from posting import attribution
    description = attribution.maybe_append(description, platform)

    # Tags
    if tags_override:
        tags = tags_override
    elif chapter_index > 0 and chapter_index in story.chapter_tags_by_platform:
        ch_tags = story.chapter_tags_by_platform[chapter_index]
        tags = ch_tags.get(platform, ch_tags.get("default", []))
    else:
        tags = story.tags_by_platform.get(platform, [])

    # Rating — default to adult, overridable
    rating = rating_override or "adult"

    # File path
    file_path = file_path_override
    file_type = ""
    if not file_path:
        file_path, file_type = _resolve_format_file(story, chapter_index, platform)

    # Thumbnail: per-chapter thumbnail takes priority over full-series
    thumbnail = None
    if chapter_index > 0 and chapter_index in story.chapter_thumbnails:
        thumbnail = story.chapter_thumbnails[chapter_index]
    elif story.thumbnail_path:
        thumbnail = story.thumbnail_path

    return StoryUploadPackage(
        story_name=story.name,
        chapter_index=chapter_index,
        chapter_title=chapter_title,
        platform=platform,
        title=title,
        description=description,
        tags=tags,
        rating=rating,
        file_path=file_path,
        file_type=file_type,
        word_count=word_count,
        thumbnail_path=thumbnail,
    )


def _resolve_format_file(
    story: StoryInfo, chapter_index: int, platform: str
) -> tuple[str | None, str]:
    """Find the appropriate format file for a platform.

    Returns (absolute_path, file_type) or (None, '') if no file needed/found.
    """
    format_specs = PLATFORM_FORMAT_MAP.get(platform, [])
    if not format_specs:
        return None, ""

    story_path = story.path

    for subdir, pattern, file_type in format_specs:
        search_dir = story_path / subdir
        if not search_dir.is_dir():
            continue

        if chapter_index > 0 and chapter_index <= len(story.chapters):
            # Look for chapter-specific file
            ch = story.chapters[chapter_index - 1]
            ch_filename = ch.filename
            # Try matching by chapter filename prefix. Guard against an
            # empty ``ch_filename`` — without it, ``"" in f.stem`` is True
            # for every file in the directory, and the first format spec
            # (often the full-story bulk file dir like ``PDF/``) wins
            # over the per-chapter dir, returning the whole story for a
            # chapter request. Pre-2.18.19 this is exactly what made
            # publishing chapter 1 to FA upload the full-story PDF.
            if ch_filename:
                for f in sorted(search_dir.iterdir()):
                    if f.is_file() and ch_filename in f.stem:
                        return str(f), file_type
            # Try matching by chapter index
            for f in sorted(search_dir.iterdir()):
                if f.is_file() and f"Chapter_{chapter_index}" in f.name:
                    return str(f), file_type
        else:
            # Full-story file — skip per-chapter subdirs (Chapters/...)
            if "Chapters" in subdir.split("/"):
                continue
            import fnmatch
            for f in sorted(search_dir.iterdir()):
                if f.is_file() and fnmatch.fnmatch(f.name, pattern):
                    return str(f), file_type

    logger.warning(
        "No format file found for %s ch%d on %s", story.name, chapter_index, platform
    )
    return None, ""


def _parse_tags_upload(
    text: str, total_chapters: int
) -> tuple[str, str, dict[str, list[str]], dict[int, dict[str, list[str]]], dict[int, str]]:
    """Parse a tags_upload.txt file.

    Returns:
        (story_description, summary, tags_by_platform, chapter_tags, chapter_descriptions)
    """
    description = ""
    summary = ""
    tags_by_platform: dict[str, list[str]] = {}
    chapter_tags: dict[int, dict[str, list[str]]] = {}
    chapter_descriptions: dict[int, str] = {}

    # Extract detailed summary (SUMMARY section — used by SQW/AO3)
    summary_match = re.search(
        r"^SUMMARY[^:]*:\s*\n(.+?)(?:\nNOTES|\nASSOCIATIONS|\n=====)",
        text, re.MULTILINE | re.DOTALL
    )
    if summary_match:
        summary = summary_match.group(1).strip()

    # Extract short story description (STORY DESCRIPTION — used by IB/SF/etc.)
    desc_match = re.search(
        r"STORY DESCRIPTION:\s*\n(.+?)(?:\n=====|\nPART \d|\nTAGS|\n$)",
        text, re.DOTALL
    )
    if desc_match:
        description = desc_match.group(1).strip()

    # Extract story-level tags sections
    # SoFurry / default tags (the first TAGS section)
    tags_match = re.search(r"^TAGS \(\d+\):\s*\n(.+?)(?:\n\n|\nINKBUNNY)", text, re.MULTILINE | re.DOTALL)
    if tags_match:
        raw = tags_match.group(1).strip()
        tags_by_platform["sf"] = [t.strip() for t in raw.split(",") if t.strip()]
        tags_by_platform["default"] = tags_by_platform["sf"]

    # Inkbunny tags (flatten all categories)
    ib_match = re.search(
        r"INKBUNNY TAGS \(Categorized\):\s*\n(.+?)(?:\n\nWATTPAD|\n\n=====|\nOther Keywords:.*?\n\n)",
        text, re.DOTALL
    )
    if ib_match:
        ib_section = ib_match.group(0)
        ib_tags = []
        for line in ib_section.split("\n"):
            line = line.strip()
            if not line or line.endswith(":") or "INKBUNNY" in line:
                continue
            ib_tags.extend(t.strip() for t in line.split(",") if t.strip())
        if ib_tags:
            tags_by_platform["ib"] = ib_tags

    # Wattpad tags
    wp_match = re.search(r"WATTPAD TAGS.*?:\s*\n(.+?)(?:\n\n|\n=====)", text, re.DOTALL)
    if wp_match:
        raw = wp_match.group(1).strip()
        tags_by_platform["wp"] = raw.split()

    # Weasyl uses same tags as Inkbunny (both accept keyword lists)
    if "ib" in tags_by_platform:
        tags_by_platform["ws"] = tags_by_platform["ib"]

    # FA uses same as default/SF but space-separated
    if "default" in tags_by_platform:
        tags_by_platform["fa"] = tags_by_platform["default"]

    # Bluesky doesn't use tags from the file

    # Extract per-chapter sections
    part_pattern = re.compile(
        r"PART (\d+) OF \d+:.*?\n(.*?)(?=PART \d+ OF \d+:|$)",
        re.DOTALL
    )
    for match in part_pattern.finditer(text):
        ch_idx = int(match.group(1))
        ch_section = match.group(2)

        # Chapter description
        ch_desc_match = re.search(r"DESCRIPTION:\s*\n(.+?)(?:\n\nTAGS|\n\n)", ch_section, re.DOTALL)
        if ch_desc_match:
            chapter_descriptions[ch_idx] = ch_desc_match.group(1).strip()

        # Chapter tags (same format as story-level)
        ch_tags: dict[str, list[str]] = {}
        ch_tags_match = re.search(r"^TAGS \(\d+\):\s*\n(.+?)(?:\n\nINKBUNNY|\n\n)", ch_section, re.MULTILINE | re.DOTALL)
        if ch_tags_match:
            raw = ch_tags_match.group(1).strip()
            ch_tags["sf"] = [t.strip() for t in raw.split(",") if t.strip()]
            ch_tags["default"] = ch_tags["sf"]
            ch_tags["fa"] = ch_tags["sf"]

        # Chapter Inkbunny tags
        ch_ib_match = re.search(r"INKBUNNY TAGS.*?:\s*\n(.+?)(?:\nWATTPAD|\n\n=====|\n\n)", ch_section, re.DOTALL)
        if ch_ib_match:
            ib_tags = []
            for line in ch_ib_match.group(0).split("\n"):
                line = line.strip()
                if not line or line.endswith(":") or "INKBUNNY" in line:
                    continue
                ib_tags.extend(t.strip() for t in line.split(",") if t.strip())
            if ib_tags:
                ch_tags["ib"] = ib_tags
                ch_tags["ws"] = ib_tags

        if ch_tags:
            chapter_tags[ch_idx] = ch_tags

    return description, summary, tags_by_platform, chapter_tags, chapter_descriptions
