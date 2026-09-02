"""Notification diagnostics.

Two destructive (Telegram send, Windows toast) gated behind confirm.
The digest builder is read-only.
"""

from __future__ import annotations

import time

import httpx

import config
from testing.registry import TestContext, register_test


@register_test(
    test_id="notifications.telegram.test_message",
    name="Send a test Telegram message",
    category="Notifications",
    description=(
        "DESTRUCTIVE: sends 'PawPoller diagnostics test: <timestamp>' "
        "to the configured chat. Use to confirm the bot + chat_id pair "
        "actually delivers messages."
    ),
    destructive=True,
    requires_creds=["telegram_bot_token", "telegram_chat_id"],
    timeout_seconds=15.0,
)
async def t_telegram_send(ctx: TestContext) -> None:
    s = config.get_settings()
    token = s["telegram_bot_token"]
    chat = s["telegram_chat_id"]
    text = f"PawPoller diagnostics test: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"
    async with httpx.AsyncClient(timeout=10.0) as cli:
        resp = await cli.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text},
        )
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        ctx.detail("status", resp.status_code)
        ctx.detail("ok", data.get("ok"))
        ctx.detail("message_id", data.get("result", {}).get("message_id"))
        assert resp.status_code == 200 and data.get("ok"), f"send failed: {data}"


@register_test(
    test_id="notifications.toast.send",
    name="Fire a Windows toast",
    category="Notifications",
    description=(
        "DESTRUCTIVE (visual only): shows a 'PawPoller Diagnostics' "
        "toast. Skipped on non-Windows / when winotify isn't installed."
    ),
    destructive=True,
    timeout_seconds=10.0,
)
async def t_toast(ctx: TestContext) -> None:
    try:
        from winotify import Notification
    except ImportError:
        raise ctx.skip("winotify not installed (desktop-only test)")
    n = Notification(
        app_id="PawPoller",
        title="Diagnostics",
        msg="Toast test from PawPoller Diagnostics suite",
        duration="short",
    )
    n.show()
    ctx.detail("dispatched", True)


@register_test(
    test_id="notifications.digest.data_fetch",
    name="Telegram digest data-fetch helpers",
    category="Notifications",
    description=(
        "Exercise the read-only data helpers behind send_digest_report() — "
        "_get_digest_deltas() and _get_platform_totals() — across all "
        "polling platforms. Confirms the queries the digest depends on "
        "still execute against the current schema. Does NOT send a digest."
    ),
)
async def t_digest_data_fetch(ctx: TestContext) -> None:
    try:
        from polling import telegram as tg
    except ImportError:
        raise ctx.skip("polling.telegram unavailable")
    deltas_fn = getattr(tg, "_get_digest_deltas", None)
    totals_fn = getattr(tg, "_get_platform_totals", None)
    if deltas_fn is None or totals_fn is None:
        raise ctx.skip(
            "digest data helpers (_get_digest_deltas / _get_platform_totals) "
            "not exposed on polling.telegram in this build"
        )
    from database.db import get_connection

    # The platforms the digest iterates over and their (snap_table, sub_table).
    platforms = {
        "inkbunny": ("daily_snapshots", "submissions"),
        "furaffinity": ("fa_daily_snapshots", "fa_submissions"),
        "weasyl": ("ws_daily_snapshots", "ws_submissions"),
        "sofurry": ("sf_daily_snapshots", "sf_submissions"),
        "squidgeworld": ("sqw_daily_snapshots", "sqw_works"),
        "ao3": ("ao3_daily_snapshots", "ao3_works"),
        "deviantart": ("da_daily_snapshots", "da_deviations"),
        "wattpad": ("wp_daily_snapshots", "wp_stories"),
        "itaku": ("ik_daily_snapshots", "ik_content"),
        "bluesky": ("bsky_daily_snapshots", "bsky_posts"),
    }
    conn = get_connection()
    try:
        ok: list[str] = []
        errors: dict[str, str] = {}
        for plat, (snap_t, sub_t) in platforms.items():
            try:
                deltas = deltas_fn(conn, snap_t, sub_t, plat, 6)
                totals = totals_fn(conn, sub_t, plat)
                assert isinstance(deltas, dict), f"{plat} deltas not a dict"
                assert isinstance(totals, dict), f"{plat} totals not a dict"
                ok.append(plat)
            except Exception as exc:  # noqa: BLE001
                errors[plat] = f"{type(exc).__name__}: {exc}"
        ctx.detail("ok", ok)
        ctx.detail("errors", errors)
        assert not errors, f"{len(errors)} platforms erred: {list(errors)}"
    finally:
        conn.close()


