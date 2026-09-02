"""SquidgeWorld platform poster.

Wraps the SquidgeWorldClient to upload, edit, and manage stories on
SquidgeWorld (an OTW Archive instance — same software as AO3).

Pulls full metadata from story.json via posting.story_reader, including
fandom, category, characters, relationships, warnings, and the
Work_Skin.css that gets uploaded as the work's Work Skin.

Posting flow:
  1. Read StoryInfo from the archive (story.json)
  2. If a Work_Skin.css exists, find or create the named Work Skin on SQW
  3. Trim freeform tags to fit SQW's 75-tag total limit
     (fandom + relationship + character + freeform <= 75)
  4. Create the work with chapter 1 content + full metadata + work_skin_id
  5. For multi-chapter stories, iterate chapters 2..N and add each via
     create_chapter(publish=False) so the work stays in draft state
  6. Returns PostResult with the work_id

Edit flow:
  1. Read StoryInfo from the archive
  2. Refresh the Work Skin (find or create)
  3. Trim tags
  4. Edit the work metadata via edit_work (safe form-fetch + save_button)
  5. For each chapter currently on SQW, edit content via edit_chapter

Rating mapping:
  explicit -> "Explicit"
  mature   -> "Mature"
  teen     -> "Teen And Up Audiences"
  general  -> "General Audiences"
"""

from __future__ import annotations

import asyncio
import logging
import re

import config
from posting import story_reader
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage
from clients.sqw.client import SquidgeWorldClient

logger = logging.getLogger(__name__)


# OTW Archive total-tag limit (fandom + relationship + character + freeform).
# Rating, warnings, categories do NOT count toward this.
OTW_TAG_LIMIT = 75


# Leading "Chapter N:", "Part N:", "Prelude:", "Epilogue:" patterns that
# our story.json titles carry but that OTW Archive auto-prefixes on display.
_CHAPTER_PREFIX_RE = re.compile(
    r"^(?:Chapter|Part|Prelude|Epilogue)\s*\d*\s*[:\-—–]\s*",
    re.IGNORECASE,
)


def _strip_chapter_prefix(title: str) -> str:
    if not title:
        return title
    stripped = _CHAPTER_PREFIX_RE.sub("", title).strip()
    return stripped or title


