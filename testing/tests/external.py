"""External-services probes.

Each test skips when its dependency isn't configured. None are
destructive in the "sends a message" sense; the Telegram getMe call
is read-only.
"""

from __future__ import annotations

import httpx

import config
from testing.registry import TestContext, register_test


# ── Cloudflare Worker proxy ──────────────────────────────────────────


@register_test(
    test_id="external.cf_proxy.ping",
    name="CF Worker proxy reachable",
    category="External Services",
    description="GET <cf_worker_url>/ with the configured key. Skip if not configured.",
    timeout_seconds=15.0,
)
async def t_cf_ping(ctx: TestContext) -> None:
    s = config.get_settings()
    url = s.get("cf_worker_url", "")
    key = s.get("cf_worker_key", "")
    if not url or not key:
        raise ctx.skip("CF Worker not configured")
    async with httpx.AsyncClient(timeout=10.0) as cli:
        # We deliberately ping with a bogus target so the worker
        # answers with its own protocol response rather than trying
        # to fetch something. A 4xx/200 indicates the worker is up.
        resp = await cli.get(
            url,
            headers={"x-proxy-key": key, "x-target-url": "https://example.com/"},
        )
        ctx.detail("status", resp.status_code)
        ctx.detail("body_bytes", len(resp.content))
        # Anything other than 5xx means the worker handled the request
        assert resp.status_code < 500, f"CF worker returned {resp.status_code}"


@register_test(
    test_id="external.cf_proxy.auth_rejection",
    name="CF Worker rejects bad key",
    category="External Services",
    description="Same endpoint with a deliberately wrong key should 403.",
    timeout_seconds=15.0,
)
async def t_cf_auth_reject(ctx: TestContext) -> None:
    s = config.get_settings()
    url = s.get("cf_worker_url", "")
    if not url:
        raise ctx.skip("CF Worker not configured")
    bad_key = "diagnostic-deliberately-invalid-key"
    async with httpx.AsyncClient(timeout=10.0) as cli:
        resp = await cli.get(
            url,
            headers={"x-proxy-key": bad_key, "x-target-url": "https://example.com/"},
        )
        ctx.detail("status", resp.status_code)
        assert resp.status_code in (401, 403), (
            f"expected 401/403, got {resp.status_code}"
        )


# ── Telegram bot reachable ───────────────────────────────────────────


@register_test(
    test_id="external.telegram.bot_info",
    name="Telegram bot reachable (getMe)",
    category="External Services",
    description="GET /bot{token}/getMe returns the bot's identity. Read-only.",
    requires_creds=["telegram_bot_token"],
    timeout_seconds=15.0,
)
async def t_telegram_get_me(ctx: TestContext) -> None:
    s = config.get_settings()
    token = s["telegram_bot_token"]
    async with httpx.AsyncClient(timeout=10.0) as cli:
        resp = await cli.get(f"https://api.telegram.org/bot{token}/getMe")
        ctx.detail("status", resp.status_code)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        ctx.detail("ok", data.get("ok"))
        result = data.get("result", {})
        ctx.detail("username", result.get("username"))
        ctx.detail("first_name", result.get("first_name"))
        assert data.get("ok"), f"getMe failed: {data}"


# ── GitHub latest release ────────────────────────────────────────────


@register_test(
    test_id="external.github.latest_release",
    name="GitHub latest release reachable",
    category="External Services",
    description=(
        "Fetch the PawPoller repo's latest release tag and compare against "
        "APP_VERSION. Uses github_pat from settings when configured so "
        "private repos work; without a PAT, an anonymous 404 (private repo "
        "or no published releases) is treated as a clean skip."
    ),
    timeout_seconds=15.0,
)
async def t_github_release(ctx: TestContext) -> None:
    repo = "knaughtykat01-prog/PawPoller"
    s = config.get_settings()
    pat = s.get("github_pat") or ""
    headers = {"User-Agent": "PawPoller-Diagnostics"}
    if pat:
        headers["Authorization"] = f"Bearer {pat}"
    async with httpx.AsyncClient(timeout=10.0) as cli:
        resp = await cli.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers=headers,
        )
        ctx.detail("status", resp.status_code)
        ctx.detail("authenticated", bool(pat))
        if resp.status_code == 404:
            if pat:
                # Authed and still 404 — really no releases yet.
                raise ctx.skip("repo has no published releases (404 with PAT)")
            # Anonymous 404 on a private repo is expected — skip cleanly.
            raise ctx.skip(
                "anonymous 404 — repo is private (set github_pat in settings "
                "to query private repos) or has no published releases"
            )
        assert resp.status_code == 200, f"GitHub returned {resp.status_code}"
        data = resp.json()
        latest = data.get("tag_name", "").lstrip("v")
        ctx.detail("github_latest", latest)
        ctx.detail("running_version", config.APP_VERSION)
        # We don't assert version equality — being behind is normal mid-cycle.
        # We only assert the API call succeeded and returned a tag.
        assert latest, "no tag_name on latest release"


# ── Cloudflare Turnstile ─────────────────────────────────────────────


@register_test(
    test_id="external.turnstile.reachable",
    name="Cloudflare Turnstile widget reachable",
    category="External Services",
    description=(
        "Turnstile is Cloudflare's privacy-preserving CAPTCHA replacement. "
        "PawPoller's dashboard login page can render the Turnstile widget "
        "in front of the password form so brute-force bots can't even "
        "reach the auth endpoint. Configured via Settings → Dashboard "
        "(turnstile_site_key + turnstile_secret_key). This test fetches "
        "the Turnstile JS bundle from Cloudflare to confirm the CDN is "
        "reachable from the server — skipped when not configured."
    ),
    timeout_seconds=15.0,
)
async def t_turnstile(ctx: TestContext) -> None:
    s = config.get_settings()
    if not s.get("turnstile_site_key"):
        raise ctx.skip("Turnstile not configured")
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as cli:
        resp = await cli.get("https://challenges.cloudflare.com/turnstile/v0/api.js")
        ctx.detail("status", resp.status_code)
        ctx.detail("content_type", resp.headers.get("content-type"))
        assert resp.status_code == 200, f"Turnstile JS not reachable: {resp.status_code}"
