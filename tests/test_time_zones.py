"""Time zones (spec 016): one parser/formatter in the browser, the saved zone made real."""
import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UTILS = ROOT / "frontend" / "js" / "utils.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")


def _node(body: str, browser_tz: str = "Australia/Sydney"):
    """Run against the real utils.js with the 'browser' in browser_tz — the output must
    follow Utils.time's zone, not the machine's."""
    prelude = (f"const fs = require('fs'); eval(fs.readFileSync({json.dumps(str(UTILS))}, 'utf8')"
               " + ';globalThis.Utils = Utils;');\nconst T = Utils.time;\n")
    r = subprocess.run(["node", "-e", prelude + textwrap.dedent(body)], capture_output=True, text=True,
                       timeout=60, encoding="utf-8", env={**os.environ, "TZ": browser_tz})
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_node
def test_every_shape_the_server_sends_parses_as_the_right_instant():
    out = _node("""
        const iso = v => { const d = T.parse(v); return d ? d.toISOString() : null; };
        console.log(JSON.stringify({
            naive: iso('2026-10-01 10:00:00'),            // stored naive UTC
            naiveT: iso('2026-10-01T10:00:00'),           // used to read as browser-local
            ib: iso('2026-01-30 03:09:28.559557+00'),     // Inkbunny: used to be Invalid
            plus0000: iso('2026-06-12T23:43:59+0000'),
            e621: iso('2020-07-02T02:08:10.164-04:00'),
            z: iso('2026-02-19T01:10:53.000000Z'),
            fa: iso('August 7, 2019 11:57:56 PM'),
            faComma: iso('Aug 7, 2019, 11:57 PM'),
            junk: iso('soon'), empty: iso(''),
            dateOnly: T.parse('2026-09-26').dateOnly === true,
        }));
    """)
    assert out == {
        "naive": "2026-10-01T10:00:00.000Z", "naiveT": "2026-10-01T10:00:00.000Z",
        "ib": "2026-01-30T03:09:28.560Z", "plus0000": "2026-06-12T23:43:59.000Z",
        "e621": "2020-07-02T06:08:10.164Z", "z": "2026-02-19T01:10:53.000Z",
        "fa": "2019-08-07T23:57:56.000Z", "faComma": "2019-08-07T23:57:00.000Z",
        "junk": None, "empty": None, "dateOnly": True,
    }


@needs_node
def test_text_follows_the_saved_zone_not_the_browser():
    out = _node("""
        T.setZone('America/New_York');
        console.log(JSON.stringify({
            day: T.dayKey('2026-10-01 02:00:00'),          // 22:00 the evening before in New York
            time: T.fmt.time('2026-10-01 02:00:00'),
            aoDate: T.fmt.date('2026-09-26') + ' ' + T.dayKey('2026-09-26'),   // a date-only value never shifts
            today: T.fmt.dateTime(new Date()).startsWith('Today '),
            old: T.fmt.dateTime('2019-03-04 12:00:00'),
            bad: T.setZone('Not/AZone'), name: T.zoneName() !== '',   // unknown → the browser's zone
        }));
    """)
    assert out["day"] == "2026-09-30"
    assert out["time"].replace(" ", " ").lower() == "10:00 pm"
    assert out["aoDate"].endswith("2026-09-26") and "26 Sep" in out["aoDate"]
    assert out["bad"] is None and out["name"] and out["today"]
    assert out["old"].replace(" ", " ").lower() == "4 mar 2019, 7:00 am"   # New York, EST


@needs_node
def test_pickers_round_trip_in_the_zone_and_flag_the_skipped_hour():
    out = _node("""
        T.setZone('Australia/Sydney');
        const gap = T.toUtc('2026-10-04T02:30');          // clocks jump 02:00 → 03:00 that night
        console.log(JSON.stringify({
            summer: T.toUtcIso('2026-10-08T17:00'), winter: T.toUtcIso('2026-07-08T17:00'),
            back: T.toPicker('2026-10-08T06:00:00Z'),
            gap: gap.toISOString(), shifted: !!gap.shifted,
            plain: !!T.toUtc('2026-10-08T17:00').shifted,
            fallBack: T.toPicker(T.toUtc('2026-04-05T02:30')),   // the repeated hour still round-trips
        }), );
    """, browser_tz="America/Los_Angeles")
    assert out["summer"] == "2026-10-08T06:00:00.000Z"
    assert out["winter"] == "2026-07-08T07:00:00.000Z"
    assert out["back"] == "2026-10-08T17:00"
    assert out["gap"] == "2026-10-03T16:30:00.000Z" and out["shifted"] and not out["plain"]
    assert out["fallBack"] == "2026-04-05T02:30"