class SquidgeWorldPoster(PlatformPoster):

    platform_id = "sqw"
    requires_mode = "any"   # Otwarchive, same engine as AO3 — see ao3.py
    platform_name = "SquidgeWorld"
    supports_edit = True
    supports_file_replace = True
    min_post_interval = 5
    max_file_size = 0  # Content pasted as HTML, no file upload
    accepted_file_types = ["html"]

    def __init__(self):
        self._client: SquidgeWorldClient | None = None

    async def _ensure_client(self) -> SquidgeWorldClient:
        """Get or create an authenticated SQW client using AUTHOR credentials."""
        if self._client and self._client._logged_in:
            return self._client

        settings = config.get_settings()
        creds = self._resolve_creds("sqw", settings)
        username = creds.get("sqw_author_username", "") or creds.get("sqw_username", "")
        password = creds.get("sqw_author_password", "") or creds.get("sqw_password", "")
        target_user = creds.get("sqw_target_user", "")
        if not username or not password:
            raise RuntimeError("SquidgeWorld author credentials not configured")

        self._client = SquidgeWorldClient(username, password, target_user)
        if not await self._client.ensure_logged_in():
            raise RuntimeError("SquidgeWorld login failed")
        return self._client

    # ─── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _rating_to_sqw(rating: str) -> str:
        r = (rating or "").lower()
        if r in ("adult", "explicit", "nsfw"):
            return "Explicit"
        if r in ("mature", "questionable"):
            return "Mature"
        if r == "teen":
            return "Teen And Up Audiences"
        return "General Audiences"

    @staticmethod
    def _trim_freeform_tags(
        tags: list[str],
        characters: list[str],
        relationships: list[str],
        fandom: str,
    ) -> list[str]:
        """Trim freeform tags so the total fits SQW's 75-tag limit.

        Total budget = 75 = (fandoms + relationships + characters + freeform).
        Returns the freeform tags trimmed to fit.
        """
        used = 0
        if fandom:
            used += 1  # we always send 1 fandom
        used += len(characters)
        used += len(relationships)
        budget = OTW_TAG_LIMIT - used
        if budget <= 0:
            return []
        if len(tags) <= budget:
            return tags
        return tags[:budget]

    @staticmethod
    def _read_chapter_content(story: story_reader.StoryInfo, ch_idx: int) -> str | None:
        """Resolve a chapter's SquidgeWorld body HTML and return its content.

        Looks first for a SquidgeWorld/ chapter file (preferred — body-only
        with single-line tags), then falls back to Chapters/SoFurry_HTML/.
        """
        # Direct SquidgeWorld file
        ch = next((c for c in story.chapters if c.index == ch_idx), None)
        if not ch:
            return None

        # Try the SquidgeWorld dir first
        sqw_dir = story.path / "SquidgeWorld"
        if sqw_dir.is_dir():
            base = ch.filename.replace(".md", "") if ch.filename else f"Chapter_{ch_idx}"
            # Prefer an EXACT match; avoids grabbing debris files like
            # "Chapter_1_The_Counter_testing_testing_1_2_3.html".
            exact = sqw_dir / f"{base}.html"
            if exact.is_file():
                try:
                    return exact.read_text(encoding="utf-8")
                except Exception:
                    pass
            # Fallback: shortest matching filename.
            for candidate in sorted(
                (c for c in sqw_dir.glob(f"{base}*") if c.suffix == ".html"),
                key=lambda c: len(c.name),
            ):
                try:
                    return candidate.read_text(encoding="utf-8")
                except Exception:
                    pass

            # Last resort: glob for any chapter file matching the index.
            for candidate in sorted(
                sqw_dir.glob(f"Chapter_{ch_idx}_*.html"),
                key=lambda c: len(c.name),
            ):
                try:
                    return candidate.read_text(encoding="utf-8")
                except Exception:
                    pass

        # Fall back to SoFurry HTML chapter
        sf_html = story.path / "Chapters" / "SoFurry_HTML"
        if sf_html.is_dir():
            for candidate in sorted(sf_html.glob(f"Chapter_{ch_idx}_*.html")):
                try:
                    return candidate.read_text(encoding="utf-8")
                except Exception:
                    pass

        return None

    async def _ensure_work_skin(
        self,
        client: SquidgeWorldClient,
        story: story_reader.StoryInfo,
    ) -> str:
        """Find or create the Work Skin for this story, and ALWAYS sync the CSS.

        Returns skin_id (or '' if no Work_Skin.css exists for the story).

        Behavior:
          1. If no Work_Skin.css → return '' (no skin applied)
          2. If skin doesn't exist by title → create new with current CSS
          3. If skin exists → call edit_work_skin to push the current CSS and
             description (auto-refresh, so local edits propagate to SquidgeWorld
             on the next post/edit call). The refresh is best-effort: if it
             fails, log a warning but still return the skin_id so the work
             can be created/updated with the existing skin.
        """
        if not story.work_skin_path or not story.work_skin_path.is_file():
            return ""

        # Skin title convention: "<Story Name> Skin"
        # Strip leading underscores — "_Test_Story" → "Test Story Skin".
        display_name = story.name.lstrip("_").replace("_", " ")
        skin_title = f"{display_name} Skin"
        skin_description = (
            f"Custom Work Skin for '{display_name}' by {story.author}."
        )

        try:
            css = story.work_skin_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("SqW: Could not read Work_Skin.css for %s: %s", story.name, e)
            return ""

        try:
            existing = await client.find_work_skin_by_title(skin_title)
        except Exception as e:
            logger.warning(
                "SqW: Could not search for existing Work Skin for %s: %s",
                story.name, e,
            )
            existing = None

        if existing:
            skin_id = existing
            # Auto-refresh: push current CSS + description to keep SQW in sync
            # with the local Work_Skin.css. Best-effort — if the edit fails,
            # log and continue with the existing skin.
            try:
                await client.edit_work_skin(
                    skin_id,
                    title=skin_title,
                    description=skin_description,
                    css=css,
                )
                logger.info(
                    "SqW: Refreshed Work Skin %s (%s) with current CSS",
                    skin_id, skin_title,
                )
            except Exception as e:
                logger.warning(
                    "SqW: Could not refresh Work Skin %s for %s: %s "
                    "(using existing CSS on SQW)",
                    skin_id, story.name, e,
                )
            return skin_id

        # No existing skin — create new
        try:
            result = await client.create_work_skin(
                title=skin_title,
                css=css,
                description=skin_description,
            )
            return result["skin_id"]
        except Exception as e:
            logger.warning("SqW: Work skin creation failed for %s: %s", story.name, e)
            return ""

    def _build_freeform_tags(
        self,
        story: story_reader.StoryInfo,
        package: StoryUploadPackage,
    ) -> list[str]:
        """Get the freeform tag list to use, prefer package > story > sqw-specific."""
        if package.tags:
            return list(package.tags)
        sqw_tags = story.tags_by_platform.get("sqw")
        if sqw_tags:
            return list(sqw_tags)
        return list(story.tags_by_platform.get("default", []))

    # ─── Posting / Editing ────────────────────────────────────────────

    async def post(self, package: StoryUploadPackage) -> PostResult:
        """Create a new work on SquidgeWorld with all metadata and chapters.

        Loads full StoryInfo from story.json, finds or creates the Work Skin,
        creates the work with chapter 1, then iterates remaining chapters
        adding each as a new chapter (publish=False to keep draft state).

        SAFETY: After creation and after every chapter add, verifies the work
        is still in /users/<user>/works/drafts. If the work ever moves to
        the published listing, the work is DELETED and the call fails.
        Set `package.extra["allow_publish"] = True` to opt out of the
        post-flight checks (e.g. for re-publishing already-live works).
        """
        _t = self._start_timer()
        allow_publish = bool(package.extra.get("allow_publish", False))

        async def _verify_still_draft(client_inst, work_id_inner: str, step: str) -> None:
            if allow_publish:
                return
            # Small delay to let SQW catch up
            await asyncio.sleep(1)
            in_drafts = await client_inst.is_work_in_drafts(work_id_inner)
            in_published = await client_inst.is_work_published(work_id_inner)
            if in_published or not in_drafts:
                # Try to delete and raise loudly
                try:
                    await client_inst.delete_work(work_id_inner)
                    msg = (
                        f"SqW: SAFETY ABORT after {step} — work {work_id_inner} "
                        f"left draft state (in_drafts={in_drafts}, in_published={in_published}). "
                        f"Work has been DELETED."
                    )
                except Exception as del_err:
                    msg = (
                        f"SqW: SAFETY ABORT after {step} — work {work_id_inner} "
                        f"left draft state and DELETE FAILED: {del_err}. "
                        f"MANUAL DELETE: https://squidgeworld.org/works/{work_id_inner}/confirm_delete"
                    )
                raise RuntimeError(msg)

        try:
            client = await self._ensure_client()
            story = story_reader.load_story(package.story_name)

            # 1. Work Skin
            skin_id = await self._ensure_work_skin(client, story)

            # 2. Build metadata
            rating = self._rating_to_sqw(story.rating or package.rating)
            categories = story.categories or ([story.category] if story.category else [])
            warnings = story.warnings or ["No Archive Warnings Apply"]
            characters_str = ", ".join(story.characters)
            relationships_str = ", ".join(story.relationships)
            fandom = story.fandom or "Original Work"

            # 3. Trim freeform tags to fit SQW's 75-tag limit
            freeform_tags = self._build_freeform_tags(story, package)
            freeform_tags = self._trim_freeform_tags(
                freeform_tags, story.characters, story.relationships, fandom,
            )
            additional_tags = ", ".join(freeform_tags)

            # 4. Get chapter 1 content
            chapter_1_content = self._read_chapter_content(story, 1)
            if not chapter_1_content:
                # If the package supplied a file_path use it
                if package.file_path:
                    with open(package.file_path, "r", encoding="utf-8") as f:
                        chapter_1_content = f.read()
                else:
                    return PostResult(
                        success=False,
                        error=f"No SQW content for {story.name} chapter 1",
                        duration_seconds=self._elapsed(_t),
                    )

            # 5. Create the work (chapter 1 only at this point)
            ch1 = next((c for c in story.chapters if c.index == 1), None)
            chapter_1_title = _strip_chapter_prefix(ch1.title if ch1 else "")
            work_title = package.title or story.name.replace("_", " ")
            summary = (story.description or package.description or "")[:1250]

            create_result = await client.create_work(
                title=work_title,
                content=chapter_1_content,
                fandom=fandom,
                rating=rating,
                warnings=warnings,
                categories=categories,
                relationship=relationships_str,
                characters=characters_str,
                additional_tags=additional_tags,
                summary=summary,
                language_id="15",
                chapter_title=chapter_1_title,
                work_skin_id=skin_id,
            )
            work_id = create_result["work_id"]
            logger.info("SqW: Created work %s for %s", work_id, story.name)

            # SAFETY: verify the new work is in drafts
            await _verify_still_draft(client, work_id, "create_work")

            # 6. Add remaining chapters as drafts (publish=False)
            remaining_chapters = [c for c in story.chapters if c.index > 1]
            for ch in sorted(remaining_chapters, key=lambda c: c.index):
                ch_content = self._read_chapter_content(story, ch.index)
                if not ch_content:
                    logger.warning(
                        "SqW: Skipping chapter %d for %s (no content found)",
                        ch.index, story.name,
                    )
                    continue
                await client.create_chapter(
                    work_id,
                    title=_strip_chapter_prefix(ch.title),
                    content=ch_content,
                    position=ch.index,
                    publish=False,  # SAFETY: keeps work in draft state
                )
                logger.info(
                    "SqW: Added chapter %d to %s (work_id=%s)",
                    ch.index, story.name, work_id,
                )
                # SAFETY: verify still draft after each chapter
                await _verify_still_draft(client, work_id, f"create_chapter ch{ch.index}")

            return PostResult(
                success=True,
                external_id=work_id,
                external_url=create_result.get("url", f"https://squidgeworld.org/works/{work_id}"),
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("SqW post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        """Edit an existing SquidgeWorld work — metadata and all chapters.

        Loads StoryInfo, refreshes the Work Skin, edits the work metadata
        (full safe form-fetch pattern), then iterates SQW's chapter list
        and updates each chapter's content from the corresponding archive
        file via the safe edit_chapter pattern.

        SAFETY: Auto-detects whether the work is currently a draft or
        published and uses the correct submit button so a draft is NEVER
        accidentally published and a published work is NEVER accidentally
        unpublished. Verifies the state is unchanged after the edit.
        Set `package.extra["allow_state_change"] = True` to opt out.
        """
        _t = self._start_timer()
        allow_state_change = bool(package.extra.get("allow_state_change", False))

        try:
            client = await self._ensure_client()
            story = story_reader.load_story(package.story_name)

            # Detect current state so we use the right submit button
            was_draft = await client.is_work_in_drafts(external_id)
            was_published = await client.is_work_published(external_id)
            if not (was_draft or was_published):
                return PostResult(
                    success=False,
                    error=f"Work {external_id} is neither draft nor published — aborting edit",
                    duration_seconds=self._elapsed(_t),
                )
            save_as_draft = was_draft  # preserve the current state

            # 1. Refresh Work Skin
            skin_id = await self._ensure_work_skin(client, story)

            # 2. Build metadata
            rating = self._rating_to_sqw(story.rating or package.rating)
            categories = story.categories or ([story.category] if story.category else [])
            warnings = story.warnings or None  # None = keep existing on edit
            characters_str = ", ".join(story.characters) if story.characters else None
            relationships_str = ", ".join(story.relationships) if story.relationships else None
            fandom = story.fandom or None

            # 3. Trim freeform tags
            freeform_tags = self._build_freeform_tags(story, package)
            freeform_tags = self._trim_freeform_tags(
                freeform_tags, story.characters, story.relationships, story.fandom or "Original Work",
            )
            additional_tags = ", ".join(freeform_tags)

            # 4. Edit work metadata
            await client.edit_work(
                external_id,
                title=package.title or story.name.replace("_", " "),
                summary=(story.description or package.description or "")[:1250],
                additional_tags=additional_tags,
                fandom=fandom,
                rating=rating,
                warnings=warnings,
                categories=categories or None,
                characters=characters_str,
                relationship=relationships_str,
                work_skin_id=skin_id or None,
                save_as_draft=save_as_draft,  # preserve the current state
            )
            logger.info(
                "SqW: Edited work %s metadata (save_as_draft=%s)",
                external_id, save_as_draft,
            )

            # SAFETY: Verify state didn't change unexpectedly
            if not allow_state_change:
                await asyncio.sleep(1)
                now_draft = await client.is_work_in_drafts(external_id)
                now_published = await client.is_work_published(external_id)
                if was_draft and now_published:
                    raise RuntimeError(
                        f"SqW: SAFETY ABORT — edit on work {external_id} "
                        f"published a draft! State flipped draft→published."
                    )
                if was_published and not now_published:
                    raise RuntimeError(
                        f"SqW: SAFETY ABORT — edit on work {external_id} "
                        f"unpublished a published work! State flipped published→draft."
                    )

            # Metadata-only mode: skip chapter BODY uploads, but still push
            # per-chapter titles since those are metadata. edit_chapter with
            # content=None preserves the existing body on SqW.
            if package.extra.get("skip_content_refresh"):
                if story.chapters and story.total_chapters > 1:
                    try:
                        sqw_chapters = await client.get_chapter_ids(external_id)
                        local_chapters = sorted(story.chapters, key=lambda c: c.index)
                        for sqw_ch, local_ch in zip(sqw_chapters, local_chapters):
                            new_title = _strip_chapter_prefix(local_ch.title)
                            if new_title and new_title != sqw_ch.get("title", ""):
                                await client.edit_chapter(
                                    external_id, sqw_ch["chapter_id"],
                                    title=new_title,  # content=None preserves body
                                )
                                logger.info(
                                    "SqW: Retitled chapter %s -> %r (metadata-only)",
                                    sqw_ch["chapter_id"], new_title,
                                )
                    except Exception as ch_err:
                        logger.warning(
                            "SqW: Chapter title refresh failed: %s", ch_err,
                        )
                logger.info(
                    "SqW: Metadata-only edit complete for %s (body content preserved)",
                    external_id,
                )
                return PostResult(
                    success=True,
                    external_id=external_id,
                    external_url=f"https://squidgeworld.org/works/{external_id}",
                    duration_seconds=self._elapsed(_t),
                )

            # 5. Update each chapter's content
            try:
                sqw_chapters = await client.get_chapter_ids(external_id)
            except Exception as e:
                logger.warning("SqW: Could not list chapters for %s: %s", external_id, e)
                sqw_chapters = []

            updated = 0
            for sqw_ch in sqw_chapters:
                ch_idx = sqw_ch.get("index", 0)
                ch_id = sqw_ch.get("chapter_id", "")
                if not ch_id:
                    continue
                content = self._read_chapter_content(story, ch_idx)
                if not content:
                    logger.warning(
                        "SqW: No archive file for chapter %d of %s",
                        ch_idx, story.name,
                    )
                    continue
                try:
                    await client.edit_chapter(external_id, ch_id, content=content)
                    updated += 1
                except Exception as ch_err:
                    logger.error(
                        "SqW: Chapter %d edit failed for %s: %s",
                        ch_idx, story.name, ch_err,
                    )

            logger.info("SqW: Updated %d/%d chapters of %s", updated, len(sqw_chapters), external_id)

            return PostResult(
                success=True,
                external_id=external_id,
                external_url=f"https://squidgeworld.org/works/{external_id}",
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("SqW edit failed for %s: %s", external_id, e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    async def probe_exists(self, external_id: str) -> bool | None:
        """Check whether a SqW work still exists.

        Uses the authenticated client so drafts are visible. Probes the
        work's edit page — 404 means deleted, 200/3xx means live. Returns
        None on transient errors so we don't falsely mark live works as
        deleted.
        """
        try:
            client = await self._ensure_client()
            if not client._logged_in:
                if not await client.ensure_logged_in():
                    return None
            resp = await client._http.get(
                f"https://squidgeworld.org/works/{external_id}/edit",
                follow_redirects=False,
            )
            if resp.status_code == 404:
                return False
            if 200 <= resp.status_code < 400:
                return True
            return None
        except Exception as e:
            logger.warning("SqW probe_exists(%s) failed: %s", external_id, e)
            return None

    async def probe_draft_state(self, external_id: str) -> bool | None:
        """True if the SqW work was Posted Without Preview (draft).

        SqW runs the same OTW Archive codebase as AO3, so the draft
        surface is identical: the preview page renders a "Post" button
        on drafts and live works lack it. See
        ``AO3Poster.probe_draft_state`` for the heuristics.
        """
        try:
            client = await self._ensure_client()
            if not client._logged_in:
                if not await client.ensure_logged_in():
                    return None
            resp = await client._http.get(
                f"https://squidgeworld.org/works/{external_id}/preview",
                follow_redirects=True,
            )
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                return None
            html = resp.text
            if 'name="post_button"' in html or ('value="Post"' in html and 'name="preview_button"' in html):
                return True
            if 'id="kudos"' in html or 'id="comments"' in html:
                return False
            return None
        except Exception as e:
            logger.warning("SqW probe_draft_state(%s) failed: %s", external_id, e)
            return None

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        """Replace the first chapter's content from a single file.

        For multi-chapter updates, use edit() instead — it iterates all chapters.
        """
        _t = self._start_timer()
        try:
            client = await self._ensure_client()
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            chapters = await client.get_chapter_ids(external_id)
            if not chapters:
                return PostResult(
                    success=False, error="No chapters found for this work",
                    duration_seconds=self._elapsed(_t),
                )
            ch = chapters[0]
            await client.edit_chapter(external_id, ch["chapter_id"], content=content)
            return PostResult(
                success=True,
                external_id=external_id,
                external_url=f"https://squidgeworld.org/works/{external_id}",
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("SqW file replace failed for %s: %s", external_id, e)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
