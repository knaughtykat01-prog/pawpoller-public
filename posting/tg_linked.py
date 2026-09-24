"""Linked mode — announcing work that is already published somewhere else.

Phase 3 of `docs/specs/telegram_broadcast.md`. Every other poster in this codebase
UPLOADS the artefact: `StoryUploadPackage.file_path` -> platform -> `external_url`
back. A linked post inverts that — it uploads nothing and references a publication
that already exists.

Four consequences, all of which the spec called out before any of this was built,
and each of which is a real constraint rather than a style choice:

1. **It is a follow-up, not a peer.** It can only run once the piece is live
   somewhere, so it is not a target you tick in the publish picker alongside FA and
   Bluesky. It is an action you take afterwards.
2. **Its data source is `publications`,** not the upload package. The URL it points
   at is one PawPoller already recorded.
3. **"No link yet" is not a failure.** It is *not yet*. Returning an error for it
   would train people to ignore the errors that matter.
4. ⚠ **It must never be recorded as a publication.** A linked Telegram post is an
   announcement OF work published elsewhere; writing a `publications` row would
   assert Telegram holds the piece and corrupt "where is this published" — the same
   class of error as `first_posted_at` meaning the import date. It is recorded in
   `tg_submissions` (with `content_type='announcement'`, so it is distinguishable
   from a direct post that really does carry the art) and nowhere else.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import config
from database import posting_queries, tg_queries
from posting import announce

logger = logging.getLogger(__name__)

# What counts as "live" — a row that claims a post exists AND can prove it with a
# URL. A 'posted' row with no external_url is a publication we cannot link to, so
# for this purpose it is not yet there.
_LIVE_STATUS = "posted"


def find_live_links(conn, *, story_name: str, content_type: str = "story",
                    chapter_index: int | None = None,
                    exclude_platforms: tuple[str, ...] = ("tg",)) -> list[tuple[str, str]]:
    """``[(platform, url), ...]`` for every place this piece is actually live.

    Telegram itself is excluded by default: announcing a piece by linking to the
    announcement is a loop, and a *direct* Telegram post of the same piece is not
    where the work lives either.
    """
    out: list[tuple[str, str]] = []
    for row in posting_queries.get_publications(
            conn, story_name=story_name, content_type=content_type):
        if row.get("status") != _LIVE_STATUS:
            continue
        if chapter_index is not None and row.get("chapter_index") != chapter_index:
            continue
        platform = str(row.get("platform") or "")
        url = str(row.get("external_url") or "").strip()
        if not url or platform in exclude_platforms:
            continue
        out.append((platform, url))
    return out


def build_announcement(*, title: str, blurb: str, links: list[tuple[str, str]],
                       tags: list[str] | None = None, link_mode: str = "auto",
                       link_platforms: list[str] | None = None,
                       with_tags: bool = True) -> str:
    """The caption. Reuses the ordering rules the direct poster already follows.

    ``announce.resolve_links`` reads nothing but ``package.extra``, so a namespace
    carrying that one field is the whole adapter — the alternative was a second
    implementation of link ordering that would drift from the first.
    """
    shim = SimpleNamespace(extra={
        "links_by_platform": [list(p) for p in links],
        "link_mode": link_mode,
        "link_platforms": link_platforms or [],
    })
    parts: list[str] = []
    if title.strip():
        parts.append(title.strip())
    if blurb.strip():
        parts.append(blurb.strip())
    ordered = announce.resolve_links(shim)
    if ordered:
        parts.append("\n".join(ordered))
    if with_tags and tags:
        hashed = announce.hashtags(tags)
        if hashed:
            parts.append(hashed)
    return "\n\n".join(p for p in parts if p)


async def announce_existing(conn, *, story_name: str, content_type: str = "story",
                            chapter_index: int | None = None,
                            title: str = "", blurb: str = "",
                            tags: list[str] | None = None,
                            account_id: int = 0,
                            link_mode: str = "auto",
                            link_platforms: list[str] | None = None,
                            dry_run: bool = False) -> dict:
    """Announce an already-published piece to the channel.

    Returns ``{"status": ...}`` where status is one of:

    * ``"posted"``   — sent; carries ``message_id`` / ``url``.
    * ``"not_yet"``  — the piece is not live anywhere linkable. **Not an error.**
    * ``"preview"``  — ``dry_run`` was set; carries the exact ``text`` that would go.
    * ``"error"``    — something actually went wrong; carries ``error``.

    ⚠ Bulk operations should default to ``dry_run`` (spec 6.5): unlike a test
    gallery, a mistake in a channel is seen by real subscribers immediately.
    """
    settings = config.get_settings()
    links = find_live_links(conn, story_name=story_name, content_type=content_type,
                            chapter_index=chapter_index)
    if not links:
        return {"status": "not_yet",
                "message": "Nothing to link to yet — this piece is not live on any "
                           "other site, so there is nothing to announce."}

    text = build_announcement(
        title=title or story_name, blurb=blurb, links=links, tags=tags,
        link_mode=link_mode, link_platforms=link_platforms,
        with_tags=announce.option_default(settings, "tg", "tags", True))

    if dry_run:
        return {"status": "preview", "text": text,
                "links": [list(p) for p in links]}

    from posting.platforms.telegram import TelegramPoster
    poster = TelegramPoster()
    poster.account_id = account_id or 0
    try:
        creds = poster._resolve_creds("tg", settings)
    except Exception:
        creds = {}
    token = creds.get("tg_bot_token", "")
    channel = creds.get("tg_channel", "")
    if not token or not channel:
        return {"status": "error",
                "error": "Telegram channel posting needs its own bot token and a "
                         "channel (Settings -> Telegram -> Channel posting)."}

    from clients.tg.client import TgClient
    try:
        client = TgClient(bot_token=token, channel=channel)
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    # ⚠ No image, and no parameter to supply one. An earlier draft took an
    # `image_path` straight from the request body and opened it — on a server
    # instance that is a remote file-read gadget with egress, because the bytes go
    # out as a photo on a public channel. `routes/artwork_api.py` already treats
    # the same shape as one and gates it to desktop.
    #
    # Gating would have been the smaller diff; deleting the parameter is the right
    # one, because linked mode is DEFINED as not uploading the artefact (see the
    # module docstring). The parameter contradicted the contract it was written
    # under. If a cover is ever wanted here it must be resolved server-side from
    # the piece being announced, never named by the caller.
    try:
        result = await client.create_post(
            text, image_paths=[],
            silent=announce.option_default(settings, "tg", "silent", False),
            protect=announce.option_default(settings, "tg", "protect", False),
            pin=announce.option_default(settings, "tg", "pin", False),
            preview=True)
    except Exception as e:
        logger.warning("Telegram linked announce failed: %s", e)
        return {"status": "error", "error": str(e)}
    if not result:
        return {"status": "error",
                "error": getattr(client, "last_error", "") or "Telegram refused the post"}

    # Recorded here and NOWHERE else. See the module docstring: a publications row
    # would claim Telegram holds the work.
    try:
        import datetime as _dt
        tg_queries.record_submission(
            conn, account_id=account_id or 0, chat_id=client.channel,
            message_id=result.get("id", 0), title=title or story_name,
            posted_at=_dt.datetime.now().isoformat(timespec="seconds"),
            link=result.get("url", ""), content_type="announcement")
        conn.commit()
    except Exception as e:
        # A post that went out must not be reported as failed because bookkeeping did.
        logger.warning("Telegram: announced but could not record it: %s", e)

    return {"status": "posted", "message_id": result.get("id", ""),
            "url": result.get("url", ""), "links": [list(p) for p in links]}
