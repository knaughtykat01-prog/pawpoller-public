"""Remove hidden picture details before posting (spec 071, EXIFSTRIP).

A picture file carries more than the picture: GPS, the phone's make and model, dates, a real name typed into an art
app's "author" box, the folder it was saved in, and sometimes a whole second picture or a video clip tucked onto the
end. None of it shows on screen; anyone who downloads the posted file can read it.

This module makes a cleaned **copy** of each outgoing picture. The library file is only ever opened for reading.

How it cleans: a byte walker per format (JPEG, PNG/APNG, WebP, GIF) copies only a fixed **keep-list** of the blocks a
picture needs to show correctly (picture data, colour profile, animation control, required headers) and stops at the
format's own end. Anything else, including blocks nobody has named yet, is dropped. Picture data is copied
byte-for-byte, so art is never recompressed. Pillow is used for two things only: redrawing a picture stored rotated
(under a pixel cap), and the independent "does it still open at the same size" re-check of every copy.

Picture files are hostile input (they arrive from other sites and through the Inbox): every length is checked against
the bytes actually left, the number of blocks walked is capped, nothing compressed is ever decompressed, and any
surprise means "couldn't be cleaned". The picture then goes out as stored and the artist is told on that site's
result (FR-SEC-003). Cleaning never stops a post and nothing public here raises.

Public surface (contract: specs/071-exif-strip/contracts/metadata_strip.md):
    inspect(path) -> Inspection       read-only "what hidden details does this carry?" (FR-013); writes nothing
    has_metadata(path) -> bool
    clean_bytes(data) -> bytes | None  the walker on bytes already in memory (Discord's raw fallback)
    CleanRun                           one per posting run: cleans each source once, removes its copies on exit
    sweep_stale()                      start-up backstop for copies a crash left behind
"""
from __future__ import annotations

import binascii
import io
import logging
import os
import re
import shutil
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Limits ───────────────────────────────────────────────────────────────────────────────────────────────────────────
# shortcut: input cap. The upload route caps at 50 MB; this also covers files that arrived another way.
MAX_READ_BYTES = 200 * 1024 * 1024
# shortcut: STRUCTURAL blocks walked per file (JPEG segments, PNG chunks other than IDAT/fdAT, WebP chunks, GIF
# blocks and frames). Picture-data runs don't count here: they are bounded by bytes (research R10).
MAX_BLOCKS = 10_000
# shortcut: picture-data units (IDAT/fdAT chunks, GIF data sub-blocks). Each consumes at least 2 bytes, so bytes bound
# them already; this keeps a 200 MB file of 1-byte sub-blocks from costing a minute of Python. A 300-frame 10 MB GIF
# is ~40 000 units, so normal files are nowhere near it.
MAX_DATA_UNITS = 2_000_000
# shortcut: the rotation redraw decodes the picture; ~2 × 40 MP × 4 bytes ≈ 320 MB peak. Raise with seat memory.
REDRAW_MAX_PIXELS = 40_000_000
# shortcut: one decoded picture at a time across every run (a scheduled run and a manual one on a 2 GB seat).
_REDRAW_LOCK = threading.Semaphore(1)

_PREFIX = "pp-clean-"          # every folder this module makes; the only folders it ever removes
_WM_PREFIX = "wm_"             # posting/watermark.py's temps, removed with the run
_STALE_SECONDS = 24 * 3600

NOTE_KEPT = ("Sent with its hidden details (location, device…) still in it: PawPoller couldn't remove them "
             "from this file.")
NOTE_SIDEWAYS = ("Hidden details removed, but this picture is too large to turn upright here, so it may show "
                 "sideways on sites that ignore its rotation.")

KINDS = frozenset({"location", "device", "date", "author", "software", "appended", "unknown"})

# Extensions that say "this is meant to be a picture" when the content isn't one of the four cleanable formats
# (HEIC, AVIF, TIFF, …): those are reported as not cleaned, never silently passed as clean.
_PICTURE_EXTS = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".apng", ".gif", ".webp", ".heic", ".heif", ".avif",
                 ".bmp", ".tif", ".tiff", ".jxl", ".jp2", ".dng"}
_ISO_PICTURE_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"}


def is_enabled(settings: dict | None = None) -> bool:
    """The one reading of the setting, for every caller (CleanRun, the pre-check, Discord): absent = on, and only a
    real False turns it off, so a stray stored `0` or `""` can't read as off in one place and on in another."""
    s = settings
    if s is None:
        try:
            import config
            s = config.get_settings()
        except Exception:  # noqa: BLE001 — unreadable settings: keep the safe default (on)
            s = {}
    return s.get("posting_strip_metadata", True) is not False


class _Kept(Exception):
    """Internal: this file can't be cleaned safely (malformed, a limit hit, an unsupported variant)."""


@dataclass(frozen=True)
class Inspection:
    format: str | None            # 'jpeg' 'png' 'webp' 'gif', else None
    kinds: frozenset              # kinds of detail found, never values; {'unknown'} when it can't be read
    orientation: int              # EXIF IFD0 0x0112, 1 when absent
    pixels: int | None            # from the header, never from decoding
    cleanable: bool
    picture: bool = True          # False for a PDF / audio / text file: never touched


