"""Discord announce webhook (gap G4; options + the picture, 4.41.0, spec 008).

Post an announcement to a user-configured Discord webhook when new work is
published. A webhook URL grants posting to one channel (no OAuth); it lives in
settings and is only ever used to POST. ⚠ It is a secret: nothing here logs it,
and errors are logged by TYPE only — an httpx error string can carry the URL.

Opt-in on two levels: nothing is sent unless a webhook URL is set, and a publish
only announces when ``discord_announce_on_publish`` is on — or when the publish
itself says so (the per-publish tick on the artwork pages, ``force``).

Options (spec 008) use the same three rungs as Telegram / X / Bluesky
(``posting/announce.py``): the piece's own choice (``categories.discord``), else
the operator's default (``announce_defaults.discord``), else the built-in:

  image   on   send the picture (uploaded with the message — works for an image
               that exists only on this machine)
  spoiler off  blur it. A FLOOR, like Telegram's spoiler: adult work is ALWAYS
               blurred unless the PIECE says off; a setting can blur everything,
               never un-blur adult work
  silent  off  no notification ping
  caption on   include the description
  tags    off  include hashtags (Discord doesn't use them; noise unless asked)
  link_mode / link_platforms — which live-post links (announce.resolve_links)

Announcing must never break a publish, so ``announce_publish`` swallows every
error and the webhook call has a short timeout.
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

import config
from posting import announce as _ann   # 'announce' is also this module's entry point

logger = logging.getLogger(__name__)

CODE = "discord"

# Embed accent per rating so an adult announce reads distinctly from a SFW one.
_RATING_COLOR = {
    "general": 0x4CAF50, "mature": 0xFF9800,
    "adult": 0xE53935, "explicit": 0xE53935,
}
_BLURPLE = 0x5865F2
_ADULT = set(_ann.SPOILER_RATINGS)

_LABELS = {"post": "📣 New post", "artwork": "🎨 New artwork", "story": "📖 New story"}

SUPPRESS_NOTIFICATIONS = 1 << 12      # Discord message flag: deliver without a ping
_DESC_LIMIT = 4096                    # embed description
_FIELD_LIMIT = 1024                   # embed field value
PREVIEW_MAX = 1600                    # px, longest side of the uploaded copy
_ORIGINAL_MAX = 8 * 1024 * 1024       # send the original only if Pillow can't shrink it

BUILT_INS = {"image": True, "spoiler": False, "silent": False, "caption": True, "tags": False}


def _webhook_url(settings: dict | None = None) -> str:
    s = settings if settings is not None else config.get_settings()
    return (s.get("discord_webhook_url") or "").strip()


def is_configured(settings: dict | None = None) -> bool:
    return bool(_webhook_url(settings))


# ── Options ──────────────────────────────────────────────────────────────────

def resolve_options(piece: dict | None, settings: dict | None, rating: str | None) -> dict:
    """The effective options for one announcement: piece, else setting, else built-in."""
    piece = piece or {}
    s = settings or {}
    out = {k: _ann.flag(piece.get(k), _ann.option_default(s, CODE, k, hard))
           for k, hard in BUILT_INS.items()}
    adult = (rating or "").strip().lower() in _ADULT
    if adult and piece.get("spoiler") is None:
        out["spoiler"] = True          # the floor: only a deliberate choice on the piece lifts it
    out["link_mode"] = piece.get("link_mode")
    out["link_platforms"] = piece.get("link_platforms")
    return out


# ── Image ────────────────────────────────────────────────────────────────────

def preview_bytes(src: str | Path | None) -> tuple[bytes, str] | None:
    """A Discord-sized copy of *src*: ``(bytes, extension)`` or None.

    WebP no longer than PREVIEW_MAX on its longest side (Discord's free upload
    ceiling is far below many originals). Falls back to the original file when
    Pillow can't read it and it is small enough; else None (a text card)."""
    if not src:
        return None
    p = Path(src)
    if not p.is_file():
        return None
    try:
        from PIL import Image, ImageOps
        with Image.open(p) as im:
            im.seek(0)
            im = ImageOps.exif_transpose(im)
            im.thumbnail((PREVIEW_MAX, PREVIEW_MAX))
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.getbands() or im.mode == "P" else "RGB")
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=85, method=4)
            return buf.getvalue(), "webp"
    except Exception as e:  # noqa: BLE001 — a preview is never worth a failed announce
        logger.info("Discord preview not made (%s)", type(e).__name__)
    try:
        ext = p.suffix.lstrip(".").lower()
        # Only a real image may go as-is: a video piece's file is not a preview (its
        # poster is — the caller passes that), and Discord would show a blank card.
        if ext in _ann.IMAGE_TYPES and p.stat().st_size <= _ORIGINAL_MAX:
            return p.read_bytes(), ext
    except OSError:
        pass
    return None


# ── Message ──────────────────────────────────────────────────────────────────

def _label(code: str) -> str:
    try:
        from database import platform_metrics
        spec = platform_metrics.BY_CODE.get(code)
        if spec and getattr(spec, "label", ""):
            return spec.label
    except Exception:  # noqa: BLE001
        pass
    return code.upper()


