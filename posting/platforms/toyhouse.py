"""Toyhouse platform poster — spec 062. Images only (literature, US3, waits on a live read of the chapter flow).

An image on Toyhouse hangs under one or more of the owner's characters, has no title and no tags, and carries
content flags. So the poster:

- takes the characters from the piece's Toyhouse options (``extra["character_ids"]``), and posts only ids
  that are in the account's own character list, read fresh before the upload (FR-SEC-002);
- sets the flags from the rating and tags, and only ever raises them (FR-SEC-006, site rule 1.2):
  Adult → sexual Explicit, Mature → sexual Mild, a nudity or gore tag ticks that box, and the piece's own
  options can raise any of them further;
- refuses Mature or Adult work tagged as depicting someone underage (site rule 2.2). This sits in
  ``refusal()`` so scheduled posts and retries meet it too;
- credits the artist in Toyhouse's own credit field: the owner when it's their own piece or nobody is
  recorded, the artist's Toyhouse name when PawPoller knows it, else their name + profile link (http(s)
  only). No "Posted via PawPoller" line (rule 4.8; ``attribution._SKIP_PLATFORMS``).

**Did it post?** The upload's reply hasn't been seen live, so the poster doesn't trust it. It notes the
newest image id in the art list before uploading. Afterwards it looks for a newer image under the same
characters: found → success with its link; not found after a "sent" → ``unconfirmed``, never retried, because a
retry could post twice (FR-SEC-009). Matching the characters keeps a gift posted meanwhile from passing as ours.
"""
from __future__ import annotations

import logging
import os
import re

import config
from clients.th.client import IMAGE_MAX_BYTES, IMAGE_TYPES, RECENT_PAGES, THChallenged, THClient
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage, rating_rank

logger = logging.getLogger(__name__)

# Tags that say a piece depicts someone underage (the e621 / Furbooru words plus the plain ones).
UNDERAGE_TAGS = frozenset({"cub", "young", "underage", "child", "minor", "loli", "lolicon", "shota", "shotacon"})
NUDITY_TAGS = frozenset({"nude", "nudity", "naked", "topless", "bottomless", "genitals", "nipples"})
GORE_TAGS = frozenset({"gore", "blood", "guro", "vore", "injury", "wound", "death"})
_ID_RE = re.compile(r"^\d{1,15}$")


def _norm(tags) -> set[str]:
    return {re.sub(r"[\s\-]+", "_", str(t).strip().lower()) for t in (tags or []) if str(t).strip()}


def _on(v) -> bool:
    return v is True or str(v).strip().lower() in ("1", "true", "yes", "on")


def underage_refusal(rating: str, tags) -> str | None:
    hit = sorted(_norm(tags) & UNDERAGE_TAGS)
    if hit and rating_rank(rating) >= 1:
        return (f"Toyhouse doesn't allow mature or adult work showing characters who are underage "
                f"(site rule 2.2). This piece is tagged {', '.join(hit)}.")
    return None


def th_flags(rating: str, tags, extra: dict | None = None) -> dict:
    """The upload form's content flags. The rating and tags set a floor; the piece's options only raise it."""
    extra = extra or {}
    t = _norm(tags)
    floor = {0: 0, 1: 1, 2: 2}[rating_rank(rating)]
    try:
        asked = int(extra.get("sexual", 0) or 0)
    except (TypeError, ValueError):
        asked = 0
    return {
        "is_sexual": max(floor, min(max(asked, 0), 2)),
        "is_nudity": bool(t & NUDITY_TAGS) or _on(extra.get("nudity")),
        "is_gore": bool(t & GORE_TAGS) or _on(extra.get("gore")),
        "is_sensitive": _on(extra.get("sensitive")),
        "warning": str(extra.get("warning", "") or "")[:250],
    }


