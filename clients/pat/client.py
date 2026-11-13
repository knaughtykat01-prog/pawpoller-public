"""Patreon client — spec 084. The creator's signed-in session against Patreon's own web JSON API.

Patreon has no post API, so PawPoller posts and reads the way Patreon's own web app does (research R1, R2, R6, R11,
mapped live on 13 Nov 2026 on a tester's account: drafts saved, read back and deleted, nothing published).

**Two connections, kept apart (plan FR-SEC-005).**

- ``PatClient`` talks only to ``https://www.patreon.com`` (port 443), carries the session cookies for that host
  only, follows redirects by hand and only on that host, sends at most one request a second, and stops reading any
  answer past 4 MB. Writes carry ``X-CSRF-Signature``: the token is read from a signed-in page and kept in memory
  for this client's life only, never written anywhere (FR-SEC-012).
- ``UploadClient`` sends a picture or file to Patreon's storage at the presigned address Patreon hands back. It has
  no cookie jar and no CSRF header, takes https only, only the one storage host seen on 13 Nov, and follows no
  redirects.

**Errors** are built from status codes and Patreon's ``errors[].detail`` (cut to 300 characters, tags stripped),
never from a response body, a cookie or the token. A bot check or a 429 raises ``PatChallenged`` (never "signed
out"); the scheduled poller turns that into the shared stand-down (``polling/fa_standdown.py``).

Patreon's API goes through ``httpx`` over curl_cffi presenting as current Chrome (operator, 13 Nov, research R8):
from the home box, plain ``httpx`` met Cloudflare's bot check on its connection fingerprint, while the same request
as Chrome passed. Uploads to S3 stay plain ``httpx``. Without curl_cffi the client refuses to build, as Toyhouse's
does: a plain connection would only meet the bot check. A test transport, passed in, is used as given.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
from html import unescape
from urllib.parse import urljoin, urlsplit

import httpx

try:    # research R8: Cloudflare in front of Patreon turns away a plain Python TLS fingerprint
    from httpx_curl_cffi import AsyncCurlTransport
    import warnings
    # Windows' default loop lacks add_reader; curl_cffi adds a selector thread itself and says so on every loop.
    warnings.filterwarnings("ignore", message=r"\s*Proactor event loop does not implement add_reader")
except ImportError:  # pragma: no cover - a build without it refuses (see _http)
    AsyncCurlTransport = None

logger = logging.getLogger(__name__)

BASE = "https://www.patreon.com"
_HOST = "www.patreon.com"
# shortcut: the one storage host seen on 13 Nov 2026; add another only after it is seen live.
UPLOAD_HOSTS = frozenset({"patreon-media.s3-accelerate.amazonaws.com"})
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
HTTP_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 300.0
MAX_BODY = 4 * 1024 * 1024
MAX_REDIRECTS = 5
MIN_GAP_S = 1.0                 # at most one request a second
DETAIL_MAX = 300
MEMBER_PAGE = 500
MAX_MEMBER_PAGES = 40           # 20,000 members; a bigger page reads as partial, never as a short count
POST_PAGE = 20
MAX_POST_PAGES = 50             # the newest 1,000 posts
COMMENT_PAGE = 10
MAX_COMMENT_PAGES = 10          # 100 top-level comments a post, with their replies
COMMENT_MAX = 5000
MEDIA_WAIT_S = 90
MEDIA_POLL_S = 2
MEDIA_TRIES = int(MEDIA_WAIT_S // (MEDIA_POLL_S + 1))   # each check also waits its turn (~1 s pacing)
JSONAPI = "application/vnd.api+json"
OFFERED_KINDS = ("public", "patrons", "tier")      # an allow-list: nothing else is ever offered or accepted
_ID_RE = re.compile(r"^\d{1,20}$")
_CSRF_RE = re.compile(r'<meta[^>]*name="csrf-token"[^>]*content="([^"]+)"|<meta[^>]*content="([^"]+)"[^>]*name="csrf-token"')
_TAG_RE = re.compile(r"<[^>]+>")


class PatAuthError(Exception):
    """Signed out, no creator page, or the session belongs to a different page."""


class PatChallenged(Exception):
    """Patreon's bot check or rate limit turned PawPoller away. Never a sign-in problem."""


class PatUnavailable(Exception):
    """Patreon answered with an error, or an answer PawPoller can't use. ``code`` is Patreon's own error code."""

    def __init__(self, message: str = "", code: str = ""):
        super().__init__(message)
        self.code = code


