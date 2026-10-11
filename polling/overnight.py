"""Overnight (spec 026): what happened while you were away, in one read.

Everything here is read from records PawPoller already keeps (FR-010): the per-site snapshot tables, the
Library's piece ↔ upload links, the Inbox, the watcher tables + follower snapshots, the posting log, the
Posts page's publications, the poll logs and the sign-in checks. Nothing is collected for it.

A post's value at a moment is its latest snapshot in the day before that moment (polls run every few
hours), so a window's gain is ``value(end) - value(start)``. Posts first seen inside the window have no
start value and are left out of the gains (they show under "what PawPoller did" instead). Score sites
(e621, Furbooru) are kept apart from views, as everywhere else (``platform_metrics.pooled``).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from database import platform_metrics as pm

logger = logging.getLogger(__name__)

MAX_DAYS = 7                 # a longer absence is capped (the weekly email covers more)
LOOKBACK = timedelta(hours=24)
USUAL_DAYS = 7               # "usual" = the same clock hours on each of the 7 days before
USUAL_MIN_DAYS = 3           # fewer earlier nights with data → no comparison, not a guess
SHOW_AFTER = ("6", "8", "12", "morning", "never")
MORNING_HOUR = 5
_NAMED_WATCHERS = {"ib": ("watchers", "1=1"), "fa": ("fa_watchers", "confirmed=1 AND is_spam=0"),
                   "sf": ("sf_watchers", "1=1")}
_TOTAL_KEYS = ("views", "score", "faves", "comments")


def _sql(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(v) -> datetime | None:
    """A stored time (SQLite 'YYYY-MM-DD HH:MM:SS' UTC, or ISO with or without a zone) as aware UTC."""
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def _keys(spec) -> list[str]:
    return [k for k in _TOTAL_KEYS if getattr(spec, k)]


def _values_at(conn, spec, t: datetime) -> dict[str, dict]:
    """``{submission_id: {key: value}}`` from each post's latest snapshot in the day up to ``t``."""
    keys = _keys(spec)
    if not keys:
        return {}
    idc, snap = spec.id_col, spec.snapshots
    cols = ", ".join(f"s.{getattr(spec, k)}" for k in keys)
    rows = conn.execute(
        f"SELECT s.{idc}, {cols} FROM {snap} s JOIN (SELECT {idc} AS sid, MAX(polled_at) AS m FROM {snap}"
        f" WHERE polled_at > ? AND polled_at <= ? GROUP BY {idc}) x ON s.{idc} = x.sid AND s.polled_at = x.m",
        (_sql(t - LOOKBACK), _sql(t))).fetchall()
    return {str(r[0]): {k: int(r[i + 1] or 0) for i, k in enumerate(keys)} for r in rows}


def changes(conn, code: str, start: datetime, end: datetime) -> tuple[dict, bool]:
    """Per-post gains on one site over [start, end]: ``({sid: {"gain": {...}, "before": {...},
    "after": {...}}}, had_data)``. ``had_data`` is False when the site had no snapshots at ``start``."""
    spec = pm.BY_CODE.get(code)
    if not spec or not spec.snapshots:
        return {}, False
    try:
        before = _values_at(conn, spec, start)
        after = _values_at(conn, spec, end) if before else {}
    except sqlite3.Error as e:
        logger.debug("overnight: %s snapshots unavailable: %s", code, e)
        return {}, False
    out = {}
    for sid, now in after.items():
        was = before.get(sid)
        if was is None:
            continue
        gain = {k: max(now[k] - was[k], 0) for k in now}
        if any(gain.values()):
            out[sid] = {"gain": gain, "before": was, "after": now}
    return out, bool(before)


def _named_followers(conn, start: datetime, end: datetime) -> list[dict]:
    out = []
    for code, (table, where) in _NAMED_WATCHERS.items():
        try:
            rows = conn.execute(
                f"SELECT username, first_seen_at FROM {table} WHERE {where} AND first_seen_at > ?"
                f" AND first_seen_at <= ? ORDER BY first_seen_at DESC", (_sql(start), _sql(end))).fetchall()
        except sqlite3.Error:
            continue
        out += [{"name": r[0], "platform": code, "at": r[1]} for r in rows]
    out.sort(key=lambda f: f["at"] or "", reverse=True)
    return out


