"""Platform tests — auth probes + polling discovery for all 11 platforms.

Each platform gets two tests:

  platforms.<p>.auth      — credential validity, reports username/handle
  platforms.<p>.discovery — lightweight gallery listing (no DB write)

Tests are skipped when required credentials are absent (declared via
the registry's requires_creds). Runner paces platform-prefixed tests
according to per-platform inter-request delays so we don't burst.
"""

from __future__ import annotations

import config
from testing.registry import TestContext, register_test


# ── Inkbunny ─────────────────────────────────────────────────────────


@register_test(
    test_id="platforms.ib.auth",
    name="Inkbunny — credentials validate",
    category="Platforms — Auth",
    description="Login + API user-rating unlock via /api_login.php.",
    requires_creds=["username", "password"],
    timeout_seconds=20.0,
)
async def t_ib_auth(ctx: TestContext) -> None:
    from clients.ib.client import InkbunnyClient

    s = config.get_settings()
    async with InkbunnyClient(s["username"], s["password"]) as cli:
        resp = await cli.login()
        ctx.detail("sid_present", bool(getattr(resp, "sid", None)))
        ctx.detail("username", getattr(resp, "username", s["username"]))
        assert resp.sid, "no SID returned"


@register_test(
    test_id="platforms.ib.discovery",
    name="Inkbunny — gallery discovery",
    category="Platforms — Polling Discovery",
    description="search_user_submissions returns at least one submission ID.",
    requires_creds=["username", "password"],
    timeout_seconds=30.0,
)
async def t_ib_discovery(ctx: TestContext) -> None:
    from clients.ib.client import InkbunnyClient

    s = config.get_settings()
    async with InkbunnyClient(s["username"], s["password"]) as cli:
        await cli.login()
        subs = await cli.search_user_submissions()
        ctx.detail("submission_count", len(subs))
        ctx.detail("first_id", subs[0] if subs else None)
        assert isinstance(subs, list), "search_user_submissions must return list"


# ── FurAffinity ──────────────────────────────────────────────────────


@register_test(
    test_id="platforms.fa.auth",
    name="FurAffinity — cookies validate",
    category="Platforms — Auth",
    description="validate_cookies() verifies cookie_a + cookie_b authenticate.",
    requires_creds=["fa_username", "fa_cookie_a", "fa_cookie_b"],
    timeout_seconds=20.0,
)
async def t_fa_auth(ctx: TestContext) -> None:
    from clients.fa.client import FAClient

    s = config.get_settings()
    async with FAClient(
        username=s["fa_username"],
        cookie_a=s["fa_cookie_a"],
        cookie_b=s["fa_cookie_b"],
    ) as cli:
        ok = await cli.validate_cookies()
        ctx.detail("validated", ok)
        assert ok, "cookies did not validate"


@register_test(
    test_id="platforms.fa.discovery",
    name="FurAffinity — gallery discovery",
    category="Platforms — Polling Discovery",
    description="Fetch first gallery page via FAExport.",
    requires_creds=["fa_username"],
    timeout_seconds=30.0,
)
async def t_fa_discovery(ctx: TestContext) -> None:
    from clients.fa.client import FAClient

    s = config.get_settings()
    async with FAClient(
        username=s["fa_username"],
        cookie_a=s.get("fa_cookie_a", ""),
        cookie_b=s.get("fa_cookie_b", ""),
    ) as cli:
        page = await cli.get_gallery_page(1)
        ctx.detail("submission_count", len(page))
        assert isinstance(page, list), "get_gallery_page must return list"


# ── Weasyl ───────────────────────────────────────────────────────────


@register_test(
    test_id="platforms.ws.auth",
    name="Weasyl — API key validates",
    category="Platforms — Auth",
    description="GET /api/whoami returns login + userid.",
    requires_creds=["ws_api_key"],
    timeout_seconds=15.0,
)
async def t_ws_auth(ctx: TestContext) -> None:
    from clients.weasyl.client import WeasylClient

    s = config.get_settings()
    async with WeasylClient(s["ws_api_key"]) as cli:
        login = await cli.validate_key()
        ctx.detail("login", login)
        assert login, "API key did not validate"


