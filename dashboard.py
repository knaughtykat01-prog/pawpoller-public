"""Web dashboard — start when you want to view analytics.

Usage:
    python dashboard.py
    Open http://127.0.0.1:8420
"""

import logging
import os
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response

import config
from database.db import init_db
from routes.api import router
from routes.fa_api import fa_router
from routes.ws_api import ws_router
from routes.sf_api import sf_router
from routes.sqw_api import sqw_router
from routes.ao3_api import ao3_router
from routes.da_api import da_router
from routes.followers_api import followers_router
from routes.wp_api import wp_router
from routes.ik_api import ik_router
from routes.bsky_api import bsky_router
from routes.tw_api import tw_router
from routes.mast_api import mast_router
from routes.tum_api import tum_router
from routes.pix_api import pix_router
from routes.thr_api import thr_router
from routes.ig_api import ig_router
from routes.e621_api import e621_router
from routes.fn_api import fn_router
from routes.fbr_api import fbr_router
from routes.tg_api import tg_router
from routes.posting_api import posting_router
from routes.artwork_api import artwork_router
from routes.posts_api import posts_router
from routes.collections_api import collections_router
from routes.commissions_api import commissions_router
from routes.artists_api import artists_router
from routes.masterpieces_api import masterpieces_router
from routes.whatsnew_api import whatsnew_router
from routes.backup_api import backup_router
from routes.mirror_api import mirror_router
from routes.discord_api import discord_router
from routes.inbox_api import inbox_router
from routes.report_api import report_router
from routes.submissions_api import works_router
from routes.editor_api import editor_router
from routes.dashboard_auth import dashboard_auth_router
from routes.settings_api import settings_router, accounts_router, personas_router
from routes.testing_api import testing_router

# Importing this package triggers @register_test decorators in every
# submodule, populating testing.registry.REGISTRY before the first
# request to /api/testing/tests.
import testing.tests  # noqa: F401

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Credential redaction (2.193.1). basicConfig() is a no-op when a handler is
# already configured (server.py/main.py import this module), but install() is
# idempotent and handler-scoped, so calling it here covers the case where
# dashboard.py IS the entry point (uvicorn dashboard:app).
import log_redaction
log_redaction.install()
config.refresh_log_secrets()   # seed the secret list; config pushes on every save

logger = logging.getLogger("dashboard")


# FastAPI lifespan context manager — replaces the deprecated on_event("startup")
# and on_event("shutdown") hooks. Everything before `yield` runs at startup (DB init,
# logging the listen address). Everything after `yield` runs at shutdown. FastAPI
# holds the context open for the entire lifetime of the server.
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    config.migrate_dashboard_auth()
    config.migrate_sofurry_credentials()
    config.migrate_browser_login_usernames()
    config.migrate_meta_user_ids()
    # Vault is always-on: sweep any plaintext credentials (pre-2.101.0
    # settings.json, hand edits, old-backup restores) into the vault.
    _migrated = config.ensure_vault()
    if _migrated:
        logger.info("Credential vault: migrated %d plaintext field(s)", _migrated)
    logger.info("Dashboard started at http://%s:%d", config.DASHBOARD_HOST, config.DASHBOARD_PORT)
    yield
    logger.info("Dashboard shutting down")


# The interactive API docs (/docs, /redoc) and the OpenAPI schema are disabled
# by default so a running instance doesn't expose its full API surface as an
# extraneous, unauthenticated-looking endpoint (ASVS 5.0 V13.4.5). Set
# PAWPOLLER_ENABLE_DOCS=1 to turn them back on for local development.
_enable_docs = bool(os.environ.get("PAWPOLLER_ENABLE_DOCS"))
app = FastAPI(
    title="PawPoller", version="1.0.0", lifespan=lifespan,
    docs_url="/docs" if _enable_docs else None,
    redoc_url="/redoc" if _enable_docs else None,
    openapi_url="/openapi.json" if _enable_docs else None,
)

# ── CORS — Block All Cross-Origin Requests ────────────────────
# PawPoller is a self-contained SPA where frontend and API are same-origin.
# No legitimate cross-origin requests should ever occur.  Empty allow_origins
# means all CORS preflight requests are denied, preventing external sites from
# making API calls to PawPoller even if a user has it open in another tab.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