def _counted_followers(conn, start: datetime, end: datetime) -> dict[str, int]:
    """Follower gains per site from the follower-count snapshots, for sites that don't name followers."""
    out: dict[str, int] = {}
    try:
        accts = conn.execute("SELECT account_id, platform FROM accounts").fetchall()
    except sqlite3.Error:
        return out

    def at(aid, t):
        r = conn.execute("SELECT followers FROM account_follower_snapshots WHERE account_id = ? AND polled_at > ?"
                         " AND polled_at <= ? ORDER BY polled_at DESC LIMIT 1",
                         (aid, _sql(t - LOOKBACK), _sql(t))).fetchone()
        return None if r is None else int(r[0] or 0)
    for aid, plat in accts:
        if plat in _NAMED_WATCHERS:
            continue
        try:
            a, b = at(aid, start), at(aid, end)
        except sqlite3.Error:
            return out
        if a is not None and b is not None and b > a:
            out[plat] = out.get(plat, 0) + (b - a)
    return out


def totals(conn, start: datetime, end: datetime, *, keep_changes: bool = False) -> dict:
    """The window's four totals (views and score apart), the sites they cover and, on request, the
    per-site per-post changes they were added up from."""
    t = {"views": 0, "score": 0, "faves": 0, "comments": 0}
    per_site, covered = {}, []
    for code in pm.ALL_CODES:
        ch, had = changes(conn, code, start, end)
        if had:
            covered.append(code)
        if not ch:
            continue
        pooled = pm.pooled((code, c["gain"]) for c in ch.values())
        for k in t:
            t[k] += pooled[k]
        if keep_changes:
            per_site[code] = ch
    named = _named_followers(conn, start, end)
    counted = _counted_followers(conn, start, end)
    t["followers"] = len(named) + sum(counted.values())
    out = {"totals": t, "covered": covered, "named_followers": named, "counted_followers": counted}
    if keep_changes:
        out["changes"] = per_site
    return out


def usual(conn, start: datetime, end: datetime) -> dict | None:
    """Average gains over the same clock hours on each of the previous 7 days. None with fewer than 3
    earlier windows that have data, or for a window longer than a day (no like-for-like comparison)."""
    if end - start > timedelta(hours=24):
        return None
    sums = {"views": 0, "score": 0, "faves": 0, "comments": 0, "followers": 0}
    days = 0
    for k in range(1, USUAL_DAYS + 1):
        shift = timedelta(days=k)
        r = totals(conn, start - shift, end - shift)
        if not r["covered"]:
            continue
        days += 1
        for key in sums:
            sums[key] += r["totals"][key]
    if days < USUAL_MIN_DAYS:
        return None
    return {k: round(v / days, 1) for k, v in sums.items()}


# ── Pieces ────────────────────────────────────────────────────────────────

def _piece_index(conn) -> dict[tuple[str, str], dict]:
    """(platform, submission_id) → the Library piece it belongs to: masterpiece members first, then the
    posting publications (the Library pools both — 4.47.0)."""
    idx: dict[tuple[str, str], dict] = {}
    try:
        for plat, sid, name in conn.execute(
                "SELECT platform, submission_id, masterpiece_name FROM masterpiece_members"):
            idx[(plat, str(sid))] = {"kind": "artwork", "name": name, "chapter": None}
    except sqlite3.Error:
        pass
    try:
        rows = conn.execute("SELECT platform, external_id, story_name, content_type, chapter_index"
                            " FROM publications WHERE external_id != ''").fetchall()
    except sqlite3.Error:
        rows = []
    for plat, eid, name, ctype, ch in rows:
        story = (ctype or "story") == "story"
        idx.setdefault((plat, str(eid)), {"kind": "story" if story else "artwork", "name": name,
                                         "chapter": (ch or 0) + 1 if story else None})
    return idx


