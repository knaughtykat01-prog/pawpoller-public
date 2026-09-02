"""Tests for server → desktop mirroring (Stage 1).

The emphasis here is deliberately lopsided. Manifest and diff logic gets
ordinary coverage; extraction safety and the database swap get most of the
file, because those two are the only places in this feature where a bug
destroys data rather than merely failing to copy it.
"""
from __future__ import annotations

import io
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest

from mirror import core


# ── Helpers ───────────────────────────────────────────────────

def _make_artwork(root: Path, name: str, files: dict[str, bytes]) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return folder


def _tar_with(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_db(path: Path, *, accounts: int = 3, with_session: bool = True) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE accounts (account_id INTEGER PRIMARY KEY, platform TEXT)")
    conn.executemany("INSERT INTO accounts VALUES (?, ?)",
                     [(i, f"p{i}") for i in range(1, accounts + 1)])
    if with_session:
        conn.execute("CREATE TABLE session_cache (id INTEGER PRIMARY KEY, sid TEXT)")
        conn.execute("INSERT INTO session_cache VALUES (1, 'live-inkbunny-sid')")
    conn.commit()
    conn.close()


# ── Manifests and digests ─────────────────────────────────────

class TestManifest:
    def test_digest_is_stable_across_calls(self, tmp_path):
        f = _make_artwork(tmp_path, "Piece", {"a.png": b"one", "b.json": b"two"})
        assert core.folder_manifest(f)["digest"] == core.folder_manifest(f)["digest"]

    def test_digest_changes_when_content_changes(self, tmp_path):
        f = _make_artwork(tmp_path, "Piece", {"a.png": b"one"})
        before = core.folder_manifest(f)["digest"]
        (f / "a.png").write_bytes(b"changed")
        assert core.folder_manifest(f)["digest"] != before

    def test_digest_changes_when_a_file_is_renamed(self, tmp_path):
        """Content-only hashing would call a rename identical. It isn't."""
        f = _make_artwork(tmp_path, "Piece", {"a.png": b"same"})
        before = core.folder_manifest(f)["digest"]
        (f / "a.png").rename(f / "b.png")
        assert core.folder_manifest(f)["digest"] != before

    def test_mtime_does_not_affect_the_digest(self, tmp_path):
        """Tar extraction rewrites mtime; if it counted, a pulled folder would
        never match the folder it was pulled from and would re-fetch forever."""
        f = _make_artwork(tmp_path, "Piece", {"a.png": b"one"})
        before = core.folder_manifest(f)["digest"]
        import os
        os.utime(f / "a.png", (0, 0))
        assert core.folder_manifest(f)["digest"] == before

    def test_bak_files_are_excluded(self, tmp_path):
        f = _make_artwork(tmp_path, "Piece", {"masterpiece.json": b"{}"})
        before = core.folder_manifest(f)["digest"]
        (f / f"masterpiece.json.bak.{int(time.time())}").write_bytes(b"{}")
        assert core.folder_manifest(f)["digest"] == before, \
            "per-device undo history must not churn the digest"

    def test_dotfiles_are_excluded(self, tmp_path):
        f = _make_artwork(tmp_path, "Piece", {"a.png": b"one"})
        before = core.folder_manifest(f)["digest"]
        (f / ".vault_key").write_bytes(b"secret")
        assert core.folder_manifest(f)["digest"] == before

    def test_vault_key_never_packs(self, tmp_path):
        """The one file whose leak moves decryption ability between machines."""
        f = _make_artwork(tmp_path, "Piece", {"a.png": b"one"})
        (f / ".vault_key").write_bytes(b"secret")
        payload = core.pack_folder(f)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            assert not any(".vault_key" in n for n in tar.getnames())

    def test_build_manifest_lists_folders(self, tmp_path):
        _make_artwork(tmp_path, "One", {"a.png": b"a"})
        _make_artwork(tmp_path, "Two", {"b.png": b"bb"})
        m = core.build_manifest(tmp_path)
        assert m["count"] == 2
        assert m["bytes"] == 3
        assert [f["name"] for f in m["folders"]] == ["One", "Two"]

    def test_missing_root_is_not_an_error(self, tmp_path):
        m = core.build_manifest(tmp_path / "nope")
        assert m["exists"] is False and m["folders"] == []


# ── Diffing ───────────────────────────────────────────────────

class TestDiff:
    def _man(self, entries):
        return {"folders": [{"name": n, "digest": d, "bytes": 10} for n, d in entries]}

    def test_missing_folders_are_fetched(self):
        plan = core.diff_manifests(self._man([("A", "1"), ("B", "2")]), self._man([("A", "1")]))
        assert plan["fetch"] == ["B"] and plan["missing"] == ["B"]

    def test_changed_folders_are_fetched(self):
        plan = core.diff_manifests(self._man([("A", "2")]), self._man([("A", "1")]))
        assert plan["fetch"] == ["A"] and plan["changed"] == ["A"]

    def test_identical_folders_are_skipped(self):
        plan = core.diff_manifests(self._man([("A", "1")]), self._man([("A", "1")]))
        assert plan["fetch"] == [] and plan["unchanged"] == ["A"]

    def _detailed(self, name, digest, files):
        return {"folders": [{"name": name, "digest": digest, "bytes": 10,
                             "files": [{"path": p, "sha256": h} for p, h in files]}]}

    def test_a_surplus_local_file_does_not_force_a_refetch(self):
        """The live case: Ms_Kristoff carries a legacy artwork.json from before
        the masterpiece.json rename. Under digest equality it reported
        "changed" after a *successful* pull and would have re-downloaded on
        every run forever while appearing to work."""
        remote = self._detailed("Ms_Kristoff", "R", [("image.png", "a"), ("masterpiece.json", "b")])
        local = self._detailed("Ms_Kristoff", "L", [("image.png", "a"), ("masterpiece.json", "b"),
                                                    ("artwork.json", "legacy")])
        plan = core.diff_manifests(remote, local)
        assert plan["fetch"] == []
        assert plan["unchanged"] == ["Ms_Kristoff"]
        assert plan["extra_local_files"] == {"Ms_Kristoff": ["artwork.json"]}

    def test_a_genuinely_differing_file_is_still_fetched(self):
        remote = self._detailed("Piece", "R", [("image.png", "NEW")])
        local = self._detailed("Piece", "L", [("image.png", "old"), ("extra.txt", "x")])
        assert core.diff_manifests(remote, local)["fetch"] == ["Piece"]

    def test_a_missing_file_is_still_fetched(self):
        remote = self._detailed("Piece", "R", [("image.png", "a"), ("new.json", "b")])
        local = self._detailed("Piece", "L", [("image.png", "a")])
        assert core.diff_manifests(remote, local)["fetch"] == ["Piece"]

    def test_without_per_file_detail_a_digest_mismatch_still_fetches(self):
        """No detail means surplus and divergence are indistinguishable, so the
        safe answer is to fetch rather than to assume."""
        plan = core.diff_manifests(self._man([("A", "2")]), self._man([("A", "1")]))
        assert plan["fetch"] == ["A"]

    def test_local_only_folders_are_never_deleted(self):
        """Standing project rule: art is never deleted. A folder the server
        lacks is equally consistent with 'removed there' and 'not yet pushed
        from here', and a one-way pull cannot tell those apart."""
        plan = core.diff_manifests(self._man([("A", "1")]), self._man([("A", "1"), ("Mine", "9")]))
        assert plan["local_only"] == ["Mine"]
        assert "Mine" not in plan["fetch"]
        assert "delete" not in plan


# ── Extraction safety ─────────────────────────────────────────

class TestSafeExtract:
    def test_plain_archive_extracts(self, tmp_path):
        payload = _tar_with([("Piece/a.png", b"data")])
        core.extract_bytes(payload, tmp_path)
        assert (tmp_path / "Piece" / "a.png").read_bytes() == b"data"

    def test_parent_traversal_is_rejected(self, tmp_path):
        payload = _tar_with([("../escaped.txt", b"pwned")])
        with pytest.raises(core.MirrorSecurityError):
            core.extract_bytes(payload, tmp_path / "dest")
        assert not (tmp_path / "escaped.txt").exists()

    def test_absolute_posix_path_is_rejected(self, tmp_path):
        payload = _tar_with([("/etc/passwd", b"pwned")])
        with pytest.raises(core.MirrorSecurityError):
            core.extract_bytes(payload, tmp_path / "dest")

    def test_windows_absolute_path_is_rejected(self, tmp_path):
        """The check this replaced tested for a leading "/" and for "..";
        a Windows drive path has neither."""
        payload = _tar_with([("C:\\Windows\\System32\\evil.dll", b"pwned")])
        with pytest.raises(core.MirrorSecurityError):
            core.extract_bytes(payload, tmp_path / "dest")

    def test_symlink_members_are_rejected(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        with pytest.raises(core.MirrorSecurityError):
            core.extract_bytes(buf.getvalue(), tmp_path / "dest")

    def test_hardlink_members_are_rejected(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("link")
            info.type = tarfile.LNKTYPE
            info.linkname = "target"
            tar.addfile(info)
        with pytest.raises(core.MirrorSecurityError):
            core.extract_bytes(buf.getvalue(), tmp_path / "dest")

    def test_nothing_is_written_when_a_member_is_unsafe(self, tmp_path):
        """Validation runs over every member before extraction starts, so a
        malicious archive cannot land its safe half first."""
        dest = tmp_path / "dest"
        payload = _tar_with([("Piece/good.png", b"ok"), ("../bad.txt", b"no")])
        with pytest.raises(core.MirrorSecurityError):
            core.extract_bytes(payload, dest)
        assert not (dest / "Piece" / "good.png").exists()


# ── Round trip ────────────────────────────────────────────────

class TestRoundTrip:
    def test_pack_then_extract_reproduces_the_digest(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        folder = _make_artwork(src, "Piece", {"a.png": b"one", "sub/b.json": b"{}"})
        core.extract_bytes(core.pack_folder(folder), dst)
        assert core.folder_manifest(dst / "Piece")["digest"] == core.folder_manifest(folder)["digest"]

    def test_a_pulled_folder_is_not_refetched(self, tmp_path):
        """The convergence property: pull once, and the next diff is empty."""
        src, dst = tmp_path / "src", tmp_path / "dst"
        dst.mkdir()
        folder = _make_artwork(src, "Piece", {"a.png": b"one"})
        core.extract_bytes(core.pack_folder(folder), dst)
        plan = core.diff_manifests(core.build_manifest(src), core.build_manifest(dst))
        assert plan["fetch"] == []


# ── Database snapshots ────────────────────────────────────────

class TestSnapshot:
    def test_snapshot_copies_the_data(self, tmp_path):
        src, dest = tmp_path / "src.db", tmp_path / "out.db"
        _make_db(src, accounts=5)
        info = core.snapshot_database(src, dest)
        assert info["bytes"] > 0
        conn = sqlite3.connect(str(dest))
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 5
        conn.close()

    def test_session_cache_is_stripped(self, tmp_path):
        """A server-created Inkbunny sid is IP-bound; shipping it to the desktop
        gets both installs presenting the same session."""
        src, dest = tmp_path / "src.db", tmp_path / "out.db"
        _make_db(src)
        core.snapshot_database(src, dest)
        conn = sqlite3.connect(str(dest))
        assert conn.execute("SELECT COUNT(*) FROM session_cache").fetchone()[0] == 0
        conn.close()

    def test_snapshot_includes_unflushed_wal_commits(self, tmp_path):
        """The bug this exists to avoid: in WAL mode a committed row can still
        live in the -wal file, so copying the .db alone loses recent commits."""
        src, dest = tmp_path / "src.db", tmp_path / "out.db"
        _make_db(src, accounts=1, with_session=False)
        live = sqlite3.connect(str(src))
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("INSERT INTO accounts VALUES (99, 'written-into-wal')")
        live.commit()
        try:
            core.snapshot_database(src, dest)
        finally:
            live.close()
        conn = sqlite3.connect(str(dest))
        got = conn.execute("SELECT platform FROM accounts WHERE account_id = 99").fetchone()
        conn.close()
        assert got is not None and got[0] == "written-into-wal"

    def test_snapshot_is_a_single_self_contained_file(self, tmp_path):
        """backup() inherits WAL from the source, but only the .db travels and
        only the .db is swapped in — anything left in a sidecar is dropped."""
        src, dest = tmp_path / "src.db", tmp_path / "out.db"
        _make_db(src, with_session=False)
        live = sqlite3.connect(str(src))
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("INSERT INTO accounts VALUES (42, 'x')")
        live.commit()
        live.close()

        core.snapshot_database(src, dest)
        conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode.lower() == "delete"
        assert not dest.with_name(dest.name + "-wal").exists()

    def test_verify_rejects_a_truncated_file(self, tmp_path):
        src, dest = tmp_path / "src.db", tmp_path / "out.db"
        _make_db(src)
        core.snapshot_database(src, dest)
        data = dest.read_bytes()
        dest.write_bytes(data[:len(data) // 2])
        with pytest.raises(sqlite3.DatabaseError):
            core.verify_snapshot(dest)


class TestPendingSwap:
    def test_no_pending_file_is_a_noop(self, tmp_path):
        db = tmp_path / "pawpoller.db"
        _make_db(db)
        assert core.apply_pending_snapshot(db) is None

    def test_pending_snapshot_replaces_the_database(self, tmp_path):
        db = tmp_path / "pawpoller.db"
        _make_db(db, accounts=2, with_session=False)
        incoming = tmp_path / "incoming.db"
        _make_db(incoming, accounts=7, with_session=False)
        incoming.rename(core.pending_snapshot_path(db))

        result = core.apply_pending_snapshot(db)
        assert result["applied"] is True and result["accounts"] == 7
        conn = sqlite3.connect(str(db))
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 7
        conn.close()

    def test_the_previous_database_is_preserved(self, tmp_path):
        """Never destroy: the desktop's own poll history exists nowhere else."""
        db = tmp_path / "pawpoller.db"
        _make_db(db, accounts=2, with_session=False)
        incoming = tmp_path / "incoming.db"
        _make_db(incoming, accounts=7, with_session=False)
        incoming.rename(core.pending_snapshot_path(db))

        result = core.apply_pending_snapshot(db)
        backup = Path(result["previous_database"])
        assert backup.exists()
        conn = sqlite3.connect(str(backup))
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2
        conn.close()

    def test_a_corrupt_pending_file_leaves_the_live_database_alone(self, tmp_path):
        db = tmp_path / "pawpoller.db"
        _make_db(db, accounts=2, with_session=False)
        core.pending_snapshot_path(db).write_bytes(b"this is not a database")

        result = core.apply_pending_snapshot(db)
        assert result["applied"] is False
        conn = sqlite3.connect(str(db))
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2, \
            "the live database must survive a bad snapshot untouched"
        conn.close()
        assert not core.pending_snapshot_path(db).exists(), "bad snapshot should be discarded"

    def test_stale_wal_is_removed_with_the_old_database(self, tmp_path):
        """A -wal left beside the new file is another database's write-ahead
        log; SQLite would try to recover it into the replacement."""
        db = tmp_path / "pawpoller.db"
        _make_db(db, accounts=2, with_session=False)
        db.with_name(db.name + "-wal").write_bytes(b"stale wal")
        db.with_name(db.name + "-shm").write_bytes(b"stale shm")
        incoming = tmp_path / "incoming.db"
        _make_db(incoming, accounts=7, with_session=False)
        incoming.rename(core.pending_snapshot_path(db))

        core.apply_pending_snapshot(db)
        assert not db.with_name(db.name + "-wal").exists()
        assert not db.with_name(db.name + "-shm").exists()

    def test_appdata_override_targets_another_install(self, tmp_path):
        """The seed runs from source but must write to the INSTALLED install's
        data dir. Uses a subprocess because config resolves paths at import."""
        import os
        import subprocess
        import sys
        env = dict(os.environ, PAWPOLLER_APPDATA_DIR=str(tmp_path / "Elsewhere"))
        out = subprocess.run(
            [sys.executable, "-c", "import config; print(config.DATA_DIR)"],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == str(tmp_path / "Elsewhere" / "data")

    def test_appdata_override_is_opt_in(self, tmp_path):
        """Unset, the default must not move -- this is a core path."""
        import os
        import subprocess
        import sys
        env = {k: v for k, v in os.environ.items() if k != "PAWPOLLER_APPDATA_DIR"}
        repo = Path(__file__).resolve().parent.parent
        out = subprocess.run(
            [sys.executable, "-c", "import config; print(config.DATA_DIR)"],
            cwd=str(repo), env=env, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == str(repo / "data")

    def test_pending_wal_contents_survive_the_swap(self, tmp_path):
        """A pending file staged in WAL mode keeps committed rows in its -wal.
        Moving only the .db would silently drop them."""
        import shutil as _shutil
        db = tmp_path / "pawpoller.db"
        _make_db(db, accounts=2, with_session=False)
        pending = core.pending_snapshot_path(db)
        _make_db(pending, accounts=3, with_session=False)

        # A clean close checkpoints and removes the -wal, so snapshot the
        # (.db, -wal) pair while a connection still holds it open and restore
        # them afterwards. That reproduces the real case -- an unclean exit
        # leaving committed rows in a -wal -- rather than a fabricated file.
        wal = pending.with_name(pending.name + "-wal")
        conn = sqlite3.connect(str(pending))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("INSERT INTO accounts VALUES (77, 'only-in-wal')")
        conn.commit()
        _shutil.copy2(pending, tmp_path / "db.copy")
        _shutil.copy2(wal, tmp_path / "wal.copy")
        conn.close()
        _shutil.copy2(tmp_path / "db.copy", pending)
        _shutil.copy2(tmp_path / "wal.copy", wal)

        assert wal.exists()
        bare = sqlite3.connect(f"file:{pending}?mode=ro&immutable=1", uri=True)
        assert bare.execute(
            "SELECT COUNT(*) FROM accounts WHERE account_id = 77").fetchone()[0] == 0, \
            "precondition: the row must live only in the -wal, not the .db"
        bare.close()

        result = core.apply_pending_snapshot(db)
        assert result["applied"] is True
        conn = sqlite3.connect(str(db))
        got = conn.execute("SELECT platform FROM accounts WHERE account_id = 77").fetchone()
        conn.close()
        assert got is not None and got[0] == "only-in-wal"
        assert not pending.with_name(pending.name + "-wal").exists()

    def test_staging_clears_a_previous_attempts_sidecars(self, tmp_path):
        """Writing only the .db leaves the last attempt's -wal/-shm beside a
        database they do not belong to. Seen for real: after re-staging, the
        sidecars on disk were 516 seconds older than the file next to them."""
        db = tmp_path / "pawpoller.db"
        _make_db(db, accounts=2, with_session=False)
        pending = core.pending_snapshot_path(db)
        pending.with_name(pending.name + "-wal").write_bytes(b"stale wal")
        pending.with_name(pending.name + "-shm").write_bytes(b"stale shm")

        payload = tmp_path / "incoming.db"
        _make_db(payload, accounts=9, with_session=False)
        check = core.stage_pending_snapshot(db, payload.read_bytes())

        assert check["ok"] is True and check["accounts"] == 9
        assert not pending.with_name(pending.name + "-wal").exists()
        assert not pending.with_name(pending.name + "-shm").exists()

    def test_staging_a_corrupt_payload_leaves_no_pending_file(self, tmp_path):
        """A bad file left in the slot is a trap for whoever reads the folder next."""
        db = tmp_path / "pawpoller.db"
        _make_db(db, accounts=2, with_session=False)
        check = core.stage_pending_snapshot(db, b"not a database at all")
        assert check["ok"] is False
        assert not core.pending_snapshot_path(db).exists()

    def test_swap_is_idempotent(self, tmp_path):
        db = tmp_path / "pawpoller.db"
        _make_db(db, accounts=2, with_session=False)
        incoming = tmp_path / "incoming.db"
        _make_db(incoming, accounts=7, with_session=False)
        incoming.rename(core.pending_snapshot_path(db))
        core.apply_pending_snapshot(db)
        assert core.apply_pending_snapshot(db) is None


# ── platform-independence (3.17.4) ───────────────────────────────

class TestForeignAbsolutePaths:
    r"""`safe_extract` must judge a member by BOTH path conventions.

    Its docstring claimed the resolve()-based check catches Windows absolute
    paths, but `Path.resolve()` is platform-dependent: on Linux
    `C:\Windows\evil.dll` is an ordinary filename containing backslashes, so
    it passed straight through — and the server is Linux. The existing
    `test_windows_absolute_path_is_rejected` encoded the Windows behaviour, so
    it went green on the dev box and red in CI.

    The mirror channel spans Windows↔Linux by design, so what is a harmless
    filename on the box unpacking an archive can be an absolute path on the box
    that produced it, or on the next one to unpack it.
    """

    @pytest.mark.parametrize("name", [
        "C:" + chr(92) + "Windows" + chr(92) + "System32" + chr(92) + "evil.dll",
        "C:/Windows/System32/evil.dll",
        "C:evil",                         # drive-RELATIVE; ntpath.isabs says False
        chr(92) + "server" + chr(92) + "share" + chr(92) + "x",
        "/etc/passwd",
        ".." + chr(92) + "escape",
    ])
    def test_it_is_rejected_regardless_of_host_platform(self, name):
        with pytest.raises(core.MirrorSecurityError):
            core._reject_foreign_absolute(name)

    @pytest.mark.parametrize("name", [
        "ok/file.txt", "a/b/c.json", "Some_Work/masterpiece.json",
        "art/piece.png", "nested/deep/thing",
    ])
    def test_ordinary_members_still_pass(self, name):
        core._reject_foreign_absolute(name)          # must not raise

    def test_the_extractor_actually_calls_it(self, tmp_path):
        """End to end through `safe_extract`, so the guard cannot be bypassed
        by a caller that forgets it."""
        payload = _tar_with([("C:" + chr(92) + "Windows" + chr(92) + "evil.dll", b"pwned")])
        with pytest.raises(core.MirrorSecurityError):
            core.extract_bytes(payload, tmp_path)
        assert not any(tmp_path.rglob("*evil*"))
