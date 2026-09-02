"""SoFurry platform poster.

Uses SoFurryClient (clients/sf/client.py) against SoFurry's **official API v1**,
authenticated with a Personal Access Token (3.4.0 — replaced the Laravel login +
OAuth2-PKCE bridge). Post + edit + replace.

Post flow (official API):
  1. PUT  /v1/submission                    → mint an empty private draft
  2. POST /v1/submission/{id}/content       → upload each chapter's HTML (>= 1 KB)
  3. POST /v1/submission/{id}               → metadata + privacy (+ chapter titles)

Edit flow:
  POST /v1/submission/{id}                  → update metadata (+ content refresh)

Content refresh note (3.4.0): the official API **cannot delete content**, so a
refresh REPLACES each existing chapter in place rather than uploading new items and
deleting the old ones. If a story's chapter count shrinks, the surplus items cannot
be removed by any API call and are logged for manual deletion in the SoFurry UI.

Rating mapping:
  General → 0 (Clean), Mature → 10, Adult → 20

Privacy mapping (SF supports a real draft state):
  privacy=1  Private    (owner-only — used by extra["draft"] = True)
  privacy=2  Unlisted   (accessible by direct link, not in feeds/search)
  privacy=3  Public     (default — listed in feeds/search)

Draft mode:
  Set ``package.extra["draft"] = True`` to create the submission as
  Private (privacy=1). The submission is uploaded with full content and
  metadata but ONLY the owner (logged in) can see it. Switch to public
  later via the SF UI or by calling ``edit_submission(privacy=3)``.

Visibility override:
  Set ``package.extra["privacy"] = 1|2|3`` (or "private"/"unlisted"/"public")
  to explicitly choose. Wins over the draft default.
"""

from __future__ import annotations

import logging
import re

import config
from posting import story_reader
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage
from clients.sf.client import SoFurryClient

logger = logging.getLogger(__name__)


_PRIVACY_PUBLIC = 3
_PRIVACY_UNLISTED = 2
_PRIVACY_PRIVATE = 1

_CHAPTER_PREFIX_RE = re.compile(
    r"^(?:Chapter|Part|Prelude|Epilogue)\s*\d*\s*[:\-\u2014\u2013]\s*",
    re.IGNORECASE,
)


def _strip_chapter_prefix(title: str) -> str:
    if not title:
        return title
    stripped = _CHAPTER_PREFIX_RE.sub("", title).strip()
    return stripped or title


def _normalize_privacy(value) -> int | None:
    """Convert a privacy override (int 1-3 or string) to the SF numeric code."""
    if value is None:
        return None
    if isinstance(value, int):
        return value if value in (1, 2, 3) else None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("private", "1", "draft"):
            return _PRIVACY_PRIVATE
        if v in ("unlisted", "2"):
            return _PRIVACY_UNLISTED
        if v in ("public", "3"):
            return _PRIVACY_PUBLIC
    return None


