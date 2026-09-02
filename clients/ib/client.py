"""Inkbunny API client with auth, search, submission details, faving users, and comment scraping.

This client uses two separate HTTP clients:
  - _http:      Talks to the official Inkbunny JSON API (api_login, api_search, etc.).
                Stateless aside from the session ID (SID) passed as a POST parameter.
  - _web_http:  Authenticated browser-style session for scraping submission pages.
                Required because the Inkbunny API does NOT expose comment text -- only
                the comment count is available via the API. To get actual comment bodies,
                we must log in through the web form and scrape the HTML.

The web client is lazily initialised (only created when comments are first requested)
to avoid unnecessary login round-trips for workflows that don't need comments.
"""

from __future__ import annotations
import asyncio
import html
import logging
import os
import re

import httpx

import config
from .models import (
    LoginResponse, SearchResponse, SubmissionDetail,
    FavingUsersResponse,
)

logger = logging.getLogger(__name__)


class InkbunnyClient:
    """Async Inkbunny API client.

    Manages two independent HTTP transports:
      _http      -- for the official JSON API (lightweight, no cookies needed)
      _web_http  -- for web-scraping comments (requires PHPSESSID cookie via web login)
    """

    def __init__(self, username: str = "", password: str = "",
                 proxy_url: str = "", proxy_key: str = ""):
        self.username = username or config.INKBUNNY_USERNAME
        self.password = password or config.INKBUNNY_PASSWORD
        # Session ID returned by api_login.php; passed as a POST param on every API call.
        self.sid: str | None = None
        # User ID returned by api_login.php; needed for web-scraping endpoints
        # that require a user_id parameter (e.g. watcher list pages).
        self.user_id: int = 0
        # Optional CF Worker proxy — opt-in backup. Inkbunny's API has
        # historically been generous to datacenter IPs, but the toggle
        # exists so we can flip it on if that ever changes.
        self._proxy_url = proxy_url
        self._proxy_key = proxy_key
        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("IB client using CF proxy: %s", proxy_url)
        else:
            transport = httpx.AsyncHTTPTransport(retries=2)
        # Primary HTTP client for all official API endpoints.
        self._http = httpx.AsyncClient(timeout=30.0, transport=transport)
        # Secondary HTTP client for web scraping (lazy-initialised in _ensure_web_session).
        # Kept separate because it carries browser cookies and follows redirects,
        # which the API client does not need.
        self._web_http: httpx.AsyncClient | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        """Shut down both HTTP clients and release their connection pools."""
        await self._http.aclose()
        if self._web_http:
            await self._web_http.aclose()

    # ── Auth ───────────────────────────────────────────────────

    async def login(self) -> LoginResponse:
        """Authenticate and get a session ID.

        After login, the ratings mask is immediately updated to include ALL content
        ratings (tags 2-5 = Violence, Sexual, Strong Violence, Strong Sexual).
        By default Inkbunny restricts newly-created sessions to General-only content,
        so without this step any search would silently omit mature/adult submissions.
        """
        resp = await self._http.post(
            f"{config.INKBUNNY_API_BASE}/api_login.php",
            data={"username": self.username, "password": self.password},
        )
        resp.raise_for_status()
        data = resp.json()
        if "sid" not in data:
            raise RuntimeError(f"Login failed: {data}")
        result = LoginResponse(**data)
        self.sid = result.sid
        self.user_id = result.user_id

        # Unlock all content ratings so searches return the full catalogue.
        # tag[2]=Violence, tag[3]=Sexual, tag[4]=Strong Violence, tag[5]=Strong Sexual.
        # Without this call, the API only returns General-rated submissions.
        rating_resp = await self._http.post(
            f"{config.INKBUNNY_API_BASE}/api_userrating.php",
            data={"sid": self.sid, "tag[2]": "yes", "tag[3]": "yes", "tag[4]": "yes", "tag[5]": "yes"},
        )
        try:
            rating_data = rating_resp.json()
            if "error_code" in rating_data:
                logger.warning("Rating unlock failed (error %s): %s — adult content may be missing",
                               rating_data.get("error_code"), rating_data.get("error_message", ""))
        except Exception:
            logger.warning("Rating unlock response was not JSON — adult content may be missing")
        logger.info("Logged in as %s (sid=%s...)", self.username, self.sid[:8])
        return result

    async def ensure_session(self, cached_sid: str | None = None) -> str:
        """Reuse a cached SID if it is still valid, otherwise re-authenticate.

        Inkbunny sessions expire after a period of inactivity. Rather than always
        performing a fresh login (which is slow and wasteful), we first probe the
        cached SID with a minimal search request. If the API returns an error_code,
        the session has expired and we fall through to a full login.

        This pattern lets callers persist the SID (e.g. in a database or file)
        between runs to avoid re-authenticating on every invocation.
        """
        if cached_sid:
            # Probe with a lightweight search -- if the SID is invalid, Inkbunny
            # responds with an error_code field rather than raising an HTTP error.
            resp = await self._http.post(
                f"{config.INKBUNNY_API_BASE}/api_search.php",
                data={"sid": cached_sid, "submissions_per_page": "1", "text": "", "username": self.username},
            )
            data = resp.json()
            if "error_code" not in data:
                # Session is still alive -- adopt it and skip login.
                self.sid = cached_sid
                if not self.user_id:
                    # user_id wasn't restored from cache (old DB schema).
                    # Do a fresh login to populate it; web scraping needs it.
                    logger.info("Cached session valid but user_id unknown, re-authenticating")
                else:
                    logger.info("Reusing cached session %s...", cached_sid[:8])
                    return cached_sid
            logger.info("Cached session expired, re-authenticating")
        # No cached SID, or it was expired -- perform a fresh login.
        result = await self.login()
        return result.sid

    # ── Search ─────────────────────────────────────────────────

    async def search_user_submissions(self, username: str | None = None) -> list[dict]:
        """Fetch all submission IDs for a user via paginated search.

        Walks through every page of results (100 submissions per page, the API max)
        until all submissions are collected. A rate-limiting sleep is inserted between
        pages to respect Inkbunny's server and avoid throttling.

        Returns a flat list of {submission_id, title} dicts -- just enough info
        for the caller to decide which submissions need full detail fetching.
        """
        username = username or self.username
        all_submissions = []
        page = 1

        for _page_safety in range(1000):
            resp = await self._http.post(
                f"{config.INKBUNNY_API_BASE}/api_search.php",
                data={
                    "sid": self.sid,
                    "submissions_per_page": "100",  # API maximum per page
                    "page": str(page),
                    "username": username,
                    "orderby": "create_datetime",
                    "type": "",       # All submission types (art, writing, music, etc.)
                    "text": "",       # No text filter -- we want everything
                    "get_rid": "no",  # Don't include submission RID (not needed here)
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if "error_code" in data:
                raise RuntimeError(f"Search error: {data}")

            search = SearchResponse(**data)
            for sub in search.submissions:
                all_submissions.append({
                    "submission_id": int(sub.submission_id),
                    "title": sub.title,
                })

            total_pages = int(search.pages_count)
            logger.info("Search page %d/%d — found %d submissions", page, total_pages, len(search.submissions))

            if page >= total_pages:
                break
            page += 1
            # Rate-limit between pages to avoid hammering the API
            await asyncio.sleep(config.REQUEST_DELAY_SECONDS)

        logger.info("Total submissions found: %d", len(all_submissions))
        return all_submissions

    # ── Submission Details ─────────────────────────────────────

    async def get_submission_details(self, submission_ids: list[int]) -> list[SubmissionDetail]:
        """Fetch full details for submissions in configurable batches.

        The Inkbunny api_submissions.php endpoint accepts a comma-separated list of
        submission IDs, but sending too many at once can cause timeouts or oversized
        responses. We split into batches of SUBMISSION_BATCH_SIZE (from config) to
        keep each request manageable.

        Rate-limiting is applied between batches (not after the final one) to stay
        within polite usage limits.

        Individual parse failures are logged and skipped rather than aborting the
        entire batch -- this is intentional so that one malformed submission doesn't
        prevent the rest from being processed.
        """
        all_details = []
        batch_size = config.SUBMISSION_BATCH_SIZE

        for i in range(0, len(submission_ids), batch_size):
            batch = submission_ids[i:i + batch_size]
            # The API expects submission IDs as a single comma-separated string
            ids_str = ",".join(str(sid) for sid in batch)
            resp = await self._http.post(
                f"{config.INKBUNNY_API_BASE}/api_submissions.php",
                data={
                    "sid": self.sid,
                    "submission_ids": ids_str,
                    "show_description": "yes",  # Include full description HTML
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if "error_code" in data:
                raise RuntimeError(f"Submissions error: {data}")

            for sub_data in data.get("submissions", []):
                try:
                    detail = SubmissionDetail(**sub_data)
                    all_details.append(detail)
                except Exception as e:
                    # Log and skip individual failures so the rest of the batch still gets processed
                    logger.warning("Failed to parse submission %s: %s", sub_data.get("submission_id"), e)

            logger.info("Fetched details batch %d-%d of %d", i + 1, min(i + batch_size, len(submission_ids)), len(submission_ids))
            # Only sleep between batches, not after the final one
            if i + batch_size < len(submission_ids):
                await asyncio.sleep(config.REQUEST_DELAY_SECONDS)

        return all_details

    # ── Faving Users ───────────────────────────────────────────

    async def get_faving_users(self, submission_id: int) -> list[dict]:
        """Get all users who faved a submission.

        Returns a list of {user_id, username} dicts. On API error (e.g. submission
        deleted or private), logs a warning and returns an empty list rather than
        raising -- this keeps batch processing from halting on a single failure.
        """
        resp = await self._http.post(
            f"{config.INKBUNNY_API_BASE}/api_submissionfavingusers.php",
            data={"sid": self.sid, "submission_id": str(submission_id)},
        )
        resp.raise_for_status()
        data = resp.json()
        if "error_code" in data:
            logger.warning("Faving users error for %d: %s", submission_id, data)
            return []

        result = FavingUsersResponse(**data)
        return [{"user_id": int(u.user_id), "username": u.username} for u in result.favingusers]

    # ── Comment Scraping (web session) ─────────────────────────
    #
    # WHY WEB SCRAPING?
    # The Inkbunny API provides a comment *count* on submissions, but does NOT
    # expose the actual comment text, authors, or threading through any API
    # endpoint. The only way to retrieve comment content is to scrape the
    # submission's HTML page while logged in (comments are hidden from guests).
    #
    # This requires a separate HTTP client (_web_http) that holds a PHPSESSID
    # cookie obtained through the web login form, distinct from the API's SID.
    #

    async def _ensure_web_session(self) -> None:
        """Login via the Inkbunny web form to get a PHPSESSID cookie for scraping.

        This performs a two-step browser-style login:
        1. GET the login page to extract the CSRF token from a hidden form field.
           Inkbunny uses a 'token' field to prevent cross-site request forgery.
        2. POST credentials + token to login_process.php, which sets a PHPSESSID
           cookie in the response. httpx stores this cookie automatically for
           subsequent requests on the same client instance.

        The web client is only created once (guarded by the None check) and reused
        for all subsequent comment scraping calls in this session.
        """
        if self._web_http is not None:
            # Quick session check — verify existing session is still valid
            try:
                probe = await self._web_http.get(f"{config.INKBUNNY_API_BASE}/")
                if "logout" in probe.text:
                    return  # Session still good
                # Session expired — close and re-create
                await self._web_http.aclose()
                self._web_http = None
            except Exception:
                await self._web_http.aclose()
                self._web_http = None

        # follow_redirects=True because Inkbunny redirects after login
        web_transport = httpx.AsyncHTTPTransport(retries=2)
        self._web_http = httpx.AsyncClient(timeout=30.0, follow_redirects=True, transport=web_transport)

        # Step 1: Fetch the login page and extract the CSRF token.
        # The token is in a hidden input: <input name="token" value="...">
        login_page = await self._web_http.get(f"{config.INKBUNNY_API_BASE}/login.php")
        token_match = re.search(r"name=['\"]token['\"].*?value=['\"]([^'\"]+)['\"]", login_page.text)
        if not token_match:
            logger.error("Web login failed: could not find CSRF token")
            await self._web_http.aclose()
            self._web_http = None
            return

        token = token_match.group(1)

        # Step 2: Submit the login form with extracted CSRF token.
        resp = await self._web_http.post(
            f"{config.INKBUNNY_API_BASE}/login_process.php",
            data={
                "token": token,
                "username": self.username,
                "password": self.password,
            },
        )
        # Presence of "logout.php" link in the response body indicates successful login
        if "logout" in resp.text:
            logger.info("Web session established for comment scraping")
        else:
            logger.warning("Web login may have failed (status %d) — closing web client", resp.status_code)
            await self._web_http.aclose()
            self._web_http = None

    async def scrape_comments(self, submission_id: int) -> list[dict]:
        """Scrape comments from a submission page via authenticated web session.

        Parses comments using regex against the page HTML. Each comment on Inkbunny
        has a consistent DOM structure with predictable element IDs, making regex
        extraction reliable. The key elements we extract per comment:

          - comment_id:          From the container div id='commentid_NNN'
          - username:            From a hidden input id='comment_ownername_commentid_NNN'
          - comment_text:        From a hidden input id='bbcode_commentid_NNN' (BBCode source)
          - commented_at:        From a div id='commentid_NNN_exact' (human-readable date)
          - is_reply:            Whether the comment div has the 'indented' CSS class
          - reply_to_comment_id: From an anchor with title='In reply to' linking to parent
        """
        await self._ensure_web_session()

        resp = await self._web_http.get(f"{config.INKBUNNY_API_BASE}/s/{submission_id}")
        page_html = resp.text

        comments = []

        # First pass: find all comment container divs and collect their IDs.
        # Each comment is wrapped in a div like:
        #   <div class='widget_commentsList_comment ...' id='commentid_12345'>
        comment_divs = re.finditer(
            r"<div\s+class='widget_commentsList_comment[^']*'\s+id='commentid_(\d+)'>",
            page_html,
        )
        comment_ids = [m.group(1) for m in comment_divs]

        # Second pass: for each comment ID, extract individual fields via targeted regex.
        for cid in comment_ids:
            comment = {"comment_id": int(cid), "submission_id": submission_id}

            # USERNAME: Stored in a hidden input used by the edit/reply JS.
            # Element: <input id='comment_ownername_commentid_NNN' value='username'>
            owner_match = re.search(
                rf"id='comment_ownername_commentid_{cid}'[^>]*value='([^']*)'",
                page_html,
            )
            comment["username"] = html.unescape(owner_match.group(1)) if owner_match else ""

            # COMMENT TEXT: Stored in a hidden input containing the raw BBCode source.
            # Element: <input id='bbcode_commentid_NNN' value='comment bbcode here'>
            # We use re.DOTALL because comment text can contain newlines.
            # html.unescape is needed because the value attribute is HTML-encoded.
            text_match = re.search(
                rf"id='bbcode_commentid_{cid}'[^>]*value='(.*?)'(?:\s*/>|\s*>)",
                page_html,
                re.DOTALL,
            )
            if not text_match:
                # Fallback: try double-quoted value attribute
                text_match = re.search(
                    rf'id="bbcode_commentid_{cid}"[^>]*value="(.*?)"(?:\s*/>|\s*>)',
                    page_html,
                    re.DOTALL,
                )
            comment["comment_text"] = html.unescape(text_match.group(1)) if text_match else ""

            # TIMESTAMP: The exact date is in a separate div that shows on hover.
            # Element: <div id='commentid_NNN_exact'>2025-06-15 03:22:10</div>
            date_match = re.search(
                rf"id='commentid_{cid}_exact'[^>]*>([^<]+)</div>",
                page_html,
            )
            comment["commented_at"] = date_match.group(1).strip() if date_match else ""

            # REPLY DETECTION: Replies are visually indented via CSS class 'indented'
            # on the comment container div. We check both possible attribute orderings
            # (id before class, class before id) because Inkbunny's HTML is inconsistent.
            is_indented = bool(re.search(
                rf"id='commentid_{cid}'[^>]*class='[^']*indented",
                page_html,
            ))
            # Check the other attribute ordering: class appears before id
            if not is_indented:
                is_indented = bool(re.search(
                    rf"class='widget_commentsList_comment[^']*indented[^']*'\s+id='commentid_{cid}'",
                    page_html,
                ))
            comment["is_reply"] = is_indented

            # REPLY-TO LINK: If this is a reply, there's an anchor linking to the parent:
            #   <a href='/s/12345#commentid_67890' title='In reply to'>
            # We search only a 3000-char window after the comment div to avoid accidentally
            # matching a link from a different comment further down the page.
            reply_to = None
            start_idx = page_html.find(f"id='commentid_{cid}'")
            if start_idx >= 0:
                # Limit search to a local section to prevent cross-comment false matches
                section = page_html[start_idx:start_idx + 3000]
                reply_match = re.search(
                    r"href='/s/\d+#commentid_(\d+)'[^>]*title='In reply to'",
                    section,
                )
                if reply_match:
                    reply_to = int(reply_match.group(1))
            comment["reply_to_comment_id"] = reply_to

            comments.append(comment)

        logger.info("Scraped %d comments from submission %d", len(comments), submission_id)
        return comments

    # ── Watcher Scraping (web session) ───────────────────────────
    #
    # WHY WEB SCRAPING?
    # The Inkbunny API does NOT provide an endpoint to list a user's watchers.
    # The only way to retrieve watcher information is to scrape the "watching"
    # user list page while logged in via the web interface, similar to how
    # comments are scraped above.
    #

    async def scrape_watchers(self) -> list[str]:
        """Scrape the list of users watching you from usersviewall.php.

        There is no Inkbunny API endpoint for retrieving a user's watcher list.
        This method logs in via the web session (same PHPSESSID cookie auth used
        by comment scraping) and paginates through the usersviewall.php page to
        extract all watchers.

        Uses mode=watched_by to get users watching YOU (not users you watch).
        The page renders watchers as widget_userNameSmall links with no user_id,
        so we extract usernames only.

        Returns a list of username strings. On error, logs a warning and returns
        whatever was collected so far rather than raising -- this keeps batch
        processing from halting on a transient failure.
        """
        await self._ensure_web_session()

        all_watchers: list[str] = []
        seen: set[str] = set()
        page = 1

        for _page_safety in range(1000):
            try:
                resp = await self._web_http.get(
                    f"{config.INKBUNNY_API_BASE}/usersviewall.php",
                    params={
                        "mode": "watchedby",
                        "user_id": str(self.user_id),
                        "page": str(page),
                    },
                )
                resp.raise_for_status()
                page_html = resp.text

                # Inkbunny renders watcher entries as:
                #   <a class="widget_userNameSmall" href="/Username">Username</a>
                # Extract the username from the href attribute.
                usernames = re.findall(
                    r'widget_userNameSmall"\s*href="/([A-Za-z0-9_\-]+)"',
                    page_html,
                )

                # Filter out our own username (appears in the page header)
                # and deduplicate within/across pages.
                page_new = []
                for u in usernames:
                    if u != self.username and u not in seen:
                        seen.add(u)
                        page_new.append(u)

                if not page_new:
                    if page == 1:
                        logger.info("No watchers found for user_id %d", self.user_id)
                    break

                all_watchers.extend(page_new)
                logger.info("Scraped watcher page %d — found %d users", page, len(page_new))

                # Check if there's a next page link; if not, we've reached the end.
                has_next = re.search(
                    rf'usersviewall\.php[^"]*page={page + 1}',
                    page_html,
                )
                if not has_next:
                    break

                page += 1
                await asyncio.sleep(config.REQUEST_DELAY_SECONDS)

            except Exception as e:
                logger.warning("Error scraping watcher page %d: %s", page, e)
                break

        logger.info("Total watchers scraped: %d", len(all_watchers))
        return all_watchers

    # ── Posting / Upload ────────────────────────────────────────

    async def upload_submission(
        self,
        file_path: str,
        *,
        submission_type: str = "4",
        thumbnail_path: str | None = None,
    ) -> int:
        """Upload a file to Inkbunny and return the new submission_id.

        This creates a submission in a draft/pending state. Call edit_submission()
        afterwards to set title, description, tags, and make it visible.

        Args:
            file_path: Absolute path to the file to upload.
            submission_type: IB type code ("1"=picture, "2"=flash, "3"=music, "4"=writing).
            thumbnail_path: Optional path to a thumbnail image (PNG/JPG, max 300x300).

        Returns:
            The new submission_id as an integer.

        Raises:
            RuntimeError: If the upload fails or returns an error.
        """
        if not self.sid:
            raise RuntimeError("Not logged in — call ensure_session() first")

        with open(file_path, "rb") as f:
            file_data = f.read()

        filename = os.path.basename(file_path)
        files = {"uploadedfile[0]": (filename, file_data)}
        if thumbnail_path and os.path.isfile(thumbnail_path):
            with open(thumbnail_path, "rb") as tf:
                thumb_data = tf.read()
            files["uploadedthumbnail[0]"] = (os.path.basename(thumbnail_path), thumb_data, "image/png")

        resp = await self._http.post(
            f"{config.INKBUNNY_API_BASE}/api_upload.php",
            data={"sid": self.sid, "submission_type": submission_type},
            files=files,
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error_code" in data:
            raise RuntimeError(f"Upload failed: {data.get('error_message', data)}")

        submission_id = int(data.get("submission_id", 0))
        if not submission_id:
            raise RuntimeError(f"Upload response missing submission_id: {data}")

        logger.info("Uploaded file to IB — submission_id=%d", submission_id)
        return submission_id

    async def add_files_to_submission(
        self,
        submission_id: int,
        *,
        file_paths: list[str] | None = None,
        thumbnail_path: str | None = None,
        replace_file_id: int | None = None,
    ) -> dict:
        """Add files and/or a thumbnail to an existing submission.

        Calls api_upload.php with submission_id set. Per the IB API docs,
        only ONE thumbnail can be sent at a time, and it must be paired with
        either a NEW file at the same index or `replace=<file_id>` to attach
        to an existing file.

        Args:
            submission_id: The existing IB submission to update.
            file_paths: Optional list of new files to add as additional pages.
                Each becomes uploadedfile[N] in the multipart request.
            thumbnail_path: Optional thumbnail (PNG/JPG, max 300x300).
                If `replace_file_id` is set, the thumbnail attaches to that
                existing file. Otherwise it pairs with the first new file at
                index 0.
            replace_file_id: When attaching a thumbnail to an EXISTING file
                without re-uploading the file, set this to the file's
                `file_id` (from api_submissions.php with show_writing=yes).
                The IB API uses `replace=<file_id>` for this.

        Returns:
            The raw API response dict.

        Raises:
            RuntimeError: If the upload fails or returns an error.
        """
        if not self.sid:
            raise RuntimeError("Not logged in — call ensure_session() first")

        if not file_paths and not thumbnail_path:
            raise ValueError("Must provide at least one of file_paths or thumbnail_path")

        files: dict = {}
        if file_paths:
            for i, fp in enumerate(file_paths):
                with open(fp, "rb") as f:
                    files[f"uploadedfile[{i}]"] = (os.path.basename(fp), f.read())

        if thumbnail_path and os.path.isfile(thumbnail_path):
            with open(thumbnail_path, "rb") as tf:
                thumb_data = tf.read()
            ext = os.path.splitext(thumbnail_path)[1].lower().lstrip(".")
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif"}.get(ext, "image/png")
            # Index 0: pairs with uploadedfile[0] for new uploads, OR
            # uses `replace` to target an existing file
            files["uploadedthumbnail[0]"] = (os.path.basename(thumbnail_path), thumb_data, mime)

        data = {
            "sid": self.sid,
            "submission_id": str(submission_id),
        }
        if replace_file_id is not None:
            data["replace"] = str(replace_file_id)

        resp = await self._http.post(
            f"{config.INKBUNNY_API_BASE}/api_upload.php",
            data=data,
            files=files,
            timeout=120.0,
        )
        resp.raise_for_status()
        result = resp.json()
        if "error_code" in result:
            raise RuntimeError(f"Add files failed: {result.get('error_message', result)}")

        logger.info(
            "IB: Added to submission %d — files=%d thumbnail=%s replace=%s",
            submission_id, len(file_paths or []), bool(thumbnail_path), replace_file_id,
        )
        return result

    async def edit_submission(
        self,
        submission_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        story: str | None = None,
        keywords: str | None = None,
        rating_tag_2: str | None = None,
        rating_tag_3: str | None = None,
        rating_tag_4: str | None = None,
        rating_tag_5: str | None = None,
        visibility: str | None = None,
        scraps: str | None = None,
        friends_only: str | None = None,
        guest_block: str | None = None,
    ) -> dict:
        """Edit an existing Inkbunny submission's metadata and/or story text.

        Only fields that are explicitly provided (not None) are sent to the API.
        IB's API blanks any field included in the request, so omitting a field
        preserves its current value on the server.

        Args:
            description: Short summary/blurb shown on the submission page (BBCode).
            story: Full story text displayed in the reading panel (BBCode).
                   These are SEPARATE fields — don't put the story in description.
        """
        if not self.sid:
            raise RuntimeError("Not logged in — call ensure_session() first")

        data: dict[str, str] = {
            "sid": self.sid,
            "submission_id": str(submission_id),
        }
        if title is not None:
            data["title"] = title
        if description is not None:
            data["desc"] = description
        if story is not None:
            data["story"] = story
        if keywords is not None:
            data["keywords"] = keywords
        if rating_tag_2 is not None:
            data["tag[2]"] = rating_tag_2
        if rating_tag_3 is not None:
            data["tag[3]"] = rating_tag_3
        if rating_tag_4 is not None:
            data["tag[4]"] = rating_tag_4
        if rating_tag_5 is not None:
            data["tag[5]"] = rating_tag_5
        if visibility is not None:
            data["visibility"] = visibility
        if scraps is not None:
            data["scraps"] = scraps
        if friends_only is not None:
            data["friends_only"] = friends_only
        if guest_block is not None:
            data["guest_block"] = guest_block

        resp = await self._http.post(
            f"{config.INKBUNNY_API_BASE}/api_editsubmission.php",
            data=data,
        )
        resp.raise_for_status()
        result = {}
        body = resp.content.strip()
        if body:
            try:
                result = resp.json()
            except Exception:
                logger.debug("IB edit returned non-JSON response (%d bytes)", len(body))
            if "error_code" in result:
                raise RuntimeError(f"Edit failed: {result.get('error_message', result)}")

        logger.info("Edited IB submission %d — fields sent: %s", submission_id,
                     [k for k in data if k not in ("sid", "submission_id")])
        return result

    async def delete_submission(self, submission_id: int) -> dict:
        """Delete an Inkbunny submission.

        Uses api_delsubmission.php to permanently remove a submission.
        """
        if not self.sid:
            raise RuntimeError("Not logged in — call ensure_session() first")

        resp = await self._http.post(
            f"{config.INKBUNNY_API_BASE}/api_delsubmission.php",
            data={"sid": self.sid, "submission_id": str(submission_id)},
        )
        resp.raise_for_status()
        result = resp.json()
        if "error_code" in result:
            raise RuntimeError(f"Delete failed: {result.get('error_message', result)}")
        logger.info("Deleted IB submission %d", submission_id)
        return result