# Global exception handler — catches any unhandled exception that escapes a route
# handler and returns a clean JSON 500 instead of letting uvicorn emit a bare
# traceback or HTML error page. Also logs the full stack trace (exc_info=True) so
# errors are visible in the console/log without exposing internals to the client.
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled error on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})


# Server-error detail scrubber. Many routes raise HTTPException(500, detail=str(e)),
# which would return raw exception text (filesystem paths, network errors) to the
# client. This handler keeps client-facing 4xx detail intact (intentional,
# operator-facing validation messages the SPA displays) but replaces the detail of
# any 5xx with a generic message, logging the real one server-side. Closes ASVS
# 5.0 V16.5.1 without touching ~200 individual raise sites.
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code >= 500:
        logger.error("Server error on %s %s: %s", request.method, request.url.path, exc.detail)
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": "Internal server error"},
                            headers=getattr(exc, "headers", None))
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))


# ── HTTP Security Headers ──────────────────────────────────────
# Applied to every response.  These are defence-in-depth measures:
#   X-Content-Type-Options  — prevents MIME-sniffing (IE/Edge attack vector)
#   X-Frame-Options         — blocks embedding in iframes (clickjacking)
#   Referrer-Policy         — limits referrer leakage to external sites
#   Content-Security-Policy — restricts script/style/image/connect sources
#     script-src 'self' <theme-hash>  : bundled JS + the inline no-flash theme
#                                       bootstrap script (hashed so the rest of
#                                       'unsafe-inline' stays disallowed)
#     style-src 'self' 'unsafe-inline' fonts.googleapis.com : CSS files + inline
#                                       style= attributes + Google Fonts CSS
#     font-src 'self' fonts.gstatic.com : Google Fonts woff2 binaries
#     img-src 'self' https:      : local proxy + platform CDN thumbnails
#     connect-src 'self'         : all API calls are same-origin
#     frame-ancestors 'none'     : no embedding allowed (supercedes X-Frame-Options)
#   When Turnstile is configured, script-src and frame-src include cloudflare.

_BASE_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


_cached_csp: str | None = None


_cached_epub_viewer_csp: str | None = None


_cached_share_csp: str | None = None


_cached_theme_hash: str | None = None


def _theme_inline_hash() -> str:
    """CSP ``script-src`` source token(s) for the inline no-flash bootstrap
    ``<script>``, computed from the HTML files themselves so they can never drift.

    That one small script applies the persisted theme + resolves mobile-mode
    synchronously (before CSS paints) without opening the policy to
    'unsafe-inline'. Its SHA-256 must be whitelisted — and a STALE hardcoded hash
    silently blocks the whole script, breaking theme AND mobile-mode resolution
    app-wide (exactly the regression that shipped in 2.70.0, when index.html's
    script was edited but the pinned constant wasn't). index.html and
    epub-viewer.html carry the same boot script and are *meant* to stay
    byte-identical; we hash each independently anyway (deduped) so editing one
    without the other can never block it. Cached after first read.
    """
    global _cached_theme_hash
    if _cached_theme_hash is not None:
        return _cached_theme_hash
    import hashlib
    import base64
    import re

    frontend = config.resource_path("frontend")
    tokens: list[str] = []
    for name in ("index.html", "epub-viewer.html"):
        path = frontend / name
        if not path.exists():
            continue
        # The browser hashes the exact text between the tags of the first
        # attribute-less <script> (the boot IIFE); later ones are <script src=…>.
        match = re.search(r"<script>(.*?)</script>", path.read_text(encoding="utf-8"), re.DOTALL)
        if not match:
            continue
        digest = hashlib.sha256(match.group(1).encode("utf-8")).digest()
        token = "'sha256-" + base64.b64encode(digest).decode() + "'"
        if token not in tokens:
            tokens.append(token)
    _cached_theme_hash = " ".join(tokens)
    return _cached_theme_hash