@dataclass
class Outcome:
    path: str                     # the file to send
    state: str                    # clean · cleaned · kept · off · not_picture
    kinds: frozenset = frozenset()
    note: str | None = None       # a plain sentence for that site's result, when the artist must be told


@dataclass
class _Walk:
    fmt: str
    out: bytes
    kinds: set = field(default_factory=set)
    orientation: int = 1
    size: tuple | None = None     # (width, height) as stored
    animated: bool = False
    deep: bool = False            # 16-bit PNG: a redraw would reduce it to 8-bit
    lossless: bool = False        # VP8L WebP
    exif_blobs: list = field(default_factory=list)   # raw TIFF payloads seen (the re-check compares them)
    scans: int = 0                # JPEG SOS count (REDRAW_MAX_SCANS)


# ── Small readers ────────────────────────────────────────────────────────────────────────────────────────────────────

def _be16(d, i):
    if i + 2 > len(d):
        raise _Kept("short read")
    return (d[i] << 8) | d[i + 1]


def _be32(d, i):
    if i + 4 > len(d):
        raise _Kept("short read")
    return struct.unpack_from(">I", d, i)[0]


def _le32(d, i):
    if i + 4 > len(d):
        raise _Kept("short read")
    return struct.unpack_from("<I", d, i)[0]


def _le24(d, i):
    if i + 3 > len(d):
        raise _Kept("short read")
    return d[i] | (d[i + 1] << 8) | (d[i + 2] << 16)


def _sniff(head: bytes) -> str | None:
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def _looks_like_other_picture(head: bytes, name: str) -> bool:
    """A picture format this module can't clean (HEIC/AVIF/TIFF/BMP/JXL…), by content or extension."""
    if head[4:8] == b"ftyp" and head[8:12] in _ISO_PICTURE_BRANDS:
        return True
    if head[:4] in (b"II*\x00", b"MM\x00*") or head[:2] == b"BM" or head[:2] == b"\xff\x0a" \
            or head[:12] == b"\x00\x00\x00\x0cJXL \r\n\x87\n":
        return True
    return Path(name).suffix.lower() in _PICTURE_EXTS


# ── What a dropped block held (kinds only, never values) ─────────────────────────────────────────────────────────────

_IFD0_KINDS = {
    0x010F: "device", 0x0110: "device",                    # Make, Model
    0x0131: "software", 0x000B: "software",                # Software, ProcessingSoftware
    0x0132: "date",                                        # DateTime
    0x013B: "author", 0x8298: "author", 0x9C9D: "author",  # Artist, Copyright, XPAuthor
    0x83BB: "author",                                      # IPTC
    0x010E: "unknown", 0x9C9B: "unknown", 0x9C9C: "unknown", 0x9C9E: "unknown", 0x9C9F: "unknown",
    0x02BC: "software",                                    # XMP inside EXIF
    0x8825: "location",                                    # GPS IFD pointer
}
_EXIFIFD_KINDS = {
    0x9003: "date", 0x9004: "date", 0x9010: "date", 0x9011: "date",          # DateTimeOriginal/Digitized, offsets
    0xA430: "author", 0xA431: "device", 0xA432: "device", 0xA433: "device",
    0xA434: "device", 0xA435: "device",                                      # owner, serials, lens
    0x927C: "device", 0x9286: "unknown",                                     # MakerNote, UserComment
}


def _tiff_info(t: bytes) -> tuple[set, int]:
    """(kinds, orientation) from a TIFF/EXIF payload. Bounded: a handful of IFDs, ≤ 512 entries each, every offset
    checked. An unreadable payload is 'unknown' with orientation 1 (posts as it would have before)."""
    try:
        if t[:4] == b"II*\x00":
            e = "<"
        elif t[:4] == b"MM\x00*":
            e = ">"
        else:
            return {"unknown"}, 1
        u16 = lambda i: struct.unpack_from(e + "H", t, i)[0]   # noqa: E731
        u32 = lambda i: struct.unpack_from(e + "I", t, i)[0]   # noqa: E731

        def entries(off):
            if off < 8 or off + 2 > len(t):
                raise _Kept("ifd offset")
            n = u16(off)
            if n > 512 or off + 2 + n * 12 + 4 > len(t):
                raise _Kept("ifd size")
            for k in range(n):
                p = off + 2 + k * 12
                yield u16(p), u16(p + 2), p + 8
            # (next-IFD pointer read by the caller)

        kinds: set = set()
        orientation = 1
        ifd0 = u32(4)
        exif_ifd = None
        for tag, typ, vp in entries(ifd0):
            if tag == 0x0112 and typ == 3:
                orientation = u16(vp)
            elif tag == 0x8769:
                exif_ifd = u32(vp)
            elif tag in _IFD0_KINDS:
                kinds.add(_IFD0_KINDS[tag])
        n0 = u16(ifd0)
        ifd1 = u32(ifd0 + 2 + n0 * 12)
        if ifd1:
            kinds.add("appended")                          # IFD1 = an embedded thumbnail picture
        if exif_ifd:
            for tag, _typ, _vp in entries(exif_ifd):
                if tag in _EXIFIFD_KINDS:
                    kinds.add(_EXIFIFD_KINDS[tag])
        return kinds, (orientation if 1 <= orientation <= 8 else 1)
    except (_Kept, struct.error, IndexError):
        return {"unknown"}, 1