class PatMissing(PatUnavailable):
    """This install lacks the Chrome-like connection Patreon needs: said as it is, never as "try again"."""


class PatPartial(PatUnavailable):
    """A paged read stopped before its end. A partial read is a failed read (no numbers written)."""


def same_page(a: str, b: str) -> bool:
    return bool((a or "").strip()) and (a or "").strip().lower() == (b or "").strip().lower()


def plain_text(text: str, limit: int) -> str:
    s = unescape(_TAG_RE.sub("", str(text or "")))
    return re.sub(r"[ \t]+", " ", s).strip()[:limit]


def error_detail(body: bytes) -> str:
    """Patreon's own reason, as plain text and short: the first ``errors[].detail`` (or its code)."""
    try:
        errs = json.loads(body.decode("utf-8", "replace")).get("errors") or []
        first = errs[0] if errs and isinstance(errs[0], dict) else {}
        return plain_text(first.get("detail") or first.get("code_name") or first.get("title") or "", DETAIL_MAX)
    except (ValueError, AttributeError, TypeError):
        return ""


def error_code(body: bytes) -> str:
    try:
        errs = json.loads(body.decode("utf-8", "replace")).get("errors") or []
        return str((errs[0] or {}).get("code_name") or "") if errs and isinstance(errs[0], dict) else ""
    except (ValueError, AttributeError, TypeError):
        return ""


SLOW_DOWN = "Patreon asked PawPoller to slow down."
BOT_CHECK = "Patreon's bot check turned PawPoller away."
# Said while the stand-down runs, after either one. posting/manager.py's no-retry list matches BOT_CHECK and PAUSED.
PAUSED = "Patreon turned PawPoller away recently, so PawPoller is waiting. Next try around {when}."


CAPTION_MAX = 140          # Patreon's own limit on a picture caption (the editor shows it)
ALT_MAX = 1000
# Where Patreon serves a picture from; an in-text picture's address must be one of these (plan in-text row).
MEDIA_HOST_SUFFIXES = (".patreonusercontent.com",)


def patreon_time(when) -> str:
    """A UTC time as Patreon's editor and PostyBirb send it: seconds, no milliseconds, no zone."""
    from datetime import timezone
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def parse_time(text):
    """An aware UTC datetime from an ISO time (with "Z", an offset, or none = UTC), or None."""
    from datetime import datetime, timezone
    s = str(text or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def same_instant(a, b) -> bool:
    """Patreon answers "...T03:35:02.000+00:00" for what was sent as "...T03:35:02": compare as instants."""
    x, y = parse_time(a) if not hasattr(a, "tzinfo") else a, parse_time(b) if not hasattr(b, "tzinfo") else b
    return x is not None and y is not None and abs((x - y).total_seconds()) < 1


def is_challenge(status: int, headers, body: bytes) -> bool:
    if status == 429:
        return True
    return status in (403, 503) and (bool(headers.get("cf-mitigated")) or b"Just a moment" in body[:4000])


def post_link(post_id) -> str:
    """The only link PawPoller keeps or shows for a post: built from the numeric id (FR-SEC-016)."""
    pid = str(post_id or "")
    return f"{BASE}/posts/{pid}" if _ID_RE.match(pid) else ""


class _Answer:
    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers, body: bytes):
        self.status, self.headers, self.body = status, headers, body

    def json(self) -> dict:
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise PatUnavailable("Patreon sent an answer PawPoller couldn't read.") from None
        if not isinstance(data, dict):
            raise PatUnavailable("Patreon sent an answer PawPoller couldn't read.")
        return data