def _named(name: str, kind: str = "", _cache: dict | None = None) -> str:
    """A Library piece's display title — its title from masterpiece.json / story.json — not the folder
    name (4.69.1: the sheet showed folder names). Falls back to the folder name with spaces."""
    if not name:
        return ""
    if _cache is not None and name in _cache:
        return _cache[name]
    title = ""
    readers = ("story", "artwork") if kind == "story" else ("artwork", "story")
    for r in readers:
        try:
            if r == "artwork":
                from posting import artwork_reader
                title = artwork_reader.load_artwork(name).title
            else:
                from posting import story_reader
                title = story_reader.load_story(name).title
        except Exception:
            title = ""
        if title:
            break
    title = title or name.replace("_", " ")
    if _cache is not None:
        _cache[name] = title
    return title


def _title(conn, code: str, sid: str) -> str:
    spec = pm.BY_CODE.get(code)
    try:
        r = conn.execute(f"SELECT title FROM {spec.table} WHERE {spec.id_col} = ?", (sid,)).fetchone()
    except sqlite3.Error:
        r = None
    return (r[0] if r and r[0] else "") or "(untitled)"


def _headline_metric(code: str) -> str:
    spec = pm.BY_CODE.get(code)
    return "score" if spec and spec.family == "score" else ("views" if spec and spec.views else "faves")


def _series(conn, members, start: datetime, end: datetime, points: int = 8) -> list[int]:
    """Cumulative headline gain across a piece's uploads at evenly spaced moments through the window."""
    times = [start + (end - start) * i / points for i in range(points + 1)]
    total = [0] * len(times)
    for code, sid in members:
        spec = pm.BY_CODE[code]
        col = getattr(spec, _headline_metric(code))
        if not col:
            continue
        try:
            rows = conn.execute(
                f"SELECT polled_at, {col} FROM {spec.snapshots} WHERE {spec.id_col} = ? AND polled_at > ?"
                f" AND polled_at <= ? ORDER BY polled_at", (sid, _sql(start - LOOKBACK), _sql(end))).fetchall()
        except sqlite3.Error:
            continue
        snaps = [(parse_ts(r[0]), int(r[1] or 0)) for r in rows]
        base = None
        for i, t in enumerate(times):
            v = None
            for at, val in snaps:
                if at and at <= t:
                    v = val
            if v is None:
                continue
            base = v if base is None else base
            total[i] += v - base
    return total


def top_pieces(conn, changes_by_site: dict, start: datetime, end: datetime, limit: int = 3) -> list[dict]:
    from polling import telegram as tg
    idx = _piece_index(conn)
    groups: dict[tuple, dict] = {}
    for code, posts in changes_by_site.items():
        for sid, c in posts.items():
            ref = idx.get((code, sid))
            key = (ref["kind"], ref["name"]) if ref else ("post", code, sid)
            g = groups.setdefault(key, {"ref": ref, "code": code, "sid": sid, "sites": {}, "members": [],
                                        "gain": {"views": 0, "score": 0, "faves": 0, "comments": 0},
                                        "chapters": {}, "milestones": []})
            g["members"].append((code, sid))
            p = pm.pooled([(code, c["gain"])])
            for k in g["gain"]:
                g["gain"][k] += p[k]
            metric = _headline_metric(code)
            g["sites"][code] = g["sites"].get(code, 0) + c["gain"].get(metric, 0)
            if ref and ref.get("chapter"):
                g["chapters"][ref["chapter"]] = g["chapters"].get(ref["chapter"], 0) + sum(c["gain"].values())
            ladder = tg._get_milestones().get(metric) or []
            hit = tg._crossed_milestone(c["after"].get(metric, 0), c["before"].get(metric, 0), ladder)
            if hit:
                g["milestones"].append({"platform": code, "metric": metric, "value": hit})

    def rank(g):
        x = g["gain"]
        return (x["views"] + x["score"] + 5 * x["faves"] + 10 * x["comments"], x["views"])
    out = []
    for key, g in sorted(groups.items(), key=lambda kv: rank(kv[1]), reverse=True)[:limit]:
        ref = g["ref"]
        piece = {"gain": g["gain"], "milestones": g["milestones"][:2],
                 "sites": [{"platform": c, "label": pm.BY_CODE[c].label_for(_headline_metric(c)), "gain": n}
                           for c, n in sorted(g["sites"].items(), key=lambda kv: -kv[1]) if n],
                 "series": _series(conn, g["members"], start, end), "thumb": "", "thumb_platform": ""}
        if ref and ref["kind"] == "artwork":
            from database import masterpiece_queries as mq
            piece.update(title=_named(ref["name"], "artwork"), href=f"#/masterpieces/{ref['name']}", kind="artwork")
            try:
                s = mq.summarize(conn, ref["name"])
                piece.update(thumb=s.get("cover_thumb") or "", thumb_platform=s.get("cover_platform") or "")
            except Exception:
                pass
        elif ref:
            ch = max(g["chapters"], key=g["chapters"].get) if g["chapters"] else None
            piece.update(title=_named(ref["name"], ref["kind"]), chapter=ch, kind="story",
                         href=f"#/posting/{ref['name']}")
        else:
            piece.update(title=_title(conn, g["code"], g["sid"]), kind="post",
                         href=f"#/{g['code']}/submission/{g['sid']}")
        out.append(piece)
    return out


