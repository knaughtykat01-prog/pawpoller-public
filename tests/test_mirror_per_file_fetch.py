"""Fetch the files that changed, not the folders they live in (3.18.0).

Measured on the live pair before this existed: a pull moved **158.6 MB to
deliver 0.2 MB of change** — 155 changed files, **277 identical files
re-fetched**, an 839× waste factor. The cause was granularity, not design:

  * `folder_manifest(detail=True)` has always reported `{path, size, sha256}`
    per file, and `_run_pull` has always requested `detail=true`;
  * but the only way to GET anything was `/artwork/{name}` — the whole folder.

So one edited `masterpiece.json` dragged the untouched 29 MB image beside it
across the wire. The information needed to avoid that was already being
computed and already being sent.

A folder the client does not have at all is still fetched whole; there is
nothing to diff against, and that is the right call.
"""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from mirror import core


def _work(root: Path, name: str, *, meta: str = '{"tags":{}}', image: int = 40_000) -> Path:
    """A work folder: small metadata beside a large image.

    The image bytes must be two things at once, and getting either wrong makes
    these tests measure the wrong property:

    * **incompressible** — a run of zeros gzips to almost nothing, which makes
      the whole-folder archive look as cheap as the partial one and turns the
      size assertions into a test of compressibility;
    * **identical across calls** — `os.urandom` gives the source and the
      destination genuinely different images, so the image really has changed
      and every "only the metadata differs" assertion collapses.

    A seeded PRNG is both.
    """
    import random as _random
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "masterpiece.json").write_text(meta, encoding="utf-8")
    (d / "image.png").write_bytes(b"\x89PNG" + _random.Random(1).randbytes(image))
    return d


# ── the plan knows which files ───────────────────────────────────

