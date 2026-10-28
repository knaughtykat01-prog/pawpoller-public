"""Go-live posts (spec 045): one post per stream, the owner's Post / Skip, pauses, end of stream.

No network: the publisher, Discord and Telegram are stubbed. Handles are the placeholder set.
"""
from __future__ import annotations

import asyncio

import pytest

import config
from database.db import get_connection
from polling import golive


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def setup(monkeypatch):
    """A Twitch account with go-live on, a Bluesky + a Threads account to post to, the publisher stubbed."""
    from database import accounts as adb
    conn = get_connection()
    try:
        ttv = adb.get_default_account_id(conn, "ttv", create=True)
        bsky = adb.create_account(conn, "bsky", "Main", handle="SecondFur", is_default=True)
        thr = adb.create_account(conn, "thr", "Main", handle="SecondFur", is_default=True)
        conn.commit()
    finally:
        conn.close()
    sent = []

    async def fake_publish(post_id, platforms, account_ids=None, settings=None, persona_id=None, announce=True):
        assert announce is False                    # a go-live never doubles as a Discord announce
        c = get_connection()
        try:
            body = c.execute("SELECT body FROM posts WHERE post_id = ?", (post_id,)).fetchone()[0]
        finally:
            c.close()
        sent.append((platforms[0], account_ids[platforms[0]], body))
        fail = getattr(fake_publish, "fail", set())
        if platforms[0] in fail:
            return [{"platform": platforms[0], "success": False, "error": "The site said no"}]
        return [{"platform": platforms[0], "success": True, "external_id": f"x{len(sent)}",
                 "external_url": f"https://example.com/{len(sent)}"}]

    from posting import post_publisher
    monkeypatch.setattr(post_publisher, "publish_post", fake_publish)
    calls = []

    async def fake_bot(token, method, payload):
        calls.append((method, payload))
        return {"message_id": 77} if method == "sendMessage" else True

    monkeypatch.setattr(golive, "_bot_post", fake_bot)
    golive.save_config("ttv", ttv, {"enabled": True, "approve_first": True, "cooldown_min": 15, "targets": [
        {"platform": "bsky", "target_account_id": bsky, "live_template": "Live: {title} ({game}) {link}"},
        {"platform": "thr", "target_account_id": thr, "live_template": "On {site} now: {title}"}]})
    return {"ttv": ttv, "bsky": bsky, "thr": thr, "sent": sent, "publish": fake_publish, "bot": calls}


def stream(key="s1", **kw):
    return {"stream_key": key, "title": "Sketching", "game": "Art", "link": "https://www.twitch.tv/inkwolf",
            "name": "Inkwolf", "started_at": golive._now(), "mature": False, **kw}


def runs():
    conn = get_connection()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM golive_runs ORDER BY id")]
    finally:
        conn.close()


def _set(sql, *args):
    conn = get_connection()
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


# ── wording ──────────────────────────────────────────────────────────────────

def test_the_template_is_filled_and_nothing_else_is_touched():
    out = golive.render("{name} is live: {title} — {game} {link} {unknown} {", stream())
    assert out == "Inkwolf is live: Sketching — Art https://www.twitch.tv/inkwolf {unknown} {"


# ── settings ─────────────────────────────────────────────────────────────────

def test_a_never_post_account_is_not_offered_or_accepted(setup):
    config.save_settings({"never_post_account_ids": [setup["thr"]]})
    conn = get_connection()
    try:
        offered = {(t["platform"], t["account_id"]) for t in golive.available_targets(conn, config.get_settings())}
    finally:
        conn.close()
    assert ("thr", setup["thr"]) not in offered and ("bsky", setup["bsky"]) in offered
    with pytest.raises(ValueError, match="Never post"):
        golive.save_config("ttv", setup["ttv"], {"targets": [{"platform": "thr", "target_account_id": setup["thr"]}]})


@pytest.mark.parametrize("cooldown", [-1, 61, "soon"])
def test_a_bad_cooldown_is_refused(setup, cooldown):
    with pytest.raises(ValueError):
        golive.save_config("ttv", setup["ttv"], {"cooldown_min": cooldown})


