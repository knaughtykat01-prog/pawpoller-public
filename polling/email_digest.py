"""Weekly email digest — a templated, deterministic recap of the week's numbers.

This is the email channel for the weekly recap. It is NOT AI: every line is a
number the pollers already collected (7-day stat deltas, follower growth, new
watchers) rendered through a fixed HTML template. No model, nothing leaves the
box except the finished email to your own SMTP server.

Layers (mirrors polling/notifications.py's shape):
  1. Data      — ``build_weekly_digest_data`` assembles a plain dict of the
                 week's deltas/totals/gainers/followers, reusing the same query
                 helpers the Telegram weekly digest uses (polling/telegram.py).
  2. Render    — ``render_weekly_digest_html`` / ``render_weekly_digest_text``
                 turn that dict into an email-safe HTML part + a plaintext
                 fallback. Inline styles + table layout for client compat.
  3. Deliver   — ``send_email`` (smtplib, STARTTLS or SSL).
  4. Orchestrate — ``send_weekly_email_digest`` gates on the enabled flag +
                 config, builds, renders, sends, and stamps the last-sent time.

The scheduler in server.py calls ``send_weekly_email_digest`` on the same poll
cycle as the Telegram weekly digest; the dashboard exposes preview + test-send.
"""
from __future__ import annotations

import html
import logging
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - zoneinfo is stdlib on 3.9+
    ZoneInfo = None  # type: ignore

import config
from database.db import get_connection

logger = logging.getLogger(__name__)

# Default SMTP host so the settings form pre-fills something sensible. Gmail is
# the most common; the user overrides host/port for anything else.
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 587


# ── Recipients ────────────────────────────────────────────────────────

def parse_recipients(settings: dict) -> list[str]:
    """Split ``email_digest_recipients`` (comma / whitespace / newline separated)
    into a de-duplicated list of addresses, order preserved."""
    raw = settings.get("email_digest_recipients") or ""
    parts = raw.replace(",", " ").replace(";", " ").split()
    seen: dict[str, None] = {}
    for p in parts:
        p = p.strip()
        if p and "@" in p and p not in seen:
            seen[p] = None
    return list(seen.keys())


# ── Data ──────────────────────────────────────────────────────────────