async def _read_capped(r: httpx.Response) -> bytes:
    """The body, stopped past MAX_BODY while it is read (the size header first, then the bytes themselves)."""
    try:
        declared = int(r.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_BODY:
        raise PatUnavailable("Patreon's answer was too large to read.")
    out = bytearray()
    async for chunk in r.aiter_bytes():
        out += chunk
        if len(out) > MAX_BODY:
            raise PatUnavailable("Patreon's answer was too large to read.")
    return bytes(out)


class PatClient:
    def __init__(self, cookie: str = "", *, transport: httpx.AsyncBaseTransport | None = None,
                 sleep=asyncio.sleep, upload_transport: httpx.AsyncBaseTransport | None = None):
        self.cookie = (cookie or "").strip()
        self._transport = transport
        self._upload_transport = upload_transport
        self._sleep = sleep
        self._client: httpx.AsyncClient | None = None
        self._csrf = ""
        self._last = 0.0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        self._csrf = ""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _cookies(self) -> dict[str, str]:
        if "=" not in self.cookie:          # a bare value is the session cookie
            return {"session_id": self.cookie} if self.cookie else {}
        out = {}
        for part in self.cookie.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip():
                    out[k.strip()] = v.strip()
        return out

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            jar = httpx.Cookies()
            for k, v in self._cookies().items():
                jar.set(k, v, domain=_HOST)          # www.patreon.com only: never sent anywhere else
            transport, headers = self._transport, {"User-Agent": USER_AGENT}
            if transport is None:
                if AsyncCurlTransport is None:
                    raise PatMissing("This install is missing the browser-matching connection Patreon needs "
                                         "(curl_cffi). Reinstall or update PawPoller.")
                # Chrome's own headers come with Chrome's fingerprint; a User-Agent of ours would contradict it.
                transport, headers = AsyncCurlTransport(impersonate="chrome", default_headers=True), {}
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False, cookies=jar,
                                             headers=headers, transport=transport)
        return self._client

    @staticmethod
    def _host_ok(url: str) -> bool:
        p = urlsplit(url)
        try:
            port = p.port
        except ValueError:
            return False
        return p.scheme == "https" and p.hostname == _HOST and port in (None, 443)

    async def _pace(self) -> None:
        wait = self._last + MIN_GAP_S - time.monotonic()
        if wait > 0:
            await self._sleep(wait)
        self._last = time.monotonic()

    async def _request(self, method: str, url: str, *, body: dict | None = None, params: dict | None = None,
                       write: bool = False) -> _Answer:
        """One request, redirects followed by hand and only on www.patreon.com (FR-SEC-005)."""
        headers = {"Accept": f"{JSONAPI}, application/json, text/html;q=0.8"}
        content = None
        if body is not None:
            headers["Content-Type"] = JSONAPI
            content = json.dumps(body, separators=(",", ":")).encode("utf-8")
        if write:
            headers["X-CSRF-Signature"] = await self.csrf()
            headers["Origin"] = BASE
            headers["Referer"] = BASE + "/"
        for _ in range(MAX_REDIRECTS + 1):
            if not self._host_ok(url):
                raise PatUnavailable("Patreon sent PawPoller somewhere else; not followed.")
            await self._pace()
            async with self._http().stream(method, url, headers=headers, content=content, params=params) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    if method != "GET":
                        raise PatUnavailable(f"Patreon moved the request (HTTP {r.status_code}); not followed.")
                    url, params = urljoin(url, r.headers["location"]), None
                    continue
                data = await _read_capped(r)
            if is_challenge(r.status_code, r.headers, data):     # waiting cures a 429, not a bot check: say which
                raise PatChallenged(SLOW_DOWN if r.status_code == 429 else BOT_CHECK)
            return _Answer(r.status_code, r.headers, data)
        raise PatUnavailable("Patreon redirected too many times.")

    async def _api(self, method: str, path: str, *, body: dict | None = None, params: dict | None = None,
                   step: str = "") -> dict:
        r = await self._request(method, BASE + path, body=body, params=params, write=method != "GET")
        if r.status == 401:
            raise PatAuthError("The saved Patreon sign-in has expired. Sign in to Patreon again.")
        if r.status >= 400:
            why = error_detail(r.body)
            raise PatUnavailable(f"Patreon refused {step or 'the request'} (HTTP {r.status})" + (f": {why}" if why else "."),
                                 code=error_code(r.body)[:60])
        if r.status == 204 or not r.body:
            return {}
        return r.json()

    # ── session ──────────────────────────────────────────────────────────────

    async def csrf(self) -> str:
        """The signed-in page's CSRF token, memory only. No token = not signed in."""
        if not self._csrf:
            r = await self._request("GET", BASE + "/home")
            if r.status >= 400:
                raise PatUnavailable(f"Patreon didn't answer normally (HTTP {r.status}). Try again later.")
            m = _CSRF_RE.search(r.body.decode("utf-8", "replace"))
            token = (m.group(1) or m.group(2)) if m else ""
            if not token:
                raise PatAuthError("The saved Patreon sign-in has expired. Sign in to Patreon again.")
            self._csrf = token
        return self._csrf

    async def current_campaign(self) -> dict:
        """{user_id, campaign_id, vanity, is_nsfw, currency, name} for the signed-in creator (research R1 step 2).
        Raises PatAuthError when signed out or when the account has no creator page."""
        if not self.cookie:
            raise PatAuthError("No Patreon sign-in is saved.")
        d = await self._api("GET", "/api/current_user", step="the account check", params={
            "include": "campaign", "fields[campaign]": "vanity,is_nsfw,currency,name", "fields[user]": "vanity",
            "json-api-version": "1.0"})
        rel = (((d.get("data") or {}).get("relationships") or {}).get("campaign") or {}).get("data") or {}
        cid = str(rel.get("id") or "")
        camp = next((i for i in d.get("included") or [] if isinstance(i, dict) and i.get("type") == "campaign"
                     and str(i.get("id")) == cid), None)
        if not _ID_RE.match(cid) or camp is None:
            raise PatAuthError("This Patreon account has no creator page.")
        a = camp.get("attributes") or {}
        return {"user_id": str((d.get("data") or {}).get("id") or ""), "campaign_id": cid,
                "vanity": plain_text(a.get("vanity") or "", 100), "is_nsfw": bool(a.get("is_nsfw")),
                "currency": str(a.get("currency") or "").upper()[:3], "name": plain_text(a.get("name") or "", 200)}

    async def validate_session(self, expected_page: str = "", expected_campaign: str = "") -> dict:
        """The shared session-check shape {ok, logged_in, username, expected, matches, detail}. A bot check raises
        PatChallenged (said as such, never "signed out")."""
        try:
            camp = await self.current_campaign()
        except PatAuthError as e:
            return {"ok": False, "logged_in": False, "username": "", "expected": expected_page, "matches": False,
                    "detail": str(e)}
        matches = ((not expected_campaign or camp["campaign_id"] == expected_campaign)
                   and (not expected_page or same_page(camp["vanity"], expected_page)))
        return {"ok": matches, "logged_in": True, "username": camp["vanity"], "expected": expected_page,
                "matches": matches, "detail": "" if matches else
                f"This Patreon sign-in is for the page {camp['vanity'] or camp['campaign_id']}, not the saved one."}

    # ── audience, figures, members, posts, comments, collections ────────────

    @staticmethod
    def _check_id(value, what: str) -> str:
        v = str(value or "")
        if not _ID_RE.match(v):
            raise PatUnavailable(f"PawPoller doesn't know which {what} this is.")
        return v

    async def audience_rules(self, campaign_id: str) -> list[dict]:
        """The page's audience choices (research R1 step 3): [{rule_id, kind, tier_id, title, amount_cents}], only
        ``public``, ``patrons`` and ``tier`` rules. ``non_member``, ``min_cents_pledged``, Patreon's negative-id
        "everyone" placeholder and any kind it adds later are dropped."""
        cid = self._check_id(campaign_id, "creator page")
        d = await self._api("GET", f"/api/campaigns/{cid}", step="the tier list", params={
            "include": "access_rules.tier.null,rewards",
            "fields[access_rule]": "access_rule_type,amount_cents",
            "fields[reward]": "title,amount_cents,patron_count,published,currency", "json-api-version": "1.0"})
        inc = [i for i in d.get("included") or [] if isinstance(i, dict)]
        rewards = {str(i.get("id")): i.get("attributes") or {} for i in inc
                   if i.get("type") == "reward" and _ID_RE.match(str(i.get("id") or ""))}
        out = []
        for i in inc:
            if i.get("type") != "access-rule" or not _ID_RE.match(str(i.get("id") or "")):
                continue
            kind = str((i.get("attributes") or {}).get("access_rule_type") or "")
            if kind not in OFFERED_KINDS:
                continue
            row = {"rule_id": str(i["id"]), "kind": kind, "tier_id": "", "title": "", "amount_cents": 0}
            if kind == "tier":
                tid = str((((i.get("relationships") or {}).get("tier") or {}).get("data") or {}).get("id") or "")
                if tid not in rewards:
                    continue          # the placeholder (negative id) or a tier that isn't listed
                row.update(tier_id=tid, title=plain_text(rewards[tid].get("title") or "", 200) or f"Tier {tid}",
                           amount_cents=max(int(rewards[tid].get("amount_cents") or 0), 0))
            else:
                row["title"] = "Everyone (public)" if kind == "public" else "All paying members"
            out.append(row)
        return out

    async def campaign_figures(self, campaign_id: str) -> dict:
        """The campaign's own numbers and its tiers (research R3). Money stays in Patreon's own figures."""
        cid = self._check_id(campaign_id, "creator page")
        d = await self._api("GET", f"/api/campaigns/{cid}", step="the page's numbers", params={
            "include": "rewards",
            "fields[campaign]": "patron_count,paid_member_count,pledge_sum,pledge_sum_currency,currency,is_monthly,is_nsfw",
            "fields[reward]": "title,amount_cents,patron_count", "json-api-version": "1.0"})
        a = (d.get("data") or {}).get("attributes") or {}
        tiers = [{"tier_id": str(i["id"]), "title": plain_text((i.get("attributes") or {}).get("title") or "", 200),
                  "amount_cents": max(int((i.get("attributes") or {}).get("amount_cents") or 0), 0),
                  "patron_count": max(int((i.get("attributes") or {}).get("patron_count") or 0), 0)}
                 for i in d.get("included") or []
                 if isinstance(i, dict) and i.get("type") == "reward" and _ID_RE.match(str(i.get("id") or ""))]
        return {"patron_count": a.get("patron_count"), "paid_member_count": a.get("paid_member_count"),
                "pledge_sum": int(a.get("pledge_sum") or 0),
                "pledge_sum_currency": str(a.get("pledge_sum_currency") or a.get("currency") or "").upper()[:3],
                "currency": str(a.get("currency") or "").upper()[:3], "is_monthly": a.get("is_monthly", True) is not False,
                "is_nsfw": bool(a.get("is_nsfw")), "tiers": tiers}

    MEMBER_FIELDS = ("patron_status,is_free_member,is_free_trial,is_gifted,campaign_pledge_amount_cents,"
                     "pledge_relationship_start,pledge_relationship_end")

    @staticmethod
    def _cut_member(m: dict) -> dict:
        """The research R3 values and the tier id. Nothing else of a member is kept, logged or raised."""
        a = m.get("attributes") or {}
        tier = (((m.get("relationships") or {}).get("reward") or {}).get("data") or {}).get("id")
        return {"patron_status": a.get("patron_status") if a.get("patron_status") in
                ("active_patron", "former_patron", "declined_patron") else None,
                "is_free_member": bool(a.get("is_free_member")), "is_free_trial": bool(a.get("is_free_trial")),
                "is_gifted": bool(a.get("is_gifted")),
                "campaign_pledge_amount_cents": max(int(a.get("campaign_pledge_amount_cents") or 0), 0),
                "pledge_relationship_start": str(a.get("pledge_relationship_start") or "")[:40],
                "pledge_relationship_end": str(a.get("pledge_relationship_end") or "")[:40],
                "tier_id": str(tier) if tier is not None and _ID_RE.match(str(tier)) else ""}

    async def members(self, campaign_id: str) -> list[dict]:
        """Every member, cut to the counted values as each page is parsed (FR-SEC-008, FR-SEC-013). A read that
        can't reach the last page raises PatPartial."""
        cid = self._check_id(campaign_id, "creator page")
        out, offset = [], 0
        for _ in range(MAX_MEMBER_PAGES):
            try:
                d = await self._api("GET", "/api/members", step="the member list", params={
                    "filter[campaign_id]": cid, "fields[member]": self.MEMBER_FIELDS, "include": "reward",
                    "fields[reward]": "amount_cents", "page[count]": str(MEMBER_PAGE), "page[offset]": str(offset),
                    "json-api-use-default-includes": "false", "json-api-version": "1.0"})
            except PatUnavailable as e:
                raise PatPartial(f"The member list stopped part-way: {e}") from None
            try:
                out.extend(self._cut_member(m) for m in d.get("data") or [] if isinstance(m, dict))
            except (TypeError, ValueError):     # a fixed sentence: the record's values never reach a log or report
                raise PatPartial("A member record on Patreon couldn't be read, so no supporter numbers this poll.") from None
            nxt = (((d.get("meta") or {}).get("pagination") or {}).get("offsets") or {}).get("next")
            if nxt is None:
                return out
            try:
                nxt = int(nxt)
            except (TypeError, ValueError):
                raise PatPartial("The member list stopped part-way.") from None
            if nxt <= offset:
                raise PatPartial("The member list stopped part-way.")
            offset = nxt
        raise PatPartial(f"This page has more than {MAX_MEMBER_PAGES * MEMBER_PAGE} members; PawPoller reads up to that.")

    async def posts(self, campaign_id: str) -> list[dict]:
        """The creator's published posts, newest first: id, title, likes, comments, when (research R6). Stops at
        MAX_POST_PAGES, or when Patreon's cursor doesn't move."""
        cid = self._check_id(campaign_id, "creator page")
        out, cursor, seen = [], None, set()
        for _ in range(MAX_POST_PAGES):
            params = {"filter[campaign_id]": cid, "filter[contains_exclusive_posts]": "true",
                      "filter[is_draft]": "false", "fields[post]": "title,like_count,comment_count,published_at,post_type",
                      "sort": "-published_at", "page[count]": str(POST_PAGE), "json-api-version": "1.0"}
            if cursor:
                params["page[cursor]"] = cursor
            d = await self._api("GET", "/api/posts", step="the post list", params=params)
            for p in d.get("data") or []:
                pid = str((p or {}).get("id") or "")
                if not _ID_RE.match(pid) or pid in seen:
                    continue
                seen.add(pid)
                a = p.get("attributes") or {}
                out.append({"submission_id": pid, "title": plain_text(a.get("title") or "", 200),
                            "favorites_count": max(int(a.get("like_count") or 0), 0),
                            "comments_count": max(int(a.get("comment_count") or 0), 0),
                            "posted_at": str(a.get("published_at") or "")[:40], "post_type": str(a.get("post_type") or "")[:40],
                            "link": post_link(pid)})
            nxt = str((((d.get("meta") or {}).get("pagination") or {}).get("cursors") or {}).get("next") or "")
            if not nxt or nxt == cursor:
                break
            cursor = nxt
        return out

    async def comments(self, post_id: str) -> list[dict]:
        """A post's comments, replies included (research R6), in the shape ``inbox_capture`` wants. Only the
        commenter's display name is asked for; the text is plain and capped; the link is built from ids."""
        pid = self._check_id(post_id, "post")
        out, cursor, seen = [], None, set()
        for _ in range(MAX_COMMENT_PAGES):
            params = {"include": "commenter,replies,replies.commenter", "fields[comment]": "body,created,is_by_creator",
                      "fields[user]": "full_name", "page[count]": str(COMMENT_PAGE), "json-api-version": "1.0"}
            if cursor:
                params["page[cursor]"] = cursor
            d = await self._api("GET", f"/api/posts/{pid}/comments", step="the comments", params=params)
            users = {str(i.get("id")): plain_text((i.get("attributes") or {}).get("full_name") or "", 100)
                     for i in d.get("included") or [] if isinstance(i, dict) and i.get("type") == "user"}
            for c in list(d.get("data") or []) + [i for i in d.get("included") or [] if isinstance(i, dict)
                                                 and i.get("type") == "comment"]:
                cid = str((c or {}).get("id") or "")
                if not _ID_RE.match(cid) or cid in seen:
                    continue
                seen.add(cid)
                a = c.get("attributes") or {}
                who = str((((c.get("relationships") or {}).get("commenter") or {}).get("data") or {}).get("id") or "")
                out.append({"comment_id": cid, "author": users.get(who, ""), "body": plain_text(a.get("body") or "", COMMENT_MAX),
                            "commented_at": str(a.get("created") or "")[:40], "is_by_creator": bool(a.get("is_by_creator")),
                            "permalink": post_link(pid)})
            nxt = str((((d.get("meta") or {}).get("pagination") or {}).get("cursors") or {}).get("next") or "")
            if not nxt or nxt == cursor:
                break
            cursor = nxt
        return out

    async def collections(self, campaign_id: str) -> list[dict]:
        cid = self._check_id(campaign_id, "creator page")
        d = await self._api("GET", "/api/collection", step="the collections", params={
            "filter[campaign_id]": cid, "fields[collection]": "title,num_posts", "json-api-version": "1.0",
            # a story's new collection holds only drafts at first (PostyBirb asks the same way; L3b proves it)
            "filter[must_contain_at_least_one_published_post]": "false"})
        return [{"id": str(c["id"]), "title": plain_text((c.get("attributes") or {}).get("title") or "", 200),
                 "num_posts": max(int((c.get("attributes") or {}).get("num_posts") or 0), 0)}
                for c in d.get("data") or [] if isinstance(c, dict) and _ID_RE.match(str(c.get("id") or ""))]

    # ── posting ──────────────────────────────────────────────────────────────

    async def create_draft(self, post_type: str) -> str:
        if post_type not in ("text_only", "image_file"):
            raise ValueError("unknown Patreon post type")
        d = await self._api("POST", "/api/posts", step="the new draft",
                            body={"data": {"type": "post", "attributes": {"post_type": post_type}}})
        return self._check_id((d.get("data") or {}).get("id"), "draft")

    async def read_post(self, post_id: str) -> dict:
        """The draft's state: {edited_at, published_at, rule_ids, content_json_string, teaser_json}. ``teaser_json``
        is the stored free preview (Patreon keeps whatever sat above the paywall node there); None when not sent.
        ``media_free`` is the editor's "Free image" switch (False = paid)."""
        pid = self._check_id(post_id, "post")
        d = await self._api("GET", f"/api/posts/{pid}", step="the draft", params={
            "include": "access_rules.tier.null", "fields[post]": "edited_at,published_at,content_json_string,teaser_text_json_string,is_header_media_free,"
                           "scheduled_for,change_visibility_at,post_type",
            "fields[access-rule]": "access_rule_type", "json-api-version": "1.0"})
        data = d.get("data") or {}
        a = data.get("attributes") or {}
        rules = ((data.get("relationships") or {}).get("access_rules") or {}).get("data") or []
        return {"edited_at": a.get("edited_at"), "published_at": a.get("published_at"),
                "content_json_string": a.get("content_json_string") or "",
                "teaser_json": a.get("teaser_text_json_string"),
                "media_free": a.get("is_header_media_free"),
                "scheduled_for": a.get("scheduled_for"), "public_at": a.get("change_visibility_at"),
                # Every id Patreon shows, unfiltered: an odd one must reach audience_matches and fail it (4.90.0
                # release review). Never sent back: save_post checks its own ids.
                "rule_ids": [str(r.get("id"))[:40] if isinstance(r, dict) else "?" for r in rules]}

    async def save_post(self, post_id: str, *, edited_at, attributes: dict, rule_ids: list[str], tags: list[str],
                        image_order: list[str], cover_media_id: str, collection_ids: list[str], notify: bool,
                        publish: bool) -> dict:
        """One PATCH: body, audience (in the relationship AND the top-level ``included``: the relationship alone
        was answered 200 and ignored, research R1 step 6), tags, picture order, cover, collections, the switch."""
        pid = self._check_id(post_id, "post")
        rules = [self._check_id(r, "audience") for r in rule_ids]
        cols = [self._check_id(c, "collection") for c in collection_ids]
        tell = bool(notify) and (publish or bool(attributes.get("scheduled_for")))   # Patreon tells them when it goes live
        meta = {"send_notifications": tell}
        attrs = dict(attributes)
        attrs["edited_at"] = edited_at
        attrs["tags"] = {"publish": bool(publish)}
        pm: dict[str, object] = {"send_notifications": tell}
        if image_order:
            pm["image_order"] = [self._check_id(m, "picture") for m in image_order]
        attrs["post_metadata"] = pm
        if cover_media_id:
            attrs["custom_preview_media_id"] = self._check_id(cover_media_id, "cover")
        tag_ids = [f"user_defined;{t}" for t in tags]
        rel = {"access-rule": {"data": [{"type": "access-rule", "id": r} for r in rules]},
               "access_rules": {"data": [{"type": "access-rule", "id": r} for r in rules]},
               "user_defined_tags": {"data": [{"type": "post_tag", "id": t} for t in tag_ids]}}
        if cols:
            rel["collections"] = {"data": [{"type": "collection", "id": c} for c in cols]}
        included = ([{"type": "access-rule", "id": r, "attributes": {}} for r in rules]
                    + [{"type": "post_tag", "id": f"user_defined;{t}", "attributes": {"value": t, "cardinality": 1}}
                       for t in tags])
        if not publish:
            meta["auto_save"] = True
        body = {"data": {"type": "post", "id": pid, "attributes": attrs, "relationships": rel},
                "included": included, "meta": meta}
        return await self._api("PATCH", f"/api/posts/{pid}", body=body,
                               step="publishing the post" if publish else "saving the draft")

    async def create_media(self, post_id: str, path: str, relationship: str, media_type: str) -> str:
        """Upload one picture or file to the draft and wait until Patreon says it's ready (research R1 step 5).
        Returns the media id."""
        pid = self._check_id(post_id, "post")
        if relationship not in ("main", "attachment", "custom-thumbnail", "inline") or media_type not in ("image", "file"):
            raise ValueError("unknown Patreon media kind")
        if not os.path.isfile(path):  # noqa: ASYNC240
            raise PatUnavailable("A file for this post is missing.")
        # A picture goes up under a neutral name (local names can carry anything); a PDF/EPUB keeps its own, since
        # that is the name a member downloads.
        name = os.path.basename(path) if media_type == "file" else "picture" + os.path.splitext(path)[1].lower()[:6]
        size = os.path.getsize(path)  # noqa: ASYNC240 — one stat of a local file
        d = await self._api("POST", "/api/media", step="the upload", body={"data": {"type": "media", "attributes": {
            "state": "pending_upload", "file_name": name, "size_bytes": size, "owner_id": pid,
            "owner_type": "post", "owner_relationship": relationship, "media_type": media_type}}})
        data = d.get("data") or {}
        mid = self._check_id(data.get("id"), "upload")
        a = data.get("attributes") or {}
        params = a.get("upload_parameters") or {}
        if not isinstance(params, dict):
            raise PatUnavailable("Patreon's upload answer wasn't as expected.")
        async with UploadClient(transport=self._upload_transport) as up:
            await up.upload(str(a.get("upload_url") or ""), {str(k): str(v) for k, v in params.items()}, path, name)
        await self.wait_ready(mid)
        return mid

    async def picture_address(self, media_id: str) -> str:
        """An uploaded picture's address for an in-text picture node: the media's ``display.url`` (b7, 13 Nov: the
        stored node's src equals it). Only an https address on Patreon's own media hosts is used."""
        mid = self._check_id(media_id, "picture")
        d = await self._api("GET", f"/api/media/{mid}", step="the picture's address",
                            params={"fields[media]": "display", "json-api-version": "1.0"})
        disp = ((d.get("data") or {}).get("attributes") or {}).get("display")
        url = str(disp.get("url") or "") if isinstance(disp, dict) else ""
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if p.scheme != "https" or not host.endswith(MEDIA_HOST_SUFFIXES) or any(c in url for c in "\"'<> "):
            raise PatUnavailable("Patreon's picture address wasn't one PawPoller knows; not used.")
        return url

    async def set_media_text(self, media_id: str, caption: str, alt: str) -> None:
        """A picture's caption and alt text, plain and capped (the editor's shape, captured 13 Nov)."""
        mid = self._check_id(media_id, "picture")
        await self._api("PATCH", f"/api/media/{mid}", step="the picture's caption", body={"data": {
            "type": "media", "attributes": {"metadata": {"caption": plain_text(caption, CAPTION_MAX),
                                                         "alt_text": plain_text(alt, ALT_MAX)}},
            "relationships": {}}})

    async def create_collection(self, campaign_id: str, title: str) -> str:
        """A new collection on the creator's page, public at once. Shape read from Patreon's own scripts (the show
        kind's create, less its show fields); proved only by L3b, so the poster keeps it behind a flag."""
        cid = self._check_id(campaign_id, "creator page")
        d = await self._api("POST", "/api/collection", step="the new collection", body={"data": {
            "type": "collection", "attributes": {"title": plain_text(title, 200), "description": "",
                                                 "post_sort_type": "old_to_new"},
            "relationships": {"campaign": {"data": {"type": "campaign", "id": cid}}}}})
        return self._check_id((d.get("data") or {}).get("id"), "collection")

    async def wait_ready(self, media_id: str) -> None:
        mid = self._check_id(media_id, "upload")
        for _ in range(MEDIA_TRIES):
            d = await self._api("GET", f"/api/media/{mid}", step="the upload check",
                                params={"fields[media]": "state", "json-api-version": "1.0"})
            state = str(((d.get("data") or {}).get("attributes") or {}).get("state") or "")
            if state == "ready":
                return
            if state == "failed":
                raise PatUnavailable("Patreon couldn't process an upload.")
            await self._sleep(MEDIA_POLL_S)
        raise PatUnavailable("Patreon didn't finish processing an upload in time.")


class UploadClient:
    """Patreon's storage: no cookie jar, no CSRF header, https only, one host, no redirects (FR-SEC-005)."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(timeout=UPLOAD_TIMEOUT, follow_redirects=False, transport=transport,
                                         headers={"User-Agent": USER_AGENT})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self._client.aclose()

    @staticmethod
    def host_ok(url: str) -> bool:
        p = urlsplit(url or "")
        try:
            port = p.port
        except ValueError:
            return False
        return p.scheme == "https" and p.hostname in UPLOAD_HOSTS and port in (None, 443)

    async def upload(self, url: str, fields: dict[str, str], path: str, name: str = "") -> None:
        if not self.host_ok(url):
            raise PatUnavailable("Patreon's upload address wasn't one PawPoller knows; not sent.")
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.lower().endswith(".epub"):
            mime = "application/epub+zip"
        with open(path, "rb") as fh:  # noqa: ASYNC230
            r = await self._client.post(url, data=fields, files={"file": (name or os.path.basename(path), fh, mime)})
        if not 200 <= r.status_code < 300:
            raise PatUnavailable(f"Patreon's storage refused the upload (HTTP {r.status_code}).")