# ── one post per stream (SC-002) ─────────────────────────────────────────────

def test_one_stream_makes_one_run_however_often_it_is_seen(setup):
    first = run(golive.on_online("ttv", setup["ttv"], stream(), detected_by="signal"))
    assert first
    assert run(golive.on_online("ttv", setup["ttv"], stream(), detected_by="signal")) is None   # a replayed signal
    run(golive.on_check_state("ttv", setup["ttv"], stream()))                                    # the minute check
    assert len(runs()) == 1 and runs()[0]["state"] == "waiting"


def test_a_restart_within_the_cooldown_is_the_same_stream(setup):
    run(golive.on_online("ttv", setup["ttv"], stream("s1"), detected_by="signal"))
    golive.on_offline("ttv", setup["ttv"])
    assert run(golive.on_online("ttv", setup["ttv"], stream("s2"), detected_by="signal")) is None
    assert len(runs()) == 1


def test_a_restart_after_the_cooldown_is_a_new_stream(setup):
    run(golive.on_online("ttv", setup["ttv"], stream("s1"), detected_by="signal"))
    golive.on_offline("ttv", setup["ttv"])
    _set("UPDATE golive_streams SET ended_at = datetime('now', '-20 minutes')")
    assert run(golive.on_online("ttv", setup["ttv"], stream("s2"), detected_by="signal"))
    assert len(runs()) == 2


def test_a_stream_first_seen_long_after_it_started_is_not_announced(setup):
    old = stream(started_at="2000-01-01 00:00:00")
    assert run(golive.on_online("ttv", setup["ttv"], old, detected_by="check")) is None
    conn = get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM golive_streams").fetchone()[0] == 1   # recorded, not posted
    finally:
        conn.close()
    assert runs() == []


def test_go_live_off_records_the_stream_and_posts_nothing(setup):
    golive.save_config("ttv", setup["ttv"], {"enabled": False})
    assert run(golive.on_online("ttv", setup["ttv"], stream(), detected_by="signal")) is None
    assert runs() == [] and setup["sent"] == []


# ── show me first ────────────────────────────────────────────────────────────

def test_a_waiting_run_posts_once_on_post(setup):
    rid = run(golive.on_online("ttv", setup["ttv"], stream(), detected_by="signal"))
    assert setup["sent"] == []                                   # nothing until the owner says so
    assert run(golive.decide(rid, "post", by="app")) == "posted"
    assert [s[0] for s in setup["sent"]] == ["bsky", "thr"]
    assert setup["sent"][0][2] == "Live: Sketching (Art) https://www.twitch.tv/inkwolf"
    assert setup["sent"][1][2] == "On Twitch now: Sketching"
    assert run(golive.decide(rid, "post", by="app")) == "posted"   # a second tap does nothing
    assert len(setup["sent"]) == 2


def test_skip_posts_nothing(setup):
    rid = run(golive.on_online("ttv", setup["ttv"], stream(), detected_by="signal"))
    assert run(golive.decide(rid, "skip", by="app")) == "skipped"
    assert run(golive.decide(rid, "post", by="app")) == "skipped" and setup["sent"] == []


def test_a_waiting_run_lapses_after_two_hours(setup):
    rid = run(golive.on_online("ttv", setup["ttv"], stream(), detected_by="signal"))
    _set("UPDATE golive_runs SET created_at = datetime('now', '-3 hours')")
    assert run(golive.decide(rid, "post", by="app")) == "expired" and setup["sent"] == []