_XMP_KINDS = (
    ("location", (b"gpslatitude", b"gpslongitude", b"exif:gps", b"photoshop:city", b"iptc4xmpcore:location")),
    ("device", (b"tiff:make", b"tiff:model", b"aux:serialnumber", b"aux:lens", b"exifex:lens", b"exifex:bodyserial")),
    ("date", (b"xmp:createdate", b"xmp:modifydate", b"xmp:metadatadate", b"photoshop:datecreated")),
    ("author", (b"dc:creator", b"dc:rights", b"xmprights", b"photoshop:authorsposition", b"tiff:artist")),
    ("software", (b"xmp:creatortool", b"stevt:softwareagent", b"softwareagent", b"photoshop:history")),
)


def _xmp_kinds(b: bytes) -> set:
    low = bytes(b[:4 * 1024 * 1024]).lower()
    found = {k for k, pats in _XMP_KINDS if any(p in low for p in pats)}
    return found or {"unknown"}


# ── The Orientation-only note (built from scratch, never copied) ─────────────────────────────────────────────────────

def _orientation_tiff(orientation: int) -> bytes:
    return (b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 1)
            + struct.pack("<HHIHH", 0x0112, 3, 1, orientation, 0) + struct.pack("<I", 0))


# ── JPEG ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

_JPEG_KEEP = {0xDB, 0xC4, 0xCC, 0xDD, 0xDC,                       # DQT DHT DAC DRI DNL
              0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}   # frame headers
_SOF = _JPEG_KEEP - {0xDB, 0xC4, 0xCC, 0xDD, 0xDC}
# The end of a scan: an FF followed by a marker byte (not 00 stuffing, not RST0-7, not an FF fill byte).
_SCAN_END = re.compile(rb"\xff[^\x00\xd0-\xd7\xff]")
_FILL = re.compile(rb"\xff*")
# A redraw decodes every scan; libjpeg sets no scan limit, so thousands of tiny progressive scans would burn minutes.
# Above this the picture keeps its rotation note instead (impl review Low). Real progressive JPEGs use about 10.
REDRAW_MAX_SCANS = 100


def _walk_jpeg(d: bytes, note: bytes | None) -> _Walk:
    w = _Walk("jpeg", b"")
    out = bytearray(b"\xff\xd8")
    insert_at = 2
    n = len(d)
    i = 2
    blocks = 0
    while True:
        if i >= n or d[i] != 0xFF:
            raise _Kept("expected a marker")
        i = _FILL.match(d, i).end()                 # fill bytes before a marker: one C-speed skip (impl review Low)
        if i >= n:
            raise _Kept("truncated")
        m = d[i]
        i += 1
        blocks += 1
        if blocks > MAX_BLOCKS:
            raise _Kept("too many blocks")
        if m == 0xD9:                               # EOI: stop; everything after is dropped (FR-SEC-001)
            out += b"\xff\xd9"
            if i < n:
                w.kinds.add("appended")
            break
        if 0xD0 <= m <= 0xD7 or m == 0x01:          # RST / TEM outside a scan: no length
            out += bytes((0xFF, m))
            continue
        if m == 0xD8 or m in (0xDE, 0xDF) or m == 0xC8 or 0xF0 <= m <= 0xFD or m < 0xC0:
            raise _Kept("unsupported marker")       # SOI again, hierarchical DHP/EXP, JPG/JPGn, reserved
        length = _be16(d, i)
        if length < 2:
            raise _Kept("segment length")
        end = i + length
        if end > n:
            raise _Kept("segment past the end")
        payload = d[i + 2:end]
        seg = bytes((0xFF, m)) + d[i:end]
        if m == 0xE0:
            if payload[:5] == b"JFIF\x00" and len(payload) >= 14:
                if payload[12] or payload[13] or len(payload) > 14:
                    w.kinds.add("appended")         # JFIF's own thumbnail can show the uncropped original
                jf = payload[:12] + b"\x00\x00"
                out += b"\xff\xe0" + struct.pack(">H", 16) + jf
                if insert_at == 2:
                    insert_at = len(out)
            elif payload[:5] == b"JFXX\x00":
                w.kinds.add("appended")
            else:
                w.kinds.add("unknown")
        elif m == 0xE1:
            if payload[:6] == b"Exif\x00\x00":
                k, o = _tiff_info(payload[6:])
                w.kinds |= k
                w.exif_blobs.append(bytes(payload[6:]))
                if o != 1:
                    w.orientation = o
            elif payload.startswith(b"http://ns.adobe.com/"):
                w.kinds |= _xmp_kinds(payload)
            else:
                w.kinds.add("unknown")
        elif m == 0xE2:
            if payload[:12] == b"ICC_PROFILE\x00":
                out += seg
            elif payload[:4] == b"MPF\x00":
                w.kinds.add("appended")             # the multi-picture index: its second picture sits after EOI
            else:
                w.kinds.add("unknown")
        elif m == 0xED:
            w.kinds.add("author")                   # APP13 Photoshop / IPTC: by-line, captions, places
        elif m == 0xEE:
            if payload[:5] == b"Adobe":
                out += seg                          # colour transform: CMYK/YCCK JPEGs need it
            else:
                w.kinds.add("unknown")
        elif 0xE3 <= m <= 0xEF or m == 0xFE:
            w.kinds.add("unknown")                  # other APPn (C2PA/JUMBF in APP11, …) and COM
        elif m in _JPEG_KEEP:
            out += seg
            if m in _SOF and len(payload) >= 5:
                w.size = (_be16(payload, 3), _be16(payload, 1))
        elif m == 0xDA:                             # SOS: header, then entropy-coded data copied byte-for-byte
            w.scans += 1
            out += seg
            hit = _SCAN_END.search(d, end)
            if not hit:
                raise _Kept("scan runs off the end")
            out += d[end:hit.start()]
            i = hit.start()
            continue
        else:
            raise _Kept("unknown marker")
        i = end
    if w.size is None:
        raise _Kept("no frame header")
    if note is not None:
        app1 = b"Exif\x00\x00" + note
        out[insert_at:insert_at] = b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
    w.out = bytes(out)
    return w


