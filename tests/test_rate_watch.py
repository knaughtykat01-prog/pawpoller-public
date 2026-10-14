"""4.74.0 (spec 039): rate watch sorts poll-log rows by fixed patterns and counts them."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polling.rate_watch import classify, pressure

# Real messages from the VM's poll logs (2026-10-14), research R1.
REAL = [
    ("error", "Client error '429 Too Many Requests' for url 'https://example.com/x'", "limited"),
    ("partial", "Rate-limited (429) — some tweets may be missing", "limited"),
    ("partial", "Rate-limited (429) — tweets may be incomplete", "limited"),
    ("error", "FurryNetwork is behind a Cloudflare bot check right now, so PawPoller can't reach it at all", "challenged"),
    ("error", "AO3 refused the sign-in (HTTP 429)", "limited"),
    ("error", "AO3 refused the sign-in (HTTP 525) — AO3-side, not your credentials.", "blocked"),
    ("error", "AO3 login is rate-limited (HTTP 429 “Retry later”) — AO3-side, not your credentials. Wait 5–60 min", "limited"),
    ("error", "AO3 is temporarily blocking automated login (“Shields are up”) — this is AO3-side", "blocked"),
    ("error", "Client error '403 Forbidden' for url 'https://example.com/x'", "blocked"),
    # Not refusals: the owner's credentials, or the site/network being down.
    ("error", "FA cookies expired — update cookie_a and cookie_b in Settings", None),
    ("error", "Login failed: {'error_code': 0, 'error_message': 'Invalid login. Username and password incorrect'}", None),
    ("error", "DA cookie validation failed -- check cookies", None),
    ("error", "FAExport upstream error (502) — third-party proxy could not fetch", None),
    ("error", "Server error '500 Internal Server Error' for url 'https://example.com/x'", None),
    ("error", "All connection attempts failed", None),
    ("error", "FurryNetwork auth failed: HTTP 403", None),
    ("error", "AO3 login failed -- check credentials", None),
    ("success", None, "clear"),
    ("running", None, None),
]


@pytest.mark.parametrize("status,msg,want", REAL)
def test_classify_real_messages(status, msg, want):
    assert classify(status, msg) == want


def _log(conn, table, status, msg, at):
    conn.execute(f"INSERT INTO {table}(status, error_message, started_at) VALUES(?,?,?)",
                 (status, msg, at.strftime("%Y-%m-%d %H:%M:%S")))


def test_pressure_counts_windows_streak_and_skips():
    from database.db import get_connection
    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    conn = get_connection()
    try:
        for t in ("ao3_poll_log", "fa_poll_log", "turn_skips"):
            conn.execute(f"DELETE FROM {t}")
        lim = "Client error '429 Too Many Requests' for url 'x'"
        _log(conn, "ao3_poll_log", "success", None, now - timedelta(days=3))       # 7 d only
        _log(conn, "ao3_poll_log", "error", lim, now - timedelta(days=2))          # 7 d only
        _log(conn, "ao3_poll_log", "error", "AO3 login failed -- check credentials", now - timedelta(hours=9))  # not counted
        _log(conn, "ao3_poll_log", "success", None, now - timedelta(hours=8))
        _log(conn, "ao3_poll_log", "error", lim, now - timedelta(hours=4))
        _log(conn, "ao3_poll_log", "error", "Cloudflare bot check", now - timedelta(hours=1))
        _log(conn, "ao3_poll_log", "success", None, now - timedelta(days=9))       # too old
        _log(conn, "fa_poll_log", "error", "FA cookies expired", now - timedelta(hours=1))  # nothing counted
        conn.execute("INSERT INTO turn_skips(site, at) VALUES('ao3', ?), ('ao3', ?)",
                     ((now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")))
        conn.commit()
        got = {p["site"]: p for p in pressure(conn, ("ao3", "fa", "ws"), now)}
    finally:
        conn.close()
    assert set(got) == {"ao3"}                     # fa: only a login error; ws: nothing
    a = got["ao3"]
    assert (a["checks_24h"], a["away_24h"], a["checks_7d"], a["away_7d"]) == (3, 2, 5, 3)
    assert a["away_in_a_row"] == 2
    assert a["kinds_7d"] == {"limited": 2, "blocked": 0, "challenged": 1}
    assert (a["skips_24h"], a["skips_7d"]) == (1, 2)


def test_record_skip_keeps_seven_days():
    from database.db import get_connection
    from polling import last_check
    conn = get_connection()
    conn.execute("DELETE FROM turn_skips")
    conn.execute("INSERT INTO turn_skips(site, at) VALUES('fa', '2000-01-01T00:00:00Z')")
    conn.commit()
    conn.close()
    last_check.record_skip("fa", "waited 10 minutes for its turn")
    conn = get_connection()
    rows = conn.execute("SELECT at FROM turn_skips WHERE site='fa'").fetchall()
    conn.close()
    assert len(rows) == 1 and rows[0][0] > "2026"
