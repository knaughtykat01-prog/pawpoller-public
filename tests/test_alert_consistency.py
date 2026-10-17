"""Spec 044: every Telegram alert opens the same way, says what changed, and uses the piece's real title.

SC-001 one first-line shape; SC-002 no folder names; SC-003 one message per multi-site publish;
SC-004 every item says what changed; SC-005 the milestone check's lookups don't grow with the gallery.
"""
import asyncio
import pathlib
import re
import sqlite3
import types

import pytest

import config
from polling import notifications
import polling.telegram as tg

UNIFORM = "bsky da e621 fbr fn ig ik mast ng pix r34 sc thr tum tw wp ws yt".split()


@pytest.fixture
def tg_on(monkeypatch):
    """Telegram on, pointed at a private chat; every send captured instead of posted."""
    sent = []
    monkeypatch.setattr(config, "get_settings", lambda: {
        "telegram_enabled": True, "telegram_bot_token": "t", "telegram_chat_id": "55501"})

    async def _send(token, chat_id, text, *, log_label=""):
        sent.append(text)
        return True
    monkeypatch.setattr(notifications, "send_telegram", _send)
    monkeypatch.setattr(notifications, "maybe_show_toast", lambda *a, **k: False)
    return sent


@pytest.fixture
def two_accounts(monkeypatch):
    """The site has accounts to tell apart: account 7 is SecondFur under persona Inkwolf."""
    monkeypatch.setattr(tg, "_should_label_account", lambda conn, p, a: True)
    monkeypatch.setattr(tg, "_account_context", lambda conn, a: ("SecondFur", "Inkwolf"))


# ── US1: the shared first line ───────────────────────────────────────────────

def test_every_site_has_its_own_emoji():
    emoji = list(tg.PLATFORM_EMOJI.values())
    assert len(emoji) == len(set(emoji)), "two sites share an emoji"


def test_header_is_site_then_account(two_accounts):
    assert tg.alert_header("da", 7) == "<b>🎨 DeviantArt · Inkwolf · SecondFur</b>"


def test_a_persona_named_like_its_account_is_said_once(monkeypatch):
    monkeypatch.setattr(tg, "_should_label_account", lambda conn, p, a: True)
    monkeypatch.setattr(tg, "_account_context", lambda conn, a: ("SecondFur", "secondfur"))
    assert tg.alert_header("fa", 7) == "<b>🦊 FurAffinity · SecondFur</b>"


def test_a_lone_account_is_not_named(monkeypatch):
    monkeypatch.setattr(tg, "_should_label_account", lambda conn, p, a: False)
    assert tg.alert_header("sf", 7) == "<b>🐺 SoFurry</b>"


def test_the_cycle_account_is_used_when_none_is_passed(two_accounts):
    token = notifications.current_alert_account.set(("ib", 7))
    try:
        assert "Inkwolf · SecondFur" in tg.alert_header("ib")
        assert tg.alert_header("fa") == "<b>🦊 FurAffinity</b>", "another site's context is not borrowed"
    finally:
        notifications.current_alert_account.reset(token)


def test_a_poll_failure_names_the_account(two_accounts, monkeypatch):
    sent = []

    async def _send(text):
        sent.append(text)
        return True
    monkeypatch.setattr(tg, "send_telegram", _send)
    monkeypatch.setattr(config, "get_settings", lambda: {"telegram_error_alerts": True})
    monkeypatch.setattr(tg, "orchestrated_poll_active", False)
    token = notifications.current_alert_account.set(("fa", 7))
    try:
        asyncio.run(tg.send_poll_error("fa", RuntimeError("FA cookies expired")))
    finally:
        notifications.current_alert_account.reset(token)
    assert sent and sent[0].startswith("<b>🦊 FurAffinity · Inkwolf · SecondFur</b>\n<b>⚠️ Poll failed</b>")


def test_event_alerts_take_the_shared_first_line(tg_on, two_accounts):
    asyncio.run(notifications.maybe_send_telegram_summary(
        config.get_settings(), "<b>2 new kudos</b>", ["Inkwolf → Sample Story"], platform="ao3"))
    assert tg_on[0].startswith("<b>📖 AO3</b>\n<b>2 new kudos</b>\n  • Inkwolf → Sample Story")


# ── US2: what changed, under the real title ──────────────────────────────────

def test_format_deltas_singular_and_plural():
    assert notifications.format_deltas({"faves": 2, "comments": 1}) == "+2 faves, +1 comment"
    assert notifications.format_deltas({"replies": 1, "kudos": 1}) == "+1 reply, +1 kudos"


def test_only_increases_are_listed():
    item = notifications.activity_item(5, "Sample Story", faves=0, comments=-1, likes=3)
    assert item["deltas"] == {"likes": 3}


def test_alert_title_cuts_on_a_word_and_is_never_blank():
    assert tg.alert_title("") == "(untitled)"
    long = "Dug out of the archives and the swimsuit is technically still drying in the sun"
    cut = tg.alert_title(long)
    assert len(cut) <= 61 and cut.endswith("…") and long.startswith(cut[:-1])