def _links(site_links: list[tuple[str, str]] | None, opts: dict,
           settings: dict | None) -> list[tuple[str, str]]:
    """Which (site, url) pairs to show, through the shared link picker."""
    pairs = [(str(c), str(u)) for c, u in (site_links or []) if c and u]
    if not pairs:
        return []
    extra: dict[str, Any] = {"run_links": pairs}
    # Discord's built-in is "all", not the picker's "auto" (first link only): the card
    # has always listed every site the piece went to, and a list is what it's for.
    extra["link_mode"] = (opts.get("link_mode")
                          or _ann.option_value(settings or {}, CODE, "link_mode", "all"))
    if isinstance(opts.get("link_platforms"), (list, tuple)):
        extra["link_platforms"] = list(opts["link_platforms"])
    urls = _ann.resolve_links(SimpleNamespace(extra=extra), settings or {}, CODE)
    by_url = {u: c for c, u in pairs}
    return [(by_url.get(u, ""), u) for u in urls]


def _fit_description(body: str, tags: list[str] | None, opts: dict) -> str:
    body = (body or "").strip() if opts.get("caption") else ""
    tag_line = _ann.hashtags(tags or []) if opts.get("tags") else ""
    text = "\n\n".join(p for p in (body, tag_line) if p)
    if len(text) <= _DESC_LIMIT:
        return text
    if len(body) <= _DESC_LIMIT:            # hashtags go first
        return body
    return body[: _DESC_LIMIT - 1].rstrip() + "…"


def build_message(kind: str, title: str, *, rating: str | None = None,
                  body: str = "", tags: list[str] | None = None,
                  site_links: list[tuple[str, str]] | None = None,
                  image: tuple[bytes, str] | None = None, opts: dict | None = None,
                  settings: dict | None = None, platforms: list[str] | None = None) -> dict:
    """``{"payload": <json>, "file": (name, bytes) | None}`` for one announcement."""
    opts = opts or resolve_options(None, settings, rating)
    label = _LABELS.get(kind, "New")
    embed: dict[str, Any] = {
        "title": (title or label)[:250],
        "color": _RATING_COLOR.get((rating or "").lower(), _BLURPLE),
        "footer": {"text": f"PawPoller · {label}"},
    }
    desc = _fit_description(body, tags, opts)
    if desc:
        embed["description"] = desc
    shown = _links(site_links, opts, settings)
    if shown:
        embed["url"] = shown[0][1]
    fields = []
    if shown:
        parts, used = [], 0
        for code, url in shown:
            piece = f"[{_label(code) if code else 'Link'}]({url})"
            if used + len(piece) + 3 > _FIELD_LIMIT:
                break
            parts.append(piece)
            used += len(piece) + 3
        fields.append({"name": "Where", "value": " · ".join(parts), "inline": False})
    elif platforms:                      # names without links (the manual/test path)
        fields.append({"name": "Where", "value": " · ".join(_label(c) for c in platforms)[:_FIELD_LIMIT],
                       "inline": False})
    if rating:
        fields.append({"name": "Rating", "value": str(rating), "inline": True})
    if fields:
        embed["fields"] = fields

    payload: dict[str, Any] = {"embeds": [embed]}
    if opts.get("silent"):
        payload["flags"] = SUPPRESS_NOTIFICATIONS
    file = None
    if image and opts.get("image"):
        data, ext = image
        if opts.get("spoiler"):
            # An image INSIDE an embed can't be spoilered; a SPOILER_ attachment is
            # blurred until clicked, so the blurred picture rides beside the card.
            file = (f"SPOILER_preview.{ext}", data)
        else:
            file = (f"preview.{ext}", data)
            embed["image"] = {"url": f"attachment://preview.{ext}"}
    return {"payload": payload, "file": file}


async def _send(webhook_url: str, message: dict) -> bool:
    """POST one message. JSON, or multipart when it carries the picture."""
    payload, file = message["payload"], message.get("file")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if file:
                name, data = file
                resp = await client.post(
                    webhook_url, data={"payload_json": json.dumps(payload)},
                    files={"files[0]": (name, data, "application/octet-stream")})
            else:
                resp = await client.post(webhook_url, json=payload)
        if resp.status_code >= 300:
            logger.warning("Discord webhook returned %s", resp.status_code)
            return False
        return True
    except Exception as e:  # noqa: BLE001 — network failure must not propagate
        logger.warning("Discord webhook post failed (%s)", type(e).__name__)
        return False


# ── Entry points ─────────────────────────────────────────────────────────────

async def announce(kind: str, title: str, *, image_path: str | Path | None = None,
                   piece: dict | None = None, **kw) -> bool:
    """Manual announce / test — sends whenever a webhook is configured."""
    s = config.get_settings()
    url = _webhook_url(s)
    if not url:
        return False
    opts = resolve_options(piece, s, kw.get("rating"))
    image = preview_bytes(image_path) if opts.get("image") else None
    return await _send(url, build_message(kind, title, image=image, opts=opts, settings=s, **kw))


async def announce_publish(kind: str, title: str, *, force: bool | None = None,
                           image_path: str | Path | None = None, piece: dict | None = None,
                           **kw) -> None:
    """Auto-announce after a successful publish. ``force`` is the per-publish
    choice (True/False); None follows ``discord_announce_on_publish``. Never raises."""
    try:
        s = config.get_settings()
        if not is_configured(s):
            return
        wanted = s.get("discord_announce_on_publish", False) if force is None else bool(force)
        if not wanted:
            return
        opts = resolve_options(piece, s, kw.get("rating"))
        image = preview_bytes(image_path) if opts.get("image") else None
        await _send(_webhook_url(s), build_message(kind, title, image=image, opts=opts,
                                                   settings=s, **kw))
    except Exception as e:  # noqa: BLE001
        logger.debug("announce_publish skipped (%s)", type(e).__name__)
