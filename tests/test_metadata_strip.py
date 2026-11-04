"""Spec 071 (EXIFSTRIP): the cleaner on its own: walkers, keep-lists, limits, rotation, CleanRun.

Every output is judged by `independent_kinds` (Pillow + raw byte searches), never by the module's own parser.
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import struct
import tempfile
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from posting import metadata_strip as ms
from tests import _metadata_fixtures as fx

MODULE_SRC = Path(ms.__file__).read_text(encoding="utf-8")


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """A private system temp folder for the test, so 'directly under gettempdir()' is observable and isolated."""
    root = tmp_path / "systemp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def _write(tmp_path, name, data):
    p = tmp_path / "library" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _clean(tmp_path, name, data, settings=None):
    """Clean one picture in a run; return (outcome, the copy's bytes or None, the run's leftover folders)."""
    src = _write(tmp_path, name, data)
    with ms.CleanRun(settings={} if settings is None else settings) as run:
        o = run.clean(src)
        out = Path(o.path).read_bytes() if o.state == "cleaned" else None
    return o, out


def _pp_dirs(root):
    return [p for p in Path(root).iterdir() if p.name.startswith("pp-clean-")]


# ── T003 JPEG ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_jpeg_drops_mpf_preview_and_appended_mp4(tmp_path, temp_root):
    src = fx.gps_jpeg()
    o, out = _clean(tmp_path, "piece.jpg", src)
    assert o.state == "cleaned", o
    assert fx.independent_kinds(out) == set()
    assert {"location", "device", "date", "author", "software", "appended"} <= o.kinds
    assert out.count(b"\xff\xd8") == 1 and out.endswith(b"\xff\xd9")      # the second picture and the clip are gone
    assert fx.jpeg_scan(out) == fx.jpeg_scan(src)                          # picture data byte-identical (FR-002)
    assert fx.pillow_icc(out) == fx.FAKE_ICC                               # colour profile kept
    assert fx.pillow_size(out) == fx.pillow_size(src)


def test_unknown_jpeg_app_segments_are_dropped(tmp_path, temp_root):
    src = fx.insert_after_soi(fx.plain_jpeg(), *(fx.seg(m, b"Inkwolf-app%x" % m) for m in range(0xE3, 0xF0)),
                              fx.seg(0xFE, b"Inkwolf"))
    o, out = _clean(tmp_path, "a.jpg", src)
    assert o.state == "cleaned"
    assert fx.independent_kinds(out) == set()
    assert not re.search(rb"\xff[\xe3-\xef\xfe]", out[:out.index(b"\xff\xda")])


def test_cmyk_jpeg_keeps_app14(tmp_path, temp_root):
    src = fx.cmyk_jpeg()
    o, out = _clean(tmp_path, "c.jpg", src)
    assert o.state == "cleaned"
    i = out.index(b"\xff\xee")
    assert out[i + 4:i + 9] == b"Adobe"                                     # APP14 kept: the colour transform
    assert fx.independent_kinds(out) == set()
    with Image.open(io.BytesIO(out)) as im:
        assert im.mode == "CMYK"


def test_jfif_thumbnail_is_removed(tmp_path, temp_root):
    o, out = _clean(tmp_path, "j.jpg", fx.jfif_thumb_jpeg())
    assert o.state == "cleaned" and "appended" in o.kinds
    app0 = out[2:2 + 2 + 16]
    assert app0[:4] == b"\xff\xe0\x00\x10" and app0[-2:] == b"\x00\x00"    # 16-byte JFIF, 0×0 thumbnail
    assert b"JFXX" not in out


def test_progressive_jpeg_drops_blocks_between_scans(tmp_path, temp_root):
    src = fx.progressive_jpeg_with_blocks_between_scans()
    o, out = _clean(tmp_path, "p.jpg", src)
    assert o.state == "cleaned"
    assert fx.independent_kinds(out) == set()
    assert out.count(b"\xff\xda") == src.count(b"\xff\xda")                # every scan kept
    with Image.open(io.BytesIO(out)) as im:
        im.load()                                                           # still decodes


def test_hierarchical_jpeg_is_kept_not_cleaned(tmp_path, temp_root):
    o, out = _clean(tmp_path, "h.jpg", fx.hierarchical_jpeg())
    assert o.state == "kept" and out is None and o.note == ms.NOTE_KEPT


def test_plain_jpeg_without_details_makes_no_copy(tmp_path, temp_root):
    src = fx.plain_jpeg()
    o, out = _clean(tmp_path, "clean.jpg", src)
    assert o.state == "clean" and o.path.endswith("clean.jpg") and "library" in o.path
    assert _pp_dirs(temp_root) == []


# ── T004 PNG ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_png_drops_bytes_after_iend(tmp_path, temp_root):
    o, out = _clean(tmp_path, "t.png", fx.png_with_text())
    assert o.state == "cleaned"
    assert out.endswith(b"IEND\xaeB`\x82")


def test_private_png_chunks_are_dropped(tmp_path, temp_root):
    src = fx.png_with_text()
    o, out = _clean(tmp_path, "t.png", src)
    assert fx.independent_kinds(out) == set()
    for gone in (b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME", b"prVt", b"hIST"):
        assert gone not in out, gone
    for kept in (b"iCCP", b"pHYs", b"IDAT"):
        assert kept in out, kept
    assert fx.pillow_icc(out) == fx.FAKE_ICC
    with Image.open(io.BytesIO(src)) as a, Image.open(io.BytesIO(out)) as b:
        assert a.tobytes() == b.tobytes()                                   # same pixels


def test_apng_and_gif_loop_survive(tmp_path, temp_root):
    src = fx.apng()
    o, out = _clean(tmp_path, "a.png", src)
    assert o.state == "cleaned"
    assert fx.independent_kinds(out) == set()
    for kept in (b"acTL", b"fcTL", b"fdAT"):
        assert out.count(kept) == src.count(kept)
    assert fx.gif_frame_count(out) == fx.gif_frame_count(src) == 3
    g = fx.gif_with_comment_xmp(frames=3)
    o2, gout = _clean(tmp_path, "g.gif", g)
    assert o2.state == "cleaned" and b"NETSCAPE2.0" in gout
    assert fx.gif_frame_count(gout) == 3


def test_unknown_critical_png_chunk_is_kept_not_cleaned(tmp_path, temp_root):
    o, out = _clean(tmp_path, "z.png", fx.png_unknown_critical())
    assert o.state == "kept" and out is None and o.note == ms.NOTE_KEPT


def test_big_animated_gif_and_many_idat_png_are_cleaned(tmp_path, temp_root):
    g = fx.big_animated_gif()
    o, out = _clean(tmp_path, "big.gif", g)
    assert o.state == "cleaned", o
    assert b"Inkwolf" not in out and fx.gif_frame_count(out) == 300
    p = fx.many_idat_png()
    assert p.count(b"IDAT") > ms.MAX_BLOCKS
    o2, out2 = _clean(tmp_path, "many.png", p)
    assert o2.state == "cleaned", o2
    assert b"Inkwolf" not in out2 and out2.count(b"IDAT") == p.count(b"IDAT")


# ── T005 WebP / GIF ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_webp_ignores_bytes_past_riff_size(tmp_path, temp_root):
    o, out = _clean(tmp_path, "w.webp", fx.webp_with_exif_xmp())
    assert o.state == "cleaned" and "appended" in o.kinds
    assert b"past-the-RIFF" not in out


@pytest.mark.parametrize("lossless,animated", [(False, False), (True, False), (False, True)])
def test_webp_flags_and_riff_size_are_right(tmp_path, temp_root, lossless, animated):
    src = fx.webp_with_exif_xmp(lossless=lossless, animated=animated)
    o, out = _clean(tmp_path, "w.webp", src)
    assert o.state == "cleaned", o
    assert fx.independent_kinds(out) == set()
    assert struct.unpack("<I", out[4:8])[0] == len(out) - 8
    names = [c for c, _ in fx.webp_chunks(out)]
    assert b"EXIF" not in names and b"XMP " not in names and b"ICCP" in names
    vp8x = out[out.index(b"VP8X") + 8]
    assert not vp8x & 0x0C and vp8x & 0x20                                  # EXIF/XMP flags off, ICC flag on
    assert fx.pillow_icc(out) == fx.FAKE_ICC
    assert fx.pillow_size(out) == fx.pillow_size(src)
    if animated:
        assert fx.gif_frame_count(out) == 2


def test_webp_frame_keeps_only_its_picture_chunks(tmp_path, temp_root):
    src = fx.webp_with_stray_frame_chunk()      # libwebp itself refuses this file; what matters is the copy
    assert b"Inkwolf!" in src
    o, out = _clean(tmp_path, "a.webp", src)
    assert o.state == "cleaned", o
    assert b"XYZW" not in out and fx.independent_kinds(out) == set()
    assert fx.gif_frame_count(out) == 2
    with Image.open(io.BytesIO(out)) as im:
        for k in range(2):
            im.seek(k)
            im.load()                                                       # every frame still decodes


def test_gif_drops_bytes_after_trailer(tmp_path, temp_root):
    o, out = _clean(tmp_path, "g.gif", fx.gif_with_comment_xmp())
    assert o.state == "cleaned"
    assert out.endswith(b"\x3b") and b"after-trailer" not in out


def test_gif_icc_extension_survives(tmp_path, temp_root):
    o, out = _clean(tmp_path, "g.gif", fx.gif_with_comment_xmp())
    assert b"ICCRGBG1012" in out and fx.FAKE_ICC[:40] in out
    assert b"XMP DataXMP" not in out and b"FAKEAPP1.00" not in out
    assert fx.independent_kinds(out) == set()


# ── T006 hostile input and limits ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(fx.hostile_inputs()))
def test_hostile_inputs_finish_fast_as_kept(tmp_path, temp_root, name):
    suffix, data = fx.hostile_inputs()[name]
    t0 = time.monotonic()
    o, out = _clean(tmp_path, "evil" + suffix, data)
    assert time.monotonic() - t0 < 3.0
    assert o.state == "kept" and out is None and o.note == ms.NOTE_KEPT
    assert ms.inspect(_write(tmp_path, "evil2" + suffix, data)).kinds == {"unknown"}
    assert ms.clean_bytes(data) is None


def test_module_never_decompresses():
    code = re.sub(r'"""[\s\S]*?"""', "", MODULE_SRC)                        # docstrings may say the word
    code = re.sub(r"#.*", "", code)
    assert not re.search(r"\b(zlib|gzip|lzma|bz2|zipfile)\b", code)
    assert ".decompress(" not in code and "decompressobj" not in code


def test_huge_rotated_picture_is_not_redrawn(tmp_path, temp_root, monkeypatch):
    monkeypatch.setattr(ms, "REDRAW_MAX_PIXELS", 100)                        # 64×48 is now "huge"
    monkeypatch.setattr(ms, "_redraw", lambda *a, **k: pytest.fail("redrawn above the pixel cap"))
    o, out = _clean(tmp_path, "r.jpg", fx.orientation_jpeg(6))
    assert o.state == "cleaned" and o.note == ms.NOTE_SIDEWAYS
    assert fx.independent_kinds(out, allow_orientation=True) == set()
    with Image.open(io.BytesIO(out)) as im:
        assert dict(im.getexif()) == {0x0112: 6}
    assert ms.precheck_note(_write(tmp_path, "r2.jpg", fx.orientation_jpeg(6)), {}) == ms.NOTE_SIDEWAYS


def test_over_read_cap_is_kept(tmp_path, temp_root, monkeypatch):
    monkeypatch.setattr(ms, "MAX_READ_BYTES", 100)
    o, _ = _clean(tmp_path, "big.jpg", fx.gps_jpeg())
    assert o.state == "kept" and o.note == ms.NOTE_KEPT


# ── T007 rotation ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_orientation_6_jpeg_is_cleaned_not_kept(tmp_path, temp_root):
    src = fx.orientation_jpeg(6)
    o, out = _clean(tmp_path, "r.jpg", src)
    assert o.state == "cleaned" and o.note is None, o
    assert fx.independent_kinds(out) == set()
    assert fx.pillow_size(out) == (48, 64)                                  # turned upright: width and height swap
    assert fx.pillow_icc(out) == fx.FAKE_ICC
    with Image.open(io.BytesIO(src)) as a:
        from PIL import ImageOps
        want = ImageOps.exif_transpose(a).convert("RGB").resize((6, 8))
    with Image.open(io.BytesIO(out)) as b:
        got = b.convert("RGB").resize((6, 8))
    diff = max(abs(x - y) for p, q in zip(want.getdata(), got.getdata()) for x, y in zip(p, q))
    assert diff < 40


def test_16bit_png_is_not_redrawn(tmp_path, temp_root, monkeypatch):
    monkeypatch.setattr(ms, "_redraw", lambda *a, **k: pytest.fail("16-bit PNG redrawn"))
    o, out = _clean(tmp_path, "d.png", fx.sixteen_bit_png(6))
    assert o.state == "cleaned" and o.note == ms.NOTE_SIDEWAYS
    assert out[24] == 16
    assert fx.independent_kinds(out, allow_orientation=True) == set()


def test_lossless_webp_redraw_stays_lossless(tmp_path, temp_root):
    o, out = _clean(tmp_path, "l.webp", fx.webp_with_exif_xmp(lossless=True, orientation=6, tail=False))
    assert o.state == "cleaned" and o.note is None, o
    assert b"VP8L" in out and b"VP8 " not in out
    assert fx.pillow_size(out) == (48, 64)
    assert fx.independent_kinds(out) == set()


def test_animated_pictures_are_never_redrawn(tmp_path, temp_root, monkeypatch):
    monkeypatch.setattr(ms, "_redraw", lambda *a, **k: pytest.fail("animation redrawn"))
    o, out = _clean(tmp_path, "a.png", fx.apng(orientation=6))
    assert o.state == "cleaned" and o.note == ms.NOTE_SIDEWAYS
    assert fx.independent_kinds(out, allow_orientation=True) == set()


def test_redraws_are_serialised(tmp_path, temp_root, monkeypatch):
    from PIL import ImageOps
    real = ImageOps.exif_transpose
    active, peak, lock = [0], [0], threading.Lock()

    def slow(im, *a, **k):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        try:
            return real(im, *a, **k)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(ImageOps, "exif_transpose", slow)
    srcs = [_write(tmp_path, "r%d.jpg" % k, fx.orientation_jpeg(6)) for k in range(3)]
    results = []

    def go(p):
        with ms.CleanRun(settings={}) as run:
            results.append(run.clean(p).state)

    ts = [threading.Thread(target=go, args=(p,)) for p in srcs]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results == ["cleaned"] * 3 and peak[0] == 1


# ── T008 inspect / clean_bytes / CleanRun ────────────────────────────────────────────────────────────────────────────

def test_inspect_writes_nothing_and_names_kinds_only(tmp_path, temp_root):
    p = _write(tmp_path, "g.jpg", fx.gps_jpeg(orientation=6))
    before = sorted(os.listdir(temp_root)), sorted(os.listdir(p.parent))
    ins = ms.inspect(p)
    assert (sorted(os.listdir(temp_root)), sorted(os.listdir(p.parent))) == before
    assert ins.format == "jpeg" and ins.cleanable and ins.orientation == 6 and ins.pixels == 64 * 48
    assert ins.kinds <= ms.KINDS and {"location", "author"} <= ins.kinds
    assert not any("Inkwolf" in k or "FakeCam" in k for k in ins.kinds)


def test_has_metadata_matches_inspect(tmp_path):
    assert ms.has_metadata(_write(tmp_path, "g.jpg", fx.gps_jpeg())) is True
    assert ms.has_metadata(_write(tmp_path, "c.jpg", fx.plain_jpeg())) is False
    assert ms.has_metadata(_write(tmp_path, "x.pdf", b"%PDF-1.4 Inkwolf")) is False


def test_clean_bytes_returns_none_when_uncleanable():
    assert ms.clean_bytes(b"") is None
    assert ms.clean_bytes(b"%PDF-1.4") is None
    assert ms.clean_bytes(fx.hierarchical_jpeg()) is None
    out = ms.clean_bytes(fx.gps_jpeg())
    assert out and fx.independent_kinds(out) == set()
    rot = ms.clean_bytes(fx.orientation_jpeg(6))                            # no redraw: the rotation note only
    assert rot and fx.independent_kinds(rot, allow_orientation=True) == set()


@pytest.mark.parametrize("name,data", [("a.pdf", b"%PDF-1.4\nInkwolf"), ("b.mp3", b"ID3\x04Inkwolf"),
                                       ("c.txt", b"Inkwolf"), ("d.mp4", fx.MP4_TAIL)])
def test_not_a_picture_is_never_touched(tmp_path, temp_root, name, data):
    o, out = _clean(tmp_path, name, data)
    assert o.state == "not_picture" and out is None and o.note is None
    assert ms.precheck_note(_write(tmp_path, "x" + name, data), {}) is None


@pytest.mark.parametrize("name,data", [
    ("a.heic", b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + bytes(32)),
    ("b.avif", b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1" + bytes(32)),
    ("c.jpg", b"not really a jpeg"),
])
def test_unsupported_format_is_kept_with_note(tmp_path, temp_root, name, data):
    o, out = _clean(tmp_path, name, data)
    assert o.state == "kept" and o.note == ms.NOTE_KEPT and out is None
    assert ms.precheck_note(_write(tmp_path, "x" + name, data), {}) == ms.NOTE_KEPT


def test_picture_without_details_makes_no_copy(tmp_path, temp_root):
    for name, data in (("a.jpg", fx.plain_jpeg()), ("b.png", fx.plain_png())):
        o, _ = _clean(tmp_path, name, data)
        assert o.state == "clean"
    assert _pp_dirs(temp_root) == []


def test_setting_off_sends_the_stored_file(tmp_path, temp_root):
    o, out = _clean(tmp_path, "g.jpg", fx.gps_jpeg(), settings={"posting_strip_metadata": False})
    assert o.state == "off" and o.path.endswith("g.jpg") and out is None
    for not_off in (0, "", "false", None):                                  # only a real False turns it off
        o2, _ = _clean(tmp_path, "g.jpg", fx.gps_jpeg(), settings={"posting_strip_metadata": not_off})
        assert o2.state == "cleaned"
    assert ms.precheck_note(_write(tmp_path, "h.heic", b"\x00\x00\x00\x18ftypheic" + bytes(40)),
                            {"posting_strip_metadata": False}) is None


def test_two_cover_jpgs_get_two_paths(tmp_path, temp_root):
    a = _write(tmp_path, "one/cover.jpg", fx.gps_jpeg())
    b = _write(tmp_path, "two/cover.jpg", fx.gps_jpeg(size=(32, 32)))
    with ms.CleanRun(settings={}) as run:
        oa, ob, oa2 = run.clean(a), run.clean(b), run.clean(a)
        assert oa.path != ob.path and oa2 is oa
        assert Path(oa.path).name == Path(ob.path).name == "cover.jpg"     # the bare name the site sees
        assert Path(oa.path).read_bytes() != Path(ob.path).read_bytes()
        assert len(run.dirs) == 2


def test_run_leaves_nothing_after_success_error_and_cancel(tmp_path, temp_root):
    import asyncio
    src = _write(tmp_path, "g.jpg", fx.gps_jpeg())
    wm = tempfile.mkstemp(prefix="wm_", suffix=".jpg")
    os.close(wm[0])
    with ms.CleanRun(settings={}) as run:
        run.clean(src)
        run.add_temp(wm[1])
    assert _pp_dirs(temp_root) == [] and not os.path.exists(wm[1])
    for exc in (RuntimeError("poster blew up"), asyncio.CancelledError()):
        with pytest.raises(type(exc)):
            with ms.CleanRun(settings={}) as run:
                run.clean(src)
                assert _pp_dirs(temp_root)
                raise exc
        assert _pp_dirs(temp_root) == []
    assert src.read_bytes() == fx.gps_jpeg()


def test_cleanup_refuses_a_library_path(tmp_path, temp_root):
    lib = _write(tmp_path, "piece.jpg", fx.gps_jpeg())
    inside = temp_root / "library-in-temp"
    inside.mkdir()
    (inside / "piece.jpg").write_bytes(b"art")
    fake = temp_root / "pp-clean-but-a-file"
    fake.write_bytes(b"art")
    with ms.CleanRun(settings={}) as run:
        run.dirs += [str(lib.parent), str(lib), str(inside), str(fake), str(temp_root)]
        run.add_temp(str(lib))
        run.add_temp(str(inside / "piece.jpg"))
    assert lib.exists() and (inside / "piece.jpg").exists() and fake.exists() and temp_root.exists()


def test_cleanup_guard_uses_resolved_paths(tmp_path, temp_root):
    with ms.CleanRun(settings={}) as run:
        o = run.clean(_write(tmp_path, "g.jpg", fx.gps_jpeg()))
        d = Path(run.dirs[0])
        run.dirs[0] = str(temp_root / "elsewhere" / ".." / d.name)          # an unresolved spelling of the same folder
        escape = temp_root / "pp-clean-x" / ".." / ".." / "library"
        run.dirs.append(str(escape))
    assert not d.exists()
    assert (tmp_path / "library" / "g.jpg").exists()
    assert o.state == "cleaned"


def test_rmtree_failure_is_retried_once_and_never_raises(tmp_path, temp_root, monkeypatch):
    real, calls = shutil.rmtree, []
    monkeypatch.setattr(ms.time, "sleep", lambda s: None)

    def flaky(p, *a, **k):
        calls.append(p)
        if len(calls) == 1:
            raise PermissionError("held by antivirus")
        return real(p, *a, **k)

    monkeypatch.setattr(ms.shutil, "rmtree", flaky)
    with ms.CleanRun(settings={}) as run:
        run.clean(_write(tmp_path, "g.jpg", fx.gps_jpeg()))
    assert len(calls) == 2 and _pp_dirs(temp_root) == []

    monkeypatch.setattr(ms.shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(PermissionError("held")))
    with ms.CleanRun(settings={}) as run:                                    # never raises
        run.clean(_write(tmp_path, "h.jpg", fx.gps_jpeg()))


def test_startup_sweep_removes_only_stale_pp_clean(temp_root):
    old, new, other = temp_root / "pp-clean-old", temp_root / "pp-clean-new", temp_root / "someone-else-old"
    for d in (old, new, other):
        d.mkdir()
        (d / "x.jpg").write_bytes(b"x")
    stale = time.time() - 2 * 86400
    os.utime(old, (stale, stale))
    os.utime(other, (stale, stale))
    assert ms.sweep_stale() == 1
    assert not old.exists() and new.exists() and other.exists()


@pytest.mark.parametrize("make", [fx.gps_jpeg, lambda: fx.orientation_jpeg(6), fx.png_with_text,
                                  lambda: fx.webp_with_exif_xmp(tail=False), fx.gif_with_comment_xmp],
                         ids=["plain-copy", "rotation-redraw", "png", "webp", "gif"])
def test_failed_recheck_falls_back_to_kept(tmp_path, temp_root, monkeypatch, make):
    data = make()
    monkeypatch.setattr(ms, "_pillow_size", lambda d: None)
    o, out = _clean(tmp_path, "g.bin", data)
    assert o.state == "kept" and out is None and o.note == ms.NOTE_KEPT and o.path.endswith("g.bin")
    assert _pp_dirs(temp_root) == []
    assert ms.clean_bytes(data) is None                                     # Discord's copy is re-checked too


def test_one_reading_of_the_setting():
    assert ms.is_enabled({}) is True
    assert ms.is_enabled({"posting_strip_metadata": True}) is True
    assert ms.is_enabled({"posting_strip_metadata": False}) is False
    for junk in (0, "", "false", None, [], "no"):
        assert ms.is_enabled({"posting_strip_metadata": junk}) is True


def test_clean_package_keeps_the_library_path_not_incoming_extras(tmp_path, temp_root):
    from posting.platforms.base import StoryUploadPackage
    src = _write(tmp_path, "g.jpg", fx.gps_jpeg())
    thumb = _write(tmp_path, "t.png", fx.png_with_text())
    pkg = StoryUploadPackage(story_name="Sample_Piece", chapter_index=0, chapter_title="", platform="fa",
                             title="Sample", description="", file_path=str(src), thumbnail_path=str(thumb),
                             extra={"library_path": "/somewhere/else.jpg", "metadata_note": "forged"})
    with ms.CleanRun(settings={}) as run:
        run.clean_package(pkg)
        assert pkg.extra["library_path"] == str(src)
        assert "metadata_note" not in pkg.extra
        assert Path(pkg.file_path).name == "g.jpg" and pkg.file_path != str(src)
        assert pkg.thumbnail_path != str(thumb)
        assert fx.independent_kinds(Path(pkg.file_path).read_bytes()) == set()
        assert fx.independent_kinds(Path(pkg.thumbnail_path).read_bytes()) == set()
        run.clean_package(pkg)                                              # a second call keeps the first answer
        assert pkg.extra["library_path"] == str(src)


def test_kept_picture_sets_the_package_note(tmp_path, temp_root):
    from posting.platforms.base import StoryUploadPackage
    src = _write(tmp_path, "z.png", fx.png_unknown_critical())
    pkg = StoryUploadPackage(story_name="Sample_Piece", chapter_index=0, chapter_title="", platform="fa",
                             title="Sample", description="", file_path=str(src))
    with ms.CleanRun(settings={}) as run:
        run.clean_package(pkg)
    assert pkg.file_path == str(src) and pkg.extra["metadata_note"] == ms.NOTE_KEPT


def test_kept_log_line_has_no_values_or_full_paths(tmp_path, temp_root, caplog):
    caplog.set_level(logging.INFO, logger="posting.metadata_strip")
    _clean(tmp_path, "evil.png", fx.png_unknown_critical())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "evil.png" in text and "Picture details not removed" in text
    assert str(tmp_path) not in text and "Inkwolf" not in text and "library" not in text


def test_library_file_opened_read_only(tmp_path, temp_root, monkeypatch):
    src = _write(tmp_path, "g.jpg", fx.gps_jpeg(orientation=6))
    modes = []
    real_open = open

    def spy(file, mode="r", *a, **k):
        modes.append((str(file), mode))
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(ms, "open", spy, raising=False)
    with ms.CleanRun(settings={}) as run:
        assert run.clean(src).state == "cleaned"
    ms.inspect(src)
    ms.precheck_note(src, {})
    assert {m for f, m in modes if f == str(src)} == {"rb"}
    assert all(m in ("rb", "xb") for _, m in modes)