def test_no_screen_parses_or_formats_dates_on_its_own():
    """SC-003: date strings become Dates, and Dates become text, only in utils.js — every
    hand-rolled `new Date(str + 'Z')` or `toLocaleTimeString()` was a bug in waiting
    (read as browser-local, or shown in the browser's zone instead of the saved one)."""
    import re
    js = ROOT / "frontend" / "js"
    banned = re.compile(r"Date\.parse\(|toLocale(Date|Time)String\(|new Date\([^)]*(\+ ?'Z'|\.replace\()"
                        r"|new Date\((?!\)|NaN\)|Date\.now|Date\.UTC|now\.|Math\.|\d|ms\b|t\b|y\b|this\._calMonth|ay\b"
                        r"|start\.getTime|\(e\.timestamp|\(entry\.timestamp|d\.getTime|dt\.getFullYear)")
    hits = []
    for f in sorted(js.glob("*.js")):
        if f.name == "utils.js":
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if banned.search(line):
                hits.append(f"{f.name}:{n}: {line.strip()[:100]}")
    assert not hits, "\n".join(hits)


def test_setup_asks_for_the_zone_instead_of_assuming_it():
    """FR-008: every setup path has a time zone step straight after Welcome, the step saves
    what was chosen, and finishing no longer saves the browser's zone behind the person's back."""
    src = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    wiz = src[src.index("async renderSetupWizard()"):src.index("document.getElementById('setup-finish')")]
    orders = [line for line in wiz.splitlines() if "return ['welcome'" in line]
    assert len(orders) == 3 and all("'welcome', 'timezone'," in o for o in orders), orders
    step = wiz[wiz.index("if (currentStep === 'timezone') {\n                    const z"):]
    assert "API.savePreferences({ display_timezone: z })" in step[:900]
    finish = src[src.index("document.getElementById('setup-finish')"):][:1200]
    assert "display_timezone" not in finish


