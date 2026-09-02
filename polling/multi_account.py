"""Manual per-account poll dispatch.

The per-platform "Poll Now" endpoints historically triggered
``run_<code>_poll_cycle()`` with no account, which polls only the platform's
*default* account. This module lets a manual poll target **one** account or
**all** enabled accounts for a platform — backing the account picker on the
dashboard poll button (``POST /api/poll/trigger/{code}?account_id=``).

The all-accounts loop mirrors ``server.py._poll_accounts`` /
``main.py._poll_platform_accounts``: enumerate enabled account rows, skip any
without usable credentials, and run the cycle once per account with its
``account_id`` — one account's failure never aborts the rest.
"""

from __future__ import annotations

import logging

import config

logger = logging.getLogger(__name__)


def get_poll_cycles() -> dict:
    """Registry: platform code → its ``run_<code>_poll_cycle`` coroutine fn.

    Imported lazily so importing this module doesn't pull every poller in at
    import time. Kept in sync with ``server.py``'s ``account_aware`` map.
    """
    from polling.poller import run_poll_cycle
    from polling.fa_poller import run_fa_poll_cycle
    from polling.ws_poller import run_ws_poll_cycle
    from polling.da_poller import run_da_poll_cycle
    from polling.wp_poller import run_wp_poll_cycle
    from polling.ik_poller import run_ik_poll_cycle
    from polling.bsky_poller import run_bsky_poll_cycle
    from polling.tw_poller import run_tw_poll_cycle
    from polling.sf_poller import run_sf_poll_cycle
    from polling.sqw_poller import run_sqw_poll_cycle
    from polling.ao3_poller import run_ao3_poll_cycle
    from polling.mast_poller import run_mast_poll_cycle
    from polling.tum_poller import run_tum_poll_cycle
    from polling.pix_poller import run_pix_poll_cycle
    from polling.thr_poller import run_thr_poll_cycle
    from polling.ig_poller import run_ig_poll_cycle
    from polling.e621_poller import run_e621_poll_cycle
    from polling.fn_poller import run_fn_poll_cycle
    from polling.fbr_poller import run_fbr_poll_cycle
    return {
        "ib": run_poll_cycle, "fa": run_fa_poll_cycle, "ws": run_ws_poll_cycle,
        "da": run_da_poll_cycle, "wp": run_wp_poll_cycle, "ik": run_ik_poll_cycle,
        "bsky": run_bsky_poll_cycle, "tw": run_tw_poll_cycle, "sf": run_sf_poll_cycle,
        "sqw": run_sqw_poll_cycle, "ao3": run_ao3_poll_cycle, "mast": run_mast_poll_cycle,
        "tum": run_tum_poll_cycle, "pix": run_pix_poll_cycle, "thr": run_thr_poll_cycle,
        "ig": run_ig_poll_cycle, "e621": run_e621_poll_cycle, "fn": run_fn_poll_cycle,
        "fbr": run_fbr_poll_cycle,
    }


async def poll_platform_accounts(platform, account_id=None, *, run_cycle=None):
    """Poll one account (``account_id`` given) or every enabled account (None).

    A specific ``account_id`` polls just that account. When ``account_id`` is
    None the platform's enabled accounts are enumerated and each is polled in
    sequence; if the accounts table can't be read or has no rows, it falls back
    to a single default-account poll (the cycle self-skips if uncredentialed).

    ``run_cycle`` is looked up from ``get_poll_cycles()`` when omitted; callers
    that already hold the coroutine (or tests) may pass it directly.
    """
    if run_cycle is None:
        run_cycle = get_poll_cycles().get(platform)
        if run_cycle is None:
            raise ValueError(f"unknown platform: {platform}")

    # Single-account manual poll.
    if account_id is not None:
        await run_cycle(account_id)
        return

    # All enabled accounts.
    from database.db import get_connection
    from database import accounts as accounts_db

    settings = config.get_settings()
    try:
        conn = get_connection()
        try:
            accounts_db.seed_default_accounts(conn, settings)
            accts = [a for a in accounts_db.list_accounts(conn, enabled_only=True)
                     if a["platform"] == platform]
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("%s: account enumeration failed (%s) — polling default account only",
                       platform, e)
        await run_cycle()
        return

    if not accts:
        await run_cycle()
        return

    check = accounts_db.DEFAULT_CRED_CHECKS.get(platform, lambda s: True)
    try:
        from polling.notifications import current_alert_account
    except Exception:  # noqa: BLE001
        current_alert_account = None
    from polling.rate_limit import tw_account_stagger
    polled_count = 0

    for a in accts:
        creds = config.resolve_account_credentials(
            platform, a["account_id"], bool(a["is_default"]), settings)
        if not check(creds):
            continue  # this account has no usable credentials — skip it
        # Space X account polls into bursts to dodge the per-IP throttle
        # (no-op for other platforms and for the first burst).
        await tw_account_stagger(platform, polled_count, settings)
        polled_count += 1
        if current_alert_account is not None:
            current_alert_account.set((platform, a["account_id"]))
        try:
            await run_cycle(a["account_id"])
        except Exception as e:  # noqa: BLE001 — one account must not kill the rest
            logger.error("%s account %s (%s) poll failed: %s",
                         platform, a["account_id"], a.get("label") or "", e)