def test_the_telegram_buttons_answer_only_the_owners_chat(setup):
    config.save_settings({"telegram_enabled": True, "telegram_bot_token": "123:abc", "telegram_chat_id": "4242"})
    rid = run(golive.on_online("ttv", setup["ttv"], stream(), detected_by="signal"))
    ask = [p for m, p in setup["bot"] if m == "sendMessage"][0]
    data = ask["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert ask["chat_id"] == "4242" and data.startswith(f"gl:p:{rid}:")
    stranger = {"id": "1", "data": data, "from": {"id": 999}, "message": {"chat": {"id": 999}}}
    assert run(golive.handle_callback("123:abc", "4242", stranger)) is True
    assert runs()[0]["state"] == "waiting" and setup["sent"] == []
    wrong = "11" if data.endswith("00") else "00"     # a random nonce ends in "00" about 1 run in 256
    forged = {"id": "2", "data": data[:-2] + wrong, "from": {"id": 4242}, "message": {"chat": {"id": 4242}}}
    run(golive.handle_callback("123:abc", "4242", forged))
    assert runs()[0]["state"] == "waiting"                        # a wrong nonce decides nothing
    owner = {"id": "3", "data": data, "from": {"id": 4242}, "message": {"chat": {"id": 4242}}}
    run(golive.handle_callback("123:abc", "4242", owner))
    assert runs()[0]["state"] == "posted" and len(setup["sent"]) == 2
    assert any(m == "editMessageText" for m, _ in setup["bot"])   # the buttons are replaced by the outcome


# ── posting on its own, and the pauses (FR-013) ──────────────────────────────

def test_without_show_me_first_it_posts_only_when_automatic_posting_is_on(setup):
    golive.save_config("ttv", setup["ttv"], {"enabled": True, "approve_first": False, "targets": [
        {"platform": "bsky", "target_account_id": setup["bsky"]}]})
    run(golive.on_online("ttv", setup["ttv"], stream("a"), detected_by="signal"))
    assert runs()[0]["state"] == "held" and setup["sent"] == []
    config.save_settings({"posting_enabled": True})
    _set("UPDATE golive_streams SET state = 'final'")
    _set("UPDATE golive_streams SET ended_at = datetime('now', '-1 hours')")
    run(golive.on_online("ttv", setup["ttv"], stream("b"), detected_by="signal"))
    assert runs()[1]["state"] == "posted" and len(setup["sent"]) == 1


def test_a_copy_that_moved_away_never_posts(setup):
    config.save_settings({"moved_away_at": "2026-10-19 00:00:00"})
    rid = run(golive.on_online("ttv", setup["ttv"], stream(), detected_by="signal"))
    assert runs()[0]["state"] == "held"
    assert run(golive.post_run(rid)) == "held" and setup["sent"] == []


def test_a_site_paused_on_the_box_is_skipped(setup, monkeypatch):
    import polling.site_turns as st
    monkeypatch.setattr(st, "operator_paused", lambda: {"thr"})
    rid = run(golive.on_online("ttv", setup["ttv"], stream(), detected_by="signal"))
    assert run(golive.decide(rid, "post", by="app")) == "partial"
    assert [s[0] for s in setup["sent"]] == ["bsky"]


def test_a_mature_stream_skips_general_only_sites_unless_allowed(setup):
    rid = run(golive.on_online("ttv", setup["ttv"], stream(mature=True), detected_by="signal"))
    assert run(golive.decide(rid, "post", by="app")) == "partial"
    assert [s[0] for s in setup["sent"]] == ["bsky"]
    conn = get_connection()
    try:
        why = conn.execute("SELECT error FROM golive_run_targets WHERE platform = 'thr'").fetchone()[0]
    finally:
        conn.close()
    assert "mature" in why


def test_an_account_marked_never_post_later_is_refused_at_send_time(setup):
    rid = run(golive.on_online("ttv", setup["ttv"], stream(), detected_by="signal"))
    config.save_settings({"never_post_account_ids": [setup["thr"]]})
    run(golive.decide(rid, "post", by="app"))
    assert [s[0] for s in setup["sent"]] == ["bsky"]


def test_one_sites_refusal_is_its_own(setup):
    setup["publish"].fail = {"bsky"}
    rid = run(golive.on_online("ttv", setup["ttv"], stream(), detected_by="signal"))
    assert run(golive.decide(rid, "post", by="app")) == "partial"
    conn = get_connection()
    try:
        rows = {r["platform"]: dict(r) for r in conn.execute("SELECT * FROM golive_run_targets")}
    finally:
        conn.close()
    assert rows["bsky"]["status"] == "failed" and rows["bsky"]["error"] == "The site said no"
    assert rows["thr"]["status"] == "posted"
    items = golive.bell_items()
    assert items and items[0]["kind"] == "golive" and items[0]["status"] == "error"


def test_the_bell_offers_a_waiting_go_live(setup):
    run(golive.on_online("ttv", setup["ttv"], stream(), detected_by="signal"))
    item = golive.bell_items()[0]
    assert item["status"] == "warn" and "post your go-live" in item["summary"] and item["url"] == "#/ttv"


# ── Discord + end of stream (US2) ────────────────────────────────────────────

def test_discord_is_a_target_and_its_post_is_edited_when_the_stream_ends(setup, monkeypatch):
    from posting import discord
    config.save_settings({"discord_webhook_url": "https://discord.com/api/webhooks/1/abc"})
    edits, sends = [], []

    async def fake_send(text, settings=None):
        sends.append(text)
        return "555"

    async def fake_edit(mid, text, settings=None):
        edits.append((mid, text))
        return True

    monkeypatch.setattr(discord, "send_text", fake_send)
    monkeypatch.setattr(discord, "edit_text", fake_edit)
    golive.save_config("ttv", setup["ttv"], {"enabled": True, "end_action": "edit", "targets": [
        {"platform": "discord", "target_account_id": 0, "live_template": "Live: {title}", "ended_template": "Over: {title}"},
        {"platform": "bsky", "target_account_id": setup["bsky"]}]})
    rid = run(golive.on_online("ttv", setup["ttv"], stream(), detected_by="signal"))
    assert run(golive.decide(rid, "post", by="app")) == "posted" and sends == ["Live: Sketching"]
    golive.on_offline("ttv", setup["ttv"])
    run(golive.finalise())
    assert edits == []                                            # still inside the cool-down
    _set("UPDATE golive_streams SET ended_at = datetime('now', '-20 minutes')")
    run(golive.finalise())
    assert edits == [("555", "Over: Sketching")]
    conn = get_connection()
    try:
        states = {r[0]: r[1] for r in conn.execute("SELECT platform, edit_state FROM golive_run_targets")}
    finally:
        conn.close()
    assert states == {"discord": "edited", "bsky": "cant_edit"}
    run(golive.finalise())
    assert len(edits) == 1                                        # once


def test_leave_them_never_edits(setup, monkeypatch):
    rid = run(golive.on_online("ttv", setup["ttv"], stream(), detected_by="signal"))
    run(golive.decide(rid, "post", by="app"))
    golive.on_offline("ttv", setup["ttv"])
    _set("UPDATE golive_streams SET ended_at = datetime('now', '-20 minutes')")
    run(golive.finalise())
    conn = get_connection()
    try:
        assert {r[0] for r in conn.execute("SELECT edit_state FROM golive_run_targets")} == {""}
    finally:
        conn.close()


def test_a_waiting_run_lapses_when_its_stream_is_over(setup):
    run(golive.on_online("ttv", setup["ttv"], stream(), detected_by="signal"))
    golive.on_offline("ttv", setup["ttv"])
    _set("UPDATE golive_streams SET ended_at = datetime('now', '-20 minutes')")
    run(golive.finalise())
    assert runs()[0]["state"] == "expired"


# ── the minute check ─────────────────────────────────────────────────────────

def test_the_minute_check_asks_once_for_every_channel_and_starts_a_run(setup, monkeypatch):
    config.save_settings({"ttv_user_id": "1001", "ttv_channel": "inkwolf", "ttv_client_id": "cid",
                          "ttv_client_secret": "sec"})
    asked = []

    class FakeClient:
        client_id = "cid"

        async def get_streams(self, ids):
            asked.append(list(ids))
            return {"1001": stream("live1")}

        async def close(self):
            pass

    import polling.ttv_poller as tp
    monkeypatch.setattr(tp, "make_client", lambda creds: FakeClient())
    golive._last_check.clear()
    run(golive.check_live())
    assert asked == [["1001"]] and len(runs()) == 1
    run(golive.check_live())                                      # within the minute: not asked again
    assert len(asked) == 1
