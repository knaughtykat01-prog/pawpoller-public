"""POLLLOGS: every line a poll writes names the account it is for; lines outside a poll don't change."""
import asyncio
import logging

from polling import log_context
from polling.site_turns import site_turn


def test_a_polls_lines_name_the_account(caplog, monkeypatch):
    monkeypatch.setattr(log_context, "label", lambda code, acct: f"{code} #{acct} SecondFur 100%")
    log = logging.getLogger("polling.test_poller")

    @site_turn("fa")
    async def run_fa_poll_cycle(account_id=None, force_full=False):
        log.info("found %d submissions", 11)
        return {}

    with caplog.at_level(logging.INFO):
        asyncio.run(run_fa_poll_cycle(2))
        log.info("outside a poll")
    msgs = [r.getMessage() for r in caplog.records if r.name == "polling.test_poller"]
    assert msgs == ["[fa #2 SecondFur 100%] found 11 submissions", "outside a poll"]


def test_the_label_names_the_account_or_falls_back(monkeypatch):
    from database import accounts as accounts_db
    from database.db import get_connection
    conn = get_connection()
    try:
        aid = accounts_db.create_account(conn, "fa", "SecondFur", handle="secondfur")
        conn.commit()
    finally:
        conn.close()
    assert log_context.label("fa", aid) == f"fa #{aid} SecondFur"
    assert log_context.label("fa", 999999) == "fa #999999"