def credit_row(artist: dict | None, is_self: bool, owner: str) -> dict:
    """One row for Toyhouse's artist credit field."""
    if is_self or not isinstance(artist, dict) or not artist.get("name"):
        return {"type": "onsite", "username": owner}
    th = str((artist.get("handles") or {}).get("th") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", th):
        return {"type": "onsite", "username": th}
    from posting.artist_credit import profile_url
    url = profile_url(artist, "th")
    return {"type": "offsite", "name": str(artist["name"])[:100],
            "url": url if url.startswith(("https://", "http://")) else ""}


class ToyhousePoster(PlatformPoster):

    platform_id = "th"
    platform_name = "Toyhouse"
    supports_edit = False
    supports_artwork_edit = False
    supports_file_replace = False
    min_post_interval = 30
    max_file_size = IMAGE_MAX_BYTES
    accepted_file_types = list(IMAGE_TYPES)
    accepted_media = {"image": list(IMAGE_TYPES)}
    max_rating = "adult"
    requires_mode = "any"

    def refusal(self, package: StoryUploadPackage) -> str | None:
        return underage_refusal(package.rating, package.tags) or super().refusal(package)

    def validate(self, package: StoryUploadPackage) -> list[str]:
        # No title or tags on a Toyhouse image, so the base checks for those don't apply.
        return [e for e in super().validate(package) if e not in ("Title is required", "At least one tag is required")]

    async def _client(self) -> THClient:
        creds = self._resolve_creds("th", config.get_settings())
        username, cookie = (creds.get("th_username") or "").strip(), (creds.get("th_cookie") or "").strip()
        if not cookie:
            raise RuntimeError("Toyhouse isn't connected (Accounts → Toyhouse → Sign in).")
        client = THClient(username=username, cookie=cookie)
        session = await client.validate_session()
        if not session.get("ok"):
            await client.close()
            raise RuntimeError(f"Toyhouse sign-in check failed: {session.get('detail') or 'sign in again'}")
        return client

    async def post(self, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        refusal = self.refusal(package)
        if refusal:
            return PostResult(success=False, error=refusal, duration_seconds=self._elapsed(_t))
        from polling import fa_standdown
        if fa_standdown.remaining("th") > 0:
            return PostResult(success=False, duration_seconds=self._elapsed(_t),
                              error=f"Toyhouse's bot check is on. Try again after {fa_standdown.next_try_text('th')}.")
        extra = package.extra or {}
        wanted = [str(c).strip() for c in (extra.get("character_ids") or []) if str(c).strip()]
        if not wanted:
            return PostResult(success=False, duration_seconds=self._elapsed(_t),
                              error="Pick at least one Toyhouse character for this piece (its Toyhouse options).")
        client, before = None, None
        try:
            client = await self._client()
            owned = {c["character_id"] for c in await client.get_characters()}
            bad = [c for c in wanted if not _ID_RE.match(c) or c not in owned]
            if bad:
                return PostResult(success=False, duration_seconds=self._elapsed(_t),
                                  error="Some picked characters aren't in this Toyhouse account's character list.")
            before = max((int(i["submission_id"]) for i in await client.get_all_images(RECENT_PAGES)), default=0)
            try:
                privacy = min(max(int(extra.get("privacy", 0) or 0), 0), 3)
            except (TypeError, ValueError):
                privacy = 0
            sent = await client.upload_image(
                file_path=package.file_path or "", caption=package.description or "", character_ids=wanted,
                flags=th_flags(package.rating, package.tags, extra), privacy=privacy,
                credits=[credit_row(extra.get("artist"), bool(extra.get("artist_is_self")), client.username)])
            if not sent.get("sent") and sent.get("error"):
                return PostResult(success=False, error=sent["error"], duration_seconds=self._elapsed(_t))
            return await self._find_new(client, before, wanted, _t)
        except THChallenged as e:
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
        except Exception as e:  # noqa: BLE001 — a timeout after sending may still have posted
            logger.error("Toyhouse post failed: %s", type(e).__name__)
            if client is not None and before is not None:    # the upload may have gone through
                return await self._find_new(client, before, wanted, _t)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
        finally:
            if client is not None:
                await client.close()

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        return PostResult(success=False, error="PawPoller can't edit Toyhouse images yet. Edit it on Toyhouse.")

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        return PostResult(success=False, error="PawPoller can't replace a Toyhouse image's file.")

    async def _find_new(self, client: THClient, before: int, wanted: list[str], _t: float) -> PostResult:
        """The image this upload made: newer than ``before`` and under the characters we sent."""
        try:
            for img in await client.get_all_images(RECENT_PAGES):   # a new image is on page 1
                if int(img["submission_id"]) > before and set(wanted) <= set(img.get("character_ids") or []):
                    return PostResult(success=True, external_id=img["submission_id"], external_url=img["link"],
                                      duration_seconds=self._elapsed(_t))
        except Exception:  # noqa: BLE001
            logger.debug("Toyhouse gallery check failed", exc_info=True)
        return PostResult(success=False, unconfirmed=True, duration_seconds=self._elapsed(_t),
                          error="Toyhouse took the upload but PawPoller couldn't find the new image in your gallery. "
                                "Check Toyhouse before posting again.")
