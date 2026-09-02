"""Archive of Our Own (AO3) platform poster.

Wraps the AO3Client to upload, edit, and manage works on AO3.

Pulls full metadata from story.json via posting.story_reader, including
fandom, category, characters, relationships, warnings.

Posting flow (single-bulk-file convention, mirrors the IB convention):
  1. Read StoryInfo from the archive (story.json)
  2. Trim freeform tags to fit OTW's 75-tag total limit
     (fandom + relationship + character + freeform <= 75)
  3. Read full-story body-only HTML (HTML/<story>_SoFurry.html)
  4. create_work via preview_button — work lands in /works/{id}/preview
     (AO3's draft equivalent), NOT published
  5. SAFETY: verify the new work is in /users/{user}/works/drafts.
     If it ever lands in published, the work is DELETED and the call fails.

Edit flow:
  1. Read StoryInfo
  2. Detect current state (draft vs published)
  3. Edit metadata via edit_work
  4. Iterate AO3's chapter list and update each chapter's content via
     edit_chapter

Rating mapping:
  explicit -> "Explicit"
  mature   -> "Mature"
  teen     -> "Teen And Up Audiences"
  general  -> "General Audiences"

Notes for the AO3-vs-SQW differences:
  - AO3 client does NOT yet support multi-chapter create_chapter or Work
    Skins. For chaptered prose we use the IB-style single bulk file
    (HTML/<story>_SoFurry.html) which contains all chapters as <p> elements
    in one big body.
  - AO3 has no "preview" → "publish" automated flow here. Work stays in
    preview/draft until you manually click Post on AO3.
"""

from __future__ import annotations

import asyncio
import logging
import re

import config
from database.db import get_connection
from database import posting_queries
from posting import story_reader
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage
from clients.ao3.client import AO3Client

logger = logging.getLogger(__name__)


# OTW Archive total-tag limit (fandom + relationship + character + freeform).
# Rating, warnings, categories do NOT count toward this.
OTW_TAG_LIMIT = 75


# Leading "Chapter N:", "Part N:", "Prelude:", "Epilogue:" patterns that
# our story.json titles carry but that OTW Archive (AO3/SQW) auto-prefixes
# on display — stripping avoids rendering like "Chapter 1: Chapter 1: Title".
_CHAPTER_PREFIX_RE = re.compile(
    r"^(?:Chapter|Part|Prelude|Epilogue)\s*\d*\s*[:\-—–]\s*",
    re.IGNORECASE,
)


def _strip_chapter_prefix(title: str) -> str:
    if not title:
        return title
    stripped = _CHAPTER_PREFIX_RE.sub("", title).strip()
    return stripped or title  # if the title was JUST the prefix, keep original