# ── PNG ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

_PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"cHRM", b"gAMA", b"iCCP", b"sBIT", b"sRGB", b"cICP",
             b"mDCv", b"cLLi", b"bKGD", b"pHYs", b"acTL", b"fcTL", b"fdAT"}
_PNG_BENIGN_DROPS = {b"hIST", b"sPLT"}
_PNG_TEXT_KINDS = {b"author": "author", b"copyright": "author", b"software": "software",
                   b"creation time": "date", b"source": "device"}


def _png_text_kinds(ctype: bytes, data: bytes) -> set:
    key = data.split(b"\x00", 1)[0].strip().lower()
    if key.startswith(b"xml:com.adobe.xmp"):
        if ctype == b"iTXt":
            parts = data.split(b"\x00", 2)
            if len(parts) == 3 and parts[2][:1] == b"\x00":          # compression flag 0: plain text
                return _xmp_kinds(parts[2])
        return {"unknown"}                                           # compressed: never inflated (FR-SEC-004)
    return {_PNG_TEXT_KINDS.get(key, "unknown")}


def _walk_png(d: bytes, note: bytes | None) -> _Walk:
    w = _Walk("png", b"")
    out = bytearray(d[:8])
    n = len(d)
    i = 8
    blocks = units = 0
    first = True
    after_ihdr = None
    while True:
        if i + 8 > n:
            raise _Kept("truncated")
        length = _be32(d, i)
        ctype = d[i + 4:i + 8]
        if length > n - i - 12:
            raise _Kept("chunk past the end")
        if not all(65 <= c <= 90 or 97 <= c <= 122 for c in ctype):
            raise _Kept("bad chunk type")
        end = i + 12 + length
        data = d[i + 8:i + 8 + length]
        if ctype in (b"IDAT", b"fdAT"):
            units += 1
            if units > MAX_DATA_UNITS:
                raise _Kept("too many data chunks")
        else:
            blocks += 1
            if blocks > MAX_BLOCKS:
                raise _Kept("too many blocks")
        if first:
            if ctype != b"IHDR" or length < 13:
                raise _Kept("IHDR first")
            w.size = (_be32(data, 0), _be32(data, 4))
            w.deep = data[8] == 16
            first = False
        if ctype in _PNG_KEEP:
            out += d[i:end]
            if ctype == b"IHDR":
                after_ihdr = len(out)
            elif ctype == b"acTL":
                w.animated = True
            elif ctype == b"IEND":
                if end < n:
                    w.kinds.add("appended")
                break
        elif ctype in (b"tEXt", b"zTXt", b"iTXt"):
            w.kinds |= _png_text_kinds(ctype, data)
        elif ctype == b"eXIf":
            k, o = _tiff_info(data)
            w.kinds |= k
            w.exif_blobs.append(bytes(data))
            if o != 1:
                w.orientation = o
        elif ctype == b"tIME":
            w.kinds.add("date")
        elif ctype in _PNG_BENIGN_DROPS:
            pass
        elif not (ctype[0] & 0x20):
            raise _Kept("unknown critical chunk")   # e.g. Apple CgBI: dropping it would break the picture
        else:
            w.kinds.add("unknown")                  # private ancillary chunk (editing apps keep paths in these)
        i = end
    if note is not None and after_ihdr is not None:
        chunk = b"eXIf" + note
        out[after_ihdr:after_ihdr] = (struct.pack(">I", len(note)) + chunk
                                      + struct.pack(">I", binascii.crc32(chunk) & 0xFFFFFFFF))
    w.out = bytes(out)
    return w


# ── WebP ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

_WEBP_KEEP = {b"VP8 ", b"VP8L", b"VP8X", b"ICCP", b"ANIM", b"ALPH"}     # ANMF is walked, not copied whole


