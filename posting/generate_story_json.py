"""Generate story.json files for all stories in the archive.

Reads existing data sources (tags_upload.txt, split_manifest.json, folder structure)
and produces a standardised story.json for each story. Can be run repeatedly —
overwrites existing story.json files.

Usage:
    python -m posting.generate_story_json                    # all stories
    python -m posting.generate_story_json Example_Story     # one story
    python -m posting.generate_story_json --dry-run           # preview without writing
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def _default_author() -> str:
    """Return the default author from settings, or empty string if unavailable."""
    try:
        import config
        return config.get_settings().get("default_author", "")
    except Exception:
        return ""


def find_archive() -> Path:
    """Find the story archive directory.

    Resolution order: ``$PAWPOLLER_ARCHIVE_DIR``, the Docker bind-mount target,
    then a sibling ``m_x`` checkout (dev convenience).
    """
    candidates = []
    env_dir = os.environ.get("PAWPOLLER_ARCHIVE_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    candidates += [
        Path("/app/story-archive"),  # Docker bind-mount target
        Path(__file__).resolve().parent.parent.parent / "m_x" / "Archives" / "Complete_Stories",
    ]
    for p in candidates:
        if p.is_dir():
            return p
    raise FileNotFoundError(
        "Story archive not found. Set PAWPOLLER_ARCHIVE_DIR to your archive path."
    )


def generate_story_json(story_path: Path) -> dict:
    """Generate a story.json dict from existing data in a story folder."""

    name = story_path.name
    parent = story_path.parent.name if story_path.parent.name != "Complete_Stories" else ""
    display_name = name.replace("_", " ")
    if parent and parent != "Complete_Stories":
        display_name = f"{parent.replace('_', ' ')} — {display_name}"

    result = {
        "title": display_name,
        "author": _default_author(),
        "description": "",
        "summary": "",
        "rating": "explicit",
        "warnings": [],
        "category": "",
        "fandom": "Original Work",
        "characters": [],
        "relationships": [],
        "word_count": 0,
        "chapters": 0,
        "chapter_info": [],
        "tags": {},
        "images": {},
        "formats": {},
        "platforms": {},
    }

    # ── Read split_manifest.json ──
    manifest_path = story_path / "Chapters" / "split_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result["word_count"] = manifest.get("total_words", 0)
        result["chapters"] = manifest.get("total_chapters", 0)
        result["author"] = manifest.get("author", _default_author())
        for ch in manifest.get("chapters", []):
            result["chapter_info"].append({
                "index": ch.get("index", 0),
                "title": ch.get("title", ""),
                "words": ch.get("word_count", 0),
                "description": "",
            })

    # ── Read tags_upload.txt ──
    tags_path = story_path / "Tags" / "tags_upload.txt"
    if tags_path.is_file():
        tags_text = tags_path.read_text(encoding="utf-8")
        _parse_tags_into(tags_text, result)

    # ── Discover images ──
    for img_dir in [story_path / "Images", story_path]:
        if not img_dir.is_dir():
            continue
        for f in img_dir.iterdir():
            if not f.is_file() or f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif"):
                continue
            fname = f.name.lower()
            rel = str(f.relative_to(story_path)).replace("\\", "/")
            if "cover" in fname or ("thumbnail" in fname and ("full" in fname or "series" in fname)):
                result["images"]["cover"] = rel
            elif "thumbnail" in fname:
                part_match = re.search(r'part[_\s]*(\d+)', fname)
                if part_match:
                    if "chapter_thumbnails" not in result["images"]:
                        result["images"]["chapter_thumbnails"] = {}
                    result["images"]["chapter_thumbnails"][part_match.group(1)] = rel

    # ── Discover available formats ──
    formats = {}
    if (story_path / "BBCode").is_dir() and any((story_path / "BBCode").iterdir()):
        formats["bbcode"] = True
    if (story_path / "HTML").is_dir() and any((story_path / "HTML").iterdir()):
        formats["html"] = True
    if (story_path / "PDF").is_dir() and any((story_path / "PDF").iterdir()):
        formats["pdf"] = True
    if (story_path / "EPUB").is_dir() and any((story_path / "EPUB").iterdir()):
        formats["epub"] = True
    if (story_path / "Markdown" / "MASTER.md").is_file():
        formats["markdown"] = True
    if (story_path / "SquidgeWorld").is_dir() and any((story_path / "SquidgeWorld").iterdir()):
        formats["squidgeworld"] = True
    if (story_path / "Chapters" / "SoFurry_HTML").is_dir():
        formats["sofurry_html"] = True
    if (story_path / "Chapters" / "BBCode").is_dir():
        formats["chapter_bbcode"] = True
    result["formats"] = formats

    # ── Set platform configs ──
    platforms = {}
    if formats.get("bbcode") or formats.get("chapter_bbcode"):
        platforms["inkbunny"] = {"format": "bbcode", "description_field": "story"}
    if formats.get("pdf"):
        platforms["furaffinity"] = {"format": "pdf", "submission_type": "story", "category": "13"}
    if formats.get("sofurry_html"):
        platforms["sofurry"] = {"format": "sofurry_html", "category": 20, "type": 21}
    if formats.get("bbcode") or formats.get("chapter_bbcode"):
        platforms["weasyl"] = {"format": "bbcode"}
    if formats.get("squidgeworld"):
        platforms["squidgeworld"] = {"format": "squidgeworld_html"}
        # Check for work skin
        skin_path = story_path / "SquidgeWorld" / "Work_Skin.css"
        if skin_path.is_file():
            # Try to extract skin name from CSS comments
            css = skin_path.read_text(encoding="utf-8")[:500]
            skin_match = re.search(r"Title:\s*(.+)", css)
            if skin_match:
                platforms["squidgeworld"]["work_skin"] = skin_match.group(1).strip()
    # AO3 uses the same SquidgeWorld HTML format (OTW Archive)
    if formats.get("squidgeworld") or formats.get("sofurry_html"):
        platforms["ao3"] = {"format": "squidgeworld_html"}
    # DeviantArt — uses OAuth2 literature API, accepts plain text/markdown
    if formats.get("markdown"):
        platforms["deviantart"] = {"format": "markdown", "api": "oauth2"}
    platforms["bluesky"] = {"type": "announcement"}
    result["platforms"] = platforms

    # ── Word count fallback from MASTER.md ──
    if result["word_count"] == 0:
        master = story_path / "Markdown" / "MASTER.md"
        if master.is_file():
            text = master.read_text(encoding="utf-8")
            result["word_count"] = len(text.split())

    return result


def _parse_tags_into(text: str, result: dict) -> None:
    """Parse tags_upload.txt and populate result dict."""

    # Story description
    desc_match = re.search(
        r"STORY DESCRIPTION:\s*\n(.+?)(?:\n=====|\nCHAPTER|\nPART|\nTAGS|\n\n)",
        text, re.DOTALL
    )
    if desc_match:
        result["description"] = desc_match.group(1).strip()

    # SQW Summary
    summary_match = re.search(
        r"^SUMMARY[^:]*:\s*\n(.+?)(?:\nNOTES|\nASSOCIATIONS|\n=====)",
        text, re.MULTILINE | re.DOTALL
    )
    if summary_match:
        result["summary"] = summary_match.group(1).strip()

    # Rating
    rating_match = re.search(r"^RATING:\s*(.+)", text, re.MULTILINE)
    if rating_match:
        result["rating"] = rating_match.group(1).strip().lower()

    # Warnings
    warn_match = re.search(r"^ARCHIVE WARNINGS?:\s*\n(.+?)(?:\n\n|\nFANDOM)", text, re.MULTILINE | re.DOTALL)
    if warn_match:
        result["warnings"] = [w.strip() for w in warn_match.group(1).strip().split("\n") if w.strip()]

    # Category
    cat_match = re.search(r"^CATEGORY:\s*(.+)", text, re.MULTILINE)
    if cat_match:
        result["category"] = cat_match.group(1).strip()

    # Fandom
    fandom_match = re.search(r"^FANDOM:\s*(.+)", text, re.MULTILINE)
    if fandom_match:
        result["fandom"] = fandom_match.group(1).strip()

    # Characters
    char_match = re.search(r"^CHARACTERS:\s*\n?(.+?)(?:\n\n|\nADDITIONAL|\nSUMMARY)", text, re.MULTILINE | re.DOTALL)
    if char_match:
        result["characters"] = [c.strip() for c in char_match.group(1).split(",") if c.strip()]

    # Relationships
    rel_match = re.search(r"^RELATIONSHIPS?:\s*\n?(.+?)(?:\n\n|\nCHARACTERS)", text, re.MULTILINE | re.DOTALL)
    if rel_match:
        result["relationships"] = [r.strip() for r in rel_match.group(1).split(",") if r.strip()]

    # Tags — SoFurry / default (first TAGS section)
    sf_match = re.search(r"^(?:SOFURRY )?TAGS?\s*\(\d+\):\s*\n(.+?)(?:\n\n|\nINKBUNNY)", text, re.MULTILINE | re.DOTALL)
    if sf_match:
        tags = [t.strip() for t in sf_match.group(1).strip().split(",") if t.strip()]
        result["tags"]["default"] = tags
        result["tags"]["sofurry"] = tags

    # Inkbunny tags (flatten categories)
    ib_section = re.search(r"INKBUNNY TAGS.*?:\s*\n(.+?)(?:\nWATTPAD|\n\n=====|\n\nSOFURRY|\n\nCHAPTER|\Z)", text, re.DOTALL)
    if ib_section:
        ib_tags = []
        for line in ib_section.group(1).split("\n"):
            line = line.strip()
            if not line or line.endswith(":") or "INKBUNNY" in line:
                continue
            ib_tags.extend(t.strip() for t in line.split(",") if t.strip())
        if ib_tags:
            result["tags"]["inkbunny"] = ib_tags
            result["tags"]["weasyl"] = ib_tags

    # Wattpad tags
    wp_match = re.search(r"WATTPAD TAGS.*?:\s*\n(.+?)(?:\n\n|\n=====)", text, re.DOTALL)
    if wp_match:
        result["tags"]["wattpad"] = wp_match.group(1).strip().split()

    # Per-chapter descriptions
    part_pattern = re.compile(r"(?:CHAPTER|PART)\s+(\d+)\s+OF\s+\d+.*?\n(.*?)(?=(?:CHAPTER|PART)\s+\d+\s+OF|$)", re.DOTALL)
    for match in part_pattern.finditer(text):
        ch_idx = int(match.group(1))
        ch_section = match.group(2)
        ch_desc = re.search(r"DESCRIPTION:\s*\n(.+?)(?:\n\n|\nTAGS|\nSOFURRY|\nINKBUNNY)", ch_section, re.DOTALL)
        if ch_desc and ch_idx <= len(result["chapter_info"]):
            for ch in result["chapter_info"]:
                if ch["index"] == ch_idx:
                    ch["description"] = ch_desc.group(1).strip()
                    break


def process_all(archive: Path, story_filter: str | None = None, dry_run: bool = False) -> list[str]:
    """Generate story.json for all stories (or one). Returns list of processed names."""
    processed = []

    for entry in sorted(archive.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name == "Reference_Guides":
            continue

        # Check for sub-stories (e.g. My_Story/Alt_Version)
        has_master = (entry / "Markdown" / "MASTER.md").is_file()
        sub_dirs = [d for d in entry.iterdir() if d.is_dir() and (d / "Markdown" / "MASTER.md").is_file()]

        if has_master:
            stories = [(entry, entry.name)]
        elif sub_dirs:
            stories = [(d, f"{entry.name}/{d.name}") for d in sub_dirs]
        else:
            continue

        for story_path, full_name in stories:
            if story_filter and story_filter not in full_name:
                continue

            data = generate_story_json(story_path)
            out_path = story_path / "story.json"

            if dry_run:
                print(f"  [DRY RUN] {full_name}: {data['chapters']} chapters, {data['word_count']} words, "
                      f"{len(data['tags'].get('default', []))} tags, {len(data['platforms'])} platforms")
            else:
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                print(f"  {full_name}: wrote story.json ({data['chapters']} ch, {data['word_count']} words)")

            processed.append(full_name)

    return processed


if __name__ == "__main__":
    archive = find_archive()
    story_filter = None
    dry_run = False

    for arg in sys.argv[1:]:
        if arg == "--dry-run":
            dry_run = True
        else:
            story_filter = arg

    print(f"Archive: {archive}")
    print(f"Mode: {'DRY RUN' if dry_run else 'GENERATE'}")
    print()

    processed = process_all(archive, story_filter, dry_run)
    print(f"\n{len(processed)} stories processed")