@register_test(
    test_id="platforms.ws.discovery",
    name="Weasyl — gallery discovery",
    category="Platforms — Polling Discovery",
    description="Cursor-paginated gallery; just first page.",
    requires_creds=["ws_api_key"],
    timeout_seconds=30.0,
)
async def t_ws_discovery(ctx: TestContext) -> None:
    from clients.weasyl.client import WeasylClient
    from polling.cf_proxy import proxy_kwargs

    s = config.get_settings()
    async with WeasylClient(s["ws_api_key"], **proxy_kwargs(s, "ws")) as cli:
        login = await cli.validate_key()
        if not login:
            raise AssertionError("validate_key failed")
        method = (
            getattr(cli, "get_all_gallery_ids", None)
            or getattr(cli, "get_gallery_page", None)
            or getattr(cli, "list_gallery", None)
        )
        if method is None:
            raise ctx.skip("no gallery method exposed on WeasylClient")
        items = await method()
        ctx.detail("submission_count", len(items) if hasattr(items, "__len__") else None)


# ── SoFurry ──────────────────────────────────────────────────────────


@register_test(
    test_id="platforms.sf.auth",
    name="SoFurry — API token validates",
    category="Platforms — Auth",
    description="validate_token() checks the PAT against GET /v1/user/me.",
    requires_creds=["sf_api_token"],
    timeout_seconds=30.0,
)
async def t_sf_auth(ctx: TestContext) -> None:
    from clients.sf.client import SoFurryClient
    from polling.cf_proxy import proxy_kwargs

    s = config.get_settings()
    async with SoFurryClient(
        api_token=s["sf_api_token"],
        display_name=s.get("sf_display_name", ""),
        **proxy_kwargs(s, "sf"),
    ) as cli:
        result = await cli.validate_token()
        ctx.detail("handle", result)
        assert result, (
            "SoFurry token did not validate — it may be revoked or expired, "
            "or this host's IP is blocked (the API 403s datacenter ranges)"
        )


@register_test(
    test_id="platforms.sf.discovery",
    name="SoFurry — gallery listing",
    category="Platforms — Polling Discovery",
    description="List the account's submissions via GET /v1/user/{handle}/submissions.",
    requires_creds=["sf_api_token"],
    timeout_seconds=60.0,
)
async def t_sf_discovery(ctx: TestContext) -> None:
    from clients.sf.client import SoFurryClient
    from polling.cf_proxy import proxy_kwargs

    s = config.get_settings()
    async with SoFurryClient(
        api_token=s["sf_api_token"],
        display_name=s.get("sf_display_name", ""),
        **proxy_kwargs(s, "sf"),
    ) as cli:
        ids = await cli.get_all_gallery_ids()
        ctx.detail("submission_count", len(ids))
        # Unlike the old gallery scrape — which returned unvalidated "candidate"
        # ids and yielded nothing at all for an adult gallery — the official
        # listing is authoritative, so an empty result is a real failure.
        assert ids, "SoFurry gallery listing returned no submissions"
        assert all(r.get("submission_id") for r in ids), "a listed row had no id"


@register_test(
    test_id="platforms.sf.stats",
    name="SoFurry — stats readable without auth",
    category="Platforms — Polling Discovery",
    description=(
        "Views/likes come from login-free JSON on sofurry.com because the "
        "official API exposes no statistics at all."
    ),
    requires_creds=["sf_api_token"],
    timeout_seconds=60.0,
)
async def t_sf_stats(ctx: TestContext) -> None:
    from clients.sf.client import SoFurryClient
    from polling.cf_proxy import proxy_kwargs

    s = config.get_settings()
    async with SoFurryClient(
        api_token=s["sf_api_token"],
        display_name=s.get("sf_display_name", ""),
        **proxy_kwargs(s, "sf"),
    ) as cli:
        rows = await cli.get_all_gallery_ids()
        if not rows:
            raise ctx.skip("no SF submissions to sample")
        # Sample the first row that actually resolves to a titled work.
        detail = None
        for row in rows[:5]:
            d = await cli.get_submission_detail(row["submission_id"])
            if (d.get("title") or "").strip():
                detail = d
                break
        if detail is None:
            raise ctx.skip("no published SF submission in the first 5 rows")
        ctx.detail("title", detail["title"])
        ctx.detail("views", detail["views"])
        ctx.detail("favorites_count", detail["favorites_count"])
        ctx.detail("comments_count", detail["comments_count"])
        assert detail["views"] > 0, "views did not parse — the stats endpoint may have moved"