def test_activity_uses_the_linked_pieces_title(tg_on, two_accounts, monkeypatch):
    monkeypatch.setattr(notifications, "canonical_title_map", lambda p, ids: {"101": "Sample Story"})
    items = [notifications.activity_item(101, "Dug out of the archives", faves=1),
             notifications.activity_item(102, "Unlinked Post", comments=2)]
    asyncio.run(notifications.send_activity_alert("e621", items, settings_key="e621_notifications_enabled",
                                                  account_id=7))
    msg = tg_on[0]
    assert msg.splitlines()[0] == "<b>🐉 e621 · Inkwolf · SecondFur</b>"
    assert "  • Sample Story: +1 fave" in msg and "Dug out" not in msg
    assert "  • Unlinked Post: +2 comments" in msg


@pytest.mark.parametrize("code", UNIFORM)
def test_every_site_alerts_through_the_shared_path(code):
    src = pathlib.Path(f"polling/{code}_poller.py").read_text(encoding="utf-8")
    assert f'send_activity_alert("{code}"' in src
    assert "Updated</b>" not in src
    assert "activity_item(" in src


def test_no_poller_hand_builds_an_updated_header():
    for path in pathlib.Path("polling").glob("*_poller.py"):
        assert not re.search(r"<b>[^\"]*Updated</b>", path.read_text(encoding="utf-8")), path.name


# ── US3: one message per publish ─────────────────────────────────────────────

@pytest.fixture
def posting(monkeypatch):
    from posting import scheduler
    sent = []

    async def _send(text):
        sent.append(text)
        return True
    monkeypatch.setattr(tg, "send_telegram", _send)
    monkeypatch.setattr(scheduler, "_NOTICE_WINDOW_S", 0.05)
    monkeypatch.setattr(config, "get_settings", lambda: {"telegram_enabled": True})
    monkeypatch.setattr(scheduler.story_reader, "load_story",
                        lambda name: types.SimpleNamespace(title="Sample Story"))
    import posting.artwork_reader as ar
    monkeypatch.setattr(ar, "load_artwork", lambda name: types.SimpleNamespace(title="Inkwolf Portrait"))
    scheduler._pending_notices.clear()
    return scheduler, sent


def _drive(coros):
    async def run():
        for c in coros:
            await c
        await asyncio.sleep(0.2)
    asyncio.run(run())


def test_one_publish_to_three_sites_is_one_message(posting):
    scheduler, sent = posting
    n = scheduler._notify_completion
    _drive([n("Sample_Story", 3, "mast", "post", True, url="https://m.example/1"),
            n("Sample_Story", 3, "bsky", "post", True, url="https://b.example/2"),
            n("Sample_Story", 3, "tw", "post", False, "X refused it")])
    assert len(sent) == 1
    msg = sent[0]
    assert msg.splitlines()[0] == "<b>✅ Posted: Sample Story · Ch 3</b>"
    assert "Sample_Story" not in msg, "folder name leaked"
    assert "  ✅ 🐘 Mastodon: https://m.example/1" in msg
    assert "  ❌ 🐦 X/Twitter: X refused it" in msg


def test_artwork_has_no_chapter_and_its_own_title(posting):
    scheduler, sent = posting
    _drive([scheduler._notify_completion("WS_PFP", 0, "ws", "post", True, content_type="artwork")])
    assert sent[0].splitlines()[0] == "<b>✅ Posted: Inkwolf Portrait</b>"
    assert "Full" not in sent[0] and "WS PFP" not in sent[0]


def test_different_pieces_get_their_own_messages(posting):
    scheduler, sent = posting
    n = scheduler._notify_completion
    _drive([n("Sample_Story", 1, "mast", "post", True), n("Other_Story", 1, "mast", "post", True)])
    assert len(sent) == 2


def test_all_failed_says_failed(posting):
    scheduler, sent = posting
    _drive([scheduler._notify_completion("Sample_Story", 2, "fa", "post", False, "cookies <expired>")])
    assert sent[0].startswith("<b>❌ Post failed: Sample Story · Ch 2</b>")
    assert "cookies &lt;expired&gt;" in sent[0], "the reason is escaped"


# ── US4: milestones from one lookup ──────────────────────────────────────────

def _milestone_db(n_subs):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE subs (submission_id INTEGER, title TEXT, account_id INTEGER)")
    c.execute("CREATE TABLE snaps (submission_id INTEGER, views INTEGER, favorites_count INTEGER, "
              "comments_count INTEGER, polled_at TEXT)")
    for i in range(n_subs):
        c.execute("INSERT INTO subs VALUES (?, ?, 1)", (i, f"Piece {i}"))
        c.execute("INSERT INTO snaps VALUES (?, 10, 48, 0, '2026-01-01')", (i,))
        c.execute("INSERT INTO snaps VALUES (?, 10, ?, 0, '2026-01-02')", (i, 52 if i == 0 else 48))
    return c


@pytest.mark.parametrize("n_subs", [3, 300])
def test_milestones_take_one_snapshot_query(n_subs, monkeypatch):
    db = _milestone_db(n_subs)
    queries = []
    db.set_trace_callback(lambda q: queries.append(q) if "snaps" in q else None)

    class _Conn:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass
    monkeypatch.setattr(tg, "get_connection", lambda: _Conn())
    fired = []

    async def _check(platform, sid, title, *a, **k):
        if a[1] >= 50 > a[4]:
            fired.append(sid)
    monkeypatch.setattr(tg, "check_milestones", _check)
    monkeypatch.setattr(config, "get_settings", lambda: {"telegram_milestones": True})
    asyncio.run(tg.check_milestones_batch("ib", "snaps", "subs", account_id=1))
    assert fired == [0]
    assert len(queries) == 1, queries
