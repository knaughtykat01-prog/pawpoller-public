"""SquidgeWorld (SqW) HTTP client.

SquidgeWorld runs the OTW Archive software (same as AO3).  Authentication
is via standard Rails form login with CSRF token.  Data is collected by
scraping the web UI since there is no public API.

Key details:
  - Work IDs are integers (e.g. 88335)
  - Stats: hits, kudos, comments, bookmarks
  - Auth: username/password login (separate from the user being tracked)
  - Anti-bot measures may require realistic headers and rate limiting
"""

from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import re
from html import unescape

import httpx

import config


def _extract_work_form_fields(html: str) -> tuple[str, list[tuple[str, str]]]:
    """Parse all work[*] form fields from a /works/{id}/edit page.

    Returns (csrf_token, list_of_(name,value)_tuples). Used by edit_work
    so that we can submit a complete form back without clearing fields.

    Handles:
      - hidden / text inputs (value attr in either order)
      - checkboxes (only those with `checked` attribute)
      - radio buttons (only those with `checked` attribute)
      - select fields (option marked `selected`)
      - textareas (body content with HTML entities decoded)
    """
    # CSRF token (works in either attribute order)
    token_m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', html)
    if not token_m:
        token_m = re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', html)
    if not token_m:
        raise RuntimeError("Could not find CSRF token in work edit form")
    token = token_m.group(1)

    # Scope to the main work edit form so we don't pick up unrelated forms
    form_match = re.search(
        r'<form[^>]*action="[^"]*works/\d+[^"]*"[^>]*>(.*?)</form>',
        html,
        re.DOTALL,
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

    # 1. Inputs
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
        if inp_type == "checkbox":
            if "checked" not in attrs.lower():
                continue
        elif inp_type == "radio":
            if "checked" not in attrs.lower():
                continue
        fields.append((name, _decode(value)))

    # 2. Selects
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

    # 3. Textareas
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

logger = logging.getLogger(__name__)

_BASE = "https://squidgeworld.org"


def _collapse_html_whitespace(html: str) -> str:
    """Collapse multi-line HTML so each element is on a single line.

    OTW Archive's chapter editor converts internal newlines within HTML tags
    to <br /> tags, causing unwanted line breaks. This function:
      1. Joins lines within <p>...</p> tags into single lines
      2. Joins lines within <div>...</div> tags into single lines
      3. Collapses runs of whitespace (but preserves single spaces)
    """
    import re as _re
    # Collapse newlines + indentation within tags to single spaces
    # Match opening tag through closing tag, collapsing internal whitespace
    def _collapse_tag(match: _re.Match) -> str:
        text = match.group(0)
        # Replace newline + optional whitespace with a single space
        collapsed = _re.sub(r'\n\s*', ' ', text)
        # Collapse multiple spaces into one
        collapsed = _re.sub(r'  +', ' ', collapsed)
        return collapsed

    # Process <p>...</p> tags
    result = _re.sub(r'<p[^>]*>.*?</p>', _collapse_tag, html, flags=_re.DOTALL)
    # Process <div>...</div> tags (non-greedy, innermost first)
    result = _re.sub(r'<div[^>]*>.*?</div>', _collapse_tag, result, flags=_re.DOTALL)
    # Remove blank lines that were left behind
    result = _re.sub(r'\n{3,}', '\n\n', result)
    return result

# Realistic browser headers to avoid bot detection
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


class SquidgeWorldClient:
    """Async HTTP client for SquidgeWorld (OTW Archive)."""

    def __init__(self, username: str, password: str, target_user: str,
                 proxy_url: str = "", proxy_key: str = ""):
        """
        Args:
            username: Login account username (e.g. PawPoller)
            password: Login account password
            target_user: User whose works to track (e.g. KnaughtyKat)
            proxy_url, proxy_key: Optional CF Worker proxy. Off by
                default — SqW works fine direct from any IP. Toggle
                via sqw_use_cf_proxy if it ever starts blocking us.
        """
        self.username = username
        self.password = password
        self.target_user = target_user
        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("SqW client using CF proxy: %s", proxy_url)
        else:
            transport = httpx.AsyncHTTPTransport(retries=2)
        self._http = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers=_HEADERS,
            transport=transport,
        )
        self._logged_in = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    def update_credentials(self, username: str, password: str, target_user: str) -> None:
        """Update credentials, invalidating cached session if changed."""
        if username != self.username or password != self.password:
            self._logged_in = False
        self.username = username
        self.password = password
        self.target_user = target_user

    async def close(self) -> None:
        await self._http.aclose()

    # ── Anubis Bot Challenge ─────────────────────────────────────

    async def _solve_anubis(self, html: str) -> bool:
        """Solve the Anubis bot challenge.

        Anubis (by Xe Iaso / Techaro) protects SquidgeWorld with a SHA-256
        challenge.  The server-side "preact" validation:
          1. Extract the challenge randomData from preact_info JSON
          2. Compute result = SHA256(randomData) — no nonce needed
          3. Wait at least difficulty * 80ms (server-side time gate)
          4. GET the pass-challenge endpoint with result=<sha256hex>
          5. Receive a JWT auth cookie for subsequent requests
        """
        # Extract preact_info JSON from the challenge page
        m = re.search(
            r'id="preact_info"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        if not m:
            logger.error("SqW: Could not find preact_info in Anubis challenge page")
            return False

        try:
            info = json.loads(m.group(1).strip())
        except json.JSONDecodeError as e:
            logger.error("SqW: Failed to parse preact_info JSON: %s", e)
            return False

        challenge = info.get("challenge", "")
        redir = info.get("redir", "")
        difficulty = info.get("difficulty", 1)

        if not challenge or not redir:
            logger.error("SqW: preact_info missing challenge or redir")
            return False

        # Server validates: result == SHA256(randomData) — no nonce involved.
        # The client-side PoW is browser-only; the server just checks the hash.
        result = hashlib.sha256(challenge.encode("utf-8")).hexdigest()
        logger.info("SqW: Computed Anubis challenge hash (difficulty=%d)", difficulty)

        # Server enforces a time gate: difficulty * 80ms minimum elapsed.
        wait_seconds = max(difficulty * 0.1, 0.2)
        await asyncio.sleep(wait_seconds)

        # Submit the solution
        pass_url = f"{_BASE}{redir}"
        separator = "&" if "?" in pass_url else "?"
        pass_url = f"{pass_url}{separator}result={result}&nonce=0"

        try:
            resp = await self._http.get(pass_url)
            logger.info("SqW: Anubis challenge response: %d -> %s", resp.status_code, resp.url)
            if resp.status_code >= 400:
                logger.error("SqW: Anubis challenge rejected (HTTP %d)", resp.status_code)
                return False
            return True
        except httpx.HTTPError as e:
            logger.error("SqW: Failed to submit Anubis solution: %s", e)
            return False

    async def _get_page(self, url: str) -> str | None:
        """Fetch a page, solving Anubis challenges if encountered."""
        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("SqW: Failed to fetch %s: %s", url, e)
            return None

        html = resp.text

        # Check if we got an Anubis challenge instead of the actual page
        if "Making sure you" in html and "anubis" in html.lower():
            logger.info("SqW: Anubis challenge detected, solving...")
            if await self._solve_anubis(html):
                # Retry the original request
                try:
                    resp = await self._http.get(url)
                    resp.raise_for_status()
                    return resp.text
                except httpx.HTTPError as e:
                    logger.error("SqW: Retry after Anubis failed: %s", e)
                    return None
            else:
                return None

        return html

    # ── Authentication ──────────────────────────────────────────

    async def login(self) -> bool:
        """Authenticate via OTW Archive Rails login form.

        Steps:
          1. GET /users/login → extract authenticity_token from form
          2. POST /users/login with credentials + token
          3. Verify login succeeded by checking for redirect or user menu
        """
        logger.info("SqW: Logging in as %s...", self.username)

        # Step 1: Get login page (may trigger Anubis challenge)
        html = await self._get_page(f"{_BASE}/users/login")
        if not html:
            logger.error("SqW: Failed to fetch login page")
            return False

        # Extract authenticity_token from the login form
        token_match = re.search(
            r'<input[^>]*name="authenticity_token"[^>]*value="([^"]+)"',
            html,
        )
        if not token_match:
            # Try alternate pattern (value before name)
            token_match = re.search(
                r'<input[^>]*value="([^"]+)"[^>]*name="authenticity_token"',
                html,
            )
        if not token_match:
            logger.error("SqW: Could not find authenticity_token on login page")
            return False

        token = token_match.group(1)

        # Step 2: POST login
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
            logger.error("SqW: Login POST failed: %s", e)
            return False

        # Step 3: Verify login — check for greeting or logged-in indicators
        page = resp.text

        def _extract_account_name_from_url(url: str) -> str | None:
            # Successful login redirects to /users/<account-name>
            m = re.search(r"/users/([^/?&#]+)", url)
            if m:
                candidate = m.group(1)
                # Skip synthetic segments like 'login', 'confirmation', etc.
                if candidate not in ("login", "logout", "register", "password"):
                    return candidate
            return None

        def _extract_account_name_from_page(html: str) -> str | None:
            # Look for the user menu link — "<a href='/users/<name>'>...</a>"
            for m in re.finditer(r'href="/users/([^/"?&]+)"', html):
                candidate = m.group(1)
                if candidate not in ("login", "logout", "register", "password"):
                    return candidate
            return None

        if "greeting" in page.lower() or f"Hi, {self.username}" in page or "Log Out" in page:
            self._logged_in = True
            # Replace login email with the actual account name so subsequent
            # /users/{name}/... URLs hit the right page.
            if "@" in self.username:
                new_name = (
                    _extract_account_name_from_url(str(resp.url))
                    or _extract_account_name_from_page(page)
                )
                if new_name:
                    logger.info(
                        "SqW: Resolved login %r -> account name %r",
                        self.username, new_name,
                    )
                    self.username = new_name
            logger.info("SqW: Successfully logged in as %s", self.username)
            return True

        # Check if we landed on the dashboard (successful login redirects)
        if resp.url and "/users/" in str(resp.url):
            self._logged_in = True
            if "@" in self.username:
                new_name = _extract_account_name_from_url(str(resp.url))
                if new_name:
                    logger.info(
                        "SqW: Resolved login %r -> account name %r",
                        self.username, new_name,
                    )
                    self.username = new_name
            logger.info("SqW: Login redirect successful for %s", self.username)
            return True

        logger.error("SqW: Login appears to have failed (no logged-in indicators)")
        return False

    async def ensure_logged_in(self) -> bool:
        """Login if not already authenticated."""
        if self._logged_in:
            # Quick session check
            html = await self._get_page(f"{_BASE}/users/{self.username}")
            if html and "Log Out" in html:
                return True
            # Conservative: only flip the flag when SqW returns a page
            # that lacks the "Log Out" link. If the verification fetch
            # itself failed (Anubis timeout, transient 5xx, network
            # blip), keep the session — a forced re-login means another
            # Anubis solve plus a login POST that could hit any future
            # rate limiter. See the AO3 client for the same pattern.
            if not html:
                return True
            self._logged_in = False

        return await self.login()

    async def validate_session(self) -> str | None:
        """Login and return the target username if successful, else None."""
        if await self.ensure_logged_in():
            return self.target_user
        return None

    # ── Works Discovery ─────────────────────────────────────────

    async def get_all_work_ids(self) -> list[dict]:
        """Scrape the target user's works page to discover all work IDs.

        Returns list of dicts with 'work_id' (int) and 'title' (str).
        """
        if not await self.ensure_logged_in():
            raise ValueError("SqW: Not authenticated")

        all_works: list[dict] = []
        page = 1
        seen_ids: set[int] = set()

        for _page_safety in range(1000):
            url = f"{_BASE}/users/{self.target_user}/works?page={page}"
            logger.info("SqW: Fetching works page %d for %s", page, self.target_user)

            html = await self._get_page(url)
            if not html:
                logger.error("SqW: Failed to fetch works page %d", page)
                break

            # Extract work IDs and titles from the listing
            # OTW Archive pattern: <h4 class="heading"><a href="/works/88335">Title</a>
            works = re.findall(
                r'<a\s+href="/works/(\d+)"[^>]*>([^<]+)</a>',
                html,
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

            # Check for next page link
            if f'page={page + 1}' not in html and 'rel="next"' not in html:
                break

            page += 1
            await asyncio.sleep(config.SQW_REQUEST_DELAY_SECONDS)

        logger.info("SqW: Found %d works for %s", len(all_works), self.target_user)
        return all_works

    # ── Work Details ────────────────────────────────────────────

    async def get_work_detail(self, work_id: int) -> dict:
        """Fetch stats and metadata for a single work.

        Parses the work page for:
          - Title, author, fandom, rating, warnings, tags
          - Stats: hits, kudos, comments, bookmarks, word_count, chapters
          - Posted date, updated date
        """
        url = f"{_BASE}/works/{work_id}?view_adult=true"

        html = await self._get_page(url)
        if not html:
            # Don't fabricate a zero-stat record — _get_page returns None on
            # timeouts / Anubis challenge / rate limits, and a zero dict would
            # flow into upsert + snapshot, clobbering the work's real cumulative
            # hit count with 0 and making the next good poll look like a huge
            # view spike in digests/milestones. Raise so get_work_details_batch
            # drops this work for the cycle; the next cycle re-reads the truth.
            raise RuntimeError(f"SqW: failed to fetch work {work_id} (no page returned)")

        detail: dict = {"work_id": work_id}

        # Title — <h2 class="title heading">Title</h2>
        # Restricted works have an <img> tag inside, so capture full h2 content
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

        # Summary — may have an <h3>Summary:</h3> before the blockquote
        m = re.search(
            r'class="summary[^"]*"[^>]*>.*?<blockquote[^>]*>(.*?)</blockquote>',
            html, re.DOTALL,
        )
        if m:
            summary_html = m.group(1).strip()
            detail["description"] = re.sub(r'<[^>]+>', '', summary_html).strip()
        else:
            detail["description"] = ""

        # Tags/keywords — collect all freeform and other tags
        tags = re.findall(r'class="tag"[^>]*>([^<]+)</a>', html)
        detail["keywords"] = [unescape(t.strip()) for t in tags]

        # ── Stats extraction ────────────────────────────────
        # OTW Archive uses <dl class="stats"> with <dd class="metric">value</dd>

        def _extract_stat(stat_class: str) -> int:
            """Extract an integer stat from the stats dl."""
            pattern = rf'<dd\s+class="{stat_class}"[^>]*>\s*(\d[\d,]*)\s*</dd>'
            m = re.search(pattern, html)
            if m:
                return int(m.group(1).replace(",", ""))
            # Fallback: stat might be wrapped in an <a> tag (bookmarks)
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
        # almost always an Anubis/challenge/redirect page rather than the work —
        # treat it as a fetch failure so we don't persist bogus zeros.
        if (detail["hits"] == 0 and detail["kudos_count"] == 0
                and detail["comments_count"] == 0 and detail["bookmarks_count"] == 0
                and not detail["title"]):
            raise RuntimeError(
                f"SqW: work {work_id} parsed to all-zero stats with no title — "
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

        # Posted date
        m = re.search(r'class="published"[^>]*>(\d{4}-\d{2}-\d{2})</dd>', html)
        detail["posted_at"] = m.group(1) if m else None

        # Updated date
        m = re.search(r'class="status"[^>]*>(\d{4}-\d{2}-\d{2})</dd>', html)
        detail["updated_date"] = m.group(1) if m else detail.get("posted_at")

        # Link
        detail["link"] = f"{_BASE}/works/{work_id}"

        # Map to consistent schema column names
        detail["views"] = detail["hits"]
        detail["favorites_count"] = detail["kudos_count"]

        return detail

    async def get_work_details_batch(self, work_ids: list[int]) -> list[dict]:
        """Fetch details for multiple works with rate limiting."""
        details = []
        for i, work_id in enumerate(work_ids):
            if i > 0:
                await asyncio.sleep(config.SQW_REQUEST_DELAY_SECONDS)
            try:
                detail = await self.get_work_detail(work_id)
                details.append(detail)
            except Exception as e:
                logger.warning("SqW: Failed to fetch work %d: %s", work_id, e)
        return details

    # ── Kudos Users ─────────────────────────────────────────────

    async def get_kudos_users(self, work_id: int) -> list[str]:
        """Extract the list of users who left kudos on a work.

        OTW Archive shows kudos users at the bottom of the work page
        in a <p class="kudos"> section.
        """
        url = f"{_BASE}/works/{work_id}?view_adult=true"
        html = await self._get_page(url)
        if not html:
            return []

        # Find kudos section: <p class="kudos">...<a href="/users/name">name</a>...
        kudos_section = re.search(
            r'id="kudos"[^>]*>(.*?)</p>', html, re.DOTALL,
        )
        if not kudos_section:
            kudos_section = re.search(
                r'class="kudos"[^>]*>(.*?)</p>', html, re.DOTALL,
            )
        if not kudos_section:
            return []

        # Extract usernames from links
        users = re.findall(
            r'<a\s+href="/users/([^"]+)"', kudos_section.group(1),
        )
        # Return just registered user names
        return [unescape(u) for u in users]

    # ── Work Skins ──────────────────────────────────────────────

    async def find_work_skin_by_title(self, title: str) -> str | None:
        """Look up an existing Work Skin owned by the current user by title.

        Returns the skin_id as a string, or None if not found.
        """
        if not self._logged_in:
            await self.ensure_logged_in()

        # KnaughtyKat's work skins are listed at /users/<user>/skins/work_skins
        # Each row in the table has a link of the form /skins/{id}/edit
        url = f"{_BASE}/users/{self.username}/skins?skin_type=WorkSkin"
        resp = await self._http.get(url)
        if resp.status_code != 200:
            return None
        html = resp.text

        # Find skin entries: each has a heading with the title and a link to edit
        # Pattern: <h5 class="heading"><a href="/skins/12345">Title</a></h5>
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
        """Create a new Work Skin on SquidgeWorld.

        Args:
            title: Skin title (visible in dropdowns).
            css: The CSS source. Should be scoped to #workskin (OTW Archive
                automatically wraps work content in <div id="workskin">).
            description: Optional skin description.
            public: If True, requests public visibility (requires admin approval).
            role: "user" (add to archive skin) or "override" (replace).

        Returns:
            Dict with 'skin_id' and 'url'.
        """
        if not self._logged_in:
            if not await self.ensure_logged_in():
                raise RuntimeError("SqW: Not logged in")

        # GET form for CSRF
        form_url = f"{_BASE}/skins/new?skin_type=WorkSkin"
        form_resp = await self._http.get(form_url)
        if form_resp.status_code != 200:
            raise RuntimeError(f"SqW: Could not load skin form (status {form_resp.status_code})")

        token_m = re.search(
            r'name="authenticity_token"[^>]*value="([^"]+)"', form_resp.text
        )
        if not token_m:
            token_m = re.search(
                r'value="([^"]+)"[^>]*name="authenticity_token"', form_resp.text
            )
        if not token_m:
            raise RuntimeError("SqW: Could not get CSRF token from skin form")
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

        await asyncio.sleep(config.SQW_REQUEST_DELAY_SECONDS)
        resp = await self._http.post(
            f"{_BASE}/skins",
            content=body,
            headers={
                "Referer": form_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )

        final_url = str(resp.url)
        # Successful create redirects to /skins/{id}
        skin_match = re.search(r'/skins/(\d+)(?:[/?]|$)', final_url)
        if skin_match:
            skin_id = skin_match.group(1)
            logger.info("SqW: Created Work Skin %s — %s", skin_id, title)
            return {"skin_id": skin_id, "url": f"{_BASE}/skins/{skin_id}", "title": title}

        # Look for validation errors
        errors = re.findall(
            r'<(?:li|div)[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)</(?:li|div)>',
            resp.text, re.DOTALL,
        )
        err_text = "; ".join(re.sub(r"<[^>]+>", "", e).strip()[:200] for e in errors[:5])
        raise RuntimeError(
            f"SqW: Skin creation failed. status={resp.status_code} url={final_url} "
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
            logger.info("SqW: Reusing existing Work Skin %s — %s", existing, title)
            return existing
        result = await self.create_work_skin(title=title, css=css, description=description)
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

        Uses the safe form-fetch pattern: GET /skins/{id}/edit, extract every
        skin[*] field with its current value, override only the requested
        fields, then POST back with `_method=patch` and `commit=Update`.

        Args:
            skin_id: The skin ID.
            title: New skin title (None = keep current).
            description: New skin description (None = keep current).
            css: New CSS source (None = keep current).
            public: Set public visibility (None = keep current).

        Returns:
            Dict with 'skin_id' and 'url'.
        """
        if not self._logged_in:
            if not await self.ensure_logged_in():
                raise RuntimeError("SqW: Not logged in")

        edit_url = f"{_BASE}/skins/{skin_id}/edit"
        form_resp = await self._http.get(edit_url)
        if form_resp.status_code != 200:
            raise RuntimeError(f"SqW: Could not load skin edit form (status {form_resp.status_code})")
        html = form_resp.text

        token_m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', html)
        if not token_m:
            token_m = re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', html)
        if not token_m:
            raise RuntimeError("SqW: Could not find CSRF token in skin edit form")
        token = token_m.group(1)

        # Scope to the skin form
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

        # Collect every skin[*] field as it currently is
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

        # Apply overrides
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

        await asyncio.sleep(config.SQW_REQUEST_DELAY_SECONDS)
        resp = await self._http.post(
            f"{_BASE}/skins/{skin_id}",
            content=body,
            headers={
                "Referer": edit_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"SqW: Skin edit failed — status {resp.status_code}")
        if "have not been saved" in resp.text:
            raise RuntimeError("SqW: Skin edit POST returned but flash says 'changes have not been saved'")

        logger.info("SqW: Updated Work Skin %s", skin_id)
        return {"skin_id": skin_id, "url": f"{_BASE}/skins/{skin_id}"}

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
        """Add a new chapter to an existing work.

        SAFETY: By default this uses `preview_button=Preview` and then submits
        the preview form's `save_button=Save As Draft` so a draft work STAYS
        a draft. Set `publish=True` to use `post_without_preview_button=Post`
        which will publish the entire work along with the new chapter.

        For PUBLISHED works, you almost always want `publish=True` to add the
        chapter to the live work without it disappearing into a draft state.

        For DRAFT works, leave `publish=False` (the default) so the chapter
        joins the draft without unintentionally publishing the whole work.

        Args:
            work_id: The work to add the chapter to.
            title: Chapter title.
            content: HTML content of the chapter.
            position: Optional position (1 = first, etc). None lets OTW append.
            summary: Optional chapter summary.
            notes_begin: Optional beginning notes.
            notes_end: Optional end notes.
            publish: If True, uses post_without_preview_button (publishes the
                work). If False (default), uses preview_button + save_button
                for safe draft-preserving behavior.

        Returns:
            Dict with 'chapter_id' (if extractable) and the response URL.
        """
        if not self._logged_in:
            if not await self.ensure_logged_in():
                raise RuntimeError("SqW: Not logged in")

        form_url = f"{_BASE}/works/{work_id}/chapters/new"
        form_resp = await self._http.get(form_url)
        if form_resp.status_code != 200:
            raise RuntimeError(f"SqW: Could not load chapter form (status {form_resp.status_code})")
        html = form_resp.text

        token_m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', html)
        if not token_m:
            token_m = re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', html)
        if not token_m:
            raise RuntimeError("SqW: Could not get CSRF token from chapter form")
        token = token_m.group(1)

        # Pseud ID for the chapter author
        pseud_m = re.search(
            r'<input[^>]*value="(\d+)"[^>]*name="chapter\[author_attributes\]\[ids\]\[\]"',
            html,
        ) or re.search(
            r'<input[^>]*name="chapter\[author_attributes\]\[ids\]\[\]"[^>]*value="(\d+)"',
            html,
        )
        if not pseud_m:
            raise RuntimeError("SqW: Could not extract chapter author pseud ID")
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

        # Button selection:
        #   publish=False (safe default): preview_button — adds chapter to work
        #     and leaves the work in its current state (draft stays draft).
        #     No follow-up POST needed; the chapter is fully created by this
        #     single request, verified by tests/test_chapter_after_preview_only.py
        #   publish=True: post_without_preview_button — publishes the entire
        #     work along with the new chapter. ONLY use on already-published
        #     works (chapter add to a draft will publish the draft).
        if publish:
            form_data.append(("post_without_preview_button", "Post"))
        else:
            form_data.append(("preview_button", "Preview"))

        body = urlencode(form_data, doseq=True)

        await asyncio.sleep(config.SQW_REQUEST_DELAY_SECONDS)
        resp = await self._http.post(
            f"{_BASE}/works/{work_id}/chapters",
            content=body,
            headers={
                "Referer": form_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"SqW: Chapter creation failed — status {resp.status_code}")

        # Check for validation errors
        if "Sorry! We couldn" in resp.text:
            errors = re.findall(
                r'<(?:li|div)[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)</(?:li|div)>',
                resp.text, re.DOTALL,
            )
            err_text = "; ".join(re.sub(r"<[^>]+>", "", e).strip()[:200] for e in errors[:5])
            raise RuntimeError(f"SqW: Chapter creation failed: {err_text or '(none parsed)'}")

        # Extract the new chapter_id from the response URL
        # Successful preview redirects to /works/{work_id}/chapters/{ch_id}/preview
        # Successful post redirects to /works/{work_id}/chapters/{ch_id}
        final_url = str(resp.url)
        ch_match = re.search(rf'/works/{work_id}/chapters/(\d+)', final_url)
        chapter_id = ch_match.group(1) if ch_match else ""

        if not chapter_id:
            raise RuntimeError(
                f"SqW: Could not extract chapter_id from response URL: {final_url}"
            )

        logger.info(
            "SqW: Added chapter to work %s — chapter_id=%s publish=%s",
            work_id, chapter_id, publish,
        )
        return {
            "chapter_id": chapter_id,
            "work_id": work_id,
            "url": final_url,
            "published": publish,
        }

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
        language_id: str = "15",  # 15 = English on SquidgeWorld
        chapter_title: str = "",
        work_skin_id: str = "",
        # Backwards-compat single-value parameters
        warning: str | None = None,
        category: str | None = None,
    ) -> dict:
        """Create a new work on SquidgeWorld as a DRAFT (preview state).

        OTW Archive form at /works/new requires CSRF token and specific field
        names. The pseud ID for the author must be extracted from the form
        and included in `work[author_attributes][ids][]`.

        Uses `preview_button` so the work lands in the user's drafts at
        /works/{id}/preview without being published. Click "Post" on the
        preview page (or call confirm_post()) to publish.

        Args:
            title: Work title.
            content: HTML chapter content (first chapter body).
            fandom: Fandom name (default: "Original Work").
            rating: "General Audiences", "Teen And Up Audiences", "Mature", "Explicit".
            warnings: List of canonical archive warnings. Each must be one of:
                "Choose Not To Use Archive Warnings", "Graphic Depictions Of Violence",
                "Major Character Death", "No Archive Warnings Apply",
                "Rape/Non-Con", "Underage", "Suicide/Suicidal Ideation",
                "Incest and/or Incestuous Relationship(s)".
                Defaults to ["No Archive Warnings Apply"].
            categories: List of relationship categories. Each must be one of:
                "F/F", "F/M", "Gen", "M/M", "Multi", "NB/F", "NB/M", "NB/NB",
                "Other", "QPR", "Vs. / Antagonistic". Defaults to [].
            relationship: Comma-separated relationship tags.
            characters: Comma-separated character tags.
            additional_tags: Comma-separated additional tags.
            summary: Work summary (HTML allowed, 1250 char max).
            notes_begin: Beginning notes.
            notes_end: End notes.
            language_id: Numeric language ID (15 = English on SquidgeWorld).
            chapter_title: Optional title for the first chapter.
            work_skin_id: Optional skin ID to apply to this work.
            warning: (deprecated) Single warning string. Use `warnings` list instead.
            category: (deprecated) Single category string. Use `categories` list instead.

        Returns:
            Dict with 'work_id' and 'url'.
        """
        # Backwards compat: accept old single-value parameters
        if warnings is None:
            warnings = [warning] if warning else ["No Archive Warnings Apply"]
        if categories is None:
            categories = [category] if category else []

        if not self._logged_in:
            if not await self.ensure_logged_in():
                raise RuntimeError("SqW: Not logged in")

        # GET the new work form to extract CSRF token AND the author pseud ID.
        # The pseud ID is required (work must have at least one creator) and
        # is unique per user, so we extract it from the form HTML.
        form_resp = await self._http.get(f"{_BASE}/works/new")
        form_html = form_resp.text

        token_m = re.search(
            r'name="authenticity_token"[^>]*value="([^"]+)"', form_html
        )
        if not token_m:
            raise RuntimeError("SqW: Could not get CSRF token from /works/new")
        token = token_m.group(1)

        # Pseud ID input — attribute order in HTML can vary, so try both
        # value-then-name and name-then-value layouts.
        pseud_m = re.search(
            r'<input[^>]*value="(\d+)"[^>]*name="work\[author_attributes\]\[ids\]\[\]"',
            form_html,
        ) or re.search(
            r'<input[^>]*name="work\[author_attributes\]\[ids\]\[\]"[^>]*value="(\d+)"',
            form_html,
        )
        if not pseud_m:
            raise RuntimeError("SqW: Could not extract author pseud ID from /works/new")
        pseud_id = pseud_m.group(1)

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
            ("work[chapter_attributes][content]", content),
            ("preview_button", "Preview"),
        ])

        # Encode manually because httpx 0.28.1 AsyncClient has a bug with
        # list-of-tuples `data=` (raises "sync request with an AsyncClient").
        # urlencode(doseq=True) handles duplicate keys correctly for the
        # array-style fields like work[archive_warning_strings][].
        from urllib.parse import urlencode
        body = urlencode(form_data, doseq=True)

        await asyncio.sleep(config.SQW_REQUEST_DELAY_SECONDS)
        resp = await self._http.post(
            f"{_BASE}/works",
            content=body,
            headers={
                "Referer": f"{_BASE}/works/new",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )

        # After preview, need to confirm by POSTing again
        final_url = str(resp.url)
        if "/works/new" in final_url or resp.status_code >= 400:
            # Check for error messages
            errors = re.findall(r'class="error"[^>]*>(.*?)</li>', resp.text, re.DOTALL)
            err_text = "; ".join(re.sub(r'<[^>]+>', '', e).strip() for e in errors[:3])
            raise RuntimeError(f"SqW: Work creation failed: {err_text or 'unknown error'}")

        # Try to find the confirm/post button and submit
        confirm_token = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', resp.text)
        if confirm_token and "post_button" not in resp.text.lower():
            # We're on the preview page, need to click Post
            pass

        # Extract work ID from URL
        work_match = re.search(r'/works/(\d+)', final_url)
        if work_match:
            work_id = work_match.group(1)
            url = f"{_BASE}/works/{work_id}"
            logger.info("SqW: Created work %s — %s", work_id, url)
            return {"work_id": work_id, "url": url}

        # Postmortem: dump the response body to the OS temp dir so we can
        # refine the parser if OTW changes its create-flow response shape.
        # (Was a hardcoded personal path — failed to write on the Linux server
        # and littered the repo root on desktop.)
        import tempfile, time
        debug_path = f"{tempfile.gettempdir()}/sqw_create_debug_{int(time.time())}.html"
        try:
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(f"<!-- final_url: {final_url} -->\n")
                f.write(f"<!-- status: {resp.status_code} -->\n")
                f.write(resp.text)
            logger.error("SqW: Response body saved to %s for inspection", debug_path)
        except Exception as e:
            logger.error("SqW: Could not save debug body: %s", e)

        # Look for error messages anywhere in the page
        errors = re.findall(r'class="[^"]*error[^"]*"[^>]*>(.*?)</', resp.text, re.DOTALL)
        err_text = "; ".join(re.sub(r'<[^>]+>', '', e).strip()[:200] for e in errors[:5])

        raise RuntimeError(
            f"SqW: Could not extract work ID from {final_url} "
            f"(status={resp.status_code}, errors={err_text or 'none found'}, "
            f"debug body: {debug_path})"
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
        """Edit metadata on an existing SquidgeWorld work.

        Uses the safe form-fetch pattern: GET the edit form, extract every
        current field value, modify only the requested fields, then POST the
        full form back. This avoids Rails-PATCH-clears-omitted-fields issues.

        Submits with `save_button=Save As Draft` (or `post_button=Post` if
        save_as_draft=False) — `preview_button` does NOT persist edits.

        Args:
            work_id: The work ID on SquidgeWorld.
            title: New title (None = keep current).
            summary: New summary (None = keep current).
            additional_tags: Comma-separated additional tags (None = keep).
            notes_begin: Beginning notes (None = keep).
            notes_end: End notes (None = keep).
            warnings: List of canonical warnings (None = keep).
            categories: List of categories (None = keep).
            relationship: Comma-separated relationships (None = keep).
            characters: Comma-separated characters (None = keep).
            fandom: Fandom name (None = keep).
            rating: Rating string (None = keep).
            work_skin_id: Skin ID to apply (None = keep).
            save_as_draft: If True, saves as draft. If False, publishes.

        Returns:
            Dict with 'work_id' and 'url'.
        """
        if not self._logged_in:
            if not await self.ensure_logged_in():
                raise RuntimeError("SqW: Not logged in")

        edit_url = f"{_BASE}/works/{work_id}/edit"
        form_resp = await self._http.get(edit_url)
        if form_resp.status_code != 200:
            raise RuntimeError(f"SqW: Could not load edit form (status {form_resp.status_code})")
        form_html = form_resp.text

        token, current_fields = _extract_work_form_fields(form_html)

        # Apply overrides
        # For array fields (warnings, categories) we replace the whole set.
        # For scalar fields we update in place.
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
                        new_fields.append((name, ""))  # hidden empty placeholder
                        for w in warnings:
                            new_fields.append((name, w))
                        warnings_handled = True
                    # Skip subsequent original entries for this field
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

        # If a setter was given but the field wasn't in the form, append it
        if work_skin_id is not None and not any(n == "work[work_skin_id]" for n, _ in new_fields):
            new_fields.append(("work[work_skin_id]", work_skin_id))

        # Build the submit body
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

        await asyncio.sleep(config.SQW_REQUEST_DELAY_SECONDS)
        resp = await self._http.post(
            f"{_BASE}/works/{work_id}",
            content=body,
            headers={
                "Referer": edit_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"SqW: Edit failed — status {resp.status_code}")

        # Check the flash message — OTW returns 200 even when nothing was saved
        if "have not been saved" in resp.text:
            raise RuntimeError(
                "SqW: Edit POST returned but flash says 'changes have not been saved' "
                "(wrong submit button or validation error)"
            )

        # Look for explicit success flash. If not present and no errors,
        # log a warning so the caller can check.
        success_patterns = [
            "successfully updated",
            "Work was successfully",
            "updated successfully",
        ]
        if not any(p in resp.text for p in success_patterns):
            # Capture validation errors if any
            err_block = re.search(
                r'<(?:div|ul)[^>]*id="error"[^>]*>(.*?)</(?:div|ul)>',
                resp.text, re.DOTALL,
            )
            err_text = ""
            if err_block:
                err_text = re.sub(r"<[^>]+>", " ", err_block.group(1)).strip()[:300]
            else:
                # Look for any flash message
                flash = re.search(
                    r'<div[^>]*class="[^"]*flash[^"]*"[^>]*>(.*?)</div>',
                    resp.text, re.DOTALL,
                )
                if flash:
                    err_text = re.sub(r"<[^>]+>", " ", flash.group(1)).strip()[:300]
            raise RuntimeError(
                f"SqW: Edit POST returned 200 but no success flash found. "
                f"flash/errors: {err_text or '(none parsed)'}"
            )

        url = f"{_BASE}/works/{work_id}"
        logger.info("SqW: Edited work %s", work_id)
        return {"work_id": work_id, "url": url}

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
    ) -> dict:
        """Edit a chapter using the safe form-fetch pattern.

        GETs /works/{work_id}/chapters/{chapter_id}/edit, extracts every
        chapter[*] field with its current value, overrides only the
        requested fields, and POSTs back with the appropriate save button.

        Button selection:
          - If save_button (Save As Draft) is present (draft work) → use it
          - Otherwise use post_without_preview_button (saves to published work)

        Pre-processes HTML to collapse internal whitespace within tags
        because OTW Archive converts newlines inside HTML elements to <br/>.
        """
        if not self._logged_in:
            if not await self.ensure_logged_in():
                raise RuntimeError("SqW: Not logged in")

        edit_url = f"{_BASE}/works/{work_id}/chapters/{chapter_id}/edit"
        form_resp = await self._http.get(edit_url)
        if form_resp.status_code != 200:
            raise RuntimeError(f"SqW: Could not load chapter edit form (status {form_resp.status_code})")
        html = form_resp.text

        token_m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', html)
        if not token_m:
            token_m = re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', html)
        if not token_m:
            raise RuntimeError("SqW: Could not find CSRF token in chapter edit form")
        token = token_m.group(1)

        # Scope to the chapter form (action contains /chapters/)
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

        # Extract every chapter[*] field with current value
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

        # Determine which submit button to use based on form availability
        # save_button = Save As Draft (draft works only)
        # post_without_preview_button = Post (saves on published works, publishes draft works)
        button_name = "post_without_preview_button"
        button_value = "Post"
        if 'name="save_button"' in form_body:
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

        await asyncio.sleep(config.SQW_REQUEST_DELAY_SECONDS)
        resp = await self._http.post(
            f"{_BASE}/works/{work_id}/chapters/{chapter_id}",
            content=body,
            headers={
                "Referer": edit_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"SqW: Chapter edit failed — status {resp.status_code}")

        # Strict success check
        if "have not been saved" in resp.text:
            raise RuntimeError(
                f"SqW: Chapter edit POST returned but flash says 'changes have not been saved' "
                f"(button={button_name}, work={work_id}, chapter={chapter_id})"
            )

        success_patterns = [
            "successfully updated",
            "chapter was successfully",
            "Chapter was successfully",
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
            raise RuntimeError(
                f"SqW: Chapter edit POST returned 200 but no success flash found. "
                f"errors: {err_text or '(none parsed)'}"
            )

        logger.info(
            "SqW: Edited chapter %s of work %s via %s",
            chapter_id, work_id, button_name,
        )
        return {"work_id": work_id, "chapter_id": chapter_id}

    async def delete_work(self, work_id: str) -> bool:
        """Delete a work via the OTW confirm_delete flow.

        Returns True on success, raises on failure. USE WITH CARE - destructive.
        """
        if not self._logged_in:
            if not await self.ensure_logged_in():
                raise RuntimeError("SqW: Not logged in")

        confirm_url = f"{_BASE}/works/{work_id}/confirm_delete"
        confirm_resp = await self._http.get(confirm_url)
        if confirm_resp.status_code != 200:
            raise RuntimeError(f"SqW: Could not load confirm_delete page (status {confirm_resp.status_code})")

        token_m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', confirm_resp.text)
        if not token_m:
            token_m = re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', confirm_resp.text)
        if not token_m:
            raise RuntimeError("SqW: Could not get CSRF token from confirm_delete page")

        from urllib.parse import urlencode
        body = urlencode([
            ("authenticity_token", token_m.group(1)),
            ("_method", "delete"),
            ("commit", "Yes, Delete Work"),
        ])

        await asyncio.sleep(config.SQW_REQUEST_DELAY_SECONDS)
        resp = await self._http.post(
            f"{_BASE}/works/{work_id}",
            content=body,
            headers={
                "Referer": confirm_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"SqW: Delete failed — status {resp.status_code}")
        # Success usually shows a flash like "Work was successfully deleted"
        if "successfully deleted" not in resp.text and "has been deleted" not in resp.text:
            # Verify by GET that the work is now 404
            check = await self._http.get(f"{_BASE}/works/{work_id}", follow_redirects=False)
            if check.status_code != 404 and "successfully deleted" not in resp.text:
                logger.warning("SqW: delete_work returned %s but work %s may still exist", resp.status_code, work_id)

        logger.info("SqW: Deleted work %s", work_id)
        return True

    async def is_work_in_drafts(self, work_id: str) -> bool:
        """Check whether a work is currently listed in the user's drafts.

        Returns True if /users/{user}/works/drafts contains the work_id,
        False otherwise. Used as a safety check after operations that
        could accidentally publish.
        """
        if not self._logged_in:
            await self.ensure_logged_in()
        url = f"{_BASE}/users/{self.username}/works/drafts"
        resp = await self._http.get(url)
        if resp.status_code != 200:
            return False
        return f"/works/{work_id}" in resp.text

    async def is_work_published(self, work_id: str) -> bool:
        """Check whether a work is in the user's PUBLISHED works listing.

        Returns True if /users/{user}/works contains the work_id.
        """
        if not self._logged_in:
            await self.ensure_logged_in()
        url = f"{_BASE}/users/{self.username}/works"
        resp = await self._http.get(url)
        if resp.status_code != 200:
            return False
        return f"/works/{work_id}" in resp.text

    async def get_chapter_ids(self, work_id: str) -> list[dict]:
        """Get all chapter IDs and titles for a work."""
        url = f"{_BASE}/works/{work_id}/navigate"
        html = await self._get_page(url)
        if not html:
            return []

        # Chapter list: <li><a href="/works/{id}/chapters/{ch_id}">N. Title</a></li>
        chapters = re.findall(
            r'href="/works/\d+/chapters/(\d+)"[^>]*>(\d+)\.\s*([^<]*)',
            html,
        )
        return [
            {"chapter_id": ch_id, "index": int(idx), "title": title.strip()}
            for ch_id, idx, title in chapters
        ]