def _build_epub_viewer_csp() -> str:
    """Relaxed CSP for the in-app EPUB viewer (/epub-viewer.html only).

    epub.js extracts CSS, images, and fonts from the EPUB archive into
    Blob URLs and references them from the rendered iframe. Without
    `blob:` in style-src/img-src/font-src those resources are CSP-blocked
    and the book renders unstyled or with broken inline images. The
    relaxation is scoped to this single page so the rest of the
    dashboard keeps the strict default.
    """
    global _cached_epub_viewer_csp
    if _cached_epub_viewer_csp is not None:
        return _cached_epub_viewer_csp
    theme_inline_hash = _theme_inline_hash()
    _cached_epub_viewer_csp = (
        "default-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        # form-action has no default-src fallback either — see _build_csp().
        "form-action 'self'; "
        f"script-src 'self' {theme_inline_hash}; "
        "style-src 'self' 'unsafe-inline' blob: https://fonts.googleapis.com; "
        "font-src 'self' blob: https://fonts.gstatic.com; "
        "img-src 'self' blob: data: https:; "
        "connect-src 'self' blob:; "
        "frame-src 'self' blob:; "
        "frame-ancestors 'none'"
    )
    return _cached_epub_viewer_csp


def _build_share_csp() -> str:
    """CSP for the public read-only ``/share/{token}`` draft preview.

    A beta-share is the one surface an unauthenticated stranger can reach, and
    the rendered body is user-authored story prose. It's self-contained HTML
    with an inline ``<style>`` and needs NO scripts — so ``script-src`` is
    denied outright (``default-src 'none'`` with no script-src). Inline styles
    plus images/fonts only. Stored markup in a draft therefore cannot execute
    script even if it slipped past the converter.
    """
    global _cached_share_csp
    if _cached_share_csp is not None:
        return _cached_share_csp
    _cached_share_csp = (
        "default-src 'none'; "
        "style-src 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "base-uri 'none'; "
        # form-action has no default-src fallback either — see _build_csp().
        "form-action 'self'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    )
    return _cached_share_csp


def _build_csp() -> str:
    """Build Content-Security-Policy, adding Turnstile origins when configured.

    Result is cached; call ``invalidate_csp_cache()`` when Turnstile config changes.
    """
    global _cached_csp
    if _cached_csp is not None:
        return _cached_csp
    settings = config.get_settings()
    has_turnstile = bool(settings.get("turnstile_site_key"))
    cf = " https://challenges.cloudflare.com" if has_turnstile else ""
    frame_src = f"frame-src 'self'{cf}; " if has_turnstile else ""
    # Hash of the inline theme-apply script in frontend/index.html, derived
    # from the file by _theme_inline_hash() so editing that script never leaves
    # a stale hash silently blocking it (see that helper's docstring).
    theme_inline_hash = _theme_inline_hash()
    _cached_csp = (
        "default-src 'self'; "
        # object-src/base-uri/form-action are named explicitly (not just via
        # default-src): ASVS 5.0 V3.4.3 requires the first two, and NONE of the
        # three inherits from default-src — so without them a <base> tag
        # injection is unconstrained and an injected
        # <form action="https://attacker/"> posts happily from our own origin.
        #
        # form-action was added in 3.17.3 after a review found the DA OAuth
        # callback rendering an unescaped `error_description` query parameter
        # (fixed alongside, in routes/da_api.py). `script-src 'self'` stopped
        # the script cases and left form-post phishing — a convincing fake
        # login prompt on the REAL origin — completely open. A CSP that blocks
        # script is not a CSP that blocks credential theft.
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        f"script-src 'self' {theme_inline_hash}{cf}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        # blob: — the Posts compose image preview uses URL.createObjectURL();
        # data: — inline data-URI images. Both are needed or the <img> is
        # CSP-blocked (renders as a broken "attachment preview"). Matches the
        # relaxed epub-viewer CSP, which already allows them.
        "img-src 'self' blob: data: https:; "
        "connect-src 'self'; "
        # PWA: the service worker (worker-src) and web app manifest (manifest-src)
        # are same-origin. Explicit so registration isn't left to fallback ambiguity.
        "worker-src 'self'; "
        "manifest-src 'self'; "
        f"{frame_src}"
        "frame-ancestors 'none'"
    )
    return _cached_csp


def invalidate_csp_cache() -> None:
    """Clear the cached CSP so it's rebuilt on the next request."""
    global _cached_csp, _cached_epub_viewer_csp, _cached_share_csp
    _cached_csp = None
    _cached_epub_viewer_csp = None
    _cached_share_csp = None


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    for header, value in _BASE_SECURITY_HEADERS.items():
        response.headers[header] = value
    # HSTS (gap-wave-4): tell the browser to only ever reach this origin over
    # https once it's seen it there — defends against first-contact downgrade /
    # cookie leak. Only when the effective request is https (behind a trusted
    # proxy uvicorn rewrites the scheme); never on plain-http LAN use.
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # /epub-viewer.html needs a relaxed CSP for epub.js's blob: URLs.
    # Anything else gets the strict default.
    if request.url.path == "/epub-viewer.html":
        response.headers["Content-Security-Policy"] = _build_epub_viewer_csp()
    elif request.url.path.startswith("/share/"):
        # Public beta-share draft preview — script-free CSP (see _build_share_csp).
        response.headers["Content-Security-Policy"] = _build_share_csp()
    else:
        response.headers["Content-Security-Policy"] = _build_csp()
    return response


# ── Brute-Force Rate Limiting ─────────────────────────────────
# Simple in-memory tracker: after 10 failed auth attempts from the same IP
# within 5 minutes, all further requests from that IP get 429 Too Many Requests.
# Single-process server so in-memory state is sufficient.  Clears on restart.
# Used by both the session auth middleware below and the login endpoint in
# routes/dashboard_auth.py (which imports _record_auth_failure / _is_rate_limited).
_AUTH_FAIL_WINDOW = 300      # seconds (5 minutes)
_AUTH_FAIL_MAX = 10          # max failures before lockout
_auth_failures: dict[str, list[float]] = {}   # IP -> list of failure timestamps

# Global soft-throttle (gap-wave-4): the per-IP limiter above is defeated by IP
# rotation (IPv6 ranges, botnets). This counts ALL failures across every IP in
# the window; once it crosses a high threshold, login attempts take a fixed
# delay (see _global_soft_throttle_secs). It deliberately never hard-locks —
# the real admin's correct password still works, just a couple seconds slower
# during an active distributed attack. Turnstile is the stronger opt-in.
_GLOBAL_FAIL_THRESHOLD = 50   # failures across all IPs in the window
_GLOBAL_THROTTLE_SECS = 2.0   # delay added per attempt once tripped
_global_failures: list[float] = []


def _record_auth_failure(ip: str) -> None:
    """Record a failed auth attempt from *ip* (+ the global counter)."""
    now = time.monotonic()
    attempts = _auth_failures.setdefault(ip, [])
    attempts.append(now)
    cutoff = now - _AUTH_FAIL_WINDOW
    _auth_failures[ip] = [t for t in attempts if t > cutoff]
    _global_failures.append(now)
    _global_failures[:] = [t for t in _global_failures if t > cutoff]


def _global_soft_throttle_secs() -> float:
    """Seconds a login should stall right now given global failure pressure
    (0 when below threshold). Never blocks — just slows distributed guessing."""
    cutoff = time.monotonic() - _AUTH_FAIL_WINDOW
    _global_failures[:] = [t for t in _global_failures if t > cutoff]
    return _GLOBAL_THROTTLE_SECS if len(_global_failures) >= _GLOBAL_FAIL_THRESHOLD else 0.0


def _is_rate_limited(ip: str) -> bool:
    """Return True if *ip* has exceeded the failure threshold."""
    attempts = _auth_failures.get(ip)
    if not attempts:
        return False
    cutoff = time.monotonic() - _AUTH_FAIL_WINDOW
    recent = [t for t in attempts if t > cutoff]
    if recent:
        _auth_failures[ip] = recent
    else:
        _auth_failures.pop(ip, None)  # Free memory for expired IPs
    return len(recent) >= _AUTH_FAIL_MAX


# ── Session-Based Dashboard Auth ──────────────────────────────
# Replaces the old HTTP Basic Auth popup with session cookies.  When auth is
# configured (bcrypt hash or legacy password exists), all API requests require
# either a valid pp_session cookie or a Bearer API key.  Static assets (/, /css/*,
# /js/*) are always exempt so the SPA can load and show its own login form.

_AUTH_EXEMPT_PATHS = frozenset({
    "/api/health",
    "/api/auth/dashboard-status",
    "/api/auth/dashboard-login",
    "/api/auth/dashboard-setup",
    # 2.16.8: favicon was returning 401 because the auth middleware
    # didn't exempt it. Browsers fetch /favicon.ico without auth
    # context on every page, producing console error noise.
    "/favicon.ico",
    # PWA: the browser fetches the manifest and (re)registers the service
    # worker outside the page's auth context — these must answer without a
    # session or install / offline support silently breaks. Neither exposes
    # any private data (static manifest + a cache-only worker script).
    "/manifest.webmanifest",
    "/sw.js",
})
_AUTH_EXEMPT_PREFIXES = ("/css/", "/js/", "/vendor/", "/img/", "/api/ig/pubmedia/", "/share/")

# Endpoints that return stored credentials / full data backups or perform
# destructive actions. On an UNCONFIGURED (no-password) instance these must
# never be served to a remote caller — otherwise an exposed server leaks every
# stored platform credential via e.g. POST /api/settings/sync. On a configured
# instance the normal auth check below applies; on an unconfigured instance we
# allow them only from a loopback client (the desktop app / local operator).
_SENSITIVE_WHEN_OPEN_PREFIXES = (
    "/api/settings/sync",
    "/api/settings/uninstall",
    "/api/backup",
    "/api/posting/sync/upload",
    # 3.6.0: /api/mirror/db-snapshot serves the entire database and
    # /api/mirror/artwork/* the whole art catalogue, so the mirror API is the
    # single largest data-egress surface in the app. /api/artwork/sync/upload
    # was missing here too — it accepts a tar that is extracted into the
    # archive, which is a write primitive, not just a read.
    "/api/mirror",
    "/api/artwork/sync/upload",
)


def _client_is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


@app.middleware("http")
async def session_auth_middleware(request: Request, call_next):
    path = request.url.path

    # If no auth is configured, pass through — EXCEPT the sensitive endpoints
    # above, which must not be reachable from a remote caller on an open
    # instance (they'd dump every stored secret / allow remote takeover).
    if not config.is_dashboard_auth_required():
        if path.startswith(_SENSITIVE_WHEN_OPEN_PREFIXES) and not _client_is_loopback(request):
            return Response(
                status_code=403,
                content="Set a dashboard password (Settings -> Security) before using this endpoint from a non-local client.",
            )
        return await call_next(request)

    # Let SPA load (index.html) and static assets through unconditionally
    if path == "/" or path.startswith(_AUTH_EXEMPT_PREFIXES):
        return await call_next(request)

    # Exempt specific API paths (login, status, setup, health)
    if path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)

    client_ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(client_ip):
        logger.warning("Auth: request blocked by rate limiter (ip=%s path=%s)", client_ip, path)
        return Response(status_code=429, content="Too many failed attempts. Try again later.")

    # Check API key (Authorization: Bearer pp_xxx)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if config.validate_api_key(token):
            return await call_next(request)
        # A malformed/expired/forged API key is a failed auth attempt —
        # count it toward the rate limiter and log it (ASVS V16.3.1).
        _record_auth_failure(client_ip)
        logger.warning("Auth: API-key rejected (ip=%s path=%s)", client_ip, path)
        return JSONResponse(status_code=401, content={"error": "Authentication required"})

    # Check session cookie (verify_session handles short/long expiry internally)
    cookie = request.cookies.get("pp_session")
    if cookie:
        payload = config.verify_session(cookie)
        if payload:
            return await call_next(request)
        # Present-but-invalid cookie (tampered/expired) — log at debug; these
        # also happen benignly on idle expiry so don't count toward lockout.
        logger.debug("Auth: session cookie invalid/expired (ip=%s path=%s)", client_ip, path)

    # Not authenticated — return 401 JSON for API paths so the frontend
    # can detect it and redirect to the login page
    return JSONResponse(status_code=401, content={"error": "Authentication required"})