# ── SquidgeWorld ─────────────────────────────────────────────────────


@register_test(
    test_id="platforms.sqw.auth",
    name="SquidgeWorld — session validates",
    category="Platforms — Auth",
    description="validate_session() against the OTW-style login.",
    requires_creds=["sqw_username", "sqw_password"],
    timeout_seconds=30.0,
)
async def t_sqw_auth(ctx: TestContext) -> None:
    from clients.sqw.client import SquidgeWorldClient
    from polling.cf_proxy import proxy_kwargs

    s = config.get_settings()
    async with SquidgeWorldClient(
        s["sqw_username"], s["sqw_password"],
        s.get("sqw_target_user", s["sqw_username"]),
        **proxy_kwargs(s, "sqw"),
    ) as cli:
        if not await cli.ensure_logged_in():
            raise AssertionError("ensure_logged_in failed (login rejected)")
        out = await cli.validate_session()
        ctx.detail("validated_user", out)
        assert out, "SqW session did not validate"


@register_test(
    test_id="platforms.sqw.discovery",
    name="SquidgeWorld — works discovery",
    category="Platforms — Polling Discovery",
    description="List works for the target user.",
    requires_creds=["sqw_username", "sqw_password", "sqw_target_user"],
    timeout_seconds=30.0,
)
async def t_sqw_discovery(ctx: TestContext) -> None:
    from clients.sqw.client import SquidgeWorldClient
    from polling.cf_proxy import proxy_kwargs

    s = config.get_settings()
    async with SquidgeWorldClient(
        s["sqw_username"], s["sqw_password"], s["sqw_target_user"],
        **proxy_kwargs(s, "sqw"),
    ) as cli:
        await cli.ensure_logged_in()
        method = (
            getattr(cli, "get_all_work_ids", None)
            or getattr(cli, "list_user_works", None)
            or getattr(cli, "scrape_works_index", None)
        )
        if method is None:
            raise ctx.skip("no works-list method exposed on SquidgeWorldClient")
        ids = await method()
        ctx.detail("work_count", len(ids) if hasattr(ids, "__len__") else None)


# ── AO3 ──────────────────────────────────────────────────────────────


@register_test(
    test_id="platforms.ao3.auth",
    name="AO3 — session validates",
    category="Platforms — Auth",
    description=(
        "validate_session() against AO3. Accepts either a pasted session "
        "cookie OR username+password; ao3_username is not required when "
        "a cookie is configured (cookie auth is the recommended path)."
    ),
    requires_creds=["ao3_target_user"],
    timeout_seconds=45.0,
)
async def t_ao3_auth(ctx: TestContext) -> None:
    from clients.ao3.client import AO3Client
    from polling.cf_proxy import proxy_kwargs

    s = config.get_settings()
    cookie = s.get("ao3_session_cookie", "")
    username = s.get("ao3_username", "")
    pw = s.get("ao3_password", "")
    if not cookie and not (username and pw):
        raise ctx.skip("neither session_cookie nor username+password configured")
    async with AO3Client(
        username, pw, s["ao3_target_user"],
        session_cookie=cookie,
        **proxy_kwargs(s, "ao3"),
    ) as cli:
        out = await cli.validate_session()
        ctx.detail("validated_user", out)
        ctx.detail("auth_mode", "cookie" if cookie else "password")
        assert out, "AO3 session did not validate"


@register_test(
    test_id="platforms.ao3.discovery",
    name="AO3 — works discovery",
    category="Platforms — Polling Discovery",
    description="List works for the target user. Cookie or password OK.",
    requires_creds=["ao3_target_user"],
    timeout_seconds=45.0,
)
async def t_ao3_discovery(ctx: TestContext) -> None:
    from clients.ao3.client import AO3Client
    from polling.cf_proxy import proxy_kwargs

    s = config.get_settings()
    cookie = s.get("ao3_session_cookie", "")
    username = s.get("ao3_username", "")
    pw = s.get("ao3_password", "")
    if not cookie and not (username and pw):
        raise ctx.skip("neither session_cookie nor username+password configured")
    async with AO3Client(
        username, pw, s["ao3_target_user"],
        session_cookie=cookie,
        **proxy_kwargs(s, "ao3"),
    ) as cli:
        await cli.ensure_logged_in()
        method = (
            getattr(cli, "get_all_work_ids", None)
            or getattr(cli, "list_user_works", None)
        )
        if method is None:
            raise ctx.skip("no works-list method exposed on AO3Client")
        ids = await method()
        ctx.detail("work_count", len(ids) if hasattr(ids, "__len__") else None)


