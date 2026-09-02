"""Telegram bot command handler — two-way interaction with PawPoller.

Polls Telegram's getUpdates API for incoming commands and dispatches them
to query functions, poll triggers, and settings mutators.

Commands:
  /help                     — list all commands with descriptions
  /stats                    — cross-platform totals (all 11 platforms)
  /top [platform]           — top 5 submissions by views/likes
  /trending                 — spike detection results (z-score)
  /digest                   — trigger a digest report now
  /fans                     — top fans leaderboard
  /poll [platform|all]      — force a poll cycle (works even when paused)
  /pause                    — pause all scheduled background polling
  /resume                   — resume scheduled background polling
  /status                   — poll status, last poll times, scheduler state
  /interval [minutes]       — change unified poll interval (min: 15)
  /notify                   — show notification toggle states
  /notify [type] [on|off]   — toggle specific notification types
"""

from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from html import escape as _esc
from zoneinfo import ZoneInfo

import httpx

import config
from database.db import get_connection
from database import queries, fa_queries, ws_queries, sf_queries, sqw_queries, ao3_queries, da_queries, wp_queries, ik_queries, bsky_queries, tw_queries
from database.analytics_queries import get_top_fans, get_trending_submissions

logger = logging.getLogger(__name__)

# Track the last processed update_id to avoid processing duplicates.
_last_update_id = 0


async def _send(token: str, chat_id: str, text: str) -> None:
    """Send an HTML message back to the user."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.warning("Bot reply failed: %s", e)


_CONFLICT_BACKOFF = 0  # seconds to sleep before next poll (0 = normal)


async def _poll_updates(token: str) -> list[dict]:
    """Fetch new messages from Telegram using long polling.

    Returns an empty list on error.  On 409 Conflict (another instance is
    polling the same bot token), sets an exponential backoff so the main
    loop sleeps instead of hammering the API every few seconds.
    """
    global _last_update_id, _CONFLICT_BACKOFF
    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": _last_update_id + 1, "timeout": 30},
            )
            # 409 Conflict = another bot instance is polling the same token.
            # Back off exponentially (30s → 60s → 120s, cap 300s) to avoid
            # flooding the logs.  Resets to 0 on a successful poll.
            if resp.status_code == 409:
                _CONFLICT_BACKOFF = min(max(_CONFLICT_BACKOFF * 2, 30), 300)
                logger.warning("Bot getUpdates 409 Conflict — another instance is polling this token. "
                               "Backing off %ds.", _CONFLICT_BACKOFF)
                return []
            data = resp.json()
            if not data.get("ok"):
                return []
            _CONFLICT_BACKOFF = 0  # Successful poll — reset backoff
            results = data.get("result", [])
            if results:
                _last_update_id = results[-1]["update_id"]
            return results
    except Exception as e:
        logger.warning("Bot getUpdates failed: %s", e)
        return []


# ── Command handlers ─────────────────────────────────────────

async def _cmd_help(token: str, chat_id: str, args: str) -> None:
    settings = config.get_settings()
    paused = settings.get("polling_paused", False)
    text = f"""<b>🐾 PawPoller Commands</b>

<b>📊 Data Queries</b>
/stats — Cross-platform totals (subs, views, faves, comments across all 11 platforms)
/top [platform] — Top 5 submissions by views/likes (default: ib)
/trending — Submissions with unusual activity spikes (z-score detection)
/fans — Top fans leaderboard by fave + comment score
/digest — Trigger a full digest report now
/digest interval [hours] — Change digest interval (1-168h, default 6)
/digest weekly — Trigger weekly digest now
/digest weekly [on|off] — Toggle weekly digest (default: on)

<b>⚙️ Polling Control</b>
/poll [platform|all] — Force an immediate poll cycle (works even when paused)
/pause — Pause all scheduled background polling
/resume — Resume scheduled background polling
/status — Last poll times, intervals, and scheduler state
/interval [platform] [mins] — Change poll interval (min: 15)

<b>🔔 Notifications</b>
/notify — Show all notification toggle states
/notify [type] [on|off] — Toggle a notification type

  <b>Telegram features:</b> summaries, errors, milestones, digest
  <b>Platform alerts:</b> ib, fa, ws, sf, sqw, ao3, da, wp, ik, bsky, tw
  <b>Filters:</b> faves, comments, watchers

<b>📤 Publishing</b>
/stories — List available stories in archive
/upload &lt;story&gt; [platforms] — Post story to platforms (e.g. /upload Example_Story ib,sf)
/update &lt;story&gt; [platforms] — Push updates to already-posted submissions
/update all [platforms] — Update all stories with changed files
/posted [story] — Show publication registry
/claim [platforms] — Claim existing submissions into publications registry
/changes — Show which stories have changed since last update
/sync — Show archive status (published vs unpublished)
/sync push — Push local archive to remote server

<b>📎 Platform Codes</b>
ib=Inkbunny  fa=FurAffinity  ws=Weasyl  sf=SoFurry
sqw=SquidgeWorld  ao3=AO3  da=DeviantArt  wp=Wattpad
ik=Itaku  bsky=Bluesky  tw=X/Twitter