# Mount API routes BEFORE static file mounts. FastAPI/Starlette matches routes
# in registration order, so API endpoints (e.g. /api/*, /fa/*, /ws/*) must be
# registered first. If static file mounts were registered first, a request to
# /api/stats could be misrouted to the static file handler and 404.
app.include_router(dashboard_auth_router)  # Dashboard auth routes (/api/auth/dashboard-*)
app.include_router(router)       # Core REST API routes (/api/*)
app.include_router(fa_router)    # FurAffinity routes (/api/fa/*)
app.include_router(ws_router)    # Weasyl routes (/api/ws/*)
app.include_router(sf_router)    # SoFurry routes (/api/sf/*)
app.include_router(sqw_router)   # SquidgeWorld routes (/api/sqw/*)
app.include_router(ao3_router)   # AO3 routes (/api/ao3/*)
app.include_router(da_router)    # DeviantArt routes (/api/da/*)
app.include_router(wp_router)    # Wattpad routes (/api/wp/*)
app.include_router(ik_router)    # Itaku routes (/api/ik/*)
app.include_router(bsky_router)  # Bluesky routes (/api/bsky/*)
app.include_router(tw_router)    # X/Twitter routes (/api/tw/*)
app.include_router(mast_router)  # Mastodon routes (/api/mast/*)
app.include_router(tum_router)   # Tumblr routes (/api/tum/*)
app.include_router(pix_router)   # Pixiv routes (/api/pix/*)
app.include_router(thr_router)   # Threads routes (/api/thr/*)
app.include_router(ig_router)    # Instagram routes (/api/ig/*)
app.include_router(e621_router)  # e621 routes (/api/e621/*)
app.include_router(fn_router)    # FurryNetwork routes (/api/fn/*)
app.include_router(fbr_router)   # Furbooru routes (/api/fbr/*)
app.include_router(tg_router)    # Telegram channel analytics (/api/tg/*)
app.include_router(posting_router)  # Posting module routes (/api/posting/*)
app.include_router(artwork_router)  # Artwork hub routes (/api/artwork/*)
app.include_router(posts_router)    # Posts (microblog) module routes (/api/posts/*)
app.include_router(works_router)    # Unified Submissions hub (/api/works)
app.include_router(collections_router)  # Collections (master container) routes (/api/collections/*)
app.include_router(commissions_router)  # Commissions (client tracker) routes (/api/commissions/*)
app.include_router(masterpieces_router)  # Masterpieces (master image record) routes (/api/masterpieces/*)
app.include_router(artists_router)       # Artist registry (/api/artists/*)
app.include_router(whatsnew_router)  # In-app "What's new" changelog popup (/api/whatsnew)
app.include_router(backup_router)    # Backup & restore (/api/backup/*)
app.include_router(mirror_router)    # Server → desktop mirroring (/api/mirror/*)
app.include_router(discord_router)   # Discord announce webhook (/api/discord/*)
app.include_router(inbox_router)     # Unified comment inbox (/api/inbox/*)
app.include_router(report_router)    # Error-report → Telegram forwarder (/api/report-error)
app.include_router(editor_router)   # Story editor routes (/api/editor/*)
app.include_router(settings_router)  # Settings sync routes (/api/settings/*)
app.include_router(accounts_router)  # Multi-account registry routes (/api/accounts/*)
app.include_router(personas_router)  # Persona (account grouping) routes (/api/personas/*)
app.include_router(followers_router)  # Cross-platform follower count + growth (/api/followers/*)
app.include_router(testing_router)   # Diagnostics & testing routes (/api/testing/*)