def build_weekly_digest_data(conn, days: int = 7, tz_name: str = "UTC") -> dict:
    """Assemble the week's numbers into a plain dict (no rendering, no IO beyond
    the DB). Reuses polling/telegram.py's per-platform delta + total + watcher
    helpers so the email and the Telegram digest never drift apart."""
    from polling import telegram as tg
    from database import followers as _foll

    now = datetime.now(timezone.utc)
    tz = timezone.utc
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
    local_now = now.astimezone(tz)
    week_start = (local_now - timedelta(days=days)).strftime("%b %d")
    week_end = local_now.strftime("%b %d, %Y")
    hours = days * 24

    platforms: list[dict] = []
    combined = {"views_delta": 0, "faves_delta": 0, "comments_delta": 0,
                "views_total": 0, "faves_total": 0, "comments_total": 0, "subs": 0}
    all_gainers: list[dict] = []

    for code, (snap, sub) in tg.PLATFORM_TABLES.items():
        try:
            totals = tg._get_platform_totals(conn, sub, code)
        except Exception:
            continue
        if not totals or (totals.get("subs") or 0) == 0:
            continue   # nothing published on this platform — skip the row
        try:
            d = tg._get_digest_deltas(conn, snap, sub, code, hours=hours)
        except Exception:
            d = {"views_delta": 0, "faves_delta": 0, "comments_delta": 0, "top_gainers": []}

        platforms.append({
            "code": code,
            "name": tg.PLATFORM_NAME.get(code, code.upper()),
            "emoji": tg.PLATFORM_EMOJI.get(code, ""),
            "subs": totals.get("subs", 0) or 0,
            "views_total": totals.get("views", 0) or 0,
            "faves_total": totals.get("faves", 0) or 0,
            "comments_total": totals.get("comments", 0) or 0,
            "views_delta": d["views_delta"], "faves_delta": d["faves_delta"],
            "comments_delta": d["comments_delta"],
        })
        combined["subs"] += totals.get("subs", 0) or 0
        combined["views_total"] += totals.get("views", 0) or 0
        combined["faves_total"] += totals.get("faves", 0) or 0
        combined["comments_total"] += totals.get("comments", 0) or 0
        combined["views_delta"] += d["views_delta"]
        combined["faves_delta"] += d["faves_delta"]
        combined["comments_delta"] += d["comments_delta"]
        for g in d.get("top_gainers", []):
            all_gainers.append({
                "title": g.get("title") or "(untitled)",
                "views": g.get("views", 0), "faves": g.get("faves", 0),
                "comments": g.get("comments", 0),
                "platform": code, "emoji": tg.PLATFORM_EMOJI.get(code, ""),
                "platform_name": tg.PLATFORM_NAME.get(code, code.upper()),
            })

    platforms.sort(key=lambda p: (p["views_delta"], p["faves_delta"]), reverse=True)
    all_gainers.sort(key=lambda g: (g["views"], g["faves"]), reverse=True)

    # Follower growth over the window (young tracking → often 0/None; that's fine).
    followers: list[dict] = []
    try:
        prows = conn.execute(
            "SELECT DISTINCT platform FROM accounts "
            "WHERE follower_count IS NOT NULL AND follower_count > 0").fetchall()
    except Exception:
        prows = []
    since = (now - timedelta(days=days)).isoformat()
    for r in prows:
        plat = r["platform"]
        latest = _foll.platform_latest(conn, plat) or {}
        series = _foll.platform_series(conn, plat, since=since)
        delta = None
        if len(series) >= 2:
            delta = (series[-1].get("followers") or 0) - (series[0].get("followers") or 0)
        followers.append({
            "platform": plat, "name": tg.PLATFORM_NAME.get(plat, plat.upper()),
            "emoji": tg.PLATFORM_EMOJI.get(plat, ""),
            "count": latest.get("followers"), "delta": delta})
    followers.sort(key=lambda f: -(f["count"] or 0))

    # New watchers this week (only ib/fa/sf track them).
    watchers: list[dict] = []
    for code in ("ib", "fa", "sf"):
        w = tg._get_watcher_stats(conn, code, days=days)
        if w:
            watchers.append({
                "platform": code, "name": tg.PLATFORM_NAME.get(code, code.upper()),
                "emoji": tg.PLATFORM_EMOJI.get(code, ""),
                "total": w["total"], "new": w["new"]})

    # Repost Radar tie-in: 3 old-but-strong pieces to consider resurfacing.
    repost_picks: list[dict] = []
    try:
        from database import analytics_queries as aq
        for c in aq.get_repost_candidates(conn, min_age_days=180, limit=3):
            repost_picks.append({
                "name": c["name"], "title": c["name"].replace("_", " "),
                "age_days": c["age_days"], "views": c["views"], "faves": c["faves"]})
    except Exception:
        pass

    return {
        "week_start": week_start, "week_end": week_end, "days": days,
        "generated_at": now.isoformat(),
        "platforms": platforms, "combined": combined,
        "top_gainers": all_gainers[:5], "followers": followers,
        "watchers": watchers, "repost_picks": repost_picks,
        "has_activity": (combined["views_delta"] + combined["faves_delta"]
                         + combined["comments_delta"]) > 0,
    }


# ── Render ────────────────────────────────────────────────────────────

def _n(v) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "0"


def _delta(v) -> str:
    """'+1,234' / '0' — signed delta for display."""
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return "+0"
    return f"+{iv:,}" if iv >= 0 else f"{iv:,}"


def _age_label(days) -> str:
    if days is None:
        return ""
    if days >= 365:
        y = round(days / 365, 1)
        return f"{y:g} yr" + ("s" if y != 1 else "")
    if days >= 60:
        return f"{round(days / 30)} mo"
    return f"{days} d"