def _walk_webp(d: bytes, note: bytes | None) -> _Walk:
    w = _Walk("webp", b"")
    n = len(d)
    if n < 12:
        raise _Kept("truncated")
    riff_end = 8 + _le32(d, 4)
    if riff_end > n:
        raise _Kept("RIFF size past the end")
    if riff_end < n:
        w.kinds.add("appended")
    chunks: list[bytearray] = []
    vp8x_index = None
    i = 12
    blocks = 0
    while i < riff_end:
        if i + 8 > riff_end:
            raise _Kept("truncated chunk header")
        fourcc = d[i:i + 4]
        size = _le32(d, i + 4)
        body_end = i + 8 + size
        if body_end > riff_end:
            raise _Kept("chunk past the RIFF end")
        end = min(body_end + (size & 1), riff_end)
        blocks += 1
        if blocks > MAX_BLOCKS:
            raise _Kept("too many blocks")
        body = d[i + 8:body_end]
        if fourcc == b"ANMF":
            # A frame: a 16-byte header, then its own chunks. Only the frame's picture data is kept; an unknown
            # chunk inside a frame is dropped like one outside it (FR-SEC-002, tasks review Low).
            if size < 16:
                raise _Kept("short ANMF")
            inner = bytearray(body[:16])
            j = 16
            while j < size:
                if j + 8 > size:
                    raise _Kept("truncated frame chunk")
                icc, isz = body[j:j + 4], _le32(body, j + 4)
                iend = j + 8 + isz
                if iend > size:
                    raise _Kept("frame chunk past the frame")
                blocks += 1
                if blocks > MAX_BLOCKS:
                    raise _Kept("too many blocks")
                if icc in (b"ALPH", b"VP8 ", b"VP8L"):
                    inner += body[j:iend] + (b"\x00" if isz & 1 else b"")
                    if icc == b"VP8L":
                        w.lossless = True
                else:
                    w.kinds.add("unknown")
                j = min(iend + (isz & 1), size)
            w.animated = True
            chunks.append(bytearray(b"ANMF" + struct.pack("<I", len(inner)) + inner))
        elif fourcc in _WEBP_KEEP:
            chunk = bytearray(d[i:body_end])
            if size & 1:
                chunk += b"\x00"                    # odd-sized chunks keep their pad byte
            if fourcc == b"VP8X":
                if size < 10:
                    raise _Kept("short VP8X")
                chunk[8] &= ~0x0C & 0xFF            # clear the EXIF (0x08) and XMP (0x04) flags
                w.size = (1 + _le24(body, 4), 1 + _le24(body, 7))
                vp8x_index = len(chunks)
            elif fourcc == b"VP8L":
                w.lossless = True
            elif fourcc == b"ANIM":
                w.animated = True
            chunks.append(chunk)
        elif fourcc == b"EXIF":
            t = body[6:] if body[:6] == b"Exif\x00\x00" else body
            k, o = _tiff_info(t)
            w.kinds |= k
            w.exif_blobs.append(bytes(t))
            if o != 1:
                w.orientation = o
        elif fourcc == b"XMP ":
            w.kinds |= _xmp_kinds(body)
        else:
            w.kinds.add("unknown")
        i = end
    if not chunks:
        raise _Kept("no picture chunk")
    if note is not None and vp8x_index is not None:
        chunks[vp8x_index][8] |= 0x08
        chunks.append(bytearray(b"EXIF" + struct.pack("<I", len(note)) + note + (b"\x00" if len(note) & 1 else b"")))
    body = b"".join(bytes(c) for c in chunks)
    w.out = b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body
    return w


# ── GIF ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

_GIF_APPS_KEPT = {b"NETSCAPE2.0", b"ANIMEXTS1.0", b"ICCRGBG1012"}


def _gif_sub_blocks(d: bytes, j: int, budget: list) -> int:
    """Index just past a data sub-block chain starting at *j* (its 0 terminator included)."""
    n = len(d)
    while True:
        if j >= n:
            raise _Kept("sub-blocks run off the end")
        size = d[j]
        if size == 0:
            return j + 1
        j += 1 + size
        budget[0] += 1
        if budget[0] > MAX_DATA_UNITS:
            raise _Kept("too many sub-blocks")


def _walk_gif(d: bytes, note: bytes | None) -> _Walk:   # noqa: ARG001 — GIF has no EXIF rotation
    w = _Walk("gif", b"")
    n = len(d)
    if n < 13:
        raise _Kept("truncated")
    w.size = (d[6] | (d[7] << 8), d[8] | (d[9] << 8))
    i = 13
    if d[10] & 0x80:
        i += 3 * (2 << (d[10] & 7))
    if i > n:
        raise _Kept("colour table past the end")
    out = bytearray(d[:i])
    blocks = frames = 0
    budget = [0]
    while True:
        if i >= n:
            raise _Kept("no trailer")
        blocks += 1
        if blocks > MAX_BLOCKS:
            raise _Kept("too many blocks")
        b = d[i]
        if b == 0x3B:
            out += b"\x3b"
            if i + 1 < n:
                w.kinds.add("appended")
            break
        if b == 0x2C:
            if i + 10 > n:
                raise _Kept("short image descriptor")
            j = i + 10
            if d[i + 9] & 0x80:
                j += 3 * (2 << (d[i + 9] & 7))
            j += 1                                  # LZW minimum code size
            if j > n:
                raise _Kept("short image")
            end = _gif_sub_blocks(d, j, budget)
            out += d[i:end]
            frames += 1
            i = end
            continue
        if b != 0x21 or i + 2 > n:
            raise _Kept("unknown block")
        label = d[i + 1]
        end = _gif_sub_blocks(d, i + 2, budget)
        if label == 0xF9:
            out += d[i:end]                          # Graphic Control: delays, disposal, transparency
        elif label == 0xFF:
            app = d[i + 3:i + 14] if i + 3 < n and d[i + 2] == 11 else b""
            if app in _GIF_APPS_KEPT:
                out += d[i:end]
            elif app == b"XMP DataXMP":
                w.kinds |= _xmp_kinds(d[i + 14:end])
            else:
                w.kinds.add("unknown")
        else:
            w.kinds.add("unknown")                   # Comment (FE), Plain Text (01), anything else
        i = end
    w.animated = frames > 1
    w.out = bytes(out)
    return w


