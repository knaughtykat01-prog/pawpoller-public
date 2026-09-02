"""FurAffinity platform poster.

Uses the existing FAClient (clients/fa/client.py) with cookie-based auth
(_fa_http with cookies a+b). FA has no official API — posting uses HTML
form scraping (same approach as PostyBirb).

Post flow (3-step form scrape):
  1. GET /submit/ → scrape hidden 'key' input
  2. POST /submit/upload → multipart: key + submission_type + file
  3. POST /submit/finalize → urlencoded: key + title + desc + tags + rating

Edit flow:
  1. GET /controls/submissions/changesubmission/{id}/ → scrape form + key
  2. POST with updated fields

Rating mapping:
  General → "0", Mature → "2", Adult → "1" (note: Adult=1, not 2)

Constraints:
  - 10 MB max file size
  - 60 char title limit
  - 3 tag minimum, 500 char max tag string
  - 70 second minimum between consecutive posts
  - Account needs 11+ posts (CAPTCHA for new accounts)
"""

from __future__ import annotations

import logging

import config
from clients.fa.client import FAClient
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage

logger = logging.getLogger(__name__)


class FurAffinityPoster(PlatformPoster):

    platform_id = "fa"
    platform_name = "FurAffinity"
    supports_edit = True
    supports_file_replace = True  # Via the edit page
    min_post_interval = 70  # FA enforces this
    # "any" since 3.26.0. FA posts fine from the server — proven 2026-08-21 by
    # a full three-step submit (form key → upload → finalize) via the CF Worker
    # proxy: view/66103446. The month of failures before that was an EXPIRED
    # SESSION, invisible because validate_cookies could not fail (3.19.1).
    #
    # ⚠ This was "desktop" until 3.26.0, kept on the stated grounds that it was
    # "FAILURE-RECOVERY ROUTING, not a hard block — the server always attempts
    # the post first". **That was wrong for scheduled work, which is most of
    # it.** `requires_mode` does two unrelated jobs:
    #
    #   1. the manager's post-failure handoff (what the comment described), and
    #   2. the value stamped into `posting_queue.requires` when a job is
    #      SCHEDULED (routes/editor_api.py), which the scheduler then filters on
    #      in SQL — `requires IN ('any', <mode>)`.
    #
    # Under (2) the server never attempted anything: a scheduled FA job was
    # invisible to it and simply waited for a desktop that might never open.
    # Measured 2026-08-23 — one artwork scheduled to 8 platforms in a single
    # batch, all for 11:30: ib, sf, bsky, ik, da, e621 and fn all completed on
    # the server; the FA row alone sat pending. Twelve jobs were stranded that
    # way, including queue #3709 from 24 July.
    #
    # The old comment even said "if it strands jobs again, the right fix is
    # honest errors, not a bigger queue". It stranded twelve. This is the fix.
    #
    # The desktop can still run FA jobs — "any" means EITHER instance may, not
    # "server only". What is gone is the routing that stopped the server trying.
    requires_mode = "any"
    max_file_size = 10 * 1024 * 1024  # 10 MB
    accepted_file_types = ["pdf", "doc", "docx", "rtf", "txt", "odt", "jpg", "jpeg", "png", "gif"]

    def __init__(self, account_id: int | None = None):
        self._client: FAClient | None = None
        # Fingerprint of the credentials the cached client was built with.
        # See _ensure_client for why the cache is keyed on this.
        self._client_creds: tuple | None = None
        # Which FA account to post as. None → the default account.
        self.account_id = account_id

    async def _ensure_client(self) -> FAClient:
        # ⚠ The cache is keyed on the CREDENTIALS, not held forever (3.19.2).
        # It used to be `if self._client: return self._client` — and the manager
        # caches poster instances per (platform, account_id) — so cookies pasted
        # into Settings did not take effect until the container restarted. Live
        # failure: fresh cookies saved, two posts attempted, both rejected with
        # the stale session. Credentials are re-resolved on every call (a
        # cached in-memory settings read) and the client rebuilt only when they
        # actually changed, so a paste takes effect on the next post while the
        # common case stays a cheap comparison.

        from database.db import get_connection
        from database import accounts as _accts
        conn = get_connection()
        try:
            acct_id = self.account_id
            if acct_id is None:
                acct_id = _accts.get_default_account_id(conn, "fa", create=True)
                self.account_id = acct_id
            acct = _accts.get_account(conn, acct_id)
            is_default = bool(acct["is_default"]) if acct else True
        finally:
            conn.close()
        creds = config.resolve_account_credentials("fa", acct_id, is_default)
        username = creds.get("fa_username", "")
        cookie_a = creds.get("fa_cookie_a", "")
        cookie_b = creds.get("fa_cookie_b", "")
        if not cookie_a or not cookie_b:
            raise RuntimeError(
                f"FurAffinity cookies not configured for account {acct_id}"
                + ("" if is_default else
                   " (a non-default account reads its OWN credential fields, "
                   "not the main FurAffinity form — edit the account's "
                   "credentials in Settings → Accounts)"))

        fingerprint = (username, cookie_a, cookie_b)
        if self._client is not None and self._client_creds == fingerprint:
            return self._client

        client = FAClient(username=username, cookie_a=cookie_a, cookie_b=cookie_b)
        if not await client.validate_cookies():
            raise RuntimeError(
                f"FurAffinity cookies are invalid or expired for account "
                f"{acct_id} ({username or 'unknown'})"
                + ("" if is_default else
                   " — note this account keeps its own cookies; pasting into "
                   "the main FurAffinity credentials form updates the DEFAULT "
                   "account, not this one"))
        self._client = client
        self._client_creds = fingerprint
        return self._client

    async def post(self, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        try:
            client = await self._ensure_client()
            if not package.file_path:
                return PostResult(success=False, error="No file for FA upload", duration_seconds=self._elapsed(_t))

            rating = _rating_to_fa(package.rating)
            # FA tags are space-separated with underscores for multi-word
            keywords = " ".join(t.replace(" ", "_") for t in package.tags)
            settings = config.get_settings()

            is_image = package.file_type in ("png", "jpg", "jpeg", "gif", "webp")
            if is_image:
                # Visual-art submission. The category/species/gender come from
                # the artwork_fa_* settings — the story defaults (cat="13"=Story)
                # are wrong for art. package.extra wins if the artwork set them.
                cat = package.extra.get("cat", settings.get("artwork_fa_category", "1"))
                atype = package.extra.get("atype", settings.get("artwork_fa_theme", "1"))
                species = package.extra.get("species", settings.get("artwork_fa_species", "1"))
                gender = package.extra.get("gender", settings.get("artwork_fa_gender", "0"))
                result = await client.submit_visual(
                    package.file_path,
                    title=package.title[:60],
                    description=package.description,
                    keywords=keywords,
                    rating=rating,
                    cat=cat, atype=atype, species=species, gender=gender,
                )
            else:
                cat = package.extra.get("cat", settings.get("posting_fa_category", "13"))
                atype = package.extra.get("atype", settings.get("posting_fa_theme", "1"))
                species = package.extra.get("species", settings.get("posting_fa_species", "1"))
                gender = package.extra.get("gender", settings.get("posting_fa_gender", "0"))
                result = await client.submit_story(
                    package.file_path,
                    title=package.title[:60],
                    description=package.description,
                    keywords=keywords,
                    rating=rating,
                    cat=cat, atype=atype, species=species, gender=gender,
                    thumbnail_path=package.thumbnail_path,
                )

            return PostResult(
                success=True,
                external_id=result.get("submission_id", ""),
                external_url=result.get("url", ""),
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("FA post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        """Edit metadata AND refresh the PDF on an existing FA submission.

        FA's changeinfo endpoint only updates metadata. To keep drifted
        local PDFs in sync we also call replace_file() via the changestory
        endpoint. Set ``package.extra["skip_content_refresh"] = True`` to
        do a metadata-only edit.
        """
        _t = self._start_timer()
        try:
            client = await self._ensure_client()
            rating = _rating_to_fa(package.rating)
            keywords = " ".join(t.replace(" ", "_") for t in package.tags)

            result = await client.edit_submission(
                external_id,
                title=package.title[:60],
                description=package.description,
                keywords=keywords,
                rating=rating,
            )

            skip_content = bool(package.extra.get("skip_content_refresh", False))
            if package.file_path and not skip_content:
                file_result = await self.replace_file(external_id, package.file_path)
                if not file_result.success:
                    logger.warning(
                        "FA edit: metadata updated but content replace failed for %s: %s",
                        external_id, file_result.error,
                    )
                    return PostResult(
                        success=False,
                        external_id=external_id,
                        external_url=result.get("url", ""),
                        error=f"Metadata updated but content refresh failed: {file_result.error}",
                        duration_seconds=self._elapsed(_t),
                    )

            return PostResult(
                success=True,
                external_id=external_id,
                external_url=result.get("url", ""),
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("FA edit failed for %s: %s", external_id, e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    async def probe_exists(self, external_id: str) -> bool | None:
        """Check whether an FA submission still exists.

        Hits the public view page — FA returns 404 for deleted submissions
        and redirects to a "The submission you are trying to find is not
        in our database" page. Returns None on transient errors so we
        don't falsely mark live submissions as deleted.
        """
        try:
            client = await self._ensure_client()
            fa = await client._get_fa_http()
            resp = await fa.get(
                f"https://www.furaffinity.net/view/{external_id}/",
                follow_redirects=False,
            )
            if resp.status_code == 404:
                return False
            if resp.status_code == 200:
                if "is not in our database" in resp.text or "System Error" in resp.text:
                    return False
                return True
            if 300 <= resp.status_code < 400:
                return True
            return None
        except Exception as e:
            logger.warning("FA probe_exists(%s) failed: %s", external_id, e)
            return None

    async def probe_draft_state(self, external_id: str) -> bool | None:
        """True if the submission is in Scraps (FA's closest equivalent of draft).

        Scraps are hidden from the main gallery, browse, and search results,
        but watchers still get notifications and the profile's Scraps tab
        lists them. Reads the changeinfo form's scrap checkbox; returns
        None on transient errors so a flapping network doesn't paint live
        cells as drafts.
        """
        try:
            client = await self._ensure_client()
            return await client.probe_scrap_state(external_id)
        except Exception as e:
            logger.warning("FA probe_draft_state(%s) failed: %s", external_id, e)
            return None

    async def publish_draft(self, external_id: str) -> PostResult:
        """Flip a scrapped submission into the main gallery.

        Calls edit_submission with scrap=False, which omits the scrap field
        from the changeinfo POST and clears the checkbox. All other metadata
        is preserved (the edit form re-emits scraped values).
        """
        _t = self._start_timer()
        try:
            client = await self._ensure_client()
            await client.edit_submission(external_id, scrap=False)
            return PostResult(
                success=True,
                external_id=external_id,
                external_url=f"https://www.furaffinity.net/view/{external_id}/",
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("FA publish_draft(%s) failed: %s", external_id, e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        """Replace the story file via FA's changestory endpoint."""
        _t = self._start_timer()
        try:
            client = await self._ensure_client()
            fa = await client._get_fa_http()

            import os
            with open(file_path, "rb") as f:
                file_data = f.read()

            edit_url = f"https://www.furaffinity.net/controls/submissions/changestory/{external_id}/"
            resp = await fa.post(
                edit_url,
                data={"update": "yes"},
                files={"newfile": (os.path.basename(file_path), file_data)},
                headers={"Referer": edit_url},
                timeout=60.0,
            )

            if "nocache" in str(resp.url) or "/view/" in str(resp.url):
                return PostResult(
                    success=True,
                    external_id=external_id,
                    external_url=f"https://www.furaffinity.net/view/{external_id}/",
                    duration_seconds=self._elapsed(_t),
                )
            return PostResult(
                success=False, error="File replacement returned unexpected URL",
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("FA file replace failed for %s: %s", external_id, e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors = super().validate(package)
        if len(package.title) > 60:
            errors.append(f"FA title max 60 chars (got {len(package.title)})")
        if len(package.tags) < 3:
            errors.append(f"FA requires at least 3 tags (got {len(package.tags)})")
        tag_str = " ".join(package.tags)
        if len(tag_str) > 500:
            errors.append(f"FA tag string max 500 chars (got {len(tag_str)})")
        return errors


def _rating_to_fa(rating: str) -> str:
    """Convert rating string to FA's rating code.

    FA's rating values are unusual: Adult=1, Mature=2, General=0.
    """
    r = rating.lower()
    if r in ("adult", "explicit", "nsfw"):
        return "1"
    elif r in ("mature", "questionable"):
        return "2"
    return "0"