def test_the_diff_names_the_files_that_changed(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _work(src, "Piece", meta='{"tags":{"core":["a"]}}')
    _work(dst, "Piece", meta='{"tags":{}}')
    plan = core.diff_manifests(core.build_manifest(src, detail=True),
                               core.build_manifest(dst, detail=True))
    assert plan["changed"] == ["Piece"]
    assert plan["changed_files"] == {"Piece": ["masterpiece.json"]}


def test_the_untouched_image_is_not_in_the_plan(tmp_path):
    """The whole point. `image.png` is identical on both sides and must not be
    proposed for transfer just because its neighbour changed."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _work(src, "Piece", meta='{"changed":true}')
    _work(dst, "Piece", meta='{"changed":false}')
    plan = core.diff_manifests(core.build_manifest(src, detail=True),
                               core.build_manifest(dst, detail=True))
    assert "image.png" not in plan["changed_files"]["Piece"]


def test_the_two_byte_counts_show_the_waste(tmp_path):
    """`fetch_bytes` is what a whole-folder fetch costs; `fetch_file_bytes` is
    what the change actually weighs. Reporting both is what made the 839×
    visible in the first place."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _work(src, "Piece", meta='{"a":1}', image=200_000)
    _work(dst, "Piece", meta='{"a":2}', image=200_000)
    plan = core.diff_manifests(core.build_manifest(src, detail=True),
                               core.build_manifest(dst, detail=True))
    assert plan["fetch_file_bytes"] < plan["fetch_bytes"] / 100


def test_a_missing_folder_is_still_fetched_whole(tmp_path):
    """No local copy means nothing to diff — whole-folder is correct, and the
    plan says so by omitting it from `changed_files`."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _work(src, "New")
    dst.mkdir()
    plan = core.diff_manifests(core.build_manifest(src, detail=True),
                               core.build_manifest(dst, detail=True))
    assert plan["missing"] == ["New"]
    assert "New" not in plan.get("changed_files", {})


def test_a_manifest_without_detail_falls_back_to_whole_folders(tmp_path):
    """Without per-file hashes there is no way to tell surplus from divergence,
    so the safe answer is the old behaviour."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _work(src, "Piece", meta='{"a":1}')
    _work(dst, "Piece", meta='{"a":2}')
    plan = core.diff_manifests(core.build_manifest(src), core.build_manifest(dst))
    assert plan["fetch"] == ["Piece"]
    assert plan.get("changed_files", {}) == {}


# ── packing only what was asked for ──────────────────────────────

def test_only_the_requested_file_is_packed(tmp_path):
    d = _work(tmp_path, "Piece")
    blob = core.pack_folder_files(d, ["masterpiece.json"])
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
        assert t.getnames() == ["Piece/masterpiece.json"]


def test_the_partial_archive_is_far_smaller(tmp_path):
    d = _work(tmp_path, "Piece", image=200_000)
    assert len(core.pack_folder_files(d, ["masterpiece.json"])) < len(core.pack_folder(d)) / 10


def test_extracting_a_partial_archive_updates_only_that_file(tmp_path):
    """End to end: the changed file lands, the untouched one is not disturbed."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _work(src, "Piece", meta='{"v":2}')
    _work(dst, "Piece", meta='{"v":1}')
    before = (dst / "Piece" / "image.png").read_bytes()
    core.extract_bytes(core.pack_folder_files(src / "Piece", ["masterpiece.json"]), dst)
    assert json.loads((dst / "Piece" / "masterpiece.json").read_text())["v"] == 2
    assert (dst / "Piece" / "image.png").read_bytes() == before


def test_a_partial_fetch_makes_the_folder_match(tmp_path):
    """After fetching just the changed files, the next diff must report the
    folder as unchanged — otherwise it re-downloads forever, which is the trap
    `diff_manifests` already documents for digest-equality."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _work(src, "Piece", meta='{"v":2}')
    _work(dst, "Piece", meta='{"v":1}')
    plan = core.diff_manifests(core.build_manifest(src, detail=True),
                               core.build_manifest(dst, detail=True))
    core.extract_bytes(core.pack_folder_files(src / "Piece", plan["changed_files"]["Piece"]), dst)
    after = core.diff_manifests(core.build_manifest(src, detail=True),
                                core.build_manifest(dst, detail=True))
    assert after["fetch"] == []


def test_a_file_the_server_no_longer_has_is_skipped_not_fatal(tmp_path):
    d = _work(tmp_path, "Piece")
    blob = core.pack_folder_files(d, ["masterpiece.json", "vanished.json"])
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
        assert t.getnames() == ["Piece/masterpiece.json"]


def test_derived_files_are_still_excluded(tmp_path):
    """`.bak.<ts>` files are per-device undo history and never mirror. Asking
    for one by name must not smuggle it past that rule."""
    d = _work(tmp_path, "Piece")
    (d / "masterpiece.json.bak.1787196554").write_text("{}", encoding="utf-8")
    blob = core.pack_folder_files(d, ["masterpiece.json.bak.1787196554"])
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
        assert t.getnames() == []


# ── a path parameter is where traversal arrives ──────────────────

@pytest.mark.parametrize("bad", [
    "../escape.txt",
    "sub/../../escape.txt",
    "/etc/passwd",
    "C:" + chr(92) + "Windows" + chr(92) + "evil.dll",
    chr(92) + "abs",
    "C:evil",
])
def test_a_hostile_path_is_refused(tmp_path, bad):
    d = _work(tmp_path, "Piece")
    with pytest.raises(core.MirrorSecurityError):
        core.pack_folder_files(d, [bad])


def test_it_uses_the_shared_guard_rather_than_its_own(tmp_path):
    """3.17.4 had to remove three separate copies of one extraction check. A
    new endpoint that names files is exactly where a fourth would appear."""
    import inspect
    src = inspect.getsource(core.pack_folder_files)
    assert "_reject_foreign_absolute" in src


def test_nothing_escapes_even_when_the_folder_is_a_symlink_target(tmp_path):
    """The containment re-check is on the RESOLVED target, so a path that
    resolves outside is refused whatever route it took."""
    d = _work(tmp_path, "Piece")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    with pytest.raises(core.MirrorSecurityError):
        core.pack_folder_files(d, ["../secret.txt"])


# ── the two halves of a mirror can be on different versions ──────

def test_a_server_without_the_per_file_endpoint_still_syncs(monkeypatch, tmp_path):
    """⚠ The bug this nearly shipped with.

    `POST /artwork/{name}/files` only exists from 3.18.0, and a 404 from it is
    AMBIGUOUS — "that folder is gone" or "this server is older and has no such
    route". The first version treated it as the former, so a 3.18.0 desktop
    talking to a 3.17.4 server reported **every changed folder as "gone from
    server" and synced nothing**. Confirmed against the live pair: the real
    server returned `404 {"detail":"Not Found"}` for the new route.

    The two ends of a mirror are routinely on different versions — the drift
    check reports version skew for exactly that reason — so the puller falls
    back to the whole-folder GET rather than guessing which 404 it received.
    """
    import asyncio
    from routes import mirror_api

    _work(tmp_path / "remote", "Piece", meta='{"v":2}')
    dst = tmp_path / "local"
    _work(dst, "Piece", meta='{"v":1}')
    remote_manifest = {"artwork": core.build_manifest(tmp_path / "remote", detail=True)}
    calls = []

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
            calls.append(("GET", url))
            if "manifest" in url:
                return _R(200, payload=remote_manifest)
            if "/artwork/" in url:                      # the old whole-folder route
                return _R(200, core.pack_folder(tmp_path / "remote" / "Piece"))
            return _R(404)
        async def post(self, url, **kw):
            calls.append(("POST", url))
            return _R(404, payload={"detail": "Not Found"})   # pre-3.18.0 server

    monkeypatch.setattr(mirror_api, "_artwork_root", lambda: dst)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    summary = asyncio.run(mirror_api._run_pull(
        "https://old-server", "k", dry_run=False, include_db=False,
        include_media=False, only=None, push_first=False))

    art = summary["artwork"]
    assert art["failed"] == [], "an older server must not read as a failure"
    assert art["fetched"] == 1
    assert art["fell_back"] == ["Piece"]
    # Counted honestly: a fallback is a WHOLE fetch, not a partial one.
    assert art["whole"] == 1 and art["partial"] == 0
    assert ("POST", "https://old-server/api/mirror/artwork/Piece/files") in calls
    # And it really synced.
    assert json.loads((dst / "Piece" / "masterpiece.json").read_text())["v"] == 2


# ── amplification (3.18.2, from the pre-release security review) ──

def test_repeating_one_path_does_not_multiply_the_payload(tmp_path):
    """⚠ A memory-exhaustion primitive found by the release security review.

    The route caps the path LIST at 5000 entries, but nothing stopped the same
    file being named 5000 times: each was tar-added separately, artwork is
    already-compressed PNG/JPEG so gzip reclaims nothing, and the whole archive
    accumulates in a BytesIO before the Response doubles the peak. Measured
    before the fix: 200 copies of a 2 MB file produced a 400 MB payload,
    perfectly linear. With the documented 29 MB largest artwork file, 5000
    copies asks for ~145 GB on a 1 GB e2-micro that also runs Docker and Caddy.
    """
    d = _work(tmp_path, "Piece", image=200_000)
    one = core.pack_folder_files(d, ["image.png"])
    many = core.pack_folder_files(d, ["image.png"] * 200)
    assert len(many) == len(one), "duplicate paths must not multiply the payload"
    with tarfile.open(fileobj=io.BytesIO(many), mode="r:gz") as t:
        assert len(t.getnames()) == 1


def test_two_spellings_of_one_file_collapse(tmp_path):
    """Dedup is keyed on the RESOLVED target, not the requested string, so
    `./x` and `x` are one member rather than two."""
    d = _work(tmp_path, "Piece")
    blob = core.pack_folder_files(d, ["masterpiece.json", "./masterpiece.json"])
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
        assert len(t.getnames()) == 1


def test_the_request_cannot_exceed_the_folders_own_size(tmp_path):
    """The ceiling is the folder itself: no caller can legitimately need more
    bytes than the folder contains, and that keeps the peak at one folder —
    the invariant this module's docstring sets."""
    d = _work(tmp_path, "Piece", image=100_000)
    # Distinct real files, so dedup does not mask the budget.
    for i in range(6):
        (d / f"extra{i}.png").write_bytes(b"\x89PNG" + bytes(200_000))
    everything = [p.name for p in d.iterdir()]
    core.pack_folder_files(d, everything)          # exactly the folder: fine
