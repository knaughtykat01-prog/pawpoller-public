"""Discord announce webhook API (gap G4).

Configure the webhook + auto-announce toggle, send a test embed, and fire a
manual "Announce to Discord" for a specific piece. The auto-announce on publish
is wired directly into the publishers (posting/post_publisher, posting/manager);
these endpoints cover configuration + the manual button.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

import config
from posting import discord

logger = logging.getLogger(__name__)
discord_router = APIRouter(prefix="/api/discord", tags=["discord"])


def _mask(url: str) -> str:
    """Show enough of the webhook to recognise it without exposing the token."""
    if not url:
        return ""
    return url[:34] + "…" + url[-4:] if len(url) > 42 else url


@discord_router.get("")
def discord_status():
    s = config.get_settings()
    return {
        "configured": discord.is_configured(s),
        "webhook_hint": _mask((s.get("discord_webhook_url") or "").strip()),
        "announce_on_publish": bool(s.get("discord_announce_on_publish", False)),
    }


@discord_router.post("")
def discord_config(body: dict):
    """Save the webhook URL and/or the announce-on-publish toggle.

    An empty/absent `webhook_url` leaves the stored one unchanged; send an
    explicit empty string via `clear: true` to remove it."""
    updates: dict = {}
    if body.get("clear"):
        updates["discord_webhook_url"] = ""
    elif body.get("webhook_url"):
        wh = str(body["webhook_url"]).strip()
        if not wh.startswith("https://"):
            raise HTTPException(400, "That doesn't look like a Discord webhook URL (must start with https://).")
        updates["discord_webhook_url"] = wh
    if "announce_on_publish" in body:
        updates["discord_announce_on_publish"] = bool(body["announce_on_publish"])
    if updates:
        config.save_settings(updates)
    return {"ok": True, **discord_status()}


@discord_router.post("/test")
async def discord_test():
    """Send a test embed so the user can confirm the webhook works."""
    if not discord.is_configured():
        raise HTTPException(400, "No Discord webhook is configured.")
    ok = await discord.announce(
        "post", "✅ PawPoller is connected",
        rating="general",
        platforms=["this is a test — your new work will announce here"],
    )
    if not ok:
        raise HTTPException(502, "Discord rejected the test post — check the webhook URL.")
    return {"ok": True}


@discord_router.post("/announce")
async def discord_announce(body: dict):
    """Manual announce for one piece. The client supplies the display fields it
    already has (title, url, rating, platforms, optional public thumbnail)."""
    if not discord.is_configured():
        raise HTTPException(400, "No Discord webhook is configured.")
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "A title is required.")
    # 4.41.0 (spec 008): the message is built by the same code as the auto-announce.
    # Pass ``artwork_name`` to send that piece's picture and its own Discord choices;
    # ``url`` (legacy) becomes a single link. A public ``thumbnail`` URL is no longer
    # used — the picture travels with the message instead.
    image_path = piece = None
    rating = body.get("rating") or None
    name = (body.get("artwork_name") or "").strip()
    if name:
        from posting import artwork_reader
        from posting.manager import _discord_preview_source
        try:
            art = artwork_reader.load_artwork(name)
        except Exception:
            raise HTTPException(404, "No such artwork.")
        image_path = _discord_preview_source(art)
        piece = (art.categories_by_platform or {}).get("discord")
        # The blur follows the PIECE's rating, never the caller's: a request that
        # names an adult piece and leaves the rating out must still blur it.
        rating = art.rating or rating
    raw_links = body.get("site_links") or []
    if not isinstance(raw_links, list) or not all(isinstance(p, (list, tuple)) and len(p) == 2 for p in raw_links):
        raise HTTPException(400, "site_links must be a list of [site, url] pairs.")
    links = [(str(c), str(u)) for c, u in raw_links if u]
    if not links and body.get("url"):
        links = [("", str(body["url"]))]
    ok = await discord.announce(
        body.get("kind") or "post",
        title,
        rating=rating,
        body=body.get("body") or "",
        site_links=links or None,
        platforms=body.get("platforms") or None,
        image_path=image_path, piece=piece,
    )
    if not ok:
        raise HTTPException(502, "Discord rejected the post — check the webhook URL.")
    return {"ok": True}
