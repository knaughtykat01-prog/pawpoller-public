"""Round-robin account selection for rate-limited platforms.

Some platforms (notably X/Twitter's scraper backends) share a *per-IP* rate
budget across all of a user's accounts. Measured on prod: the datacenter IP
throttles after only ~2 X account-scrapes per window, then forces a >8 min
wait — so polling 3+ X accounts back-to-back in one cycle guarantees the tail
accounts get 429'd and time out.

Round-robin fixes this by polling only the N least-recently-polled accounts
each cycle and rotating the rest to the next cycle, keeping every cycle inside
the per-IP budget. Selection is derived from the platform's poll-log timestamps
(not an in-memory cursor), so it stays fair across redeploys and process
restarts, which reset the poll timer.

This module is deliberately pure and stateless: `select_roundrobin` takes the
account list, the batch size, and a {account_id: last_poll_iso} map, and
returns the accounts to poll this cycle. The caller supplies the last-poll map
(e.g. from ``tw_queries.get_tw_last_poll_by_account``).
"""

from __future__ import annotations


def select_roundrobin(accts: list, batch_size: int,
                      last_poll_by_id: dict) -> list:
    """Return the ``batch_size`` least-recently-polled accounts.

    Never-polled accounts sort first, then oldest ``last_poll`` first; ties
    (and never-polled accounts) break by ``account_id`` ascending so the order
    is deterministic. A ``batch_size`` of 0 or less, or one that is at least the
    number of accounts, disables round-robin and returns every account (a copy),
    so callers can pass it through unconditionally.

    Args:
        accts: account rows/dicts, each indexable by ``"account_id"``.
        batch_size: max accounts to poll this cycle.
        last_poll_by_id: ``{account_id: iso_timestamp_str}``; a missing id means
            the account has never been polled.
    """
    if batch_size <= 0 or batch_size >= len(accts):
        return list(accts)

    def _key(a):
        aid = a["account_id"]
        ts = last_poll_by_id.get(aid)
        # (has-been-polled, timestamp, id): never-polled (False, "") sorts ahead
        # of any real ISO timestamp; among polled accounts the oldest string
        # (ISO 8601 sorts lexicographically) comes first; id is the tie-break.
        return (ts is not None, ts or "", aid)

    return sorted(accts, key=_key)[:batch_size]


def effective_batch(configured_batch: int, *, official_primary: bool,
                    save_tokens: bool) -> int:
    """Batch size to actually apply this cycle, given the PRIMARY X backend.

    The scraper backends (gallery-dl / GraphQL) share one per-IP rate budget,
    so whenever a scraper is the primary poll path they **must** round-robin —
    it's throttle protection, not a choice. Only when the IP-agnostic official
    API is the *primary* backend can we poll **every** account each cycle
    (unless the user opts into throttling to spend fewer paid reads).

    ``official_primary`` must mean the official API is what actually polls first
    — i.e. it's enabled AND no scraper preempts it. Since 2.119.0 gallery-dl is
    the default primary and the official API is only a paid *fallback*, so a mere
    "token present" no longer implies IP-agnostic polling; the caller computes
    ``official_primary = official_api.is_enabled(s) and not gallerydl.is_enabled(s)``.

    Returns 0 (= poll all, round-robin disabled) when throttling shouldn't
    apply, otherwise the configured batch.
    """
    if official_primary and not save_tokens:
        return 0
    return configured_batch
