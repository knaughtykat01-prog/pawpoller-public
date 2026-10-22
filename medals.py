"""Medals on the server (WORKLEVELS, spec 052 US2/US7): earned once, dated, never revoked.

``build(d)`` is a line-for-line port of ``laurels.js::_buildMedals`` with every id kept, so
nothing a browser already showed is renamed (``tests/test_medals_parity.py`` runs both on one
input until the browser copy goes). ``inputs(conn)`` gathers that input here instead of in
the page. Where the page read one site (``/api/summary`` and ``/api/aggregate`` are Inkbunny's
own routes), the server reads every site, so each figure is at least what the page saw and
no medal earned in a browser is lost in the move.

``evaluate(conn)`` inserts newly earned ``(medal_id, subject)`` rows. The first run is the
catch-up: everything earned so far goes in already announced, dated "before" today, and one
note covers it (FR-005). Work medals (research R6) use the subject ``work:<type>:<name>``.
Design: ``specs/052-progress-everywhere/research.md`` R3, R6, R7; guide §178.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import config
import progress
import xp


def _num(n) -> str:
    return f"{int(n or 0):,}"


# ── The install catalogue: laurels.js::_buildMedals, ported ────────────────

def build(d: dict, already: frozenset | set = frozenset()) -> list[dict]:
    """Every install medal, earned or not, in Laurels order. ``already`` holds ids stored as
    earned: they stay earned whatever the numbers say now (FR-004) and count toward the
    "Earn N achievements" medals."""
    m: list[dict] = []

    def ladder(group, key, icon, unit, verb, total, rungs, names=None, desc=None):
        for r in rungs:
            earned = (total or 0) >= r
            m.append({"group": group, "id": f"{key}-{r}", "icon": icon,
                      "name": (names or {}).get(r) or f"{_num(r)} {unit}",
                      "desc": desc(r) if desc else f"{verb} {_num(r)} {unit.lower()}.",
                      "earned": earned, "sub": "" if earned else f"{_num(total)}/{_num(r)}"})

    def badge(group, mid, cond, icon, name, desc, sub=""):
        m.append({"group": group, "id": mid, "icon": icon, "name": name, "desc": desc,
                  "earned": bool(cond), "sub": sub or ""})

    t = d["totals"]
    ladder("Views", "views", "👁", "Views", "Reach", t["views"],
           [100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000, 250000, 500000, 1000000],
           {1000: "1K Views", 10000: "10K Club", 100000: "100K Club", 1000000: "Millionaire"})
    ladder("Favourites", "faves", "⭐", "Favourites", "Reach", t["favorites"],
           [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000],
           {1000: "1K Favourites", 10000: "10K Favourites"})
    ladder("Comments", "comments", "💬", "Comments", "Reach", t["comments"],
           [10, 25, 50, 100, 250, 500, 1000, 2500, 5000], {1000: "1K Comments"})

    ladder("Library", "works", "📚", "Works", "Have", d["works"], [1, 3, 5, 10, 25, 50, 100, 250],
           {1: "First Work", 10: "Shelf of Ten", 100: "Century"})
    ladder("Library", "stories", "📖", "Stories", "Publish", d["stories"], [1, 5, 10, 25, 50],
           {1: "First Words"})
    ladder("Library", "art", "🎨", "Artworks", "Publish", d["artworks"], [1, 5, 10, 25, 50],
           {1: "First Canvas"})

    tp, best = d["totalPlatforms"], d["maxPlatsOnWork"]
    ladder("Reach", "breadth", "🌐", "Platforms", "Publish across", d["allPlats"],
           [1, 2, 3, 4, 5, 8, 10, 12, 16], {tp: "Full Spread"})
    badge("Reach", "crosspost-3", best >= 3, "🔗", "Cross-Poster", "Put one work on 3+ platforms.",
          "" if best >= 3 else f"best {best}/3")
    badge("Reach", "crosspost-5", best >= 5, "🕸", "Wide Load", "Put one work on 5+ platforms.",
          "" if best >= 5 else f"best {best}/5")
    badge("Reach", "crosspost-8", best >= 8, "📡", "Omnipost", "Put one work on 8+ platforms.",
          "" if best >= 8 else f"best {best}/8")
    badge("Reach", "crosspost-all", best >= tp, "🌟", "Everywhere at Once",
          f"Put one work on all {tp} platforms.", "" if best >= tp else f"best {best}/{tp}")

    ladder("Following", "watchers", "👥", "Watchers", "Gather", d["watchers"],
           [10, 25, 50, 100, 250, 500, 1000, 5000],
           {100: "Following of 100", 1000: "1K Followers", 5000: "Devoted Legion"})

    ladder("Breakouts", "breakout", "🚀", "Views", "Land a single work at", d["breakoutViews"],
           [1000, 2500, 5000, 10000, 25000, 50000, 100000],
           {1000: "Breakout", 2500: "Rising Star", 5000: "Hit", 10000: "Viral",
            25000: "Sensation", 50000: "Phenomenon", 100000: "Legendary"})

    ladder("Momentum", "streak", "🔥", "Week Streak", "Publish", d["streak"], [2, 3, 4, 6, 8, 12],
           {2: "Warming Up", 4: "On a Roll", 8: "Relentless", 12: "Unbroken Quarter"},
           desc=lambda r: f"Publish in {r} weeks running.")
    ladder("Momentum", "track", "📅", "Days Tracked", "Track", d["trackingDays"],
           [7, 30, 90, 180, 365, 730], {365: "Dedicated", 730: "Veteran"},
           desc=lambda r: f"Track your work for {_num(r)} days.")

    pv, pc = d["personaViews"], d["personaCount"]
    for mid, line, icon, name in (("persona-silver", 1000, "🥈", "Silver Persona"),
                                  ("persona-gold", 10000, "🥇", "Gold Persona"),
                                  ("persona-plat", 50000, "🏅", "Platinum Persona"),
                                  ("persona-diamond", 100000, "💎", "Diamond Persona")):
        badge("Personas", mid, pv >= line, icon, name, f"Take a persona past {_num(line)} views.",
              "" if pv >= line else f"{_num(pv)}/{_num(line)}")
    badge("Personas", "persona-two", pc >= 2, "🎭", "Two Faces", "Run 2 personas at once.",
          "" if pc >= 2 else f"{pc}/2")
    badge("Personas", "persona-three", pc >= 3, "🎬", "Full Cast", "Run 3 personas at once.",
          "" if pc >= 3 else f"{pc}/3")

    badge("Milestones", "all-rounder", d["stories"] >= 1 and d["artworks"] >= 1, "🎪",
          "All-Rounder", "Publish both a story and an artwork.")
    for x in m:
        x["earned"] = x["earned"] or x["id"] in already
    so_far = sum(1 for x in m if x["earned"])
    for n, name, icon in ((15, "Decorated", "🎖"), (30, "Distinguished", "🏵"), (50, "Illustrious", "🎗"),
                          (75, "Hall of Fame", "🏛"), (100, "Completionist", "🏆")):
        mid = f"decorated-{n}"
        badge("Milestones", mid, so_far >= n or mid in already, icon, name, f"Earn {n} achievements.",
              "" if so_far >= n else f"{so_far}/{n}")
    return m


# ── Work medals (research R6) ──────────────────────────────────────

def _work_catalogue() -> list[dict]:
    c = config.PROGRESS_TABLE["medals"]
    out = [{"id": "w_first_comment", "icon": "💬", "name": "First comment", "desc": "Someone commented on it."}]
    out += [{"id": f"w_sites_{n}", "icon": "🔗", "name": f"On {n} sites", "desc": f"Live on {n} or more sites."}
            for n in c["sites"]]
    out += [{"id": f"w_views_{'1k' if n == 1000 else '10k' if n == 10000 else n}", "icon": "👁",
             "name": f"{n:,} views", "desc": f"{n:,} views across its sites.", "_views": n} for n in c["views"]]
    out += [
        {"id": "w_breakout", "icon": "🚀", "name": "Breakout",
         "desc": f"{c['breakout_ratio']:g}× your usual in its first 2 days, on {c['breakout_min_sites']}+ sites."},
        {"id": "w_slow_burn", "icon": "🕯", "name": "Slow burn",
         "desc": f"Still growing a month on: up {round(c['slow_burn_growth'] * 100)}% between day 30 and day 60."},
        {"id": "w_found_people", "icon": "🤝", "name": "Found its people",
         "desc": "Fewer views than usual, but twice your usual share of faves and comments."},
    ]
    return out


def work_checks(w: dict) -> set[str]:
    """The cheap work medals, from the pooled totals ``progress.work_scores`` gives."""
    c = config.PROGRESS_TABLE["medals"]
    got = set()
    if (w.get("comments") or 0) >= 1:
        got.add("w_first_comment")
    got |= {f"w_sites_{n}" for n in c["sites"] if len(w.get("platforms") or ()) >= n}
    got |= {m["id"] for m in _work_catalogue() if "_views" in m and (w.get("views") or 0) >= m["_views"]}
    return got


def perf_checks(ws: dict, w: dict) -> set[str]:
    """The medals that need history (Breakout, Slow burn, Found its people), from
    ``progress.windows`` at 2, 7, 30 and 60 days."""
    c = config.PROGRESS_TABLE["medals"]
    got = set()
    two = ws.get(48)
    if two and (two["blended"] or 0) >= c["breakout_ratio"] \
            and sum(1 for r in two["sites"] if r["ratio"] is not None) >= c["breakout_min_sites"]:
        got.add("w_breakout")
    d30, d60 = ws.get(720), ws.get(1440)
    if d30 and d60:
        a = {r["code"]: r["value"] for r in d30["sites"] if r["value"] is not None}
        b = {r["code"]: r["value"] for r in d60["sites"] if r["value"] is not None}
        both = a.keys() & b.keys()
        s30, s60 = sum(a[k] for k in both), sum(b[k] for k in both)
        if s30 and s60 >= c["slow_burn_min_score"] and (s60 - s30) / s30 >= c["slow_burn_growth"]:
            got.add("w_slow_burn")
    week = ws.get(168)
    if week and week["blended"] is not None and week["blended"] < 1 \
            and week["depth"] is not None and week["usual_depth"] \
            and week["depth"] >= c["found_depth_x"] * week["usual_depth"] \
            and (w.get("faves") or 0) + (w.get("comments") or 0) >= c["found_min_response"]:
        got.add("w_found_people")
    return got


# ── Inputs, gathered on the server (research R3) ───────────────────

def _iso_week(dt: datetime) -> tuple[int, int]:
    y, w, _ = dt.isocalendar()
    return y, w


def _streak(conn) -> int:
    """Weeks running, ending this week, with at least one successful post (local time)."""
    weeks = set()
    for (at,) in conn.execute("SELECT created_at FROM posting_log WHERE status = 'success' AND action = 'post'"):
        try:
            weeks.add(_iso_week(xp._local(at)))
        except (TypeError, ValueError):
            continue
    now = datetime.now(config.display_zone())
    n = 0
    while _iso_week(now - timedelta(weeks=n)) in weeks:
        n += 1
    return n


def _tracking_days(conn) -> int:
    """Days since the first snapshot on any site. The first date never moves, so it is found
    once and kept in xp_state (a MIN over every snapshot table is a full scan)."""
    from database import platform_metrics
    since = xp._state(conn, "tracking_since")
    if not since:
        firsts = []
        for code in platform_metrics.ALL_CODES:
            try:
                v = conn.execute(f"SELECT MIN(polled_at) FROM {platform_metrics.snapshots_for(code)}").fetchone()[0]
            except sqlite3.Error:
                continue
            if v:
                firsts.append(str(v)[:10])
        if not firsts:
            return 0
        since = min(firsts)
        xp._set_state(conn, "tracking_since", since)
    first = datetime.strptime(since, "%Y-%m-%d").date()
    return (datetime.now(timezone.utc).date() - first).days + 1


def _watchers(conn) -> int:
    total = 0
    for mod, fn in (("queries", "get_watchers_count"), ("fa_queries", "get_fa_watchers_count"),
                    ("sf_queries", "get_sf_watchers_count")):
        try:
            module = __import__(f"database.{mod}", fromlist=[fn])
            total += int(getattr(module, fn)(conn) or 0)
        except Exception:
            continue
    return total


def _best_single_views(conn) -> int:
    from database import platform_metrics
    best = 0
    for code in platform_metrics.VIEW_PLATFORMS:
        spec = platform_metrics.get(code)
        try:
            v = conn.execute(f"SELECT MAX({spec.views}) FROM {spec.table}").fetchone()[0]
        except sqlite3.Error:
            continue
        best = max(best, int(v or 0))
    return best


def inputs(conn: sqlite3.Connection, scores: dict) -> dict:
    """The page's ``_buildMedals`` input, from the server's own tables. ``scores`` is
    ``progress.work_scores`` for the catalogue."""
    from database import accounts as accounts_db
    from database import personas as personas_db
    totals = {"views": 0, "favorites": 0, "comments": 0, "submissions": 0}
    for a in accounts_db.list_accounts(conn):
        st = accounts_db.account_stats(conn, a["account_id"], a["platform"]) or {}
        for k in totals:
            totals[k] += st.get(k) or 0
    persona_views = [personas_db.persona_stats(conn, p["persona_id"])["combined"]["views"] or 0
                     for p in personas_db.list_personas(conn)]
    plats = [w.get("platforms") or set() for w in scores.values()]
    return {
        "totals": totals,
        "works": len(scores),
        "stories": sum(1 for k in scores if k[0] == "story"),
        "artworks": sum(1 for k in scores if k[0] == "artwork"),
        "allPlats": len(set().union(*plats)) if plats else 0,
        "maxPlatsOnWork": max((len(p) for p in plats), default=0),
        "totalPlatforms": len(accounts_db.PLATFORMS),
        "watchers": _watchers(conn),
        "breakoutViews": max([_best_single_views(conn)] + [w["views"] for w in scores.values()]),
        "streak": _streak(conn),
        "trackingDays": _tracking_days(conn),
        "personaViews": max(persona_views, default=0),
        "personaCount": len(persona_views),
    }


# ── Earning, catch-up and the announce queue (FR-004, FR-005, research R7) ───

def _stored(conn, subject: str | None = None) -> dict:
    sql, args = "SELECT medal_id, subject, earned_at, dated_exactly, source, announced_at FROM earned_medals", ()
    if subject:
        sql, args = sql + " WHERE subject = ?", (subject,)
    return {(r[0], r[1]): {"earned_at": r[2], "dated_exactly": bool(r[3]), "source": r[4], "announced_at": r[5]}
            for r in conn.execute(sql, args)}


def evaluate(conn: sqlite3.Connection, synced: dict) -> int:
    """Insert every newly earned medal. ``synced`` is what ``progress.sync`` returned.
    Install medals and the cheap work medals are checked every time; the history-based work
    medals only for works whose score just moved. Returns rows inserted. Does not commit."""
    now = xp._now_utc()
    stored = _stored(conn)
    have_install = {mid for (mid, subj) in stored if subj == "install"}
    earned = {(m["id"], "install") for m in build(inputs(conn, synced["scores"]), have_install) if m["earned"]}
    for key, w in synced["scores"].items():
        subj = f"work:{key[0]}:{key[1]}"
        earned |= {(mid, subj) for mid in work_checks(w)}
        if key in synced["moved"]:
            earned |= {(mid, subj) for mid in perf_checks(progress.windows(conn, key, (48, 168, 720, 1440)), w)}
    return _insert(conn, earned - stored.keys(), now)


def evaluate_work(conn: sqlite3.Connection, key: tuple, w: dict, ws: dict) -> int:
    """The work page's own check, with the windows it has just worked out."""
    subj = f"work:{key[0]}:{key[1]}"
    earned = {(mid, subj) for mid in work_checks(w) | perf_checks(ws, w)}
    return _insert(conn, earned - _stored(conn, subj).keys(), xp._now_utc())


