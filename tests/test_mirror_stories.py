"""The story archive mirrors too (3.19.0).

Reported: *"stories should be synced with the mirror too right? everything should be
synced over not just art."*

He was right, and the gap had just bitten him. Artwork, post media and the
database all crossed; the story archive moved only through `pawsync`/`pawpull`,
two maintainer shell scripts. So a desktop restored from the server came back
**unable to post anything** — proved live when an uninstall wiped
`%APPDATA%/PawPoller/story-archive` and a queued FurAffinity job failed with
`Story folder not found: …\\story-archive\\Chosen`.

Two things had to be got right rather than copied:

  * **The exclusion rule is different.** Stories carry derived-but-needed trees
    (`HTML/`, `BBCode/`, `PDF/`, `EPUB/`, `Chapters/`) because the posters
    upload those files directly and the server has no browser to regenerate a
    PDF — while `Backups/` and `Drafts/` must NOT cross. The artwork rule gets
    both of those backwards, so the story pass delegates to
    `deploy/archive_sync_rules.is_excluded`, which is already the one place
    those rules live. A third list here would recreate the exact drift that
    module was written to end.

  * **An older server has no `stories` key at all**, and absent is not empty.
    Reporting a clean story sync that never happened is the same failure as
    3.18.1's 404 read as "gone from server".
"""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from mirror import core


def _story(root: Path, name: str, *, master: str = "# Story\n") -> Path:
    d = root / name
    (d / "Markdown").mkdir(parents=True, exist_ok=True)
    (d / "Markdown" / "MASTER.md").write_text(master, encoding="utf-8")
    (d / "story.json").write_text('{"title":"x"}', encoding="utf-8")
    (d / "HTML").mkdir(exist_ok=True)
    (d / "HTML" / "out.html").write_text("<p>y</p>", encoding="utf-8")
    (d / "Backups").mkdir(exist_ok=True)
    (d / "Backups" / "old.md").write_text("old", encoding="utf-8")
    (d / "MASTER.md.bak.1787196554").write_text("bak", encoding="utf-8")
    return d


# ── the rule is the archive's own, not the artwork one ───────────

def test_derived_but_needed_trees_still_travel(tmp_path):
    """`HTML/` is derived, and it must cross anyway: the posters upload those
    files directly and the server cannot regenerate them."""
    d = _story(tmp_path, "Chosen")
    files = {p.relative_to(d).as_posix()
             for p in core.iter_mirrored_files(d, d, include=core.is_mirrored_story_file)}
    assert "HTML/out.html" in files
    assert "Markdown/MASTER.md" in files and "story.json" in files


def test_backups_do_not_travel(tmp_path):
    """The artwork rule would carry `Backups/`. The archive rule must not —
    and that difference is why the two cannot share a predicate."""
    d = _story(tmp_path, "Chosen")
    files = {p.relative_to(d).as_posix()
             for p in core.iter_mirrored_files(d, d, include=core.is_mirrored_story_file)}
    assert not any(f.startswith("Backups/") for f in files)


def test_bak_files_do_not_travel(tmp_path):
    d = _story(tmp_path, "Chosen")
    files = {p.name for p in core.iter_mirrored_files(d, d,
                                                     include=core.is_mirrored_story_file)}
    assert not any(".bak." in f for f in files)


def test_dotfiles_never_travel_whatever_the_store(tmp_path):
    """`.vault_key` must not cross in any store."""
    d = _story(tmp_path, "Chosen")
    (d / ".vault_key").write_text("secret", encoding="utf-8")
    files = {p.name for p in core.iter_mirrored_files(d, d,
                                                     include=core.is_mirrored_story_file)}
    assert ".vault_key" not in files


def test_the_rule_is_delegated_not_restated():
    """`deploy/archive_sync_rules.py` exists because pawsync and pawpull each
    carried their own exclude list and a push-then-pull stopped being
    idempotent. The mirror must import it, not add a third."""
    import inspect
    assert "archive_sync_rules" in inspect.getsource(core.is_mirrored_story_file)


def test_the_two_predicates_genuinely_disagree(tmp_path):
    """Guard against someone 'simplifying' them into one."""
    d = _story(tmp_path, "Chosen")
    backup = d / "Backups" / "old.md"
    assert core.is_mirrored_file(backup, d) is True
    assert core.is_mirrored_story_file(backup, d) is False


# ── manifest + packing ───────────────────────────────────────────

def test_a_story_manifest_reports_only_canonical_files(tmp_path):
    _story(tmp_path, "Chosen")
    m = core.build_manifest(tmp_path, detail=True, include=core.is_mirrored_story_file)
    paths = {f["path"] for f in m["folders"][0]["files"]}
    assert "Markdown/MASTER.md" in paths
    assert not any(p.startswith("Backups/") for p in paths)


def test_packing_a_story_folder_honours_the_rule(tmp_path):
    d = _story(tmp_path, "Chosen")
    blob = core.pack_folder(d, arcname="Chosen", include=core.is_mirrored_story_file)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
        names = t.getnames()
    assert "Chosen/Markdown/MASTER.md" in names
    assert not any("Backups" in n for n in names)


def test_per_file_story_fetch_cannot_smuggle_an_excluded_file(tmp_path):
    """Asking for `Backups/old.md` by name must not bypass the rule."""
    d = _story(tmp_path, "Chosen")
    blob = core.pack_folder_files(d, ["Backups/old.md"],
                                  include=core.is_mirrored_story_file)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
        assert t.getnames() == []


