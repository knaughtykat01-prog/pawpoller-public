"""Progress everywhere (WORKLEVELS, spec 052): work, account and persona levels, audience
XP, and performance against "your usual".

Two separate ideas, so the numbers stay fair (spec "Why this exists"):

- **Level** is absolute and only goes up. ``score = views + 5·faves + 20·comments`` pooled
  over every upload of a work; each level is about double the response of the one before.
- **Performance** is relative and can move: a piece's response on each site at a fixed age
  (2, 7, 30 days) against the median of your earlier pieces there at the same age, blended
  by audience share.

A work's uploads are the ones the Library card pools (``masterpiece_queries.summarize_many``):
its posted publications plus, for artwork, its member uploads, de-duplicated on
``(platform, submission_id)``. Only works the Library actually has count, so an orphaned
publication earns nothing. Maths over polled numbers only, no AI (constitution I).
Design: ``specs/052-progress-everywhere/research.md``; guide §178.
"""
from __future__ import annotations

import logging
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone

import config
import xp
from database import platform_metrics

logger = logging.getLogger(__name__)

_FMT = "%Y-%m-%d %H:%M:%S"


def _t() -> dict:
    return config.PROGRESS_TABLE


# ── The scale (FR-001) ─────────────────────────────────────────────

def score(code: str, stats: dict | None) -> int:
    """One upload's score from canonical stats (``read_stats`` / ``canonical`` shape). A site
    with no views but a net score (the booru family) puts that score with its faves; a site
    with neither gives only what it has (research R1)."""
    spec = platform_metrics.get(code)
    if not spec or not stats:
        return 0
    w = _t()["weights"]
    views = (stats.get("views") or 0) if spec.views else 0
    faves = (stats.get("faves") or 0) if spec.faves else 0
    if not spec.views and spec.score:
        faves += max(0, stats.get("score") or 0)
    comments = (stats.get("comments") or 0) if spec.comments else 0
    return max(0, w["views"] * views + w["faves"] * faves + w["comments"] * comments)


def level_for_score(s: int) -> int:
    """``max(1, 1 + floor(log2(s / 10)))`` in integers: floor(log2(x)) is the bit length of
    floor(x) less one, so no float ever decides a boundary."""
    q = int(s) // _t()["level_base"]
    return max(1, q.bit_length())


def next_level_score(level: int) -> int:
    return _t()["level_base"] * 2 ** level


