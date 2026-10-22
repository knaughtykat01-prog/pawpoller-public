"""Level rewards (LVLREWARDS, spec 051): the table, catch-up, next reward, titles,
unlocks, equip and the theme whitelist. Seeds xp_ledger directly; no network,
no personal data."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import HTTPException

import config
import rewards
import xp

R = config.REWARD_TABLE
ROOT = Path(__file__).resolve().parent.parent
_TABLES = ("xp_ledger", "xp_state", "xp_rewards", "profile_picture")


@pytest.fixture(autouse=True)
def _levels_on():
    """Gamification is off until the person says yes (22 Oct); these tests are about it being on."""
    config.save_settings({"levels_enabled": True})


@pytest.fixture
def conn(db_conn):
    def clear():
        for t in _TABLES:
            db_conn.execute(f"DELETE FROM {t}")
        # Start the sweep past any posting_log rows other tests left behind.
        top = db_conn.execute("SELECT COALESCE(MAX(log_id), 0) FROM posting_log").fetchone()[0]
        xp._set_state(db_conn, "posting_log_watermark", str(top))
        xp._set_state(db_conn, "backfilled_at", "2026-01-01 00:00:00")
        db_conn.commit()
    clear()
    s = config.get_settings()
    saved = {k: s.get(k) for k in ("rewards_equipped", "theme", "levels_enabled")}
    yield db_conn
    clear()
    config.save_settings(saved)


_n = [0]


def _give(conn, points, kind="work", key=None, site=None):
    _n[0] += 1
    xp.award(conn, kind, key or f"seed:{_n[0]}", points, at="2026-10-20 03:00:00", site=site, source="backfill")


def _to_level(conn, level):
    _give(conn, xp.level_start(level) - xp._sum(conn, "1 = 1", ()))


def _rows(conn):
    return dict(conn.execute("SELECT reward_id, source FROM xp_rewards").fetchall())


def _theme_ids():
    return [r["id"][len("theme_"):] for r in R["items"] if r["kind"] == "theme"]


# ── T006: the table itself ─────────────────────────────────────────

def test_table_shape():
    ids = [r["id"] for r in (*R["items"], *R["surprises"])]
    assert len(ids) == len(set(ids))
    assert sorted(r["level"] for r in R["items"]) == list(range(5, 51, 5))      # SC-002
    assert len(R["titles"]) == 11
    assert all(s["rule"][0] in rewards._CHECKS for s in R["surprises"])


def test_every_reward_theme_has_tokens_and_a_picker_entry():
    css = (ROOT / "frontend" / "css" / "tokens.css").read_text(encoding="utf-8")
    app = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    for t in _theme_ids():
        assert f'[data-theme="{t}"]' in css, t
        assert re.search(rf"id:\s*'{t}'[^}}]*reward:\s*\d+, rewardId: 'theme_{t}'", app), t   # FR-014


# ── T010: catch-up (US5) ───────────────────────────────────────────

def test_catch_up_once_then_live(conn):
    _to_level(conn, 18)
    xp.sync(conn)
    assert _rows(conn) == {"ring_copper": "catchup", "lv_ink": "catchup", "theme_dusk": "catchup"}
    xp.sync(conn)
    assert len(_rows(conn)) == 3
    assert rewards.reveal(conn, ["ring_copper", "lv_ink", "theme_dusk", "nope"]) == 3
    assert rewards.reveal(conn, ["ring_copper"]) == 0                              # idempotent
    _to_level(conn, 20)
    xp.sync(conn)
    assert _rows(conn)["theme_ember"] == "level"


def test_surprise_already_met_lands_in_catch_up(conn):
    for s in ("fa", "ws", "bsky", "ib", "da"):
        _give(conn, 10, kind="site", key=f"site:artwork:A:{s}", site=s)
    xp.sync(conn)
    assert _rows(conn)["s_sites5"] == "catchup"


# ── T014: next reward (US1) ────────────────────────────────────────

def test_next_reward():
    n = rewards.next_reward(18, 8000)
    assert (n["name"], n["level"], n["xp_to_go"], n["title"]) == ("Ember", 20, xp.level_start(20) - 8000, "Colourist")
    assert rewards.next_reward(50, xp.level_start(50)) is None


def test_levels_off_hides_next_and_reveals(conn):
    _to_level(conn, 18)
    xp.sync(conn)
    config.save_settings({"levels_enabled": False})
    body = xp.totals(conn)["rewards"]
    assert body["next"] is None and body["unrevealed"] == []
    assert len(body["unlocked"]) == 3                                               # still recorded (R7)


# ── T017: titles (US2) ─────────────────────────────────────────────

def test_titles():
    assert [rewards.title_for(n) for n in (1, 4, 5, 49, 50, 120)] == [
        "Newcomer", "Newcomer", "Sketcher", "Maestro", "Legend", "Legend"]


# ── T025: unlocks, equip, theme whitelist (US3) ────────────────────

def test_crossing_twenty_unlocks_ember(conn):
    _to_level(conn, 19)
    xp.sync(conn)
    assert "theme_ember" not in _rows(conn)
    _to_level(conn, 20)
    xp.sync(conn)
    assert _rows(conn)["theme_ember"] == "level"


def test_equip_needs_an_unlocked_ring(conn):
    from routes.xp_api import equip_reward
    with pytest.raises(HTTPException) as e:
        equip_reward({"kind": "ring", "id": "ring_copper"})
    assert e.value.status_code == 409
    _to_level(conn, 10)
    xp.sync(conn)
    assert equip_reward({"kind": "ring", "id": "ring_copper"})["ring"] == "ring_copper"
    with pytest.raises(HTTPException) as e:
        equip_reward({"kind": "style", "id": "ring_copper"})                        # wrong kind
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:
        equip_reward({"kind": "hat", "id": None})
    assert e.value.status_code == 400
    assert equip_reward({"kind": "ring", "id": None})["ring"] is None


def test_theme_whitelist_takes_ember_once_unlocked(conn):
    from routes.api import save_preferences
    config.save_settings({"theme": "quill"})
    save_preferences({"theme": "ember"})
    assert config.get_settings()["theme"] == "quill"
    _to_level(conn, 20)
    xp.sync(conn)
    save_preferences({"theme": "ember"})
    assert config.get_settings()["theme"] == "ember"


# ── levels.js: catch-up and the next line (US5, US1) ──────────────

import json
import shutil
import subprocess

_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run_levels(body, store):
    js = (ROOT / "frontend" / "js" / "levels.js").read_text(encoding="utf-8")
    harness = """
    const store = %s, posts = [], sounds = [];
    global.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } };
    global.window = { addEventListener() {} };
    global.document = { visibilityState: 'visible', getElementById: () => null };
    global.API = { get: async () => (%s), post: async (p, b) => { posts.push([p, b]); return {}; } };
    global.Sound = { play: n => sounds.push(n) }; window.Sound = Sound;
    global.toast = { success() {} }; window.toast = toast;
    %s
    (async () => { await window.Levels.refresh();
      console.log(JSON.stringify({ posts, sounds, next: window.Levels.nextLine() })); })();
    """ % (json.dumps(store), json.dumps(body), js)
    r = subprocess.run(["node", "-e", harness], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _body(conn, level):
    _to_level(conn, level)
    xp.sync(conn)
    return json.loads(json.dumps(xp.totals(conn)))


@_node
def test_catch_up_reveals_all_without_a_level_up(conn):
    out = _run_levels(_body(conn, 18), {"pp-xp-seen-level": "12"})
    assert out["sounds"] == []
    assert out["posts"] == [["/api/xp/rewards/reveal", {"ids": ["ring_copper", "lv_ink", "theme_dusk"]}]]
    assert out["next"] == f"Next: Ember theme at level 20 · {xp.level_start(20) - xp.level_start(18):,} XP to go"


# ── Never revoked (FR-012), retired ids ────────────────────────────

def test_retired_id_stays_on_the_shelf(conn):
    conn.execute("INSERT INTO xp_rewards (reward_id, source, unlocked_at) VALUES ('ring_old', 'level', 'x')")
    cat = {c["id"]: c for c in rewards.catalog(rewards.unlocked_ids(conn))}
    assert cat["ring_old"]["retired"] and cat["ring_old"]["kind"] == "ring"
    assert "name" not in cat["s_streak4"]


# ── T033: the shelf hides an unfound surprise's name (US4) ─────────

def test_catalog_names_a_surprise_only_once_found():
    assert "name" not in {c["id"]: c for c in rewards.catalog(set())}["s_sites5"]
    found = {c["id"]: c for c in rewards.catalog({"s_sites5"})}["s_sites5"]
    assert found["name"] and found["surprise"]


# ── T035: surprise rules (US6) ─────────────────────────────────────

def _weeks(conn, *weeks):
    for w in weeks:
        _give(conn, 5, kind="streak", key=f"week:2026-W{w:02d}")


@pytest.mark.parametrize("weeks,due", [((10, 11, 12, 13), True), ((10, 11, 12), False), ((10, 11, 13, 14), False)])
def test_four_week_streak(conn, weeks, due):
    _weeks(conn, *weeks)
    xp._set_state(conn, "rewards_caught_up_at", "2026-01-01 00:00:00")      # past the catch-up
    xp.sync(conn)
    assert (_rows(conn).get("s_streak4") == "surprise") is due


def test_five_sites(conn):
    xp._set_state(conn, "rewards_caught_up_at", "2026-01-01 00:00:00")
    for s in ("fa", "ws", "bsky", "ib"):
        _give(conn, 10, kind="site", key=f"site:artwork:A:{s}", site=s)
    xp.sync(conn)
    assert "s_sites5" not in _rows(conn)
    _give(conn, 10, kind="site", key="site:artwork:A:da", site="da")
    xp.sync(conn)
    assert _rows(conn)["s_sites5"] == "surprise"


# ── T038: level history (US7) ──────────────────────────────────────

def test_history_dates_and_months(conn, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(config, "display_zone", lambda: timezone.utc)
    xp.award(conn, "work", "h:1", xp.level_start(2), at="2026-08-05 10:00:00", source="backfill")
    xp.award(conn, "work", "h:2", xp.level_start(4) - xp.level_start(2), at="2026-09-30 23:59:00", source="backfill")
    xp.award(conn, "work", "h:3", 7, at="2026-10-01 00:00:00", source="backfill")
    h = rewards.history(conn, now=datetime(2026, 10, 22, tzinfo=timezone.utc))
    assert h["levels"] == [{"level": 2, "date": "2026-08-05"}, {"level": 3, "date": "2026-09-30"},
                           {"level": 4, "date": "2026-09-30"}]
    assert h["month"] == {"this": 7, "last": xp.level_start(4) - xp.level_start(2),
                          "this_label": "October", "last_label": "September"}


def test_history_month_edge_is_the_display_zone(conn, monkeypatch):
    from datetime import datetime, timedelta, timezone
    zone = timezone(timedelta(hours=10))
    monkeypatch.setattr(config, "display_zone", lambda: zone)
    xp.award(conn, "work", "h:z", 5, at="2026-09-30 15:00:00", source="backfill")   # 1 Oct, 01:00 at +10
    assert rewards.history(conn, now=datetime(2026, 10, 2, tzinfo=zone))["month"]["this"] == 5


# ── T030: profile picture (US8) ────────────────────────────────────

import io

from PIL import Image


@pytest.fixture
def api(conn):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.xp_api import xp_router
    app = FastAPI()
    app.include_router(xp_router)
    return TestClient(app)


def _png(w=1000, h=600, exif=False):
    buf = io.BytesIO()
    im = Image.new("RGB", (w, h), (200, 80, 40))
    kw = {}
    if exif:
        e = Image.Exif()
        e[0x010F] = "SampleCam"                                                  # Make
        kw["exif"] = e
    im.save(buf, "JPEG" if exif else "PNG", **kw)
    return buf.getvalue()


def _put(api, data, **form):
    return api.put("/api/xp/avatar", files={"image": ("p.png", data)}, data={"source": "upload", **form})


def test_avatar_reencodes_to_square_webp(api):
    r = _put(api, _png())
    assert r.status_code == 200 and r.json()["rating"] == "general"
    g = api.get("/api/xp/avatar")
    assert g.headers["content-type"] == "image/webp" and g.headers["etag"]
    with Image.open(io.BytesIO(g.content)) as im:
        assert (im.format, im.size) == ("WEBP", (256, 256))
    assert api.get("/api/xp/avatar", headers={"If-None-Match": g.headers["etag"]}).status_code == 304


def test_avatar_drops_exif(api):
    assert _put(api, _png(exif=True)).status_code == 200
    with Image.open(io.BytesIO(api.get("/api/xp/avatar").content)) as im:
        assert not im.getexif() and "exif" not in im.info


def test_avatar_rejects_bad_input(api):
    assert _put(api, b"not a picture at all").status_code == 400
    assert _put(api, _png(), rating="spicy").status_code == 400
    assert _put(api, _png(), source="web").status_code == 400
    assert _put(api, _png(4100, 10)).status_code == 400


def test_avatar_delete_then_404(api, conn):
    _put(api, _png(), rating="adult")
    assert rewards.avatar(conn)["rating"] == "adult"
    assert api.delete("/api/xp/avatar").json() == {"ok": True}
    assert api.get("/api/xp/avatar").status_code == 404
    assert rewards.avatar(conn) is None


# ── T039: levels off (FR-013) ──────────────────────────────────────

def test_unlocks_still_written_while_levels_are_off(conn):
    config.save_settings({"levels_enabled": False})
    _to_level(conn, 20)
    xp.sync(conn)
    assert "theme_ember" in _rows(conn)
    body = xp.totals(conn)["rewards"]
    assert body["unrevealed"] == [] and body["next"] is None and "theme_ember" in body["unlocked"]


# ── T040: rewards gate nothing (SC-005) ────────────────────────────

# Schema, data classification and the mirror registry name the tables; only these read them.
# polling/overnight.py only names the rewards unlocked while you were away (spec 052 US5).
_REWARD_READERS = {"rewards.py", "xp.py", "routes/xp_api.py", "routes/api.py", "polling/overnight.py"}
_SCHEMA_ONLY = {"database/db.py", "datamap.py", "mirror/registry.py"}


def test_rewards_gate_nothing():
    """Rewards are cosmetic: no posting, polling or limit may depend on them."""
    skip = {"tests", ".plan", "node_modules", "_archive", "build", "dist", ".venv", "venv"}
    bad = []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if skip & set(rel.split("/")) or rel in _REWARD_READERS | _SCHEMA_ONLY:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"xp_rewards|rewards_equipped|^\s*(import rewards|from rewards )", text, re.M):
            bad.append(rel)
    assert not bad, f"reads level rewards outside the allowed files: {bad}"
