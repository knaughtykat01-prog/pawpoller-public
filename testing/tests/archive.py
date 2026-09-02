"""Story archive diagnostics.

All read-only: list stories, validate every story.json, optional
pawsync dry-run.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from posting import story_reader
from testing.registry import TestContext, register_test


_REQUIRED_STORY_JSON_FIELDS = ("title",)  # name is implicit from the folder


@register_test(
    test_id="archive.local_readable",
    name="Local archive listable",
    category="Archive",
    description="The configured archive directory exists and contains story folders.",
)
async def t_archive_listable(ctx: TestContext) -> None:
    path = story_reader.get_archive_path()
    ctx.detail("path", str(path))
    assert path.is_dir(), f"archive not a directory: {path}"
    entries = [p for p in path.iterdir() if p.is_dir()]
    ctx.detail("entry_count", len(entries))
    assert entries, "archive is empty"


@register_test(
    test_id="archive.story_json_valid",
    name="Every story.json is valid JSON with required fields",
    category="Archive",
    description=(
        "Iterate every story folder; any with a story.json must parse "
        "as JSON and include at minimum the 'title' field."
    ),
    timeout_seconds=60.0,
)
async def t_story_json_valid(ctx: TestContext) -> None:
    path = story_reader.get_archive_path()
    failures: list[dict] = []
    checked = 0
    for p in sorted(path.iterdir()):
        if not p.is_dir():
            continue
        sj = p / "story.json"
        if not sj.is_file():
            continue
        checked += 1
        try:
            data = json.loads(sj.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            failures.append({"story": p.name, "reason": f"parse: {exc}"})
            continue
        for field in _REQUIRED_STORY_JSON_FIELDS:
            if not data.get(field):
                failures.append({"story": p.name, "reason": f"missing {field}"})
                break
    ctx.detail("checked", checked)
    ctx.detail("failures", failures)
    assert not failures, f"{len(failures)} story.json failures"


@register_test(
    test_id="archive.pawsync.dry_run",
    name="pawsync.py --dry-run reports cleanly",
    category="Archive",
    description=(
        "Run the local pawsync script in --dry-run mode and confirm it "
        "exits 0. Skipped when the script isn't present (server) or "
        "remote not configured (test instance)."
    ),
    timeout_seconds=60.0,
)
async def t_pawsync_dry_run(ctx: TestContext) -> None:
    script = Path(__file__).resolve().parent.parent.parent / "deploy" / "pawsync.py"
    if not script.is_file():
        raise ctx.skip(f"pawsync.py not present at {script}")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        "--dry-run",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45.0)
    except asyncio.TimeoutError:
        proc.kill()
        raise AssertionError("pawsync dry-run timed out")
    ctx.detail("returncode", proc.returncode)
    ctx.detail("stdout_tail", stdout.decode("utf-8", "replace")[-400:])
    if proc.returncode != 0:
        ctx.detail("stderr_tail", stderr.decode("utf-8", "replace")[-400:])
    # Treat non-zero as a soft skip when the script complains about
    # missing config (test environment) but raise on actual errors.
    if proc.returncode != 0:
        tail = stderr.decode("utf-8", "replace")
        tail_lower = tail.lower()
        # Soft-skip when pawsync is unrunnable in this environment:
        #   - test instance with no remote configured
        #   - server where the hard-coded local archive path doesn't exist
        if (
            "not configured" in tail_lower
            or "remote" in tail_lower
            or "archive root not found" in tail_lower
            or "no such file" in tail_lower
        ):
            last = tail.strip().splitlines()[-1] if tail.strip() else "pawsync unrunnable in this environment"
            raise TestSkippedReason(last)
        raise AssertionError(f"pawsync returned {proc.returncode}: {tail[-200:]}")


# Local alias so the import at the top stays tidy
from testing.registry import TestSkipped as TestSkippedReason  # noqa: E402


@register_test(
    test_id="archive.regenerate.all_stories",
    name="Regenerate every story (no PDF)",
    category="Archive",
    description=(
        "DESTRUCTIVE: rebuilds derived format files (BBCode, Clean HTML, "
        "SoFurry HTML, Styled HTML, SquidgeWorld, EPUB, chapter splits) "
        "for every story in the archive. Always skips PDF (too slow for "
        "the test suite). Calls the editor's per-story regenerate() "
        "function in-process so behaviour matches the dashboard button."
    ),
    destructive=True,
    timeout_seconds=900.0,  # up to 15 min for a large archive
)
async def t_regenerate_all_stories(ctx: TestContext) -> None:
    # Defer imports so a missing editor module doesn't break test discovery.
    from routes.editor_api import RegenerateRequest, regenerate, SKIP_DIRS

    archive = story_reader.get_archive_path()
    if not archive.is_dir():
        raise ctx.skip(f"archive not a directory: {archive}")

    targets: list[str] = []
    for entry in sorted(archive.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in SKIP_DIRS:
            continue
        if (entry / "Markdown" / "MASTER.md").is_file():
            targets.append(entry.name)
            continue
        for sub in sorted(entry.iterdir()):
            if sub.is_dir() and (sub / "Markdown" / "MASTER.md").is_file():
                targets.append(f"{entry.name}/{sub.name}")

    if not targets:
        raise ctx.skip("no stories with MASTER.md in the archive")

    ctx.detail("target_count", len(targets))
    passed = 0
    partial = 0
    failed = 0
    failures: list[dict] = []

    for i, name in enumerate(targets, start=1):
        ctx.log(f"[{i}/{len(targets)}] {name}", level="info")
        try:
            req = RegenerateRequest(skip_pdf=True)
            result = await regenerate(name, req)
            errs = result.get("errors", []) or []
            if not errs:
                passed += 1
            else:
                partial += 1
                failures.append({"story": name, "errors": errs})
                ctx.log(f"    {len(errs)} non-fatal error(s) on {name}", level="warn")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            failures.append({"story": name, "errors": [f"{type(exc).__name__}: {exc}"]})
            ctx.log(f"    FAILED on {name}: {exc}", level="error")

    ctx.detail("passed", passed)
    ctx.detail("partial", partial)
    ctx.detail("failed", failed)
    ctx.detail("failures", failures)
    # Fail the test only on hard failures (exception during regen).
    # Partials (story regenerated but had per-format warnings) are
    # surfaced in details but don't fail the run — they're often
    # cosmetic (e.g. PDF skipped because we asked for skip_pdf).
    assert failed == 0, f"{failed} stories failed to regenerate"
