"""Analytics queries: Top Fans, Trending/Spike Detection, Cross-Platform Linking.

This module provides advanced analytics that span across platforms, unlike
the platform-specific query modules (queries.py, fa_queries.py, ws_queries.py)
which are scoped to a single platform each.

Major features:
  - Top Fans leaderboard: weighted scoring of user engagement across platforms
  - Trending/Spike Detection: z-score analysis to find unusual activity
  - Cross-Platform Linking: 1:1 mappings of the same content posted to
    multiple platforms, with combined stats and time-series
  - Auto-Suggest Links: Jaccard title similarity to suggest likely matches
"""

from __future__ import annotations
import logging
import math
import sqlite3
from typing import Any

from database import platform_metrics

logger = logging.getLogger(__name__)


# ── Top Fans Leaderboard ─────────────────────────────────────

def get_top_fans(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Aggregate fave and comment activity per user across platforms.

    Data sources by platform:
      IB: faving_users table (individual fave tracking) + comments table
      FA: fa_comments table (comment tracking only -- no individual fave data)
      WS: Not included (Weasyl exposes no user-level activity data at all)

    Scoring formula: score = (fave_count * 2) + (comment_count * 1)
    Faves are weighted 2x because they represent a deliberate action of
    appreciation, whereas comments are weighted 1x. This means a user who
    faved 5 submissions (score: 10) ranks higher than a user who left 9
    comments (score: 9).

    Platform aggregation uses a set() for the platforms field because a user
    can appear on multiple platforms with the same username. Using a set
    naturally deduplicates platform entries (e.g. if a user is found in both
    IB faving_users and IB comments, "ib" only appears once).

    Each platform query is wrapped in try/except to gracefully handle cases
    where the table does not exist (e.g. fresh database without FA data).
    """
    # Accumulator dict: keyed by username, value is aggregated stats + platform set.
    user_stats: dict[str, dict] = {}  # username -> {fave_count, comment_count, platforms}

    # IB faving users -- COUNT(DISTINCT submission_id) gives unique submissions
    # faved per user (a user faving the same submission twice is impossible due
    # to the UNIQUE constraint, but DISTINCT is defensive).
    try:
        rows = conn.execute(
            "SELECT username, COUNT(DISTINCT submission_id) as fave_count FROM faving_users GROUP BY username"
        ).fetchall()
        for r in rows:
            name = r["username"]
            if name not in user_stats:
                user_stats[name] = {"fave_count": 0, "comment_count": 0, "platforms": set()}
            user_stats[name]["fave_count"] += r["fave_count"]
            user_stats[name]["platforms"].add("ib")
    except Exception:
        pass

    # IB comments -- COUNT(*) gives total comments per user across all submissions.
    # is_own = 0 excludes our own comments (2.192.0): before that filter the
    # posting account ranked as its own top fan.
    try:
        rows = conn.execute(
            "SELECT username, COUNT(*) as comment_count FROM comments"
            " WHERE COALESCE(is_own, 0) = 0 GROUP BY username"
        ).fetchall()
        for r in rows:
            name = r["username"]
            if name not in user_stats:
                user_stats[name] = {"fave_count": 0, "comment_count": 0, "platforms": set()}
            user_stats[name]["comment_count"] += r["comment_count"]
            user_stats[name]["platforms"].add("ib")
    except Exception:
        pass

    # FA comments -- FA does not provide individual fave user data, so only
    # comment activity contributes to FA users' scores.
    try:
        rows = conn.execute(
            "SELECT username, COUNT(*) as comment_count FROM fa_comments"
            " WHERE COALESCE(is_own, 0) = 0 GROUP BY username"
        ).fetchall()
        for r in rows:
            name = r["username"]
            if name not in user_stats:
                user_stats[name] = {"fave_count": 0, "comment_count": 0, "platforms": set()}
            user_stats[name]["comment_count"] += r["comment_count"]
            user_stats[name]["platforms"].add("fa")
    except Exception:
        pass

    # WS is intentionally excluded -- Weasyl does not expose any user-level
    # engagement data (no faving users, no individual comments).

    # Calculate weighted scores and sort descending by score.
    result = []
    for username, stats in user_stats.items():
        # Weighted formula: faves are worth 2 points each, comments 1 point.
        score = stats["fave_count"] * 2 + stats["comment_count"]
        result.append({
            "username": username,
            "platforms": sorted(stats["platforms"]),  # Convert set to sorted list for JSON serialization.
            "fave_count": stats["fave_count"],
            "comment_count": stats["comment_count"],
            "score": score,
        })

    result.sort(key=lambda x: x["score"], reverse=True)
    return result[:limit]


# ── Trending / Spike Detection ───────────────────────────────
# Uses z-score statistical analysis to detect unusual activity spikes.
# A "spike" is when the most recent change in a metric (views, faves,
# or comments) is significantly larger than the typical change over the
# past 30 days. This surfaces content that is suddenly getting more
# attention than its historical baseline.

def get_trending_submissions(conn: sqlite3.Connection, hours: int = 24, z_threshold: float = 2.0) -> list[dict]:
    """Find submissions with unusual activity based on z-score analysis.

    Algorithm overview:
    1. For each submission on each platform, get the delta between the
       two most recent snapshots (the "current delta").
    2. Build a 30-day baseline of consecutive-snapshot deltas.
    3. Compute the mean and standard deviation of the baseline deltas.
    4. Calculate z-score: z = (current_delta - mean) / stddev
    5. If z >= z_threshold (default 2.0), the submission is "spiking".

    A z-score of 2.0 means the current activity is 2 standard deviations
    above the 30-day average -- roughly the top 2.3% of expected variation.

    Results are sorted by max_z (highest spike first) across all platforms.
    """
    trending = []

    # Table names come from the canonical registry rather than a literal list.
    # `_find_spikes` already read its metric COLUMNS from there, so the list was
    # the last hand-written half — and it stopped at e621, meaning FurryNetwork,
    # Furbooru and Telegram could never appear in Trending however hard they
    # spiked. Each platform is wrapped in try/except so a table that does not
    # exist yet is skipped rather than killing the whole sweep.
    for platform in platform_metrics.ALL_CODES:
        sub_table = platform_metrics.table_for(platform)
        snap_table = platform_metrics.snapshots_for(platform)
        try:
            _find_spikes(conn, platform, sub_table, snap_table, hours, z_threshold, trending)
        except Exception:
            pass

    # Sort all results across platforms by maximum z-score, highest first.
    trending.sort(key=lambda x: x.get("max_z", 0), reverse=True)
    return trending


def _find_spikes(conn: sqlite3.Connection, platform: str, sub_table: str, snap_table: str,
                 hours: int, z_threshold: float, results: list[dict]) -> None:
    """Find spike submissions for a single platform.

    Step-by-step z-score spike detection per submission:

    1. CURRENT DELTA: Fetch the 2 most recent snapshots. The difference
       between them is the "current delta" -- how much the metric changed
       in the most recent poll interval.

    2. BASELINE WINDOW (30 days): Fetch all snapshots from the last 30 days,
       ordered chronologically. Compute consecutive deltas (snap[i] - snap[i-1])
       to build a list of historical changes. This is the baseline distribution.
       Requires at least 3 snapshots (yielding at least 2 deltas) to compute
       meaningful statistics.

    3. STATISTICS: Calculate the sample mean and sample standard deviation
       (using Bessel's correction: N-1 denominator) of the baseline deltas.

    4. Z-SCORE: z = (current_delta - mean) / stddev. If stddev is 0 (all
       baseline deltas are identical), we skip -- can't compute a meaningful
       z-score.

    5. THRESHOLD: If z >= z_threshold, this metric is spiking. Record the
       delta, z-score, mean, and stddev for reporting.

    Results are appended to the shared `results` list (mutated in place).
    Uses platform-aware column names for Wattpad (reads/votes) and Itaku (likes, no views).
    """
    # Which metric columns this platform stores — from the canonical registry
    # (database/platform_metrics.py), so a newly wired platform gets spike
    # detection without editing this function.
    metric_cols = (platform_metrics.columns_for(platform)
                   or ["views", "favorites_count", "comments_count"])

    # Get all submission IDs and titles from this platform.
    subs = conn.execute(f"SELECT submission_id, title FROM {sub_table}").fetchall()

    for sub_row in subs:
        sub_id = sub_row["submission_id"]
        title = sub_row["title"]

        # Step 1: Get the 2 most recent snapshots to compute the current delta.
        cols_str = ", ".join(metric_cols)
        latest = conn.execute(
            f"SELECT {cols_str}, polled_at FROM {snap_table} "
            f"WHERE submission_id = ? ORDER BY polled_at DESC LIMIT 2",
            (sub_id,),
        ).fetchall()

        if len(latest) < 2:
            # Need at least 2 snapshots to compute a delta.
            continue

        current = dict(latest[0])   # Most recent snapshot
        previous = dict(latest[1])  # Second most recent snapshot

        # Step 2: Get all snapshots from the last 30 days for the baseline.
        # The 30-day window provides enough data points for reliable statistics
        # while being recent enough to reflect current activity patterns.
        baseline_snaps = conn.execute(
            f"SELECT {cols_str} FROM {snap_table} "
            f"WHERE submission_id = ? AND polled_at >= datetime('now', '-30 days') "
            f"ORDER BY polled_at ASC",
            (sub_id,),
        ).fetchall()

        if len(baseline_snaps) < 3:
            # Need at least 3 snapshots to compute 2+ baseline deltas.
            continue

        # Step 3-5: Compute z-scores for each metric independently.
        spike_info = {}
        max_z = 0

        for metric in metric_cols:
            # Current delta: change in this metric between the two most recent snapshots.
            current_delta = current[metric] - previous[metric]
            if current_delta <= 0:
                # No increase -- not a spike. Skip this metric.
                continue

            # Compute consecutive deltas from the 30-day baseline snapshots.
            # Each delta represents the change between two adjacent poll cycles.
            deltas = []
            for i in range(1, len(baseline_snaps)):
                d = baseline_snaps[i][metric] - baseline_snaps[i - 1][metric]
                deltas.append(d)

            if len(deltas) < 2:
                # Need at least 2 deltas for meaningful standard deviation.
                continue

            # Sample mean of baseline deltas.
            mean = sum(deltas) / len(deltas)
            # Sample variance using Bessel's correction (N-1 denominator)
            # for unbiased estimation from a sample.
            variance = sum((d - mean) ** 2 for d in deltas) / (len(deltas) - 1)
            stddev = math.sqrt(variance) if variance > 0 else 0

            if stddev == 0:
                # All baseline deltas are identical -- z-score is undefined.
                continue

            # Z-score: how many standard deviations the current delta is
            # above the baseline mean.
            z = (current_delta - mean) / stddev
            if z >= z_threshold:
                spike_info[metric] = {
                    "delta": current_delta,
                    "z_score": round(z, 2),
                    "mean": round(mean, 2),
                    "stddev": round(stddev, 2),
                }
                max_z = max(max_z, z)

        if spike_info:
            results.append({
                "platform": platform,
                "submission_id": sub_id,
                "title": title,
                "spikes": spike_info,
                "max_z": round(max_z, 2),
            })


# ── Cross-Platform Linking ───────────────────────────────────
# Links represent 1:1 mappings of the SAME content posted to multiple platforms
# (e.g. the same story posted to IB, FA, and WS). Unlike groups (which are
# arbitrary user-defined collections), links are specifically for tracking
# cross-posted content and computing combined performance metrics.
#
# The data model uses link_id as a grouping key:
# - submission_links: Auto-increment link_id with a created_at timestamp.
# - submission_link_members: Junction table (link_id, platform, submission_id)
#   mapping each link to its constituent submissions across platforms.
#
# Combined stats and time-series are computed dynamically by querying each
# member's platform-specific tables, same dynamic table lookup pattern as
# group_queries.py.

def create_link(conn: sqlite3.Connection, members: list[dict]) -> int:
    """Create a submission link with members. Each member: {platform, submission_id}.

    The link_id is auto-generated (INSERT DEFAULT VALUES creates a row with
    only the auto-increment primary key and default created_at). Members are
    then inserted into the junction table referencing this link_id.
    """
    cur = conn.execute("INSERT INTO submission_links DEFAULT VALUES")
    link_id = cur.lastrowid
    for m in members:
        conn.execute(
            "INSERT INTO submission_link_members (link_id, platform, submission_id) VALUES (?, ?, ?)",
            (link_id, m["platform"], m["submission_id"]),
        )
    conn.commit()
    return link_id


def delete_link(conn: sqlite3.Connection, link_id: int) -> None:
    # Cascade deletes junction table entries via ON DELETE CASCADE in schema.
    conn.execute("DELETE FROM submission_links WHERE link_id = ?", (link_id,))
    conn.commit()


def get_links(conn: sqlite3.Connection) -> list[dict]:
    """Get all links with their member details eagerly loaded.

    For each link, fetches its members from the junction table and enriches
    each member with title and current stats from the platform-specific
    submissions table. Uses the same dynamic table lookup pattern as
    group_queries.get_group_stats.
    """
    links = conn.execute("SELECT * FROM submission_links ORDER BY created_at DESC").fetchall()
    result = []
    for link in links:
        l = dict(link)
        members = conn.execute(
            "SELECT * FROM submission_link_members WHERE link_id = ?", (l["link_id"],)
        ).fetchall()
        l["members"] = []
        for m in members:
            md = dict(m)
            # Enrich each member with title and stats from the platform's table.
            table = {"ib": "submissions", "fa": "fa_submissions", "ws": "ws_submissions", "sf": "sf_submissions", "sqw": "sqw_submissions", "ao3": "ao3_submissions", "da": "da_submissions", "wp": "wp_submissions", "ik": "ik_submissions"}.get(md["platform"])
            if table:
                try:
                    sub = conn.execute(
                        f"SELECT * FROM {table} WHERE submission_id = ?",
                        (md["submission_id"],),
                    ).fetchone()
                    if sub:
                        md.update(dict(sub))
                except Exception:
                    pass
            l["members"].append(md)
        result.append(l)
    return result


def get_link_combined_stats(conn: sqlite3.Connection, link_id: int) -> dict:
    """Get aggregate stats for a linked set of submissions.

    Sums views, faves, and comments across all linked platform submissions
    to show the total reach of cross-posted content. Same dynamic table
    lookup pattern as group_queries.get_group_stats.
    """
    members = conn.execute(
        "SELECT platform, submission_id FROM submission_link_members WHERE link_id = ?",
        (link_id,),
    ).fetchall()

    total_views = 0
    total_score = 0
    total_faves = 0
    total_comments = 0
    subs = []

    # Table + metric columns come from the canonical registry
    # (database/platform_metrics.py). The local copy this replaces knew nothing
    # of FurryNetwork/Furbooru, and mapped e621's `score` into the VIEWS slot —
    # so a linked set containing a booru post counted net upvotes as page views
    # (and a downvoted post could take the total DOWN).
    for m in members:
        plat = m["platform"]
        spec = platform_metrics.get(plat)
        if not spec:
            continue
        try:
            row = conn.execute(
                f"SELECT * FROM {spec.table} WHERE submission_id = ?",
                (m["submission_id"],),
            ).fetchone()
        except sqlite3.Error as e:
            logger.warning("link stats: %s unavailable (%s): %s", plat, spec.table, e)
            continue
        if row:
            r = dict(row)
            r["platform"] = plat
            total_views += (r.get(spec.views, 0) or 0) if spec.views else 0
            total_score += (r.get(spec.score, 0) or 0) if spec.score else 0
            total_faves += (r.get(spec.faves, 0) or 0) if spec.faves else 0
            total_comments += (r.get(spec.comments, 0) or 0) if spec.comments else 0
            subs.append(r)

    return {
        "total_views": total_views,
        "total_score": total_score,
        "total_favorites": total_faves,
        "total_comments": total_comments,
        "submissions": subs,
    }


def get_combined_snapshots(conn: sqlite3.Connection, pairs) -> list[dict]:
    """Merged time-series (sum views/faves/comments at each timestamp) for a set
    of `(platform, submission_id)` pairs.

    This is the reusable core: a Cross-Platform link and a Collection both boil
    down to "a set of platform submissions that are the same piece", so both
    chart their combined growth through here. Merges snapshots across platforms
    by exact `polled_at` timestamp string — platforms polled at the same instant
    sum, non-overlapping timestamps carry only whoever had a snapshot then.

    `pairs` is any iterable of `(platform, submission_id)` tuples.
    """
    # Accumulate snapshots across platforms, indexed by timestamp string.
    # Each timestamp entry sums values from all linked submissions that
    # have a snapshot at that exact time.
    time_data: dict[str, dict] = {}

    # Snapshot table + metric columns come from the canonical registry
    # (database/platform_metrics.py) — this used to keep its own copy, which
    # knew nothing of FurryNetwork/Furbooru and charted e621's net `score` as
    # if it were views.
    for plat, sid in pairs:
        spec = platform_metrics.get(plat)
        if not spec:
            continue
        snap_table = spec.snapshots
        v_col, f_col, c_col = spec.views, spec.faves, spec.comments
        # Build SELECT with only the columns this platform actually has.
        select_cols = ["polled_at"] + [c for c in (v_col, spec.score, f_col, c_col) if c]
        try:
            rows = conn.execute(
                f"SELECT {', '.join(select_cols)} FROM {snap_table} "
                f"WHERE submission_id = ? ORDER BY polled_at ASC",
                (sid,),
            ).fetchall()
        except sqlite3.Error as e:
            logger.warning("combined snapshots: %s unavailable (%s): %s", plat, snap_table, e)
            continue
        for r in rows:
            ts = r["polled_at"]
            if ts not in time_data:
                time_data[ts] = {"polled_at": ts, "views": 0, "favorites_count": 0,
                                 "comments_count": 0, "score": 0}
            # Sum values from multiple platforms at the same timestamp.
            # Map platform-specific columns to the canonical output keys, and
            # keep the score family in its own series.
            time_data[ts]["views"] += (r[v_col] or 0) if v_col else 0
            time_data[ts]["score"] += (r[spec.score] or 0) if spec.score else 0
            time_data[ts]["favorites_count"] += (r[f_col] or 0) if f_col else 0
            time_data[ts]["comments_count"] += (r[c_col] or 0) if c_col else 0

    # Return sorted by timestamp for chronological chart rendering.
    return sorted(time_data.values(), key=lambda x: x["polled_at"])


def get_link_combined_snapshots(conn: sqlite3.Connection, link_id: int) -> list[dict]:
    """Combined time-series for a Cross-Platform link — thin wrapper over
    get_combined_snapshots after resolving the link's members to pairs."""
    members = conn.execute(
        "SELECT platform, submission_id FROM submission_link_members WHERE link_id = ?",
        (link_id,),
    ).fetchall()
    return get_combined_snapshots(conn, [(m["platform"], m["submission_id"]) for m in members])


def auto_suggest_links(conn: sqlite3.Connection) -> list[dict]:
    """Cross-platform link suggestions by title similarity, excluding pairs the
    user has already linked. Thin wrapper over the shared `_auto_suggest` engine.
    """
    existing = set()
    try:
        for m in conn.execute("SELECT platform, submission_id FROM submission_link_members").fetchall():
            existing.add((m["platform"], str(m["submission_id"])))
    except Exception:
        pass
    return _auto_suggest(conn, existing)


def _auto_suggest(conn: sqlite3.Connection, existing: set) -> list[dict]:
    """Find potential cross-platform matches by title similarity.

    Shared engine behind both Cross-Platform link suggestions and Collection
    suggestions — the only difference between them is *what's already grouped*,
    passed in as `existing` (a set of `(platform, str(submission_id))` pairs to
    exclude).

    Algorithm:
    1. Load all submissions from all nine platforms.
    2. `existing` marks pairs already grouped (linked or collected), excluded
       from suggestions so we never re-propose what the user already merged.
    3. Compare every pair of submissions across different platforms (not within
       the same platform -- cross-posting to the same site is not meaningful).
       Uses nested loops over platform pairs (IB-FA, IB-WS, FA-WS).
    4. For each cross-platform pair, compute title similarity using the Jaccard
       index on word sets. If the similarity meets the 0.6 threshold (60% word
       overlap), it is considered a likely match.
    5. Results are sorted by similarity score (highest first) and capped at 20.

    The 0.6 Jaccard threshold was chosen as a balance: high enough to avoid
    false positives from generic titles, low enough to catch titles that differ
    slightly across platforms (e.g. minor wording changes, added chapter numbers).

    Note: This is an O(N*M) comparison across platforms, which is acceptable
    for the expected submission counts (hundreds, not millions).
    """
    suggestions = []

    # Step 1: Load all submissions from each platform.
    # IB uses create_datetime; most others use posted_at for the post date column.
    date_col = {
        "ib": "create_datetime", "fa": "posted_at", "ws": "posted_at",
        "sf": "posted_at", "sqw": "posted_at", "ao3": "posted_at",
        "da": "posted_at", "wp": "posted_at", "ik": "posted_at",
    }
    platforms = {}
    for platform, table in [
        ("ib", "submissions"), ("fa", "fa_submissions"), ("ws", "ws_submissions"),
        ("sf", "sf_submissions"), ("sqw", "sqw_submissions"), ("ao3", "ao3_submissions"),
        ("da", "da_submissions"), ("wp", "wp_submissions"), ("ik", "ik_submissions"),
    ]:
        try:
            rows = conn.execute(
                f"SELECT submission_id, title, {date_col[platform]} as posted_at FROM {table}"
            ).fetchall()
            platforms[platform] = [dict(r) for r in rows]
        except Exception:
            platforms[platform] = []

    # Step 2: exclusion set (`existing`) is provided by the caller — links pass
    # their linked pairs, collections pass their collected pairs. Keys compared
    # as (platform, str(submission_id)) so int/str id storage never mismatches.

    # Step 3-4: Compare across platforms (not within the same platform).
    # Iterates over all unique platform pairs: (ib, fa), (ib, ws), (fa, ws).
    platform_keys = list(platforms.keys())
    for i in range(len(platform_keys)):
        for j in range(i + 1, len(platform_keys)):
            p1, p2 = platform_keys[i], platform_keys[j]
            for s1 in platforms[p1]:
                # Skip submissions already grouped on this platform.
                if (p1, str(s1["submission_id"])) in existing:
                    continue
                for s2 in platforms[p2]:
                    # Skip submissions already grouped on the other platform.
                    if (p2, str(s2["submission_id"])) in existing:
                        continue
                    similarity = _title_similarity(s1["title"], s2["title"])
                    # Jaccard threshold of 0.6: at least 60% word overlap
                    # required to consider titles a potential match.
                    if similarity >= 0.6:
                        suggestions.append({
                            "similarity": round(similarity, 2),
                            "submissions": [
                                {"platform": p1, "submission_id": s1["submission_id"], "title": s1["title"]},
                                {"platform": p2, "submission_id": s2["submission_id"], "title": s2["title"]},
                            ],
                        })

    # Step 5: Sort by similarity (best matches first) and cap at 20 results.
    suggestions.sort(key=lambda x: x["similarity"], reverse=True)
    return suggestions[:20]


def _title_similarity(a: str, b: str) -> float:
    """Compute title similarity using the Jaccard index on word sets.

    Jaccard index = |intersection| / |union| of the two word sets.
    - Returns 1.0 for identical titles (after lowercasing).
    - Returns 0.0 for titles with no words in common.
    - Returns 0.0 if either title is empty.

    Example: "The Quick Fox" vs "The Quick Brown Fox"
      words_a = {"the", "quick", "fox"}
      words_b = {"the", "quick", "brown", "fox"}
      intersection = {"the", "quick", "fox"} (3 words)
      union = {"the", "quick", "brown", "fox"} (4 words)
      Jaccard = 3/4 = 0.75
    """
    if not a or not b:
        return 0.0
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


# ── Posting insights: benchmarks + best-time (gap-wave-3 §2+3) ───────────────
# Module-level maps (the route-local/function-local copies above predate these;
# new analytics code should use THESE — consolidation seed, see gap_wave3.md).
INSIGHT_TABLES = {
    "ib": "submissions", "fa": "fa_submissions", "ws": "ws_submissions",
    "sf": "sf_submissions", "sqw": "sqw_submissions", "ao3": "ao3_submissions",
    "da": "da_submissions", "wp": "wp_submissions", "ik": "ik_submissions",
    "bsky": "bsky_submissions", "tw": "tw_submissions", "mast": "mast_submissions",
    "tum": "tum_submissions", "pix": "pix_submissions", "thr": "thr_submissions",
    "ig": "ig_submissions", "e621": "e621_submissions", "fn": "fn_submissions", "sc": "sc_submissions",
    "ng": "ng_submissions", "yt": "yt_submissions",
    "fbr": "fbr_submissions",
}
INSIGHT_PRIMARY = {   # the platform's headline engagement column
    "ib": "views", "fa": "views", "ws": "views", "sf": "views", "sqw": "views",
    "ao3": "views", "da": "views", "wp": "reads", "ik": "likes", "bsky": "likes",
    "tw": "views", "mast": "likes", "tum": "notes", "pix": "views",
    "thr": "views", "ig": "views", "e621": "score", "fn": "views", "fbr": "score", "sc": "views",
    "ng": "views", "yt": "views",
}
INSIGHT_DATE_COL = {"ib": "create_datetime"}   # everything else: posted_at


def _parse_posted(raw):
    """Best-effort posted-at parse → (datetime|None, has_time). ISO first, then
    the scraped formats (FA's human-readable string); date-only rows (SQW/AO3)
    parse but carry has_time=False → weekday histogram only. Unparseable rows
    are dropped, never guessed."""
    from datetime import datetime
    if not raw:
        return None, False
    s = str(raw).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")), len(s) > 10
    except ValueError:
        pass
    for fmt, has_time in (("%Y-%m-%d %H:%M:%S", True),
                          # FA scrapes a human-readable date WITH seconds, full or
                          # abbreviated month, sometimes a comma before the time —
                          # e.g. "August 11, 2019 07:17:50 PM". Without these the
                          # whole platform silently drops out of every date-based
                          # analytic (this is what left FA off the repost radar).
                          ("%B %d, %Y %I:%M:%S %p", True), ("%b %d, %Y %I:%M:%S %p", True),
                          ("%B %d, %Y, %I:%M:%S %p", True), ("%b %d, %Y, %I:%M:%S %p", True),
                          ("%b %d, %Y %I:%M %p", True),
                          ("%B %d, %Y %I:%M %p", True), ("%b %d, %Y, %I:%M %p", True),
                          ("%d %b %Y %H:%M", True), ("%Y-%m-%d", False)):
        try:
            return datetime.strptime(s, fmt), has_time
        except ValueError:
            continue
    return None, False


def get_posting_insights(conn: sqlite3.Connection, tz_offset_minutes: int = 0,
                         zone_name: str | None = None) -> dict:
    """Benchmarks + best-time in one pass over the 17 submission tables.

    - platforms: per-platform {metric, count, median} of the headline metric.
    - overperformers: pieces ≥1.5× their OWN platform's median (within-platform
      ratios only — views and likes are never compared to each other).
    - weekday/hour: histograms of RELATIVE engagement (each post ÷ its
      platform's median → cross-platform comparable; 1.0 = typical), bucketed
      in the operator's zone (zone_name, DST-correct; tz_offset_minutes only when
      no zone loads — 4.50.0). Each bucket carries its sample count so the UI can
      grey out thin evidence.
    """
    import statistics
    from datetime import timezone

    zone, _ = _when_zone(zone_name, tz_offset_minutes)

    per_platform: dict = {}
    weekday = [[] for _ in range(7)]     # Mon..Sun
    hour = [[] for _ in range(24)]
    overperformers: list[dict] = []

    for code, table in INSIGHT_TABLES.items():
        primary = INSIGHT_PRIMARY[code]
        date_col = INSIGHT_DATE_COL.get(code, "posted_at")
        try:
            rows = conn.execute(
                f"SELECT title, {primary} AS m, {date_col} AS d FROM {table}"
            ).fetchall()
        except sqlite3.OperationalError:
            continue   # table/column missing on this install — skip platform
        vals = [(r["m"] or 0) for r in rows]
        if not vals:
            continue
        med = statistics.median(vals)
        per_platform[code] = {"metric": primary, "count": len(vals),
                              "median": round(med, 1)}
        if med <= 0:
            continue
        for r in rows:
            m = r["m"] or 0
            ratio = m / med
            if len(vals) >= 5 and ratio >= 1.5:
                overperformers.append({
                    "platform": code, "title": r["title"] or "(untitled)",
                    "value": m, "metric": primary, "ratio": round(ratio, 1)})
            dt, has_time = _parse_posted(r["d"])
            if not dt:
                continue
            # An offset-carrying value is converted, not read by its own clock (bug 9).
            local = ((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(zone)
                     if has_time else dt)
            weekday[local.weekday()].append(ratio)
            if has_time:
                hour[local.hour].append(ratio)

    overperformers.sort(key=lambda x: -x["ratio"])

    def _buckets(groups):
        return [{"median": round(statistics.median(g), 2) if g else 0,
                 "count": len(g)} for g in groups]

    return {"platforms": per_platform, "overperformers": overperformers[:10],
            "weekday": _buckets(weekday), "hour": _buckets(hour)}


# ── When to post (4.48.0, spec 015) ──────────────────────────────────────────
# The combined timing page: best windows, where the audience is, habit vs response,
# the day × hour grid. Same relative-engagement maths as get_posting_insights (a post
# ÷ its site's median of the headline metric), bucketed in the operator's display
# time zone (DST-correct via zoneinfo). Deterministic; nothing stored.
WHEN_KINDS = ("artwork", "story", "post")
_STORY_SITES = {"ao3", "sqw", "wp"}                      # unlinked rows on these → story
_POST_SITES = {"tw", "bsky", "mast", "tum", "thr", "ig"}  # unlinked microblog rows → post
_MIN_CELL = 3      # a bucket needs this many posts before it is evidence
_MIN_TIMED = 20    # below this many timed posts, don't name best windows at all


def _when_zone(zone_name, tz_offset_minutes):
    """(tzinfo, label): the display time zone when it loads, else the browser's fixed
    offset (a desktop without tzdata, or no zone saved yet — spec 016 removes this)."""
    from datetime import timezone, timedelta
    if zone_name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(zone_name), zone_name
        except Exception:
            pass
    # Real offsets are within ±14 h; anything past a day made timezone() raise → a 500 (WTPCLAMP).
    tz_offset_minutes = max(-1439, min(1439, int(tz_offset_minutes)))
    m = abs(tz_offset_minutes)
    label = f"UTC{'+' if tz_offset_minutes >= 0 else '-'}{m // 60:02d}:{m % 60:02d} (this browser)"
    return timezone(timedelta(minutes=tz_offset_minutes)), label


def _confidence(n: int) -> str:
    return "sure" if n >= 8 else "fairly sure" if n >= 5 else "early sign"


def _best_windows(grid, limit: int = 3) -> list[dict]:
    """FR-002. grid[day][hour] = list of relative-engagement ratios. Take the best cell
    with ≥ 3 posts, grow it over neighbouring hours of the same day while each stays
    within 85 % of it, score the window by the median of every post in it; repeat on
    what's left. A chosen window and the hour either side of it are spent, so the
    cards are different choices, never three adjacent hours of one day."""
    import statistics
    med = [[statistics.median(c) if len(c) >= _MIN_CELL else None for c in row] for row in grid]
    used: set = set()
    out: list[dict] = []
    while len(out) < limit:
        seeds = [(med[d][h], d, h) for d in range(7) for h in range(24)
                 if med[d][h] is not None and (d, h) not in used]
        if not seeds:
            break
        best, d, h = max(seeds, key=lambda s: (s[0], -s[1], -s[2]))   # ties: earliest day, hour

        def ok(x):
            return (0 <= x < 24 and med[d][x] is not None and (d, x) not in used
                    and med[d][x] >= 0.85 * best)
        h0 = h1 = h
        while ok(h0 - 1):
            h0 -= 1
        while ok(h1 + 1):
            h1 += 1
        pooled = [r for x in range(h0, h1 + 1) for r in grid[d][x]]
        out.append({"day": d, "h0": h0, "h1": h1, "lift": round(statistics.median(pooled), 2),
                    "count": len(pooled), "confidence": _confidence(len(pooled))})
        used.update((d, x) for x in range(h0 - 1, h1 + 2))
    out.sort(key=lambda w: -w["lift"])   # stable: equal lifts keep seed order
    return out


def _habit_range(counts: list[int]):
    """The shortest run of hours (wrapping midnight) holding ≥ 40 % of the posts →
    (first_hour, last_hour, share), or None with no timed posts."""
    total = sum(counts)
    if not total:
        return None
    for length in range(1, 25):
        cover, neg_start = max((sum(counts[(s + i) % 24] for i in range(length)), -s)
                               for s in range(24))
        if cover >= 0.4 * total:
            s = -neg_start
            return s, (s + length - 1) % 24, cover / total
    return None   # unreachable: length 24 covers everything


def _next_at(zone, day: int, hour: int, now) -> str:
    """The next `day` at `hour`:00 in `zone`, as a UTC ISO string (for the schedule link)."""
    from datetime import timedelta, timezone
    local = now.astimezone(zone)
    for add in range(8):
        cand = (local + timedelta(days=add)).replace(hour=hour, minute=0, second=0, microsecond=0)
        if cand.weekday() == day and cand > local:
            return cand.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ""


def _shares(totals: dict) -> dict:
    """Site share of one metric: ranked, the top 6 kept and the rest folded into
    "Other N sites" once there are more than 7."""
    ranked = sorted(((v, c) for c, v in totals.items() if v > 0), key=lambda x: (-x[0], x[1]))
    total = sum(v for v, _ in ranked)
    keep, rest = (ranked, []) if len(ranked) <= 7 else (ranked[:6], ranked[6:])

    def row(code, label, v):
        return {"code": code, "label": label, "value": v, "pct": round(100 * v / total, 1)}
    sites = [row(c, platform_metrics.get(c).label, v) for v, c in keep]
    if rest:
        sites.append(row("", f"Other {len(rest)} sites", sum(v for v, _ in rest)))
    return {"total": total, "sites": sites}


def get_when_to_post(conn: sqlite3.Connection, *, zone_name: str | None = None,
                     tz_offset_minutes: int = 0, site: str = "", kind: str = "",
                     span_days: int = 365, now=None) -> dict:
    """Everything the When-to-post section shows, for one set of filters (FR-001).

    site = a platform code or "" (all); kind = artwork / story / post or "" (all);
    span_days = how far back (0 = all time). A post's kind comes from its publication
    link where PawPoller knows it, else from the site (story archives → story,
    microblogs → post, the rest → artwork). Sites that only store a date count in
    the weekday view only. Imported pieces use the site's own post date (the
    submission row), never the import date."""
    import statistics
    from datetime import datetime, timedelta, timezone

    zone, zone_label = _when_zone(zone_name, tz_offset_minutes)
    now = now or datetime.now(timezone.utc)
    span_days = max(0, min(36500, int(span_days)))   # 100 years; a huge span overflowed the date (WTPCLAMP)
    since = now - timedelta(days=span_days) if span_days else None

    kinds: dict = {}
    try:
        for r in conn.execute("SELECT platform, external_id, content_type FROM publications"
                              " WHERE external_id != ''"):
            kinds[(r["platform"], str(r["external_id"]))] = r["content_type"]
    except sqlite3.OperationalError:
        pass

    grid = [[[] for _ in range(24)] for _ in range(7)]
    weekday = [[] for _ in range(7)]
    hour = [[] for _ in range(24)]
    totals = {"views": {}, "faves": {}, "comments": {}}
    present: list[str] = []

    for code, table in INSIGHT_TABLES.items():
        spec = platform_metrics.get(code)
        date_col = INSIGHT_DATE_COL.get(code, "posted_at")
        cols = {"m": INSIGHT_PRIMARY[code], "views": spec.views,   # None for score/engagement sites
                "faves": spec.faves, "comments": spec.comments}
        sel = ", ".join(f"{c} AS {k}" if c else f"NULL AS {k}" for k, c in cols.items())
        try:
            rows = conn.execute(f"SELECT submission_id AS sid, {sel}, {date_col} AS d"
                                f" FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue   # table/column missing on this install — skip platform
        picked = []
        for r in rows:
            dt, has_time = _parse_posted(r["d"])
            if dt is None:
                continue
            aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            if since and aware < since:
                continue
            k = kinds.get((code, str(r["sid"]))) or (
                "story" if code in _STORY_SITES else "post" if code in _POST_SITES else "artwork")
            if kind and k != kind:
                continue
            # A date-only value is already the site's calendar day — converting its
            # midnight would shift it across the date line.
            picked.append((r, aware.astimezone(zone) if has_time else dt, has_time))
        if not picked:
            continue
        present.append(code)
        if site and code != site:
            continue
        for key in ("views", "faves", "comments"):
            if cols[key]:
                totals[key][code] = totals[key].get(code, 0) + sum(max(0, r[key] or 0) for r, _, _ in picked)
        med = statistics.median([r["m"] or 0 for r, _, _ in picked])
        if med <= 0:
            continue
        for r, local, has_time in picked:
            ratio = (r["m"] or 0) / med
            weekday[local.weekday()].append(ratio)
            if has_time:
                hour[local.hour].append(ratio)
                grid[local.weekday()][local.hour].append(ratio)

    def bucket(g, total):
        return {"median": round(statistics.median(g), 2) if g else 0, "count": len(g),
                "share": round(len(g) / total, 3) if total else 0}
    timed = sum(len(g) for g in hour)
    dated = sum(len(g) for g in weekday)
    too_few = timed < _MIN_TIMED
    windows = [] if too_few else _best_windows(grid)

    gap = None
    habit = _habit_range([len(g) for g in hour])
    if windows and habit:
        h0, h1, share = habit
        hrs = [(h0 + i) % 24 for i in range((h1 - h0) % 24 + 1)]
        in_habit = [r for x in hrs for r in hour[x]]
        best = windows[0]
        gap = {"habit_h0": h0, "habit_h1": h1, "habit_share": round(share, 3),
               "habit_lift": round(statistics.median(in_habit), 2),
               "aligned": all(x in hrs for x in range(best["h0"], best["h1"] + 1)),
               "best": best, "next_at": _next_at(zone, best["day"], best["h0"], now)}

    return {
        "zone": zone_label, "filters": {"site": site, "kind": kind, "span_days": span_days},
        "sites": [{"code": c, "label": platform_metrics.get(c).label} for c in present],
        "posts": dated, "timed_posts": timed, "too_few": too_few, "min_timed": _MIN_TIMED,
        "windows": windows, "gap": gap,
        "shares": {k: _shares(v) for k, v in totals.items()},
        "hour": [bucket(g, timed) for g in hour],
        "weekday": [bucket(g, dated) for g in weekday],
        "grid": [[{"median": round(statistics.median(c), 2) if c else 0, "count": len(c)}
                  for c in row] for row in grid],
    }


# The "gallery" platforms whose submission `posted_at` is the artwork's REAL
# original post date (what makes a piece "old"). Publications.first_posted_at is
# the PawPoller *import* date for back-catalogue art — useless for age — so the
# radar anchors on these instead. Microblogs (tw/bsky/…) and e621 are excluded
# from the age anchor: their dates are recent crossposts/re-uploads, not the
# art's origin. They still count for links.
_GALLERY_DATE_PLATFORMS = {"ib", "fa", "ws", "sf", "sqw", "ao3", "wp", "da", "ik"}


def _artwork_gallery_dates(conn: sqlite3.Connection, pubs: list[dict]) -> dict:
    """Map (platform, str(external_id)) -> the platform's REAL posted-at string,
    read from the gallery submission tables (the authentic upload date polling
    captured). Batched: one query per platform, chunked under SQLite's var cap."""
    ids_by_plat: dict[str, set] = {}
    for p in pubs:
        plat = p.get("platform")
        ext = p.get("external_id")
        if plat in _GALLERY_DATE_PLATFORMS and plat in INSIGHT_TABLES and ext:
            ids_by_plat.setdefault(plat, set()).add(ext)
    out: dict[tuple, str] = {}
    for plat, ids in ids_by_plat.items():
        table = INSIGHT_TABLES[plat]
        date_col = INSIGHT_DATE_COL.get(plat, "posted_at")
        id_list = list(ids)
        for i in range(0, len(id_list), 900):
            chunk = id_list[i:i + 900]
            norm = [int(x) if str(x).isdigit() else x for x in chunk]
            ph = ",".join("?" * len(norm))
            try:
                rows = conn.execute(
                    f"SELECT submission_id AS sid, {date_col} AS d FROM {table} "
                    f"WHERE submission_id IN ({ph})", norm).fetchall()
            except sqlite3.OperationalError:
                continue   # table/column/id-col mismatch on this install — skip
            for r in rows:
                if r["d"]:
                    out[(plat, str(r["sid"]))] = r["d"]
    return out


def get_repost_candidates(conn: sqlite3.Connection, min_age_days: int = 60,
                          limit: int = 25) -> list[dict]:
    """Older, well-performing ARTWORK worth resurfacing to your feed.

    Pools each piece's artwork publications across every platform, ranks by
    pooled engagement, and gates to pieces whose ORIGINAL gallery post is older
    than ``min_age_days``. Fully deterministic — no model involved: it reads the
    real upload dates + the view/fave/comment counts the pollers already
    collected. The route layers on follower-growth-since-post context where the
    (young) follower history covers a piece; this core is purely the age +
    engagement ranking.
    """
    from datetime import datetime, timezone
    from database import posting_queries as _pq

    pubs = _pq.get_publications_with_stats(conn, content_type="artwork")
    gallery_dates = _artwork_gallery_dates(conn, pubs)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def _naive(dt):
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt

    by_piece: dict[str, dict] = {}
    for p in pubs:
        name = p.get("story_name")
        if not name:
            continue
        st = p.get("stats") or {}
        views = st.get("views") or st.get("hits") or st.get("reads") or 0
        faves = st.get("favorites_count") or st.get("kudos") or st.get("votes") or 0
        comments = st.get("comments_count") or 0
        d = by_piece.setdefault(name, {
            "name": name, "views": 0, "faves": 0, "comments": 0,
            "posted": None, "posted_raw": "", "import_dt": None, "import_raw": "",
            "platforms": {}})
        d["views"] += views
        d["faves"] += faves
        d["comments"] += comments

        plat = p.get("platform") or ""
        ext = str(p.get("external_id") or "")
        # Real gallery upload date → the age anchor (earliest = when the art
        # first went out). Keep the import date only as a last-resort fallback.
        raw = gallery_dates.get((plat, ext))
        if raw:
            dt, _ = _parse_posted(raw)
            if dt is not None:
                dt = _naive(dt)
                if d["posted"] is None or dt < d["posted"]:
                    d["posted"] = dt
                    d["posted_raw"] = raw
        idt, _ = _parse_posted(p.get("first_posted_at"))
        if idt is not None:
            idt = _naive(idt)
            if d["import_dt"] is None or idt < d["import_dt"]:
                d["import_dt"] = idt
                d["import_raw"] = p.get("first_posted_at") or ""

        # First non-empty url per platform wins (so a card always deep-links).
        if plat and (plat not in d["platforms"]
                     or (not d["platforms"][plat] and p.get("external_url"))):
            d["platforms"][plat] = p.get("external_url") or ""

    out = []
    for d in by_piece.values():
        anchor = d["posted"] or d["import_dt"]
        anchor_raw = d["posted_raw"] or d["import_raw"]
        if anchor is None:
            continue
        age_days = (now - anchor).days
        if age_days < min_age_days:
            continue
        # Nothing worth resurfacing if it never drew engagement (or was never
        # polled) — don't pad the radar with dead rows.
        if (d["views"] + d["faves"] + d["comments"]) <= 0:
            continue
        score = d["faves"] * 3 + d["views"] + d["comments"] * 5
        out.append({
            "name": d["name"],
            "posted": anchor_raw,
            "age_days": age_days,
            "views": d["views"], "faves": d["faves"], "comments": d["comments"],
            "score": score,
            "platforms": [{"platform": k, "url": v}
                          for k, v in sorted(d["platforms"].items())],
        })
    out.sort(key=lambda x: (-x["score"], -x["age_days"]))
    return out[:limit]


def _norm_tag(t: str) -> str:
    """Canonicalise a keyword for cross-platform aggregation: lowercase and
    fold underscores to spaces so FA's ``big_muscle`` and SoFurry's
    ``big muscle`` count as the same tag (platforms format tags differently —
    see the tag-format rule)."""
    return t.strip().lower().replace("_", " ")


def _is_machine_tag(code: str, raw: str) -> bool:
    """True for a platform's auto-generated faceted keyword — not an
    artist-chosen tag, so it must be excluded from tag performance.

    FA stamps every submission's keywords with faceted atoms: ``u_<username>``
    (uploader), ``c_<category>``, ``t_<type>``, ``s_<species>``, ``g_<gender>``.
    They ride on every FA piece, so ``u_secondfur`` would otherwise top the
    "on your best work" list — meaningless as advice. Matched on the raw tag
    (before underscore-folding). Rare real collateral like an FA ``t_rex`` tag
    is an acceptable trade against the per-submission noise on every FA piece.
    """
    return (code == "fa" and len(raw) > 2 and raw[1] == "_"
            and raw[0] in "uctsg")


def get_tag_performance(conn: sqlite3.Connection, min_works: int = 3,
                        limit: int = 40, platform: str | None = None) -> dict:
    """Which tags correlate with better engagement across YOUR own posts.

    Deterministic — no model, no AI. It reads the keywords the pollers already
    captured on each submission alongside that submission's stats, normalises
    every piece against its OWN platform's median headline metric (so a
    10k-view SoFurry story and a 200-view FA picture are comparable), then
    aggregates per tag. ``index`` is the median of those ratios: > 1.0 means
    pieces carrying that tag beat their platform's typical piece; < 1.0 means
    they lag. Also returns the best-performing tag PAIRS. Tags appearing on
    fewer than ``min_works`` pieces are dropped as too noisy to trust.
    """
    import statistics
    from collections import defaultdict
    from database import collections_queries as _cq
    try:
        from polling.telegram import PLATFORM_METRICS
    except Exception:
        PLATFORM_METRICS = {}

    tag_agg: dict = defaultdict(lambda: {"works": 0, "reach": 0, "faves": 0, "ratios": []})
    pair_agg: dict = defaultdict(lambda: {"works": 0, "ratios": []})

    for code, table in INSIGHT_TABLES.items():
        if platform and code != platform:
            continue
        primary = INSIGHT_PRIMARY.get(code, "views")
        fcol = (PLATFORM_METRICS.get(code) or {}).get("faves")
        sel_faves = fcol if fcol else "0"
        try:
            rows = conn.execute(
                f"SELECT keywords, {primary} AS m, {sel_faves} AS f FROM {table}"
            ).fetchall()
        except sqlite3.OperationalError:
            continue   # table lacks keywords / the metric column on this install
        ms = [(r["m"] or 0) for r in rows]
        if not ms:
            continue
        med = statistics.median(ms)
        for r in rows:
            tags = _cq._parse_tags(r["keywords"])
            if not tags:
                continue
            m = r["m"] or 0
            f = r["f"] or 0
            ratio = (m / med) if med and med > 0 else 0.0
            norm = sorted({_norm_tag(t) for t in tags
                           if t and t.strip() and not _is_machine_tag(code, t)})
            for t in norm:
                a = tag_agg[t]
                a["works"] += 1
                a["reach"] += m
                a["faves"] += f
                a["ratios"].append(ratio)
            # Best pairs — bound the per-row combinatorics (skip mega-tagged rows
            # so one 80-tag piece can't emit thousands of pairs).
            if 2 <= len(norm) <= 25:
                for i in range(len(norm)):
                    for j in range(i + 1, len(norm)):
                        p = pair_agg[(norm[i], norm[j])]
                        p["works"] += 1
                        p["ratios"].append(ratio)

    tags_out = []
    for t, a in tag_agg.items():
        if a["works"] < min_works:
            continue
        idx = statistics.median(a["ratios"]) if a["ratios"] else 0
        tags_out.append({
            "tag": t, "works": a["works"], "reach": a["reach"], "faves": a["faves"],
            "avg_reach": round(a["reach"] / a["works"]) if a["works"] else 0,
            "index": round(idx, 2),
        })
    tags_out.sort(key=lambda x: (x["index"], x["reach"]), reverse=True)

    pair_min = max(min_works, 3)
    pairs_out = []
    for (x, y), p in pair_agg.items():
        if p["works"] < pair_min:
            continue
        idx = statistics.median(p["ratios"]) if p["ratios"] else 0
        pairs_out.append({"tags": [x, y], "works": p["works"], "index": round(idx, 2)})
    pairs_out.sort(key=lambda x: (x["index"], x["works"]), reverse=True)

    return {"tags": tags_out[:limit], "pairs": pairs_out[:15],
            "min_works": min_works, "tag_universe": len(tag_agg)}