# ── Comments, what PawPoller did, best time ──────────────────────────────

def comments(conn, start: datetime, end: datetime, limit: int = 3) -> dict:
    from database import inbox_queries
    rows = [r for r in inbox_queries.get_inbox(conn, unhandled_only=True, limit=500)
            if (lambda t: t and start < t <= end)(parse_ts(r.get("first_seen_at")))]
    return {"count": len(rows), "items": [
        {"platform": r["platform"], "comment_id": r["comment_id"], "author": r.get("author") or "",
         "body": (r.get("body") or "")[:280], "title": r.get("submission_title") or "",
         "at": r.get("first_seen_at")} for r in rows[:limit]]}


def did(conn, start: datetime, end: datetime) -> dict:
    """What PawPoller did: attention first (failed posts not since fixed, sign-ins that need redoing),
    then what went out, then one line for the routine checks."""
    attention, posted = [], []
    s, e = _sql(start), _sql(end)
    try:
        rows = conn.execute(
            "SELECT l.platform, l.story_name, l.chapter_index, l.status, l.error_message, l.created_at,"
            " q.scheduled_at FROM posting_log l LEFT JOIN posting_queue q ON q.queue_id = l.queue_id"
            " WHERE l.created_at > ? AND l.created_at <= ? ORDER BY l.created_at", (s, e)).fetchall()
    except sqlite3.Error:
        rows = []
    ok_after: dict[tuple, str] = {}
    for r in rows:
        if r[3] == "success":
            ok_after[(r[0], r[1])] = r[5]
    went: dict[str, dict] = {}
    names: dict[str, str] = {}
    for plat, name, ch, status, err, at, sched in rows:
        title = _named(name or "", _cache=names)
        if status == "success":
            w = went.setdefault(name, {"title": title, "sites": [], "at": at, "scheduled": False})
            if plat not in w["sites"]:
                w["sites"].append(plat)
            w["scheduled"] = w["scheduled"] or bool(sched)
        elif (ok_after.get((plat, name)) or "") <= at:
            attention.append({"kind": "post", "title": title, "platform": plat, "at": at,
                              "reason": (err or "")[:300], "href": f"#/posting/{name}" if ch else "#/library"})
    posted += list(went.values())
    try:
        prow = conn.execute(
            "SELECT p.post_id, p.title, p.body, pp.platform, pp.status, pp.error, pp.created_at"
            " FROM post_publications pp JOIN posts p ON p.post_id = pp.post_id"
            " WHERE pp.created_at >= ? ORDER BY pp.created_at", (start.date().isoformat(),)).fetchall()
    except sqlite3.Error:
        prow = []
    social: dict[int, dict] = {}
    for pid, ptitle, body, plat, status, err, at in prow:
        t = parse_ts(at)
        if not t or not (start < t <= end):
            continue
        title = ptitle or (body or "").strip().split("\n")[0][:60] or "a post"
        if status == "posted":
            w = social.setdefault(pid, {"title": title, "sites": [], "at": at, "scheduled": False})
            w["sites"].append(plat)
        elif status == "failed":
            attention.append({"kind": "post", "title": title, "platform": plat, "at": at,
                              "reason": (err or "")[:300], "href": "#/posts"})
    posted += list(social.values())
    try:
        from polling import session_check
        for p in session_check.summarize_problems():
            attention.append({"kind": "signin", "title": p["label"], "platform": p["code"],
                              "reason": p.get("detail") or "", "href": f"#/settings/platforms/{p['code']}"})
    except Exception:
        pass
    checks, sites = 0, 0
    for code in pm.ALL_CODES:
        table = "poll_log" if code == "ib" else f"{code}_poll_log"
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE started_at > ? AND started_at <= ?",
                             (s, e)).fetchone()[0]
        except sqlite3.Error:
            continue
        if n:
            checks += n
            sites += 1
    return {"attention": attention, "posted": posted, "checks": checks, "check_sites": sites}


