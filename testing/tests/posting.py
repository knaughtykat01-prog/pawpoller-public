"""Posting dry-run tests.

For each posting platform, build a package for chapter 1 (or full)
of the test story and run `poster.validate(package)`. NO network
upload. Empty validation result == pass.
"""

from __future__ import annotations

from posting import manager, story_reader
from testing.registry import TestContext, register_test


_POSTING_PLATFORMS = ["ib", "fa", "ws", "sf", "sqw", "ao3", "ik", "da", "bsky"]


def _pick_test_story() -> str | None:
    """Same logic as story_reader tests."""
    try:
        archive = story_reader.get_archive_path()
    except Exception:  # noqa: BLE001
        return None
    if not archive.is_dir():
        return None
    candidates = [p for p in archive.iterdir() if p.is_dir()]
    for p in candidates:
        if p.name == "_Test_Story":
            return p.name
    for p in candidates:
        if (p / "story.json").is_file() or (p / "Markdown" / "MASTER.md").is_file():
            return p.name
    return None


def _make_dry_run_test(plat: str):
    @register_test(
        test_id=f"posting.{plat}.dry_run",
        name=f"{plat.upper()} — package validates",
        category="Posting (Dry-Run)",
        description=(
            f"Build a chapter-1 (or full) StoryUploadPackage for {plat} "
            "and run the poster's validate(). No network."
        ),
    )
    async def _t(ctx: TestContext) -> None:
        name = _pick_test_story()
        if name is None:
            raise ctx.skip("no story available")
        story = story_reader.load_story(name)
        ch_idx = 1 if story.total_chapters >= 1 else 0
        try:
            poster = manager._get_poster(plat)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"could not instantiate {plat} poster: {exc}") from exc
        pkg = story_reader.build_package(story, ch_idx, plat)
        errors = poster.validate(pkg)
        ctx.detail("file_path", pkg.file_path)
        ctx.detail("title", pkg.title)
        ctx.detail("tag_count", len(pkg.tags) if pkg.tags else 0)
        ctx.detail("validation_errors", errors)
        assert not errors, f"validate() returned errors: {errors}"

    return _t


# Register one dry-run test per posting platform
for _plat in _POSTING_PLATFORMS:
    _make_dry_run_test(_plat)
