"""Archive of Our Own (AO3) HTTP client.

AO3 runs the OTW Archive software (same as SquidgeWorld). Authentication
is via standard Rails form login with CSRF token. Data is collected by
scraping the web UI since there is no public API.

Key details:
  - Work IDs are integers (e.g. 12345678)
  - Stats: hits, kudos, comments, bookmarks
  - Auth: username/password login (separate from the user being tracked)
  - AO3 uses Cloudflare; realistic headers and respectful rate limiting required
"""

from __future__ import annotations
import asyncio
import logging
import random
import re
import time
from html import unescape

import httpx

import config

logger = logging.getLogger(__name__)

_BASE = "https://archiveofourown.org"


# Module-level backoff cache (2.22.6).
# When a request gets 429 with Retry-After: N, we record `time.time() + N`
# here so the poll orchestrator can skip a cycle entirely instead of
# enqueuing requests that will inevitably wait + re-429. This prevents
# the punishment window from escalating further because of our own
# retries, which is the failure mode we observed across 2.22.2-2.22.5.
# Read by `get_backoff_until_ts()`; written by `_get_page` on 429.
_ao3_backoff_until_ts: float = 0.0


def get_backoff_until_ts() -> float:
    """Return the unix timestamp until which AO3 is throttling us, or 0.0
    if no throttle is currently observed. Polled by the orchestrator
    before scheduling an AO3 cycle.
    """
    return _ao3_backoff_until_ts


def _record_throttle(wait_seconds: int) -> None:
    """Record an observed AO3 throttle window. Called from both _get_page
    and _post_with_retry on 429. Takes the max of any existing window and
    the new one so concurrent observers don't shorten the cooldown.
    """
    global _ao3_backoff_until_ts
    new_until = time.time() + wait_seconds
    if new_until > _ao3_backoff_until_ts:
        _ao3_backoff_until_ts = new_until


class AO3ThrottledError(RuntimeError):
    """Raised when AO3 has us in an active rate-limit window.

    Investigation of the OTW Archive source (otwarchive v0.9.475.3,
    config/initializers/rack_attack.rb) confirmed:
      - Single per-IP bucket: 300 requests / 300 seconds for everything
      - Fixed window (not sliding), so Retry-After reports time until the
        current 300s window rolls over
      - Requests INSIDE the window count toward the NEXT window's quota
    The third point is why in-method retry-on-429 hurts us — each retry
    wakes up at window rollover and immediately starts eating the new
    window's budget. The 2.22.10 fix raises this error instead of
    sleeping-and-retrying so the caller can fail fast; the scheduler
    short-circuits subsequent attempts via the backoff cache until the
    window has fully drained.
    """
    def __init__(self, retry_after: int, url: str):
        self.retry_after = retry_after
        self.url = url
        super().__init__(
            f"AO3 throttled on {url}, window expires in {retry_after}s"
        )

# Realistic browser headers — Chrome 131 on Windows 10. Full Sec-Fetch +
# Sec-Ch-Ua set is required to get past AO3's "Shields are up!" page when
# hitting from residential IPs. Without these AO3 flags the client as bot
# traffic and returns 403 on /users/login even though the same IP works
# fine in a real browser.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": (
        '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'
    ),
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Priority": "u=0, i",
}