# Serve frontend static files. config.resource_path() resolves differently
# depending on the build mode:
#   - Frozen (PyInstaller exe): looks inside the bundled _MEIPASS temp directory
#     where PyInstaller extracts data files at runtime.
#   - Dev (plain python): looks relative to the project root on disk.
# This abstraction lets the same code serve assets in both environments.
frontend_dir = config.resource_path("frontend")
app.mount("/css", StaticFiles(directory=str(frontend_dir / "css")), name="css")
app.mount("/js", StaticFiles(directory=str(frontend_dir / "js")), name="js")
app.mount("/vendor", StaticFiles(directory=str(frontend_dir / "vendor")), name="vendor")
app.mount("/img", StaticFiles(directory=str(frontend_dir / "img")), name="img")


# Browsers request /favicon.ico at the document root regardless of <link> tags;
# serve the nib-badge .ico here. The path is auth-exempt (_AUTH_EXEMPT_PATHS).
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(str(frontend_dir / "img" / "favicon.ico"))


# ── PWA (installable to the home screen) ─────────────────────────────
# The manifest describes the installed app (name/icons/standalone display).
@app.get("/manifest.webmanifest", include_in_schema=False)
async def serve_manifest():
    return FileResponse(
        str(frontend_dir / "manifest.webmanifest"),
        media_type="application/manifest+json",
    )