class AO3Poster(PlatformPoster):

    platform_id = "ao3"
    # Explicit, because the docs make AO3 *look* desktop-only and it isn't.
    # What is desktop-only is bulk IMPORTING (AO3 throttles shared datacenter
    # IPs, so a sweep from the VM stalls). Posting is a handful of requests and
    # goes direct from either side; when AO3 raises Cloudflare "Shields up" the
    # answer is the opt-in CF Worker fallback (`ao3_use_cf_proxy`, AO3 is in
    # PROXY_OPTIONAL_PLATFORMS), not a desktop lock. Setting "desktop" here
    # would strand every AO3 job whenever the desktop is closed.
    requires_mode = "any"
    platform_name = "AO3"
    supports_edit = True
    supports_file_replace = True
    min_post_interval = 5
    max_file_size = 0  # Content pasted as HTML, no file upload
    accepted_file_types = ["html"]

    def __init__(self):
        self._client: AO3Client | None = None

    async def _ensure_client(self) -> AO3Client:
        if self._client and self._client._logged_in:
            return self._client

        settings = config.get_settings()
        creds = self._resolve_creds("ao3", settings)
        username = creds.get("ao3_username", "")
        password = creds.get("ao3_password", "")
        target_user = creds.get("ao3_target_user", "") or username
        session_cookie = creds.get("ao3_session_cookie", "")
        if not session_cookie and (not username or not password):
            raise RuntimeError("AO3 credentials not configured")

        # 2.22.11: route through the cf_proxy module's proxy_kwargs() so
        # the per-platform classification (PROXY_REQUIRED_PLATFORMS vs
        # PROXY_OPTIONAL_PLATFORMS) is honoured. Previously this hardcoded
        # the proxy whenever cf_worker_url was set in settings, regardless
        # of category — which kept AO3 routing through the shared CF
        # Worker egress IP pool even after AO3 was reclassified.
        from polling.cf_proxy import proxy_kwargs
        self._client = AO3Client(
            username, password, target_user,
            session_cookie=session_cookie,
            **proxy_kwargs(settings, "ao3"),
        )
        if not await self._client.ensure_logged_in():
            raise RuntimeError("AO3 login failed")
        return self._client

    # ─── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _rating_to_ao3(rating: str) -> str:
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
        """Trim freeform tags so the total fits OTW's 75-tag limit."""
        used = 0
        if fandom:
            used += 1
        used += len(characters)
        used += len(relationships)
        budget = OTW_TAG_LIMIT - used
        if budget <= 0:
            return []
        if len(tags) <= budget:
            return tags
        return tags[:budget]

    async def _ensure_work_skin(
        self,
        client: AO3Client,
        story: story_reader.StoryInfo,
    ) -> str:
        """Find or create the Work Skin for this story and sync the CSS.

        Ports the SquidgeWorld poster's behaviour — both platforms run the
        OTW Archive software, so the endpoints and skin conventions are
        identical. Returns the skin_id, or '' if the story doesn't have a
        Work_Skin.css file (in which case no skin gets applied).

        Behavior:
          1. No Work_Skin.css → return '' (no skin applied)
          2. Skin doesn't exist by title → create new with current CSS
          3. Skin exists → push current CSS + description via edit_work_skin
             so local edits propagate on every post/edit. Best-effort —
             if the refresh fails, log and return the existing skin_id.
        """
        if not story.work_skin_path or not story.work_skin_path.is_file():
            return ""

        # Strip leading underscores that story folder names sometimes have
        # (e.g. "_Test_Story" → "Test Story Skin", not "_Test Story Skin").
        display_name = story.name.lstrip("_").replace("_", " ")
        skin_title = f"{display_name} Skin"
        skin_description = (
            f"Custom Work Skin for '{display_name}' by {story.author}."
        )

        try:
            css = story.work_skin_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(
                "AO3: Could not read Work_Skin.css for %s: %s", story.name, e,
            )
            return ""

        try:
            existing = await client.find_work_skin_by_title(skin_title)
        except Exception as e:
            logger.warning(
                "AO3: Could not search for existing Work Skin for %s: %s",
                story.name, e,
            )
            existing = None

        if existing:
            skin_id = existing
            try:
                await client.edit_work_skin(
                    skin_id,
                    title=skin_title,
                    description=skin_description,
                    css=css,
                )
                logger.info(
                    "AO3: Refreshed Work Skin %s (%s) with current CSS",
                    skin_id, skin_title,
                )
            except Exception as e:
                logger.warning(
                    "AO3: Could not refresh Work Skin %s for %s: %s "
                    "(using existing CSS on AO3)",
                    skin_id, story.name, e,
                )
            return skin_id

        try:
            result = await client.create_work_skin(
                title=skin_title, css=css, description=skin_description,
            )
            return result["skin_id"]
        except Exception as e:
            logger.warning("AO3: Work skin creation failed for %s: %s", story.name, e)
            return ""

    async def probe_exists(self, external_id: str) -> bool | None:
        """Check whether an AO3 work still exists.

        Uses the authenticated client so drafts are visible. Hits the
        work's edit page — AO3 returns 404 for deleted works and 200 (or
        a redirect to the work) for live ones. Returns None on transient
        errors (Cloudflare hiccups, timeouts) so we don't falsely mark
        still-alive works as deleted.
        """
        try:
            client = await self._ensure_client()
            if not client._logged_in:
                if not await client.ensure_logged_in():
                    return None
            resp = await client._http.get(
                f"https://archiveofourown.org/works/{external_id}/edit",
                follow_redirects=False,
            )
            if resp.status_code == 404:
                return False
            if 200 <= resp.status_code < 400:
                return True
            return None
        except Exception as e:
            logger.warning("AO3 probe_exists(%s) failed: %s", external_id, e)
            return None

    async def probe_draft_state(self, external_id: str) -> bool | None:
        """True if the AO3 work was Posted Without Preview (draft).

        AO3's draft state is exposed via the work edit page: a draft
        work redirects to ``/works/{id}/preview`` rather than rendering
        the work body directly, and the public ``/works/{id}`` URL
        returns 302 to ``/users/login`` for non-logged-in viewers
        because drafts are owner-only. Authenticated edit-page response
        contains either the live work form or the preview banner.
        Detects drafts by looking for the unmistakable
        ``id="post_without_preview_notice"`` div / "Post" submit button
        on the preview page.
        """
        try:
            client = await self._ensure_client()
            if not client._logged_in:
                if not await client.ensure_logged_in():
                    return None
            # The preview page is the canonical draft surface — live
            # works return their normal show page even when this URL is
            # requested by the owner.
            resp = await client._http.get(
                f"https://archiveofourown.org/works/{external_id}/preview",
                follow_redirects=True,
            )
            if resp.status_code == 404:
                return None  # delegated to probe_exists
            if resp.status_code != 200:
                return None
            html = resp.text
            # AO3 shows a "Post Without Preview" / "Post Draft" form on
            # works that are still drafts. Live works redirect or render
            # the show page without those buttons.
            if 'name="post_button"' in html or 'value="Post"' in html and 'name="preview_button"' in html:
                return True
            # Fallback heuristic: live show page has the chapter index
            # / kudos / comments controls; drafts don't.
            if 'id="kudos"' in html or 'id="comments"' in html:
                return False
            return None
        except Exception as e:
            logger.warning("AO3 probe_draft_state(%s) failed: %s", external_id, e)
            return None

    @staticmethod
    def _read_chapter_content(story: story_reader.StoryInfo, ch_idx: int) -> str | None:
        """Resolve a single chapter's body HTML for AO3.

        Uses the SquidgeWorld/ per-chapter files (same OTW Archive format),
        falling back to Chapters/SoFurry_HTML/ as a last resort.
        """
        ch = next((c for c in story.chapters if c.index == ch_idx), None)
        if not ch:
            return None

        sqw_dir = story.path / "SquidgeWorld"
        if sqw_dir.is_dir():
            base = ch.filename.replace(".md", "") if ch.filename else f"Chapter_{ch_idx}"
            # Prefer an EXACT match to avoid picking up stale debris files
            # like "Chapter_1_The_Counter_testing_testing_1_2_3.html".
            exact = sqw_dir / f"{base}.html"
            if exact.is_file():
                try:
                    return exact.read_text(encoding="utf-8")
                except Exception:
                    pass
            # Fallback: shortest matching filename so variants with noise
            # suffixes lose to the clean version.
            candidates = sorted(
                (c for c in sqw_dir.glob(f"{base}*") if c.suffix == ".html"),
                key=lambda c: len(c.name),
            )
            for candidate in candidates:
                try:
                    return candidate.read_text(encoding="utf-8")
                except Exception:
                    pass
            # Last resort: match by chapter index prefix
            for candidate in sorted(
                sqw_dir.glob(f"Chapter_{ch_idx}_*.html"),
                key=lambda c: len(c.name),
            ):
                try:
                    return candidate.read_text(encoding="utf-8")
                except Exception:
                    pass

        sf_html = story.path / "Chapters" / "SoFurry_HTML"
        if sf_html.is_dir():
            for candidate in sorted(sf_html.glob(f"Chapter_{ch_idx}_*.html")):
                try:
                    return candidate.read_text(encoding="utf-8")
                except Exception:
                    pass

        return None

    @staticmethod
    def _read_full_story_html(story: story_reader.StoryInfo) -> str | None:
        """Read the body-only full-story HTML for AO3.

        AO3 is an OTW Archive site, same family as SquidgeWorld, so we
        use the SquidgeWorld per-chapter HTML as the source of truth.
        That format already has the OTW-style chapter markers, the
        correct warning-icon glyph, and the same semantic anchor
        processing the OTW Rails app understands. Concatenating those
        body fragments yields a complete OTW-compatible body.

        Order of preference:
          1. Concatenate SquidgeWorld/Chapter_*.html — same source SqW
             posts; the right shape for an OTW Archive.
          2. HTML/<Story>_Clean.html — last-resort fallback for stories
             that pre-date the SquidgeWorld output. Worth keeping so
             older archives without per-chapter SqW files still post,
             but anything regenerated since 2.18.x will have the SqW
             files and that path will fire instead.
        """
        # 1. Prefer SquidgeWorld concatenation (matches SqW poster's source)
        sqw_dir = story.path / "SquidgeWorld"
        if sqw_dir.is_dir():
            chapters: list[str] = []
            for ch in sorted(story.chapters, key=lambda c: c.index):
                matches = sorted(sqw_dir.glob(f"Chapter_{ch.index}_*.html"))
                if not matches:
                    continue
                try:
                    body = matches[0].read_text(encoding="utf-8")
                    chapters.append(body)
                except Exception:
                    pass
            if chapters:
                return "\n<hr />\n".join(chapters)

        # 2. Legacy fallback: bulk Clean HTML for pre-SqW archives.
        html_dir = story.path / "HTML"
        if html_dir.is_dir():
            for f in sorted(html_dir.glob("*_SoFurry.html")):
                try:
                    return f.read_text(encoding="utf-8")
                except Exception:
                    pass

        return None

    def _build_freeform_tags(
        self,
        story: story_reader.StoryInfo,
        package: StoryUploadPackage,
    ) -> list[str]:
        """Choose the freeform tag list — prefer package.tags, then story tags."""
        if package.tags:
            return list(package.tags)
        ao3_tags = story.tags_by_platform.get("ao3")
        if ao3_tags:
            return list(ao3_tags)
        sqw_tags = story.tags_by_platform.get("sqw")
        if sqw_tags:
            return list(sqw_tags)
        return list(story.tags_by_platform.get("default", []))

    # ─── Posting / Editing ────────────────────────────────────────────

    async def post(self, package: StoryUploadPackage) -> PostResult:
        """Create a new work on AO3 with full metadata, single bulk content.

        SAFETY: AO3's create_work uses preview_button so the work lands in
        /works/{id}/preview (drafts) by default. After creation, this method
        verifies the work is in the user's drafts listing. If it has been
        published unexpectedly, the work is DELETED and the call fails.
        Set ``package.extra["allow_publish"] = True`` to skip the safety
        check (e.g. for re-publishing already-live works).
        """
        _t = self._start_timer()
        allow_publish = bool(package.extra.get("allow_publish", False))
        # User's "live" toggle from dashboard. package.extra["draft"] is True
        # if user wants draft, False if user wants live. Default to draft for
        # safety when the flag is absent (older clients / direct API callers).
        publish_live = not bool(package.extra.get("draft", True))

        async def _verify_still_draft(client_inst, work_id_inner: str, step: str) -> None:
            """Best-effort verification that the work is in draft state.

            create_work uses preview_button which AO3 guarantees creates a
            preview/draft. The check below is a defensive net for the
            impossible case of accidental publish — it ONLY aborts on
            POSITIVE confirmation the work is published.

            States from is_work_published:
              True  -> definitely published    -> abort + delete
              False -> definitely not          -> safe, return
              None  -> fetch failed (timeout)  -> warn but trust preview_button
            """
            # Skip safety check if either:
            #  - allow_publish: re-publishing an already-live work (no need to verify draft)
            #  - publish_live: user explicitly chose to publish live — work SHOULD be live
            if allow_publish or publish_live:
                return
            await asyncio.sleep(2)  # let AO3 catch up
            in_published = await client_inst.is_work_published(work_id_inner)
            if in_published is True:
                # Confirmed published — try to delete
                try:
                    await client_inst.delete_work(work_id_inner)
                    msg = (
                        f"AO3: SAFETY ABORT after {step} — work {work_id_inner} "
                        f"is published. Work has been DELETED."
                    )
                except Exception as del_err:
                    msg = (
                        f"AO3: SAFETY ABORT after {step} — work {work_id_inner} "
                        f"is published and DELETE FAILED: {del_err}. "
                        f"MANUAL DELETE: https://archiveofourown.org/works/{work_id_inner}/confirm_delete"
                    )
                raise RuntimeError(msg)
            elif in_published is None:
                # Fetch failed — trust preview_button, log a warning
                logger.warning(
                    "AO3: post-flight is_work_published check failed for %s after %s. "
                    "create_work used preview_button so the work is in draft state by "
                    "construction; trusting that. Verify manually if concerned.",
                    work_id_inner, step,
                )
            # in_published is False -> definitely not published, safe

        # Resume detection: if a previous post() for this story left a
        # publication row with external_id set and status != "posted", a
        # work was created on AO3 but the chapter loop didn't finish.
        # We resume into that work instead of creating a duplicate.
        existing_work_id = ""
        existing_work_url = ""
        try:
            conn = get_connection()
            try:
                pubs = posting_queries.get_publications(
                    conn, story_name=package.story_name, platform="ao3",
                )
            finally:
                conn.close()
            for p in pubs:
                if p.get("chapter_index") == 0 and p.get("external_id") and p.get("status") != "posted":
                    existing_work_id = p["external_id"]
                    existing_work_url = p.get("external_url") or ""
                    break
        except Exception as _resume_lookup_err:
            logger.warning(
                "AO3: Resume lookup failed for %s: %s — continuing as fresh post",
                package.story_name, _resume_lookup_err,
            )

        def _checkpoint(work_id_inner: str, url_inner: str, status_inner: str) -> None:
            """Persist work_id + status to publications so a later retry can resume."""
            try:
                conn = get_connection()
                try:
                    posting_queries.upsert_publication(
                        conn,
                        package.story_name,
                        0,  # AO3 always uses ch_idx=0 (full-work record)
                        "ao3",
                        external_id=work_id_inner,
                        external_url=url_inner,
                        status=status_inner,
                    )
                finally:
                    conn.close()
            except Exception as ck_err:
                logger.warning(
                    "AO3: Checkpoint write failed for work %s (status=%s): %s",
                    work_id_inner, status_inner, ck_err,
                )

        try:
            client = await self._ensure_client()
            story = story_reader.load_story(package.story_name)

            # 1. Build metadata
            rating = self._rating_to_ao3(story.rating or package.rating)
            categories = story.categories or ([story.category] if story.category else [])
            warnings = story.warnings or ["No Archive Warnings Apply"]
            characters_str = ", ".join(story.characters)
            relationships_str = ", ".join(story.relationships)
            fandom = story.fandom or "Original Work"

            # 2. Trim freeform tags to fit OTW's 75-tag limit
            freeform_tags = self._build_freeform_tags(story, package)
            freeform_tags = self._trim_freeform_tags(
                freeform_tags, story.characters, story.relationships, fandom,
            )
            additional_tags = ", ".join(freeform_tags)

            # 3. Decide content strategy.
            # Multi-chapter story → create work with Ch1, then create_chapter
            # for Ch2..N (mirrors the SQW flow so AO3 works match the source
            # chapter structure).
            # Single-chapter story / no chapter_info → use the full-story
            # Clean HTML as one chapter.
            has_chapters = bool(story.chapters) and story.total_chapters > 1

            if has_chapters:
                chapter_1 = next((c for c in story.chapters if c.index == 1), None)
                chapter_1_title = _strip_chapter_prefix(
                    chapter_1.title if chapter_1 else ""
                )
                content = self._read_chapter_content(story, 1)
                if not content:
                    return PostResult(
                        success=False,
                        error=f"No AO3 content for {story.name} chapter 1",
                        duration_seconds=self._elapsed(_t),
                    )
            else:
                chapter_1_title = ""
                content = self._read_full_story_html(story)
                if not content and package.file_path:
                    with open(package.file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                if not content:
                    return PostResult(
                        success=False,
                        error=f"No AO3 content for {story.name} (no HTML/<story>_SoFurry.html and no fallback)",
                        duration_seconds=self._elapsed(_t),
                    )

            # 4. Title + summary
            work_title = package.title or story.name.replace("_", " ")
            summary = (story.description or package.description or "")[:1250]

            # 4b. Ensure the Work Skin exists and is up-to-date (no-op if
            # the story has no Work_Skin.css). Done BEFORE create_work so
            # the skin_id can be passed through.
            skin_id = await self._ensure_work_skin(client, story)

            # 5. Create or resume the work.
            # If a prior post() left a checkpointed work_id behind, verify
            # the work still exists, then resume into it. If the user
            # manually deleted the orphaned draft, fall back to a fresh
            # create_work.
            already_created_chapter_indices: set[int] = set()
            if existing_work_id:
                still_exists = await self.probe_exists(existing_work_id)
                if still_exists is False:
                    logger.info(
                        "AO3: Checkpointed work %s for %s no longer exists "
                        "(likely user-deleted) — falling back to fresh post",
                        existing_work_id, story.name,
                    )
                    existing_work_id = ""
                    existing_work_url = ""
                # still_exists is None (probe failed) → trust the checkpoint
                # and try anyway; a 404 in the chapter form will surface clearly

            if existing_work_id:
                work_id = existing_work_id
                url = existing_work_url or f"https://archiveofourown.org/works/{work_id}"
                logger.info(
                    "AO3: Resuming into existing work %s for %s — skipping create_work",
                    work_id, story.name,
                )
                # The resume branch skips create_work, which is normally where
                # work_skin_id gets attached to the work. If the original
                # create_work ran before the Work Skin existed (the case for
                # work 84822261 — fix shipped 2.22.12), the skin CSS is on
                # AO3 but isn't applied to the work itself. Push edit_work
                # to attach it now. Idempotent: re-submitting the same
                # skin_id is a no-op on AO3's side.
                if skin_id:
                    try:
                        await client.edit_work(work_id, work_skin_id=skin_id)
                        logger.info(
                            "AO3: Attached Work Skin %s to resumed work %s",
                            skin_id, work_id,
                        )
                    except Exception as skin_attach_err:
                        logger.warning(
                            "AO3: Could not attach Work Skin %s to work %s on "
                            "resume: %s (continuing — skin can be attached "
                            "manually via Update)",
                            skin_id, work_id, skin_attach_err,
                        )
                try:
                    ao3_chapters = await client.get_chapter_ids(work_id)
                    already_created_chapter_indices = {
                        c.get("index") for c in ao3_chapters if c.get("index")
                    }
                    logger.info(
                        "AO3: Work %s has %d existing chapters: %s",
                        work_id, len(already_created_chapter_indices),
                        sorted(already_created_chapter_indices),
                    )
                except Exception as nav_err:
                    logger.warning(
                        "AO3: Could not list chapters on work %s during resume: %s — "
                        "will attempt to add all and rely on AO3's idempotency",
                        work_id, nav_err,
                    )
            else:
                create_kwargs: dict = dict(
                    title=work_title,
                    content=content,
                    fandom=fandom,
                    rating=rating,
                    warnings=warnings,
                    categories=categories,
                    relationship=relationships_str,
                    characters=characters_str,
                    additional_tags=additional_tags,
                    summary=summary,
                    language_id="1",  # AO3 English (numeric, like SQW's "15")
                    work_skin_id=skin_id,
                )
                if chapter_1_title:
                    create_kwargs["chapter_title"] = chapter_1_title
                # Single-chapter stories: publish the work directly when user
                # selected live. For multi-chapter, keep as draft here and flip
                # to live on the LAST chapter (mirrors AO3's "Post Without
                # Preview" semantics, which publishes the whole work).
                if publish_live and not has_chapters:
                    create_kwargs["publish"] = True
                create_result = await client.create_work(**create_kwargs)
                work_id = create_result["work_id"]
                url = create_result.get("url", f"https://archiveofourown.org/works/{work_id}")
                state_str = "published" if create_result.get("published") else "preview/draft"
                logger.info("AO3: Created work %s (%s) for %s — %s",
                            work_id, state_str, story.name, url)
                # Checkpoint: persist work_id so any subsequent chapter
                # failure can resume rather than create a duplicate work.
                # Status="partial" for multi-chapter (more chapters to add),
                # "posted" not used here — manager.post_story flips to
                # "posted" only on full success.
                checkpoint_status = "partial" if has_chapters else "failed"
                _checkpoint(work_id, url, checkpoint_status)
                already_created_chapter_indices = {1}  # ch1 in via create_work

                # SAFETY: verify the new work is in drafts (skipped when
                # publish_live: user intentionally wanted it live)
                await _verify_still_draft(client, work_id, "create_work")

            # 6. Add remaining chapters (Ch2..N).
            # For multi-chapter posts: all chapters except the LAST are
            # added as drafts. The LAST chapter uses publish=publish_live —
            # AO3's "Post Without Preview" on a chapter publishes the
            # whole work in one shot.
            if has_chapters:
                remaining = sorted(
                    [c for c in story.chapters if c.index > 1],
                    key=lambda c: c.index,
                )
                last_idx = remaining[-1].index if remaining else None
                for ch in remaining:
                    if ch.index in already_created_chapter_indices:
                        logger.info(
                            "AO3: Chapter %d already on work %s — skipping",
                            ch.index, work_id,
                        )
                        continue
                    ch_content = self._read_chapter_content(story, ch.index)
                    if not ch_content:
                        logger.warning(
                            "AO3: Skipping chapter %d for %s (no content found)",
                            ch.index, story.name,
                        )
                        continue
                    is_last = (ch.index == last_idx)
                    publish_this_chapter = publish_live and is_last
                    try:
                        await client.create_chapter(
                            work_id,
                            title=_strip_chapter_prefix(ch.title),
                            content=ch_content,
                            position=ch.index,
                            publish=publish_this_chapter,
                        )
                    except Exception as ch_err:
                        # Checkpoint partial progress before bubbling the
                        # failure up — the retry will pick up from the next
                        # missing chapter rather than recreating the work.
                        _checkpoint(work_id, url, "partial")
                        raise
                    already_created_chapter_indices.add(ch.index)
                    logger.info(
                        "AO3: Added chapter %d to %s (work_id=%s, publish=%s)",
                        ch.index, story.name, work_id, publish_this_chapter,
                    )
                    # SAFETY: verify still draft after each chapter
                    await _verify_still_draft(
                        client, work_id, f"create_chapter ch{ch.index}",
                    )

            # 7. If publishing live on a multi-chapter work, walk every
            # chapter and publish any that are still drafts. AO3 chapters
            # have INDEPENDENT draft state — `post_without_preview_button`
            # on the last chapter only publishes that chapter (and may
            # flip the work to "posted"), it doesn't auto-publish other
            # draft chapters. Without this loop, multi-chapter live posts
            # land with only the final chapter visible.
            # Bug surfaced on a live 5-chapter work: the 2.22.11b success
            # run published ch5 live
            # but chs 2-4 remained drafts. Fixed in 2.22.13.
            if has_chapters and publish_live:
                try:
                    publish_summary = await client.publish_all_draft_chapters(work_id)
                    logger.info(
                        "AO3: post() publish-all-drafts for %s — published=%d, "
                        "already_posted=%d, failed=%d (total %d)",
                        story.name,
                        publish_summary["published"],
                        publish_summary["already_posted"],
                        len(publish_summary["failed"]),
                        publish_summary["total"],
                    )
                except Exception as pub_err:
                    logger.warning(
                        "AO3: publish_all_draft_chapters failed for work %s on %s: %s "
                        "(work_id will remain checkpointed; user can re-trigger via "
                        "dashboard Update to retry)",
                        work_id, story.name, pub_err,
                    )

            return PostResult(
                success=True,
                external_id=work_id,
                external_url=url,
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("AO3 post failed: %s", e, exc_info=True)
            # Return work_id on the failure result so the manager's
            # upsert_publication preserves it for the next retry. Without
            # this, the manager would write external_id="" and resume
            # logic would never trigger.
            failed_work_id = ""
            try:
                failed_work_id = work_id  # noqa: F821 — only set if create_work ran
            except NameError:
                failed_work_id = existing_work_id  # resume failed mid-chapter loop
            failed_url = ""
            if failed_work_id:
                failed_url = f"https://archiveofourown.org/works/{failed_work_id}"
            return PostResult(
                success=False,
                external_id=failed_work_id,
                external_url=failed_url,
                error=str(e),
                duration_seconds=self._elapsed(_t),
            )

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        """Edit an existing AO3 work — metadata + per-chapter content refresh.

        For multi-chapter stories, iterates AO3's chapters in order and
        updates each one's content from the matching SquidgeWorld HTML
        file. Chapters present on AO3 beyond the local story length are
        left alone; chapters present locally but missing on AO3 are
        appended via create_chapter. Chapter count/order should match
        the local source after this call.
        """
        _t = self._start_timer()
        # User's "live" toggle from dashboard (same shape as post()).
        # When True (dashboard "Save as draft" unchecked), edit() walks
        # every chapter at the end and publishes any drafts. Default is
        # False — preserves existing draft/posted state per chapter.
        publish_live = not bool(package.extra.get("draft", True))

        try:
            client = await self._ensure_client()
            story = story_reader.load_story(package.story_name)

            freeform_tags = self._build_freeform_tags(story, package)
            freeform_tags = self._trim_freeform_tags(
                freeform_tags, story.characters, story.relationships,
                story.fandom or "Original Work",
            )
            additional_tags = ", ".join(freeform_tags)

            # Ensure work skin is synced — reuses existing skin by title,
            # pushes the current CSS, then assigns it to the work.
            skin_id = await self._ensure_work_skin(client, story)

            # Resolve AO3-style metadata for the edit (mirrors post()).
            rating = self._rating_to_ao3(story.rating or package.rating)
            warnings_list = story.warnings or ["Choose Not To Use Archive Warnings"]
            categories_list = story.categories or (
                [story.category] if story.category else []
            )
            fandom = story.fandom or "Original Work"
            characters_str = ", ".join(story.characters)
            relationships_str = ", ".join(story.relationships)

            await client.edit_work(
                external_id,
                title=package.title or story.name.replace("_", " "),
                summary=(story.description or package.description or "")[:1250],
                additional_tags=additional_tags,
                warnings=warnings_list,
                categories=categories_list,
                relationship=relationships_str or None,
                characters=characters_str or None,
                fandom=fandom,
                rating=rating,
                work_skin_id=skin_id if skin_id else None,
            )
            logger.info("AO3: Updated work %s metadata", external_id)

            # Metadata-only mode: skip chapter BODY uploads, but still push
            # per-chapter titles since those are metadata. edit_chapter with
            # content=None preserves the existing body on AO3.
            if package.extra.get("skip_content_refresh"):
                if story.chapters and story.total_chapters > 1:
                    try:
                        ao3_chapters = await client.get_chapter_ids(external_id)
                        local_chapters = sorted(story.chapters, key=lambda c: c.index)
                        for ao3_ch, local_ch in zip(ao3_chapters, local_chapters):
                            new_title = _strip_chapter_prefix(local_ch.title)
                            if new_title and new_title != ao3_ch.get("title", ""):
                                await client.edit_chapter(
                                    external_id, ao3_ch["chapter_id"],
                                    title=new_title,  # content=None preserves body
                                    publish=publish_live if publish_live else None,
                                )
                                logger.info(
                                    "AO3: Retitled chapter %s -> %r (metadata-only)",
                                    ao3_ch["chapter_id"], new_title,
                                )
                    except Exception as ch_err:
                        logger.warning(
                            "AO3: Chapter title refresh failed: %s", ch_err,
                        )
                # If publishing live, walk every chapter and post any drafts.
                # See post()'s identical call site for the rationale (AO3
                # chapters have independent draft state).
                if publish_live:
                    try:
                        ps = await client.publish_all_draft_chapters(external_id)
                        logger.info(
                            "AO3: edit(metadata-only) publish-all-drafts for %s — "
                            "published=%d, already_posted=%d, failed=%d (total %d)",
                            story.name, ps["published"], ps["already_posted"],
                            len(ps["failed"]), ps["total"],
                        )
                    except Exception as pub_err:
                        logger.warning(
                            "AO3: publish_all_draft_chapters failed on metadata-only "
                            "edit of work %s: %s",
                            external_id, pub_err,
                        )
                logger.info(
                    "AO3: Metadata-only edit complete for %s (body content preserved)",
                    external_id,
                )
                return PostResult(
                    success=True,
                    external_id=external_id,
                    external_url=f"https://archiveofourown.org/works/{external_id}",
                    duration_seconds=self._elapsed(_t),
                )

            # Update chapter content. Multi-chapter: edit each existing
            # chapter from local source; append any extras. Single-chapter:
            # fall back to the full-story Clean HTML blob (unchanged behaviour).
            try:
                ao3_chapters = await client.get_chapter_ids(external_id)
                has_chapters = bool(story.chapters) and story.total_chapters > 1

                if has_chapters:
                    local_chapters = sorted(story.chapters, key=lambda c: c.index)
                    for ao3_ch, local_ch in zip(ao3_chapters, local_chapters):
                        ch_content = self._read_chapter_content(story, local_ch.index)
                        if not ch_content:
                            logger.warning(
                                "AO3: Skipping ch%d edit for %s (no content)",
                                local_ch.index, story.name,
                            )
                            continue
                        await client.edit_chapter(
                            external_id, ao3_ch["chapter_id"],
                            title=_strip_chapter_prefix(local_ch.title),
                            content=ch_content,
                            publish=publish_live if publish_live else None,
                        )
                        logger.info(
                            "AO3: Updated chapter %s (local idx %d) of work %s",
                            ao3_ch["chapter_id"], local_ch.index, external_id,
                        )

                    # Append any local chapters that aren't on AO3 yet
                    if len(local_chapters) > len(ao3_chapters):
                        for local_ch in local_chapters[len(ao3_chapters):]:
                            ch_content = self._read_chapter_content(story, local_ch.index)
                            if not ch_content:
                                continue
                            await client.create_chapter(
                                external_id,
                                title=_strip_chapter_prefix(local_ch.title),
                                content=ch_content,
                                position=local_ch.index,
                                publish=False,
                            )
                            logger.info(
                                "AO3: Appended missing ch%d to work %s",
                                local_ch.index, external_id,
                            )
                elif ao3_chapters:
                    # Single-chapter work: push the full-story HTML blob
                    content = self._read_full_story_html(story)
                    if content:
                        await client.edit_chapter(
                            external_id, ao3_chapters[0]["chapter_id"], content=content,
                            publish=publish_live if publish_live else None,
                        )
                        logger.info(
                            "AO3: Updated chapter %s content of work %s",
                            ao3_chapters[0]["chapter_id"], external_id,
                        )
            except Exception as ch_err:
                logger.warning("AO3: Chapter content update failed: %s", ch_err)

            # If publishing live on an edit, walk every chapter and post
            # any drafts. Mirrors post()'s identical call site. Critical
            # for resumed/legacy works where create_work + later partial
            # runs left some chapters as drafts.
            if publish_live:
                try:
                    ps = await client.publish_all_draft_chapters(external_id)
                    logger.info(
                        "AO3: edit() publish-all-drafts for %s — "
                        "published=%d, already_posted=%d, failed=%d (total %d)",
                        story.name, ps["published"], ps["already_posted"],
                        len(ps["failed"]), ps["total"],
                    )
                except Exception as pub_err:
                    logger.warning(
                        "AO3: publish_all_draft_chapters failed on edit of "
                        "work %s: %s",
                        external_id, pub_err,
                    )

            return PostResult(
                success=True,
                external_id=external_id,
                external_url=f"https://archiveofourown.org/works/{external_id}",
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("AO3 edit failed for %s: %s", external_id, e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        """Replace chapter 1 content on AO3."""
        _t = self._start_timer()
        try:
            client = await self._ensure_client()

            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            chapters = await client.get_chapter_ids(external_id)
            if not chapters:
                return PostResult(
                    success=False, error="No chapters found",
                    duration_seconds=self._elapsed(_t),
                )

            await client.edit_chapter(
                external_id, chapters[0]["chapter_id"], content=content,
            )

            return PostResult(
                success=True,
                external_id=external_id,
                external_url=f"https://archiveofourown.org/works/{external_id}",
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("AO3 file replace failed for %s: %s", external_id, e)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