# ── Non-destructive payload format tests ─────────────────────────
# Verify the helpers that build Telegram message bodies still
# produce the expected output. These don't talk to Telegram or
# Windows — they exercise pure formatting code so a regression in
# the helper signatures or output shape surfaces in Diagnostics
# without burning a real-message budget.


@register_test(
    test_id="notifications.format.telegram_summary",
    name="format_telegram_summary builds the expected layout",
    category="Notifications",
    description=(
        "Non-destructive: invokes format_telegram_summary() with a "
        "header and a list of items. Confirms the header is preserved, "
        "items render with the '  • ' bullet prefix, and the overflow "
        "tail fires when the list exceeds max_visible."
    ),
)
async def t_format_telegram_summary(ctx: TestContext) -> None:
    try:
        from polling.notifications import format_telegram_summary
    except ImportError as e:
        raise ctx.skip(f"polling.notifications not importable: {e}")
    items = [f"item {i}" for i in range(7)]
    result = format_telegram_summary("<b>Header</b>", items, max_visible=3)
    ctx.detail("output", result)
    lines = result.split("\n")
    assert lines[0] == "<b>Header</b>", f"header not preserved: {lines[0]!r}"
    bulleted = [ln for ln in lines if ln.startswith("  • ")]
    assert len(bulleted) == 3, f"expected 3 bulleted lines, got {len(bulleted)}"
    overflow = [ln for ln in lines if "more" in ln]
    assert overflow, "overflow tail missing for items > max_visible"
    assert "4 more" in overflow[0], f"overflow count wrong: {overflow[0]!r}"


@register_test(
    test_id="notifications.format.error_classify",
    name="_classify_error maps raw exceptions to user-friendly labels",
    category="Notifications",
    description=(
        "Non-destructive: probes _classify_error against a handful of "
        "representative error strings (Cloudflare block, 429, timeout, "
        "SSL, generic). Catches accidental regressions to the pattern "
        "list that would degrade Telegram error UX to bare stack-traces."
    ),
)
async def t_classify_error(ctx: TestContext) -> None:
    try:
        from polling.telegram import _classify_error
    except ImportError as e:
        raise ctx.skip(f"polling.telegram not importable: {e}")
    cases = [
        ("Cloudflare challenge encountered",          "Cloudflare"),
        ("429 Too Many Requests",                     "rate"),
        ("ReadTimeout while contacting platform",     "Timed out"),
        ("ConnectError: connection refused",          "Connection"),
        ("SSL handshake failure",                     "SSL"),
    ]
    misses = []
    results = []
    for raw, expected_substr in cases:
        label, hint = _classify_error(raw)
        results.append({"raw": raw, "label": label, "hint": hint})
        if expected_substr.lower() not in label.lower():
            misses.append({"raw": raw, "expected_in_label": expected_substr, "got_label": label})
    ctx.detail("results", results)
    ctx.detail("misses", misses)
    # Allow generic "Error" fallback only for cases we don't expect to
    # match a specific pattern. The five above all should map to a
    # non-generic label.
    assert not misses, f"{len(misses)} classifier misses: {misses}"


@register_test(
    test_id="notifications.format.error_for_telegram",
    name="_format_error_for_telegram includes platform name + label + hint",
    category="Notifications",
    description=(
        "Non-destructive: composes a fake AO3 throttle error through "
        "_format_error_for_telegram and asserts the platform name, "
        "label, and hint all appear in the output."
    ),
)
async def t_format_error_for_telegram(ctx: TestContext) -> None:
    try:
        from polling.telegram import _format_error_for_telegram
    except ImportError as e:
        raise ctx.skip(f"polling.telegram not importable: {e}")
    out = _format_error_for_telegram("ao3", "429 Too Many Requests from AO3")
    ctx.detail("output", out)
    assert "AO3" in out, f"platform name missing: {out!r}"
    assert "rate" in out.lower(), f"rate-limit label missing: {out!r}"
    # Hint is wrapped in <i>...</i> tags via the formatter
    assert "<i>" in out, f"hint formatting missing: {out!r}"