@pytest.mark.parametrize("bad", ["../escape.md", "/etc/passwd",
                                 "C:" + chr(92) + "x", "a/../../x"])
def test_story_paths_use_the_same_traversal_guard(tmp_path, bad):
    d = _story(tmp_path, "Chosen")
    with pytest.raises(core.MirrorSecurityError):
        core.pack_folder_files(d, [bad], include=core.is_mirrored_story_file)


# ── end to end through the puller ────────────────────────────────

def _fake_client(remote_manifest, art_root, story_root):
    class _R:
        def __init__(self, code, content=b"", payload=None):
            self.status_code, self.content, self._p = code, content, payload
        def json(self): return self._p
        text = ""

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **kw):
            if "manifest" in url:
                return _R(200, payload=remote_manifest)
            if "/story/" in url:
                name = url.rsplit("/", 1)[-1]
                return _R(200, core.pack_folder(story_root / name, arcname=name,
                                                include=core.is_mirrored_story_file))
            if "/artwork/" in url:
                name = url.rsplit("/", 1)[-1]
                return _R(200, core.pack_folder(art_root / name, arcname=name))
            return _R(404)
        async def post(self, url, **kw):
            seg = "/story/" if "/story/" in url else "/artwork/"
            name = url.split(seg)[1].rsplit("/files", 1)[0]
            root = story_root if seg == "/story/" else art_root
            inc = core.is_mirrored_story_file if seg == "/story/" else None
            return _R(200, core.pack_folder_files(root / name, kw["json"]["paths"],
                                                  arcname=name, include=inc))
    return _Client


def test_a_missing_story_is_pulled_down(monkeypatch, tmp_path):
    """The scenario that prompted this: the desktop's story archive is gone and
    a queued FA post cannot find `Chosen`."""
    import asyncio
    from routes import mirror_api

    remote_art, remote_story = tmp_path / "ra", tmp_path / "rs"
    remote_art.mkdir(); _story(remote_story, "Chosen")
    local_art, local_story = tmp_path / "la", tmp_path / "ls"
    local_art.mkdir(); local_story.mkdir()

    remote = {"artwork": core.build_manifest(remote_art, detail=True),
              "stories": core.build_manifest(remote_story, detail=True,
                                             include=core.is_mirrored_story_file)}
    monkeypatch.setattr(mirror_api, "_artwork_root", lambda: local_art)
    monkeypatch.setattr(mirror_api, "_story_root", lambda: local_story)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(remote, remote_story, remote_story))

    summary = asyncio.run(mirror_api._run_pull(
        "https://s", "k", dry_run=False, include_db=False, include_media=False,
        only=None, push_first=False))

    assert summary["stories"]["fetched"] == 1
    assert summary["stories"]["failed"] == []
    assert (local_story / "Chosen" / "Markdown" / "MASTER.md").is_file()
    # And the rule held on the way in.
    assert not (local_story / "Chosen" / "Backups").exists()


def test_an_older_server_is_reported_as_skipped_not_synced(monkeypatch, tmp_path):
    """⚠ A server before 3.19.0 has no `stories` key. Treating absent as empty
    would report a clean story sync that never happened — the same mistake as
    reading 3.18.1's 404 as "gone from server"."""
    import asyncio
    from routes import mirror_api

    remote_art = tmp_path / "ra"; remote_art.mkdir()
    local_art, local_story = tmp_path / "la", tmp_path / "ls"
    local_art.mkdir(); local_story.mkdir()
    remote = {"artwork": core.build_manifest(remote_art, detail=True)}   # no stories

    monkeypatch.setattr(mirror_api, "_artwork_root", lambda: local_art)
    monkeypatch.setattr(mirror_api, "_story_root", lambda: local_story)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(remote, remote_art, local_story))

    summary = asyncio.run(mirror_api._run_pull(
        "https://s", "k", dry_run=False, include_db=False, include_media=False,
        only=None, push_first=False))
    assert "skipped" in summary["stories"]
    assert "3.19.0" in summary["stories"]["skipped"]


def test_a_dry_run_reports_both_stores(monkeypatch, tmp_path):
    """Returning after the artwork plan would show stories as clean without
    ever having looked."""
    import asyncio
    from routes import mirror_api

    remote_art, remote_story = tmp_path / "ra", tmp_path / "rs"
    remote_art.mkdir(); _story(remote_story, "Chosen")
    local_art, local_story = tmp_path / "la", tmp_path / "ls"
    local_art.mkdir(); local_story.mkdir()
    remote = {"artwork": core.build_manifest(remote_art, detail=True),
              "stories": core.build_manifest(remote_story, detail=True,
                                             include=core.is_mirrored_story_file)}
    monkeypatch.setattr(mirror_api, "_artwork_root", lambda: local_art)
    monkeypatch.setattr(mirror_api, "_story_root", lambda: local_story)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(remote, remote_story, remote_story))

    summary = asyncio.run(mirror_api._run_pull(
        "https://s", "k", dry_run=True, include_db=False, include_media=False,
        only=None, push_first=False))
    assert summary["stories_plan"]["fetch"] == ["Chosen"]
    assert not (local_story / "Chosen").exists(), "a dry run must write nothing"


def test_the_two_stores_share_one_loop():
    """Artwork and stories differ only in root, endpoint and rule. A second
    copy of the loop is how the two drift — the bug `archive_sync_rules.py`
    was written to end."""
    import inspect
    from routes import mirror_api
    src = inspect.getsource(mirror_api._run_pull)
    assert src.count("_sync_folder_store") >= 3      # def + artwork + stories
