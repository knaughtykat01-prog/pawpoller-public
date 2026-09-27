"""Small WebP copies of images for grids and cards (4.38.0).

Until 4.38.0 every grid — the Library, pickers, board covers — loaded each
ORIGINAL: a 40-piece grid could pull 50-100 MB to draw postage stamps, and one
request in the deploy logs was 7 MB. A thumbnail is made once, kept on disk
under ``DATA_DIR/thumbs``, and served instead wherever the page only needs a
card-sized image.

⚠ Originals are never touched. Posting needs the full file, and artists care
about quality; this module only ever writes into its own cache folder.

The cache key is the source path + its size + mtime + the width, so replacing an
image makes a new thumbnail rather than serving a stale one. A thumbnail that
cannot be made (a format Pillow cannot read, a corrupt file) returns None and the
caller serves the original — a missing thumbnail is never an error.

ponytail: no eviction. Thumbnails are ~2-5% of the originals; add an LRU sweep
if the folder ever matters.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path

import config

logger = logging.getLogger(__name__)

WIDTHS = (200, 400, 800)
QUALITY = 80
_lock = threading.Lock()


def root() -> Path:
    p = Path(config.DATA_DIR) / "thumbs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def snap(width) -> int:
    """The nearest supported width at or above the request, so a handful of sizes
    cover every layout and the cache stays small."""
    try:
        w = int(width)
    except (TypeError, ValueError):
        return WIDTHS[1]
    return next((x for x in WIDTHS if x >= w), WIDTHS[-1])


def _key(src: Path, width: int) -> str:
    st = src.stat()
    raw = f"{src.resolve()}|{st.st_size}|{st.st_mtime_ns}|{width}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def thumbnail(src: Path, width) -> Path | None:
    """A cached WebP no wider than ``width``, or None to serve the original."""
    width = snap(width)
    try:
        dest = root() / f"{_key(src, width)}.webp"
    except OSError:
        return None
    if dest.is_file():
        return dest
    with _lock:                      # two requests for the same grid must not race
        if dest.is_file():
            return dest
        try:
            from PIL import Image, ImageOps
            with Image.open(src) as im:
                im.seek(0)                               # first frame of a GIF
                im = ImageOps.exif_transpose(im)
                if im.width <= width:
                    return None                          # already small: serve as is
                im.thumbnail((width, width * 4))
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGBA" if "A" in im.getbands() or im.mode == "P" else "RGB")
                tmp = dest.with_suffix(".tmp")
                im.save(tmp, "WEBP", quality=QUALITY, method=4)
                tmp.replace(dest)
            return dest
        except Exception as e:                           # a thumbnail is never worth a 500
            logger.info("Thumbnail not made (%s); serving the original.", type(e).__name__)
            return None


if __name__ == "__main__":                                # pragma: no cover
    assert snap(1) == 200 and snap(300) == 400 and snap(5000) == 800 and snap("x") == 400
    print("ok")
