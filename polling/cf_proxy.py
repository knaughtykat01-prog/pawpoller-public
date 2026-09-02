"""Cloudflare Worker proxy transport for httpx.

Routes HTTP requests through a Cloudflare Worker to bypass datacenter IP
blocking on sites like DeviantArt and SoFurry.  Works at the httpx transport
layer so the rest of the code (cookies, redirects, etc.) behaves normally.

The Worker expects two headers:
  - x-proxy-key:  shared secret for authentication
  - x-target-url: the real URL to fetch

The Worker forwards the request, strips proxy headers, and returns the
response with redirect: 'manual' so httpx can handle redirects itself
(each redirect also goes through the proxy transport).

Cookie handling:
  httpx's cookie jar doesn't work correctly through the proxy because
  the HTTP-level request goes to the worker URL, breaking domain matching.
  The transport provides set_cookies() / get_response_cookies() to manage
  cookies at the transport level, bypassing the jar entirely.
"""

from __future__ import annotations
import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


# ── Per-platform proxy gating (2.18.6 / 2.18.7 / 2.22.11 / 2.47.0) ───────
# One platform (SF) needs the CF proxy to function from datacenter
# IPs and uses it implicitly whenever cf_worker_url is configured —
# *_use_cf_proxy toggles do not apply to it.
#
# 2.47.0: DeviantArt left PROXY_REQUIRED (same story as AO3 in
# 2.22.11). DA polling moved from the cookie/Eclipse `_napi` frontend
# — which IS IP-walled on datacenter ranges — to the official OAuth2
# API, which is not. Verified 200 from the GCP VM for both
# `/browse/dailydeviations` and `/gallery/all`. Only the legacy cookie
# fallback would still need the proxy, and that path is not the default.
#
# All other platforms work direct by default. Their per-platform
# toggle (`<platform>_use_cf_proxy`) means "if a direct call hits a
# block-like failure, retry once through the CF Worker". The proxy
# is *never* the default transport for these platforms — only a
# fallback. That keeps the worker quota low (we only burn it when
# direct actually fails) and keeps server-IP-based access patterns
# (cookie scope, IP-locked sessions) intact in the happy path.
#
# 2.22.11: AO3 moved from REQUIRED to OPTIONAL. The original
# classification was because AO3's login form throttles datacenter
# IPs ("Shields are up!"). Cookie-mode auth (added 2.18.8 / made the
# default-on-GCP path) bypasses the login endpoint entirely, so the
# proxy is no longer needed. Routing AO3 through the CF Worker
# turned out to be actively harmful: every Worker tenant shares
# Cloudflare's egress IP pool, and AO3's per-IP throttle (300 req /
# 300s, from rack_attack.rb in otwarchive v0.9.475.3) saw aggregate
# Worker traffic from across all tenants — keeping our shared egress
# IP perpetually throttled. The GCP VM's IP is unique to us;
# direct from there gives us our own quota.

# Optional platforms — toggle `<platform>_use_cf_proxy` enables the
# fallback retry. Direct is always tried first.
PROXY_OPTIONAL_PLATFORMS = frozenset({
    "ib", "fa", "ws", "sqw", "bsky", "ik", "wp", "tw", "ao3",
})

# Required platforms — proxy is the *default* transport whenever
# cf_worker_url is configured. No fallback / no toggle: these
# platforms don't work direct from datacenter IPs at all.
PROXY_REQUIRED_PLATFORMS = frozenset({"sf"})


def proxy_kwargs(settings: dict, platform_code: str) -> dict:
    """Default-path proxy kwargs.

    Returns proxy creds ONLY for platforms in PROXY_REQUIRED_PLATFORMS
    (and only when the worker is configured). Everything else returns
    an empty dict — direct transport. The eight optional platforms
    rely on `proxy_kwargs_fallback()` instead, which is consulted
    only after a direct call has already failed.
    """
    if platform_code not in PROXY_REQUIRED_PLATFORMS:
        return {}
    worker_url = settings.get("cf_worker_url", "")
    worker_key = settings.get("cf_worker_key", "")
    if not (worker_url and worker_key):
        return {}
    return {"proxy_url": worker_url, "proxy_key": worker_key}


