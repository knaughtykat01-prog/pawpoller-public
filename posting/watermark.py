"""Artwork watermark on export (gap-wave-5 §1).

Stamps a small configurable text credit onto artwork images just before they're
posted. Hooked at the single choke point in `manager.post_artwork` (before
validation), so one call covers every image platform.

Contract mirrors bluesky._prepare_bsky_image: `apply()` returns
`(path_to_post, temp_or_None)` — the caller posts `path_to_post` and deletes
`temp_or_None` afterwards if set. When watermarking is off, unconfigured, or
fails, it returns `(original_path, None)` so a bad font / odd image can never
block a post.
"""
from __future__ import annotations

import logging
import os
import tempfile

import config

logger = logging.getLogger(__name__)

# Where the stamp sits. Keys match the settings dropdown.
_POSITIONS = {"bottom-right", "bottom-left", "top-right", "top-left", "bottom-center"}


def _load_font(px: int):
    """A truetype font at ~px, or the PIL bitmap default. Tries the server's
    bundled DejaVu, then a couple of common paths, before falling back."""
    from PIL import ImageFont
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(path, px)
        except (OSError, ImportError):
            continue
    try:
        return ImageFont.load_default()
    except Exception:  # noqa: BLE001
        return None


def is_enabled(settings: dict | None = None) -> bool:
    s = settings or config.get_settings()
    return bool(s.get("artwork_watermark_enabled") and (s.get("artwork_watermark_text") or "").strip())


def apply(src_path: str, settings: dict | None = None) -> tuple[str, str | None]:
    """Return (path_to_post, temp_to_cleanup_or_None). No-op unless enabled."""
    s = settings or config.get_settings()
    if not is_enabled(s) or not src_path or not os.path.isfile(src_path):
        return src_path, None
    text = (s.get("artwork_watermark_text") or "").strip()
    position = s.get("artwork_watermark_position", "bottom-right")
    if position not in _POSITIONS:
        position = "bottom-right"
    try:
        opacity = float(s.get("artwork_watermark_opacity", 0.5))
    except (TypeError, ValueError):
        opacity = 0.5
    opacity = min(max(opacity, 0.1), 1.0)

    try:
        from PIL import Image, ImageDraw

        base = Image.open(src_path).convert("RGBA")
        w, h = base.size
        # Scale the stamp to the image (~3.5% of the short edge, min 14px).
        font_px = max(14, int(min(w, h) * 0.035))
        font = _load_font(font_px)
        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Measure the text box (textbbox on modern Pillow; fall back to a guess).
        try:
            box = draw.textbbox((0, 0), text, font=font)
            tw, th = box[2] - box[0], box[3] - box[1]
        except Exception:  # noqa: BLE001 — very old Pillow
            tw, th = font_px * len(text) // 2, font_px
        pad = max(6, font_px // 2)

        if "right" in position:
            x = w - tw - pad
        elif "center" in position:
            x = (w - tw) // 2
        else:
            x = pad
        y = pad if "top" in position else h - th - pad * 2

        alpha = int(255 * opacity)
        # A soft shadow so the mark reads on light and dark art alike.
        draw.text((x + 1, y + 1), text, font=font, fill=(0, 0, 0, min(alpha, 160)))
        draw.text((x, y), text, font=font, fill=(255, 255, 255, alpha))

        composited = Image.alpha_composite(base, overlay)

        # Preserve the source format where it matters (PNG keeps transparency;
        # everything else is flattened to a high-quality JPEG).
        ext = os.path.splitext(src_path)[1].lower()
        if ext == ".png":
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="wm_")
            os.close(fd)
            composited.save(tmp, "PNG")
        else:
            fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix="wm_")
            os.close(fd)
            bg = Image.new("RGB", composited.size, (255, 255, 255))
            bg.paste(composited, mask=composited.split()[-1])
            bg.save(tmp, "JPEG", quality=92)
        return tmp, tmp
    except Exception as e:  # noqa: BLE001 — never block a post on a watermark error
        logger.warning("Watermark skipped (%s): %s", src_path, e)
        return src_path, None