def _insert(conn, new: set, now: str) -> int:
    if not new:
        return 0
    catchup = not xp._state(conn, "medals_caught_up_at")
    conn.executemany(
        "INSERT OR IGNORE INTO earned_medals (medal_id, subject, earned_at, dated_exactly, source, announced_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        # ponytail: catch-up rows are all dated "before" today; dating single-series medals from
        # their first snapshot crossing (research R3) can come later as a one-off pass.
        [(mid, subj, now, 0 if catchup else 1, "catchup" if catchup else "live", now if catchup else None)
         for mid, subj in sorted(new)])
    if catchup:
        xp._set_state(conn, "medals_caught_up_at", now)
        xp._set_state(conn, "medals_catchup_pending", str(sum(1 for _, s in new if s == "install")))
    return len(new)


def _names() -> dict:
    zero = {"totals": {"views": 0, "favorites": 0, "comments": 0}, "works": 0, "stories": 0, "artworks": 0,
            "allPlats": 0, "maxPlatsOnWork": 0, "totalPlatforms": 28, "watchers": 0, "breakoutViews": 0,
            "streak": 0, "trackingDays": 0, "personaViews": 0, "personaCount": 0}
    from database import accounts as accounts_db
    zero["totalPlatforms"] = len(accounts_db.PLATFORMS)
    out = {m["id"]: m for m in build(zero)}
    out.update({m["id"]: m for m in _work_catalogue()})
    return out