def best_time(conn, zone: str | None) -> dict | None:
    try:
        from database import analytics_queries as aq
        w = aq.get_when_to_post(conn, zone_name=zone, span_days=90)
    except Exception as e:
        logger.debug("overnight: when-to-post unavailable: %s", e)
        return None
    if w.get("too_few") or not w.get("windows"):
        return None
    top = w["windows"][0]
    return {"day": top.get("day"), "h0": top.get("h0"), "h1": top.get("h1"), "zone": w.get("zone")}


# ── The sheet ─────────────────────────────────────────────────────────────

def build(conn, start: datetime, end: datetime, zone: str | None = None) -> dict:
    start = max(start, end - timedelta(days=MAX_DAYS))
    t = totals(conn, start, end, keep_changes=True)
    names = t["named_followers"]
    sites_with_posts = [c for c in pm.ALL_CODES if _has_posts(conn, c)]
    d = did(conn, start, end)
    cm = comments(conn, start, end)
    out = {
        "since": start.isoformat(), "until": end.isoformat(),
        "capped": (end - start) >= timedelta(days=MAX_DAYS),
        "totals": t["totals"], "usual": usual(conn, start, end),
        "coverage": {"covered": len([c for c in t["covered"] if c in sites_with_posts]),
                     "sites": len(sites_with_posts)},
        "pieces": top_pieces(conn, t["changes"], start, end),
        "comments": cm,
        "followers": {"named": [{"name": f["name"], "platform": f["platform"]} for f in names],
                      "counted": t["counted_followers"]},
        "did": d, "best_time": best_time(conn, zone),
    }
    tt = t["totals"]
    out["has_news"] = bool(any(tt.values()) or cm["count"] or d["attention"] or d["posted"])
    return out


def _has_posts(conn, code: str) -> bool:
    spec = pm.BY_CODE.get(code)
    try:
        return bool(conn.execute(f"SELECT 1 FROM {spec.table} LIMIT 1").fetchone())
    except (sqlite3.Error, AttributeError):
        return False


def due(last_seen: datetime | None, show_after: str, now: datetime, zone: str | None) -> bool:
    """Whether the sheet should open by itself on this visit."""
    if last_seen is None or show_after == "never":
        return False
    if show_after == "morning":
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(zone) if zone else timezone.utc
        except Exception:
            tz = timezone.utc
        b = now.astimezone(tz)
        cut = b.replace(hour=MORNING_HOUR, minute=0, second=0, microsecond=0)   # this morning, 5 am local
        return b >= cut > last_seen.astimezone(tz)
    hours = int(show_after) if show_after in SHOW_AFTER[:3] else 6
    return now - last_seen >= timedelta(hours=hours)