class AO3Client:
    """Async HTTP client for Archive of Our Own (OTW Archive)."""

    def __init__(self, username: str, password: str, target_user: str,
                 proxy_url: str = "", proxy_key: str = "",
                 session_cookie: str = ""):
        self.username = username
        self.password = password
        self.target_user = target_user
        self._session_cookie = session_cookie.strip()
        # Set when login fails due to an AO3-side *block* (shields up / rate-limit)
        # rather than bad credentials — lets validate_session raise so the session
        # check reports amber "temporarily blocked", not red "expired".
        self.blocked_reason = ""

        # Use Cloudflare Worker proxy if configured — bypasses AO3's
        # "Shields are up!" Cloudflare TLS fingerprint check on
        # residential IPs. The Worker runs on CF's own edge so it
        # won't challenge itself. Same pattern as clients.sf.
        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("AO3 client using CF proxy: %s", proxy_url)
        else:
            transport = httpx.AsyncHTTPTransport(retries=2)

        self._http = httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            headers=_HEADERS,
            transport=transport,
        )
        # 2.22.10: wrap _http.get/post so EVERY request gets pre-flight
        # throttle check + 429 recording, regardless of which method in
        # the client called it. Without this wrap, raw self._http.get(...)
        # calls (the chapter form load in create_chapter, edit-page
        # fetches, work-deletion confirm pages, etc.) would dodge the
        # checks built into _get_page and _post_with_retry.
        _orig_get = self._http.get
        _orig_post = self._http.post

        async def _wrapped_get(url, **kw):
            if _ao3_backoff_until_ts > time.time():
                remaining = int(_ao3_backoff_until_ts - time.time())
                logger.warning(
                    "AO3: short-circuit GET %s — %ds remain in throttle window",
                    url, remaining,
                )
                return httpx.Response(
                    429, headers={"Retry-After": str(remaining)},
                )
            resp = await _orig_get(url, **kw)
            if resp.status_code == 429:
                wait = self._parse_retry_after(resp, default=300)
                _record_throttle(wait)
            return resp

        async def _wrapped_post(url, **kw):
            if _ao3_backoff_until_ts > time.time():
                remaining = int(_ao3_backoff_until_ts - time.time())
                logger.warning(
                    "AO3: short-circuit POST %s — %ds remain in throttle window",
                    url, remaining,
                )
                return httpx.Response(
                    429, headers={"Retry-After": str(remaining)},
                )
            resp = await _orig_post(url, **kw)
            if resp.status_code == 429:
                wait = self._parse_retry_after(resp, default=300)
                _record_throttle(wait)
            return resp

        self._http.get = _wrapped_get
        self._http.post = _wrapped_post

        self._logged_in = False
        self._pseud_id: str | None = None  # cached after first form fetch

        # Cookie-based auth: when the user pastes their own browser's
        # `_otwarchive_session` cookie we skip the username/password login
        # entirely. AO3's per-IP login throttle (5–10 min cooldown after
        # one bad attempt) makes cold-login from datacenter IPs unreliable;
        # the cookie path bypasses the rate-limited login endpoint and
        # uses the user's already-warm browser session instead. The cookie
        # is long-lived (~1 year on AO3) and rotates only on logout.
        if self._session_cookie:
            self._http.cookies.set(
                "_otwarchive_session",
                self._session_cookie,
                domain="archiveofourown.org",
                path="/",
            )
            self._logged_in = True
            logger.info("AO3 client using pasted session cookie (skipping form login)")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    def update_credentials(self, username: str, password: str, target_user: str,
                           session_cookie: str = "") -> None:
        cookie = (session_cookie or "").strip()
        if username != self.username or password != self.password:
            # Only flip logged_in off when we don't have a fresh cookie
            # to lean on; otherwise the cookie keeps the session alive.
            if not cookie:
                self._logged_in = False
        self.username = username
        self.password = password
        self.target_user = target_user

        if cookie and cookie != self._session_cookie:
            self._session_cookie = cookie
            self._http.cookies.set(
                "_otwarchive_session",
                cookie,
                domain="archiveofourown.org",
                path="/",
            )
            self._logged_in = True
            logger.info("AO3 client: session cookie updated from settings")
        elif not cookie and self._session_cookie:
            # Cookie cleared from settings — drop it and fall back to login.
            self._session_cookie = ""
            try:
                self._http.cookies.delete(
                    "_otwarchive_session", domain="archiveofourown.org", path="/"
                )
            except Exception:
                pass
            self._logged_in = False

    async def close(self) -> None:
        await self._http.aclose()

    # ── Page Fetching ────────────────────────────────────────────

    async def _get_page(self, url: str, *, max_attempts: int = 3) -> str | None:
        """Fetch a page, handling Cloudflare errors and timeouts gracefully.

        AO3 from datacenter IPs sees intermittent ReadTimeouts (~1 in 5
        requests). The transport-level retries=2 in __init__ only helps with
        connect failures, not read timeouts after the headers arrive. We
        retry the whole GET up to max_attempts times with a brief pause.

        429 handling (2.22.10): no in-method retry. AO3's per-IP bucket is
        300 req / 300s, fixed window — retries inside the window count
        against the NEXT window's quota and just keep us throttled. On 429
        we record the throttle, log, and return None. The backoff cache
        gates future requests until the window expires.
        """
        # Pre-flight: if we know we're throttled, don't even attempt the
        # request. Saves a wasted round-trip that would just count toward
        # the next window's quota.
        if _ao3_backoff_until_ts > time.time():
            remaining = int(_ao3_backoff_until_ts - time.time())
            logger.warning(
                "AO3: skipping GET %s — %ds remain in observed throttle window",
                url, remaining,
            )
            return None

        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await self._http.get(url)
                if resp.status_code == 403:
                    if "Shields are up" in resp.text:
                        logger.error("AO3: 'Shields are up!' page returned for %s", url)
                    else:
                        logger.error("AO3: 403 Forbidden for %s", url)
                    return None
                if resp.status_code == 429:
                    wait = self._parse_retry_after(resp, default=300)
                    logger.warning(
                        "AO3: 429 rate limited on %s — recording throttle "
                        "window (%ds remaining) and aborting; queue retry "
                        "will run after window expires",
                        url, wait,
                    )
                    _record_throttle(wait)
                    return None
                if resp.status_code == 525:
                    logger.warning("AO3: 525 SSL handshake from origin (attempt %d/%d)", attempt, max_attempts)
                    last_exc = RuntimeError("525 origin SSL")
                    await asyncio.sleep(2 * attempt)
                    continue
                resp.raise_for_status()
                return resp.text
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
                last_exc = e
                logger.warning(
                    "AO3: timeout fetching %s (attempt %d/%d): %s",
                    url, attempt, max_attempts, type(e).__name__,
                )
                await asyncio.sleep(2 * attempt)
                continue
            except httpx.HTTPError as e:
                last_exc = e
                logger.error("AO3: HTTPError fetching %s: %s %r", url, type(e).__name__, e)
                return None

        logger.error(
            "AO3: failed to fetch %s after %d attempts (last error: %s %r)",
            url, max_attempts, type(last_exc).__name__ if last_exc else "?", last_exc,
        )
        return None

    @staticmethod
    async def _polite_delay() -> None:
        """Sleep `AO3_REQUEST_DELAY_SECONDS` ± 30% jitter between requests.

        Fixed inter-request gaps look bot-like; jitter spreads the request
        timing into a more human-shaped distribution. Range is intentionally
        narrow — we want politeness, not unpredictability that could hammer
        AO3 with an unlucky 0.7×–1.3× sequence.
        """
        import config
        base = config.AO3_REQUEST_DELAY_SECONDS
        await asyncio.sleep(base * random.uniform(0.7, 1.3))

    @staticmethod
    def _parse_retry_after(resp: httpx.Response, default: int = 30) -> int:
        """Extract wait time from Retry-After header, or use default."""
        raw = resp.headers.get("Retry-After", "")
        if raw:
            try:
                return max(int(raw), 5)
            except ValueError:
                pass
        return default

    async def _post_with_retry(self, url: str, max_attempts: int = 3,
                                **kwargs) -> httpx.Response:
        """POST with throttle awareness.

        2.22.10: no in-method retry on 429. AO3's per-IP bucket is 300 req
        / 300s fixed window — sleeping the Retry-After and retrying just
        wakes up at window rollover and starts eating the next window's
        quota immediately. On 429 we record the throttle, raise
        AO3ThrottledError, and let the caller bubble up to the queue
        retry, which gates on the backoff cache.

        max_attempts is retained for non-429 transient failures (none
        currently handled — kept for signature compatibility).
        """
        # Pre-flight: short-circuit if we know we're throttled.
        if _ao3_backoff_until_ts > time.time():
            remaining = int(_ao3_backoff_until_ts - time.time())
            logger.warning(
                "AO3: skipping POST %s — %ds remain in observed throttle window",
                url, remaining,
            )
            raise AO3ThrottledError(remaining, url)

        resp = await self._http.post(url, **kwargs)
        if resp.status_code == 429:
            wait = self._parse_retry_after(resp, default=300)
            logger.warning(
                "AO3: 429 on POST %s — recording throttle window (%ds "
                "remaining) and aborting; queue retry will run after "
                "window expires",
                url, wait,
            )
            _record_throttle(wait)
            raise AO3ThrottledError(wait, url)
        return resp

    # ── Authentication ──────────────────────────────────────────

    async def login(self) -> bool:
        """Authenticate via OTW Archive Rails login form."""
        logger.info("AO3: Logging in as %s...", self.username)

        # Warmup: hit the homepage first so AO3 sees us doing a realistic
        # navigation sequence (same-site request for /users/login with the
        # Referer set) instead of a cold direct-hit on the login URL. Helps
        # get past the "Shields are up!" 403 that residential IPs hit when
        # going straight to /users/login.
        try:
            await self._http.get(_BASE + "/", headers=_HEADERS)
        except Exception as e:
            logger.debug("AO3: homepage warmup failed (non-fatal): %s", e)

        # For the login page request, include navigation-style headers
        # (Referer from homepage, Sec-Fetch-Site=same-origin).
        login_nav_headers = {
            **_HEADERS,
            "Referer": _BASE + "/",
            "Sec-Fetch-Site": "same-origin",
        }

        # Retry login page fetch up to 3 times with backoff — AO3's
        # Cloudflare layer sometimes returns transient 429/503/challenge
        # responses that clear on retry. Persistent 429 with body
        # "Retry later" is the long-term ban (5–60 min) and retrying
        # in-band makes it worse — bail out fast in that case so the
        # caller can surface a clear error.
        html = None
        last_status = 0
        rate_limited = False
        for attempt in range(3):
            if attempt > 0:
                delay = 5 * (2 ** (attempt - 1))
                logger.info("AO3: login page retry %d/2 after %ds", attempt, delay)
                await asyncio.sleep(delay)
            try:
                resp = await self._http.get(
                    _BASE + "/users/login",
                    headers=login_nav_headers,
                )
                last_status = resp.status_code
                if resp.status_code == 200:
                    html = resp.text
                    break
                if resp.status_code == 403 or "Shields are up" in (resp.text or ""):
                    logger.error(
                        "AO3: 'Shields are up!' page returned (HTTP %d) for %s/users/login",
                        resp.status_code, _BASE,
                    )
                    self.blocked_reason = (
                        "AO3 is temporarily blocking automated login (“Shields are up”) — "
                        "this is AO3-side, not your credentials. Retry later, or use cookie auth "
                        "(paste _otwarchive_session) to skip the login page.")
                    return False
                if resp.status_code == 429 and "Retry later" in (resp.text or ""):
                    # Long-term per-IP login ban — additional retries within
                    # the same call burn through the cooldown without
                    # accomplishing anything. One probe was enough.
                    rate_limited = True
                    self.blocked_reason = (
                        "AO3 login is rate-limited (HTTP 429 “Retry later”) — AO3-side, "
                        "not your credentials. Wait 5–60 min before retrying.")
                    logger.warning(
                        "AO3: login page rate-limited (HTTP 429, 'Retry later'); "
                        "stopping in-band retries — wait 5-60 min before retrying",
                    )
                    break
                logger.warning(
                    "AO3: login page returned HTTP %d (attempt %d/3), body prefix: %.200s",
                    resp.status_code, attempt + 1, (resp.text or "")[:200],
                )
            except Exception as e:
                logger.error("AO3: Login page fetch failed: %s", e, exc_info=True)
                last_status = 0

        if not html:
            if rate_limited:
                logger.error("AO3: Login blocked by rate limiter (HTTP 429 'Retry later')")
            else:
                logger.error("AO3: Failed to fetch login page after 3 attempts (last HTTP %d)", last_status)
            return False

        # Extract authenticity_token
        token_match = re.search(
            r'<input[^>]*name="authenticity_token"[^>]*value="([^"]+)"',
            html,
        )
        if not token_match:
            token_match = re.search(
                r'<input[^>]*value="([^"]+)"[^>]*name="authenticity_token"',
                html,
            )
        if not token_match:
            logger.error("AO3: Could not find authenticity_token on login page")
            return False

        token = token_match.group(1)

        login_data = {
            "authenticity_token": token,
            "user[login]": self.username,
            "user[password]": self.password,
            "user[remember_me]": "1",
            "commit": "Log In",
        }

        try:
            resp = await self._http.post(
                f"{_BASE}/users/login",
                data=login_data,
                headers={
                    **_HEADERS,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": f"{_BASE}/users/login",
                },
            )
        except httpx.HTTPError as e:
            logger.error("AO3: Login POST failed: %s", e)
            return False

        page = resp.text

        def _extract_account_name_from_url(url: str) -> str | None:
            m = re.search(r"/users/([^/?&#]+)", url)
            if m:
                candidate = m.group(1)
                if candidate not in ("login", "logout", "register", "password"):
                    return candidate
            return None

        def _extract_account_name_from_page(html: str) -> str | None:
            for m in re.finditer(r'href="/users/([^/"?&]+)"', html):
                candidate = m.group(1)
                if candidate not in ("login", "logout", "register", "password"):
                    return candidate
            return None

        if f"Hi, {self.username}" in page or "Log Out" in page or 'class="greeting"' in page:
            self._logged_in = True
            if "@" in self.username:
                new_name = (
                    _extract_account_name_from_url(str(resp.url))
                    or _extract_account_name_from_page(page)
                )
                if new_name:
                    logger.info(
                        "AO3: Resolved login %r -> account name %r",
                        self.username, new_name,
                    )
                    self.username = new_name
            logger.info("AO3: Successfully logged in as %s", self.username)
            return True

        if resp.url and "/users/" in str(resp.url):
            self._logged_in = True
            if "@" in self.username:
                new_name = _extract_account_name_from_url(str(resp.url))
                if new_name:
                    logger.info(
                        "AO3: Resolved login %r -> account name %r",
                        self.username, new_name,
                    )
                    self.username = new_name
            logger.info("AO3: Login redirect successful for %s", self.username)
            return True

        logger.error("AO3: Login appears to have failed (no logged-in indicators)")
        return False

    async def ensure_logged_in(self) -> bool:
        # Cookie-only mode: trust the pasted cookie and skip the verify
        # fetch. The verification probe (`GET /users/{name}`) is itself
        # rate-limited from datacenter IPs — when it 429s the loop
        # exhausts and the body lacks "Log Out", which would normally
        # tear down the session. With a pasted cookie we can't fall
        # back to login anyway (would re-trip the rate limiter the
        # cookie was supposed to avoid), so the verify fetch only
        # creates false negatives. Let the actual import/poll request
        # be the source of truth — if the cookie is bad, that fetch
        # will return a public/login-redirect page and the caller
        # surfaces the error.
        if self._session_cookie:
            return True

        if self._logged_in:
            html = await self._get_page(f"{_BASE}/users/{self.username}")
            if html and "Log Out" in html:
                return True
            # Conservative: only tear the session down when AO3 explicitly
            # tells us we're logged out (a fetched page that lacks the
            # "Log Out" link). If the verification fetch failed entirely
            # (timeouts, 429-exhausted retries, transient Cloudflare), the
            # session cookies are very likely still valid and forcing a
            # re-login here trips AO3's per-IP login throttle for 5–10
            # minutes — far worse than letting the actual call retry.
            if html is None:
                logger.warning(
                    "AO3: session verification fetch failed for %s; "
                    "assuming session is still valid and skipping relogin",
                    self.username,
                )
                return True
            self._logged_in = False
        return await self.login()

    async def validate_session(self) -> str | None:
        # Cookie mode: do an actual fetch of the target user's drafts
        # page (or fall back to the public profile page) so we can
        # confirm the cookie is alive. Only used by /auth/connect —
        # ensure_logged_in() trusts the cookie without checking.
        if self._session_cookie:
            html = await self._get_page(
                f"{_BASE}/users/{self.target_user or self.username}"
            )
            if html and "Log Out" in html:
                return self.target_user or self.username
            if html is None:
                # Rate-limited or transient — trust the cookie was
                # accepted on the way in (the user just pasted it).
                logger.warning(
                    "AO3: cookie validate fetch failed transiently; "
                    "accepting cookie and letting next call confirm",
                )
                return self.target_user or self.username
            logger.error(
                "AO3: pasted cookie did not produce a logged-in page. "
                "Re-copy `_otwarchive_session` from your browser.",
            )
            return None
        self.blocked_reason = ""
        if await self.ensure_logged_in():
            return self.target_user
        # An AO3-side block (shields up / rate-limit) is NOT expired creds —
        # raise so the session check marks it amber "temporarily blocked" instead
        # of red "expired — re-enter credentials".
        if self.blocked_reason:
            raise RuntimeError(self.blocked_reason)
        return None

    # ── Works Discovery ─────────────────────────────────────────

    async def get_all_work_ids(self) -> list[dict]:
        """Scrape the target user's works page to discover all work IDs."""
        if not await self.ensure_logged_in():
            raise ValueError("AO3: Not authenticated")

        all_works: list[dict] = []
        page = 1
        seen_ids: set[int] = set()

        for _page_safety in range(1000):
            url = f"{_BASE}/users/{self.target_user}/works?page={page}"
            logger.info("AO3: Fetching works page %d for %s", page, self.target_user)

            html = await self._get_page(url)
            if not html:
                logger.error("AO3: Failed to fetch works page %d", page)
                break

            # Extract only works from the main work listing, not sidebar/related works.
            # AO3 wraps the user's works in <ol class="work index group">.
            work_list_match = re.search(
                r'<ol[^>]*class="[^"]*work\s+index[^"]*"[^>]*>(.*?)</ol>',
                html, re.DOTALL,
            )
            work_section = work_list_match.group(1) if work_list_match else html
            works = re.findall(
                r'<a\s+href="/works/(\d+)"[^>]*>([^<]+)</a>',
                work_section,
            )

            if not works:
                break

            new_this_page = 0
            for work_id_str, title in works:
                work_id = int(work_id_str)
                if work_id not in seen_ids:
                    seen_ids.add(work_id)
                    all_works.append({
                        "work_id": work_id,
                        "title": unescape(title.strip()),
                    })
                    new_this_page += 1

            if new_this_page == 0:
                break

            if f'page={page + 1}' not in html and 'rel="next"' not in html:
                break

            page += 1
            await self._polite_delay()

        logger.info("AO3: Found %d works for %s", len(all_works), self.target_user)
        return all_works

    # ── Work Details ────────────────────────────────────────────

    async def get_work_detail(self, work_id: int) -> dict:
        """Fetch stats and metadata for a single work."""
        url = f"{_BASE}/works/{work_id}?view_adult=true"

        html = await self._get_page(url)
        if not html:
            # Do NOT fabricate a zero-stat record here. _get_page returns None
            # on exhausted timeouts / 403 "Shields are up" / 429 throttle / 525,
            # and the old zero dict flowed straight into upsert + snapshot,
            # clobbering the work's real (cumulative, never-decreasing) hit count
            # with 0. The next good poll then looked like a +N,000 view spike in
            # digests and milestones. Raise instead so get_work_details_batch
            # drops this work for the cycle; the next cycle re-reads the truth.
            raise RuntimeError(f"AO3: failed to fetch work {work_id} (no page returned)")

        detail: dict = {"work_id": work_id}

        # Title
        m = re.search(r'<h2\s+class="title[^"]*heading"[^>]*>(.*?)</h2>', html, re.DOTALL)
        if m:
            title_html = m.group(1)
            detail["title"] = unescape(re.sub(r'<[^>]+>', '', title_html).strip())
        else:
            detail["title"] = ""

        # Author
        m = re.search(r'<a\s+rel="author"[^>]*>([^<]+)</a>', html)
        detail["username"] = unescape(m.group(1).strip()) if m else self.target_user

        # Fandom
        m = re.search(r'class="fandom[^"]*"[^>]*>.*?<a[^>]*>([^<]+)</a>', html, re.DOTALL)
        detail["fandom"] = unescape(m.group(1).strip()) if m else ""

        # Rating
        m = re.search(r'class="rating[^"]*"[^>]*>.*?<a[^>]*>([^<]+)</a>', html, re.DOTALL)
        detail["rating"] = unescape(m.group(1).strip()) if m else ""

        # Summary
        m = re.search(
            r'class="summary[^"]*"[^>]*>.*?<blockquote[^>]*>(.*?)</blockquote>',
            html, re.DOTALL,
        )
        if m:
            summary_html = m.group(1).strip()
            detail["description"] = re.sub(r'<[^>]+>', '', summary_html).strip()
        else:
            detail["description"] = ""

        # Tags/keywords
        tags = re.findall(r'class="tag"[^>]*>([^<]+)</a>', html)
        detail["keywords"] = [unescape(t.strip()) for t in tags]

        # Stats extraction
        def _extract_stat(stat_class: str) -> int:
            pattern = rf'<dd\s+class="{stat_class}"[^>]*>\s*(\d[\d,]*)\s*</dd>'
            m = re.search(pattern, html)
            if m:
                return int(m.group(1).replace(",", ""))
            pattern2 = rf'<dd\s+class="{stat_class}"[^>]*>\s*<a[^>]*>\s*(\d[\d,]*)\s*</a>'
            m = re.search(pattern2, html)
            if m:
                return int(m.group(1).replace(",", ""))
            return 0

        detail["hits"] = _extract_stat("hits")
        detail["kudos_count"] = _extract_stat("kudos")
        detail["comments_count"] = _extract_stat("comments")
        detail["bookmarks_count"] = _extract_stat("bookmarks")

        # A real work page always renders a <dl class="stats"> block and a
        # title. A 200 response that parses to a title-less, all-zero record is
        # almost always a Cloudflare interstitial / adult-content gate / login
        # redirect rather than the work itself — treat it as a fetch failure so
        # we don't persist bogus zeros (same rationale as the _get_page None
        # branch above). A genuinely brand-new work still has a title, so this
        # won't drop real works.
        if (detail["hits"] == 0 and detail["kudos_count"] == 0
                and detail["comments_count"] == 0 and detail["bookmarks_count"] == 0
                and not detail["title"]):
            raise RuntimeError(
                f"AO3: work {work_id} parsed to all-zero stats with no title — "
                f"likely a challenge/redirect page, not the work"
            )

        # Word count and chapters
        detail["word_count"] = _extract_stat("words")
        m = re.search(r'<dd\s+class="chapters"[^>]*>(\d+)/(\d+|\?)', html)
        if m:
            detail["chapters_current"] = int(m.group(1))
            detail["chapters_total"] = m.group(2)
            detail["chapters"] = f"{m.group(1)}/{m.group(2)}"
        else:
            detail["chapters_current"] = 1
            detail["chapters_total"] = "1"
            detail["chapters"] = "1/1"

        # Dates
        m = re.search(r'class="published"[^>]*>(\d{4}-\d{2}-\d{2})</dd>', html)
        detail["posted_at"] = m.group(1) if m else None

        m = re.search(r'class="status"[^>]*>(\d{4}-\d{2}-\d{2})</dd>', html)
        detail["updated_date"] = m.group(1) if m else detail.get("posted_at")

        # Link
        detail["link"] = f"{_BASE}/works/{work_id}"

        # Map to consistent schema column names
        detail["views"] = detail["hits"]
        detail["favorites_count"] = detail["kudos_count"]

        return detail

    async def get_work_details_batch(self, work_ids: list[int]) -> list[dict]:
        details = []
        for i, work_id in enumerate(work_ids):
            if i > 0:
                await self._polite_delay()
            try:
                detail = await self.get_work_detail(work_id)
                details.append(detail)
            except Exception as e:
                logger.warning("AO3: Failed to fetch work %d: %s", work_id, e)
        return details

    # ── Kudos Users ─────────────────────────────────────────────

    async def get_kudos_users(self, work_id: int) -> list[str]:
        """Extract the list of users who left kudos on a work."""
        url = f"{_BASE}/works/{work_id}?view_adult=true"
        html = await self._get_page(url)
        if not html:
            return []

        kudos_section = re.search(
            r'id="kudos"[^>]*>(.*?)</p>', html, re.DOTALL,
        )
        if not kudos_section:
            kudos_section = re.search(
                r'class="kudos"[^>]*>(.*?)</p>', html, re.DOTALL,
            )
        if not kudos_section:
            return []

        users = re.findall(
            r'<a\s+href="/users/([^"]+)"', kudos_section.group(1),
        )
        return [unescape(u) for u in users]

    # ── Posting / Upload ────────────────────────────────────────

    async def _get_authenticity_token(self, url: str) -> str | None:
        """Fetch a page and extract the Rails authenticity_token."""
        html = await self._get_page(url)
        if not html:
            return None
        m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', html)
        if not m:
            m = re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', html)
        return m.group(1) if m else None

    async def create_work(
        self,
        *,
        title: str,
        content: str,
        fandom: str = "Original Work",
        rating: str = "Explicit",
        warnings: list[str] | None = None,
        categories: list[str] | None = None,
        relationship: str = "",
        characters: str = "",
        additional_tags: str = "",
        summary: str = "",
        notes_begin: str = "",
        notes_end: str = "",
        language_id: str = "1",  # AO3 numeric language ID; 1 = English
        chapter_title: str = "",
        work_skin_id: str = "",
        publish: bool = False,
        # Backwards-compat single-value parameters
        warning: str | None = None,
        category: str | None = None,
    ) -> dict:
        """Create a new work on AO3 as a DRAFT (default) or PUBLISHED.

        Same OTW form as SquidgeWorld. When ``publish=False`` (default),
        uses ``preview_button`` so the work lands in the user's drafts at
        ``/works/{id}/preview`` without being published.

        When ``publish=True``, uses ``post_without_preview_button=Post``
        which AO3 treats as "Post Work Without Preview" — the work is
        created already in published state. Use this when the caller has
        verified the post is safe to go live (e.g. dashboard's "live"
        toggle, set ``publish_live=True`` on the package).

        Args:
            title: Work title.
            content: HTML chapter content (first chapter body).
            fandom: Fandom name (default: "Original Work").
            rating: "General Audiences", "Teen And Up Audiences", "Mature", "Explicit".
            warnings: List of canonical archive warnings. Defaults to
                ``["No Archive Warnings Apply"]``. Each must be one of:
                "Choose Not To Use Archive Warnings", "Graphic Depictions Of Violence",
                "Major Character Death", "No Archive Warnings Apply",
                "Rape/Non-Con", "Underage", "Suicide/Suicidal Ideation",
                "Incest and/or Incestuous Relationship(s)".
            categories: List of relationship categories (e.g. ["M/M"]).
            relationship: Comma-separated relationship tags.
            characters: Comma-separated character tags.
            additional_tags: Comma-separated freeform tags.
            summary: Work summary (HTML allowed, 1250 char max).
            notes_begin: Beginning notes.
            notes_end: End notes.
            language_id: Language ID. AO3 uses ISO codes (e.g. "en"); SQW
                uses numeric IDs ("15"). AO3 form accepts both.
            chapter_title: Optional title for the first chapter.
            work_skin_id: Optional Work Skin ID.
            warning: (deprecated) Single warning string.
            category: (deprecated) Single category string.

        Returns:
            Dict with 'work_id' and 'url'.
        """
        # Backwards compat
        if warnings is None:
            warnings = [warning] if warning else ["No Archive Warnings Apply"]
        if categories is None:
            categories = [category] if category else []

        if not self._logged_in:
            if not await self.ensure_logged_in():
                raise RuntimeError("AO3: Not logged in")

        # GET the new work form to extract CSRF token AND the author pseud ID.
        # The pseud ID is REQUIRED — every OTW work must have at least one
        # creator linked via work[author_attributes][ids][]. Without it the
        # form silently fails validation.
        form_html = await self._get_page(f"{_BASE}/works/new")
        if not form_html:
            raise RuntimeError("AO3: Could not fetch /works/new form")

        # A stale/invalid `_otwarchive_session` cookie makes AO3 302 the
        # /works/new request off to /users/login; _get_page follows the
        # redirect, so form_html is actually the login page. Detect it via
        # the login form's `user[login]` field (never present on the work
        # form) and surface an actionable error instead of the cryptic
        # "couldn't find CSRF token / pseud" that would otherwise follow.
        if 'name="user[login]"' in form_html:
            raise RuntimeError(
                "AO3: session expired or invalid — /works/new redirected to the "
                "login page. Re-copy your `_otwarchive_session` cookie from a "
                "logged-in browser into Settings → Platforms → AO3."
            )

        token_m = re.search(
            r'name="authenticity_token"[^>]*value="([^"]+)"', form_html
        )
        if not token_m:
            raise RuntimeError("AO3: Could not get CSRF token from /works/new")
        token = token_m.group(1)

        pseud_m = re.search(
            r'<input[^>]*value="(\d+)"[^>]*name="work\[author_attributes\]\[ids\]\[\]"',
            form_html,
        ) or re.search(
            r'<input[^>]*name="work\[author_attributes\]\[ids\]\[\]"[^>]*value="(\d+)"',
            form_html,
        )
        if not pseud_m:
            raise RuntimeError("AO3: Could not extract author pseud ID from /works/new")
        pseud_id = pseud_m.group(1)
        self._pseud_id = pseud_id

        clean_content = _collapse_html_whitespace(content)

        form_data: list[tuple[str, str]] = [
            ("authenticity_token", token),
            ("work[title]", title),
            ("work[author_attributes][ids][]", pseud_id),
            ("work[fandom_string]", fandom),
            ("work[rating_string]", rating),
        ]
        # Warnings: array notation. Hidden empty value first, then each warning.
        form_data.append(("work[archive_warning_strings][]", ""))
        for w in warnings:
            form_data.append(("work[archive_warning_strings][]", w))
        # Categories: array notation. Hidden empty value first, then each category.
        form_data.append(("work[category_strings][]", ""))
        for c in categories:
            form_data.append(("work[category_strings][]", c))
        form_data.extend([
            ("work[relationship_string]", relationship),
            ("work[character_string]", characters),
            ("work[freeform_string]", additional_tags),
            ("work[summary]", summary[:1250]),
            ("work[notes]", notes_begin),
            ("work[endnotes]", notes_end),
            ("work[language_id]", language_id),
            ("work[work_skin_id]", work_skin_id),
            ("work[wip_length]", "1"),
            ("work[chapter_attributes][title]", chapter_title),
            ("work[chapter_attributes][content]", clean_content),
        ])
        # Button choice determines draft-vs-live (mirrors add_chapter pattern)
        if publish:
            form_data.append(("post_without_preview_button", "Post"))
        else:
            form_data.append(("preview_button", "Preview"))

        # Manual urlencode because httpx 0.28.x AsyncClient has a bug with
        # list-of-tuples data= (raises "sync request with an AsyncClient").
        from urllib.parse import urlencode
        body = urlencode(form_data, doseq=True)

        await self._polite_delay()
        resp = await self._post_with_retry(
            f"{_BASE}/works",
            content=body,
            headers={
                "Referer": f"{_BASE}/works/new",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )

        final_url = str(resp.url)
        if "/works/new" in final_url or resp.status_code >= 400:
            errors = re.findall(r'class="error"[^>]*>(.*?)</li>', resp.text, re.DOTALL)
            err_text = "; ".join(re.sub(r'<[^>]+>', '', e).strip() for e in errors[:5])
            # A 5xx (esp. Cloudflare 502/503/520-524/525) is AO3's server/CDN being
            # transiently unavailable, not a problem with the work — that error page
            # carries no AO3 <li class="error"> markup, so err_text is empty and the
            # log said "unknown error". Give an obviously-transient message instead;
            # the posting manager retries it (1/5/30-min backoff). (2.122.0)
            if not err_text and resp.status_code >= 500:
                err_text = "AO3/Cloudflare temporarily unavailable — will retry"
            raise RuntimeError(f"AO3: Work creation failed (status {resp.status_code}): {err_text or 'unknown error'}")

        work_match = re.search(r'/works/(\d+)', final_url)
        if work_match:
            work_id = work_match.group(1)
            url = f"{_BASE}/works/{work_id}"
            state = "published" if publish else "preview/draft"
            logger.info("AO3: Created work %s (%s) — %s", work_id, state, url)
            return {"work_id": work_id, "url": url, "published": publish}

        # Some AO3 paths (e.g. "Preview" button on the new-work form) render
        # the Preview Work page inline instead of redirecting. The final URL
        # stays at /works with no ID, but the draft IS created — the page
        # carries action URLs like /works/{id}/post, /works/{id}/edit,
        # /works/{id}/preview. Detect the success-shape via the page title
        # or flash banner, then pull the ID out of the body to avoid false
        # positives from unrelated work mentions.
        body = resp.text
        success_markers = (
            "Draft was successfully created" in body
            or "<title>Preview Work" in body
            or "<title>Edit Work" in body
        )
        if success_markers:
            body_match = re.search(r'/works/(\d+)', body)
            if body_match:
                work_id = body_match.group(1)
                url = f"{_BASE}/works/{work_id}"
                # If publish=True was requested but we still got a preview
                # response, that's a meaningful warning — the button didn't
                # take effect for some reason and the work IS still a draft.
                if publish:
                    logger.warning(
                        "AO3: Work %s created but landed in preview/draft "
                        "despite publish=True. Manual publish needed at %s",
                        work_id, url,
                    )
                    return {"work_id": work_id, "url": url, "published": False}
                logger.info(
                    "AO3: Created work %s (preview-page response, "
                    "URL stayed at /works) — %s", work_id, url,
                )
                return {"work_id": work_id, "url": url, "published": False}

        # Postmortem: dump the response body to the OS temp dir. Portable —
        # AO3 posting runs on the Windows desktop, where /tmp doesn't exist,
        # so the old hardcoded /tmp path silently never wrote anything.
        import tempfile, time
        debug_path = f"{tempfile.gettempdir()}/ao3_create_debug_{int(time.time())}.html"
        try:
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(f"<!-- final_url: {final_url} -->\n")
                f.write(f"<!-- status: {resp.status_code} -->\n")
                f.write(body)
            logger.error("AO3: response body saved to %s", debug_path)
        except Exception:
            pass
        errors = re.findall(r'class="[^"]*error[^"]*"[^>]*>(.*?)</', body, re.DOTALL)
        err_text = "; ".join(re.sub(r'<[^>]+>', '', e).strip()[:200] for e in errors[:5])
        raise RuntimeError(
            f"AO3: Could not extract work ID from {final_url} "
            f"(status={resp.status_code}, errors={err_text or 'none found'})"
        )

    async def edit_work(
        self,
        work_id: str,
        *,
        title: str | None = None,
        summary: str | None = None,
        additional_tags: str | None = None,
        notes_begin: str | None = None,
        notes_end: str | None = None,
        warnings: list[str] | None = None,
        categories: list[str] | None = None,
        relationship: str | None = None,
        characters: str | None = None,
        fandom: str | None = None,
        rating: str | None = None,
        work_skin_id: str | None = None,
        save_as_draft: bool = True,
    ) -> dict:
        """Edit metadata on an existing AO3 work.

        Uses the safe form-fetch pattern (ported from SqW): GET the edit
        form, extract every current field value, modify only the requested
        fields, then POST the full form back with `save_button=Save As
        Draft` (or `post_button=Post`). This fixes the bug where sending
        only a handful of work[*] fields and _method=patch alone returned
        302 but didn't persist — OTW Archive needs all fields + a commit
        button to actually save.

        Args:
            work_id: AO3 work ID.
            title / summary / additional_tags / notes_begin / notes_end /
            relationship / characters / fandom / rating / work_skin_id:
                scalar fields. None = keep current value on AO3.
            warnings / categories: list fields; None = keep current set.
            save_as_draft: True (default) saves the work as a draft;
                False publishes via post_button=Post.

        Returns:
            Dict with work_id and url.
        """
        if not self._logged_in:
            if not await self.ensure_logged_in():
                raise RuntimeError("AO3: Not logged in")

        edit_url = f"{_BASE}/works/{work_id}/edit"
        # Use _get_page so transient 525 / ReadTimeout from CF retry up to
        # 3× before the whole edit operation fails. Direct self._http.get
        # here used to surface a single 525 as a hard RuntimeError and
        # bounce the work into the retry queue for no good reason.
        form_html = await self._get_page(edit_url)
        if form_html is None:
            raise RuntimeError(
                "AO3: Could not load edit form (transient fetch failure)"
            )

        token, current_fields = _extract_work_form_fields(form_html)

        new_fields: list[tuple[str, str]] = []
        warnings_handled = False
        categories_handled = False

        for name, value in current_fields:
            if name == "work[title]" and title is not None:
                new_fields.append((name, title))
            elif name == "work[summary]" and summary is not None:
                new_fields.append((name, summary[:1250]))
            elif name == "work[freeform_string]" and additional_tags is not None:
                new_fields.append((name, additional_tags))
            elif name == "work[notes]" and notes_begin is not None:
                new_fields.append((name, notes_begin))
            elif name == "work[endnotes]" and notes_end is not None:
                new_fields.append((name, notes_end))
            elif name == "work[relationship_string]" and relationship is not None:
                new_fields.append((name, relationship))
            elif name == "work[character_string]" and characters is not None:
                new_fields.append((name, characters))
            elif name == "work[fandom_string]" and fandom is not None:
                new_fields.append((name, fandom))
            elif name == "work[rating_string]" and rating is not None:
                new_fields.append((name, rating))
            elif name == "work[work_skin_id]" and work_skin_id is not None:
                new_fields.append((name, work_skin_id))
            elif name == "work[archive_warning_strings][]":
                if warnings is not None:
                    if not warnings_handled:
                        new_fields.append((name, ""))  # hidden placeholder
                        for w in warnings:
                            new_fields.append((name, w))
                        warnings_handled = True
                else:
                    new_fields.append((name, value))
            elif name == "work[category_strings][]":
                if categories is not None:
                    if not categories_handled:
                        new_fields.append((name, ""))
                        for c in categories:
                            new_fields.append((name, c))
                        categories_handled = True
                else:
                    new_fields.append((name, value))
            else:
                new_fields.append((name, value))

        # Fallback append: if the form didn't have a field we wanted to
        # override (rare, but happens when OTW renders fields differently
        # between new-work and edit forms, e.g. autocomplete widgets that
        # don't emit a hidden input), add the override directly. Without
        # this, a missing form field silently swallows the update.
        def _append_if_missing(field_name: str, value: str | None):
            if value is None:
                return
            if not any(n == field_name for n, _ in new_fields):
                new_fields.append((field_name, value))

        _append_if_missing("work[title]", title)
        if summary is not None:
            _append_if_missing("work[summary]", summary[:1250])
        _append_if_missing("work[freeform_string]", additional_tags)
        _append_if_missing("work[notes]", notes_begin)
        _append_if_missing("work[endnotes]", notes_end)
        _append_if_missing("work[relationship_string]", relationship)
        _append_if_missing("work[character_string]", characters)
        _append_if_missing("work[fandom_string]", fandom)
        _append_if_missing("work[rating_string]", rating)
        _append_if_missing("work[work_skin_id]", work_skin_id)

        # Diagnostics — log what work[*] overrides we're actually sending
        # so next time we can tell whether the field was shipped or dropped.
        overrides_sent = {
            n: v for n, v in new_fields
            if n in (
                "work[title]", "work[freeform_string]",
                "work[relationship_string]", "work[character_string]",
                "work[fandom_string]", "work[rating_string]",
                "work[work_skin_id]",
            )
        }
        logger.info("AO3 edit_work(%s) override summary: %s", work_id, overrides_sent)

        from urllib.parse import urlencode
        submit_data: list[tuple[str, str]] = [
            ("authenticity_token", token),
            ("_method", "patch"),
        ]
        submit_data.extend(new_fields)
        if save_as_draft:
            submit_data.append(("save_button", "Save As Draft"))
        else:
            submit_data.append(("post_button", "Post"))

        body = urlencode(submit_data, doseq=True)

        await self._polite_delay()
        resp = await self._post_with_retry(
            f"{_BASE}/works/{work_id}",
            content=body,
            headers={
                "Referer": edit_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"AO3: Edit failed — status {resp.status_code}")

        # OTW returns 200 even when nothing was saved — check the flash.
        if "have not been saved" in resp.text:
            raise RuntimeError(
                "AO3: Edit POST returned but flash says 'changes have not been saved' "
                "(wrong submit button or validation error)"
            )

        success_patterns = [
            "successfully updated",
            "Work was successfully",
            "updated successfully",
        ]
        if not any(p in resp.text for p in success_patterns):
            err_block = re.search(
                r'<(?:div|ul)[^>]*id="error"[^>]*>(.*?)</(?:div|ul)>',
                resp.text, re.DOTALL,
            )
            err_text = ""
            if err_block:
                err_text = re.sub(r"<[^>]+>", " ", err_block.group(1)).strip()[:300]
            else:
                flash = re.search(
                    r'<div[^>]*class="[^"]*flash[^"]*"[^>]*>(.*?)</div>',
                    resp.text, re.DOTALL,
                )
                if flash:
                    err_text = re.sub(r"<[^>]+>", " ", flash.group(1)).strip()[:300]
            # Missing success flash isn't always fatal (draft redirect can
            # swallow it) — log as warning instead of raising, so the
            # flow continues and the caller can verify by reload.
            logger.warning(
                "AO3: Edit POST returned 200 but no explicit success flash "
                "(flash/errors: %s)", err_text or "(none parsed)",
            )

        # Diagnostics: log any notice/warning flash and any parsed validation
        # errors so tag-dropping / canonicalisation issues surface in the logs.
        for cls in ("notice", "error", "caution", "warning"):
            flash = re.search(
                rf'<(?:div|ul)[^>]*class="[^"]*flash[^"]*\b{cls}\b[^"]*"[^>]*>(.*?)</(?:div|ul)>',
                resp.text, re.DOTALL,
            )
            if flash:
                msg = re.sub(r"<[^>]+>", " ", flash.group(1)).strip()[:400]
                logger.info("AO3 edit_work(%s) flash.%s: %s", work_id, cls, msg)

        logger.info("AO3: Edited work %s", work_id)
        return {"work_id": work_id, "url": f"{_BASE}/works/{work_id}"}

    async def edit_chapter(
        self,
        work_id: str,
        chapter_id: str,
        *,
        content: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        notes_begin: str | None = None,
        notes_end: str | None = None,
        publish: bool | None = None,
    ) -> dict:
        """Edit a chapter using the safe form-fetch overlay pattern.

        Ported from clients.sqw — GETs /works/{w}/chapters/{c}/edit,
        extracts every current chapter[*] field, overrides only the
        requested fields, and POSTs the full form back. Passing
        content=None preserves the existing content on AO3 — useful
        for title-only edits (chapter rename without re-uploading the
        body). Collapses HTML whitespace in content to prevent AO3's
        auto-formatter from inserting <br /> inside elements.

        Args:
            publish: Controls which submit button is sent.
                True  → force ``post_without_preview_button=Post`` — keeps
                        the chapter LIVE after the edit. Use when the
                        caller wants to ensure a draft chapter becomes
                        live, or that a live chapter stays live.
                False → force ``save_button=Save As Draft`` — keeps the
                        chapter as a DRAFT after the edit. Errors if the
                        edit form doesn't expose save_button (e.g.,
                        chapter is already live).
                None  → auto-detect based on form (legacy behaviour):
                        prefer ``save_button`` when present (drafts),
                        else ``post_without_preview_button``. This causes
                        draft chapters to STAY drafts even when the
                        caller wants them live, so prefer passing
                        publish=True explicitly when publishing.
        """
        if not self._logged_in:
            if not await self.ensure_logged_in():
                raise RuntimeError("AO3: Not logged in")

        edit_url = f"{_BASE}/works/{work_id}/chapters/{chapter_id}/edit"
        html = await self._get_page(edit_url)
        if html is None:
            raise RuntimeError(
                "AO3: Could not load chapter edit form (transient fetch failure)"
            )

        token_m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', html)
        if not token_m:
            token_m = re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', html)
        if not token_m:
            raise RuntimeError("AO3: Could not find CSRF token in chapter edit form")
        token = token_m.group(1)

        form_match = re.search(
            r'<form[^>]*action="[^"]*chapters/\d+[^"]*"[^>]*>(.*?)</form>',
            html, re.DOTALL,
        )
        form_body = form_match.group(1) if form_match else html

        def _decode(s: str) -> str:
            return (
                s.replace("&amp;", "&")
                .replace("&quot;", '"')
                .replace("&#39;", "'")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
            )

        # Extract every chapter[*] field with its current value
        current: list[tuple[str, str]] = []
        for inp in re.finditer(r'<input([^>]*?)>', form_body):
            attrs = inp.group(1)
            t_m = re.search(r'\btype="([^"]+)"', attrs)
            t = t_m.group(1).lower() if t_m else "text"
            if t in ("submit", "button", "image", "reset", "file"):
                continue
            n_m = re.search(r'\bname="([^"]+)"', attrs)
            if not n_m:
                continue
            name = n_m.group(1)
            if not (name.startswith("chapter[") or "pseud" in name or "author" in name):
                continue
            v_m = re.search(r'\bvalue="([^"]*)"', attrs)
            v = v_m.group(1) if v_m else ""
            if t in ("checkbox", "radio") and "checked" not in attrs.lower():
                continue
            current.append((name, _decode(v)))

        for sel in re.finditer(r'<select([^>]*?)>(.*?)</select>', form_body, re.DOTALL):
            attrs, body = sel.group(1), sel.group(2)
            n_m = re.search(r'\bname="([^"]+)"', attrs)
            if not n_m or not n_m.group(1).startswith("chapter["):
                continue
            opt = re.search(r'<option[^>]*\bselected[^>]*\bvalue="([^"]*)"', body)
            if not opt:
                opt = re.search(r'<option[^>]*\bvalue="([^"]*)"[^>]*\bselected', body)
            current.append((n_m.group(1), opt.group(1) if opt else ""))

        for ta in re.finditer(r'<textarea([^>]*?)>(.*?)</textarea>', form_body, re.DOTALL):
            attrs, body = ta.group(1), ta.group(2)
            n_m = re.search(r'\bname="([^"]+)"', attrs)
            if not n_m or not n_m.group(1).startswith("chapter["):
                continue
            current.append((n_m.group(1), _decode(body)))

        # Apply overrides
        if content is not None:
            content = _collapse_html_whitespace(content)

        new_fields: list[tuple[str, str]] = []
        for name, value in current:
            if name == "chapter[content]" and content is not None:
                new_fields.append((name, content))
            elif name == "chapter[title]" and title is not None:
                new_fields.append((name, title))
            elif name == "chapter[summary]" and summary is not None:
                new_fields.append((name, summary))
            elif name == "chapter[notes]" and notes_begin is not None:
                new_fields.append((name, notes_begin))
            elif name == "chapter[endnotes]" and notes_end is not None:
                new_fields.append((name, notes_end))
            else:
                new_fields.append((name, value))

        # Button selection.
        # publish=True  → force post_without_preview_button (keep live)
        # publish=False → force save_button if available (keep as draft)
        # publish=None  → legacy auto-detect: prefer save_button when
        #                 present, which keeps drafts as drafts after the
        #                 edit. Bad for "Update + publish live" flows; the
        #                 caller should pass publish=True when they want
        #                 the chapter to end up live regardless of its
        #                 current state.
        has_save = 'name="save_button"' in form_body
        if publish is True:
            button_name = "post_without_preview_button"
            button_value = "Post"
        elif publish is False:
            if has_save:
                button_name = "save_button"
                button_value = "Save As Draft"
            else:
                # Chapter is already live and the form has no Save As
                # Draft button. Fall through to post_without_preview;
                # caller asked for draft but AO3 doesn't expose that path
                # here. Log so callers see what happened.
                button_name = "post_without_preview_button"
                button_value = "Post"
                logger.warning(
                    "AO3: edit_chapter(publish=False) requested but chapter "
                    "%s on work %s has no save_button (already live?); "
                    "submitting with post_without_preview_button",
                    chapter_id, work_id,
                )
        else:
            # publish=None — legacy auto-detect
            button_name = "post_without_preview_button"
            button_value = "Post"
            if has_save:
                button_name = "save_button"
                button_value = "Save As Draft"

        from urllib.parse import urlencode
        submit_data: list[tuple[str, str]] = [
            ("authenticity_token", token),
            ("_method", "patch"),
        ]
        submit_data.extend(new_fields)
        submit_data.append((button_name, button_value))

        body = urlencode(submit_data, doseq=True)

        await self._polite_delay()
        resp = await self._post_with_retry(
            f"{_BASE}/works/{work_id}/chapters/{chapter_id}",
            content=body,
            headers={
                "Referer": edit_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"AO3: Chapter edit failed — status {resp.status_code}")

        if "have not been saved" in resp.text:
            raise RuntimeError(
                f"AO3: Chapter edit POST returned but flash says 'changes have not been saved' "
                f"(button={button_name}, work={work_id}, chapter={chapter_id})"
            )

        logger.info("AO3: Edited chapter %s of work %s", chapter_id, work_id)
        return {"work_id": work_id, "chapter_id": chapter_id}

    async def get_chapter_ids(self, work_id: str) -> list[dict]:
        """Get all chapter IDs and titles for a work."""
        url = f"{_BASE}/works/{work_id}/navigate"
        html = await self._get_page(url)
        if not html:
            return []

        chapters = re.findall(
            r'href="/works/\d+/chapters/(\d+)"[^>]*>(\d+)\.\s*([^<]*)',
            html,
        )
        return [
            {"chapter_id": ch_id, "index": int(idx), "title": title.strip()}
            for ch_id, idx, title in chapters
        ]

    async def post_chapter(self, work_id: str, chapter_id: str) -> dict:
        """Publish a draft chapter by POSTing to /chapters/{cid}/post.

        AO3 chapters have INDEPENDENT draft state. When a work is created
        via preview_button, its ch1 is a draft. Chapters added with
        create_chapter(publish=False) are also drafts. Using
        post_without_preview_button=Post on a chapter only publishes that
        specific chapter (and may flip the work to "posted"); it does
        NOT auto-publish other draft chapters on the same work. To clean
        up all drafts on a work, iterate get_chapter_ids() and call this
        for each.

        Form fields (from the per-chapter "Post Chapter" button on AO3):
          - authenticity_token: extracted from a fresh page fetch
          - commit: "Post Chapter"

        Returns dict with keys:
          - work_id, chapter_id: echoes input
          - already_posted: True if AO3 indicated the chapter was already
            live (idempotent no-op)
          - published: True if this call published it
        """
        if not self._logged_in:
            if not await self.ensure_logged_in():
                raise RuntimeError("AO3: Not logged in")

        page_url = f"{_BASE}/works/{work_id}/chapters/{chapter_id}"
        html = await self._get_page(page_url)
        if not html:
            raise RuntimeError(
                f"AO3: Could not load chapter {chapter_id} page (work {work_id})"
            )

        # Already-posted chapters render the normal show page with no
        # "Post Chapter" button. If no /post form is present, treat as
        # already-posted no-op (don't waste a POST that AO3 will reject).
        if "/chapters/" + str(chapter_id) + "/post" not in html:
            return {
                "work_id": work_id,
                "chapter_id": chapter_id,
                "already_posted": True,
                "published": False,
            }

        token_m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', html)
        if not token_m:
            token_m = re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', html)
        if not token_m:
            raise RuntimeError(
                f"AO3: Could not extract CSRF token from chapter {chapter_id} page"
            )
        token = token_m.group(1)

        post_url = f"{_BASE}/works/{work_id}/chapters/{chapter_id}/post"
        from urllib.parse import urlencode
        body = urlencode([
            ("authenticity_token", token),
            ("commit", "Post Chapter"),
        ])
        resp = await self._post_with_retry(
            post_url,
            content=body,
            headers={
                "Referer": page_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )

        # 302 redirect to /works/{wid}/chapters/{cid} = success.
        # 200 with "already posted" / "already published" notice = no-op.
        already = (
            resp.status_code == 200
            and ("already posted" in resp.text.lower()
                 or "already published" in resp.text.lower())
        )
        published = not already and resp.status_code < 400
        return {
            "work_id": work_id,
            "chapter_id": chapter_id,
            "already_posted": already,
            "published": published,
        }

    async def publish_all_draft_chapters(self, work_id: str) -> dict:
        """Iterate every chapter on a work and publish any that are drafts.

        Used after a multi-chapter post()/edit() with publish_live=True to
        clean up draft chapters left behind by AO3's independent-state
        chapter model.

        Returns dict with:
          - total: total chapters on the work
          - published: count of chapters this call newly published
          - already_posted: count that were already live
          - failed: list of (chapter_id, error_string) for chapters that
            errored — does not raise, surfaces them in the result so the
            caller can log/continue
        """
        chapters = await self.get_chapter_ids(work_id)
        published = 0
        already_posted = 0
        failed: list[tuple[str, str]] = []

        for ch in chapters:
            cid = ch.get("chapter_id")
            if not cid:
                continue
            try:
                result = await self.post_chapter(work_id, cid)
                if result.get("published"):
                    published += 1
                    logger.info(
                        "AO3: Published chapter %s (idx %s) on work %s",
                        cid, ch.get("index"), work_id,
                    )
                else:
                    already_posted += 1
            except Exception as e:
                failed.append((cid, str(e)))
                logger.warning(
                    "AO3: Failed to publish chapter %s on work %s: %s",
                    cid, work_id, e,
                )
            await self._polite_delay()

        return {
            "total": len(chapters),
            "published": published,
            "already_posted": already_posted,
            "failed": failed,
        }

    async def create_chapter(
        self,
        work_id: str,
        *,
        title: str,
        content: str,
        position: int | None = None,
        summary: str = "",
        notes_begin: str = "",
        notes_end: str = "",
        publish: bool = False,
    ) -> dict:
        """Add a new chapter to an existing AO3 work.

        Ported from the SqW client — AO3 and SquidgeWorld run the same OTW
        Archive software so the chapters/new form is identical.

        SAFETY: By default (publish=False) this uses preview_button=Preview,
        which adds the chapter to the work while PRESERVING the work's
        current state (a draft stays a draft). No follow-up POST is needed —
        the preview request creates the chapter fully. Set publish=True only
        when you want to publish the entire work along with the new chapter.

        Args:
            work_id: The work to add the chapter to.
            title: Chapter title.
            content: HTML content of the chapter.
            position: Optional position (1 = first, etc). None lets OTW append.
            summary: Optional chapter summary.
            notes_begin: Optional beginning notes.
            notes_end: Optional end notes.
            publish: If True, uses post_without_preview_button (publishes).
                If False (default), uses preview_button — safe for drafts.

        Returns:
            Dict with 'chapter_id', 'work_id', 'url', 'published'.
        """
        if not self._logged_in:
            if not await self.ensure_logged_in():
                raise RuntimeError("AO3: Not logged in")

        form_url = f"{_BASE}/works/{work_id}/chapters/new"
        html = await self._get_page(form_url)
        if html is None:
            raise RuntimeError(
                "AO3: Could not load chapter form (transient fetch failure)"
            )

        token_m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', html)
        if not token_m:
            token_m = re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', html)
        if not token_m:
            raise RuntimeError("AO3: Could not get CSRF token from chapter form")
        token = token_m.group(1)

        pseud_m = re.search(
            r'<input[^>]*value="(\d+)"[^>]*name="chapter\[author_attributes\]\[ids\]\[\]"',
            html,
        ) or re.search(
            r'<input[^>]*name="chapter\[author_attributes\]\[ids\]\[\]"[^>]*value="(\d+)"',
            html,
        )
        if not pseud_m:
            raise RuntimeError("AO3: Could not extract chapter author pseud ID")
        pseud_id = pseud_m.group(1)

        from urllib.parse import urlencode
        form_data: list[tuple[str, str]] = [
            ("authenticity_token", token),
            ("chapter[author_attributes][ids][]", pseud_id),
            ("chapter[title]", title),
            ("chapter[summary]", summary),
            ("chapter[notes]", notes_begin),
            ("chapter[endnotes]", notes_end),
            ("chapter[content]", content),
        ]
        if position is not None:
            form_data.append(("chapter[position]", str(position)))

        if publish:
            form_data.append(("post_without_preview_button", "Post"))
        else:
            form_data.append(("preview_button", "Preview"))

        body = urlencode(form_data, doseq=True)

        await self._polite_delay()
        resp = await self._post_with_retry(
            f"{_BASE}/works/{work_id}/chapters",
            content=body,
            headers={
                "Referer": form_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"AO3: Chapter creation failed — status {resp.status_code}")

        if "Sorry! We couldn" in resp.text:
            errors = re.findall(
                r'<(?:li|div)[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)</(?:li|div)>',
                resp.text, re.DOTALL,
            )
            err_text = "; ".join(
                re.sub(r"<[^>]+>", "", e).strip()[:200] for e in errors[:5]
            )
            raise RuntimeError(
                f"AO3: Chapter creation failed: {err_text or '(none parsed)'}"
            )

        final_url = str(resp.url)
        ch_match = re.search(rf'/works/{work_id}/chapters/(\d+)', final_url)
        chapter_id = ch_match.group(1) if ch_match else ""

        # Body fallback: when AO3 stays on a /chapters page without an
        # ID in the URL (preview flow, or just an ID-less render), pull
        # the new chapter ID out of the body. We pick the HIGHEST ID
        # because the response page may also reference the work's
        # earlier chapters in nav links — the newest one always has
        # the largest numeric ID on AO3.
        if not chapter_id:
            body_text = resp.text
            ids_in_body = re.findall(rf'/works/{work_id}/chapters/(\d+)', body_text)
            if ids_in_body:
                chapter_id = max(ids_in_body, key=int)
                logger.info(
                    "AO3: Added chapter via body-scan fallback "
                    "(URL=%s) chapter_id=%s (max of %d ids in body)",
                    final_url, chapter_id, len(ids_in_body),
                )

        # Last-resort fallback: fetch /works/{work_id}/navigate which
        # lists every chapter (including drafts) and grab the newest.
        if not chapter_id:
            try:
                nav_html = await self._get_page(
                    f"{_BASE}/works/{work_id}/navigate"
                )
                if nav_html:
                    nav_ids = re.findall(
                        rf'/works/{work_id}/chapters/(\d+)', nav_html
                    )
                    if nav_ids:
                        chapter_id = max(nav_ids, key=int)
                        logger.info(
                            "AO3: Recovered chapter_id=%s from /navigate "
                            "after POST response had no parseable ID (URL=%s)",
                            chapter_id, final_url,
                        )
            except Exception as nav_err:
                logger.warning(
                    "AO3: /navigate fallback failed for work %s: %s",
                    work_id, nav_err,
                )

        if not chapter_id:
            # Dump the response body for postmortem so we can refine
            # the parser if AO3 changes its response shape again.
            try:
                import tempfile, time
                dump_path = (
                    f"{tempfile.gettempdir()}/ao3_chapter_debug_"
                    f"{work_id}_{int(time.time())}.html"
                )
                with open(dump_path, "w", encoding="utf-8") as f:
                    f.write(resp.text)
                logger.error(
                    "AO3: Dumped failed chapter response to %s", dump_path
                )
            except Exception:
                pass
            raise RuntimeError(
                f"AO3: Could not extract chapter_id from response URL: {final_url}"
            )

        logger.info(
            "AO3: Added chapter to work %s — chapter_id=%s publish=%s",
            work_id, chapter_id, publish,
        )
        return {
            "chapter_id": chapter_id,
            "work_id": work_id,
            "url": final_url,
            "published": publish,
        }

    # ── Safety / Cleanup ────────────────────────────────────────

    async def delete_work(self, work_id: str) -> bool:
        """Delete a work via the OTW confirm_delete flow.

        Returns True on success, raises on failure. USE WITH CARE - destructive.
        """
        if not self._logged_in:
            if not await self.ensure_logged_in():
                raise RuntimeError("AO3: Not logged in")

        confirm_url = f"{_BASE}/works/{work_id}/confirm_delete"
        confirm_resp = await self._http.get(confirm_url)
        if confirm_resp.status_code != 200:
            raise RuntimeError(
                f"AO3: Could not load confirm_delete page (status {confirm_resp.status_code})"
            )

        token_m = re.search(
            r'name="authenticity_token"[^>]*value="([^"]+)"', confirm_resp.text
        ) or re.search(
            r'value="([^"]+)"[^>]*name="authenticity_token"', confirm_resp.text
        )
        if not token_m:
            raise RuntimeError("AO3: Could not get CSRF token from confirm_delete page")

        from urllib.parse import urlencode
        body = urlencode([
            ("authenticity_token", token_m.group(1)),
            ("_method", "delete"),
            ("commit", "Yes, Delete Work"),
        ])

        await self._polite_delay()
        resp = await self._post_with_retry(
            f"{_BASE}/works/{work_id}",
            content=body,
            headers={
                "Referer": confirm_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"AO3: Delete failed — status {resp.status_code}")
        if "successfully deleted" not in resp.text and "has been deleted" not in resp.text:
            check = await self._http.get(
                f"{_BASE}/works/{work_id}", follow_redirects=False
            )
            if check.status_code != 404:
                logger.warning(
                    "AO3: delete_work returned %s but work %s may still exist",
                    resp.status_code, work_id,
                )

        logger.info("AO3: Deleted work %s", work_id)
        return True

    async def is_work_in_drafts(self, work_id: str) -> bool | None:
        """Check whether a work is in /users/{user}/works/drafts.

        Returns:
            True   — work is in the drafts listing
            False  — drafts page fetched, work not present
            None   — fetch failed (network/timeout/CF) — caller cannot conclude
        """
        if not self._logged_in:
            await self.ensure_logged_in()
        # In cookie-only auth mode self.username is empty (the cookie carries
        # the identity, not the form-login handle). Fall back to target_user
        # so we don't hit /users//works/drafts — that URL 429s as a 404 and
        # burns budget on a dead request.
        owner = self.username or self.target_user
        url = f"{_BASE}/users/{owner}/works/drafts"
        html = await self._get_page(url)
        if html is None:
            return None
        return f"/works/{work_id}" in html

    async def is_work_published(self, work_id: str) -> bool | None:
        """Check whether a work is in /users/{user}/works (the published listing).

        Returns:
            True   — work is in the published listing
            False  — published page fetched, work not present
            None   — fetch failed (caller cannot conclude)
        """
        if not self._logged_in:
            await self.ensure_logged_in()
        owner = self.username or self.target_user
        url = f"{_BASE}/users/{owner}/works"
        html = await self._get_page(url)
        if html is None:
            return None
        return f"/works/{work_id}" in html

    # ── Work Skins ──────────────────────────────────────────────
    #
    # AO3 runs the OTW Archive software (same as SquidgeWorld), so the
    # skin endpoints are identical: GET /skins/new?skin_type=WorkSkin,
    # POST /skins, /skins/{id}/edit, /skins/{id}. These methods are a
    # near-verbatim port of the SqW client's Work Skin CRUD.

    async def find_work_skin_by_title(self, title: str) -> str | None:
        """Look up an existing Work Skin owned by the current user by title.

        Returns the skin_id as a string, or None if not found.
        """
        if not self._logged_in:
            await self.ensure_logged_in()

        owner = self.username or self.target_user
        url = f"{_BASE}/users/{owner}/skins?skin_type=WorkSkin"
        html = await self._get_page(url)
        if html is None:
            return None

        for m in re.finditer(
            r'<a\s+href="/skins/(\d+)"[^>]*>([^<]+)</a>',
            html,
        ):
            skin_id, skin_title = m.group(1), m.group(2).strip()
            if skin_title == title:
                return skin_id
        return None

    async def create_work_skin(
        self,
        *,
        title: str,
        css: str,
        description: str = "",
        public: bool = False,
        role: str = "user",
    ) -> dict:
        """Create a new Work Skin on AO3.

        Args:
            title: Skin title (visible in dropdowns).
            css: The CSS source. Should be scoped to #workskin (OTW Archive
                wraps work content in <div id="workskin">).
            description: Optional skin description.
            public: If True, requests public visibility (requires admin approval).
            role: "user" (add to archive skin) or "override" (replace).

        Returns:
            Dict with 'skin_id' and 'url'.
        """
        if not self._logged_in:
            if not await self.ensure_logged_in():
                raise RuntimeError("AO3: Not logged in")

        form_url = f"{_BASE}/skins/new?skin_type=WorkSkin"
        form_html = await self._get_page(form_url)
        if form_html is None:
            raise RuntimeError(
                "AO3: Could not load skin form (transient fetch failure)"
            )

        token_m = re.search(
            r'name="authenticity_token"[^>]*value="([^"]+)"', form_html
        )
        if not token_m:
            token_m = re.search(
                r'value="([^"]+)"[^>]*name="authenticity_token"', form_html
            )
        if not token_m:
            raise RuntimeError("AO3: Could not get CSRF token from skin form")
        token = token_m.group(1)

        from urllib.parse import urlencode
        form_data = [
            ("authenticity_token", token),
            ("skin_type", "WorkSkin"),
            ("skin[title]", title),
            ("skin[description]", description),
            ("skin[public]", "0"),
            ("skin[unusable]", "0"),
            ("skin[role]", role),
            ("skin[ie_condition]", ""),
            ("skin[css]", css),
            ("commit", "Submit"),
        ]
        if public:
            form_data.append(("skin[public]", "1"))

        body = urlencode(form_data, doseq=True)

        await self._polite_delay()
        resp = await self._post_with_retry(
            f"{_BASE}/skins",
            content=body,
            headers={
                "Referer": form_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )

        final_url = str(resp.url)
        skin_match = re.search(r'/skins/(\d+)(?:[/?]|$)', final_url)
        if skin_match:
            skin_id = skin_match.group(1)
            logger.info("AO3: Created Work Skin %s — %s", skin_id, title)
            return {"skin_id": skin_id, "url": f"{_BASE}/skins/{skin_id}", "title": title}

        errors = re.findall(
            r'<(?:li|div)[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)</(?:li|div)>',
            resp.text, re.DOTALL,
        )
        err_text = "; ".join(
            re.sub(r"<[^>]+>", "", e).strip()[:200] for e in errors[:5]
        )
        raise RuntimeError(
            f"AO3: Skin creation failed. status={resp.status_code} url={final_url} "
            f"errors={err_text or '(none parsed)'}"
        )

    async def get_or_create_work_skin(
        self,
        *,
        title: str,
        css: str,
        description: str = "",
    ) -> str:
        """Find an existing Work Skin by title or create a new one. Returns skin_id."""
        existing = await self.find_work_skin_by_title(title)
        if existing:
            logger.info("AO3: Reusing existing Work Skin %s — %s", existing, title)
            return existing
        result = await self.create_work_skin(
            title=title, css=css, description=description,
        )
        return result["skin_id"]

    async def edit_work_skin(
        self,
        skin_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        css: str | None = None,
        public: bool | None = None,
    ) -> dict:
        """Edit an existing Work Skin's metadata or CSS.

        Uses the safe form-fetch pattern: GET /skins/{id}/edit, extract
        every skin[*] field with its current value, override only the
        requested fields, then POST back with _method=patch.
        """
        if not self._logged_in:
            if not await self.ensure_logged_in():
                raise RuntimeError("AO3: Not logged in")

        edit_url = f"{_BASE}/skins/{skin_id}/edit"
        html = await self._get_page(edit_url)
        if html is None:
            raise RuntimeError(
                "AO3: Could not load skin edit form (transient fetch failure)"
            )

        token_m = re.search(
            r'name="authenticity_token"[^>]*value="([^"]+)"', html
        )
        if not token_m:
            token_m = re.search(
                r'value="([^"]+)"[^>]*name="authenticity_token"', html
            )
        if not token_m:
            raise RuntimeError("AO3: Could not find CSRF token in skin edit form")
        token = token_m.group(1)

        form_match = re.search(
            r'<form[^>]*action="[^"]*skins/\d+[^"]*"[^>]*>(.*?)</form>',
            html, re.DOTALL,
        )
        form_body = form_match.group(1) if form_match else html

        def _decode(s: str) -> str:
            return (
                s.replace("&amp;", "&")
                .replace("&quot;", '"')
                .replace("&#39;", "'")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
            )

        current: list[tuple[str, str]] = []
        for inp in re.finditer(r'<input([^>]*?)>', form_body):
            attrs = inp.group(1)
            t_m = re.search(r'\btype="([^"]+)"', attrs)
            t = t_m.group(1).lower() if t_m else "text"
            if t in ("submit", "button", "image", "reset", "file"):
                continue
            n_m = re.search(r'\bname="([^"]+)"', attrs)
            if not n_m or not n_m.group(1).startswith("skin["):
                continue
            v_m = re.search(r'\bvalue="([^"]*)"', attrs)
            v = v_m.group(1) if v_m else ""
            if t in ("checkbox", "radio"):
                if "checked" not in attrs.lower():
                    continue
            current.append((n_m.group(1), _decode(v)))

        for sel in re.finditer(r'<select([^>]*?)>(.*?)</select>', form_body, re.DOTALL):
            attrs, body = sel.group(1), sel.group(2)
            n_m = re.search(r'\bname="([^"]+)"', attrs)
            if not n_m or not n_m.group(1).startswith("skin["):
                continue
            opt = re.search(r'<option[^>]*\bselected[^>]*\bvalue="([^"]*)"', body)
            if not opt:
                opt = re.search(r'<option[^>]*\bvalue="([^"]*)"[^>]*\bselected', body)
            current.append((n_m.group(1), opt.group(1) if opt else ""))

        for ta in re.finditer(r'<textarea([^>]*?)>(.*?)</textarea>', form_body, re.DOTALL):
            attrs, body = ta.group(1), ta.group(2)
            n_m = re.search(r'\bname="([^"]+)"', attrs)
            if not n_m or not n_m.group(1).startswith("skin["):
                continue
            current.append((n_m.group(1), _decode(body)))

        new_fields: list[tuple[str, str]] = []
        for name, value in current:
            if name == "skin[title]" and title is not None:
                new_fields.append((name, title))
            elif name == "skin[description]" and description is not None:
                new_fields.append((name, description))
            elif name == "skin[css]" and css is not None:
                new_fields.append((name, css))
            elif name == "skin[public]" and public is not None:
                new_fields.append((name, "1" if public else "0"))
            else:
                new_fields.append((name, value))

        from urllib.parse import urlencode
        submit_data: list[tuple[str, str]] = [
            ("authenticity_token", token),
            ("_method", "patch"),
        ]
        submit_data.extend(new_fields)
        submit_data.append(("commit", "Update"))

        body = urlencode(submit_data, doseq=True)

        await self._polite_delay()
        resp = await self._post_with_retry(
            f"{_BASE}/skins/{skin_id}",
            content=body,
            headers={
                "Referer": edit_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"AO3: Skin edit failed — status {resp.status_code}")
        if "have not been saved" in resp.text:
            raise RuntimeError(
                "AO3: Skin edit POST returned but flash says 'changes have not been saved'"
            )

        logger.info("AO3: Updated Work Skin %s", skin_id)
        return {"skin_id": skin_id, "url": f"{_BASE}/skins/{skin_id}"}


def _extract_work_form_fields(html: str) -> tuple[str, list[tuple[str, str]]]:
    """Parse all work[*] form fields from a /works/{id}/edit page.

    Ported from clients.sqw — AO3 runs the same OTW Archive software, so
    the edit form layout is identical. Returns (csrf_token, list_of_
    (name, value)_tuples) so edit_work can resubmit the complete form
    without Rails clearing omitted fields.

    Handles hidden/text inputs, checkboxes (checked only), radios
    (checked only), selects (selected option), and textareas.
    """
    token_m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', html)
    if not token_m:
        token_m = re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', html)
    if not token_m:
        raise RuntimeError("AO3: Could not find CSRF token in work edit form")
    token = token_m.group(1)

    form_match = re.search(
        r'<form[^>]*action="[^"]*works/\d+[^"]*"[^>]*>(.*?)</form>',
        html, re.DOTALL,
    )
    form_html = form_match.group(1) if form_match else html

    def _decode(s: str) -> str:
        return (
            s.replace("&amp;", "&")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
        )

    fields: list[tuple[str, str]] = []

    for inp_match in re.finditer(r'<input([^>]*?)>', form_html):
        attrs = inp_match.group(1)
        type_m = re.search(r'\btype="([^"]+)"', attrs)
        inp_type = type_m.group(1).lower() if type_m else "text"
        if inp_type in ("submit", "button", "image", "reset", "file"):
            continue
        name_m = re.search(r'\bname="([^"]+)"', attrs)
        if not name_m:
            continue
        name = name_m.group(1)
        if name in ("authenticity_token", "_method", "utf8"):
            continue
        if not (name.startswith("work[") or "pseud" in name or "author" in name):
            continue
        value_m = re.search(r'\bvalue="([^"]*)"', attrs)
        value = value_m.group(1) if value_m else ""
        if inp_type in ("checkbox", "radio") and "checked" not in attrs.lower():
            continue
        fields.append((name, _decode(value)))

    for sel_match in re.finditer(r'<select([^>]*?)>(.*?)</select>', form_html, re.DOTALL):
        attrs = sel_match.group(1)
        body = sel_match.group(2)
        name_m = re.search(r'\bname="([^"]+)"', attrs)
        if not name_m:
            continue
        name = name_m.group(1)
        if not name.startswith("work["):
            continue
        sel_opt = re.search(r'<option[^>]*\bselected[^>]*\bvalue="([^"]*)"', body)
        if not sel_opt:
            sel_opt = re.search(r'<option[^>]*\bvalue="([^"]*)"[^>]*\bselected', body)
        value = sel_opt.group(1) if sel_opt else ""
        fields.append((name, value))

    for ta_match in re.finditer(r'<textarea([^>]*?)>(.*?)</textarea>', form_html, re.DOTALL):
        attrs = ta_match.group(1)
        body = ta_match.group(2)
        name_m = re.search(r'\bname="([^"]+)"', attrs)
        if not name_m:
            continue
        name = name_m.group(1)
        if not name.startswith("work["):
            continue
        fields.append((name, _decode(body)))

    return token, fields


def _collapse_html_whitespace(html: str) -> str:
    """Collapse multi-line HTML so each element is on a single line.

    OTW Archive's chapter editor converts internal newlines within HTML tags
    to <br /> tags, causing unwanted line breaks.
    """
    def _collapse_tag(match: re.Match) -> str:
        text = match.group(0)
        collapsed = re.sub(r'\n\s*', ' ', text)
        collapsed = re.sub(r'  +', ' ', collapsed)
        return collapsed

    result = re.sub(r'<p[^>]*>.*?</p>', _collapse_tag, html, flags=re.DOTALL)
    result = re.sub(r'<div[^>]*>.*?</div>', _collapse_tag, result, flags=re.DOTALL)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result
