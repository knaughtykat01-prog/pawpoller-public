"""Story schedules and drips post as a persona (4.43.1).

Before, the story schedule / drip routes took no account at all — every row posted as
the platform's default — and the story publish panel's persona picker never rendered
(it guarded on ``window.Components``, and a top-level ``const`` is not a window property).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from database import accounts as accounts_db
from database.db import get_connection
from routes import editor_api

ROOT = Path(__file__).resolve().parent.parent


def _src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _persona_with_account(platform="ib"):
    conn = get_connection()
    try:
        pid = conn.execute("INSERT INTO personas(name) VALUES ('Main')").lastrowid
        acct = accounts_db.create_account(conn, platform, "one")
        conn.execute("UPDATE accounts SET persona_id = ? WHERE account_id = ?", (pid, acct))
        conn.commit()
        return pid, acct
    finally:
        conn.close()


def test_a_persona_schedule_needs_that_personas_account_on_every_site():
    pid, acct = _persona_with_account("ib")
    editor_api._story_persona_check(["ib"], {"ib": acct}, pid)                  # fine
    editor_api._story_persona_check(["ib", "sf"], {}, None)                     # no persona: unchanged
    with pytest.raises(HTTPException) as e:
        editor_api._story_persona_check(["ib", "sf"], {"ib": acct}, pid)       # no sf account
    assert e.value.status_code == 400 and "sf" in e.value.detail


def test_the_routes_store_the_account_and_persona():
    src = _src("routes/editor_api.py")
    assert src.count("persona_id=req.persona_id,") == 3                         # publish + schedule + drip
    assert "account_id=req.account_id," in src and "account_id=account_ids.get(platform)," in src
    assert src.count("_story_persona_check(") == 3                              # def + two routes


def test_the_panel_sends_them_and_the_picker_can_render():
    js = _src("frontend/js/publish_check.js")
    assert js.count("persona_id: (function ()") == 3                            # publish, schedule, drip
    assert "drip-persona-row" in js and "selectClass: 'drip-acct'" in js
    # Other modules guard on window.X — a top-level const is not one, so each must assign itself.
    for f, name in (("components", "Components"), ("utils", "Utils"), ("api", "API"), ("app", "App"),
                    ("charts", "Charts"), ("editor", "Editor"), ("posting", "Posting")):
        assert f"window.{name} = {name};" in _src(f"frontend/js/{f}.js"), name


def test_without_a_persona_the_chosen_account_must_still_be_real_and_on():
    """Release review (4.43.2, Low): with no persona, an explicit account was never checked."""
    conn = get_connection()
    try:
        ib = accounts_db.create_account(conn, "ib", "ib one")
        off = accounts_db.create_account(conn, "sf", "sf off", enabled=False)
    finally:
        conn.close()
    editor_api._story_persona_check(["ib"], {"ib": ib}, None)                       # fine
    editor_api._story_persona_check(["ib", "sf"], {}, None)                         # nothing chosen: default
    for plats, ids, needle in ((["sf"], {"sf": ib}, "not a sf account"),
                               (["sf"], {"sf": off}, "disabled"),
                               (["ib"], {"ib": 999999}, "not a ib account")):
        with pytest.raises(HTTPException) as e:
            editor_api._story_persona_check(plats, ids, None)
        assert needle in e.value.detail