def proxy_kwargs_fallback(settings: dict, platform_code: str) -> dict:
    """Fallback-path proxy kwargs.

    Returns proxy creds when the caller has just hit a block-like
    failure and wants to retry through the Worker. Honours the
    per-platform toggle for OPTIONAL platforms; always returns
    creds for REQUIRED platforms (those would already be running
    on proxy via the default path, so this branch usually doesn't
    fire for them — included for completeness so callers can use
    one helper irrespective of platform class).
    """
    worker_url = settings.get("cf_worker_url", "")
    worker_key = settings.get("cf_worker_key", "")
    if not (worker_url and worker_key):
        return {}
    if platform_code in PROXY_REQUIRED_PLATFORMS:
        return {"proxy_url": worker_url, "proxy_key": worker_key}
    if platform_code in PROXY_OPTIONAL_PLATFORMS:
        if settings.get(f"{platform_code}_use_cf_proxy", False):
            return {"proxy_url": worker_url, "proxy_key": worker_key}
    return {}


def is_blocking_failure(exc: BaseException) -> bool:
    """Heuristic: does *exc* look like an IP/Cloudflare/rate-limit
    block (worth retrying through the proxy), or a real
    application-level error (don't retry)?

    True for: 403, 429, "Shields are up", "Retry later" body,
    Cloudflare challenge text, datacenter-IP block phrases, and
    common timeouts/connection errors that often indicate a
    network-level filter rather than a server-side problem.
    False for: 401/credential failures, 404, 5xx (likely a real
    server error rather than a block we can route around).
    """
    s = str(exc)
    needles = (
        "403", "429",
        "Shields are up", "Retry later", "Cloudflare",
        "Forbidden", "rate limit", "rate-limit", "rate limited",
        "blocked", "ConnectTimeout", "ReadTimeout", "ConnectError",
        "Anubis", "challenge",
    )
    return any(n.lower() in s.lower() for n in needles)


