"""Test pictures for spec 071 (EXIFSTRIP), built in the tests: Pillow plus hand-built bytes, never a real photo.

Every value is fake and neutral: GPS (0, 0), the placeholder author `Inkwolf`, a camera called `FakeCam`. The judge,
`independent_kinds`, deliberately does NOT use `posting.metadata_strip`: it reads the output with Pillow and with raw
byte searches for the fixtures' own values, so a blind spot in the cleaner's parser can't also hide in the tests
(research R11).
"""
from __future__ import annotations

import io
import os
import struct
import zlib

from PIL import Image

FAKE_ICC = b"\x00\x00\x02\x0cFAKEICCPROFILE-v1" + bytes(range(256)) * 2
FAKE_VALUES = (b"FakeCam", b"FakeModel", b"Inkwolf", b"FakePaint", b"2020:01:01")

XMP = (b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
       b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
       b'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
       b'xmp:CreatorTool="FakePaint 1.0"><dc:creator>Inkwolf</dc:creator></rdf:Description></rdf:RDF>'
       b'</x:xmpmeta><?xpacket end="w"?>')


# ── TIFF / EXIF ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _ascii(tag, s):
    v = s.encode() + b"\x00"
    return (tag, 2, v, len(v))


def _short(tag, v):
    return (tag, 3, struct.pack("<H", v), 1)


def _rationals(tag, *nums):
    return (tag, 5, b"".join(struct.pack("<II", n, 1) for n in nums), len(nums))


def _ifd(entries, base, next_off=0):
    n = len(entries)
    data_off = base + 2 + 12 * n + 4
    head = struct.pack("<H", n)
    data = b""
    for tag, typ, val, count in sorted(entries):
        if len(val) <= 4:
            head += struct.pack("<HHI", tag, typ, count) + val.ljust(4, b"\x00")
        else:
            head += struct.pack("<HHII", tag, typ, count, data_off + len(data))
            data += val + (b"\x00" if len(val) % 2 else b"")
    return head + struct.pack("<I", next_off) + data


def build_tiff(ifd0, gps=None, exif=None, ifd1=None) -> bytes:
    ifd0 = list(ifd0)
    if gps:
        ifd0.append((0x8825, 4, b"\x00" * 4, 1))
    if exif:
        ifd0.append((0x8769, 4, b"\x00" * 4, 1))
    off = 8 + len(_ifd(ifd0, 8))
    subs, blobs = {}, []
    for tag, ents in ((0x8825, gps), (0x8769, exif)):
        if ents:
            subs[tag] = off
            b = _ifd(ents, off)
            blobs.append(b)
            off += len(b)
    ifd1_off = off if ifd1 else 0
    if ifd1:
        blobs.append(_ifd(ifd1, off))
    ifd0 = [(t, ty, struct.pack("<I", subs[t]) if t in subs else v, c) for t, ty, v, c in ifd0]
    return b"II*\x00" + struct.pack("<I", 8) + _ifd(ifd0, 8, ifd1_off) + b"".join(blobs)


def gps_tiff(orientation: int | None = None, thumbnail: bool = False) -> bytes:
    ifd0 = [_ascii(0x010F, "FakeCam"), _ascii(0x0110, "FakeModel"), _ascii(0x0132, "2020:01:01 00:00:00"),
            _ascii(0x013B, "Inkwolf"), _ascii(0x0131, "FakePaint 1.0")]
    if orientation:
        ifd0.append(_short(0x0112, orientation))
    gps = [_ascii(1, "N"), _rationals(2, 0, 0, 0), _ascii(3, "E"), _rationals(4, 0, 0, 0)]
    exif = [_ascii(0x9003, "2020:01:01 00:00:00")]
    ifd1 = [_short(0x0103, 6)] if thumbnail else None
    return build_tiff(ifd0, gps=gps, exif=exif, ifd1=ifd1)


def orientation_only_tiff(orientation: int) -> bytes:
    return build_tiff([_short(0x0112, orientation), _ascii(0x010F, "FakeCam")])


# ── JPEG ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def seg(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + struct.pack(">H", len(payload) + 2) + payload


