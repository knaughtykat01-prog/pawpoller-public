"""A Patreon account's own saved values (spec 084, data-model "Settings").

``pat_campaign_id``, ``pat_is_nsfw`` and ``pat_tiers`` are written ONLY here, and only from PawPoller's own read of
Patreon (``record_campaign``): never from a sign-in request's body (FR-SEC-006). The poster's ``refusal()`` reads
them so it needs no network call; ``post()`` and every poll refresh them.
"""
from __future__ import annotations

import hashlib
import json
import time

import config

# A passed sign-in check, held in memory for the save that follows it: {sha256(cookie): (campaign, rules, when)}.
# Every save path calls check_session() first; the save refuses a cookie with no fresh entry (FR-SEC-006).
_checked: dict[str, tuple] = {}
CHECK_TTL_S = 600


def _digest(cookie: str) -> str:
    return hashlib.sha256((cookie or "").strip().encode("utf-8")).hexdigest()


async def check_session(cookie: str, stored: str) -> tuple[bool, str, str]:
    """(ok, page name Patreon reported, reason). ok only when the session's own page is ``stored`` (the typed page
    name, case-insensitive). On ok, the page's id, 18+ flag and tiers are held for the save."""
    import httpx
    from clients.pat.client import PatAuthError, PatChallenged, PatClient, PatMissing, PatUnavailable, same_page
    if not (stored or "").strip():
        return False, "", "Type your Patreon page name first (as in patreon.com/<name>)."
    try:
        async with PatClient(cookie) as c:
            camp = await c.current_campaign()
            if not same_page(camp["vanity"], stored):
                return False, camp["vanity"], (f"That browser is signed in to the Patreon page {camp['vanity'] or 'with no name'}, "
                                               f"not {stored.strip()}. Nothing was saved.")
            rules = await c.audience_rules(camp["campaign_id"])
    except PatChallenged as e:
        return False, "", f"{e} Try again later."
    except PatAuthError as e:
        return False, "", str(e)
    except PatMissing as e:
        return False, "", str(e)
    except (PatUnavailable, httpx.HTTPError):
        return False, "", "Patreon didn't answer the account check. Try again in a minute."
    now = time.monotonic()
    for k in [k for k, v in _checked.items() if now - v[2] > CHECK_TTL_S]:
        del _checked[k]
    _checked[_digest(cookie)] = (camp, rules, now)
    return True, camp["vanity"], ""


def was_checked(cookie: str) -> bool:
    entry = _checked.get(_digest(cookie))
    return bool(entry) and time.monotonic() - entry[2] <= CHECK_TTL_S


def record_checked(account_id: int, cookie: str) -> bool:
    """After the save: record the checked page's id, 18+ flag and tiers on the account. False if no fresh check."""
    entry = _checked.pop(_digest(cookie), None)
    if not entry or time.monotonic() - entry[2] > CHECK_TTL_S:
        return False
    record_campaign(account_id, entry[0], entry[1])
    return True

_PLAIN = ("pat_campaign_id", "pat_is_nsfw", "pat_tiers", "pat_default_audience", "pat_story_collections")
# Every pat_* key an account can hold: the disconnect and the account removal clear all of them.
ALL_KEYS = ("pat_username", "pat_cookie") + _PLAIN


def _is_default(account_id: int) -> bool:
    from database import accounts as accts
    from database.db import get_connection
    conn = get_connection()
    try:
        row = accts.get_account(conn, account_id)
    finally:
        conn.close()
    return bool(row["is_default"]) if row else True


def _json(value, fallback):
    if isinstance(value, (list, dict)):
        return value
    try:
        v = json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback
    return v if isinstance(v, type(fallback)) else fallback


def values(account_id: int, settings: dict | None = None) -> dict:
    """{campaign_id, is_nsfw, tiers, default_audience, story_collections} for one account."""
    s = settings if settings is not None else config.get_settings()
    d = _is_default(account_id)
    get = lambda f: s.get(config.account_setting_key(account_id, f, d))   # noqa: E731
    return {"campaign_id": str(get("pat_campaign_id") or ""),
            "is_nsfw": get("pat_is_nsfw") in (True, "true", "1", 1),
            "tiers": _json(get("pat_tiers"), []),
            "default_audience": _json(get("pat_default_audience"), {}),
            "story_collections": _json(get("pat_story_collections"), {})}


def save(account_id: int, **fields) -> None:
    d = _is_default(account_id)
    out = {}
    for f, v in fields.items():
        key = f if f.startswith("pat_") else f"pat_{f}"
        if key not in _PLAIN:
            raise ValueError(f"not a Patreon account value: {key}")
        out[config.account_setting_key(account_id, key, d)] = json.dumps(v) if isinstance(v, (list, dict)) else v
    if out:
        config.save_settings(out)


def record_campaign(account_id: int, campaign: dict, rules: list[dict] | None = None) -> None:
    """The page's id and 18+ flag (and its tier list, when read) from PawPoller's own read of Patreon."""
    vals = {"pat_campaign_id": str(campaign["campaign_id"]), "pat_is_nsfw": bool(campaign.get("is_nsfw"))}
    if rules is not None:
        vals["pat_tiers"] = [{k: r[k] for k in ("rule_id", "kind", "tier_id", "title", "amount_cents")} for r in rules]
    save(account_id, **vals)


def clear(account_id: int) -> list[str]:
    """Remove every pat_* key this account holds (disconnect / remove). Returns the keys cleared."""
    d = _is_default(account_id)
    keys = [config.account_setting_key(account_id, f, d) for f in ALL_KEYS]
    current = config.get_settings()
    gone = [k for k in keys if k in current]
    if gone:
        config.delete_settings_keys(gone)
    return gone