def next_hint(s: int, level: int) -> str:
    """"about 375 more views, or 75 faves": the gap in the cheapest plain words."""
    gap = max(0, next_level_score(level) - s)
    w = _t()["weights"]
    faves = -(-gap // w["faves"])
    return f"about {gap:,} more views, or {faves:,} fave{'' if faves == 1 else 's'}"


# ── Uploads and scores per work ───────────────────────────────────

def uploads(conn: sqlite3.Connection, key: tuple | None = None) -> dict:
    """``{(content_type, name): {(platform, submission_id): account_id}}``, or one work's."""
    where, args = "", ()
    if key:
        where, args = " AND content_type = ? AND story_name = ?", key
    out: dict = {}
    for ctype, name, plat, ext, acct in conn.execute(
            "SELECT content_type, story_name, platform, external_id, account_id FROM publications"
            " WHERE status = 'posted' AND content_type IN ('story', 'artwork')"
            " AND external_id IS NOT NULL AND external_id != ''" + where, args):
        out.setdefault((ctype, name), {})[(plat, str(ext))] = acct
    if not key or key[0] == "artwork":
        mwhere, margs = (" WHERE masterpiece_name = ?", (key[1],)) if key else ("", ())
        for name, plat, sid, acct in conn.execute(
                "SELECT masterpiece_name, platform, submission_id, account_id FROM masterpiece_members"
                + mwhere, margs):
            out.setdefault(("artwork", name), {}).setdefault((plat, str(sid)), acct)
    return out


def _stats(conn, ups: dict) -> dict:
    """``{(platform, sid): canonical stats}`` for every upload, one query per platform."""
    ids: dict[str, set] = {}
    for per_work in ups.values():
        for plat, sid in per_work:
            if platform_metrics.get(plat):
                ids.setdefault(plat, set()).add(sid)
    out = {}
    for plat, sids in ids.items():
        for sid, st in platform_metrics.read_stats(conn, plat, sids).items():
            out[(plat, sid)] = st
    return out


def work_scores(conn: sqlite3.Connection, works: dict | None = None) -> dict:
    """``{(content_type, name): {score, views, faves, comments, sites, accounts, no_view_sites}}``
    for every work in ``works`` (``{key: title}``; the Library's catalogue when None)."""
    works = _catalogue() if works is None else works
    ups = {k: v for k, v in uploads(conn).items() if k in works}
    stats = _stats(conn, ups)
    out = {}
    for key in works:
        per = ups.get(key, {})
        total = platform_metrics.pooled((plat, stats.get((plat, sid))) for plat, sid in per)
        s = sum(score(plat, stats.get((plat, sid))) for plat, sid in per)
        sites = {plat for plat, sid in per if (plat, sid) in stats}
        out[key] = {"score": s, "views": total["views"], "faves": total["faves"],
                    "comments": total["comments"], "sites": sites, "platforms": {p for p, _ in per},
                    "accounts": {a for a in per.values() if a is not None},
                    "no_view_sites": sorted(c for c in sites if not platform_metrics.get(c).views)}
    return out


def _catalogue() -> dict:
    """``{(content_type, name): title}`` for every story and artwork the Library lists."""
    from posting import artwork_reader, story_reader
    out = {("story", s["name"]): s.get("title") or s["name"].replace("_", " ")
           for s in story_reader.list_stories()}
    out.update({("artwork", a["name"]): a.get("title") or a["name"].replace("_", " ")
                for a in artwork_reader.list_artworks()})
    return out


# ── Sync: levels, peaks and audience XP (FR-002, FR-006, FR-007) ───────────

def sync(conn: sqlite3.Connection, works: dict | None = None) -> dict:
    """Upsert every work's and account's level, keep peaks, and pay 10 × L audience XP once
    for each level a work has reached and not been paid for. Does not commit.

    The first run back-pays every work from level 0 (operator, 22 Oct, research R4) and marks
    those levels announced, so the jump arrives as one catch-up line rather than a toast per
    work. Works on never-post accounts only, and junked works, are levelled but not paid.
    Returns ``{"scores": work_scores, "moved": {key: (old, new)}, "titles": {key: title}}``;
    ``moved`` holds works whose score changed, for ``medals.evaluate``."""
    from database import accounts as accounts_db
    from database import masterpiece_queries
    works = _catalogue() if works is None else works
    now = xp._now_utc()
    first = not xp._state(conn, "progress_baselined_at")
    never = accounts_db.never_post_ids()
    junk = {n for n, s in masterpiece_queries.statuses(conn).items() if s == "junk"}
    per_level = _t()["audience_xp_per_level"]
    have = {(r[0], r[1]): r[2:] for r in conn.execute(
        "SELECT content_type, work_name, score, peak_level, paid_level FROM work_levels")}
    moved, backpay = {}, 0
    scores = work_scores(conn, works)
    for key, w in scores.items():
        lvl = level_for_score(w["score"])
        old_score, peak, paid = have.get(key, (None, 0, 0))
        peak = max(peak, lvl)
        payable = (w["accounts"] - never or not w["accounts"]) and w["sites"] \
            and not (key[0] == "artwork" and key[1] in junk)
        if payable:
            for L in range(paid + 1, peak + 1):
                got = xp.award(conn, "audience", f"audience:{key[0]}:{key[1]}:{L}", per_level * L,
                               now, source="audience")
                backpay += got if first else 0
            paid = max(paid, peak)
        if old_score is None:
            # Seen for the first time: on the baseline run every level is already "announced";
            # after it, a new work's levels above 1 get their toast. OR IGNORE: two reads at once
            # (Overview + the level check) can both see it as new; the award keys already dedupe XP.
            conn.execute(
                "INSERT OR IGNORE INTO work_levels (content_type, work_name, score, level, peak_level, paid_level,"
                " announced_level, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (*key, w["score"], lvl, peak, paid, peak if first else 1, now))
        elif old_score != w["score"] or have[key][1] != peak or have[key][2] != paid:
            conn.execute(
                "UPDATE work_levels SET score = ?, level = ?, peak_level = ?, paid_level = ?, updated_at = ?"
                " WHERE content_type = ? AND work_name = ?", (w["score"], lvl, peak, paid, now, *key))
        if old_score is not None and old_score != w["score"]:
            moved[key] = (old_score, w["score"])
    _sync_accounts(conn, now)
    if first:
        xp._set_state(conn, "progress_baselined_at", now)
        if backpay:
            xp._set_state(conn, "audience_backpay_pending", str(backpay))
    return {"scores": scores, "moved": moved, "titles": works}


def _account_score(platform: str, st: dict) -> int:
    return score(platform, {"views": st.get("views"), "faves": st.get("favorites"),
                            "comments": st.get("comments"), "score": st.get("score")})


def _sync_accounts(conn, now: str) -> None:
    """``acct:<id>`` and ``persona:<id>`` levels from the per-account totals the Accounts page
    and persona cards already show (research R9). Levels only; accounts never give XP."""
    from database import accounts as accounts_db
    scores: dict[str, int] = {}
    for a in accounts_db.list_accounts(conn):
        st = accounts_db.account_stats(conn, a["account_id"], a["platform"])
        if not st:
            continue
        s = _account_score(a["platform"], st)
        scores[f"acct:{a['account_id']}"] = s
        if a.get("persona_id"):
            scores[f"persona:{a['persona_id']}"] = scores.get(f"persona:{a['persona_id']}", 0) + s
    conn.executemany(
        "INSERT INTO account_levels (subject, score, level, peak_level, updated_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(subject) DO UPDATE SET score = excluded.score, level = excluded.level,"
        " peak_level = MAX(account_levels.peak_level, excluded.level), updated_at = excluded.updated_at"
        " WHERE account_levels.score != excluded.score",
        [(k, s, level_for_score(s), level_for_score(s), now) for k, s in scores.items()])


def carry(conn: sqlite3.Connection, ctype: str, drop: str, keep: str) -> None:
    """Two works became one (a merge): the survivor keeps the higher peak, paid and announced
    levels of the two, so no level is ever paid twice (spec edge case, research R4)."""
    rows = conn.execute(
        "SELECT work_name, score, level, peak_level, paid_level, announced_level FROM work_levels"
        " WHERE content_type = ? AND work_name IN (?, ?)", (ctype, drop, keep)).fetchall()
    if not any(r[0] == drop for r in rows):
        return
    peak, paid, ann = (max(r[i] for r in rows) for i in (3, 4, 5))
    kept = next((r for r in rows if r[0] == keep), None)
    conn.execute("DELETE FROM work_levels WHERE content_type = ? AND work_name = ?", (ctype, drop))
    conn.execute(
        "INSERT INTO work_levels (content_type, work_name, score, level, peak_level, paid_level,"
        " announced_level, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(content_type, work_name) DO UPDATE SET peak_level = excluded.peak_level,"
        " paid_level = excluded.paid_level, announced_level = excluded.announced_level",
        (ctype, keep, kept[1] if kept else 0, kept[2] if kept else 1, peak, paid, ann, xp._now_utc()))


# The poll hook runs once per platform per account after every round; one sync a minute is
# plenty, and the next GET /api/progress catches up anyway.
_last_poll_sync = 0.0


def after_poll() -> None:
    """``check_milestones_batch`` calls this after each poll. Levels, audience XP and medals are
    earned with no browser open, committed before any network call, and a fault here is
    logged and never fails the poll."""
    global _last_poll_sync
    if time.monotonic() - _last_poll_sync < 60:
        return
    _last_poll_sync = time.monotonic()
    import medals
    from database.db import get_connection
    try:
        conn = get_connection()
        try:
            medals.evaluate(conn, sync(conn))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("progress: sync after poll skipped: %s", e, exc_info=True)


# ── Performance against your usual (FR-008, FR-009, research R5) ─────────

_AT: dict = {}            # (code, sid, age_h) -> stats at that age, once the age is past
_CACHE: dict = {}         # (ctype, name) -> (monotonic, result)
_SHARE: dict = {}         # "shares" -> (monotonic, {code: weight})
_TTL = 600


def _parse(s: str) -> datetime | None:
    try:
        return datetime.strptime(s[:19], _FMT)
    except (TypeError, ValueError):
        return None


def _norm_id(sid: str):
    return int(sid) if str(sid).isdigit() else sid


def _at_age(conn, code: str, sid: str, posted: datetime, age_h: int, now: datetime) -> dict | None:
    """The stats of one upload at ``age_h`` after posting: its latest snapshot within the
    tolerance either side of that age. None when no poll landed near it."""
    k = (code, sid, age_h)
    if k in _AT:
        return _AT[k]
    spec = platform_metrics.get(code)
    tol = timedelta(hours=_t()["perf_tolerance_h"])
    at = posted + timedelta(hours=age_h)
    if not spec or not spec.columns or at - tol > now:
        return None
    try:
        row = conn.execute(
            f"SELECT {', '.join(spec.columns)} FROM {spec.snapshots} WHERE {spec.id_col} = ?"
            " AND polled_at >= ? AND polled_at <= ? ORDER BY polled_at DESC LIMIT 1",
            (_norm_id(sid), (at - tol).strftime(_FMT), (at + tol).strftime(_FMT))).fetchone()
    except sqlite3.Error:
        return None
    out = spec.canonical(dict(zip(spec.columns, row))) if row else None
    if at + tol < now:
        _AT[k] = out              # the past doesn't change
    return out


def _history(conn, code: str, account_id, before: datetime) -> list[tuple[str, datetime]]:
    """Your earlier pieces on a site and account, newest first: ``[(sid, posted)]``."""
    spec = platform_metrics.get(code)
    col = platform_metrics._date_col(conn, spec.table)
    if not col:
        return []
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({spec.table})")}
    sql, args = f"SELECT {spec.id_col}, {col} FROM {spec.table}", ()
    if "account_id" in cols and account_id is not None:
        sql, args = sql + " WHERE account_id = ?", (account_id,)
    out = []
    for sid, raw in conn.execute(sql, args):
        p = _parse(platform_metrics.normalize_posted(raw))
        if p and p < before:
            out.append((str(sid), p))
    out.sort(key=lambda r: r[1], reverse=True)
    return out


def _shares(conn) -> dict:
    """Each site's share of your audience: the score your uploads there gained over the last
    90 days (latest totals less each upload's last snapshot before the window)."""
    hit = _SHARE.get("shares")
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    from database import accounts as accounts_db
    never = accounts_db.never_post_ids()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_t()["perf_share_days"])).strftime(_FMT)
    out = {}
    for code in platform_metrics.ALL_CODES:
        spec = platform_metrics.get(code)
        if not spec.columns:
            continue
        try:
            tcols = {r[1] for r in conn.execute(f"PRAGMA table_info({spec.table})")}
            skip = (f" WHERE account_id NOT IN ({','.join('?' * len(never))})"
                    if never and "account_id" in tcols else "")
            now_rows = conn.execute(f"SELECT {spec.id_col}, {', '.join(spec.columns)} FROM {spec.table}{skip}",
                                    tuple(never) if skip else ()).fetchall()
            then = {str(r[0]): r[1:] for r in conn.execute(
                f"SELECT {spec.id_col}, {', '.join(spec.columns)} FROM ("
                f"SELECT *, ROW_NUMBER() OVER (PARTITION BY {spec.id_col} ORDER BY polled_at DESC) AS rn"
                f" FROM {spec.snapshots} WHERE polled_at < ?) WHERE rn = 1", (cutoff,))}
        except sqlite3.Error:
            continue
        gained = 0
        for r in now_rows:
            cur = score(code, spec.canonical(dict(zip(spec.columns, r[1:]))))
            old = then.get(str(r[0]))
            gained += max(0, cur - (score(code, spec.canonical(dict(zip(spec.columns, old)))) if old else 0))
        if gained:
            out[code] = gained
    _SHARE["shares"] = (time.monotonic(), out)
    return out


