"""Fit the Library's poster to a site's picture rules — MEDIAPLATS §2 (4.21.0).

Every music / video home wants a picture of its own shape: SoundCloud a square ≥ 800 px,
podcast episode art a square 1400–3000 px, YouTube a 16:9 1280×720 JPEG, Newgrounds a square
icon. The Library's poster is whatever shape the video was, or a 16:9 waveform. Rather than
storing a second asset per site, a poster fits the picture itself, the way Telegram's
``_thumbnail_for`` does: centre-crop to the aspect, resize (up or down) to the pixel rule,
encode as the site demands, into a temp file the caller removes after the send.

Deterministic — the same poster gives the same bytes — and never generative.
"""
from __future__ import annotations

import os
import tempfile


def fit_poster(source: str, *, size: tuple[int, int], fmt: str = "JPEG",
               quality: int = 88, max_bytes: int | None = None) -> str | None:
    """Return a temp path holding *source* fitted to ``size`` (w, h) as ``fmt``; None
    when there is no readable source. The caller deletes the file.

    The crop keeps the centre and covers the target aspect exactly; the resize goes
    both ways (a 1600×900 waveform becomes a 1400×1400 square by cropping the sides
    and upscaling). ``max_bytes`` steps the JPEG quality down until the file fits.
    """
    if not source or not os.path.isfile(source):
        return None
    try:
        from PIL import Image
    except ImportError:                                   # pragma: no cover
        return None
    tw, th = int(size[0]), int(size[1])
    if tw <= 0 or th <= 0:
        raise ValueError("fit_poster needs a positive size")
    try:
        with Image.open(source) as im:
            im = im.convert("RGB") if fmt.upper() == "JPEG" else im.convert("RGBA")
            w, h = im.size
            target = tw / th
            have = w / h
            if have > target:                             # too wide → trim the sides
                nw = max(1, int(round(h * target)))
                x0 = (w - nw) // 2
                im = im.crop((x0, 0, x0 + nw, h))
            elif have < target:                           # too tall → trim top and bottom
                nh = max(1, int(round(w / target)))
                y0 = (h - nh) // 2
                im = im.crop((0, y0, w, y0 + nh))
            im = im.resize((tw, th), Image.LANCZOS)
            suffix = ".jpg" if fmt.upper() == "JPEG" else ".png"
            fd, out = tempfile.mkstemp(prefix="pp-poster-", suffix=suffix)
            os.close(fd)
            q = quality
            while True:
                if fmt.upper() == "JPEG":
                    im.save(out, format="JPEG", quality=q, optimize=True)
                else:
                    im.save(out, format="PNG", optimize=True)
                if not max_bytes or os.path.getsize(out) <= max_bytes or q <= 40 or fmt.upper() != "JPEG":
                    return out
                q -= 12
    except Exception:
        return None


def square(source: str, side: int, **kw) -> str | None:
    """A square of ``side`` px — the common case (artwork, episode art, icons)."""
    return fit_poster(source, size=(side, side), **kw)


def widescreen(source: str, width: int = 1280, **kw) -> str | None:
    """A 16:9 picture of ``width`` px — YouTube's thumbnail shape."""
    return fit_poster(source, size=(width, int(round(width * 9 / 16))), **kw)