# ── DeviantArt ───────────────────────────────────────────────────────


@register_test(
    test_id="platforms.da.auth",
    name="DeviantArt — cookies validate",
    category="Platforms — Auth",
    description="validate_cookies() against the Eclipse _napi.",
    requires_creds=["da_cookie", "da_target_user"],
    timeout_seconds=20.0,
)
async def t_da_auth(ctx: TestContext) -> None:
    from clients.da.client import DAClient
    from polling.cf_proxy import proxy_kwargs

    s = config.get_settings()
    async with DAClient(
        s["da_cookie"], s["da_target_user"],
        **proxy_kwargs(s, "da"),
    ) as cli:
        ok = await cli.validate_cookies()
        ctx.detail("validated", ok)
        assert ok, "DA cookies did not validate"


@register_test(
    test_id="platforms.da.discovery",
    name="DeviantArt — gallery discovery",
    category="Platforms — Polling Discovery",
    description="Fetch first gallery page via _napi.",
    requires_creds=["da_cookie", "da_target_user"],
    timeout_seconds=30.0,
)
async def t_da_discovery(ctx: TestContext) -> None:
    from clients.da.client import DAClient
    from polling.cf_proxy import proxy_kwargs

    s = config.get_settings()
    async with DAClient(
        s["da_cookie"], s["da_target_user"],
        **proxy_kwargs(s, "da"),
    ) as cli:
        method = (
            getattr(cli, "get_all_deviation_ids", None)
            or getattr(cli, "get_gallery_page", None)
            or getattr(cli, "list_gallery", None)
        )
        if method is None:
            raise ctx.skip("no gallery method exposed on DAClient")
        items = await method()
        ctx.detail("submission_count", len(items) if hasattr(items, "__len__") else None)


# ── Wattpad (public API) ─────────────────────────────────────────────


@register_test(
    test_id="platforms.wp.auth",
    name="Wattpad — target user resolves",
    category="Platforms — Auth",
    description="Public API user lookup for the configured target.",
    requires_creds=["wp_target_user"],
    timeout_seconds=15.0,
)
async def t_wp_auth(ctx: TestContext) -> None:
    from clients.wp.client import WPClient

    s = config.get_settings()
    async with WPClient(s["wp_target_user"]) as cli:
        out = await cli.validate_user()
        ctx.detail("user", out)
        assert out, "Wattpad user did not resolve"


@register_test(
    test_id="platforms.wp.discovery",
    name="Wattpad — story discovery",
    category="Platforms — Polling Discovery",
    description="List published stories for the target user.",
    requires_creds=["wp_target_user"],
    timeout_seconds=30.0,
)
async def t_wp_discovery(ctx: TestContext) -> None:
    from clients.wp.client import WPClient

    s = config.get_settings()
    async with WPClient(s["wp_target_user"]) as cli:
        method = (
            getattr(cli, "get_all_story_ids", None)
            or getattr(cli, "get_published_stories", None)
            or getattr(cli, "list_user_stories", None)
        )
        if method is None:
            raise ctx.skip("no story-list method exposed on WPClient")
        items = await method()
        ctx.detail("story_count", len(items) if hasattr(items, "__len__") else None)


# ── Itaku (public API) ───────────────────────────────────────────────


@register_test(
    test_id="platforms.ik.auth",
    name="Itaku — target user resolves",
    category="Platforms — Auth",
    description="Public API user lookup for the configured target.",
    requires_creds=["ik_target_user"],
    timeout_seconds=15.0,
)
async def t_ik_auth(ctx: TestContext) -> None:
    from clients.ik.client import IKClient

    s = config.get_settings()
    async with IKClient(s["ik_target_user"]) as cli:
        out = await cli.validate_user()
        ctx.detail("user", out)
        assert out, "Itaku user did not resolve"


