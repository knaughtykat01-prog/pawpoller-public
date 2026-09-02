"""Inkbunny platform poster.

Uses the existing InkbunnyClient (clients/ib/client.py) with the new
upload_submission() and edit_submission() methods. Inkbunny has a fully
documented public API for posting, making this the most reliable platform.

Post flow:
  1. ensure_session(cached_sid)
  2. upload_submission(file) → submission_id
  3. edit_submission(submission_id, title, desc, tags, ratings, visibility=yes)

Edit flow:
  1. edit_submission(existing_id, updated fields)

Rating mapping:
  General  → all tags "no"
  Mature   → tag[2]="yes" (Nudity — Nonsexual)
  Adult    → tag[4]="yes" (Sexual Situations — Strong), tag[5]="yes"
"""

from __future__ import annotations

import logging

import config
from clients.ib.client import InkbunnyClient
from database.db import get_connection
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage

logger = logging.getLogger(__name__)


class InkbunnyPoster(PlatformPoster):

    platform_id = "ib"
    platform_name = "Inkbunny"
    supports_edit = True
    supports_file_replace = True
    min_post_interval = 5
    max_file_size = 200 * 1024 * 1024  # 200 MB
    accepted_file_types = ["txt", "doc", "rtf", "pdf", "png", "jpg", "jpeg", "gif", "webp", "mp3", "mp4"]

    def __init__(self, account_id: int | None = None):
        self._client: InkbunnyClient | None = None
        # Which Inkbunny account to post as. None → the default account.
        self.account_id = account_id

    async def _ensure_client(self) -> InkbunnyClient:
        """Get or create an authenticated Inkbunny client for this account."""
        if self._client and self._client.sid:
            return self._client

        conn = get_connection()
        try:
            from database import accounts as _accts
            acct_id = self.account_id
            if acct_id is None:
                acct_id = _accts.get_default_account_id(conn, "ib", create=True)
                self.account_id = acct_id
            acct = _accts.get_account(conn, acct_id)
            is_default = bool(acct["is_default"]) if acct else True
            creds = config.resolve_account_credentials("ib", acct_id, is_default)
            username = creds.get("username", "")
            password = creds.get("password", "")
            if not username or not password:
                raise RuntimeError("Inkbunny credentials not configured")
            # Reuse this account's cached SID (no longer the singleton id=1).
            row = conn.execute(
                "SELECT sid FROM session_cache WHERE account_id = ?", (acct_id,)).fetchone()
            cached_sid = row["sid"] if row else None
        finally:
            conn.close()

        self._client = InkbunnyClient(username=username, password=password)
        await self._client.ensure_session(cached_sid)
        return self._client

    async def post(self, package: StoryUploadPackage) -> PostResult:
        """Upload a new submission to Inkbunny.

        Visibility behavior:
          - Default: publishes immediately (visibility="yes" — visible to all
            and notifies watchers)
          - Set `package.extra["draft"] = True` → submission stays HIDDEN.
            The submission is created and metadata is set but it is not
            visible to the public. Owner can change visibility later in the IB
            UI or via another edit_submission call with visibility="yes".
          - Set `package.extra["visibility"] = "yes_nowatch"` → visible but
            doesn't notify watchers.
        """
        _t = self._start_timer()

        draft_mode = bool(package.extra.get("draft", False))
        # Override (e.g. "yes_nowatch") wins over draft default
        explicit_visibility = package.extra.get("visibility")

        try:
            client = await self._ensure_client()

            if not package.file_path:
                return PostResult(
                    success=False,
                    error="No file path provided for Inkbunny upload",
                    duration_seconds=self._elapsed(_t),
                )

            # Determine submission type
            sub_type = "4"  # writing (default for stories)
            if package.file_type in ("png", "jpg", "jpeg", "gif", "webp"):
                sub_type = "1"  # picture (artwork upload)

            # Step 1: Upload file (+ thumbnail if available)
            submission_id = await client.upload_submission(
                package.file_path, submission_type=sub_type,
                thumbnail_path=package.thumbnail_path,
            )

            # Step 2: Read story content from BBCode file for the story text field
            story_text = None
            if package.file_path and package.file_type == "bbcode":
                with open(package.file_path, "r", encoding="utf-8") as f:
                    story_text = f.read()

            # Step 3: Set metadata. Visibility chosen based on draft mode.
            #  - draft_mode = True  → don't pass visibility (stays hidden)
            #  - explicit set      → use what the caller asked for
            #  - default          → "yes" (publish + notify watchers)
            rating_tags = _rating_to_tags(package.rating)
            keywords = ", ".join(package.tags)

            if explicit_visibility is not None:
                visibility = explicit_visibility
            elif draft_mode:
                visibility = None  # leave hidden — IB defaults to hidden
            else:
                visibility = "yes"  # legacy behavior: publish

            edit_kwargs = {
                "title": package.title[:100],
                "description": package.description,
                "story": story_text,
                "keywords": keywords,
                **rating_tags,
            }
            if visibility is not None:
                edit_kwargs["visibility"] = visibility

            await client.edit_submission(submission_id, **edit_kwargs)

            url = f"https://inkbunny.net/s/{submission_id}"
            logger.info(
                "IB: Posted submission %d (draft=%s, visibility=%s) — %s",
                submission_id, draft_mode, visibility or "hidden", url,
            )
            return PostResult(
                success=True,
                external_id=str(submission_id),
                external_url=url,
                duration_seconds=self._elapsed(_t),
            )

        except Exception as e:
            logger.error("IB post failed: %s", e, exc_info=True)
            return PostResult(
                success=False,
                error=str(e),
                duration_seconds=self._elapsed(_t),
            )

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        """Edit metadata and story text on an existing Inkbunny submission."""
        _t = self._start_timer()
        try:
            client = await self._ensure_client()
            submission_id = int(external_id)
            rating_tags = _rating_to_tags(package.rating)
            keywords = ", ".join(package.tags)

            # Read updated story content from BBCode file. Skipped when
            # package.extra["skip_content_refresh"] is set — lets the
            # caller push just metadata and leave the story text alone.
            story_text = None
            skip_content = bool(package.extra.get("skip_content_refresh", False))
            if (
                not skip_content
                and package.file_path
                and package.file_type == "bbcode"
            ):
                with open(package.file_path, "r", encoding="utf-8") as f:
                    story_text = f.read()

            await client.edit_submission(
                submission_id,
                title=package.title[:100],
                description=package.description,
                story=story_text,
                keywords=keywords,
                **rating_tags,
            )

            url = f"https://inkbunny.net/s/{submission_id}"
            return PostResult(
                success=True,
                external_id=external_id,
                external_url=url,
                duration_seconds=self._elapsed(_t),
            )

        except Exception as e:
            logger.error("IB edit failed for %s: %s", external_id, e, exc_info=True)
            return PostResult(
                success=False,
                error=str(e),
                duration_seconds=self._elapsed(_t),
            )

    async def probe_exists(self, external_id: str) -> bool | None:
        """Check whether an IB submission still exists via api_submissions.php.

        Uses the authenticated client so drafts (hidden=yes) are visible.
        Returns False only when IB actively says the submission isn't
        available (empty submissions[] in the response).
        """
        try:
            client = await self._ensure_client()
            details = await client.get_submission_details([int(external_id)])
            return bool(details)
        except Exception as e:
            logger.warning("IB probe_exists(%s) failed: %s", external_id, e)
            return None

    async def probe_draft_state(self, external_id: str) -> bool | None:
        """True if the IB submission is held / not publicly listed.

        IB's `public` field reads "yes" / "no" — "no" covers held / under-
        review / friends-only / scrap-equivalent states. Treat all of those
        as "draft" so the user gets a single visual signal in the matrix
        even though IB has a slightly fuzzier private/public spectrum than
        FA's binary scrap toggle.
        """
        try:
            client = await self._ensure_client()
            details = await client.get_submission_details([int(external_id)])
            if not details:
                return None
            public = (details[0].public or "").strip().lower()
            if public in ("yes", "no"):
                return public == "no"
            return None
        except Exception as e:
            logger.warning("IB probe_draft_state(%s) failed: %s", external_id, e)
            return None

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        """Replace the story text on an existing Inkbunny submission.

        Reads the BBCode file and pushes it via edit_submission(story=...).
        Only the story body is updated — title, description, tags, and
        visibility are preserved (IB API blanks omitted fields, so we don't
        send them).
        """
        _t = self._start_timer()
        try:
            client = await self._ensure_client()
            with open(file_path, "r", encoding="utf-8") as f:
                story_text = f.read()
            await client.edit_submission(
                int(external_id),
                story=story_text,
            )
            return PostResult(
                success=True,
                external_id=external_id,
                external_url=f"https://inkbunny.net/s/{external_id}",
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("IB file replace failed for %s: %s", external_id, e)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors = super().validate(package)
        if len(package.tags) < 4:
            errors.append(f"Inkbunny requires at least 4 tags (got {len(package.tags)})")
        return errors


def _rating_to_tags(rating: str) -> dict:
    """Convert a rating string to IB rating tag flags."""
    r = rating.lower()
    if r in ("adult", "explicit", "nsfw"):
        return {"rating_tag_2": "yes", "rating_tag_3": "no", "rating_tag_4": "yes", "rating_tag_5": "yes"}
    elif r in ("mature", "questionable"):
        return {"rating_tag_2": "yes", "rating_tag_3": "no", "rating_tag_4": "no", "rating_tag_5": "no"}
    else:  # general
        return {"rating_tag_2": "no", "rating_tag_3": "no", "rating_tag_4": "no", "rating_tag_5": "no"}