_WALKERS = {"jpeg": _walk_jpeg, "png": _walk_png, "webp": _walk_webp, "gif": _walk_gif}


def _walk(fmt: str, data: bytes, note: bytes | None = None) -> _Walk:
    try:
        return _WALKERS[fmt](data, note)
    except _Kept:
        raise
    except (IndexError, struct.error, ValueError) as e:
        raise _Kept(type(e).__name__) from e


# ── Redraw (rotation only) and the re-check ──────────────────────────────────────────────────────────────────────────

def _pillow_size(data: bytes) -> tuple | None:
    """Pillow's own reading of the size, header only (no pixel decode): the independent half of the re-check."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return tuple(im.size)
    except Exception:  # noqa: BLE001 — anything Pillow can't open fails the re-check
        return None


def _can_redraw(w: _Walk) -> bool:
    """A still, 8-bit picture of known size within the pixel and scan limits (the pre-check asks the same)."""
    return bool(not w.animated and not w.deep and w.size and w.size[0] * w.size[1] <= REDRAW_MAX_PIXELS
                and w.scans <= REDRAW_MAX_SCANS)


def _redraw(w: _Walk, data: bytes) -> bytes:
    """Turn a rotated still picture upright, at high quality, colour profile kept, no other details written."""
    from PIL import Image, ImageOps
    with _REDRAW_LOCK:
        with Image.open(io.BytesIO(data)) as im:
            if im.size[0] * im.size[1] > REDRAW_MAX_PIXELS:
                raise _Kept("too many pixels")
            if im.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                raise _Kept(f"mode {im.mode}")       # CMYK etc.: keep the pixels, use the rotation note
            icc = im.info.get("icc_profile")
            up = ImageOps.exif_transpose(im)
            buf = io.BytesIO()
            kw = {"icc_profile": icc} if icc else {}
            if w.fmt == "jpeg":
                if up.mode not in ("RGB", "L"):
                    up = up.convert("RGB")
                up.save(buf, "JPEG", quality=95, **kw)
            elif w.fmt == "png":
                up.save(buf, "PNG", **kw)
            else:
                up.save(buf, "WEBP", lossless=True, **kw) if w.lossless else up.save(buf, "WEBP", quality=95, **kw)
            return buf.getvalue()


def _recheck(out: bytes, fmt: str, expected: tuple, note: bytes | None) -> bool:
    """FR-SEC-011: the copy opens as a picture of the expected size, and walking it again finds nothing left, apart
    from an Orientation-only note byte-equal to the one this module wrote."""
    if _pillow_size(out) != tuple(expected):
        return False
    try:
        again = _walk(fmt, out)
    except _Kept:
        return False
    if again.kinds:
        return False
    return again.exif_blobs == ([note] if note is not None else [])


# ── Public: inspect / clean_bytes ────────────────────────────────────────────────────────────────────────────────────

def _read_head(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read(32)


def inspect(path) -> Inspection:
    """Read-only: which kinds of hidden detail *path* carries (never their values). Writes nothing, decompresses
    nothing. A picture that can't be read counts as carrying details ({'unknown'})."""
    p = str(path or "")
    try:
        if not p or not os.path.isfile(p):
            return Inspection(None, frozenset(), 1, None, False, picture=False)
        head = _read_head(p)
        fmt = _sniff(head)
        if fmt is None:
            if _looks_like_other_picture(head, p):
                return Inspection(None, frozenset({"unknown"}), 1, None, False)
            return Inspection(None, frozenset(), 1, None, False, picture=False)
        if os.path.getsize(p) > MAX_READ_BYTES:
            return Inspection(fmt, frozenset({"unknown"}), 1, None, False)
        with open(p, "rb") as f:
            data = f.read(MAX_READ_BYTES + 1)
        return _inspect_bytes(fmt, data)
    except OSError:
        return Inspection(None, frozenset({"unknown"}), 1, None, False)


def _inspect_bytes(fmt: str, data: bytes) -> Inspection:
    try:
        w = _walk(fmt, data)
    except _Kept:
        return Inspection(fmt, frozenset({"unknown"}), 1, None, False)
    px = w.size[0] * w.size[1] if w.size else None
    return Inspection(fmt, frozenset(w.kinds), w.orientation, px, True)


def precheck_note(path, settings: dict | None = None) -> str | None:
    """The pre-post check's warning for one picture (FR-SEC-003, FR-009): the sentence the post would show, or None.
    Read-only and file-free like inspect(); it predicts from the header, so a redraw failing later can still turn
    into the 'may show sideways' note at post time."""
    try:
        if not is_enabled(settings):
            return None
        p = str(path or "")
        if not p or not os.path.isfile(p):
            return None
        head = _read_head(p)
        fmt = _sniff(head)
        if fmt is None:
            return NOTE_KEPT if _looks_like_other_picture(head, p) else None
        if os.path.getsize(p) > MAX_READ_BYTES:
            return NOTE_KEPT
        with open(p, "rb") as f:
            data = f.read(MAX_READ_BYTES + 1)
        try:
            w = _walk(fmt, data)
        except _Kept:
            return NOTE_KEPT
        if w.orientation != 1 and not _can_redraw(w):
            return NOTE_SIDEWAYS
        return None
    except Exception:  # noqa: BLE001 — a check that can't run says nothing; the post still reports
        return None


