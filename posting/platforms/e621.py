"""e621 platform poster.

e621 is an art gallery. We upload a single image plus a tag set and rating via
the official REST API (``POST /uploads.json``), authenticated with the same
HTTP Basic **username + API key** used for polling — no browser session.

Caveats that make e621 stricter than the other galleries:
  - Uploads hit a **moderation queue** and must be approved by janitors.
  - e621 demands an **accurate, real tag set** (not one keyword) and a valid
    rating; badly-tagged posts get flagged. We enforce a small tag floor.
  - **Duplicates are rejected** (by file hash); the client surfaces the
    existing post's URL in the error.
  - Editing a live post is a **three-way merge**, not an overwrite: e621
    wants the old value beside each new one and reconciles concurrent
    changes. Tags are communal, so ``edit()`` merges rather than replaces
    by default. There is **no title field** anywhere in the post model.
  - A **source** is strongly encouraged — pass one via the artwork's
    per-platform ``source`` override (package.extra["source"]).

Rating mapping (PawPoller rating -> e621 s/q/e):
  general / safe / sfw       -> s
  mature / questionable      -> q
  adult / explicit / nsfw    -> e   (also the fallback for anything unknown,
                                     since under-rating adult content violates
                                     e621 policy)
"""

from __future__ import annotations

import logging

import config
from clients.e621.client import E621Client
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage

logger = logging.getLogger(__name__)

# e621 wants a genuine tag set, not a single keyword. A modest floor protects
# the user's standing without blocking legitimate posts.
_MIN_TAGS = 4


class E621Poster(PlatformPoster):

    platform_id = "e621"
    platform_name = "e621"
    # e621 edits are an ordinary PATCH /posts/{id}.json with the same HTTP
    # Basic auth as everything else here (3.33.0). The long-standing False
    # was inherited from a spec note that e621 needed "a separate tag-edit
    # API"; it does not. File replacement genuinely is impossible — a new
    # image means a new post, related to the old one as a parent.
    supports_edit = True
    supports_file_replace = False
    min_post_interval = 5
    max_file_size = 100 * 1024 * 1024  # e621 accepts large files (100 MB)
    accepted_file_types = ["png", "jpg", "jpeg", "gif", "webp", "webm"]
    requires_mode = "any"              # official API works from the server

    def __init__(self):
        self._client: E621Client | None = None

    async def _ensure_client(self) -> E621Client:
        settings = config.get_settings()
        creds = self._resolve_creds("e621", settings)
        username = creds.get("e621_username", "")
        api_key = creds.get("e621_api_key", "")
        if not (username and api_key):
            raise RuntimeError("e621 credentials not configured "
                               "(e621_username + e621_api_key)")
        if self._client is None:
            from polling.cf_proxy import proxy_kwargs
            self._client = E621Client(username=username, api_key=api_key,
                                      **proxy_kwargs(settings, "e621"))
        else:
            self._client.update_credentials(username, api_key)
        return self._client

    async def post(self, package: StoryUploadPackage) -> PostResult:
        """Upload one image to e621."""
        _t = self._start_timer()
        try:
            client = await self._ensure_client()
            rating = _rating_to_e621(package.rating)
            tag_string = " ".join(package.tags)
            source = str(package.extra.get("source", "") or "")

            result = await client.upload_post(
                tag_string=tag_string,
                rating=rating,
                file_path=package.file_path or "",
                source=source,
                description=package.description or "",
            )
            return PostResult(
                success=True,
                external_id=result.get("post_id", ""),
                external_url=result.get("url", ""),
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("e621 post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e),
                              duration_seconds=self._elapsed(_t))

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        """Push canonical metadata to an existing e621 post.

        WARNING: **e621 has no title.** A post is tags + rating + description +
        sources + parent, and nothing else — checked against the live API
        rather than read off the edit form, whose JSON carries no title key at
        any nesting level. ``package.title`` is therefore dropped here. It is
        deliberately NOT folded into the first line of the description: that
        would rewrite the visible caption of every synced post, and on e621
        the description is where the artist credit lives.

        Tags **merge** by default — ours are added, anything a janitor or
        another user put there is kept. e621 tags are communal in a way no
        other gallery PawPoller posts to is, and silently deleting someone
        else's tagging on a routine metadata sync is both rude and the exact
        thing spec 0-A1 forbids. Set ``package.extra["e621_replace_tags"]``
        to push the canonical set exactly, removals included.

        ``extra["skip_content_refresh"]`` is accepted and ignored — there is
        no content to refresh, and Sync-all sets it on every member.
        """
        _t = self._start_timer()
        try:
            client = await self._ensure_client()

            tags = [t for t in (package.tags or []) if str(t).strip()]
            if len(tags) < _MIN_TAGS:
                # The same floor post() enforces. A thin tag set is worse on an
                # edit than on an upload: in replace mode it would strip a
                # well-tagged live post down to a handful.
                return PostResult(
                    success=False,
                    external_id=external_id,
                    error=(f"e621 expects a real tag set - add at least "
                           f"{_MIN_TAGS} tags (got {len(tags)})"),
                    duration_seconds=self._elapsed(_t),
                )

            source = str(package.extra.get("source", "") or "").strip()
            replace = bool(package.extra.get("e621_replace_tags", False))

            result = await client.edit_post(
                external_id,
                tags=tags,
                tag_mode="replace" if replace else "merge",
                rating=_rating_to_e621(package.rating),
                description=package.description or "",
                # None leaves the post's existing sources alone; only push
                # when we actually have one to push.
                sources=[source] if source else None,
                edit_reason=str(package.extra.get("edit_reason", "") or ""),
            )

            return PostResult(
                success=True,
                external_id=external_id,
                external_url=result.get("url", ""),
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("e621 edit failed for %s: %s", external_id, e, exc_info=True)
            return PostResult(success=False, external_id=external_id, error=str(e),
                              duration_seconds=self._elapsed(_t))

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        return PostResult(success=False, error="e621 does not support file replacement")

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors: list[str] = []
        if not package.file_path:
            errors.append("e621 requires an image file")
        if len(package.tags) < _MIN_TAGS:
            errors.append(f"e621 expects a real tag set - add at least "
                          f"{_MIN_TAGS} tags (got {len(package.tags)})")
        if package.file_path:
            import os
            if os.path.isfile(package.file_path):
                size = os.path.getsize(package.file_path)
                if size > self.max_file_size:
                    errors.append(f"File too large: {size / 1024 / 1024:.1f}MB "
                                  f"(max {self.max_file_size / 1024 / 1024:.0f}MB)")
        return errors


def _rating_to_e621(rating: str) -> str:
    r = (rating or "").strip().lower()
    if r in ("s", "safe", "general", "sfw", "g"):
        return "s"
    if r in ("q", "questionable", "mature", "m"):
        return "q"
    # explicit / adult / nsfw and any unknown value -> explicit (under-rating
    # adult content on e621 is a policy violation; over-rating is harmless).
    return "e"