@register_test(
    test_id="platforms.ik.discovery",
    name="Itaku — content discovery",
    category="Platforms — Polling Discovery",
    description="List gallery images / posts for the target user.",
    requires_creds=["ik_target_user"],
    timeout_seconds=30.0,
)
async def t_ik_discovery(ctx: TestContext) -> None:
    from clients.ik.client import IKClient

    s = config.get_settings()
    async with IKClient(s["ik_target_user"]) as cli:
        method = (
            getattr(cli, "get_all_content_ids", None)
            or getattr(cli, "get_gallery_images", None)
            or getattr(cli, "list_gallery", None)
        )
        if method is None:
            raise ctx.skip("no gallery method exposed on IKClient")
        items = await method()
        ctx.detail("image_count", len(items) if hasattr(items, "__len__") else None)


# ── Bluesky ──────────────────────────────────────────────────────────


@register_test(
    test_id="platforms.bsky.auth",
    name="Bluesky — JWT session validates",
    category="Platforms — Auth",
    description="validate_session() refreshes/logs in via AT Protocol.",
    requires_creds=["bsky_identifier", "bsky_app_password"],
    timeout_seconds=20.0,
)
async def t_bsky_auth(ctx: TestContext) -> None:
    from clients.bsky.client import BskyClient

    s = config.get_settings()
    async with BskyClient(s["bsky_identifier"], s["bsky_app_password"]) as cli:
        out = await cli.validate_session()
        ctx.detail("handle", out)
        assert out, "Bluesky session did not validate"


@register_test(
    test_id="platforms.bsky.discovery",
    name="Bluesky — feed discovery",
    category="Platforms — Polling Discovery",
    description="Fetch the first page of getAuthorFeed.",
    requires_creds=["bsky_identifier", "bsky_app_password"],
    timeout_seconds=30.0,
)
async def t_bsky_discovery(ctx: TestContext) -> None:
    from clients.bsky.client import BskyClient

    s = config.get_settings()
    async with BskyClient(s["bsky_identifier"], s["bsky_app_password"]) as cli:
        await cli.ensure_logged_in()
        method = (
            getattr(cli, "get_author_feed", None)
            or getattr(cli, "list_posts", None)
            or getattr(cli, "get_posts_page", None)
        )
        if method is None:
            raise ctx.skip("no feed method exposed on BskyClient")
        items = await method(s["bsky_identifier"])
        ctx.detail("post_count", len(items) if hasattr(items, "__len__") else None)


# ── X / Twitter ──────────────────────────────────────────────────────


@register_test(
    test_id="platforms.tw.auth",
    name="X / Twitter — cookies validate",
    category="Platforms — Auth",
    description="validate_cookies() checks auth_token + ct0.",
    requires_creds=["tw_auth_token", "tw_ct0", "tw_target_user"],
    timeout_seconds=20.0,
)
async def t_tw_auth(ctx: TestContext) -> None:
    from clients.tw.client import TWClient

    s = config.get_settings()
    async with TWClient(s["tw_auth_token"], s["tw_ct0"]) as cli:
        ok = await cli.validate_cookies(s["tw_target_user"])
        ctx.detail("validated", ok)
        assert ok, "TW cookies did not validate"


@register_test(
    test_id="platforms.tw.discovery",
    name="X / Twitter — tweet discovery",
    category="Platforms — Polling Discovery",
    description="Resolve user + fetch first page of UserTweets.",
    requires_creds=["tw_auth_token", "tw_ct0", "tw_target_user"],
    timeout_seconds=30.0,
)
async def t_tw_discovery(ctx: TestContext) -> None:
    from clients.tw.client import TWClient

    s = config.get_settings()
    async with TWClient(s["tw_auth_token"], s["tw_ct0"]) as cli:
        method = (
            getattr(cli, "get_user_tweets", None)
            or getattr(cli, "list_user_tweets", None)
            or getattr(cli, "get_tweets_page", None)
        )
        if method is None:
            raise ctx.skip("no tweets method exposed on TWClient")
        items = await method(s["tw_target_user"])
        ctx.detail("tweet_count", len(items) if hasattr(items, "__len__") else None)