def has_metadata(path) -> bool:
    """FR-013: does this picture carry hidden details? (For a later "location data will be removed" line.)"""
    return bool(inspect(path).kinds)


def clean_bytes(data: bytes) -> bytes | None:
    """The lossless walker on bytes already in memory (Discord's raw fallback). Never redraws: a rotated picture keeps
    an Orientation-only note. Returns None when the data can't be cleaned; the caller then sends no picture."""
    try:
        if not data or len(data) > MAX_READ_BYTES:
            return None
        fmt = _sniff(data[:32])
        if fmt is None:
            return None
        w = _walk(fmt, data)
        note = None
        if w.orientation != 1:
            note = _orientation_tiff(w.orientation)
            w = _walk(fmt, data, note)
        if w.size is None or not _recheck(w.out, fmt, w.size, note):
            return None
        return w.out
    except Exception:  # noqa: BLE001 — never raises (contract)
        return None


# ── The delete guard ─────────────────────────────────────────────────────────────────────────────────────────────────

def _temp_root() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def _ours(path, prefix: str, want_dir: bool) -> bool:
    """FR-SEC-006: True only for a folder/file directly under the system temp folder whose name starts with *prefix*,
    and not a link. Both sides resolved (Windows %TEMP% can be an 8.3 short name; /tmp can be a symlink)."""
    try:
        raw = Path(path)
        if raw.is_symlink():
            return False
        rp = raw.resolve()
        if rp.parent != _temp_root() or not rp.name.startswith(prefix):
            return False
        return rp.is_dir() if want_dir else rp.is_file()
    except (OSError, ValueError):
        return False


def _remove_dir(d) -> None:
    if not _ours(d, _PREFIX, want_dir=True):
        if os.path.exists(d):
            logger.warning("Picture clean-up skipped a folder that isn't one of its own")
        return
    d = Path(d).resolve()                           # Linux won't walk `..` through a folder that doesn't exist
    for attempt in (1, 2):                          # Windows antivirus can hold a new file for a moment
        try:
            shutil.rmtree(d)
            return
        except OSError:
            if attempt == 1:
                time.sleep(0.2)
    logger.warning("Picture clean-up couldn't remove a temporary folder; the start-up sweep will")


def _remove_temp_file(t) -> None:
    if not _ours(t, _WM_PREFIX, want_dir=False):
        return
    try:
        os.remove(Path(t).resolve())
    except OSError:
        pass


_TEMP_FOLDER = re.compile(r"pp-clean-[^\\/\s'\"]*[\\/]+")


def redact_temp_paths(text):
    """An error that names one of this module's copies (`...\\Temp\\pp-clean-ab12\\cover.jpg`) carries the OS user
    name; stored errors keep only the file's name (SEC071). Anything else, and None, passes through unchanged."""
    if not text or not isinstance(text, str) or _PREFIX not in text:
        return text
    roots = {tempfile.gettempdir()}
    try:
        roots.add(str(Path(tempfile.gettempdir()).resolve()))
    except OSError:
        pass
    # The temp root may hold spaces, so it is stripped as a literal, in each spelling an error message uses:
    # as is, with forward slashes, and doubled backslashes (str(OSError) shows the path's repr on Windows).
    for root in sorted(roots, key=len, reverse=True):
        root = root.rstrip("\\/")
        for r, sep in ((root, os.sep), (root.replace("\\", "/"), "/"), (root.replace("\\", "\\\\"), "\\\\")):
            text = text.replace(r + sep, "")
    return _TEMP_FOLDER.sub("", text)


def sweep_stale(max_age: float = _STALE_SECONDS) -> int:
    """Start-up backstop: remove `pp-clean-*` folders older than *max_age* (a crash, or a folder antivirus held).
    Same guard as the run's own clean-up. Never raises; returns how many it removed."""
    removed = 0
    try:
        root = _temp_root()
        cutoff = time.time() - max_age
        getuid = getattr(os, "getuid", None)     # POSIX only
        uid = getuid() if getuid else None
        with os.scandir(root) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False) if entry.name.startswith(_PREFIX) else None
                    # Linux /tmp is shared between users: only this user's own folders (tasks review Low).
                    if (st is not None and entry.is_dir(follow_symlinks=False) and st.st_mtime < cutoff
                            and (uid is None or st.st_uid == uid)):
                        _remove_dir(entry.path)
                        if not os.path.exists(entry.path):
                            removed += 1
                except OSError:
                    continue
    except Exception:  # noqa: BLE001 — never stops start-up
        logger.warning("Picture clean-up sweep didn't run")
    return removed


# ── CleanRun ─────────────────────────────────────────────────────────────────────────────────────────────────────────

