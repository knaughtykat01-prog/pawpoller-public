"""Supporter figures — spec 084. The ONE place each figure is worked out (constitution VI).

Members are counted in memory by :func:`figures` and only the totals are stored (FR-062, FR-SEC-008): never who a
supporter is, their email, a member id, or what one person pays. ``record_snapshot`` validates before writing; a
partial member read never reaches it (the poller writes nothing then).

What the numbers mean (research R3, checked against Patreon's own creator page on 13 Nov 2026):

- paying (``paid_count``) = ``active_patron`` and not on a free trial; gifted memberships count as paying.
- on a trial (``trial_count``) is apart; free members (``free_count``) = ``is_free_member`` (a former patron who
  stayed on free counts here).
- total (``total_count``) = Patreon's own ``patron_count`` (paid + free, the figure under its Membership number).
- monthly (``monthly_cents``) = the campaign's ``pledge_sum`` in ``pledge_sum_currency``: exactly what Patreon shows
  the creator. ``is_monthly`` false = billed per post.
- per tier: patrons = active patrons on it (free members on a free tier); pledged = Σ ``campaign_pledge_amount_cents``
  (already in the page's currency), before Patreon's adjustments, so the tiers needn't add up to the headline.
  ``currently_entitled_amount_cents`` is in each patron's own currency and is never read or summed.
- new / lost since the last poll: active patrons whose pledge started after it / former patrons whose pledge ended
  after it (UTC). The first poll records 0 / 0.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
TITLE_MAX = 200
HISTORY_MAX_DAYS = 730


def _when(text) -> datetime | None:
    """An ISO time from Patreon, or a stored "YYYY-MM-DD HH:MM:SS" (UTC), as an aware datetime; None if unreadable."""
    s = str(text or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _count(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError("counts and cents must be whole numbers") from None
    if n < 0:
        raise ValueError("counts and cents can't be negative")
    return n


def figures(members, campaign: dict, since: str | None) -> dict:
    """The snapshot for one complete read: ``members`` are the already-cut member records (research R3 fields plus
    ``tier_id``), ``campaign`` the campaign's own figures and tiers, ``since`` the previous poll's time (None on the
    first). Returns the kwargs for :func:`record_snapshot`."""
    after = _when(since)
    tiers = {str(t["tier_id"]): {"tier_id": str(t["tier_id"]), "title": str(t.get("title") or "")[:TITLE_MAX],
                                 "amount_cents": int(t.get("amount_cents") or 0), "patron_count": 0,
                                 "monthly_cents": 0}
             for t in (campaign.get("tiers") or [])}
    paid = trial = free = new = lost = 0
    for m in members:
        status = m.get("patron_status")
        tier = tiers.get(str(m.get("tier_id") or ""))
        if status == "active_patron":
            if m.get("is_free_trial"):
                trial += 1
            else:
                paid += 1
            if tier is not None:
                tier["patron_count"] += 1
                tier["monthly_cents"] += max(int(m.get("campaign_pledge_amount_cents") or 0), 0)
            start = _when(m.get("pledge_relationship_start"))
            if after and start and start > after:
                new += 1
        elif status == "former_patron":
            end = _when(m.get("pledge_relationship_end"))
            if after and end and end > after:
                lost += 1
        if m.get("is_free_member"):
            free += 1
            if tier is not None and tier["amount_cents"] == 0 and status != "active_patron":
                tier["patron_count"] += 1
    total = campaign.get("patron_count")
    return {
        "paid_count": paid, "trial_count": trial, "free_count": free,
        "total_count": int(total) if total is not None else None,
        "monthly_cents": int(campaign.get("pledge_sum") or 0),
        "currency": str(campaign.get("pledge_sum_currency") or campaign.get("currency") or "").upper(),
        "is_monthly": bool(campaign.get("is_monthly", True)),
        "new_count": new, "lost_count": lost,
        "tiers": list(tiers.values()),
    }


def record_snapshot(conn: sqlite3.Connection, *, platform: str, account_id: int, paid_count, trial_count,
                    free_count, total_count, monthly_cents, currency: str, is_monthly: bool, new_count, lost_count,
                    tiers=(), polled_at: str | None = None) -> int:
    """Write one complete poll's totals and its tiers. Raises ValueError (nothing written) on a bad value:
    counts and cents are integers ≥ 0; currency matches ``^[A-Z]{3}$``; titles are cut to 200 characters."""
    if platform not in ("pat", "ssa"):
        raise ValueError("unknown supporter platform")
    if not _CURRENCY_RE.match(str(currency or "")):
        raise ValueError("currency must be a three-letter code")
    row = [_count(paid_count), _count(trial_count), _count(free_count),
           None if total_count is None else _count(total_count), _count(monthly_cents)]
    nl = [_count(new_count), _count(lost_count)]
    tier_rows = [(str(t["tier_id"])[:40], str(t.get("title") or "")[:TITLE_MAX], _count(t.get("amount_cents") or 0),
                  _count(t.get("patron_count") or 0), _count(t.get("monthly_cents") or 0)) for t in tiers]
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO supporter_snapshots (platform, account_id, polled_at, paid_count, trial_count, free_count, "
        "total_count, monthly_cents, currency, is_monthly, new_count, lost_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (platform, int(account_id), ts, *row, currency, int(bool(is_monthly)), *nl))
    sid = cur.lastrowid
    if sid is None:                    # never after an INSERT; the tier rows need its id
        raise sqlite3.DatabaseError("the supporter totals row wasn't written")
    conn.executemany(
        "INSERT INTO supporter_tier_snapshots (snapshot_id, tier_id, title, amount_cents, patron_count, monthly_cents) "
        "VALUES (?,?,?,?,?,?)", [(sid, *t) for t in tier_rows])
    return sid


def last_polled_at(conn: sqlite3.Connection, account_id: int) -> str | None:
    row = conn.execute("SELECT MAX(polled_at) FROM supporter_snapshots WHERE account_id = ?", (account_id,)).fetchone()
    return row[0] if row else None


_LIVE = "account_id IN (SELECT account_id FROM accounts)"     # removed accounts never show (FR-SEC-007)


def _tiers(conn, snapshot_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT tier_id, title, amount_cents, patron_count, monthly_cents FROM supporter_tier_snapshots "
        "WHERE snapshot_id = ? ORDER BY amount_cents, title COLLATE NOCASE", (snapshot_id,))]


def summary(conn: sqlite3.Connection) -> list[dict]:
    """Per account: the latest snapshot, its tiers, and the change since the one before. Money is never summed
    across accounts or currencies."""
    out = []
    for (aid,) in conn.execute(f"SELECT DISTINCT account_id FROM supporter_snapshots WHERE {_LIVE} ORDER BY account_id"):
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM supporter_snapshots WHERE account_id = ? ORDER BY polled_at DESC, id DESC LIMIT 2", (aid,))]
        latest = rows[0]
        prev = rows[1] if len(rows) > 1 else None
        latest["tiers"] = _tiers(conn, latest["id"])
        before = {t["tier_id"]: t["patron_count"] for t in _tiers(conn, prev["id"])} if prev else {}
        for t in latest["tiers"]:          # FR-031: each tier's change since the poll before (a new tier has none)
            t["change"] = t["patron_count"] - before[t["tier_id"]] if t["tier_id"] in before else None
        latest["change"] = ({k: latest[k] - prev[k] for k in ("paid_count", "free_count", "monthly_cents")}
                            if prev and prev["currency"] == latest["currency"] else None)
        out.append(latest)
    return out


def history(conn: sqlite3.Connection, account_id: int, days: int) -> list[dict]:
    if not 1 <= int(days) <= HISTORY_MAX_DAYS:
        raise ValueError(f"days must be between 1 and {HISTORY_MAX_DAYS}")
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM supporter_snapshots WHERE account_id = ? AND {_LIVE} AND polled_at >= datetime('now', ?) "
        "ORDER BY polled_at", (account_id, f"-{int(days)} days"))]
    for r in rows:
        r["tiers"] = _tiers(conn, r["id"])
    return rows


def period(conn: sqlite3.Connection, account_id: int, start: str, end: str) -> dict:
    a, b = _when(start), _when(end)
    if a is None or b is None or a > b:
        raise ValueError("the period's start must be a date on or before its end")
    row = conn.execute(
        f"SELECT COALESCE(SUM(new_count),0), COALESCE(SUM(lost_count),0), COUNT(*) FROM supporter_snapshots "
        f"WHERE account_id = ? AND {_LIVE} AND polled_at >= ? AND polled_at <= ?",
        (account_id, a.strftime("%Y-%m-%d %H:%M:%S"), b.strftime("%Y-%m-%d 23:59:59" if len(str(end)) <= 10
                                                                  else "%Y-%m-%d %H:%M:%S"))).fetchone()
    return {"new": row[0], "lost": row[1], "polls": row[2]}


def delete_for_account(conn: sqlite3.Connection, account_id: int) -> int:
    """Removing an account removes its supporter rows (the data map's "how to remove it")."""
    conn.execute("DELETE FROM supporter_tier_snapshots WHERE snapshot_id IN "
                 "(SELECT id FROM supporter_snapshots WHERE account_id = ?)", (account_id,))
    return conn.execute("DELETE FROM supporter_snapshots WHERE account_id = ?", (account_id,)).rowcount