class SoFurryPoster(PlatformPoster):

    platform_id = "sf"
    # SoFurry's own WAF blocks the GCP range outright (api./www./developer. all
    # 403 with a 5-byte body), so this looks like a desktop-only platform — but
    # SF is PROXY_REQUIRED, so the client always routes through the CF Worker
    # and posting works from the server. "any" is correct; the proxy is the fix.
    requires_mode = "any"
    platform_name = "SoFurry"
    supports_edit = True
    supports_file_replace = True
    min_post_interval = 5
    max_file_size = 512 * 1024  # 512 KB
    accepted_file_types = ["txt", "html", "png", "jpg", "jpeg", "gif", "webp", "mp3"]

    def __init__(self):
        self._client: SoFurryClient | None = None
        self._tmp_files: list[str] = []

    async def _ensure_client(self) -> SoFurryClient:
        if self._client and self._client.has_token:
            return self._client

        settings = config.get_settings()
        creds = self._resolve_creds("sf", settings)
        api_token = creds.get("sf_api_token", "")
        display_name = creds.get("sf_display_name", "")
        if not api_token:
            raise RuntimeError(
                "SoFurry is not connected — add a Personal Access Token in Settings"
            )

        # CF proxy settings are global (not per-account).
        proxy_url = settings.get("cf_worker_url", "")
        proxy_key = settings.get("cf_worker_key", "")

        self._client = SoFurryClient(
            api_token=api_token,
            display_name=display_name,
            proxy_url=proxy_url,
            proxy_key=proxy_key,
        )
        return self._client

    async def _set_chapter_titles(
        self,
        client: SoFurryClient,
        submission_id: str,
        story: story_reader.StoryInfo,
    ) -> None:
        """Set chapter titles on all content items of a submission.

        SF creates content items untitled by default. This reads each
        contentId (in stored order) via the API, then sets the title from
        story.chapters in order.
        """
        content_ids = await client.get_content_ids(submission_id)
        if not content_ids:
            logger.warning(
                "SF: no content items found for %s — skipping chapter titles",
                submission_id,
            )
            return

        local_chapters = sorted(story.chapters, key=lambda c: c.index)
        for cid, local_ch in zip(content_ids, local_chapters):
            title = _strip_chapter_prefix(local_ch.title)
            if not title:
                continue
            try:
                await client.set_content_title(submission_id, cid, title)
                logger.info("SF: Set chapter title '%s' on content %s", title, cid)
            except Exception as e:
                logger.warning("SF: Failed to set title on content %s: %s", cid, e)

    def _read_sf_chapter_file(self, story: story_reader.StoryInfo, ch_idx: int) -> str | None:
        """Resolve a chapter's SoFurry HTML file path."""
        ch = next((c for c in story.chapters if c.index == ch_idx), None)
        if not ch:
            return None

        sf_html = story.path / "Chapters" / "SoFurry_HTML"
        if sf_html.is_dir():
            base = ch.filename.replace(".md", "") if ch.filename else f"Chapter_{ch_idx}"
            exact = sf_html / f"{base}.html"
            if exact.is_file():
                return str(exact)
            for candidate in sorted(
                sf_html.glob(f"Chapter_{ch_idx}_*.html"),
                key=lambda c: len(c.name),
            ):
                return str(candidate)
        return None

    def _read_sf_front_matter(self, story: story_reader.StoryInfo) -> str:
        """Extract front matter (title, subtitle, warning, disclaimer) from
        the full-story SoFurry HTML. Returns everything before the first
        chapter heading (<h3>) so it can be prepended to chapter 1."""
        html_dir = story.path / "HTML"
        for f in sorted(html_dir.glob("*_SoFurry.html")):
            try:
                content = f.read_text(encoding="utf-8")
                # New (TipTap) format: the first chapter heading is an <h2>;
                # front matter is everything before it.
                idx = content.find("<h2")
                if idx > 0:
                    return content[:idx]
                # Legacy formats: take everything up to and including the first
                # horizontal rule after the disclaimer (<hr /> or <hr>), or an <h3>.
                idx = content.find("<h3")
                if idx > 0:
                    return content[:idx]
                for hr in ("<hr />", "<hr>"):
                    idx = content.find(hr)
                    if idx > 0:
                        return content[:idx + len(hr)]
            except Exception:
                pass
        return ""

    def _read_sf_chapter_content(self, story: story_reader.StoryInfo, ch_idx: int) -> str | None:
        """Read a single chapter's SoFurry HTML for upload.

        For chapter 1: prepends the front matter (title, warning,
        disclaimer) from the full-story SoFurry HTML so the first
        chapter displays the story header. Chapters 2+ are body-only.
        """
        path = self._read_sf_chapter_file(story, ch_idx)
        if not path:
            return None
        if ch_idx == 1:
            front_matter = self._read_sf_front_matter(story)
            if front_matter:
                import tempfile
                body = open(path, "r", encoding="utf-8").read()
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".html", encoding="utf-8", delete=False,
                )
                tmp.write(front_matter + "\n" + body)
                tmp.close()
                self._tmp_files.append(tmp.name)
                return tmp.name
        return path

    def _cleanup_tmp_files(self):
        import os
        for f in self._tmp_files:
            try:
                os.unlink(f)
            except OSError:
                pass
        self._tmp_files.clear()

    async def post(self, package: StoryUploadPackage) -> PostResult:
        """Create a new SoFurry submission, with chaptered support.

        For multi-chapter stories: creates the submission with chapter 1,
        then appends chapters 2..N via POST to /ui/submission/{id}/content
        (SF's content endpoint APPENDS chapters, it doesn't replace).

        Visibility behaviour:
          - Default: privacy=3 (Public) — listed in feeds and search
          - ``extra["draft"] = True`` → privacy=1 (Private) — owner-only
          - ``extra["privacy"] = 1|2|3`` → explicit override (wins over draft)

        Returns the submission ID and URL on success.
        """
        _t = self._start_timer()
        try:
            client = await self._ensure_client()

            # Artwork (single image) — bypass the chaptered-story machinery and
            # post the image directly as a SoFurry Artwork submission.
            if package.file_type in ("png", "jpg", "jpeg", "gif", "webp"):
                return await self._post_image(client, package, _t)

            # Resolve chapter 1 file (if chaptered) or full-story file
            story = story_reader.load_story(package.story_name)
            has_chapters = bool(story.chapters) and story.total_chapters > 1

            ch1_file = package.file_path
            if has_chapters:
                ch1_path = self._read_sf_chapter_content(story, 1)
                if ch1_path:
                    ch1_file = ch1_path

            if not ch1_file:
                return PostResult(
                    success=False, error="No file for SoFurry upload",
                    duration_seconds=self._elapsed(_t),
                )

            rating = _rating_to_sf(package.rating)

            # Determine privacy. extra["privacy"] wins over draft default.
            draft_mode = bool(package.extra.get("draft", False))
            explicit_privacy = _normalize_privacy(package.extra.get("privacy"))
            if explicit_privacy is not None:
                privacy = explicit_privacy
            elif draft_mode:
                privacy = _PRIVACY_PRIVATE
            else:
                privacy = _PRIVACY_PUBLIC

            privacy_label = {1: "Private", 2: "Unlisted", 3: "Public"}.get(privacy, str(privacy))
            logger.info(
                "SF: Posting %r as %s (draft=%s, privacy=%d, chapters=%d)",
                package.title, privacy_label, draft_mode, privacy,
                story.total_chapters if has_chapters else 1,
            )

            result = await client.create_submission(
                ch1_file,
                title=package.title,
                description=package.description,
                tags=package.tags,
                category=20,  # Writing
                sub_type=21,  # Short story
                rating=rating,
                privacy=privacy,
                thumbnail_path=package.thumbnail_path,
            )

            submission_id = result.get("submission_id", "")
            url = result.get("url", "")

            # Add remaining chapters (2..N): each upload_content() appends another
            # content item to the submission's content[] array (in order).
            if has_chapters and submission_id:
                remaining = [c for c in story.chapters if c.index > 1]
                for ch in sorted(remaining, key=lambda c: c.index):
                    ch_path = self._read_sf_chapter_content(story, ch.index)
                    if not ch_path:
                        logger.warning(
                            "SF: Skipping chapter %d for %s (no SoFurry HTML)",
                            ch.index, story.name,
                        )
                        continue
                    try:
                        await client.upload_content(submission_id, ch_path)
                        logger.info(
                            "SF: Added chapter %d to submission %s",
                            ch.index, submission_id,
                        )
                    except Exception as ch_err:
                        logger.warning(
                            "SF: Chapter %d upload failed for %s: %s",
                            ch.index, submission_id, ch_err,
                        )

            # Set chapter titles on all content items. SF creates them as
            # "Untitled Chapter" by default. Fetch the submission to get
            # each content item's contentId, then POST the title.
            if has_chapters and submission_id:
                try:
                    await self._set_chapter_titles(client, submission_id, story)
                except Exception as title_err:
                    logger.warning("SF: Chapter title setting failed: %s", title_err)

            # SAFETY: when posting as draft/private, verify the submission
            # actually landed Private. get_submission_detail strips the privacy
            # field, so read the full object from the official API instead.
            if privacy == _PRIVACY_PRIVATE and submission_id:
                try:
                    raw = await client.get_submission(submission_id)
                    if raw:
                        server_privacy = raw.get("privacy")
                        if server_privacy is not None and int(server_privacy) != _PRIVACY_PRIVATE:
                            logger.warning(
                                "SF: SAFETY WARN — submission %s posted with privacy=%s "
                                "(expected 1/Private). Inspect at %s",
                                submission_id, server_privacy, url,
                            )
                        elif server_privacy is None:
                            logger.info(
                                "SF: post-flight check found no privacy field for %s "
                                "(API may not expose it); trusting create flow",
                                submission_id,
                            )
                        else:
                            logger.info(
                                "SF: verified submission %s is Private (privacy=1)",
                                submission_id,
                            )
                    else:
                        logger.warning(
                            "SF: post-flight verify returned nothing for %s — "
                            "trusting create flow", submission_id,
                        )
                except Exception as verify_err:
                    logger.warning(
                        "SF: post-flight privacy verification failed for %s: %s",
                        submission_id, verify_err,
                    )

            return PostResult(
                success=True,
                external_id=submission_id,
                external_url=url,
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("SF post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
        finally:
            self._cleanup_tmp_files()

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        """Edit metadata AND refresh content on an existing SoFurry submission.

        SF's edit_submission() only touches metadata (title/desc/tags/rating/
        privacy) — it does NOT replace the story content. To keep drifted
        local files in sync with what's on the platform, we also call
        replace_file() when a file is attached to the package. If the caller
        only wants a metadata refresh, they can set
        ``package.extra["skip_content_refresh"] = True``.

        By default this PRESERVES the existing privacy state (whatever the
        submission is currently set to on the server). To change privacy
        explicitly, set ``package.extra["privacy"]`` (1=Private, 2=Unlisted,
        3=Public) or use ``package.extra["draft"] = True`` for Private.
        """
        _t = self._start_timer()
        try:
            client = await self._ensure_client()
            rating = _rating_to_sf(package.rating)

            # Optional privacy override on edit (defaults: preserve current state)
            draft_mode = bool(package.extra.get("draft", False))
            explicit_privacy = _normalize_privacy(package.extra.get("privacy"))
            privacy: int | None
            if explicit_privacy is not None:
                privacy = explicit_privacy
            elif draft_mode:
                privacy = _PRIVACY_PRIVATE
            else:
                privacy = None  # None = preserve existing state on the server

            result = await client.edit_submission(
                external_id,
                title=package.title,
                description=package.description,
                tags=package.tags,
                rating=rating,
                privacy=privacy,
            )

            # Refresh content on SF. For multi-chapter stories, we need
            # to replace ALL content items with per-chapter files (not use
            # replace_file which treats the whole story as one item).
            skip_content = bool(package.extra.get("skip_content_refresh", False))

            # Artwork stops here. `post()` branches on exactly this test before
            # it reaches the story machinery; `edit()` did not, so editing a
            # SoFurry *image* fell through to load_story() and died with
            # "Story folder not found: /app/story-archive/<artwork name>" —
            # observed on prod editing `Rear_View` (naOMVbXe) on 2026-08-19.
            #
            # The metadata edit above has already succeeded by this point, which
            # is the whole of what an artwork edit means: there are no chapters
            # to refresh, and the image itself is replaced through
            # `replace_file`, not here. Returning success is therefore accurate,
            # and it matters — the exception was propagating out and flipping a
            # live, correctly-posted submission's publication row to `failed`.
            if package.file_type in ("png", "jpg", "jpeg", "gif", "webp"):
                logger.info("SF: metadata updated for artwork %s (no content refresh)",
                            external_id)
                return PostResult(
                    success=True,
                    external_id=external_id,
                    external_url=result.get("url", ""),
                    duration_seconds=self._elapsed(_t),
                )

            story = story_reader.load_story(package.story_name)
            has_chapters = bool(story.chapters) and story.total_chapters > 1

            if not skip_content and has_chapters:
                # Chapter-aware content refresh. 3.4.0: the official API has no
                # delete, so instead of "upload all new, then delete all old" this
                # REPLACES each existing content item in place and only uploads for
                # chapters beyond the current count. That also removes the window in
                # which the submission held both copies.
                try:
                    old_ids = await client.get_content_ids(external_id)
                    chapters = sorted(story.chapters, key=lambda c: c.index)

                    for pos, ch in enumerate(chapters):
                        ch_path = self._read_sf_chapter_content(story, ch.index)
                        if not ch_path:
                            continue
                        title = _strip_chapter_prefix(ch.title) or None
                        try:
                            if pos < len(old_ids):
                                with open(ch_path, "r", encoding="utf-8") as fh:
                                    body = fh.read()
                                await client.update_content(
                                    external_id, old_ids[pos],
                                    body_html=body, title=title,
                                )
                                logger.info("SF: Replaced chapter %d in place (%s)",
                                            ch.index, old_ids[pos])
                            else:
                                new_cid = await client.upload_content(external_id, ch_path)
                                if new_cid and title:
                                    await client.set_content_title(external_id, new_cid, title)
                                logger.info("SF: Added chapter %d for %s", ch.index, external_id)
                        except Exception as up_err:
                            logger.warning("SF: Chapter %d refresh failed: %s", ch.index, up_err)

                    # Surplus items when a story LOSES chapters. No API call can
                    # remove these — flag them rather than leaving stale text live.
                    if len(old_ids) > len(chapters):
                        logger.warning(
                            "SF: %s now has %d chapters but %d content items remain. "
                            "SoFurry's API cannot delete content — remove the surplus "
                            "manually at https://sofurry.com/s/%s/edit (ids: %s)",
                            external_id, len(chapters), len(old_ids), external_id,
                            ", ".join(old_ids[len(chapters):]),
                        )
                except Exception as ch_err:
                    logger.warning("SF: Chaptered content refresh failed: %s", ch_err)

            elif not skip_content and package.file_path:
                # Single-file content replacement (non-chaptered)
                file_result = await self.replace_file(external_id, package.file_path)
                if not file_result.success:
                    logger.warning(
                        "SF edit: metadata updated but content replace failed for %s: %s",
                        external_id, file_result.error,
                    )
                    return PostResult(
                        success=False,
                        external_id=external_id,
                        external_url=result.get("url", ""),
                        error=f"Metadata updated but content refresh failed: {file_result.error}",
                        duration_seconds=self._elapsed(_t),
                    )

            # Refresh chapter titles (works for both full-update and
            # metadata-only — cheap JSON POST per content item).
            if has_chapters:
                try:
                    await self._set_chapter_titles(client, external_id, story)
                except Exception as title_err:
                    logger.warning("SF: Chapter title refresh failed: %s", title_err)

            return PostResult(
                success=True,
                external_id=external_id,
                external_url=result.get("url", ""),
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("SF edit failed for %s: %s", external_id, e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
        finally:
            self._cleanup_tmp_files()

    async def probe_exists(self, external_id: str) -> bool | None:
        """Check whether an SF submission still exists.

        Uses the authenticated client so drafts (privacy=1) are visible too.
        Returns False only when the server actively says the submission is
        gone (404 / deleted). Network errors return None so the caller
        doesn't incorrectly mark a still-live submission as deleted.
        """
        try:
            client = await self._ensure_client()
            # get_submission returns {} on a 404 and raises on transport errors,
            # which is exactly the True / False / None contract this needs.
            sub = await client.get_submission(external_id)
            if not sub:
                return False
            return bool(sub.get("id") or sub.get("title"))
        except Exception as e:
            logger.warning("SF probe_exists(%s) failed: %s", external_id, e)
            return None

    async def probe_draft_state(self, external_id: str) -> bool | None:
        """True if the SF submission is unpublished / scheduled-future.

        SF's submission JSON exposes ``publishedAt``. Empty string / null /
        `0000-00-00` sentinel / future ISO dates all map to "draft" — anything
        else is live. Returns None on transport / parse errors.
        """
        try:
            client = await self._ensure_client()
            data = await client.get_submission(external_id)
            if not isinstance(data, dict) or not data:
                return None
            published = (data.get("publishedAt") or "").strip()
            if not published or published.startswith("0000"):
                return True
            # Future-dated → still draft until that timestamp passes.
            try:
                from datetime import datetime, timezone
                # SF returns ISO-ish strings; tolerant parse.
                dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt > datetime.now(timezone.utc)
            except Exception:
                # If we can't parse it, presence of a non-zero string is
                # good enough to treat as live.
                return False
        except Exception as e:
            logger.warning("SF probe_draft_state(%s) failed: %s", external_id, e)
            return None

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        """Replace content on an existing SF submission.

        3.4.0 simplified this considerably. The old flow had to APPEND a new
        content item, then delete the old ones — SF refused to delete the last
        remaining item, so the order mattered and the submission briefly held both
        copies. The official API updates a content item **in place**, so the whole
        dance collapses to a single call that cannot leave the submission in a
        half-updated state.

        Falls back to appending only when the submission somehow has no content
        item to replace.
        """
        _t = self._start_timer()
        try:
            client = await self._ensure_client()

            old_content_ids = await client.get_content_ids(external_id)

            try:
                if old_content_ids:
                    # Replace the FIRST item in place; any others belong to a
                    # chaptered work and are handled by the chapter-aware path.
                    with open(file_path, "r", encoding="utf-8") as fh:
                        body = fh.read()
                    await client.update_content(external_id, old_content_ids[0],
                                                body_html=body)
                    logger.info("SF: Replaced content %s on submission %s in place",
                                old_content_ids[0], external_id)
                else:
                    await client.upload_content(external_id, file_path)
                    logger.info("SF: Submission %s had no content — uploaded a new item",
                                external_id)
            except Exception as up_err:
                return PostResult(
                    success=False,
                    error=f"Content replace failed: {up_err}",
                    duration_seconds=self._elapsed(_t),
                )

            if len(old_content_ids) > 1:
                logger.info(
                    "SF: %s has %d content items; replace_file updated only the first. "
                    "Use the chapter-aware edit path for multi-chapter works.",
                    external_id, len(old_content_ids),
                )
            return PostResult(
                success=True,
                external_id=external_id,
                external_url=f"https://sofurry.com/s/{external_id}",
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("SF file replace failed for %s: %s", external_id, e)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    # SoFurry caps story HTML at 512 KB but accepts much larger images.
    _IMAGE_MAX = 30 * 1024 * 1024  # 30 MB for artwork

    async def _post_image(self, client, package: StoryUploadPackage, _t) -> PostResult:
        """Post a single image as a SoFurry Artwork submission.

        Same beta /api create flow as stories, but category=10 (Artwork) and an
        image content item (the client's upload_content auto-detects the image
        MIME). Honors the draft/privacy extras like the story path.
        """
        if not package.file_path:
            return PostResult(success=False, error="No image for SoFurry upload",
                              duration_seconds=self._elapsed(_t))
        rating = _rating_to_sf(package.rating)
        draft_mode = bool(package.extra.get("draft", False))
        explicit_privacy = _normalize_privacy(package.extra.get("privacy"))
        if explicit_privacy is not None:
            privacy = explicit_privacy
        elif draft_mode:
            privacy = _PRIVACY_PRIVATE
        else:
            privacy = _PRIVACY_PUBLIC

        settings = config.get_settings()
        try:
            sub_type = int(package.extra.get("sub_type")
                           or settings.get("artwork_sf_sub_type") or 11)
        except (TypeError, ValueError):
            sub_type = 11  # Drawing

        result = await client.create_submission(
            package.file_path,
            title=package.title,
            description=package.description,
            tags=package.tags,
            category=10,        # Artwork
            sub_type=sub_type,  # 11 = Drawing (default)
            rating=rating,
            privacy=privacy,
        )
        return PostResult(
            success=True,
            external_id=result.get("submission_id", ""),
            external_url=result.get("url", ""),
            duration_seconds=self._elapsed(_t),
        )

    def validate(self, package: StoryUploadPackage) -> list[str]:
        is_image = package.file_type in ("png", "jpg", "jpeg", "gif", "webp")
        errors = []
        if not package.title:
            errors.append("Title is required")
        if len(package.tags) < 2:
            errors.append(f"SoFurry requires at least 2 tags (got {len(package.tags)})")
        if package.file_path:
            import os
            if not os.path.isfile(package.file_path):
                errors.append(f"File not found: {package.file_path}")
            else:
                size = os.path.getsize(package.file_path)
                cap = self._IMAGE_MAX if is_image else self.max_file_size
                if size > cap:
                    label = "30MB" if is_image else "512KB"
                    errors.append(f"SoFurry max file size is {label} (got {size / 1024:.0f}KB)")
        return errors


def _rating_to_sf(rating: str) -> int:
    r = rating.lower()
    if r in ("adult", "explicit", "nsfw"):
        return 20
    elif r in ("mature", "questionable"):
        return 10
    return 0