<b>Scheduler:</b> {'⏸ Paused' if paused else '▶️ Active'}"""
    await _send(token, chat_id, text)


async def _cmd_stats(token: str, chat_id: str, args: str) -> None:
    conn = get_connection()
    try:
        lines = ["<b>📊 PawPoller Stats</b>", ""]

        # Standard platforms: views, favorites_count, comments_count
        std_platforms = [
            ("🐾", "Inkbunny", "submissions"),
            ("🦊", "FurAffinity", "fa_submissions"),
            ("🦎", "Weasyl", "ws_submissions"),
            ("🐺", "SoFurry", "sf_submissions"),
            ("🦑", "SquidgeWorld", "sqw_submissions"),
            ("📖", "AO3", "ao3_submissions"),
        ]

        grand_v = grand_f = grand_c = grand_s = 0

        for emoji, name, table in std_platforms:
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) as subs, COALESCE(SUM(views),0) as views, "
                    f"COALESCE(SUM(favorites_count),0) as faves, "
                    f"COALESCE(SUM(comments_count),0) as comments "
                    f"FROM {table}"
                ).fetchone()
                row = dict(row)
                if row["subs"] == 0:
                    continue
                grand_v += row["views"]
                grand_f += row["faves"]
                grand_c += row["comments"]
                grand_s += row["subs"]
                lines.append(f"<b>{emoji} {name}</b> ({row['subs']} subs)")
                lines.append(f"  Views: {row['views']:,}  Faves: {row['faves']:,}  Comments: {row['comments']:,}")
            except Exception:
                continue

        # DeviantArt: views, favorites_count, comments_count, downloads
        try:
            row = conn.execute(
                "SELECT COUNT(*) as subs, COALESCE(SUM(views),0) as views, "
                "COALESCE(SUM(favorites_count),0) as faves, "
                "COALESCE(SUM(comments_count),0) as comments, "
                "COALESCE(SUM(downloads),0) as downloads "
                "FROM da_submissions"
            ).fetchone()
            row = dict(row)
            if row["subs"] > 0:
                grand_v += row["views"]
                grand_f += row["faves"]
                grand_c += row["comments"]
                grand_s += row["subs"]
                lines.append(f"<b>🎨 DeviantArt</b> ({row['subs']} subs)")
                lines.append(f"  Views: {row['views']:,}  Faves: {row['faves']:,}  Comments: {row['comments']:,}  DLs: {row['downloads']:,}")
        except Exception:
            pass

        # Wattpad: reads, votes, comments_count, num_lists
        try:
            row = conn.execute(
                "SELECT COUNT(*) as subs, COALESCE(SUM(reads),0) as reads, "
                "COALESCE(SUM(votes),0) as votes, "
                "COALESCE(SUM(comments_count),0) as comments, "
                "COALESCE(SUM(num_lists),0) as lists "
                "FROM wp_submissions"
            ).fetchone()
            row = dict(row)
            if row["subs"] > 0:
                grand_v += row["reads"]
                grand_f += row["votes"]
                grand_c += row["comments"]
                grand_s += row["subs"]
                lines.append(f"<b>📙 Wattpad</b> ({row['subs']} subs)")
                lines.append(f"  Reads: {row['reads']:,}  Votes: {row['votes']:,}  Comments: {row['comments']:,}  Lists: {row['lists']:,}")
        except Exception:
            pass

        # Itaku: likes, comments_count, reshares (no views)
        try:
            row = conn.execute(
                "SELECT COUNT(*) as subs, COALESCE(SUM(likes),0) as likes, "
                "COALESCE(SUM(comments_count),0) as comments, "
                "COALESCE(SUM(reshares),0) as reshares "
                "FROM ik_submissions"
            ).fetchone()
            row = dict(row)
            if row["subs"] > 0:
                grand_f += row["likes"]
                grand_c += row["comments"]
                grand_s += row["subs"]
                lines.append(f"<b>🎯 Itaku</b> ({row['subs']} subs)")
                lines.append(f"  Likes: {row['likes']:,}  Comments: {row['comments']:,}  Reshares: {row['reshares']:,}")
        except Exception:
            pass

        # Bluesky: likes, reposts, replies, quotes (no views)
        try:
            row = conn.execute(
                "SELECT COUNT(*) as subs, COALESCE(SUM(likes),0) as likes, "
                "COALESCE(SUM(reposts),0) as reposts, "
                "COALESCE(SUM(replies),0) as replies, "
                "COALESCE(SUM(quotes),0) as quotes "
                "FROM bsky_submissions"
            ).fetchone()
            row = dict(row)
            if row["subs"] > 0:
                grand_f += row["likes"]
                grand_c += row["replies"]
                grand_s += row["subs"]
                lines.append(f"<b>🦋 Bluesky</b> ({row['subs']} subs)")
                lines.append(f"  Likes: {row['likes']:,}  Reposts: {row['reposts']:,}  Replies: {row['replies']:,}  Quotes: {row['quotes']:,}")
        except Exception:
            pass

        # X/Twitter: views, likes, retweets, replies, quotes, bookmarks
        try:
            row = conn.execute(
                "SELECT COUNT(*) as subs, COALESCE(SUM(views),0) as views, "
                "COALESCE(SUM(likes),0) as likes, "
                "COALESCE(SUM(retweets),0) as retweets, "
                "COALESCE(SUM(replies),0) as replies "
                "FROM tw_submissions"
            ).fetchone()
            row = dict(row)
            if row["subs"] > 0:
                grand_v += row["views"]
                grand_f += row["likes"]
                grand_c += row["replies"]
                grand_s += row["subs"]
                lines.append(f"<b>🐦 X/Twitter</b> ({row['subs']} subs)")
                lines.append(f"  Views: {row['views']:,}  Likes: {row['likes']:,}  Retweets: {row['retweets']:,}  Replies: {row['replies']:,}")
        except Exception:
            pass

        if grand_s > 0:
            lines.append("")
            lines.append(f"<b>📈 Total</b>: {grand_v:,} views, {grand_f:,} faves, {grand_c:,} comments")

        await _send(token, chat_id, "\n".join(lines) if grand_s > 0 else "No data yet.")
    finally:
        conn.close()


async def _cmd_top(token: str, chat_id: str, args: str) -> None:
    platform = args.strip().lower() if args.strip() else "ib"

    # table, sort_col, display columns: list of (col_name, label)
    platform_cfg = {
        "ib":  ("submissions",     "views", [("views", "views"), ("favorites_count", "faves"), ("comments_count", "comments")]),
        "fa":  ("fa_submissions",  "views", [("views", "views"), ("favorites_count", "faves"), ("comments_count", "comments")]),
        "ws":  ("ws_submissions",  "views", [("views", "views"), ("favorites_count", "faves"), ("comments_count", "comments")]),
        "sf":  ("sf_submissions",  "views", [("views", "views"), ("favorites_count", "faves"), ("comments_count", "comments")]),
        "sqw": ("sqw_submissions", "views", [("views", "views"), ("favorites_count", "faves"), ("comments_count", "comments")]),
        "ao3": ("ao3_submissions", "views", [("views", "views"), ("favorites_count", "faves"), ("comments_count", "comments")]),
        "da":  ("da_submissions",  "views", [("views", "views"), ("favorites_count", "faves"), ("comments_count", "comments"), ("downloads", "DLs")]),
        "wp":  ("wp_submissions",  "reads", [("reads", "reads"), ("votes", "votes"), ("comments_count", "comments"), ("num_lists", "lists")]),
        "ik":  ("ik_submissions",  "likes", [("likes", "likes"), ("comments_count", "comments"), ("reshares", "reshares")]),
        "bsky": ("bsky_submissions", "likes", [("likes", "likes"), ("reposts", "reposts"), ("replies", "replies"), ("quotes", "quotes")]),
        "tw":  ("tw_submissions",  "views", [("views", "views"), ("likes", "likes"), ("retweets", "retweets"), ("replies", "replies")]),
    }
    emoji_map = {"ib": "🐾", "fa": "🦊", "ws": "🦎", "sf": "🐺", "sqw": "🦑", "ao3": "📖", "da": "🎨", "wp": "📙", "ik": "🎯", "bsky": "🦋", "tw": "🐦"}
    name_map = {"ib": "Inkbunny", "fa": "FurAffinity", "ws": "Weasyl", "sf": "SoFurry", "sqw": "SquidgeWorld", "ao3": "AO3", "da": "DeviantArt", "wp": "Wattpad", "ik": "Itaku", "bsky": "Bluesky", "tw": "X/Twitter"}

    if platform not in platform_cfg:
        await _send(token, chat_id, "Usage: /top [ib|fa|ws|sf|sqw|ao3|da|wp|ik|bsky|tw]")
        return

    table, sort_col, columns = platform_cfg[platform]
    col_names = [c[0] for c in columns]

    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT title, {', '.join(col_names)} FROM {table} "
            f"ORDER BY {sort_col} DESC LIMIT 5"
        ).fetchall()

        if not rows:
            await _send(token, chat_id, f"No {name_map[platform]} submissions found.")
            return

        lines = [f"<b>{emoji_map[platform]} {name_map[platform]} Top 5</b>", ""]
        for i, r in enumerate(rows, 1):
            r = dict(r)
            lines.append(f"{i}. <b>{_esc(r['title'][:40])}</b>")
            stats = " | ".join(f"{r[c]:,} {label}" for c, label in columns)
            lines.append(f"   {stats}")

        await _send(token, chat_id, "\n".join(lines))
    finally:
        conn.close()


async def _cmd_trending(token: str, chat_id: str, args: str) -> None:
    conn = get_connection()
    try:
        results = get_trending_submissions(conn)
        if not results:
            await _send(token, chat_id, "No trending submissions detected.")
            return

        lines = ["<b>🔥 Trending Submissions</b>", ""]
        for r in results[:5]:
            emoji = {"ib": "🐾", "fa": "🦊", "ws": "🦎", "sf": "🐺", "sqw": "🦑", "ao3": "📖", "da": "🎨", "wp": "📙", "ik": "🎯"}.get(r["platform"], "")
            lines.append(f"{emoji} <b>{_esc(r['title'][:35])}</b> (z={r['max_z']})")
            for metric, info in r["spikes"].items():
                label = metric.replace("_count", "").replace("favorites", "faves")
                lines.append(f"   +{info['delta']} {label} (avg {info['mean']})")

        await _send(token, chat_id, "\n".join(lines))
    finally:
        conn.close()


async def _cmd_digest(token: str, chat_id: str, args: str) -> None:
    parts = args.strip().split()

    # /digest interval <hours> — change digest interval
    if len(parts) == 2 and parts[0] == "interval":
        try:
            hours = int(parts[1])
            if hours < 1 or hours > 168:
                await _send(token, chat_id, "Interval must be between 1 and 168 hours.")
                return
            config.save_settings({"telegram_digest_interval_hours": hours})
            await _send(token, chat_id, f"✅ Digest interval set to <b>{hours}h</b>. Takes effect next cycle.")
        except ValueError:
            await _send(token, chat_id, "Usage: /digest interval [hours]  (1-168)")
        return

    # /digest interval — show current interval
    if len(parts) == 1 and parts[0] == "interval":
        hours = config.get_settings().get("telegram_digest_interval_hours", 6)
        await _send(token, chat_id, f"Current digest interval: <b>{hours}h</b>\nUsage: /digest interval [hours]")
        return

    # /digest weekly — trigger weekly digest now
    if len(parts) == 1 and parts[0] == "weekly":
        from polling.telegram import send_weekly_digest_report
        try:
            await send_weekly_digest_report()
            await _send(token, chat_id, "Weekly digest sent.")
        except Exception as e:
            await _send(token, chat_id, f"Weekly digest failed: {_esc(str(e)[:200])}")
        return

    # /digest weekly on|off — toggle weekly digest
    if len(parts) == 2 and parts[0] == "weekly":
        if parts[1] in ("on", "off"):
            enabled = parts[1] == "on"
            config.save_settings({"telegram_weekly_digest": enabled})
            await _send(token, chat_id, f"Weekly digest: <b>{'on' if enabled else 'off'}</b>")
            return

    # /digest — trigger immediately
    from polling.telegram import send_digest_report
    try:
        await send_digest_report()
        await _send(token, chat_id, "Digest sent.")
    except Exception as e:
        await _send(token, chat_id, f"Digest failed: {_esc(str(e)[:200])}")


async def _cmd_fans(token: str, chat_id: str, args: str) -> None:
    conn = get_connection()
    try:
        fans = get_top_fans(conn, limit=10)
        if not fans:
            await _send(token, chat_id, "No fan data yet.")
            return

        lines = ["<b>⭐ Top Fans</b>", ""]
        for i, f in enumerate(fans, 1):
            plats = ", ".join(f["platforms"])
            lines.append(f"{i}. <b>{_esc(f['username'])}</b> — score {f['score']} ({f['fave_count']} faves, {f['comment_count']} comments) [{plats}]")

        await _send(token, chat_id, "\n".join(lines))
    finally:
        conn.close()


async def _cmd_poll(token: str, chat_id: str, args: str) -> None:
    platform = args.strip().lower() if args.strip() else "all"
    valid = {"ib", "fa", "ws", "sf", "sqw", "ao3", "da", "wp", "ik", "bsky", "tw", "all"}
    if platform not in valid:
        await _send(token, chat_id, "Usage: /poll [ib|fa|ws|sf|sqw|ao3|da|wp|ik|bsky|tw|all]")
        return

    all_platforms = ["ib", "fa", "ws", "sf", "sqw", "ao3", "da", "wp", "ik", "bsky", "tw"]
    targets = all_platforms if platform == "all" else [platform]
    name_map = {"ib": "Inkbunny", "fa": "FurAffinity", "ws": "Weasyl", "sf": "SoFurry", "sqw": "SquidgeWorld", "ao3": "AO3", "da": "DeviantArt", "wp": "Wattpad", "ik": "Itaku", "bsky": "Bluesky", "tw": "X/Twitter"}

    await _send(token, chat_id, f"Starting poll for: {', '.join(name_map[t] for t in targets)}...")

    from polling.poller import run_poll_cycle
    from polling.fa_poller import run_fa_poll_cycle
    from polling.ws_poller import run_ws_poll_cycle
    from polling.sf_poller import run_sf_poll_cycle
    from polling.sqw_poller import run_sqw_poll_cycle
    from polling.ao3_poller import run_ao3_poll_cycle
    from polling.da_poller import run_da_poll_cycle
    from polling.wp_poller import run_wp_poll_cycle
    from polling.ik_poller import run_ik_poll_cycle
    from polling.bsky_poller import run_bsky_poll_cycle
    from polling.tw_poller import run_tw_poll_cycle

    poll_funcs = {
        "ib": run_poll_cycle, "fa": run_fa_poll_cycle, "ws": run_ws_poll_cycle, "sf": run_sf_poll_cycle,
        "sqw": run_sqw_poll_cycle, "ao3": run_ao3_poll_cycle, "da": run_da_poll_cycle, "wp": run_wp_poll_cycle, "ik": run_ik_poll_cycle,
        "bsky": run_bsky_poll_cycle, "tw": run_tw_poll_cycle,
    }

    results = []
    for t in targets:
        try:
            stats = await poll_funcs[t]()
            subs = stats.get("submissions_found", 0)
            results.append(f"{name_map[t]}: {subs} subs polled")
        except Exception as e:
            results.append(f"{name_map[t]}: failed — {_esc(str(e)[:100])}")

    await _send(token, chat_id, "\n".join(results))


async def _cmd_status(token: str, chat_id: str, args: str) -> None:
    conn = get_connection()
    try:
        settings = config.get_settings()
        paused = settings.get("polling_paused", False)
        lines = [f"<b>📋 Poll Status</b> {'⏸ PAUSED' if paused else '▶️ Active'}", ""]

        # Resolve display timezone
        tz_name = settings.get("display_timezone", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except (KeyError, Exception):
            tz = timezone.utc

        poll_funcs = [
            ("🐾 Inkbunny", queries.get_last_poll),
            ("🦊 FurAffinity", fa_queries.get_fa_last_poll),
            ("🦎 Weasyl", ws_queries.get_ws_last_poll),
            ("🐺 SoFurry", sf_queries.get_sf_last_poll),
            ("🦑 SquidgeWorld", sqw_queries.get_sqw_last_poll),
            ("📖 AO3", ao3_queries.get_ao3_last_poll),
            ("🎨 DeviantArt", da_queries.get_da_last_poll),
            ("📙 Wattpad", wp_queries.get_wp_last_poll),
            ("🎯 Itaku", ik_queries.get_ik_last_poll),
            ("🦋 Bluesky", bsky_queries.get_bsky_last_poll),
            ("🐦 X/Twitter", tw_queries.get_tw_last_poll),
        ]

        for name, func in poll_funcs:
            try:
                poll = func(conn)
                if poll:
                    status = poll.get("status", "?")
                    raw_time = poll.get("started_at", "")
                    # Convert stored UTC timestamp to display timezone
                    if raw_time:
                        try:
                            dt = datetime.fromisoformat(raw_time.replace(" ", "T"))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            local = dt.astimezone(tz)
                            time_str = local.strftime("%Y-%m-%d %H:%M %Z")
                        except (ValueError, TypeError):
                            time_str = raw_time
                    else:
                        time_str = "?"
                    dur = poll.get("duration_seconds")
                    dur_str = f" ({dur:.1f}s)" if dur else ""
                    err = poll.get("error_message")
                    lines.append(f"<b>{name}</b>")
                    lines.append(f"  Last: {time_str}{dur_str} — {status}")
                    if err:
                        lines.append(f"  Error: {_esc(err[:100])}")
                else:
                    lines.append(f"<b>{name}</b>: No polls recorded")
            except Exception:
                lines.append(f"<b>{name}</b>: No data")

        # Show unified poll interval
        interval = settings.get("poll_interval_minutes", 60)
        lines.append("")
        lines.append(f"<b>Poll Interval:</b> {interval} min (all platforms)")
        lines.append("All 11 platforms poll together in a single cycle.")

        await _send(token, chat_id, "\n".join(lines))
    finally:
        conn.close()


async def _cmd_interval(token: str, chat_id: str, args: str) -> None:
    parts = args.strip().lower().split()

    if len(parts) == 0:
        current = config.get_settings().get("poll_interval_minutes", 60)
        await _send(token, chat_id, f"Current poll interval: {current} min\nUsage: /interval [minutes]")
        return

    # Backward compat: if someone sends the old "/interval fa 60" format,
    # explain that per-platform intervals are no longer used.
    if len(parts) == 2:
        first, second = parts
        platform_keys = {"ib", "fa", "ws", "sf", "sqw", "ao3", "da", "wp", "ik", "bsky", "tw"}
        if first in platform_keys:
            await _send(
                token, chat_id,
                "Per-platform intervals are no longer used. "
                "All 11 platforms now poll together in a single unified cycle.\n\n"
                f"Usage: <code>/interval {second}</code> to set the interval to {second} min for all platforms.",
            )
            return

    if len(parts) != 1:
        await _send(token, chat_id, "Usage: /interval [minutes]\nSets the unified poll interval for all platforms.")
        return

    try:
        minutes = int(parts[0])
        if minutes < 15:
            await _send(token, chat_id, "Minimum interval is 15 minutes.")
            return
    except ValueError:
        await _send(token, chat_id, "Minutes must be a number.")
        return

    config.save_settings({"poll_interval_minutes": minutes})
    await _send(token, chat_id, f"Poll interval set to {minutes} minutes for all platforms.")


async def _cmd_pause(token: str, chat_id: str, args: str) -> None:
    settings = config.get_settings()
    if settings.get("polling_paused", False):
        await _send(token, chat_id, "⏸ Polling is already paused.")
        return
    config.save_settings({"polling_paused": True})
    logger.info("Polling PAUSED via Telegram")
    await _send(token, chat_id, "⏸ <b>Polling paused.</b>\nAll scheduled background polls are now skipped.\nManual /poll commands still work.\nUse /resume to restart.")


async def _cmd_resume(token: str, chat_id: str, args: str) -> None:
    settings = config.get_settings()
    if not settings.get("polling_paused", False):
        await _send(token, chat_id, "▶️ Polling is already active.")
        return
    config.save_settings({"polling_paused": False})
    logger.info("Polling RESUMED via Telegram")
    await _send(token, chat_id, "▶️ <b>Polling resumed.</b>\nScheduled background polls will run on their normal intervals.")


async def _cmd_notify(token: str, chat_id: str, args: str) -> None:
    settings = config.get_settings()

    # Notification type -> settings key mapping
    toggle_map = {
        "summaries": "telegram_poll_summaries",
        "errors": "telegram_error_alerts",
        "milestones": "telegram_milestones",
        "digest": "telegram_digest",
        "faves": "notification_comments_only",  # inverted — comments_only=True means faves OFF
        "comments": "ib_comment_notifications_enabled",
        "watchers": "watcher_notifications_enabled",
        "ib": "notifications_enabled",
        "fa": "fa_notifications_enabled",
        "ws": "ws_notifications_enabled",
        "sf": "sf_notifications_enabled",
        "sqw": "sqw_notifications_enabled",
        "ao3": "ao3_notifications_enabled",
        "da": "da_notifications_enabled",
        "wp": "wp_notifications_enabled",
        "ik": "ik_notifications_enabled",
        "bsky": "bsky_notifications_enabled",
        "tw": "tw_notifications_enabled",
    }

    parts = args.strip().lower().split()

    # No args — show current state
    if not parts:
        lines = ["<b>🔔 Notification Settings</b>", ""]

        lines.append("<b>Telegram Features</b>")
        lines.append(f"  Summaries: {'on' if settings.get('telegram_poll_summaries', True) else 'off'}")
        lines.append(f"  Errors: {'on' if settings.get('telegram_error_alerts', True) else 'off'}")
        lines.append(f"  Milestones: {'on' if settings.get('telegram_milestones', True) else 'off'}")
        digest_hours = settings.get('telegram_digest_interval_hours', 6)
        lines.append(f"  Digest: {'on' if settings.get('telegram_digest', True) else 'off'} (every {digest_hours}h)")

        lines.append("")
        lines.append("<b>Platform Notifications</b>")
        lines.append(f"  IB: {'on' if settings.get('notifications_enabled', True) else 'off'}")
        lines.append(f"  FA: {'on' if settings.get('fa_notifications_enabled', True) else 'off'}")
        lines.append(f"  WS: {'on' if settings.get('ws_notifications_enabled', True) else 'off'}")
        lines.append(f"  SF: {'on' if settings.get('sf_notifications_enabled', True) else 'off'}")
        lines.append(f"  SqW: {'on' if settings.get('sqw_notifications_enabled', True) else 'off'}")
        lines.append(f"  AO3: {'on' if settings.get('ao3_notifications_enabled', True) else 'off'}")
        lines.append(f"  DA: {'on' if settings.get('da_notifications_enabled', True) else 'off'}")
        lines.append(f"  WP: {'on' if settings.get('wp_notifications_enabled', True) else 'off'}")
        lines.append(f"  IK: {'on' if settings.get('ik_notifications_enabled', True) else 'off'}")
        lines.append(f"  BSKY: {'on' if settings.get('bsky_notifications_enabled', True) else 'off'}")
        lines.append(f"  TW: {'on' if settings.get('tw_notifications_enabled', True) else 'off'}")

        lines.append("")
        lines.append("<b>Filters</b>")
        lines.append(f"  Comments only (IB): {'on' if settings.get('notification_comments_only', False) else 'off'}")
        lines.append(f"  Watcher alerts: {'on' if settings.get('watcher_notifications_enabled', True) else 'off'}")
        min_faves = settings.get('notification_min_faves_delta', 0)
        lines.append(f"  Min faves delta: {min_faves if min_faves > 0 else 'off'}")

        await _send(token, chat_id, "\n".join(lines))
        return

    if len(parts) != 2 or parts[1] not in ("on", "off"):
        await _send(token, chat_id, "Usage: /notify [type] [on|off]\nType /help for all options.")
        return

    ntype, state = parts
    enabled = state == "on"

    if ntype not in toggle_map:
        await _send(token, chat_id, f"Unknown type: {ntype}. Type /help for options.")
        return

    key = toggle_map[ntype]

    # Special case: "faves" is inverted (comments_only=True means faves OFF)
    if ntype == "faves":
        config.save_settings({key: not enabled})
        await _send(token, chat_id, f"Fave notifications: {'on' if enabled else 'off'}")
        return

    config.save_settings({key: enabled})
    await _send(token, chat_id, f"{ntype.capitalize()} notifications: {'on' if enabled else 'off'}")


# ── Posting commands ─────────────────────────────────────────


async def _cmd_upload(token: str, chat_id: str, args: str) -> None:
    """Handle /upload <story> [platforms] — post story to platforms."""
    parts = args.strip().split()
    if not parts:
        await _send(token, chat_id, "<b>Usage:</b> /upload &lt;story_name&gt; [platforms]\nExample: /upload Example_Story ib,sf")
        return

    story_name = parts[0]
    platforms = parts[1].split(",") if len(parts) > 1 else None

    if not platforms:
        settings = config.get_settings()
        platforms = settings.get("posting_default_platforms", ["ib"])

    await _send(token, chat_id, f"📤 Uploading <b>{_esc(story_name.replace('_', ' '))}</b> to {_esc(', '.join(platforms))}...")

    try:
        from posting import manager
        results = await manager.post_story(story_name, platforms)

        lines = []
        for r in results:
            emoji = manager.PLATFORM_EMOJIS.get(r["platform"], "📦")
            ch_label = f'Ch{r["chapter_index"]} "{_esc(r["chapter_title"])}"' if r.get("chapter_title") else "Full"
            if r["success"]:
                lines.append(f'{emoji} {r["platform"].upper()} {ch_label} — posted ✅')
            else:
                lines.append(f'{emoji} {r["platform"].upper()} {ch_label} — failed ❌ {_esc(r.get("error", "")[:80])}')

        successes = sum(1 for r in results if r["success"])
        lines.append(f"\n✅ {successes}/{len(results)} uploads complete")
        await _send(token, chat_id, "\n".join(lines))
    except Exception as e:
        await _send(token, chat_id, f"❌ Upload failed: {_esc(str(e)[:200])}")


async def _cmd_update(token: str, chat_id: str, args: str) -> None:
    """Handle /update <story|all> [platforms] — push updates to posted submissions.

    /update Example_Story       — update one story on all platforms
    /update Example_Story ib    — update one story on specific platform
    /update all                — update all stories with changed files
    /update all fa             — update all changed stories on FA only
    """
    parts = args.strip().split()
    if not parts:
        await _send(token, chat_id,
            "<b>Usage:</b>\n"
            "/update &lt;story&gt; [platforms] — update one story\n"
            "/update all [platforms] — update all changed stories")
        return

    story_name = parts[0]
    platforms = parts[1].split(",") if len(parts) > 1 else None

    from posting import manager

    if story_name.lower() == "all":
        await _send(token, chat_id, "🔄 Checking for changed stories...")
        try:
            results = await manager.update_all_changed(platforms)

            if results and results[0].get("status") == "no_changes":
                await _send(token, chat_id, "✅ All publications are up to date — no changes detected.")
                return

            lines = []
            for r in results:
                if "error" in r and "success" not in r:
                    lines.append(f"⚠️ {_esc(str(r['error']))}")
                    continue
                emoji = manager.PLATFORM_EMOJIS.get(r.get("platform", ""), "📦")
                story = _esc(r.get("story_name", "?").replace("_", " "))
                ch_label = f'Ch{r["chapter_index"]}' if r.get("chapter_index") else "Full"
                if r.get("success"):
                    lines.append(f'{emoji} {story} {ch_label} — updated ✅')
                elif r.get("queued_desktop"):
                    lines.append(f'{emoji} {story} {ch_label} — queued for desktop 🖥')
                else:
                    lines.append(f'{emoji} {story} {ch_label} — failed ❌ {_esc(r.get("error", "")[:60])}')

            successes = sum(1 for r in results if r.get("success"))
            queued = sum(1 for r in results if r.get("queued_desktop"))
            summary = f"✅ {successes}/{len(results)} updated"
            if queued:
                summary += f", {queued} queued for desktop"
            lines.append(f"\n{summary}")
            await _send(token, chat_id, "\n".join(lines))
        except Exception as e:
            await _send(token, chat_id, f"❌ Batch update failed: {_esc(str(e)[:200])}")
        return

    await _send(token, chat_id, f"🔄 Updating <b>{_esc(story_name.replace('_', ' '))}</b>...")

    try:
        results = await manager.update_story(story_name, platforms)

        lines = []
        for r in results:
            if "error" in r and "success" not in r:
                lines.append(f"⚠️ {_esc(str(r['error']))}")
                continue
            emoji = manager.PLATFORM_EMOJIS.get(r["platform"], "📦")
            ch_label = f'Ch{r["chapter_index"]}' if r.get("chapter_index") else "Full"
            if r["success"]:
                lines.append(f'{emoji} {r["platform"].upper()} {ch_label} — updated ✅')
            elif r.get("queued_desktop"):
                lines.append(f'{emoji} {r["platform"].upper()} {ch_label} — queued for desktop 🖥')
            else:
                lines.append(f'{emoji} {r["platform"].upper()} {ch_label} — failed ❌ {_esc(r.get("error", "")[:80])}')

        await _send(token, chat_id, "\n".join(lines) if lines else "No publications found to update.")
    except Exception as e:
        await _send(token, chat_id, f"❌ Update failed: {_esc(str(e)[:200])}")


async def _cmd_posted(token: str, chat_id: str, args: str) -> None:
    """Handle /posted [story] — show publication registry."""
    story_name = args.strip() or None

    conn = get_connection()
    try:
        from database import posting_queries
        pubs = posting_queries.get_publications(conn, story_name=story_name, status="posted")

        if not pubs:
            await _send(token, chat_id, "No publications found." + (" Try /posted without a story name." if story_name else ""))
            return

        from posting.manager import PLATFORM_EMOJIS
        lines = ["<b>📋 Publications</b>", ""]
        current_story = ""
        for p in pubs:
            if p["story_name"] != current_story:
                current_story = p["story_name"]
                lines.append(f"<b>{_esc(current_story.replace('_', ' '))}</b>")
            emoji = PLATFORM_EMOJIS.get(p["platform"], "📦")
            ch = f'Ch{p["chapter_index"]}' if p["chapter_index"] > 0 else "Full"
            ext = _esc(p["external_id"][:15]) if p["external_id"] else "?"
            updates = f" ({p['update_count']} updates)" if p["update_count"] > 0 else ""
            lines.append(f"  {emoji} {p['platform'].upper()} {ch} #{ext}{updates}")

        await _send(token, chat_id, "\n".join(lines))
    finally:
        conn.close()


async def _cmd_stories(token: str, chat_id: str, args: str) -> None:
    """Handle /stories — list available stories in archive."""
    try:
        from posting import story_reader
        stories = story_reader.list_stories()
        if not stories:
            await _send(token, chat_id, "No stories found in archive.")
            return
        lines = ["<b>📚 Available Stories</b>", ""]
        for s in stories:
            tags = "🏷" if s["has_tags"] else "  "
            manifest = "📖" if s["has_manifest"] else "  "
            lines.append(f"  {manifest}{tags} {s['name'].replace('_', ' ')}")
        lines.append("\n📖=chapters  🏷=tags ready")
        await _send(token, chat_id, "\n".join(lines))
    except Exception as e:
        await _send(token, chat_id, f"❌ Error: {_esc(str(e)[:200])}")


async def _cmd_sync(token: str, chat_id: str, args: str) -> None:
    """Handle /sync [story] — check archive status or push to server."""
    story_filter = args.strip() or None

    if story_filter and story_filter.lower() == "push":
        # Push local archive to server
        await _send(token, chat_id, "🔄 Syncing stories to server...")
        try:
            from posting import story_reader
            import io, tarfile, httpx as _httpx

            archive_path = story_reader.get_archive_path()
            if not archive_path.is_dir():
                await _send(token, chat_id, f"❌ Archive not found at {archive_path}")
                return

            settings = config.get_settings()
            server_url = settings.get("posting_server_url", "")
            api_key = settings.get("posting_server_api_key", "")
            if not server_url:
                await _send(token, chat_id, "❌ No server URL configured (posting_server_url)")
                return

            # Tar + push
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                for entry in sorted(archive_path.iterdir()):
                    if entry.is_dir() and not entry.name.startswith("."):
                        tar.add(str(entry), arcname=entry.name)
            buf.seek(0)

            headers = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            async with _httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{server_url.rstrip('/')}/api/posting/sync/upload",
                    files={"file": ("archive.tar.gz", buf.getvalue(), "application/gzip")},
                    headers=headers,
                )
            if resp.status_code == 200:
                data = resp.json()
                await _send(token, chat_id, f"✅ Synced {data.get('stories', '?')} stories ({data.get('bytes_received', 0) // 1024}KB)")
            else:
                await _send(token, chat_id, f"❌ Sync failed: {resp.status_code} — {_esc(resp.text[:150])}")
        except Exception as e:
            await _send(token, chat_id, f"❌ Sync failed: {_esc(str(e)[:200])}")
        return

    # Default: show sync status
    try:
        from posting import story_reader
        from routes.posting_api import _hash_story

        archive_path = story_reader.get_archive_path()
        if not archive_path.is_dir():
            await _send(token, chat_id, f"❌ Archive not found at {archive_path}")
            return

        conn = get_connection()
        try:
            from database import posting_queries
            pubs = posting_queries.get_publications(conn, status="posted")
        finally:
            conn.close()

        pub_stories = set(p["story_name"] for p in pubs)
        lines = ["<b>📂 Archive Status</b>", ""]

        for entry in sorted(archive_path.iterdir()):
            if not entry.is_dir() or entry.name.startswith(".") or entry.name == "Reference_Guides":
                continue
            name = entry.name
            published = name in pub_stories
            status = "📤" if published else "📝"
            lines.append(f"  {status} {_esc(name.replace('_', ' '))}")

        lines.append(f"\n📤=published  📝=not published")
        lines.append(f"\n<b>Usage:</b> /sync push — push archive to server")
        await _send(token, chat_id, "\n".join(lines))
    except Exception as e:
        await _send(token, chat_id, f"❌ Error: {_esc(str(e)[:200])}")


async def _cmd_claim(token: str, chat_id: str, args: str) -> None:
    """Handle /claim [platforms] — claim existing submissions into publications."""
    platforms = args.strip().split(",") if args.strip() else None

    await _send(token, chat_id, "🔗 Scanning platform submissions and matching to archive stories...")

    try:
        from posting.sync import claim_existing_submissions
        from posting.manager import PLATFORM_EMOJIS

        results = claim_existing_submissions(platforms=platforms)

        claimed = [r for r in results if r["status"] == "claimed"]
        already = [r for r in results if r["status"] == "already_claimed"]
        unmatched = [r for r in results if r["status"] == "unmatched"]

        lines = [f"<b>🔗 Claim Results</b>", ""]

        if claimed:
            lines.append(f"<b>Claimed ({len(claimed)}):</b>")
            for r in claimed:
                emoji = PLATFORM_EMOJIS.get(r["platform"], "📦")
                ch = f" Ch{r['chapter_index']}" if r.get("chapter_index") else ""
                story = _esc((r.get("story_name") or "").replace("_", " "))
                lines.append(f"  {emoji} {story}{ch} ← {_esc(r['title'][:40])}")
            lines.append("")

        if already:
            lines.append(f"Already claimed: {len(already)}")
        if unmatched:
            lines.append(f"Unmatched: {len(unmatched)}")
            for r in unmatched[:5]:
                emoji = PLATFORM_EMOJIS.get(r["platform"], "📦")
                lines.append(f"  {emoji} ? ← {_esc(r['title'][:40])}")
            if len(unmatched) > 5:
                lines.append(f"  ...and {len(unmatched) - 5} more")

        lines.append(f"\n✅ {len(claimed)} new, {len(already)} existing, {len(unmatched)} unmatched")
        await _send(token, chat_id, "\n".join(lines))
    except Exception as e:
        await _send(token, chat_id, f"❌ Claim failed: {_esc(str(e)[:200])}")


async def _cmd_changes(token: str, chat_id: str, args: str) -> None:
    """Handle /changes — show which stories have changed since last update."""
    try:
        from posting.sync import get_sync_status_summary
        from posting.manager import PLATFORM_EMOJIS

        summaries = get_sync_status_summary()
        if not summaries:
            await _send(token, chat_id, "No publications found. Run /claim first.")
            return

        changed = [s for s in summaries if s["changed"]]
        up_to_date = [s for s in summaries if not s["changed"]]

        lines = ["<b>📊 Change Detection</b>", ""]

        if changed:
            lines.append(f"<b>Changed ({len(changed)}):</b>")
            for s in changed:
                plats = " ".join(PLATFORM_EMOJIS.get(p, p) for p in s["changed_platforms"])
                lines.append(f"  {_esc(s['name'].replace('_', ' '))} — {s['changed_count']} items on {plats}")
            lines.append("")

        lines.append(f"Up to date: {len(up_to_date)} stories")
        if changed:
            lines.append(f"\nRun /update all to push all changes")

        await _send(token, chat_id, "\n".join(lines))
    except Exception as e:
        await _send(token, chat_id, f"❌ Error: {_esc(str(e)[:200])}")


# ── Command dispatcher ───────────────────────────────────────

COMMANDS = {
    "/help": _cmd_help,
    "/start": _cmd_help,
    "/stats": _cmd_stats,
    "/top": _cmd_top,
    "/trending": _cmd_trending,
    "/digest": _cmd_digest,
    "/fans": _cmd_fans,
    "/poll": _cmd_poll,
    "/pause": _cmd_pause,
    "/resume": _cmd_resume,
    "/status": _cmd_status,
    "/interval": _cmd_interval,
    "/notify": _cmd_notify,
    "/upload": _cmd_upload,
    "/update": _cmd_update,
    "/posted": _cmd_posted,
    "/stories": _cmd_stories,
    "/sync": _cmd_sync,
    "/claim": _cmd_claim,
    "/changes": _cmd_changes,
}


async def _handle_message(token: str, chat_id: str, text: str) -> None:
    """Parse and dispatch a single message."""
    text = text.strip()
    if not text.startswith("/"):
        return

    # Split "/command args" — handle @botname suffix (e.g. /stats@PawPollerBot)
    parts = text.split(None, 1)
    cmd = parts[0].split("@")[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    handler = COMMANDS.get(cmd)
    if handler:
        try:
            await handler(token, chat_id, args)
        except Exception as e:
            logger.error("Bot command %s failed: %s", cmd, e)
            await _send(token, chat_id, f"Command failed: {_esc(str(e)[:200])}")
    else:
        await _send(token, chat_id, f"Unknown command: {_esc(cmd)}\nType /help for available commands.")


# ── Main bot loop ────────────────────────────────────────────

async def run_bot() -> None:
    """Main bot loop — polls for updates and dispatches commands."""
    global _last_update_id

    settings = config.get_settings()
    if not settings.get("telegram_enabled", False):
        logger.info("Telegram bot disabled — not starting command listener")
        return

    token = settings.get("telegram_bot_token")
    chat_id = settings.get("telegram_chat_id")
    if not token or not chat_id:
        logger.info("Telegram bot not configured — skipping command listener")
        return

    logger.info("Telegram bot command listener started")

    # Flush old updates on startup so we don't process stale commands
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": -1},
            )
            data = resp.json()
            results = data.get("result", [])
            if results:
                _last_update_id = results[-1]["update_id"]
                logger.info("Flushed %d old Telegram updates", len(results))
    except Exception as e:
        logger.warning("Failed to flush old updates: %s", e)

    while True:
        # Re-read settings each loop in case Telegram gets disconnected
        settings = config.get_settings()
        if not settings.get("telegram_enabled", False):
            await asyncio.sleep(30)
            continue

        token = settings.get("telegram_bot_token", "")
        if not token:
            await asyncio.sleep(30)
            continue

        # If another bot instance is contending for this token, back off
        # instead of hammering the API.  _CONFLICT_BACKOFF is set by
        # _poll_updates() on 409 responses and reset on success.
        if _CONFLICT_BACKOFF > 0:
            await asyncio.sleep(_CONFLICT_BACKOFF)

        updates = await _poll_updates(token)
        for update in updates:
            msg = update.get("message", {})
            text = msg.get("text", "")
            msg_chat_id = str(msg.get("chat", {}).get("id", ""))

            # Only respond to the configured chat (security)
            if msg_chat_id != str(chat_id):
                continue

            if text:
                await _handle_message(token, chat_id, text)