def _picture(size=(64, 48), mode="RGB"):
    """A non-symmetric picture: left half red, right half blue, top strip green (so a rotation is visible)."""
    w, h = size
    im = Image.new(mode, size, (200, 30, 30) if mode == "RGB" else 60)
    if mode == "RGB":
        im.paste((30, 30, 200), (w // 2, 0, w, h))
        im.paste((30, 200, 30), (0, 0, w, max(1, h // 6)))
    return im


def plain_jpeg(size=(64, 48), icc=True, progressive=False, mode="RGB", **kw) -> bytes:
    buf = io.BytesIO()
    extra = {"icc_profile": FAKE_ICC} if icc else {}
    _picture(size, "RGB").convert(mode).save(buf, "JPEG", quality=90, progressive=progressive, **extra, **kw)
    return buf.getvalue()


def insert_after_soi(jpeg: bytes, *segments: bytes) -> bytes:
    return jpeg[:2] + b"".join(segments) + jpeg[2:]


MP4_TAIL = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"Inkwolf-motion-clip" + bytes(64)


def gps_jpeg(size=(64, 48), orientation: int | None = None, icc=True, appended=True) -> bytes:
    """GPS + device + date + author in EXIF, XMP author, IPTC, COM, an MPF index, an embedded second picture with its
    own GPS after EOI, then a motion-photo MP4."""
    base = plain_jpeg(size, icc=icc)
    out = insert_after_soi(
        base,
        seg(0xE1, b"Exif\x00\x00" + gps_tiff(orientation, thumbnail=True)),
        seg(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + XMP),
        seg(0xED, b"Photoshop 3.0\x008BIM\x04\x04\x00\x00\x00\x00\x00\x0bInkwolf-by"),
        seg(0xFE, b"Inkwolf comment"),
        seg(0xE2, b"MPF\x00II*\x00" + bytes(24)),
        seg(0xEB, b"JP\x00\x00c2pa-Inkwolf"),
    )
    if appended:
        second = insert_after_soi(plain_jpeg((16, 16), icc=False),
                                  seg(0xE1, b"Exif\x00\x00" + gps_tiff()))
        out += second + MP4_TAIL
    return out


def jfif_thumb_jpeg() -> bytes:
    """A JFIF APP0 carrying its own 2×2 thumbnail, plus a JFXX extension thumbnail."""
    base = plain_jpeg(icc=False)
    assert base[2:4] == b"\xff\xe0"
    app0_len = struct.unpack(">H", base[4:6])[0]
    rest = base[4 + app0_len:]
    jfif = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01" + b"\x02\x02" + bytes(12)
    return b"\xff\xd8" + seg(0xE0, jfif) + seg(0xE0, b"JFXX\x00\x10" + bytes(40)) + rest


def progressive_jpeg_with_blocks_between_scans() -> bytes:
    """A progressive JPEG with a COM and an APP1 XMP put between its first and second scans."""
    d = plain_jpeg((64, 48), icc=True, progressive=True)
    sos = d.index(b"\xff\xda")
    nxt = d.index(b"\xff\xda", sos + 2)
    # Put the junk right before the second SOS (after any tables that precede it).
    return d[:nxt] + seg(0xFE, b"Inkwolf between scans") + seg(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + XMP) \
        + d[nxt:]


def cmyk_jpeg() -> bytes:
    d = plain_jpeg(icc=False, mode="CMYK")
    assert b"Adobe" in d
    return insert_after_soi(d, seg(0xE1, b"Exif\x00\x00" + gps_tiff()))


def hierarchical_jpeg() -> bytes:
    return insert_after_soi(plain_jpeg(icc=False), seg(0xDE, bytes(8)))


def orientation_jpeg(orientation: int, size=(64, 48)) -> bytes:
    return insert_after_soi(plain_jpeg(size, icc=True), seg(0xE1, b"Exif\x00\x00" + gps_tiff(orientation)))


def jpeg_scan(d: bytes) -> bytes:
    """The main picture's entropy-coded data: first SOS through its EOI."""
    s = d.index(b"\xff\xda")
    return d[s:d.index(b"\xff\xd9", s) + 2]


# ── PNG ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def png_chunk(ctype: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)


def _insert_after_ihdr(png: bytes, *chunks: bytes) -> bytes:
    end = 8 + 12 + struct.unpack(">I", png[8:12])[0]
    return png[:end] + b"".join(chunks) + png[end:]


def _insert_before_iend(png: bytes, *chunks: bytes) -> bytes:
    i = png.rindex(b"IEND") - 4
    return png[:i] + b"".join(chunks) + png[i:]


def png_with_text(orientation: int | None = None) -> bytes:
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    info.add_text("Author", "Inkwolf")
    info.add_text("Software", "FakePaint 1.0")
    info.add_text("Comment", "Inkwolf note", zip=True)
    info.add_itxt("XML:com.adobe.xmp", XMP.decode())
    buf = io.BytesIO()
    _picture().save(buf, "PNG", pnginfo=info, icc_profile=FAKE_ICC, dpi=(72, 72))
    d = _insert_after_ihdr(buf.getvalue(), png_chunk(b"eXIf", gps_tiff(orientation)))
    d = _insert_before_iend(d, png_chunk(b"tIME", b"\x07\xe4\x01\x01\x00\x00\x00"),
                            png_chunk(b"prVt", b"C:/Users/Inkwolf/art/piece.kra"),
                            png_chunk(b"hIST", bytes(8)))
    return d + b"Inkwolf-after-IEND"


def plain_png(size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    _picture(size).save(buf, "PNG")
    return buf.getvalue()


def apng(frames=3, orientation: int | None = None) -> bytes:
    ims = [_picture().rotate(0) for _ in range(frames)]
    for k, im in enumerate(ims):
        im.paste((k * 60 % 255, 0, 0), (0, 0, 8, 8))
    buf = io.BytesIO()
    ims[0].save(buf, "PNG", save_all=True, append_images=ims[1:], loop=0, duration=100)
    d = buf.getvalue()
    assert b"acTL" in d
    extra = [png_chunk(b"tEXt", b"Author\x00Inkwolf")]
    if orientation:
        extra.append(png_chunk(b"eXIf", orientation_only_tiff(orientation)))
    return _insert_after_ihdr(d, *extra)


def sixteen_bit_png(orientation=6) -> bytes:
    im = Image.new("I;16", (32, 24))
    im.paste(40000, (0, 0, 16, 24))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    d = buf.getvalue()
    assert d[24] == 16          # IHDR bit depth
    return _insert_after_ihdr(d, png_chunk(b"eXIf", gps_tiff(orientation)))


def png_unknown_critical() -> bytes:
    return _insert_after_ihdr(plain_png(), png_chunk(b"ZzZz", b"critical"),
                              png_chunk(b"tEXt", b"Author\x00Inkwolf"))


def many_idat_png(chunks=10_001) -> bytes:
    w, h = 200, 200
    raw = b"".join(b"\x00" + os.urandom(w) for _ in range(h))
    z = zlib.compress(raw, 1)
    step = max(1, len(z) // chunks)
    parts = [z[k:k + step] for k in range(0, len(z), step)]
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr) + png_chunk(b"tEXt", b"Author\x00Inkwolf")
            + b"".join(png_chunk(b"IDAT", p) for p in parts) + png_chunk(b"IEND", b""))


# ── WebP ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def webp_with_exif_xmp(lossless=False, animated=False, orientation: int | None = None, tail=True) -> bytes:
    buf = io.BytesIO()
    kw = dict(exif=gps_tiff(orientation), xmp=XMP, icc_profile=FAKE_ICC, lossless=lossless, quality=90)
    if animated:
        ims = [_picture(), _picture().transpose(Image.Transpose.FLIP_LEFT_RIGHT)]
        ims[0].save(buf, "WEBP", save_all=True, append_images=ims[1:], duration=100, loop=0, **kw)
    else:
        _picture().save(buf, "WEBP", **kw)
    d = buf.getvalue()
    assert b"VP8X" in d
    return d + (b"Inkwolf-past-the-RIFF-end" if tail else b"")


def webp_with_stray_frame_chunk() -> bytes:
    """An animated WebP whose first ANMF frame carries an unknown chunk holding a fake author."""
    d = webp_with_exif_xmp(animated=True, tail=False)
    i = d.index(b"ANMF")
    size = struct.unpack("<I", d[i + 4:i + 8])[0]
    stray = b"XYZW" + struct.pack("<I", 8) + b"Inkwolf!"
    body = d[i + 8:i + 8 + 16] + stray + d[i + 8 + 16:i + 8 + size]
    anmf = b"ANMF" + struct.pack("<I", len(body)) + body
    rest = d[12:i] + anmf + d[i + 8 + size + (size & 1):]
    return b"RIFF" + struct.pack("<I", len(rest) + 4) + b"WEBP" + rest


def webp_chunks(d: bytes) -> list[tuple[bytes, int]]:
    out, i, end = [], 12, 8 + struct.unpack("<I", d[4:8])[0]
    while i < end:
        cc, size = d[i:i + 4], struct.unpack("<I", d[i + 4:i + 8])[0]
        out.append((cc, size))
        i += 8 + size + (size & 1)
    return out


# ── GIF ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def sub_blocks(data: bytes) -> bytes:
    return b"".join(bytes((len(data[k:k + 255]),)) + data[k:k + 255] for k in range(0, len(data), 255)) + b"\x00"


def gif_with_comment_xmp(frames=2) -> bytes:
    ims = []
    for k in range(frames):
        im = _picture()
        im.paste((255, 255 - 80 * k, 40 * k), (0, 0, 16 + 8 * k, 16))
        ims.append(im.convert("P"))
    buf = io.BytesIO()
    ims[0].save(buf, "GIF", save_all=True, append_images=ims[1:], loop=0, duration=100, comment=b"Inkwolf")
    d = buf.getvalue()
    assert d.endswith(b"\x3b") and b"NETSCAPE2.0" in d
    xmp_ext = b"\x21\xff\x0b" + b"XMP DataXMP" + sub_blocks(XMP)
    icc_ext = b"\x21\xff\x0b" + b"ICCRGBG1012" + sub_blocks(FAKE_ICC)
    other = b"\x21\xff\x0b" + b"FAKEAPP1.00" + sub_blocks(b"Inkwolf-app")
    plain_text = b"\x21\x01" + sub_blocks(bytes(12) + b"Inkwolf")
    return d[:-1] + xmp_ext + icc_ext + other + plain_text + b"\x3b" + b"Inkwolf-after-trailer"


def gif_frame_count(d: bytes) -> int:
    with Image.open(io.BytesIO(d)) as im:
        return getattr(im, "n_frames", 1)


def big_animated_gif(frames=300, side=120) -> bytes:
    ims = [Image.frombytes("L", (side, side), os.urandom(side * side)) for _ in range(frames)]
    buf = io.BytesIO()
    ims[0].save(buf, "GIF", save_all=True, append_images=ims[1:], loop=0, duration=40, comment=b"Inkwolf")
    return buf.getvalue()


# ── Hostile inputs ───────────────────────────────────────────────────────────────────────────────────────────────────

def hostile_inputs() -> dict[str, tuple[str, bytes]]:
    """name → (file suffix, bytes). Each must come out 'kept', fast, without decompressing anything."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = png_chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 0, 0, 0, 0))
    lsd = b"GIF89a" + struct.pack("<HH", 8, 8) + b"\x00\x00\x00"
    return {
        "jpeg_segment_length_under_2": (".jpg", b"\xff\xd8\xff\xe1\x00\x01" + bytes(100)),
        "jpeg_segment_past_the_end": (".jpg", b"\xff\xd8\xff\xe1\xff\xf0Exif\x00\x00Inkwolf"),
        "jpeg_scan_without_end": (".jpg", plain_jpeg(icc=False)[:-2] + b"Inkwolf"),
        "png_2gb_chunk": (".png", sig + struct.pack(">I", 0x7FFFFFFF) + b"tEXt" + b"Author\x00Inkwolf"),
        "png_20000_tiny_chunks": (".png", sig + ihdr + png_chunk(b"prVt", b"") * 20_000 + png_chunk(b"IEND", b"")),
        "webp_riff_past_the_end": (".webp", b"RIFF" + struct.pack("<I", 10 ** 6) + b"WEBP" + b"VP8 "
                                   + struct.pack("<I", 4) + bytes(4)),
        "gif_endless_sub_blocks": (".gif", lsd + b"\x21\xfe" + b"\x01A" * 200_000),
    }


# ── The independent judge ────────────────────────────────────────────────────────────────────────────────────────────

_RAW_MARKERS = (b"<x:xmpmeta", b"http://ns.adobe.com/xap/1.0/", b"ftyp", b"8BIM") + FAKE_VALUES


def independent_kinds(data: bytes, allow_orientation: bool = False) -> set[str]:
    """What hidden details are still in *data*, judged without posting.metadata_strip. Empty = clean."""
    found = {m.decode("latin1") for m in _RAW_MARKERS if m in data}
    if not allow_orientation and b"Exif\x00\x00" in data:
        found.add("Exif header")
    try:
        with Image.open(io.BytesIO(data)) as im:
            ex = im.getexif()
            tags = set(ex.keys()) - ({0x0112} if allow_orientation else set())
            if tags:
                found.add("exif tags %s" % sorted(tags))
            if ex.get_ifd(0x8825):
                found.add("gps ifd")
            for key in ("xmp", "comment", "Author", "Software", "Comment", "XML:com.adobe.xmp"):
                if im.info.get(key):
                    found.add("info:" + key)
            if "exif" in im.info and not allow_orientation:
                found.add("info:exif")
    except Exception as e:  # noqa: BLE001
        found.add("pillow could not open: %s" % type(e).__name__)
    return found


def pillow_size(data: bytes):
    with Image.open(io.BytesIO(data)) as im:
        return im.size


def pillow_icc(data: bytes):
    with Image.open(io.BytesIO(data)) as im:
        return im.info.get("icc_profile")