class CleanRun:
    """One per posting run (artwork, story, post). Cleans each source picture once and reuses the copy for every
    site; removes every copy, and the run's watermark temps, on every way out of the `with` block.

    ⚠ `package.extra["library_path"]` (set by clean_package) is an absolute path holding the OS user name. It exists
    so posting records keep the library path (FR-SEC-008). Never forward `extra` wholesale to a site or a log.
    """

    def __init__(self, settings: dict | None = None):
        self._settings = settings
        self.enabled = True
        self.copies: dict[str, Outcome] = {}
        self.dirs: list[str] = []
        self._temps: list[str] = []

    def __enter__(self) -> "CleanRun":
        # Read once, when the run starts: a scheduled post reads it when it fires, not when it was scheduled.
        self.enabled = is_enabled(self._settings)
        return self

    def __exit__(self, *exc) -> None:          # never swallows an exception
        for d in self.dirs:
            try:
                _remove_dir(d)
            except Exception:  # noqa: BLE001 — __exit__ never raises
                pass
        for t in self._temps:
            try:
                _remove_temp_file(t)
            except Exception:  # noqa: BLE001
                pass
        self.dirs.clear()
        self._temps.clear()

    def add_temp(self, path) -> None:
        """A watermark temp (posting/watermark.py, `wm_` prefix) to remove when the run ends."""
        if path:
            self._temps.append(str(path))

    def clean(self, path) -> Outcome:
        p = str(path or "")
        if p in self.copies:
            return self.copies[p]
        try:
            o = self._clean(p)
        except Exception as e:  # noqa: BLE001 — cleaning never stops a post (FR-008)
            logger.info("Picture cleaning failed (%s)", type(e).__name__)
            o = Outcome(p, "kept", frozenset({"unknown"}), NOTE_KEPT)
        if o.state == "kept":
            logger.info("Picture details not removed (%s): %s", ",".join(sorted(o.kinds)) or "unknown",
                        Path(p).name)
        self.copies[p] = o
        return o

    def _clean(self, p: str) -> Outcome:
        if not p or not os.path.isfile(p):
            return Outcome(p, "not_picture")
        head = _read_head(p)
        fmt = _sniff(head)
        if fmt is None:
            if not _looks_like_other_picture(head, p):
                return Outcome(p, "not_picture")
            if not self.enabled:
                return Outcome(p, "off")
            return Outcome(p, "kept", frozenset({"unknown"}), NOTE_KEPT)
        if not self.enabled:
            return Outcome(p, "off")
        if os.path.getsize(p) > MAX_READ_BYTES:
            return Outcome(p, "kept", frozenset({"unknown"}), NOTE_KEPT)
        with open(p, "rb") as f:                    # the library file is only ever read (FR-SEC-006)
            data = f.read(MAX_READ_BYTES + 1)
        try:
            w = _walk(fmt, data)
        except _Kept:
            return Outcome(p, "kept", frozenset({"unknown"}), NOTE_KEPT)
        kinds = frozenset(w.kinds)
        note_text = None
        note = None
        out = w.out
        expected = w.size
        if w.orientation != 1:
            note = _orientation_tiff(w.orientation)
            try:
                # The cleaned bytes plus the Orientation-only note: what Pillow decodes for a redraw, so it never
                # parses the hostile file's own text blocks (tasks review, FR-SEC-004 defence in depth).
                noted = _walk(fmt, data, note).out
            except _Kept:
                return Outcome(p, "kept", kinds or frozenset({"unknown"}), NOTE_KEPT)
            redrawn = None
            if _can_redraw(w):
                try:
                    redrawn = _walk(fmt, _redraw(w, noted)).out
                except Exception:  # noqa: BLE001 — fall back to the rotation note
                    redrawn = None
            if redrawn is not None:
                out = redrawn
                note = None
                expected = (w.size[1], w.size[0]) if w.orientation >= 5 else w.size
            else:
                out = noted
                note_text = NOTE_SIDEWAYS
        if out == data:
            return Outcome(p, "clean", kinds)
        if expected is None or not _recheck(out, fmt, expected, note):
            return Outcome(p, "kept", kinds or frozenset({"unknown"}), NOTE_KEPT)
        d = tempfile.mkdtemp(prefix=_PREFIX)        # one private folder per source (FR-SEC-010)
        self.dirs.append(d)
        target = os.path.join(d, Path(p).name)      # the site sees the stored file's bare name
        with open(target, "xb") as f:
            f.write(out)
        return Outcome(target, "cleaned", kinds, note_text)

    def clean_package(self, package) -> None:
        """Swap the cleaned copies into an upload package (render and thumbnail), keeping the library path for the
        posting record and a plain note for that site's result."""
        extra = package.extra
        made = {o.path for o in self.copies.values() if o.state == "cleaned"}
        # The library path comes from the package itself, never from incoming extras (tasks review Low); a second
        # call on an already-swapped package keeps the first answer instead of recording a temp path.
        if package.file_path not in made or "library_path" not in extra:
            extra["library_path"] = package.file_path
        extra.pop("metadata_note", None)
        notes = []
        for attr in ("file_path", "thumbnail_path"):
            src = getattr(package, attr, None)
            if not src:
                continue
            o = self.clean(src)
            if o.state == "cleaned":
                setattr(package, attr, o.path)
            if o.note:
                notes.append(o.note)
        if notes:
            extra["metadata_note"] = " ".join(dict.fromkeys(notes))