# The service worker MUST be served from the document root so its scope covers
# the whole app ("/"); a worker under /js/ could only control /js/. APP_VERSION
# is spliced into its cache name (same __APP_VERSION__ substitution as index),
# so every release changes the file's bytes → the browser installs the new
# worker and its activate() purges older caches. `no-cache` makes the browser
# revalidate the worker on each load so a new version is picked up promptly.
@app.get("/sw.js", include_in_schema=False)
async def serve_service_worker():
    raw = (frontend_dir / "sw.js").read_text(encoding="utf-8")
    rendered = raw.replace("__APP_VERSION__", config.APP_VERSION)
    return Response(
        content=rendered,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# SPA (Single Page Application) serving pattern. The root route serves index.html,
# which bootstraps the JS frontend. Client-side routing is handled entirely in the
# browser by the JS app — there are no additional server-side page routes. Any
# navigation the user performs in the UI is managed by the frontend JS without
# additional HTML pages from the server.
#
# Cache-buster substitution: index.html ships with `?v=__APP_VERSION__` on every
# CSS and JS reference. We splice config.APP_VERSION in here at request time so
# every release automatically invalidates browser caches without requiring
# someone to remember to bump per-file `?v=NNN` numbers (the source of BUG-001
# in 2.14.6).
# The redesigned dashboard shell (frontend/index.html) is the one and only UI.
# A pre-2.29.0 "legacy" shell used to ship beside it behind a ?ui= toggle for
# side-by-side comparison; that scaffold (index_legacy.html + *_legacy.{css,js}
# + the injected Legacy/Beta switch) was removed in 2.51.2 now that beta has
# fully settled.
_index_html_cache: dict[str, str] = {}  # version -> rendered html


def _render_index_html() -> str:
    version = config.APP_VERSION
    cached = _index_html_cache.get(version)
    if cached is not None:
        return cached
    raw = (frontend_dir / "index.html").read_text(encoding="utf-8")
    rendered = raw.replace("__APP_VERSION__", version)
    _index_html_cache[version] = rendered
    return rendered


@app.get("/")
async def serve_index(request: Request):
    return Response(content=_render_index_html(), media_type="text/html")


@app.get("/epub-viewer.html")
async def serve_epub_viewer():
    """In-app EPUB reader. Opened in a new tab from the editor's
    Downloads dropdown. Renders any EPUB served by /api/posting/file
    using vendored epub.js. Auth is the standard session-cookie middleware
    — opened from the authenticated dashboard, the cookie tags along
    same-origin so the EPUB fetch and the page itself both succeed.
    """
    raw = (frontend_dir / "epub-viewer.html").read_text(encoding="utf-8")
    rendered = raw.replace("__APP_VERSION__", config.APP_VERSION)
    return Response(content=rendered, media_type="text/html")


_SHARE_404_HTML = (
    "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<meta name='robots' content='noindex, nofollow'><title>Link not found</title>"
    "<style>body{font-family:system-ui,Arial,sans-serif;background:#17150f;color:#e7e2d8;"
    "display:flex;min-height:100vh;margin:0;align-items:center;justify-content:center;text-align:center}"
    "div{max-width:22rem;padding:1rem}h1{font-size:1.3rem;margin:0 0 .5rem}p{color:#b3ab9c;line-height:1.6}</style>"
    "</head><body><div><h1>This draft link isn't available</h1>"
    "<p>It may have been revoked, expired, or never existed. Ask whoever shared it for a fresh link.</p>"
    "</div></body></html>"
)


@app.get("/share/{token}")
async def serve_shared_draft(token: str):
    """Public, read-only beta-reader preview of a story draft (gap-wave-5 §3).

    No login: the token IS the credential. Look it up → check enabled + not
    expired → render the story's self-contained styled HTML. Any miss (unknown
    token, revoked, expired, or a story that vanished) returns an identical 404
    page so a probe can't distinguish "wrong token" from "revoked".
    """
    from database.db import get_connection
    from database import share_tokens
    from routes import editor_api

    conn = get_connection()
    try:
        row = share_tokens.get_token(conn, token)
    finally:
        conn.close()
    if not share_tokens.is_live(row):
        return Response(content=_SHARE_404_HTML, media_type="text/html", status_code=404)

    html = editor_api.render_story_share_html(row["story_name"])
    if html is None:
        return Response(content=_SHARE_404_HTML, media_type="text/html", status_code=404)
    return Response(content=html, media_type="text/html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard:app", host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT)