def _row(mid: str, row: dict, names: dict) -> dict:
    m = names.get(mid, {"name": mid, "icon": "🏅", "desc": ""})
    return {"id": mid, "name": m["name"], "icon": m["icon"], "desc": m["desc"],
            "earned_at": row["earned_at"][:10], "dated_exactly": row["dated_exactly"]}


def for_subject(conn: sqlite3.Connection, subject: str) -> list[dict]:
    """Earned medals of one subject (a work page), newest first."""
    names = _names()
    rows = sorted(_stored(conn, subject).items(), key=lambda kv: kv[1]["earned_at"], reverse=True)
    return [_row(mid, r, names) for (mid, _), r in rows]


def install_list(conn: sqlite3.Connection, scores: dict) -> list[dict]:
    """Every install medal for Laurels, earned or not, with its stored date (contract)."""
    stored = {mid: r for (mid, subj), r in _stored(conn).items() if subj == "install"}
    out = []
    for m in build(inputs(conn, scores), set(stored)):
        r = stored.get(m["id"])
        out.append({**m, "earned_at": r["earned_at"][:10] if r else None,
                    "dated_exactly": r["dated_exactly"] if r else True})
    return out


def pending(conn: sqlite3.Connection, titles: dict) -> dict:
    """The announce queue: medals not yet announced, works above their announced level, the
    one-off catch-up count and back-pay. ``titles`` maps work keys to canonical titles."""
    from database import masterpiece_queries
    names = _names()
    junk = {n for n, s in masterpiece_queries.statuses(conn).items() if s == "junk"}
    hidden = {f"work:artwork:{n}" for n in junk}
    medal_rows = [{"id": mid, "subject": subj, "name": names.get(mid, {}).get("name", mid),
                   "icon": names.get(mid, {}).get("icon", "🏅"),
                   "work": titles.get(tuple(subj.split(":", 2)[1:])) if subj.startswith("work:") else None}
                  for (mid, subj), r in _stored(conn).items()
                  if r["announced_at"] is None and subj not in hidden]
    per = config.PROGRESS_TABLE["audience_xp_per_level"]
    works = [{"type": c, "name": n, "title": titles.get((c, n), n), "from": a, "to": p,
              "xp": sum(per * L for L in range(a + 1, p + 1))}
             for c, n, a, p in conn.execute(
                 "SELECT content_type, work_name, announced_level, peak_level FROM work_levels"
                 " WHERE peak_level > announced_level")
             if (c, n) in titles and not (c == "artwork" and n in junk)]
    return {"medals": medal_rows, "works": works,
            "catchup_count": int(xp._state(conn, "medals_catchup_pending") or 0),
            "backpay_xp": int(xp._state(conn, "audience_backpay_pending") or 0)}


def announce(conn: sqlite3.Connection, body: dict) -> None:
    """Mark what the browser just showed (idempotent). Does not commit."""
    now = xp._now_utc()
    conn.executemany("UPDATE earned_medals SET announced_at = ? WHERE medal_id = ? AND subject = ?"
                     " AND announced_at IS NULL",
                     [(now, str(m.get("id")), str(m.get("subject"))) for m in body.get("medals") or []])
    conn.executemany("UPDATE work_levels SET announced_level = MAX(announced_level, ?)"
                     " WHERE content_type = ? AND work_name = ?",
                     [(int(w.get("level") or 0), str(w.get("type")), str(w.get("name")))
                      for w in body.get("works") or []])
    if body.get("catchup"):
        conn.execute("DELETE FROM xp_state WHERE key IN ('medals_catchup_pending', 'audience_backpay_pending')")
