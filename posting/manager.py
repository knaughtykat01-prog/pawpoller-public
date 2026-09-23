"""Posting manager — orchestrates multi-platform story uploads and updates.

This is the main entry point for all posting operations. It coordinates
between the story_reader (which resolves local files and tags) and the
platform posters (which handle the actual HTTP uploads).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any

from database.db import get_connection
from database import posting_queries
from posting import story_reader
from posting.platforms.base import PlatformPoster

logger = logging.getLogger(__name__)

# Retry backoff schedule: attempt 1 → 1min, attempt 2 → 5min, attempt 3 → 30min
_RETRY_DELAYS = [60, 300, 1800]

# Failures no retry can repair: they need a human to re-enter a credential or
# fix something on the platform. Retrying them is not merely wasted work —
# every failure queues another row, so an unfixable error becomes an unbounded
# loop that also hammers the platform. A DeviantArt refresh token died on
# 2026-08-19 and by 2026-08-22 had queued 919 rows and was still calling DA's
# token endpoint every five seconds.
_PERMANENT_ERROR_MARKERS = (
    "not configured",                    # credentials never entered
    "refresh token is no longer valid",  # DA, worded for humans
    "refresh_token is invalid",          # DA, as the API words it
    "invalid_grant",                     # the OAuth family's spelling of it
    "not logged in",                     # FA session cookies expired
    "expired or invalid",                # FA/SF session checks
    "token expired or invalid",
    "cookies stored for @",              # X: the session belongs to another account (4.6.3)
)


def _schedule_retry(story_name: str, ch_idx: int, platform: str, action: str,
                    error: str, content_type: str = "story",
                    account_id: int | None = None, variant_key: str = "") -> bool:
    """Queue a retry for a failed post/update if under max attempts.

    Returns True if a retry was queued, False if it must not be retried.
    content_type='artwork' keeps an artwork's retry routed back to post_artwork
    by the scheduler (which branches on the queued row's content_type).

    *account_id* is the account that just failed, and it is not optional in
    spirit: a retry row queued without it falls back to the platform's DEFAULT
    account, so a failed post as account 27 quietly comes back as account 7 and
    would publish to the wrong gallery if it then succeeded. Every one of the
    919 DeviantArt retry rows from the 2026-08-19 incident had been re-pointed
    at the default account that way.

    The attempt number is counted from the queue (3.21.0), not passed in. It
    used to be a parameter, and all three call sites passed the literal 0 — so
    the ceiling below could never be reached and a permanently failing job
    queued a fresh row for ever.
    """
    # Permanent failures — retrying can never succeed, so don't queue a backoff
    # that just re-fails every minute. Two families: credentials that were
    # never entered, and credentials that have since died. Both need a person.
    _err_l = (error or "").lower()
    _permanent = next((m for m in _PERMANENT_ERROR_MARKERS if m in _err_l), None)
    if _permanent:
        logger.warning("Retry: %s ch%d on %s — permanent credential/config error, "
                       "NOT retrying (fix it in Settings, then re-queue): %s",
                       story_name, ch_idx, platform, error[:160])
        return False

    # The upload half already succeeded and only the metadata step failed, so
    # the submission EXISTS on the platform. A retry re-runs the whole post from
    # scratch: it cannot repair the existing submission, and it leaves another
    # one behind every time. FurryNetwork does this — a fresh upload lands as a
    # draft and the metadata PATCH is what promotes it, so a retry loop quietly
    # fills the drafts folder with untitled copies.
    if "already uploaded" in _err_l:
        logger.warning("Retry: %s ch%d on %s — NOT retrying, the upload succeeded and only "
                       "the metadata failed. Retrying would upload a second copy. "
                       "Fix or delete the existing submission on the platform: %s",
                       story_name, ch_idx, platform, error[:200])
        return False

    max_attempts = 3
    conn = get_connection()
    try:
        attempt = posting_queries.count_retry_rows(
            conn, story_name, ch_idx, platform, action,
            content_type=content_type, account_id=account_id,
        )
        if attempt >= max_attempts:
            logger.info("Retry: %s ch%d on %s — max attempts (%d) reached, giving up: %s",
                        story_name, ch_idx, platform, max_attempts, error[:120])
            return False

        delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
        scheduled = (datetime.now(timezone.utc) + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")

        posting_queries.add_to_queue(
            conn, story_name, ch_idx, platform, action,
            account_id=account_id,
            content_type=content_type,
            scheduled_at=scheduled,
            priority=-1,
            # A retry must re-post the SAME render (4.34.0). Without this the row comes
            # back as the rating's pick, which for an alternate render is the primary.
            variant_key=variant_key,
        )
        logger.info("Retry: %s ch%d on %s queued for %s (attempt %d/%d, account %s, error: %s)",
                     story_name, ch_idx, platform, scheduled, attempt + 1, max_attempts,
                     account_id, error[:100])
    finally:
        conn.close()
    return True

# Platform poster registry — lazy-loaded to avoid circular imports.
# Keyed by (platform, account_id) so each account keeps its own authenticated
# client/session — reusing one poster across accounts would leak account A's
# logged-in session into account B's uploads.
_posters: dict[tuple[str, int | None], PlatformPoster] = {}


def _get_poster(platform: str, account_id: int | None = None) -> PlatformPoster:
    """Get or create a platform poster instance for a specific account."""
    key = (platform, account_id)
    if key not in _posters:
        if platform == "ib":
            from posting.platforms.inkbunny import InkbunnyPoster
            poster: PlatformPoster = InkbunnyPoster()
        elif platform == "bsky":
            from posting.platforms.bluesky import BlueskyPoster
            poster = BlueskyPoster()
        elif platform == "ws":
            from posting.platforms.weasyl import WeasylPoster
            poster = WeasylPoster()
        elif platform == "sf":
            from posting.platforms.sofurry import SoFurryPoster
            poster = SoFurryPoster()
        elif platform == "fa":
            from posting.platforms.furaffinity import FurAffinityPoster
            poster = FurAffinityPoster()
        elif platform == "sqw":
            from posting.platforms.squidgeworld import SquidgeWorldPoster
            poster = SquidgeWorldPoster()
        elif platform == "ao3":
            from posting.platforms.ao3 import AO3Poster
            poster = AO3Poster()
        elif platform == "tg":
            from posting.platforms.telegram import TelegramPoster
            poster = TelegramPoster()
        elif platform == "pod":
            from posting.platforms.podcast import PodcastPoster
            poster = PodcastPoster()
        elif platform == "sc":
            from posting.platforms.soundcloud import SoundCloudPoster
            poster = SoundCloudPoster()
        elif platform == "ng":
            from posting.platforms.newgrounds import NewgroundsPoster
            poster = NewgroundsPoster()
        elif platform == "yt":
            from posting.platforms.youtube import YouTubePoster
            poster = YouTubePoster()
        elif platform == "tw":
            from posting.platforms.twitter import TwitterPoster
            poster = TwitterPoster()
        elif platform == "ik":
            from posting.platforms.itaku import ItakuPoster
            poster = ItakuPoster()
        elif platform == "da":
            from posting.platforms.deviantart import DeviantArtPoster
            poster = DeviantArtPoster()
        elif platform == "e621":
            from posting.platforms.e621 import E621Poster
            poster = E621Poster()
        elif platform == "fn":
            from posting.platforms.furrynetwork import FurryNetworkPoster
            poster = FurryNetworkPoster()
        elif platform == "fbr":
            from posting.platforms.furbooru import FurbooruPoster
            poster = FurbooruPoster()
        elif platform == "ig":
            from posting.platforms.instagram import InstagramPoster
            poster = InstagramPoster()
        else:
            raise ValueError(f"Unknown platform: {platform}")
        # All posters carry an account_id; account-aware posters (IB, and FA
        # once refactored) read it in _ensure_client to authenticate as the
        # right account. None means "the platform's default account".
        poster.account_id = account_id
        _posters[key] = poster
    return _posters[key]


def _resolve_account_id(platform: str, account_id: int | None,
                        persona_id: int | None = None) -> int:
    """Return a concrete account_id for a platform.

    Two regimes, and the difference is the whole of publish_flow spec §3:

    * ``persona_id`` given — a persona-first publish. The account must be one
      of that persona's on this platform, or the platform is REFUSED
      (``ValueError``). Never the platform default (it may be another
      persona's), never a freshly invented one.
    * no persona — the pre-4.2.0 behaviour, unchanged: an explicit id wins,
      else the platform's default account, created if missing. ``create=True``
      stays here deliberately; changing it is a behaviour change for every
      existing caller and belongs in its own release.
    """
    if persona_id is not None:
        from database import personas as personas_db
        conn = get_connection()
        try:
            err = personas_db.persona_account_error(conn, platform, account_id, persona_id)
        finally:
            conn.close()
        if err:
            raise ValueError(err)
        return account_id
    if account_id is not None:
        return account_id
    from database import accounts as accounts_db
    conn = get_connection()
    try:
        return accounts_db.get_default_account_id(conn, platform, create=True)
    finally:
        conn.close()


# Platforms that ANNOUNCE a work rather than host it. They post LAST so the
# links they carry can include what this same publish just created — "wherever
# it lands first" (publish_flow spec §10 Q3). Everything else keeps the
# caller's order.
# Since 4.3.7 X and Bluesky are announcers too — their posts carry the same
# links — so they take the same last place. Read from one list so the manager,
# the artwork reader and the UI cannot disagree about who announces.
from posting.announce import ANNOUNCERS as _ANNOUNCES_LAST  # noqa: E402


def _announcers_last(platforms: list[str]) -> list[str]:
    return ([p for p in platforms if p not in _ANNOUNCES_LAST]
            + [p for p in platforms if p in _ANNOUNCES_LAST])


def _run_links(results: list[dict[str, Any]], chapter_index: int | None = None) -> list[tuple[str, str]]:
    """``(platform, url)`` of this publish's successes so far, for the announcer.

    A chapter announcement links that chapter (or a whole-story post); an
    artwork has no chapters and takes everything.
    """
    out: list[tuple[str, str]] = []
    for r in results:
        url = r.get("external_url") or r.get("url") or ""
        if not r.get("success") or not url:
            continue
        if chapter_index and r.get("chapter_index") not in (None, 0, chapter_index):
            continue
        out.append((str(r.get("platform") or ""), str(url)))
    return out


def _refused(platform: str, err: Exception, **extra) -> dict[str, Any]:
    """A result row for a platform the persona guard refused — same shape as a
    poster failure so the results panel renders it beside the others."""
    return {"platform": platform, "success": False, "url": "", "error": str(err),
            "refused": True, **extra}


def get_platform_requires(platform: str) -> str:
    """Get the runtime mode requirement for a platform."""
    try:
        poster = _get_poster(platform)
        return poster.requires_mode
    except ValueError:
        return "any"


# Error patterns that mean "the submission no longer exists on the platform".
# When we detect one of these during update_story(), we mark the publication
# as 'deleted' in the registry so the matrix flips it back to a re-postable
# state instead of repeatedly retrying an edit against a dead ID.
#
# All patterns scoped to phrasings that refer to the submission/work/URL, not
# generic "not found" (which would false-positive on unrelated errors like
# "model not found in cache" or local file-not-found exceptions).
DELETION_ERROR_PATTERNS = (
    "submission has been deleted",     # Inkbunny
    "submission not found",            # FA / generic
    "submission was not found",        # FA
    "work not found",                  # AO3
    "work has been deleted",           # AO3
    "work does not exist",             # AO3
    "page does not exist",             # generic OTW
    "no such submission",              # SF-ish
    "404 not found",                   # direct httpx error string
    "client error '404",               # httpx's formatted 404 message
)


def _queue_edit_for_desktop(content_type: str, name: str, ch_idx: int,
                            plat: str, account_id: int, poster) -> bool:
    """Hand an edit to the desktop *before* attempting a request that cannot work.

    ⚠ **Inert since 3.26.0: no platform declares ``requires_mode = 'desktop'``.**
    Kept because the mechanism is sound for a platform that genuinely cannot be
    reached from the server — but its original justification was wrong, so do
    not re-read it as evidence.

    It used to say FurAffinity "blocks the datacenter IP outright: ``/controls/``
    pages come back as an empty shell even with valid cookies", making a
    server-side edit **guaranteed** to fail, citing

        FA edit failed for 37056222: FA: Could not find changeinfo form on edit page

    That empty shell is what FA serves when you are **logged out**. The cookies
    were expired, not blocked — `validate_cookies` could not fail (3.19.1), so
    they looked valid — and a server-side FA submit has since completed in full
    (2026-08-21, view/66103446).

    The edit paths already queued for desktop, but only as *failure recovery* —
    after making the doomed request, raising, and logging a traceback that reads
    like a scraper break rather than a routing decision. Checking first is the
    same outcome with none of that, and it spends one fewer request against a
    platform that is already refusing us.

    Returns True when the job has been queued and the caller should skip the
    attempt entirely.
    """
    from posting.scheduler import _runtime_mode
    if not (poster.requires_mode == "desktop" and _runtime_mode == "server"):
        return False
    conn = get_connection()
    try:
        posting_queries.add_to_queue(
            conn, name, ch_idx, plat, "update",
            account_id=account_id,
            content_type=content_type,
            requires="desktop",
        )
    finally:
        conn.close()
    logger.info(
        "Queued %s edit of %s on %s (account %s) for desktop — %s cannot be "
        "edited from the server (datacenter IP block)",
        content_type, name, plat, account_id, plat,
    )
    return True


def _looks_like_deletion(error: str | None) -> bool:
    if not error:
        return False
    low = error.lower()
    return any(p in low for p in DELETION_ERROR_PATTERNS)


PLATFORM_EMOJIS = {
    "ib": "🐾",
    "fa": "🦊",
    "ws": "🦎",
    "sf": "🐺",
    "sqw": "🦑",
    "ao3": "📖",
    "ik": "🎯",
    "da": "🎨",
    "bsky": "🦋",
}


def _log_validation_failure(platform: str, name: str, chapter_index: int,
                            errors: list[str], *, account_id: int = 0,
                            content_type: str = "story") -> None:
    """Record a refused-before-sending attempt in `posting_log`.

    Validation failures used to `continue` straight past the logging call, so
    they existed only in the app log and in the HTTP response of whoever
    triggered the post. Anyone looking afterwards — the activity feed, the
    notification centre, the per-work posting history — saw a platform that had
    simply never been attempted, which reads as "I forgot to tick it", not as
    "it was rejected".

    That is how the 3.17.0 DeviantArt tag bug stayed invisible: every other
    platform logged `success` for the same piece and DA logged nothing at all.
    Status is `failed` because from the user's side it is a failed post; the
    absent `external_id` and the "Rejected before sending" prefix distinguish it
    from a failure at the API.
    """
    try:
        conn = get_connection()
        try:
            posting_queries.log_posting_action(
                conn, platform, name, chapter_index,
                action="post",
                account_id=account_id,
                content_type=content_type,
                status="failed",
                error_message="Rejected before sending: " + "; ".join(errors),
            )
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — never let bookkeeping sink a post run
        logger.debug("Could not log validation failure for %s/%s: %s",
                     name, platform, e)


async def post_story(
    story_name: str,
    platforms: list[str],
    chapters: list[int] | None = None,
    extras: dict[str, Any] | None = None,
    account_ids: dict[str, int] | None = None,
    persona_id: int | None = None,
    description_overrides: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Post a story to multiple platforms.

    Args:
        story_name: Story folder name (e.g. "Example_Story").
        platforms: Platform IDs (e.g. ["ib", "bsky"]).
        chapters: Specific chapter indices (None = all). [0] = full story.
        extras: Per-package overrides merged into ``package.extra`` before
            posting (e.g. ``{"draft": True}`` to post as a draft on
            platforms that support it).
        account_ids: Optional ``{platform: account_id}`` selecting which account
            to post AS per platform. Platforms not listed use their default
            account — unless ``persona_id`` is given.
        persona_id: When set this is a persona-first publish: every platform
            must have one of that persona's accounts in ``account_ids`` or it
            is refused (a result row with ``refused=True``), never defaulted.
        description_overrides: ``{platform: text}`` for THIS post only —
            ``build_package``'s first cascade branch (4.3.0). The stored
            per-platform description is untouched.

    Returns:
        List of result dicts with platform, chapter, success, url, error.
    """
    account_ids = account_ids or {}
    story = story_reader.load_story(story_name)
    results: list[dict[str, Any]] = []

    # Determine chapters to post
    if chapters is None:
        if story.total_chapters > 0:
            chapter_list = list(range(1, story.total_chapters + 1))
        else:
            chapter_list = [0]  # Full story
    else:
        chapter_list = chapters

    for platform in _announcers_last(platforms):
        try:
            account_id = _resolve_account_id(platform, account_ids.get(platform), persona_id)
        except ValueError as e:
            results.append(_refused(platform, e, chapter=None))
            continue
        poster = _get_poster(platform, account_id)
        for ch_idx in chapter_list:
            package = story_reader.build_package(
                story, ch_idx, platform,
                description_override=(description_overrides or {}).get(platform))
            if extras:
                package.extra.update(extras)
            if platform in _ANNOUNCES_LAST:
                package.extra["run_links"] = _run_links(results, ch_idx)

            # Validate
            refusal = poster.refusal(package) if hasattr(poster, "refusal") else None   # media kind, then rating (4.21.0)
            errors = [refusal] if refusal else poster.validate(package)
            if errors:
                result_dict = {
                    "platform": platform,
                    "chapter_index": ch_idx,
                    "chapter_title": package.chapter_title,
                    "success": False,
                    "error": "; ".join(errors),
                }
                results.append(result_dict)
                logger.warning("Validation failed for %s ch%d on %s: %s",
                               story_name, ch_idx, platform, errors)
                _log_validation_failure(platform, story_name, ch_idx, errors,
                                        account_id=account_id)
                continue

            # Post
            result = await poster.post(package)

            # Compute file hash for change detection
            from posting.sync import hash_file
            current_hash = hash_file(package.file_path) if package.file_path else ""

            # If the post failed, try to auto-recover:
            # 1. Desktop-requiring platforms → queue for desktop
            # 2. Rate limit / transient errors → schedule retry with backoff
            queued_for_desktop = False
            retry_queued = False
            if not result.success:
                from posting.scheduler import _runtime_mode
                if poster.requires_mode == "desktop" and _runtime_mode == "server":
                    conn = get_connection()
                    try:
                        posting_queries.add_to_queue(
                            conn, story_name, ch_idx, platform, "post",
                            account_id=account_id,
                            requires="desktop",
                        )
                        queued_for_desktop = True
                        logger.info(
                            "Auto-queued %s ch%d on %s (account %s) for desktop (server post failed: %s)",
                            story_name, ch_idx, platform, account_id, result.error,
                        )
                    finally:
                        conn.close()
                elif not queued_for_desktop:
                    retry_queued = _schedule_retry(
                        story_name, ch_idx, platform, "post", result.error or "unknown",
                        account_id=account_id,
                    )

            # Record in database
            conn = get_connection()
            try:
                pub_id = posting_queries.upsert_publication(
                    conn, story_name, ch_idx, platform,
                    account_id=account_id,
                    external_id=result.external_id,
                    external_url=result.external_url,
                    title_used=package.title,
                    description_used=package.description[:500],
                    tags_used=package.tags,
                    rating_used=package.rating,
                    format_file=package.file_path or "",
                    file_hash=current_hash,
                    word_count=package.word_count,
                    status="posted" if result.success else "failed",
                )
                posting_queries.log_posting_action(
                    conn, platform, story_name, ch_idx,
                    action="post",
                    account_id=account_id,
                    status="success" if result.success else ("queued_desktop" if queued_for_desktop else "failed"),
                    pub_id=pub_id,
                    external_id=result.external_id,
                    external_url=result.external_url,
                    error_message=result.error,
                    duration_seconds=result.duration_seconds,
                )
            finally:
                conn.close()

            results.append({
                "platform": platform,
                "chapter_index": ch_idx,
                "chapter_title": package.chapter_title,
                "success": result.success,
                "queued_desktop": queued_for_desktop,
                "retry_queued": retry_queued,
                "external_id": result.external_id,
                "external_url": result.external_url,
                "error": result.error,
                "duration": result.duration_seconds,
            })

            # Rate limit between chapters on the same platform
            if ch_idx != chapter_list[-1]:
                await poster._rate_limit()

    return results


# The sentinel a caller sends to mean "the piece's own image", as distinct from "you
# choose" (absent). Without it there is no way to say no to an automatic substitution.
_PRIMARY_RENDER = "__primary__"

# "let PawPoller choose" — the rating's pick, which is the 4.33.0 default. Needed as an
# explicit value because the picker always sends a list once a piece HAS renders, and a
# list containing only the primary would silently switch the automatic routing OFF for
# every piece that has a variant — disabling the feature for exactly the pieces it exists
# to serve.
_AUTO_RENDER = "__auto__"


async def post_artwork(
    artwork_name: str,
    platforms: list[str],
    extras: dict[str, Any] | None = None,
    account_ids: dict[str, int] | None = None,
    persona_id: int | None = None,
    description_overrides: dict[str, str] | None = None,
    variant_overrides: dict[str, str] | None = None,
    renders: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Post one artwork (a single image) to multiple platforms.

    The image-posting parallel to ``post_story``: same per-platform posters,
    validation, registry, and desktop-queue/retry fallbacks. The only
    differences are the source (artwork_reader, not story_reader), the fixed
    chapter_index 0, and content_type='artwork' on every registry write.

    Args:
        artwork_name: Artwork folder name in the artwork archive.
        platforms: Platform IDs (e.g. ["ib", "fa", "bsky"]).
        extras: Per-package overrides merged into ``package.extra`` before posting.
        account_ids: Optional ``{platform: account_id}`` selecting which account
            to post AS per platform. Platforms not listed use their default —
            unless ``persona_id`` is given.
        persona_id: Persona-first publish: a platform without one of this
            persona's accounts in ``account_ids`` is refused, never defaulted.
        description_overrides: ``{platform: text}`` for THIS post only (4.3.0).
        variant_overrides: ``{platform: variant_key}`` — post a NAMED render to that
            site instead of the one the rating would pick (4.33.0). ``"__primary__"``
            forces the piece's own image. This is how you send an alternate render
            that the rating alone would never select, because it is rated the same as
            the piece: two colourways, a text-free version, a different pose.
            It does NOT bypass the rating gate — asking for the adult render on a
            general-audience site is refused, with the gate's own wording.
        renders: post SEVERAL renders, each as its own submission (4.34.0). A list of
            variant keys, `"__primary__"` for the piece's own image. Every site named in
            `platforms` gets every render in the list, one submission each, rate-limited
            between them. Outranks `variant_overrides`. Each render is gated on its own,
            so one refused render does not refuse its siblings.

    Returns:
        List of result dicts with platform, success, url, error.
    """
    from posting import artwork_reader
    account_ids = account_ids or {}
    artwork = artwork_reader.load_artwork(artwork_name)
    results: list[dict[str, Any]] = []

    _wm_temps: list[str] = []   # watermark temp files, cleaned after the loop
    for platform in _announcers_last(platforms):
        try:
            account_id = _resolve_account_id(platform, account_ids.get(platform), persona_id)
        except ValueError as e:
            results.append(_refused(platform, e))
            continue
        poster = _get_poster(platform, account_id)
        # Which render this site gets (4.33.0). A piece whose primary is adult used to be
        # refused outright on an SFW-only site even when an SFW variant of it existed;
        # now the variant is posted there, with its own rating, tags and description.
        # A named render for this site beats the rating's pick (4.33.0). This is the
        # only way to send an alternate render that the rating would never choose,
        # because it is rated the same as the piece.
        # Which renders this site gets (4.34.0, VARSPLIT). A piece can be posted to one
        # site as SEVERAL submissions — a second colourway, a text-free version — so this
        # resolves to a LIST and the loop below runs once per render.
        #
        # Precedence: an explicit `renders` list, then a named override for this site,
        # then the rating's pick. `None` in the list means the piece's own image.
        _renders: list = []
        _asked = (variant_overrides or {}).get(platform) or ""
        if renders:
            for _key in renders:
                if _key == _AUTO_RENDER:
                    _renders.append(artwork_reader.variant_for_rating(
                        artwork, getattr(poster, "max_rating", "adult")))
                    continue
                if _key == _PRIMARY_RENDER:
                    _renders.append(None)
                    continue
                _v = next((v for v in (artwork.variants or [])
                           if isinstance(v, dict) and v.get("key") == _key), None)
                if _v is None:
                    results.append(_refused(
                        platform, ValueError(f"no render called {_key!r} on this piece"),
                        chapter_index=0, chapter_title="", variant=_key))
                    continue
                _renders.append(_v)
            # Auto can resolve to a render that was also ticked by name — one submission,
            # not two identical ones.
            _seen, _uniq = set(), []
            for _v in _renders:
                _k = (_v or {}).get("key") or ""
                if _k in _seen:
                    continue
                _seen.add(_k)
                _uniq.append(_v)
            _renders = _uniq
            if not _renders:
                continue          # every named render was unknown; already reported
        elif _asked == _PRIMARY_RENDER:
            _renders = [None]
        elif _asked:
            _v = next((v for v in (artwork.variants or [])
                       if isinstance(v, dict) and v.get("key") == _asked), None)
            if _v is None:
                results.append(_refused(
                    platform, ValueError(f"no render called {_asked!r} on this piece"),
                    chapter_index=0, chapter_title="", variant=_asked))
                continue
            _renders = [_v]
        else:
            _renders = [artwork_reader.variant_for_rating(
                artwork, getattr(poster, "max_rating", "adult"))]

        # Several renders to ONE site is several uploads where there used to be one.
        # FurAffinity enforces 70 seconds between posts; post_artwork was the only
        # publish path that never rate-limited, because one post per platform never
        # needed it. Nesting platform-OUTER is what makes this per-platform sleep the
        # right one (post_story does the same between chapters).
        for _ri, _variant in enumerate(_renders):
            if _ri:
                await poster._rate_limit()
            _multi = len(_renders) > 1
            try:
                package = artwork_reader.build_artwork_package(
                    artwork, platform,
                    description_override=(description_overrides or {}).get(platform),
                    variant_key=(_variant or {}).get("key") or None,
                    multi_render=_multi,
                    account_id=account_id)
            except FileNotFoundError as e:
                # A render that vanished between the selection and the build. One platform's
                # missing file must not take the rest of the run down with it.
                results.append(_refused(platform, e,
                                        chapter_index=0, chapter_title="",
                                        variant=(_variant or {}).get("label")
                                                or (_variant or {}).get("key") or ""))
                logger.warning("Artwork %s on %s: %s", artwork_name, platform, e)
                continue
            if _variant:
                logger.info("Artwork %s on %s: posting the %r render (rated %s)", artwork_name,
                            platform, _variant.get("label") or _variant.get("key"),
                            _variant.get("rating") or artwork.rating)
            if extras:
                package.extra.update(extras)
            if platform in _ANNOUNCES_LAST:
                package.extra["run_links"] = _run_links(results)

            # Watermark (gap-wave-5 §1): swap in a stamped temp copy before
            # validation (so the size check sees the real bytes) and post that.
            # No-op / never raises when disabled or on any PIL error. Temps are
            # collected and deleted after the whole loop (a retry within an
            # iteration re-posts the same package, so they must outlive it).
            from posting import watermark
            _wm_path, _wm_tmp = watermark.apply(package.file_path)
            if _wm_tmp:
                package.file_path = _wm_path
                _wm_temps.append(_wm_tmp)

            # Validate
            refusal = poster.refusal(package) if hasattr(poster, "refusal") else None   # media kind, then rating (4.21.0)
            errors = [refusal] if refusal else poster.validate(package)
            if errors:
                results.append({
                    "platform": platform,
                    "chapter_index": 0,
                    "chapter_title": "",
                    "success": False,
                    "error": "; ".join(errors),
                    "variant": (_variant or {}).get("label") or (_variant or {}).get("key") or "",
                })
                logger.warning("Validation failed for artwork %s on %s: %s",
                               artwork_name, platform, errors)
                _log_validation_failure(platform, artwork_name, 0, errors,
                                        account_id=account_id,
                                        content_type="artwork")
                continue

            # Post
            result = await poster.post(package)

            # Compute file hash for change detection (the image itself)
            from posting.sync import hash_file
            current_hash = hash_file(package.file_path) if package.file_path else ""

            # Auto-recover failures, mirroring post_story:
            #   1. Desktop-requiring platforms (FA/DA) → queue for desktop
            #   2. Rate-limit / transient errors → backoff retry
            queued_for_desktop = False
            retry_queued = False
            if not result.success:
                from posting.scheduler import _runtime_mode
                if poster.requires_mode == "desktop" and _runtime_mode == "server":
                    conn = get_connection()
                    try:
                        posting_queries.add_to_queue(
                            conn, artwork_name, 0, platform, "post",
                            account_id=account_id,
                            content_type="artwork",
                            requires="desktop",
                            # Without this, N renders failing a server post queue N rows
                            # that all mean "the rating's pick" — the desktop publishes
                            # the same image N times and the alternates never go out.
                            # posting_queue has no UNIQUE, so nothing dedupes them.
                            variant_key=(_variant or {}).get("key") or "",
                        )
                        queued_for_desktop = True
                        logger.info(
                            "Auto-queued artwork %s on %s (account %s) for desktop "
                            "(server post failed: %s)",
                            artwork_name, platform, account_id, result.error,
                        )
                    finally:
                        conn.close()
                elif not queued_for_desktop:
                    retry_queued = _schedule_retry(
                        artwork_name, 0, platform, "post", result.error or "unknown",
                        content_type="artwork", account_id=account_id,
                        variant_key=(_variant or {}).get("key") or "",
                    )

            # Record in database (content_type='artwork' so it never collides with
            # a same-named story and the Stories views never show it).
            conn = get_connection()
            try:
                pub_id = posting_queries.upsert_publication(
                    conn, artwork_name, 0, platform,
                    account_id=account_id,
                    content_type="artwork",
                    # Which render this row is (4.34.0) — the UNIQUE key includes it, so
                    # two renders on one site are two rows instead of one overwriting.
                    variant_key=(_variant or {}).get("key") or "",
                    external_id=result.external_id,
                    external_url=result.external_url,
                    title_used=package.title,
                    description_used=package.description[:500],
                    tags_used=package.tags,
                    rating_used=package.rating,
                    format_file=package.file_path or "",
                    file_hash=current_hash,
                    word_count=0,
                    status="posted" if result.success else "failed",
                )
                posting_queries.log_posting_action(
                    conn, platform, artwork_name, 0,
                    action="post",
                    account_id=account_id,
                    content_type="artwork",
                    status="success" if result.success else (
                        "queued_desktop" if queued_for_desktop else "failed"),
                    pub_id=pub_id,
                    external_id=result.external_id,
                    external_url=result.external_url,
                    error_message=result.error,
                    duration_seconds=result.duration_seconds,
                )
                # Publishing IS mastering (spec §6.1): the artwork folder IS the
                # Masterpiece (Phase 0), so a successful upload becomes a member with
                # linked_via='publication'. This is what makes a fresh "New Masterpiece"
                # accumulate its members automatically as it is posted. Idempotent
                # (add_member = INSERT OR IGNORE + ensure_indexed); best-effort so a
                # membership-link failure never breaks an already-recorded post.
                if result.success and result.external_id:
                    try:
                        from database import masterpiece_queries
                        masterpiece_queries.add_member(
                            conn, artwork_name, platform, result.external_id,
                            account_id=account_id, role="crosspost",
                            linked_via="publication",
                            # Which render this site holds — the column existed and nothing
                            # wrote it, so every edit had to guess by re-deriving (4.33.0).
                            variant_key=(_variant or {}).get("key") or "")
                        conn.commit()
                    except Exception:
                        logger.warning("Masterpiece member link failed for %s/%s",
                                       artwork_name, platform, exc_info=True)
            finally:
                conn.close()

            results.append({
                "platform": platform,
                "chapter_index": 0,
                "chapter_title": "",
                "success": result.success,
                "queued_desktop": queued_for_desktop,
                "retry_queued": retry_queued,
                "external_id": result.external_id,
                "external_url": result.external_url,
                "error": result.error,
                "duration": result.duration_seconds,
                # Which render went to this site — "" for the primary (4.33.0).
                "variant": (_variant or {}).get("label") or (_variant or {}).get("key") or "",
            })

    # Clean up watermark temp files (gap-wave-5 §1) now every post + retry is done.
    for _t in _wm_temps:
        try:
            os.remove(_t)
        except OSError:
            pass

    # Discord announce (gap G4) — once per publish if any platform succeeded.
    # Best-effort; announce_publish self-gates on config + never raises.
    succeeded = [r["platform"] for r in results if r.get("success")]
    if succeeded:
        from posting import discord
        first_url = next((r.get("external_url") for r in results
                          if r.get("success") and r.get("external_url")), None)
        await discord.announce_publish(
            kind="artwork", title=getattr(artwork, "title", "") or artwork_name,
            url=first_url, rating=getattr(artwork, "rating", ""), platforms=succeeded,
        )
    return results


async def update_story(
    story_name: str,
    platforms: list[str] | None = None,
    chapters: list[int] | None = None,
    extras: dict[str, Any] | None = None,
    account_filter: int | None = None,
) -> list[dict[str, Any]]:
    """Push updates to already-posted submissions.

    Looks up existing publications and sends updated metadata/files. Each
    publication is updated AS the account it was posted under (pub.account_id).

    Args:
        story_name: Story folder name.
        platforms: Filter by platform (None = all posted platforms).
        chapters: Filter by chapter (None = all posted chapters).
        extras: Per-package overrides merged into ``package.extra`` before
            the edit runs (e.g. ``{"skip_content_refresh": True}`` to
            push metadata only, skipping the file/chapter content
            upload where supported).
        account_filter: When set, only update publications owned by this
            account_id (used by the scheduler to update the specific account a
            queued item targeted).

    Returns:
        List of result dicts.
    """
    story = story_reader.load_story(story_name)
    results: list[dict[str, Any]] = []

    conn = get_connection()
    try:
        # Include both posted and failed publications (failed ones may need retrying)
        posted = posting_queries.get_publications(conn, story_name=story_name, status="posted")
        failed = posting_queries.get_publications(conn, story_name=story_name, status="failed")
        # Deduplicate by (story, chapter, platform, account) — prefer posted over
        # failed. account_id is part of the key so two accounts' copies of the
        # same chapter are updated independently.
        seen = set()
        pubs = []
        for p in posted + failed:
            key = (p["story_name"], p["chapter_index"], p["platform"], p["account_id"])
            if key not in seen:
                seen.add(key)
                pubs.append(p)
    finally:
        conn.close()

    if not pubs:
        logger.warning("No publications found for %s", story_name)
        return [{"error": f"No publications found for {story_name}"}]

    for pub in pubs:
        plat = pub["platform"]
        ch_idx = pub["chapter_index"]
        ext_id = pub["external_id"]
        account_id = pub["account_id"]

        if platforms and plat not in platforms:
            continue
        if chapters and ch_idx not in chapters:
            continue
        if account_filter is not None and account_id != account_filter:
            continue
        if not ext_id:
            continue

        poster = _get_poster(plat, account_id)
        package = story_reader.build_package(story, ch_idx, plat)
        if extras:
            package.extra.update(extras)

        if not poster.supports_edit:
            logger.warning(
                "Platform %s does not support in-place editing — will delete+repost",
                plat,
            )

        if _queue_edit_for_desktop("story", story_name, ch_idx, plat, account_id, poster):
            results.append({"platform": plat, "chapter_index": ch_idx,
                            "success": False, "queued_desktop": True})
            continue

        result = await poster.edit(ext_id, package)

        from posting.sync import hash_file
        current_hash = hash_file(package.file_path) if package.file_path else ""

        # Was the submission deleted on the platform side? Mark the
        # publication so the matrix prompts a re-post rather than
        # retrying the edit (which will keep failing).
        was_deleted = (not result.success) and _looks_like_deletion(result.error)

        # If the edit failed on the server for some OTHER reason, auto-queue
        # for desktop as a fallback. Deletion errors skip the queue — desktop
        # would hit the same wall.
        queued_for_desktop = False
        retry_queued = False
        if not result.success and not was_deleted:
            from posting.scheduler import _runtime_mode
            if poster.requires_mode == "desktop" and _runtime_mode == "server":
                conn = get_connection()
                try:
                    posting_queries.add_to_queue(
                        conn, story_name, ch_idx, plat, "update",
                        account_id=account_id,
                        requires="desktop",
                    )
                    queued_for_desktop = True
                    logger.info(
                        "Auto-queued %s ch%d on %s (account %s) for desktop (server edit failed: %s)",
                        story_name, ch_idx, plat, account_id, result.error,
                    )
                finally:
                    conn.close()
            elif not queued_for_desktop:
                retry_queued = _schedule_retry(
                    story_name, ch_idx, plat, "update", result.error or "unknown",
                    account_id=account_id,
                )
        elif was_deleted:
            logger.info(
                "Publication %s ch%d on %s was deleted upstream — marking registry",
                story_name, ch_idx, plat,
            )

        conn = get_connection()
        try:
            if was_deleted:
                # Mark as deleted so the matrix treats this slot as
                # re-postable. Keep the external_id/url for history.
                posting_queries.upsert_publication(
                    conn, story_name, ch_idx, plat,
                    account_id=account_id,
                    external_id=ext_id,
                    external_url=pub["external_url"],
                    title_used=package.title,
                    description_used=package.description[:500],
                    tags_used=package.tags,
                    rating_used=package.rating,
                    format_file=package.file_path or "",
                    file_hash=current_hash,
                    word_count=package.word_count,
                    status="deleted",
                )
            elif result.success:
                posting_queries.upsert_publication(
                    conn, story_name, ch_idx, plat,
                    account_id=account_id,
                    external_id=result.external_id or ext_id,
                    external_url=result.external_url or pub["external_url"],
                    title_used=package.title,
                    description_used=package.description[:500],
                    tags_used=package.tags,
                    rating_used=package.rating,
                    format_file=package.file_path or "",
                    file_hash=current_hash,
                    word_count=package.word_count,
                    status="posted",
                )
            log_status = (
                "success" if result.success
                else "deleted_upstream" if was_deleted
                else "queued_desktop" if queued_for_desktop
                else "failed"
            )
            posting_queries.log_posting_action(
                conn, plat, story_name, ch_idx,
                action="update",
                account_id=account_id,
                status=log_status,
                pub_id=pub["pub_id"],
                external_id=result.external_id or ext_id,
                external_url=result.external_url,
                error_message=result.error,
                duration_seconds=result.duration_seconds,
            )
        finally:
            conn.close()

        results.append({
            "platform": plat,
            "chapter_index": ch_idx,
            "chapter_title": package.chapter_title,
            "success": result.success,
            "queued_desktop": queued_for_desktop,
            "retry_queued": retry_queued,
            "deleted_upstream": was_deleted,
            "external_id": result.external_id or ext_id,
            "external_url": result.external_url or pub["external_url"],
            "error": result.error,
            "duration": result.duration_seconds,
        })

        await poster._rate_limit()

    return results


async def update_artwork(
    artwork_name: str,
    platforms: list[str] | None = None,
    account_filter: int | None = None,
    extras: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Push a Masterpiece's canonical metadata to its editable members ("Sync all",
    spec §6.2). The artwork parallel of ``update_story``, but driven off the
    ``masterpiece_members`` table (so it also reaches members linked by promote /
    pHash that never had a publication) and **metadata-only** by default.

    For each member whose poster reports ``supports_edit``: rebuild the artwork
    package from ``masterpiece.json`` and call ``poster.edit(submission_id,
    package)`` with ``extra['skip_content_refresh']=True`` — we push
    title/description/tags/rating, never re-upload the image. Members on platforms
    that can't edit (Bluesky/Itaku/Instagram/FurryNetwork) are returned as skipped
    ``post-only`` and
    never touched (§0-A1). Each edit is recorded (log + publication metadata,
    content_type='artwork').

    Args:
        artwork_name: Masterpiece / artwork folder name.
        platforms: Restrict to these platforms (None = all editable members).
        account_filter: Only members owned by this account_id.
        extras: Extra package overrides (merged after skip_content_refresh).

    Returns: one result dict per member (skipped members carry ``skipped=True``).
    """
    from posting import artwork_reader
    from database import masterpiece_queries

    artwork = artwork_reader.load_artwork(artwork_name)

    conn = get_connection()
    try:
        members = masterpiece_queries.get_members(conn, artwork_name)
    finally:
        conn.close()
    if not members:
        return [{"error": f"No linked uploads for {artwork_name}"}]

    results: list[dict[str, Any]] = []
    for m in members:
        plat = m["platform"]
        ext_id = str(m["submission_id"])
        account_id = m.get("account_id")

        if platforms and plat not in platforms:
            continue
        if account_filter is not None and account_id != account_filter:
            continue
        if not ext_id:
            continue

        # Resolve None → the platform's default account (a concrete id is required
        # for _get_poster / the publication + log writes), mirroring post_artwork.
        account_id = _resolve_account_id(plat, account_id)
        try:
            poster = _get_poster(plat, account_id)
        except ValueError:
            results.append({"platform": plat, "submission_id": ext_id,
                            "success": False, "skipped": True, "reason": "no poster"})
            continue
        # Non-editable platforms are post-only — never silently overwrite them.
        # `supports_artwork_edit` catches the subtler case: DeviantArt CAN edit
        # (literature) but has no image-deviation endpoint, so attempting it
        # both fails and records that failure against a live deviation.
        # getattr-with-default: every real poster inherits the attribute from
        # PlatformPoster, but test stubs and any out-of-tree poster may not,
        # and the default (True) is the safe reading of "unspecified".
        if not (poster.supports_edit and getattr(poster, "supports_artwork_edit", True)):
            results.append({"platform": plat, "submission_id": ext_id,
                            "success": False, "skipped": True, "reason": "post-only"})
            continue

        # The render this site was actually POSTED as (4.33.0). Without this an edit
        # pushed the PRIMARY's rating, tags and description to a site holding the SFW
        # render — the post/edit asymmetry that arrived with variant routing. The gate
        # runs here too, for the same reason it runs on the way out.
        #
        # Read what was recorded, and only re-derive for a post made before the column
        # was written. Re-deriving is NOT equivalent: a deliberately-chosen alt render
        # rated the same as the piece would re-derive to None, and the edit would push
        # the primary's metadata over it.
        _recorded = (m.get("variant_key") or "").strip()
        if _recorded:
            _variant = next((v for v in (artwork.variants or [])
                             if isinstance(v, dict) and v.get("key") == _recorded), None)
            if _variant is None:
                # Recorded a render the piece no longer declares. Falling through to the
                # primary would push ITS rating over a live submission that still holds
                # the other render's bytes — an edit sets skip_content_refresh, so the
                # image stays. On a piece rated below one of its renders that is a rating
                # DOWNGRADE on live adult work: the same rating/bytes decoupling as the
                # post side, reached from the edit side.
                results.append({
                    "platform": plat, "submission_id": ext_id, "success": False,
                    "refused": True, "variant": _recorded,
                    "error": (f"this site holds the {_recorded!r} render, which the piece "
                              f"no longer declares — re-link or re-post it")})
                logger.warning("Artwork edit %s on %s: recorded render %r is gone",
                               artwork_name, plat, _recorded)
                continue
        else:
            _variant = artwork_reader.variant_for_rating(
                artwork, getattr(poster, "max_rating", "adult"))
        try:
            package = artwork_reader.build_artwork_package(
                artwork, plat, variant_key=(_variant or {}).get("key") or None,
                account_id=account_id)
        except FileNotFoundError as e:
            results.append({"platform": plat, "submission_id": ext_id,
                            "success": False, "error": str(e), "refused": True})
            logger.warning("Artwork edit %s on %s: %s", artwork_name, plat, e)
            continue
        package.extra["skip_content_refresh"] = True   # metadata sync only — never re-upload the image
        if extras:
            package.extra.update(extras)

        refusal = poster.refusal(package) if hasattr(poster, "refusal") else None
        if refusal:
            results.append({"platform": plat, "submission_id": ext_id,
                            "success": False, "error": refusal, "refused": True,
                            "variant": (_variant or {}).get("label")
                                       or (_variant or {}).get("key") or ""})
            logger.warning("Artwork edit refused for %s on %s: %s", artwork_name, plat, refusal)
            continue

        # update_artwork had NO desktop handling at all — not even the
        # after-the-fact fallback the story path carried — so an FA artwork edit
        # on the server failed and stayed failed, with nothing queued to ever
        # retry it anywhere it could work.
        if _queue_edit_for_desktop("artwork", artwork_name, 0, plat, account_id, poster):
            results.append({"platform": plat, "submission_id": ext_id,
                            "success": False, "queued_desktop": True})
            continue

        result = await poster.edit(ext_id, package)

        conn = get_connection()
        try:
            pub_id = posting_queries.upsert_publication(
                conn, artwork_name, 0, plat,
                account_id=account_id,
                content_type="artwork",
                # The render this submission IS (4.34.0). Omitting it defaults to "" and
                # matches the PRIMARY's row, so an edit to the alt overwrote the
                # primary's external_id, url, title, tags and rating — the submission
                # stays live and PawPoller loses it. Exactly the harm this release's
                # migration exists to stop, reached from the edit side.
                # `_recorded` ALONE, deliberately. Falling back to the auto-derived
                # variant writes a second row for a member linked before 4.33.0 (whose
                # key is "") whenever the rating now picks a render — one submission,
                # two publication rows. The member row is the source of truth for which
                # render a submission holds, and for a legacy member "" is the honest
                # answer: it predates renders, so it IS the primary.
                variant_key=_recorded,
                external_id=result.external_id or ext_id,
                external_url=result.external_url or "",
                title_used=package.title,
                description_used=package.description[:500],
                tags_used=package.tags,
                rating_used=package.rating,
                status="posted" if result.success else "failed",
            )
            posting_queries.log_posting_action(
                conn, plat, artwork_name, 0,
                action="update",
                account_id=account_id,
                content_type="artwork",
                status="success" if result.success else "failed",
                pub_id=pub_id,
                external_id=result.external_id or ext_id,
                external_url=result.external_url,
                error_message=result.error,
                duration_seconds=result.duration_seconds,
            )
        finally:
            conn.close()

        results.append({
            "platform": plat,
            "submission_id": ext_id,
            "success": result.success,
            "external_url": result.external_url or "",
            # A successful edit may carry a soft note (e.g. Weasyl: file content
            # can't be replaced via API) — surface it without failing the sync.
            "note": result.error if result.success else None,
            "error": None if result.success else result.error,
            "duration": result.duration_seconds,
        })

        await poster._rate_limit()

    return results


async def update_all_changed(
    platforms: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Push updates to all publications whose archive files have changed.

    Uses change detection to find stories with modified files, then calls
    update_story() for each changed story.

    Args:
        platforms: Filter to specific platforms (None = all).

    Returns:
        Aggregated list of result dicts from all update_story() calls.
    """
    from posting.sync import get_changed_stories

    changed = get_changed_stories()
    if not changed:
        return [{"status": "no_changes", "message": "All publications are up to date"}]

    all_results: list[dict[str, Any]] = []

    for story_name, items in changed.items():
        story_platforms = sorted(set(i["platform"] for i in items))
        if platforms:
            story_platforms = [p for p in story_platforms if p in platforms]
        if not story_platforms:
            continue

        story_results = await update_story(story_name, story_platforms)
        all_results.extend(story_results)

    return all_results
