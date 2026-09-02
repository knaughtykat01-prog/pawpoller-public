"""FurAffinity client using FAExport API for data and direct cookies for validation.

This client uses a DUAL HTTP CLIENT PATTERN, similar to the Inkbunny client but for
different reasons:

  _http      -- Talks to FAExport (https://faexport.spangle.org.uk), a third-party
                REST API that wraps FurAffinity's data into clean JSON endpoints.
                No authentication needed -- FAExport is a public proxy.

  _fa_http   -- Talks directly to furaffinity.net with the user's session cookies
                (cookie 'a' and cookie 'b'). Used ONLY for cookie validation, not
                for data retrieval.

WHY FAEXPORT INSTEAD OF DIRECT SCRAPING?
FurAffinity does not have an official API. The only way to get structured data is to
scrape the HTML pages directly. FAExport handles this scraping server-side and exposes
the data as JSON, which is far more reliable and maintainable than parsing FA's HTML
ourselves. FAExport provides endpoints for gallery listings, submission details, and
comments -- covering all our data needs.

The direct FA client (_fa_http) exists solely to validate that the user's cookies are
still active, since FAExport doesn't support authenticated requests and we need valid
cookies for other parts of the system.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)


# FA renders the signed-in user's avatar in the mobile nav on every logged-in
# page:
#     <img class="loggedin_user_avatar avatar" alt="ThirdFur" ...>
# The class carries a second token, so the pattern must not anchor on the
# closing quote — an earlier attempt did and matched nothing, which reads
# exactly like "no marker" and would have manufactured a false mismatch.
_LOGGED_IN_AVATAR_RE = re.compile(
    r'class="[^"]*loggedin_user_avatar[^"]*"[^>]*alt="([^"]+)"', re.I)
# Fallback: the same block links the userpage immediately before the avatar.
_LOGGED_IN_LINK_RE = re.compile(
    r'href="/user/([A-Za-z0-9._~\[\]-]+)/?"[^>]*>\s*<img[^>]*loggedin_user_avatar', re.I)


def _logged_in_username(html: str) -> str:
    """The account an FA page is signed in as, or "" if it does not say."""
    for rx in (_LOGGED_IN_AVATAR_RE, _LOGGED_IN_LINK_RE):
        m = rx.search(html or "")
        if m:
            return m.group(1).strip()
    return ""


def _same_fa_user(a: str, b: str) -> bool:
    """Compare two FA usernames the way FurAffinity itself does.

    URLs are lowercased with underscores dropped (`Kii_Tiger` -> `kiitiger`)
    while the display name keeps both, so a literal comparison reports a
    mismatch between two spellings of one account — which would lock someone
    out of their own credentials.
    """
    norm = lambda x: (x or "").strip().lower().replace("_", "")
    return bool(norm(a)) and norm(a) == norm(b)



class FAClient:
    """FurAffinity data client -- FAExport for gallery/submission data, cookies for validation.

    Two independent HTTP transports:
      _http      -- unauthenticated client for FAExport JSON API (public proxy)
      _fa_http   -- authenticated client for direct FA access (cookies, lazy-init)
    """

    def __init__(self, username: str = "", cookie_a: str = "", cookie_b: str = "",
                 proxy_url: str = "", proxy_key: str = ""):
        self.username = username or config.FA_USERNAME
        # FA uses two cookies ('a' and 'b') together as the session token.
        # Both must be present and valid for an authenticated session.
        self.cookie_a = cookie_a or config.FA_COOKIE_A
        self.cookie_b = cookie_b or config.FA_COOKIE_B
        # Optional CF Worker proxy — opt-in backup. Affects both the
        # FAExport client and the lazy direct-FA cookie client. The
        # latter is the more likely target if FA's Cloudflare ever
        # starts challenging datacenter IPs. Enabled via fa_use_cf_proxy.
        self._proxy_url = proxy_url
        self._proxy_key = proxy_key
        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("FA client using CF proxy: %s", proxy_url)
        else:
            transport = httpx.AsyncHTTPTransport(retries=2)
        # Primary client: talks to FAExport (no auth needed)
        self._http = httpx.AsyncClient(timeout=30.0, transport=transport)
        # Secondary client: direct FA with cookies (lazy-initialised)
        self._fa_http: httpx.AsyncClient | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        """Shut down both HTTP clients and release their connection pools."""
        await self._http.aclose()
        if self._fa_http:
            await self._fa_http.aclose()

    def _fa_cookies(self) -> dict[str, str]:
        """Return the FA session cookies as a dict for httpx cookie injection."""
        return {"a": self.cookie_a, "b": self.cookie_b}

    async def _get_with_retry(
        self,
        url: str,
        *,
        params: dict | None = None,
        max_retries: int = 1,
        max_sleep: float = 60.0,
    ) -> httpx.Response:
        """GET via the FAExport client with bounded Retry-After handling on 429.

        FAExport's bucket is shared across all its users — a 429 here is usually
        someone else's traffic, not ours. Sleeping the server-supplied
        `Retry-After` and retrying once recovers the call instead of dropping
        the data for the cycle. Non-429 responses are returned untouched (the
        caller still owns raise_for_status for genuine 4xx/5xx).
        """
        attempt = 0
        while True:
            resp = await self._http.get(url, params=params)
            if resp.status_code != 429 or attempt >= max_retries:
                return resp
            retry_after_raw = resp.headers.get("retry-after", "")
            try:
                sleep_for = float(retry_after_raw) if retry_after_raw else 30.0
            except ValueError:
                sleep_for = 30.0
            sleep_for = min(max(sleep_for, 1.0), max_sleep)
            logger.warning(
                "FAExport 429 on %s — sleeping %.1fs then retrying (attempt %d/%d)",
                url, sleep_for, attempt + 1, max_retries,
            )
            await asyncio.sleep(sleep_for)
            attempt += 1

    async def _get_fa_http(self) -> httpx.AsyncClient:
        """Lazy-init the direct FA client with session cookies.

        Created on first use rather than in __init__ because most operations only
        need FAExport (_http). The direct FA client is only needed for cookie
        validation, so we avoid the overhead of creating it unless actually needed.
        """
        if self._fa_http is None:
            if self._proxy_url and self._proxy_key:
                # Server path: FA blocks datacenter IPs, so route the direct
                # cookie scrape through the CF Worker (whose egress FA allows).
                # The proxy transport manages cookies itself (raw string) and
                # bypasses httpx's jar — so we must NOT also pass cookies= to the
                # client, or the jar and transport both accumulate Set-Cookie and
                # corrupt the session on the second request (FA then serves a
                # degraded, stats-less page).
                from polling.cf_proxy import CloudflareProxyTransport
                fa_transport = CloudflareProxyTransport(self._proxy_url, self._proxy_key)
                fa_transport.set_cookies(f"a={self.cookie_a}; b={self.cookie_b}")
                jar_cookies = None
                logger.info("FA direct client using CF proxy: %s", self._proxy_url)
            else:
                fa_transport = httpx.AsyncHTTPTransport(retries=2)
                jar_cookies = self._fa_cookies()
            self._fa_http = httpx.AsyncClient(
                timeout=30.0,
                cookies=jar_cookies,
                follow_redirects=True,
                # Custom UA to identify our traffic to FA's servers
                headers={"User-Agent": "PawPoller/1.0"},
                transport=fa_transport,
            )
        return self._fa_http

    # ── Cookie Validation ─────────────────────────────────────

    async def validate_cookies(self) -> bool:
        """Test cookies by looking for a LOGGED-IN marker on FurAffinity.

        ⚠ The previous version could not fail. It fetched the user's gallery and
        returned::

            "<figure" in resp.text or f"gallery/{self.username}" in str(resp.url)

        A FurAffinity gallery is **public**. It serves `<figure>` thumbnails to
        anyone, logged in or not, and the second clause is true for any
        successful fetch of that URL — so the function returned True for a
        logged-OUT session. Its docstring claimed "if cookies are expired… no
        <figure> elements", which is simply not true of a public page.

        Measured on the production server against real expired cookies: gallery
        200, `<figure>` present, no logout link, page offering "log in" — and
        `validate_cookies()` returned **True**.

        The cost of that false positive was a three-layer disguise: expired
        cookies passed validation, the post then died on "Could not find form
        key on /submit/", and `requires_mode = "desktop"` re-queued it — so an
        expired login presented to the user as a platform limitation ("FA needs
        the desktop"). Nobody could see the real cause because the one check
        that existed to report it was incapable of reporting anything.

        So: ask for a page only a logged-in session gets, and look for a marker
        only a logged-in page carries. `/controls/submissions/` is behind auth,
        and a logged-in FA page always renders a logout link. Fails CLOSED —
        anything unexpected is treated as invalid, because a false negative
        costs a retry while a false positive costs a silent, misattributed
        failure.
        """
        if not self.cookie_a or not self.cookie_b or not self.username:
            return False
        try:
            client = await self._get_fa_http()
            resp = await client.get(f"{config.FA_BASE}/controls/submissions/")
            if resp.status_code != 200:
                return False
            low = resp.text.lower()
            # A logged-in FA page always carries a logout control. A logged-out
            # one never does — it offers a login form instead.
            if "/logout" in low or "sign out" in low:
                return True
            logger.info("FA cookie validation: no logged-in marker on "
                        "/controls/submissions/ — session is not authenticated")
            return False
        except Exception as e:
            logger.warning("FA cookie validation failed: %s", e)
            return False

    async def validate_session(self) -> dict:
        """Who is this cookie pair actually logged in as? (3.31.0)

        ``validate_cookies`` answers "is somebody logged in", which is one
        question short. FA keeps one session per browser, so copying cookies
        while signed in as the wrong account produces a **valid** session for
        the **wrong** user — and every check in this codebase said yes to it.
        The operator pasted three sets of renewed cookies, watched them save,
        and two accounts kept failing with nothing able to say why.

        Getting it wrong is not cosmetic. The poller would file one account's
        gallery under another, and the poster would upload to whichever account
        the session belongs to rather than the one selected — which is exactly
        how a friend's account gets posted to by accident.

        Returns ``{ok, logged_in, username, expected, matches, detail}``.
        ``ok`` is the conjunction: logged in AND as the right person.
        """
        out = {"ok": False, "logged_in": False, "username": "",
               "expected": self.username or "", "matches": False, "detail": ""}
        if not self.cookie_a or not self.cookie_b or not self.username:
            out["detail"] = "No username or cookies stored for this account."
            return out
        try:
            client = await self._get_fa_http()
            resp = await client.get(f"{config.FA_BASE}/controls/submissions/")
            if resp.status_code != 200:
                out["detail"] = f"FurAffinity returned HTTP {resp.status_code}."
                return out
            body = resp.text
            if not ("/logout" in body.lower() or "sign out" in body.lower()):
                # The recurring cause, measured: FurAffinity keeps ONE session
                # per browser and rotates cookie `a` on each sign-in while
                # cookie `b` persists. Renewing several accounts in one browser
                # therefore leaves every pair except the last one holding a
                # stale `a` against the current `b` — three sets pasted, one
                # works. Naming that is the difference between fixing it and
                # pasting the same dead cookies again.
                out["detail"] = (
                    "Not logged in — this cookie pair no longer authenticates. "
                    "If you renewed several accounts from one browser, only the "
                    "last one still works: FurAffinity replaces the session each "
                    "time you sign in as someone else. Sign in as "
                    f"{self.username} in a fresh private window, copy cookies a "
                    "and b together, and save this account before signing in "
                    "as anyone else.")
                return out
            out["logged_in"] = True
            who = _logged_in_username(body)
            out["username"] = who
            if not who:
                # Logged in but the page did not carry the marker. Do not invent
                # a mismatch from a parse miss — report the session as usable
                # and say the identity is unconfirmed.
                out["ok"] = True
                out["matches"] = True
                out["detail"] = ("Logged in, but FurAffinity did not say which "
                                 "account — identity unconfirmed.")
                return out
            out["matches"] = _same_fa_user(who, self.username)
            out["ok"] = out["matches"]
            if not out["matches"]:
                out["detail"] = (
                    f"These cookies are signed in as {who}, not {self.username}. "
                    f"FurAffinity keeps one session per browser — sign in as "
                    f"{self.username} and copy cookies a and b again.")
            return out
        except Exception as e:
            logger.warning("FA session validation failed: %s", e)
            out["detail"] = f"Could not reach FurAffinity: {e}"
            return out

    # ── FAExport Gallery Listing ──────────────────────────────

    async def get_gallery_page(self, page: int = 1) -> list[dict]:
        """Fetch one page of gallery via FAExport.

        The `full=1` parameter tells FAExport to return expanded submission data
        (title, thumbnail, etc.) rather than just bare submission IDs.
        FAExport returns an empty list when the page is beyond the last page.
        """
        resp = await self._get_with_retry(
            f"{config.FAEXPORT_BASE}/user/{self.username}/gallery.json",
            params={"page": str(page), "full": "1"},
        )
        resp.raise_for_status()
        items = resp.json()
        # FAExport should return a list; if it returns something else
        # (e.g. an error object), treat it as empty.
        if not isinstance(items, list):
            return []
        return items

    async def get_all_gallery_ids(self) -> list[dict]:
        """Paginate through all gallery pages and return submission stubs.

        Walks pages sequentially until FAExport returns an empty list (indicating
        we've gone past the last page). Rate-limited between pages to be polite
        to the FAExport server.

        Returns minimal stubs {submission_id, title, thumbnail_url} -- enough for
        the caller to decide which submissions need full detail fetching.
        """
        all_subs: list[dict] = []
        page = 1
        for _page_safety in range(1000):
            items = await self.get_gallery_page(page)
            # Empty list = no more pages
            if not items:
                break
            for item in items:
                sub_id = item.get("id")
                if sub_id:
                    all_subs.append({
                        "submission_id": int(sub_id),
                        "title": item.get("title", ""),
                        "thumbnail_url": item.get("thumbnail", ""),
                    })
            page += 1
            # Rate-limit between pages to avoid overloading FAExport
            await asyncio.sleep(config.FA_REQUEST_DELAY_SECONDS)
        return all_subs

    # ── FAExport Submission Detail ────────────────────────────

    async def get_submission_detail(self, submission_id: int) -> dict:
        """Fetch full submission details from FAExport and normalize.

        FAExport returns the raw scraped data in its own JSON schema. We normalise
        it into our internal DB format via _normalize_submission() so the rest of
        the application works with a consistent structure regardless of platform.
        """
        resp = await self._get_with_retry(
            f"{config.FAEXPORT_BASE}/submission/{submission_id}.json",
        )
        resp.raise_for_status()
        raw = resp.json()
        return self._normalize_submission(raw, submission_id)

    async def get_submission_details_batch(self, submission_ids: list[int]) -> list[dict]:
        """Fetch details for multiple submissions one-by-one with rate limiting.

        Unlike the Inkbunny API which supports batch fetching (multiple IDs in one
        request), FAExport only serves one submission at a time. We therefore loop
        through IDs sequentially with a rate-limiting delay between each request.

        Individual failures are logged and skipped so one bad submission doesn't
        abort the entire batch.
        """
        details: list[dict] = []
        for i, sid in enumerate(submission_ids):
            try:
                detail = await self.get_submission_detail(sid)
                details.append(detail)
            except Exception as e:
                logger.warning("Failed to fetch FA submission %d: %s", sid, e)
            # Rate-limit between requests, but not after the final one
            if i < len(submission_ids) - 1:
                await asyncio.sleep(config.FA_REQUEST_DELAY_SECONDS)
        return details

    # ── FAExport Comments ─────────────────────────────────────

    async def get_submission_comments(self, submission_id: int) -> list[dict]:
        """Fetch comments for a submission from FAExport.

        Unlike Inkbunny (where we must scrape comments from HTML because the API
        doesn't expose comment text), FAExport provides a dedicated comments endpoint
        that returns structured JSON with full comment text, threading info, and
        timestamps. Each comment is normalised to our internal format.
        """
        resp = await self._get_with_retry(
            f"{config.FAEXPORT_BASE}/submission/{submission_id}/comments.json",
        )
        resp.raise_for_status()
        raw_comments = resp.json()
        # Guard against unexpected response shapes (e.g. error objects)
        if not isinstance(raw_comments, list):
            return []
        return [self._normalize_comment(c, submission_id) for c in raw_comments]

    # ── Profile Sniff (Spam Detection) ──────────────────────

    async def get_user_profile(self, username: str) -> dict | None:
        """Fetch a user's profile summary from FAExport.

        Returns a dict with profile data (name, profile, submissions count, etc.)
        or None if the user doesn't exist or the request fails. Used to sniff
        new watchers for bot characteristics (zero submissions, zero favorites).
        """
        try:
            resp = await self._get_with_retry(
                f"{config.FAEXPORT_BASE}/user/{username}.json",
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.debug("Failed to fetch profile for %s: %s", username, e)
            return None

    async def sniff_watcher_profiles(self, usernames: list[str]) -> dict[str, bool]:
        """Check a batch of watcher usernames for bot characteristics.

        For each username, fetches their FAExport profile and checks:
        - Zero submissions + zero favorites + zero watches = likely bot
        - Profile doesn't exist (banned already) = definitely bot

        Returns a dict of {username: is_spam} for each checked user.
        Rate-limited to avoid hammering FAExport.
        """
        results: dict[str, bool] = {}
        for username in usernames:
            await asyncio.sleep(config.FA_REQUEST_DELAY_SECONDS)
            profile = await self.get_user_profile(username)
            if profile is None:
                # Profile doesn't exist = already banned = spam
                results[username] = True
                continue
            # Check activity indicators
            stats = profile.get("stats", {})
            submissions = _safe_int(stats.get("submissions", 0))
            favorites = _safe_int(stats.get("favorites", 0))
            watches = _safe_int(stats.get("watches", 0))
            # Zero activity across the board = almost certainly a bot
            if submissions == 0 and favorites == 0 and watches == 0:
                results[username] = True
            else:
                results[username] = False
            logger.debug("Profile sniff %s: subs=%d fav=%d watches=%d -> spam=%s",
                         username, submissions, favorites, watches, results[username])
        return results

    # ── Watcher Tracking ──────────────────────────────────────

    async def get_watchers_page(self, page: int = 1) -> list[str]:
        """Fetch one page of watcher usernames via FAExport.

        FAExport returns a plain JSON array of username strings for the watchers
        endpoint. Returns an empty list when the page is beyond the last page.
        """
        resp = await self._get_with_retry(
            f"{config.FAEXPORT_BASE}/user/{self.username}/watchers.json",
            params={"page": str(page)},
        )
        resp.raise_for_status()
        items = resp.json()
        # FAExport should return a list; if it returns something else
        # (e.g. an error object), treat it as empty.
        if not isinstance(items, list):
            return []
        return items

    async def get_all_watchers(self) -> list[str]:
        """Paginate through all watcher pages and return the complete list.

        Walks pages sequentially until FAExport returns an empty list or
        repeats the previous page (FAExport returns the last page's data
        indefinitely instead of an empty list for some accounts).
        Rate-limited between pages to be polite to the FAExport server.

        Returns a deduplicated list of all watcher usernames.
        """
        all_watchers: list[str] = []
        seen: set[str] = set()
        page = 1
        for _page_safety in range(1000):
            items = await self.get_watchers_page(page)
            # Empty list = no more pages
            if not items:
                break
            # FAExport repeats the last page forever instead of returning
            # empty — stop when we see no new usernames
            new_items = [u for u in items if u not in seen]
            if not new_items:
                break
            seen.update(new_items)
            all_watchers.extend(new_items)
            page += 1
            # Rate-limit between pages to avoid overloading FAExport
            await asyncio.sleep(config.FA_REQUEST_DELAY_SECONDS)
        logger.info("Fetched %d total watchers for %s", len(all_watchers), self.username)
        return all_watchers

    # ── Normalization ─────────────────────────────────────────
    #
    # FAExport's JSON schema doesn't match our internal DB format. These methods
    # translate FAExport field names/types into the consistent structure used by
    # the rest of the application (same shape as Inkbunny's to_db_dict output).
    #

    # ── Direct FA scraping (FAExport fallback) ────────────────
    # When FAExport (the third-party proxy) is unavailable — e.g. the
    # long-running Cloudflare block on faexport.spangle.org.uk
    # (Deer-Spangle/faexport#129) — these scrape FurAffinity's own HTML
    # directly using the user's session cookies, mirroring what FAExport does
    # server-side. CONSTRAINT: FA's Cloudflare blocks datacenter IPs (the same
    # reason FA *posting* requires desktop), so direct polling only works from a
    # residential IP (the desktop instance), not the GCP server. Output dicts
    # match _normalize_submission so the poller is agnostic to the source.

    _GALLERY_SID_RE = re.compile(r'id="sid-(\d+)"')

    async def get_all_gallery_ids_direct(self, max_pages: int = 50) -> list[dict]:
        """Scrape the user's gallery submission IDs directly from FA.

        Paginates /gallery/{user}/{page}/ until a page yields no new IDs.
        Requires valid cookies (the poller validates them first).
        """
        client = await self._get_fa_http()
        ids: list[int] = []
        seen: set[int] = set()
        page = 1
        while page <= max_pages:
            resp = await client.get(f"{config.FA_BASE}/gallery/{self.username}/{page}/")
            if resp.status_code != 200:
                break
            page_ids = [int(m) for m in self._GALLERY_SID_RE.findall(resp.text)]
            new_ids = [i for i in page_ids if i not in seen]
            if not new_ids:
                break
            for i in new_ids:
                seen.add(i)
                ids.append(i)
            page += 1
            await asyncio.sleep(config.FA_REQUEST_DELAY_SECONDS)
        logger.info("FA direct: scraped %d gallery submission ids across %d page(s)",
                    len(ids), page - 1)
        return [{"submission_id": i} for i in ids]

    async def get_submission_detail_direct(self, submission_id: int) -> dict:
        """Scrape one submission page directly from FA into our DB dict format."""
        client = await self._get_fa_http()
        resp = await client.get(f"{config.FA_BASE}/view/{submission_id}/")
        resp.raise_for_status()
        return self._parse_submission_html(resp.text, submission_id, self.username)

    async def get_submission_details_batch_direct(self, submission_ids: list[int]) -> list[dict]:
        """Scrape multiple submission pages directly, paced by the FA rate-limit delay."""
        out: list[dict] = []
        for sid in submission_ids:
            try:
                out.append(await self.get_submission_detail_direct(sid))
            except Exception as e:  # noqa: BLE001 — one bad page must not abort the cycle
                logger.warning("FA direct scrape failed for submission %s: %s", sid, e)
            await asyncio.sleep(config.FA_REQUEST_DELAY_SECONDS)
        return out

    @staticmethod
    def _parse_submission_html(html: str, submission_id: int, username: str) -> dict:
        """Parse a FurAffinity (Beta) submission page into our DB submission dict.

        Best-effort regex scraping — extracts the stats that drive the snapshot
        time-series (views/favourites/comments) plus title/rating/tags/thumbnail.
        Fields FA doesn't surface cheaply (category/species/gender/description)
        are left blank; the poller only requires the id + stats.
        """
        # FA's current stats live in <div class="submission-page-stats"> as
        #   <div title="Views"><div>72</div>...  (Comments likewise)
        # and the Favorites count is wrapped in a /favslist link:
        #   <div title="Favorites"><div><a href="/favslist/{id}/">1</a></div>
        def _stat(title: str) -> int:
            # ReDoS-safe WITHOUT bounding the whitespace (FA indents deeply — the
            # gap between the outer and inner <div> exceeds 30 chars, so a {0,30}
            # bound matched nothing). The original blowup came from two \s* runs
            # straddling the optional <a> group; moving the second \s* INSIDE that
            # group leaves each remaining \s* separated by a literal, so there is
            # no overlapping-quantifier ambiguity to backtrack over.
            m = re.search(
                rf'<div title="{title}">\s*<div>\s*(?:<a[^>]*>\s*)?([\d,]+)', html
            )
            return _safe_int(m.group(1)) if m else 0

        views = _stat("Views")
        favorites = _stat("Favorites")
        comments = _stat("Comments")

        # Title: <div class="submission-title"><h2>Title</h2>...  (fall back to <title>).
        tm = re.search(
            r'class="submission-title"[^>]*>\s*<h2>\s*(?:<p>\s*)?(.*?)\s*(?:</p>)?\s*</h2>',
            html, re.S,
        ) or re.search(r'<title>(.*?)\s+by\s+.*?Fur Affinity', html, re.S)
        title = _strip_tags(tm.group(1)).strip() if tm else ""

        # Rating: cheap via the twitter:label2/data2 meta pair, else the
        # "<X> rating" title attribute on the rating control.
        rm = re.search(
            r'name="twitter:label2"\s+content="Rating"\s*/?>\s*'
            r'<meta\s+name="twitter:data2"\s+content="([^"]+)"',
            html,
        ) or re.search(r'\btitle="([A-Za-z]+) rating"', html)
        rating = rm.group(1).strip() if rm else ""

        # Posted date: the popup_date span's human-readable title attribute.
        pm = re.search(r'class="popup_date"[^>]*\btitle="([^"]+)"', html)
        posted_at = pm.group(1).strip() if pm else ""

        # Thumbnail / main image.
        im = re.search(r'id="submissionImg"[^>]*\bsrc="([^"]+)"', html) \
            or re.search(r'id="submissionImg"[^>]*\bdata-fullview-src="([^"]+)"', html)
        thumb = im.group(1).strip() if im else ""
        if thumb.startswith("//"):
            thumb = "https:" + thumb

        # Tags: FA renders each tag as a data-tag-name="..." attribute.
        tags = [t for t in re.findall(r'data-tag-name="([^"]+)"', html) if t]

        return {
            "submission_id": submission_id,
            "title": title,
            "username": username,
            "posted_at": posted_at,
            "category": "", "theme": "", "species": "", "gender": "",
            "rating": rating,
            "thumbnail_url": thumb,
            "download_url": thumb,
            "description": "",
            "keywords": tags,
            "link": f"https://www.furaffinity.net/view/{submission_id}/",
            "views": views,
            "favorites_count": favorites,
            "comments_count": comments,
        }

    @staticmethod
    def _normalize_submission(raw: dict, submission_id: int) -> dict:
        """Normalize FAExport submission JSON to our DB dict format.

        Handles several inconsistencies in FAExport's response:
        - Tags may be under "tags" or "keywords" (varies by FAExport version)
        - Tags may be a list of strings OR a single comma-separated string
        - Numeric stats (views, favorites) may be strings or ints
        - Some metadata lives in a nested "info" dict, some at top level
        - The "comments" field can be either a count (int/str) or a full list
          of comment objects -- we just need the count here
        """
        # Tags: FAExport returns these under different keys depending on endpoint.
        # May be a list ["tag1", "tag2"] or a comma-separated string "tag1, tag2".
        tags = raw.get("tags") or raw.get("keywords") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]

        # Numeric stats need _safe_int because FAExport may return them as
        # strings (e.g. "1,234"), ints, or None depending on the submission.
        views = _safe_int(raw.get("views", 0))
        favorites = _safe_int(raw.get("favorites", 0))

        # The "comments" field is polymorphic in FAExport:
        #   - On the submission detail endpoint: usually an integer count
        #   - On the full endpoint with comments included: a list of comment objects
        # We need just the count, so if it's a list, we take its length.
        comments_raw = raw.get("comments", 0)
        if isinstance(comments_raw, list):
            comments_count = len(comments_raw)
        else:
            comments_count = _safe_int(comments_raw)

        # FAExport nests some metadata (category, species, etc.) under an "info" dict
        # on certain endpoints, but puts them at the top level on others.
        # We check "info" first, then fall back to top-level keys.
        info = raw.get("info", {}) if isinstance(raw.get("info"), dict) else {}

        return {
            "submission_id": submission_id,
            "title": raw.get("title", ""),
            # Author name: FAExport uses "name" or "profile_name" inconsistently
            "username": raw.get("name", raw.get("profile_name", "")),
            # Posted date: may be "posted_at" or just "posted"
            "posted_at": raw.get("posted_at", raw.get("posted", "")),
            # Metadata with info-dict fallback
            "category": info.get("category", raw.get("category", "")),
            "theme": info.get("theme", raw.get("theme", "")),
            "species": info.get("species", raw.get("species", "")),
            "gender": info.get("gender", raw.get("gender", "")),
            "rating": raw.get("rating", ""),
            "thumbnail_url": raw.get("thumbnail", ""),
            "download_url": raw.get("download", ""),
            "description": raw.get("description", ""),
            "keywords": tags,
            # Construct canonical FA URL as fallback if "link" is missing
            "link": raw.get("link", f"https://www.furaffinity.net/view/{submission_id}/"),
            "views": views,
            "favorites_count": favorites,
            "comments_count": comments_count,
        }

    @staticmethod
    def _normalize_comment(raw: dict, submission_id: int) -> dict:
        """Normalize FAExport comment JSON to our DB comment dict format.

        FAExport provides structured comment data including threading info
        (reply_to parent ID and reply_level nesting depth) and deletion status.
        """
        return {
            "comment_id": str(raw.get("id", "")),
            "submission_id": submission_id,
            # Author: same inconsistent naming as submissions
            "username": raw.get("name", raw.get("profile_name", "")),
            "comment_text": raw.get("text", ""),
            # Timestamp: same dual-key pattern as submissions
            "commented_at": raw.get("posted_at", raw.get("posted", "")),
            # reply_to: parent comment ID (None for top-level comments)
            "reply_to": str(raw["reply_to"]) if raw.get("reply_to") else None,
            # reply_level: nesting depth (0 = top-level, 1 = direct reply, etc.)
            "reply_level": _safe_int(raw.get("reply_level", 0)),
            # FAExport includes a flag for comments that were deleted by the author/mod
            "is_deleted": raw.get("is_deleted", False),
        }


    # ── Posting / Upload ────────────────────────────────────────

    async def submit_story(
        self,
        file_path: str,
        *,
        title: str = "",
        description: str = "",
        keywords: str = "",
        rating: str = "1",
        cat: str = "13",
        atype: str = "1",
        species: str = "1",
        gender: str = "0",
        scrap: bool = False,
        thumbnail_path: str | None = None,
        submission_type: str = "story",
    ) -> dict:
        """Upload a submission to FurAffinity.

        Three-step form scraping flow (same as PostyBirb):
          1. GET /submit/ → scrape hidden 'key' input
          2. POST /submit/upload → multipart with key + file + submission_type
          3. POST /submit/finalize → urlencoded with new key + all metadata

        submission_type selects FA's upload kind: "story" (text) or
        "submission" (visual art). The finalize metadata is identical; only the
        category (``cat``) differs by kind, set by the caller.

        Args:
            file_path: Path to PDF/TXT/DOC file.
            title: Title (max 60 chars).
            description: BBCode description.
            keywords: Space-separated tags (underscores for multi-word).
            rating: "0"=General, "2"=Mature, "1"=Adult.
            cat: Category ("13"=Story).
            atype: Theme ("1"=All).
            species: Species code ("1"=Unspecified).
            gender: Gender code ("0"=Any).
            scrap: Post to scraps if True.
            thumbnail_path: Optional cover image path.

        Returns:
            Dict with 'submission_id' and 'url'.
        """
        client = await self._get_fa_http()

        # Step 1: GET /submit/ and scrape the key
        resp = await client.get(f"{config.FA_BASE}/submit/")
        if resp.status_code != 200:
            raise RuntimeError(f"FA: GET /submit/ failed — status {resp.status_code}")

        # Extract the key from the upload form specifically (not the logout form)
        upload_form = re.search(
            r'<form[^>]*action="/submit/upload/"[^>]*>(.*?)</form>', resp.text, re.DOTALL
        )
        if upload_form:
            key_match = re.search(r'name="key"\s*value="([^"]+)"', upload_form.group(1))
        else:
            # Fallback: try id="myform"
            myform = re.search(r'id="myform"(.*?)</form>', resp.text, re.DOTALL)
            key_match = re.search(r'name="key"\s*value="([^"]+)"', myform.group(1)) if myform else None

        if not key_match:
            low = resp.text.lower()
            if "captcha" in low:
                raise RuntimeError("FA: CAPTCHA required — account needs 11+ posts")
            # ⚠ Name the most likely cause instead of describing the symptom.
            # The submit page renders for anyone; logged OUT it simply has no
            # form key. Reporting "could not find form key" sent a real expired
            # session off to be diagnosed as a scraping or platform problem —
            # and because the old `validate_cookies` could not fail, nothing
            # upstream had contradicted it.
            if "/logout" not in low and "sign out" not in low:
                raise RuntimeError(
                    "FA: not logged in — the session cookies (a/b) are expired "
                    "or invalid. Re-copy them from a signed-in browser.")
            raise RuntimeError("FA: Could not find form key on /submit/")
        key1 = key_match.group(1)
        logger.info("FA: Got upload form key")

        # Step 2: POST /submit/upload with file
        with open(file_path, "rb") as f:
            file_data = f.read()
        filename = os.path.basename(file_path)

        upload_files = {"submission": (filename, file_data)}
        if thumbnail_path and os.path.isfile(thumbnail_path):
            with open(thumbnail_path, "rb") as tf:
                upload_files["thumbnail"] = (os.path.basename(thumbnail_path), tf.read())

        upload_data = {
            "key": key1,
            "submission_type": submission_type,
        }

        resp = await client.post(
            f"{config.FA_BASE}/submit/upload/",
            data=upload_data,
            files=upload_files,
            headers={"Referer": f"{config.FA_BASE}/submit/"},
            timeout=120.0,
        )

        # Scrape the new key from the finalize form
        # Look for the form that posts to /submit/finalize/
        finalize_form = re.search(
            r'<form[^>]*action="/submit/finalize/"[^>]*>(.*?)</form>', resp.text, re.DOTALL
        )
        if finalize_form:
            key2_match = re.search(r'name="key"\s*value="([^"]+)"', finalize_form.group(1))
        else:
            # Fallback: last key on the page (skip the logout form key)
            all_keys = re.findall(r'name="key"\s*value="([^"]+)"', resp.text)
            key2_match = None
            if all_keys:
                # The finalize key is typically the last one on the page
                class _M:
                    def group(self, n): return all_keys[-1]
                key2_match = _M()
        if not key2_match:
            errors = re.findall(r'(?:error|Error)[^>]*>([^<]+)', resp.text)
            raise RuntimeError(f"FA: Could not find finalize key — upload may have failed. Errors: {errors[:2]}")
        key2 = key2_match.group(1)
        logger.info("FA: File uploaded, got finalize key")

        # Step 3: POST /submit/finalize with metadata
        finalize_data = {
            "key": key2,
            "title": title[:60],
            "message": description,
            "keywords": keywords,
            "rating": rating,
            "cat": cat,
            "atype": atype,
            "species": species,
            "gender": gender,
        }
        if scrap:
            finalize_data["scrap"] = "1"

        resp = await client.post(
            f"{config.FA_BASE}/submit/finalize/",
            data=finalize_data,
            headers={
                "Referer": f"{config.FA_BASE}/submit/upload/",
            },
            timeout=30.0,
        )

        final_url = str(resp.url)
        if "upload-successful" not in final_url and "/view/" not in final_url:
            raise RuntimeError(f"FA: Finalize may have failed — final URL: {final_url}")

        # Extract submission ID from URL
        clean_url = final_url.split("?")[0]
        sid_match = re.search(r'/view/(\d+)', clean_url)
        submission_id = sid_match.group(1) if sid_match else ""

        logger.info("FA: Story submitted — %s (id=%s)", clean_url, submission_id)
        return {"submission_id": submission_id, "url": clean_url}

    async def submit_visual(
        self,
        file_path: str,
        *,
        title: str = "",
        description: str = "",
        keywords: str = "",
        rating: str = "1",
        cat: str = "1",
        atype: str = "1",
        species: str = "1",
        gender: str = "0",
        scrap: bool = False,
        thumbnail_path: str | None = None,
    ) -> dict:
        """Upload a visual-art submission (image) to FurAffinity.

        Same 3-step form flow as submit_story, but submission_type='submission'
        so FA treats the file as artwork rather than a story. ``cat`` defaults to
        "1" (All); callers pass a visual category from settings. The thumbnail
        is unused for image submissions (FA derives the preview from the image).

        Returns a dict with 'submission_id' and 'url'.
        """
        return await self.submit_story(
            file_path,
            title=title,
            description=description,
            keywords=keywords,
            rating=rating,
            cat=cat,
            atype=atype,
            species=species,
            gender=gender,
            scrap=scrap,
            thumbnail_path=thumbnail_path,
            submission_type="submission",
        )

    async def edit_submission(
        self,
        submission_id: str,
        *,
        title: str = "",
        description: str = "",
        keywords: str = "",
        rating: str | None = None,
        scrap: bool | None = None,
    ) -> dict:
        """Edit an existing FurAffinity submission.

        Scrapes the edit form at /controls/submissions/changeinfo/{id}/
        to get the key and existing field values, merges in the caller's changes,
        and posts the complete form back. This avoids blanking fields that the
        caller didn't provide.

        FA has separate edit pages:
          changeinfo/{id}/   — title, description, keywords, rating, category
          changethumbnail/   — thumbnail image
          changesubmission/  — replace the source file
          changestory/       — story text content

        scrap: None preserves current state (read from form, re-emitted as-is),
        True forces the submission into scraps, False moves it to the main
        gallery. HTML semantics: a present "scrap=1" field keeps/sets scrap;
        omitting the field clears it (standard unchecked-checkbox behaviour).
        """
        client = await self._get_fa_http()
        edit_url = f"{config.FA_BASE}/controls/submissions/changeinfo/{submission_id}/"

        # GET the edit page
        resp = await client.get(edit_url)
        if resp.status_code != 200:
            raise RuntimeError(f"FA: GET edit page failed — status {resp.status_code}")
        page = resp.text

        # Extract the changeinfo form and its key (not the logout form key)
        changeinfo_form = re.search(
            r'<form[^>]*action="/controls/submissions/changeinfo/[^"]*"[^>]*>(.*?)</form>',
            page, re.DOTALL,
        )
        if not changeinfo_form:
            raise RuntimeError("FA: Could not find changeinfo form on edit page")
        form_html = changeinfo_form.group(1)

        key_match = re.search(r'name="key"\s*value="([^"]+)"', form_html)
        if not key_match:
            raise RuntimeError("FA: Could not find key in changeinfo form")
        key = key_match.group(1)

        # Scrape existing values from the form to preserve fields the caller didn't provide
        def _scrape_input(name: str) -> str:
            m = re.search(rf'name="{name}"[^>]*value="([^"]*)"', form_html)
            return m.group(1) if m else ""

        def _scrape_textarea(name: str) -> str:
            m = re.search(rf'name="{name}"[^>]*>(.*?)</textarea>', form_html, re.DOTALL)
            return m.group(1).strip() if m else ""

        def _scrape_select(name: str) -> str:
            m = re.search(rf'name="{name}".*?<option[^>]*selected[^>]*value="([^"]*)"', form_html, re.DOTALL)
            return m.group(1) if m else ""

        def _scrape_checkbox(name: str) -> bool:
            m = re.search(rf'<input[^>]*name="{name}"[^>]*>', form_html)
            return bool(m and re.search(r'\bchecked\b', m.group(0)))

        # Build complete form data: current values as base, overlay caller's changes
        form_data: dict[str, str] = {
            "key": key,
            "update": "yes",
            "title": title[:60] if title else _scrape_input("title"),
            "message": description if description else _scrape_textarea("message"),
            "keywords": keywords if keywords else _scrape_textarea("keywords"),
            "rating": rating if rating is not None else _scrape_select("rating"),
            "cat": _scrape_input("cat") or "13",
            "atype": _scrape_select("atype") or "1",
            "species": _scrape_select("species") or "1",
        }

        # Scrap checkbox: preserve unless caller forces a state. Without this,
        # any metadata edit on a scrapped submission would silently un-scrap it
        # (the omitted field clears the box).
        keep_scrap = _scrape_checkbox("scrap") if scrap is None else scrap
        if keep_scrap:
            form_data["scrap"] = "1"

        resp = await client.post(
            edit_url,
            data=form_data,
            headers={"Referer": edit_url},
            timeout=30.0,
        )

        # Check for success
        final_url = str(resp.url)
        if resp.status_code >= 400:
            raise RuntimeError(f"FA: Edit POST failed — status {resp.status_code}")

        url = f"{config.FA_BASE}/view/{submission_id}/"
        logger.info("FA: Edited submission %s — title=%r", submission_id, title[:40] if title else "(unchanged)")
        return {"submission_id": submission_id, "url": url}

    async def probe_scrap_state(self, submission_id: str) -> bool:
        """Return True if the FA submission is currently in scraps.

        FAExport doesn't expose scrap state (it's a list-visibility property
        rather than a submission-page badge), so we read it from the
        changeinfo form HTML — the same page edit_submission already scrapes.
        Used as the closest FA-side equivalent of "is this a draft?".
        """
        client = await self._get_fa_http()
        edit_url = f"{config.FA_BASE}/controls/submissions/changeinfo/{submission_id}/"
        resp = await client.get(edit_url)
        if resp.status_code != 200:
            raise RuntimeError(f"FA: probe_scrap_state GET failed — status {resp.status_code}")
        form_match = re.search(
            r'<form[^>]*action="/controls/submissions/changeinfo/[^"]*"[^>]*>(.*?)</form>',
            resp.text, re.DOTALL,
        )
        if not form_match:
            raise RuntimeError("FA: probe_scrap_state could not locate changeinfo form")
        form_html = form_match.group(1)
        m = re.search(r'<input[^>]*name="scrap"[^>]*>', form_html)
        return bool(m and re.search(r'\bchecked\b', m.group(0)))


def _strip_tags(text: str) -> str:
    """Remove any HTML tags and unescape basic entities from a scraped fragment."""
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", "", text or ""))


def _safe_int(val: Any) -> int:
    """Safely convert a value to int, handling None, comma-formatted strings, and type errors.

    FA and FAExport return numeric values in inconsistent formats:
      - Integers: 42
      - Plain strings: "42"
      - Comma-formatted strings: "1,234" (common for view/fav counts on FA pages)
      - None: when the field is missing entirely

    This helper normalises all of these to a plain int, returning 0 on any failure.
    """
    if val is None:
        return 0
    try:
        # Strip commas from formatted numbers like "1,234" before int conversion
        if isinstance(val, str):
            val = val.replace(",", "").strip()
        return int(val)
    except (ValueError, TypeError):
        return 0
