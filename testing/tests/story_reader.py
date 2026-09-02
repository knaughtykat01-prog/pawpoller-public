"""Story reader tests.

6 read-only tests against the live story archive. The "pick a story"
logic prefers `_Test_Story` if present, else the first story in the
archive. Tests skip when no stories are available.
"""

from __future__ import annotations

import json

from posting import story_reader
from testing.registry import TestContext, register_test


def _pick_test_story() -> str | None:
    """Return a story name suitable for diagnostics, or None."""
    try:
        archive = story_reader.get_archive_path()
    except Exception:  # noqa: BLE001
        return None
    if not archive.is_dir():
        return None
    candidates = [p for p in archive.iterdir() if p.is_dir()]
    # Prefer the canonical test fixture
    for p in candidates:
        if p.name == "_Test_Story":
            return p.name
    # Else: any story with story.json or Markdown/MASTER.md
    for p in candidates:
        if (p / "story.json").is_file() or (p / "Markdown" / "MASTER.md").is_file():
            return p.name
    return None


@register_test(
    test_id="story_reader.archive.path_resolves",
    name="Archive path resolves",
    category="Story Reader",
    description="story_reader.get_archive_path() returns a directory that exists.",
)
async def t_archive_path(ctx: TestContext) -> None:
    path = story_reader.get_archive_path()
    ctx.detail("path", str(path))
    assert path.is_dir(), f"archive path not a directory: {path}"


@register_test(
    test_id="story_reader.archive.has_stories",
    name="Archive has at least one story",
    category="Story Reader",
    description="Listdir of archive returns ≥ 1 story-shaped directory.",
)
async def t_has_stories(ctx: TestContext) -> None:
    path = story_reader.get_archive_path()
    assert path.is_dir(), "archive path missing"
    stories = [
        p for p in path.iterdir()
        if p.is_dir() and (
            (p / "story.json").is_file()
            or (p / "Markdown" / "MASTER.md").is_file()
        )
    ]
    ctx.detail("story_count", len(stories))
    ctx.detail("first", stories[0].name if stories else None)
    assert stories, "no story-shaped subdirectories in archive"


@register_test(
    test_id="story_reader.load_test_story",
    name="load_story succeeds on the test story",
    category="Story Reader",
    description="story_reader.load_story populates title, chapters, tags_by_platform.",
)
async def t_load_story(ctx: TestContext) -> None:
    name = _pick_test_story()
    if name is None:
        raise ctx.skip("no story available to load")
    story = story_reader.load_story(name)
    ctx.detail("name", story.name)
    ctx.detail("total_chapters", story.total_chapters)
    ctx.detail("total_words", story.total_words)
    assert story.name == name
    assert story.title, "story title empty"


@register_test(
    test_id="story_reader.build_packages_all_platforms",
    name="build_package works for every poster platform",
    category="Story Reader",
    description="Iterate IB/FA/WS/SF/SqW/AO3/IK/DA/Bsky, build a package for chapter 1 (or full).",
)
async def t_build_packages(ctx: TestContext) -> None:
    name = _pick_test_story()
    if name is None:
        raise ctx.skip("no story available")
    story = story_reader.load_story(name)
    platforms = ["ib", "fa", "ws", "sf", "sqw", "ao3", "ik", "da", "bsky"]
    ch_idx = 1 if story.total_chapters >= 1 else 0
    results: dict[str, str | None] = {}
    for plat in platforms:
        try:
            pkg = story_reader.build_package(story, ch_idx, plat)
            results[plat] = pkg.file_path or "(no file)"
        except Exception as exc:  # noqa: BLE001
            results[plat] = f"ERROR: {type(exc).__name__}: {exc}"
    ctx.detail("packages", results)
    errors = [k for k, v in results.items() if v and v.startswith("ERROR")]
    assert not errors, f"build_package failed for: {errors}"


@register_test(
    test_id="story_reader.format_resolution_chapter_not_full",
    name="Chapter 1 resolution ≠ full-story file (regression 2.18.19)",
    category="Story Reader",
    description=(
        "When story has chapters, build_package(ch=1, platform='ib') must "
        "return Chapters/BBCode/Chapter_1_*.txt, not BBCode/<Story>_bbcode.txt."
    ),
)
async def t_format_resolution(ctx: TestContext) -> None:
    name = _pick_test_story()
    if name is None:
        raise ctx.skip("no story available")
    story = story_reader.load_story(name)
    if story.total_chapters < 1:
        raise ctx.skip("test story is single-piece; no chapter resolution to check")
    pkg_full = story_reader.build_package(story, 0, "ib")
    pkg_ch1 = story_reader.build_package(story, 1, "ib")
    ctx.detail("full_path", pkg_full.file_path)
    ctx.detail("ch1_path", pkg_ch1.file_path)
    if pkg_full.file_path and pkg_ch1.file_path:
        assert pkg_full.file_path != pkg_ch1.file_path, (
            "chapter resolution returned full-story file"
        )


@register_test(
    test_id="story_reader.manifest_parsing",
    name="split_manifest parses with 'number' key (regression 2.18.19)",
    category="Story Reader",
    description=(
        "ChapterInfo.filename should be populated (derived from manifest "
        "files dict), proving the manifest lookup keys on 'number' rather "
        "than 'index'."
    ),
)
async def t_manifest_parsing(ctx: TestContext) -> None:
    name = _pick_test_story()
    if name is None:
        raise ctx.skip("no story available")
    story = story_reader.load_story(name)
    if story.total_chapters < 1:
        raise ctx.skip("single-piece story has no manifest to check")
    bad = [ch for ch in story.chapters if not ch.filename]
    ctx.detail("chapter_filenames", [ch.filename for ch in story.chapters])
    assert not bad, f"chapters missing filename: {[(c.index) for c in bad]}"
