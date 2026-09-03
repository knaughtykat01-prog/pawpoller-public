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


def get_poll_progress() -> dict:
    """Registry: platform code → its live progress dict (4.3.2).

    The twin of :func:`get_poll_cycles` for the other thing every poller
    exports. ``/api/poll/all-progress`` hand-listed seventeen of these and
    stopped at ``e621``, so FurryNetwork, Furbooru and Telegram could not
    report progress even on the server — the same shape as the four other
    lists that stopped in the same place.

    Names follow one convention with two originals: ``poll_progress`` in
    ``polling.poller`` for Inkbunny (it came first) and ``progress`` in
    ``polling.tg_poller``; everything else is ``<code>_poll_progress``.
    """
    from polling.poller import poll_progress
    from polling.tg_poller import progress as tg_progress
    out = {"ib": poll_progress, "tg": tg_progress}
    for code in get_poll_cycles():
        if code in out:
            continue
        mod = __import__(f"polling.{code}_poller", fromlist=[f"{code}_poll_progress"])
        out[code] = getattr(mod, f"{code}_poll_progress")
    return out


def get_poll_cycles() -> dict:
    """Registry: platform code → its ``run_<code>_poll_cycle`` coroutine fn.

    Imported lazily so importing this module doesn't pull every poller in at
    import time.

    ⚠ This is the ONE list. ``server.py`` used to keep a second copy of it as
    ``account_aware``, and ``main.py`` a third as sixteen hand-written thread
    functions that stopped at ``ig`` — which is how e621, FurryNetwork,
    Furbooru and Telegram ended up polled on the server and never on the
    desktop (4.3.2). Both now read this.
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
    from polling.tg_poller import run_tg_poll_cycle
    return {
        "ib": run_poll_cycle, "fa": run_fa_poll_cycle, "ws": run_ws_poll_cycle,
        "da": run_da_poll_cycle, "wp": run_wp_poll_cycle, "ik": run_ik_poll_cycle,
        "bsky": run_bsky_poll_cycle, "tw": run_tw_poll_cycle, "sf": run_sf_poll_cycle,
        "sqw": run_sqw_poll_cycle, "ao3": run_ao3_poll_cycle, "mast": run_mast_poll_cycle,
        "tum": run_tum_poll_cycle, "pix": run_pix_poll_cycle, "thr": run_thr_poll_cycle,
        "ig": run_ig_poll_cycle, "e621": run_e621_poll_cycle, "fn": run_fn_poll_cycle,
        "fbr": run_fbr_poll_cycle, "tg": run_tg_poll_cycle,
    }


async def poll_platform_accounts(platform, account_id=None, *, run_cycle=None):
    """Poll one account (``account_id`` given) or every enabled account (None).

    A specific ``account_id`` polls just that account — an explicit "Poll Now"
    is honoured whatever the settings say. When ``account_id`` is None the
    platform's enabled accounts are enumerated and each is polled in sequence.

    ⚠ A platform with no credentials is not polled at all. This paragraph used
    to claim "the cycle self-skips if uncredentialed", which is not true of
    every cycle — the AO3 one authenticates with an empty cookie and lets the
    site answer, so an install that had never entered an AO3 credential
    collected a 403 "Shields are up!" every cycle. The gate is now here, where
    it can be relied on, and it asks ``DEFAULT_CRED_CHECKS`` — the same question
    ``seed_default_accounts`` asks when deciding whether the platform gets an
    account row at all.

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
    check = accounts_db.DEFAULT_CRED_CHECKS.get(platform, lambda s: True)
    configured = bool(check(settings))
    rows = []
    try:
        conn = get_connection()
        try:
            accounts_db.seed_default_accounts(conn, settings)
            rows = [a for a in accounts_db.list_accounts(conn)
                    if a["platform"] == platform]
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        if not configured:
            logger.debug("%s: enumeration failed (%s) and no credentials — nothing to poll",
                         platform, e)
            return
        logger.warning("%s: account enumeration failed (%s) — polling default account only",
                       platform, e)
        await run_cycle()
        return

    accts = [a for a in rows if a["enabled"]]

    if not accts:
        # ⚠ This used to be a bare `await run_cycle()`. Zero enabled accounts
        # meant "poll the default anyway" — a fallback from before account rows
        # were seeded FROM credentials. They are now (seed_default_accounts
        # skips any platform whose DEFAULT_CRED_CHECKS says it has none), so
        # zero rows means the platform is NOT CONFIGURED, and polling it
        # regardless sends a real request to a site the user never connected.
        # A tester who has never entered an AO3 credential collected an AO3 403
        # "Shields are up!" every cycle from this line — and AO3 throttles per
        # IP, so the app was spending goodwill on a platform nobody uses.
        if not configured:
            logger.debug("%s: not configured — skipping poll", platform)
            return
        if rows:
            # Rows exist and every one is switched off. That is a decision the
            # user made on the Accounts page, not an install to fall back for.
            logger.info("%s: all accounts disabled — skipping poll", platform)
            return
        await run_cycle()
        return
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