def render_weekly_digest_html(data: dict) -> str:
    """Email-safe HTML: 600px table container, inline styles, no external assets."""
    e = html.escape
    d = data
    C = d["combined"]

    def stat_cell(label, total, delta):
        return (
            f'<td align="center" style="padding:10px 6px;">'
            f'<div style="font-size:24px;font-weight:700;color:#1a1a2e;">{_n(total)}</div>'
            f'<div style="font-size:13px;color:#2e9e5b;font-weight:600;">{_delta(delta)}</div>'
            f'<div style="font-size:11px;color:#8a8a9a;text-transform:uppercase;letter-spacing:.5px;">{e(label)}</div>'
            f'</td>')

    # Per-platform rows
    plat_rows = ""
    for p in d["platforms"]:
        plat_rows += (
            '<tr>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:14px;color:#1a1a2e;">{e(p["emoji"])} {e(p["name"])}</td>'
            f'<td align="right" style="padding:8px 10px;border-bottom:1px solid #eee;font-size:14px;color:#1a1a2e;">{_n(p["views_total"])} <span style="color:#2e9e5b;font-size:12px;">{_delta(p["views_delta"])}</span></td>'
            f'<td align="right" style="padding:8px 10px;border-bottom:1px solid #eee;font-size:14px;color:#1a1a2e;">{_n(p["faves_total"])} <span style="color:#2e9e5b;font-size:12px;">{_delta(p["faves_delta"])}</span></td>'
            f'<td align="right" style="padding:8px 10px;border-bottom:1px solid #eee;font-size:14px;color:#1a1a2e;">{_n(p["comments_total"])} <span style="color:#2e9e5b;font-size:12px;">{_delta(p["comments_delta"])}</span></td>'
            '</tr>')
    if not plat_rows:
        plat_rows = '<tr><td colspan="4" style="padding:12px;color:#8a8a9a;font-size:14px;">No published works yet.</td></tr>'

    # Top gainers
    gainer_rows = ""
    for g in d["top_gainers"]:
        gainer_rows += (
            '<tr>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #f0f0f0;font-size:14px;color:#1a1a2e;">{e(g["emoji"])} {e(g["title"])}</td>'
            f'<td align="right" style="padding:6px 10px;border-bottom:1px solid #f0f0f0;font-size:13px;color:#2e9e5b;white-space:nowrap;">{_delta(g["views"])} views · {_delta(g["faves"])} faves</td>'
            '</tr>')
    gainers_block = (
        '<h2 style="font-size:16px;color:#1a1a2e;margin:26px 0 8px;">🔥 Top gainers this week</h2>'
        f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">{gainer_rows}</table>'
        if gainer_rows else "")

    # Followers
    foll_rows = ""
    for f in d["followers"]:
        cnt = _n(f["count"]) if f["count"] is not None else "—"
        dl = f' <span style="color:#2e9e5b;font-size:12px;">{_delta(f["delta"])}</span>' if f["delta"] is not None else ""
        foll_rows += (
            f'<td align="center" style="padding:8px;font-size:13px;color:#1a1a2e;">'
            f'<div style="font-size:18px;">{e(f["emoji"])}</div>'
            f'<div style="font-weight:600;">{cnt}{dl}</div>'
            f'<div style="font-size:11px;color:#8a8a9a;">{e(f["name"])}</div></td>')
    followers_block = (
        '<h2 style="font-size:16px;color:#1a1a2e;margin:26px 0 8px;">👥 Following</h2>'
        f'<table width="100%" cellpadding="0" cellspacing="0"><tr>{foll_rows}</tr></table>'
        if foll_rows else "")

    # Watchers
    watch_bits = " · ".join(
        f'{e(w["emoji"])} {e(w["name"])} <b>{_delta(w["new"])}</b> new ({_n(w["total"])} total)'
        for w in d["watchers"])
    watchers_block = (
        f'<p style="font-size:14px;color:#4a4a5a;margin:16px 0;">📈 New watchers: {watch_bits}</p>'
        if watch_bits else "")

    # Repost picks
    repost_rows = ""
    for r in d["repost_picks"]:
        repost_rows += (
            '<tr>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #f0f0f0;font-size:14px;color:#1a1a2e;">{e(r["title"])}</td>'
            f'<td align="right" style="padding:6px 10px;border-bottom:1px solid #f0f0f0;font-size:13px;color:#8a8a9a;white-space:nowrap;">posted ~{_age_label(r["age_days"])} · {_n(r["views"])} views</td>'
            '</tr>')
    repost_block = (
        '<h2 style="font-size:16px;color:#1a1a2e;margin:26px 0 8px;">🔄 Worth resurfacing</h2>'
        '<p style="font-size:13px;color:#8a8a9a;margin:0 0 8px;">Older pieces that still perform — good candidates to repost to your feed.</p>'
        f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">{repost_rows}</table>'
        if repost_rows else "")

    return f"""\
<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f7;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f7;padding:24px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
  <tr><td style="background:#1a1a2e;padding:24px 28px;">
    <div style="font-size:20px;font-weight:700;color:#ffffff;">🐾 PawPoller Weekly Digest</div>
    <div style="font-size:14px;color:#a9a9c9;margin-top:4px;">Week of {e(d["week_start"])} — {e(d["week_end"])}</div>
  </td></tr>
  <tr><td style="padding:20px 28px 4px;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f8fb;border-radius:10px;">
      <tr>{stat_cell("Views", C["views_total"], C["views_delta"])}{stat_cell("Faves", C["faves_total"], C["faves_delta"])}{stat_cell("Comments", C["comments_total"], C["comments_delta"])}</tr>
    </table>
    <p style="font-size:13px;color:#8a8a9a;text-align:center;margin:10px 0 0;">across {_n(C["subs"])} works · last {d["days"]} days</p>
  </td></tr>
  <tr><td style="padding:12px 28px 0;">
    <h2 style="font-size:16px;color:#1a1a2e;margin:16px 0 8px;">By platform</h2>
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
      <tr style="background:#f8f8fb;">
        <th align="left" style="padding:8px 10px;font-size:12px;color:#8a8a9a;text-transform:uppercase;">Platform</th>
        <th align="right" style="padding:8px 10px;font-size:12px;color:#8a8a9a;text-transform:uppercase;">Views</th>
        <th align="right" style="padding:8px 10px;font-size:12px;color:#8a8a9a;text-transform:uppercase;">Faves</th>
        <th align="right" style="padding:8px 10px;font-size:12px;color:#8a8a9a;text-transform:uppercase;">Comments</th>
      </tr>
      {plat_rows}
    </table>
    {gainers_block}
    {followers_block}
    {watchers_block}
    {repost_block}
  </td></tr>
  <tr><td style="padding:24px 28px;border-top:1px solid #eee;">
    <p style="font-size:12px;color:#a9a9b9;margin:0;">Sent by your PawPoller server — all figures are your own poll data. No AI, nothing shared. Turn this off any time in Settings → Weekly digest.</p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def render_weekly_digest_text(data: dict) -> str:
    """Plaintext fallback part."""
    d = data
    C = d["combined"]
    lines = [
        "PawPoller Weekly Digest",
        f"Week of {d['week_start']} - {d['week_end']}",
        "",
        f"Views:    {_n(C['views_total'])} ({_delta(C['views_delta'])})",
        f"Faves:    {_n(C['faves_total'])} ({_delta(C['faves_delta'])})",
        f"Comments: {_n(C['comments_total'])} ({_delta(C['comments_delta'])})",
        f"across {_n(C['subs'])} works, last {d['days']} days",
        "",
        "By platform:",
    ]
    for p in d["platforms"]:
        lines.append(f"  {p['name']}: {_n(p['views_total'])} views ({_delta(p['views_delta'])}), "
                     f"{_n(p['faves_total'])} faves ({_delta(p['faves_delta'])})")
    if d["top_gainers"]:
        lines += ["", "Top gainers this week:"]
        for g in d["top_gainers"]:
            lines.append(f"  {g['title']} — {_delta(g['views'])} views, {_delta(g['faves'])} faves")
    if d["followers"]:
        lines += ["", "Following:"]
        for f in d["followers"]:
            cnt = _n(f["count"]) if f["count"] is not None else "-"
            dl = f" ({_delta(f['delta'])})" if f["delta"] is not None else ""
            lines.append(f"  {f['name']}: {cnt}{dl}")
    if d["repost_picks"]:
        lines += ["", "Worth resurfacing:"]
        for r in d["repost_picks"]:
            lines.append(f"  {r['title']} — posted ~{_age_label(r['age_days'])}, {_n(r['views'])} views")
    lines += ["", "Sent by your PawPoller server. No AI. Turn off in Settings > Weekly digest."]
    return "\n".join(lines)


# ── Deliver ───────────────────────────────────────────────────────────

def send_email(settings: dict, subject: str, html_body: str,
               text_body: str | None = None) -> None:
    """Send a multipart email via the configured SMTP server. Raises on failure
    (bad config, auth reject, connection error) so callers can surface it."""
    host = settings.get("smtp_host") or DEFAULT_SMTP_HOST
    port = int(settings.get("smtp_port") or DEFAULT_SMTP_PORT)
    user = settings.get("smtp_username") or ""
    password = settings.get("smtp_password") or ""
    sender = settings.get("smtp_from") or user
    recipients = parse_recipients(settings)

    if not recipients:
        raise ValueError("No digest recipients configured")
    if not (host and user and password):
        raise ValueError("SMTP is not fully configured (need host, username and password)")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, password)
            s.sendmail(sender, recipients, msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            if settings.get("smtp_use_tls", True):
                s.starttls(context=ctx)
                s.ehlo()
            s.login(user, password)
            s.sendmail(sender, recipients, msg.as_string())


# ── Orchestrate ───────────────────────────────────────────────────────

def send_weekly_email_digest(force: bool = False) -> dict:
    """Build + send the weekly digest email. ``force`` (test-send) bypasses the
    enabled gate and does NOT reset the weekly clock. Returns a status dict;
    raises only on hard delivery errors (callers catch and report)."""
    settings = config.get_settings()
    if not force and not settings.get("email_digest_enabled", False):
        return {"sent": False, "reason": "disabled"}
    recipients = parse_recipients(settings)
    if not recipients:
        return {"sent": False, "reason": "no recipients"}

    days = int(settings.get("email_digest_interval_days", 7) or 7)
    conn = get_connection()
    try:
        data = build_weekly_digest_data(
            conn, days=days, tz_name=settings.get("display_timezone", "UTC"))
    finally:
        conn.close()

    subject = f"PawPoller Weekly Digest — {data['week_end']}"
    html_body = render_weekly_digest_html(data)
    text_body = render_weekly_digest_text(data)
    send_email(settings, subject, html_body, text_body)

    # Only the scheduled path advances the weekly clock; a manual test doesn't.
    if not force:
        config.save_settings(
            {"last_email_digest_sent_at": datetime.now(timezone.utc).isoformat()})
    logger.info("Weekly email digest sent to %d recipient(s)%s",
                len(recipients), " (test)" if force else "")
    return {"sent": True, "recipients": recipients, "subject": subject}