def test_a_drip_keeps_its_wall_time_across_daylight_saving():
    """Every 7 days at 20:00 Sydney stays 20:00 when clocks go forward (4 Oct 2026):
    10:00Z before, 09:00Z after. 24 h steps in UTC drifted an hour."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from routes.editor_api import drip_slots
    start = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)   # Sun 27 Sep, 20:00 AEST
    assert drip_slots(start, 7, 3, ZoneInfo("Australia/Sydney")) == [
        "2026-09-27 10:00:00", "2026-10-04 09:00:00", "2026-10-11 09:00:00"]


@needs_node
def test_the_drip_preview_steps_like_the_server():
    out = _node("""
        T.setZone('Australia/Sydney');
        console.log(JSON.stringify([0, 7, 14].map(d => T.toUtcIso(T.addDays('2026-09-27T20:00', d)))));
    """)
    assert out == ["2026-09-27T10:00:00.000Z", "2026-10-04T09:00:00.000Z", "2026-10-11T09:00:00.000Z"]


@needs_node
def test_a_persona_time_in_its_own_zone_lands_right_in_the_pickers_zone():
    out = _node("""
        T.setZone('Australia/Sydney');
        // 20:00 New York on 8 Oct (EDT, UTC-4) = 00:00Z 9 Oct = 11:00 Sydney (AEDT) 9 Oct.
        console.log(JSON.stringify(T.toPicker(T.toUtc('2026-10-08T20:00', 'America/New_York'))));
    """)
    assert out == "2026-10-09T11:00"


def test_insights_bucket_in_the_zone_not_by_a_fixed_offset():
    """20:00 Sydney is 10:00Z in winter and 09:00Z in summer — both land in hour 20."""
    from database.db import get_connection
    from database import analytics_queries as aq
    conn = get_connection()
    try:
        for i, when in enumerate(["2026-09-01 10:00:00", "2026-11-01 09:00:00"]):
            conn.execute("INSERT INTO submissions (submission_id, title, views, create_datetime)"
                         " VALUES (?, 'P', 100, ?)", (i + 1, when))
        conn.commit()
        ins = aq.get_posting_insights(conn, tz_offset_minutes=600, zone_name="Australia/Sydney")
    finally:
        conn.close()
    assert ins["hour"][20]["count"] == 2


def test_history_months_are_the_operators_months(monkeypatch):
    """A gain polled at 15:00Z on 30 Sep is 1 Oct in Sydney — it belongs to October."""
    import config
    from routes import api
    from database.db import get_connection
    monkeypatch.setattr(config, "get_settings", lambda: {"display_timezone": "Australia/Sydney"})
    conn = get_connection()
    try:
        conn.execute("INSERT INTO submissions (submission_id, title, views) VALUES (1, 'P', 500)")
        for when, v in (("2026-09-30 15:00:00", 100), ("2026-09-30 15:30:00", 500)):
            conn.execute("INSERT INTO snapshots (submission_id, views, favorites_count, comments_count,"
                         " polled_at) VALUES (1, ?, 0, 0, ?)", (v, when))
        conn.commit()
    finally:
        conn.close()
    out = api.get_historical_analytics(weeks=12)
    assert out["best_month"]["views"] == {"period": "2026-10", "delta": 400}


def test_a_persona_keeps_its_preferred_time_zone():
    from database.db import get_connection
    from database import personas
    conn = get_connection()
    try:
        pid = personas.create_persona(conn, "Inkwolf")
        personas.update_persona(conn, pid, preferred_post_time="20:00", preferred_post_tz="America/New_York")
        man = personas.get_manifest(conn)
        assert personas.get_persona(conn, pid)["preferred_post_tz"] == "America/New_York"
        assert man[-1]["preferred_post_tz"] == "America/New_York"
        man[-1]["preferred_post_tz"] = "Europe/London"
        personas.apply_manifest(conn, man)
        assert personas.get_persona(conn, pid)["preferred_post_tz"] == "Europe/London"
    finally:
        conn.close()


def test_stored_times_are_never_the_hosts_clock():
    """FR-005: these wrote the HOST's local time into UTC columns / names (bugs 6, 13, 16)."""
    for rel in ("posting/platforms/telegram.py", "posting/tg_linked.py", "routes/mirror_api.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "datetime.now().isoformat" not in src and 'time.strftime("%Y-%m-%d %H:%M:%S")' not in src, rel
    for rel in ("server.py", "main.py", "dashboard.py"):
        assert "logging.Formatter.converter = __import__('time').gmtime" in (ROOT / rel).read_text(encoding="utf-8")


@needs_node
def test_the_clock_reads_the_server_date_and_names_the_zone():
    out = _node("""
        T.setZone('Australia/Sydney');
        console.log(JSON.stringify({
            http: new Date(T.ms('Thu, 01 Oct 2026 13:00:00 GMT')).toISOString(),
            winter: T.abbr('2026-07-01 00:00:00'), summer: T.abbr('2026-12-01 00:00:00'),
        }));
    """, browser_tz="UTC")
    assert out["http"] == "2026-10-01T13:00:00.000Z"
    assert out["winter"] in ("AEST", "GMT+10") and out["summer"] in ("AEDT", "GMT+11")


def test_the_clock_sits_in_the_top_bar_and_opens_details():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert 'id="pp-clock"' in html and 'aria-controls="pp-clock-pop"' in html
    assert "this._initClock();" in app and "Change time zone" in app and "60000 - (Date.now() % 60000)" in app


def test_a_saved_zone_must_be_a_real_one():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.api import router
    import config
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    assert c.post("/api/settings/preferences", json={"display_timezone": "Mars/Olympus"}).status_code == 400
    assert c.post("/api/settings/preferences", json={"display_timezone": "America/New_York"}).status_code == 200
    assert config.get_settings().get("display_timezone") == "America/New_York"
    assert c.get("/api/settings/preferences").json()["display_timezone"] == "America/New_York"