class CloudflareProxyTransport(httpx.AsyncBaseTransport):
    """httpx transport that routes requests through a Cloudflare Worker."""

    def __init__(self, worker_url: str, proxy_key: str):
        self.worker_url = worker_url.rstrip("/")
        self.proxy_key = proxy_key
        self._worker_host = urlparse(worker_url).netloc
        self._inner = httpx.AsyncHTTPTransport(retries=2)
        self._session_cookies: str = ""  # Raw "name=val; name2=val2" string

    def set_cookies(self, cookie_str: str) -> None:
        """Store a raw cookie string to inject into every proxied request."""
        self._session_cookies = cookie_str

    async def login_and_fetch(self, login_url: str, email: str, password: str,
                              then_url: str) -> httpx.Response:
        """Execute a full login sequence + fetch in one Worker invocation.

        Uses the Worker's x-proxy-login mode which does:
          GET login_url → extract CSRF → POST login_url → GET then_url
        all in one execution (same egress IP).

        Returns the response from the 'then_url' fetch.
        """
        import json as _json
        login_data = _json.dumps({
            "url": login_url,
            "email": email,
            "password": password,
            "then": then_url,
        })

        logger.debug("CF proxy: login_and_fetch login=%s then=%s", login_url, then_url)

        headers = [
            (b"host", self._worker_host.encode()),
            (b"x-proxy-key", self.proxy_key.encode()),
            (b"x-proxy-login", login_data.encode()),
            (b"user-agent", b"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            (b"accept", b"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            (b"referer", b"https://sofurry.com/"),
        ]

        proxy_request = httpx.Request(
            method="GET",
            url=self.worker_url,
            headers=headers,
        )

        response = await self._inner.handle_async_request(proxy_request)

        # Capture session cookies from the Worker
        session_cookies = response.headers.get("x-session-cookies")
        if session_cookies:
            logger.debug("CF proxy: login_and_fetch received session cookies (%d chars)", len(session_cookies))
            self._session_cookies = session_cookies

        self._update_cookies_from_response(response)

        reused = response.headers.get("x-session-reused", "unknown")
        logger.info("CF proxy: login_and_fetch status=%d final=%s session_reused=%s",
                     response.status_code,
                     response.headers.get("x-final-url", ""),
                     reused)

        return response

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Why cookies are managed here instead of using httpx's cookie jar:
        # httpx's jar uses domain matching to decide which cookies to send.
        # When proxying through a CF Worker, the HTTP-level request goes to the
        # worker URL (e.g. workers.dev), not the real target (e.g. sofurry.com).
        # This breaks domain matching — cookies set for sofurry.com won't be
        # attached to a workers.dev request.  So we bypass the jar entirely and
        # manage cookies as raw strings at the transport level, injecting them
        # into every proxied request regardless of the destination host header.
        target_url = str(request.url)

        logger.debug("CF proxy: %s %s | cookies: %s",
                      request.method, target_url,
                      [p.split("=", 1)[0] for p in self._session_cookies.split("; ") if "=" in p]
                      if self._session_cookies else "(none)")

        # Build new headers: keep originals but replace Host with worker host
        # and inject session cookies (bypassing httpx's broken cookie jar).
        headers = []
        has_cookie = False
        for k, v in request.headers.raw:
            if k.lower() == b"host":
                headers.append((b"host", self._worker_host.encode()))
            elif k.lower() == b"cookie" and self._session_cookies:
                headers.append((b"cookie", self._session_cookies.encode()))
                has_cookie = True
            else:
                headers.append((k, v))

        if not has_cookie and self._session_cookies:
            headers.append((b"cookie", self._session_cookies.encode()))

        # Add proxy-specific headers
        headers.append((b"x-proxy-key", self.proxy_key.encode()))
        headers.append((b"x-target-url", target_url.encode()))

        # Rewrite request to go to the Worker instead of the real target
        proxy_request = httpx.Request(
            method=request.method,
            url=self.worker_url,
            headers=headers,
            stream=request.stream,
        )

        response = await self._inner.handle_async_request(proxy_request)

        # Log response status and Set-Cookie headers for debugging
        set_cookies = response.headers.get_list("set-cookie")
        logger.debug("CF proxy: response %d for %s | Set-Cookie count: %d",
                      response.status_code, target_url, len(set_cookies))
        if set_cookies:
            for sc in set_cookies:
                logger.debug("CF proxy:   Set-Cookie: %s", sc.split("=", 1)[0])

        # Capture cookies from response
        self._update_cookies_from_response(response)

        session_cookies = response.headers.get("x-session-cookies")
        if session_cookies:
            logger.debug("CF proxy: X-Session-Cookies: %s",
                         [p.split("=", 1)[0] for p in session_cookies.split("; ") if "=" in p])
            self._session_cookies = session_cookies

        if self._session_cookies:
            cookie_names = [p.split("=")[0] for p in self._session_cookies.split("; ") if "=" in p]
            logger.debug("CF proxy: stored cookies: %s", cookie_names)

        return response

    def _update_cookies_from_response(self, response: httpx.Response) -> None:
        """Parse Set-Cookie headers and merge into stored session cookies."""
        new_cookies: dict[str, str] = {}
        # Start with existing cookies
        if self._session_cookies:
            for part in self._session_cookies.split("; "):
                if "=" in part:
                    name, _, value = part.partition("=")
                    new_cookies[name.strip()] = value.strip()

        # Merge in any Set-Cookie from the response
        changed = False
        for header_value in response.headers.get_list("set-cookie"):
            cookie_part = header_value.split(";")[0].strip()
            if "=" in cookie_part:
                name, _, value = cookie_part.partition("=")
                name = name.strip()
                value = value.strip()
                if name and not name.startswith("__"):
                    if new_cookies.get(name) != value:
                        new_cookies[name] = value
                        changed = True

        if changed:
            self._session_cookies = "; ".join(
                f"{k}={v}" for k, v in new_cookies.items()
            )

    async def aclose(self) -> None:
        await self._inner.aclose()