def _depth(st: dict | None) -> float | None:
    if not st or not st.get("views"):
        return None
    return ((st.get("faves") or 0) + (st.get("comments") or 0)) / st["views"]


def _blend(rows: list[dict], shares: dict, field: str = "ratio") -> float | None:
    got = [r for r in rows if r.get(field) is not None]
    if not got:
        return None
    weights = [shares.get(r["code"], 0) for r in got]
    if not sum(weights):
        weights = [1] * len(got)
    return sum(w * r[field] for w, r in zip(weights, got)) / sum(weights)


def _window(conn, ups: dict, posted: dict, age_h: int, now: datetime, shares: dict) -> dict:
    """Every site of one work at one age: its value, your usual there, and the ratio."""
    t = _t()
    rows = []
    for code in sorted({p for p, _ in ups}):
        mine = [(sid, acct) for (p, sid), acct in ups.items() if p == code and (p, sid) in posted]
        if not mine:
            continue
        vals = [_at_age(conn, code, sid, posted[(code, sid)], age_h, now) for sid, _ in mine]
        vals = [v for v in vals if v is not None]
        value = sum(score(code, v) for v in vals) if vals else None
        first_sid, acct = min(mine, key=lambda m: posted[(code, m[0])])
        prev_vals, prev_depths = [], []
        for sid, p in _history(conn, code, acct, posted[(code, first_sid)]):
            if (code, sid) in ups:
                continue
            st = _at_age(conn, code, sid, p, age_h, now)
            if st is None:
                continue
            prev_vals.append(score(code, st))
            d = _depth(st)
            if d is not None:
                prev_depths.append(d)
            if len(prev_vals) >= t["perf_history_cap"]:
                break
        row = {"code": code, "value": value, "usual": None, "ratio": None}
        if len(prev_vals) < t["perf_min_history"] or value is None:
            row["reason"] = "not enough history yet" if value is not None else "no poll near this age"
        else:
            usual = statistics.median(prev_vals)
            row["usual"] = round(usual, 1)
            if usual > 0:
                row["ratio"] = round(value / usual, 2)
                row["top_pct"] = 100 * (1 + sum(1 for v in prev_vals if v > value)) / (len(prev_vals) + 1)
            else:
                row["reason"] = "not enough history yet"
        depths = [_depth(v) for v in vals if _depth(v) is not None]
        row["depth"] = sum(depths) / len(depths) if depths else None
        row["usual_depth"] = statistics.median(prev_depths) if len(prev_depths) >= t["perf_min_history"] else None
        rows.append(row)
    blended = _blend(rows, shares)
    return {"age_h": age_h, "sites": rows, "blended": round(blended, 2) if blended is not None else None,
            "top_pct": _blend(rows, shares, "top_pct"), "depth": _blend(rows, shares, "depth"),
            "usual_depth": _blend(rows, shares, "usual_depth")}


