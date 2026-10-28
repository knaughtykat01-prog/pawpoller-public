"""Property tests: nothing from an upload or archive lands outside its folder (PROPTESTS).

The guards were tested with a handful of known-bad names (``../``, ``C:\\``). Here the names
are generated: dot-dots, both slash styles, drive letters, absolute paths, odd characters,
mixed in with the names the importer really wants.
"""
import io
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("hypothesis")
from hypothesis import given, settings, strategies as st  # noqa: E402

import config  # noqa: E402
import pb_import  # noqa: E402
import pb_upload  # noqa: E402
from mirror import core  # noqa: E402

part = st.one_of(st.sampled_from(["..", ".", "", "C:", "c:", "\\\\host", "~", "data", "submission",
                                  "files", "Partitions", "Network", "Cookies", "accounts.db", "a b", " x "]),
                 st.text(alphabet="abcXYZ09._-:~ ", min_size=1, max_size=10))
evil_name = st.tuples(st.lists(part, min_size=1, max_size=6), st.sampled_from(["/", "\\"]),
                      st.sampled_from(["", "/", "\\", "C:/", "C:\\", "//"])
                      ).map(lambda t: t[2] + t[1].join(t[0]))


def _all_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


@given(st.one_of(evil_name, st.text(max_size=40), st.none(), st.integers()))
def test_postybirb_file_lookup_never_leaves_the_folder(recorded):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "files"
        root.mkdir()
        (root / "pic.png").write_bytes(b"x")
        (Path(tmp) / "secret.txt").write_bytes(b"x")
        got = pb_import._inside(root, recorded)
        assert got is None or got.parent == root.resolve()


def _tar(names: list[str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for n in names:
            info = tarfile.TarInfo(n)
            info.size = 1
            t.addfile(info, io.BytesIO(b"x"))
    return buf.getvalue()


@settings(max_examples=100)
@given(st.lists(st.one_of(evil_name, st.just("ok/file.txt")), min_size=1, max_size=5))
def test_mirror_extract_never_writes_outside(names):
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "dest"
        try:
            core.extract_bytes(_tar(names), dest)
        except (core.MirrorSecurityError, OSError, ValueError, tarfile.TarError):
            pass
        outside = [p for p in _all_files(Path(tmp)) if dest.resolve() not in p.resolve().parents]
        assert not outside, outside


def _zip(names: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("PostyBirb/data/submissions.db", b"x")     # marks the PostyBirb folder
        for n in names:
            info = zipfile.ZipInfo("x")
            info.filename = n                                   # raw name, as a hostile zip would carry it
            z.writestr(info, b"x")
    return buf.getvalue()


@settings(max_examples=60)
@given(st.lists(st.one_of(evil_name, evil_name.map(lambda n: "PostyBirb/data/" + n),
                          evil_name.map(lambda n: "PostyBirb/submission/files/" + n),
                          evil_name.map(lambda n: "app/Partitions/" + n)), min_size=1, max_size=6, unique=True))
def test_postybirb_upload_unpacks_only_inside_its_session(names):
    tmp = tempfile.mkdtemp()
    saved = config.DATA_DIR
    try:
        config.DATA_DIR = Path(tmp) / "data"
        data = _zip(names)
        uid = pb_upload.start()
        fid = pb_upload.add_file(uid, len(data))["file"]
        pb_upload.write_piece(uid, fid, 0, data)
        try:
            pb_upload.take(uid)
        except pb_upload.Refused:
            pass
        session = (pb_upload.root() / uid).resolve()
        for p in _all_files(Path(tmp)):
            assert session in p.resolve().parents, p
            rel = p.resolve().relative_to(session).as_posix()
            assert rel == "session.json" or rel.startswith(("x/docs/", "x/app/")), rel
    finally:
        config.DATA_DIR = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_generators_do_produce_escapes():
    """The properties above mean little if no generated name ever tries to escape."""
    from hypothesis import find
    assert ".." in find(evil_name, lambda n: ".." in n)
    assert find(evil_name, lambda n: n.startswith(("/", "\\", "C:"))) is not None