def _posted(conn, ups: dict) -> dict:
    """``{(platform, sid): posted datetime}`` from each site's own post date."""
    by_plat: dict[str, list] = {}
    for plat, sid in ups:
        by_plat.setdefault(plat, []).append(sid)
    out = {}
    for plat, sids in by_plat.items():
        for sid, raw in platform_metrics.read_posted_at(conn, plat, sids).items():
            p = _parse(raw)
            if p:
                out[(plat, sid)] = p
    return out


def windows(conn: sqlite3.Connection, key: tuple, ages=None) -> dict:
    """``{age_h: window}`` for a work at each age it has reached (default: the perf windows)."""
    ups = uploads(conn, key).get(key, {})
    posted = _posted(conn, ups)
    if not posted:
        return {}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    age = (now - min(posted.values())).total_seconds() / 3600
    tol = _t()["perf_tolerance_h"]
    shares = _shares(conn)
    return {a: _window(conn, ups, posted, a, now, shares)
            for a in (ages or _t()["perf_windows_h"]) if a <= age + tol}


def _per_follower(conn, key: tuple, main: dict) -> float | None:
    """The piece's score at the shown age per follower of the accounts it is on.
    # ponytail: today's follower count, not the count on the post date; accounts keep only
    # the latest figure. Use a follower series if one is ever stored."""
    accts = {a for a in uploads(conn, key).get(key, {}).values() if a is not None}
    if not accts:
        return None
    ph = ",".join("?" * len(accts))
    followers = conn.execute(f"SELECT COALESCE(SUM(follower_count), 0) FROM accounts WHERE account_id IN ({ph})",
                             tuple(accts)).fetchone()[0]
    total = sum(r["value"] for r in main["sites"] if r["value"] is not None)
    return round(total / followers, 3) if followers else None


def performance(conn: sqlite3.Connection, ctype: str, name: str) -> dict | None:
    """The work page's performance line and breakdown (contracts/progress-api.md), or None
    while the piece is younger than 2 days. Cached ten minutes per work."""
    key = (ctype, name)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    ws = windows(conn, key)
    result = None
    if ws:
        main = ws[max(ws)]
        top = main["top_pct"]
        result = {
            "at_age_h": main["age_h"],
            "blended": main["blended"],
            "sites": [{k: r[k] for k in ("code", "value", "usual", "ratio", "reason") if k in r}
                      for r in main["sites"]],
            "early": {str(a): ws[a]["blended"] for a in (48, 168) if a in ws},
            "depth": round(main["depth"], 3) if main["depth"] is not None else None,
            "usual_depth": round(main["usual_depth"], 3) if main["usual_depth"] is not None else None,
            "spread": {"posted": len(main["sites"]),
                       "at_or_above": sum(1 for r in main["sites"] if (r["ratio"] or 0) >= 1)},
            "per_follower": _per_follower(conn, key, main),
        }
        if top is not None and top <= 25:
            result["top_pct"] = max(1, round(top))
    _CACHE[key] = (time.monotonic(), result)
    return result


def work_detail(conn: sqlite3.Connection, ctype: str, name: str) -> dict | None:
    """``GET /api/progress/work/{type}/{name}``: level, peak, score, the next step and
    performance. None for a work with no level row yet (the route's 404)."""
    row = conn.execute("SELECT score, level, peak_level FROM work_levels WHERE content_type = ? AND work_name = ?",
                       (ctype, name)).fetchone()
    if not row:
        return None
    s, lvl, peak = row
    key = (ctype, name)
    w = work_scores(conn, {key: name}).get(key, {})
    import medals
    perf = performance(conn, ctype, name)
    if perf is not None:          # the page's own medal check, on windows already cached
        medals.evaluate_work(conn, key, w, windows(conn, key, (48, 168, 720, 1440)))
    return {"level": lvl, "peak_level": peak, "score": s, "next_level_score": next_level_score(peak),
            "next_hint": next_hint(s, peak), "no_view_sites": w.get("no_view_sites", []),
            "medals": medals.for_subject(conn, f"work:{ctype}:{name}"),
            "performance": perf}
