"""Which account a poll is for, on every log line it writes (POLLLOGS, 4.79.2).

A round polls several accounts per site one after another, and the log said "FA poll failed: FA cookies
expired" with no way to tell whose. ``site_turns.site_turn`` (which wraps every ``run_<code>_poll_cycle``) sets
``current`` for the length of one account's cycle; the record factory below puts it in front of each message:

    [fa #2 KnaughtyKat] FA poll failed: FA cookies expired — ...

A ContextVar, so concurrent polls of different sites never mix their labels, and tasks a cycle starts inherit it.
Lines written outside a poll are untouched.
"""
from __future__ import annotations

import contextvars
import logging

current: contextvars.ContextVar[str] = contextvars.ContextVar("pp_poll_account", default="")

_installed = False


def label(code: str, account_id: int | None) -> str:
    """``"fa #2 KnaughtyKat"``: the site, the account id and its name. Falls back to less, never raises."""
    name = ""
    try:
        from database.db import get_connection
        from database import accounts as accounts_db
        conn = get_connection()
        try:
            if account_id is None:
                account_id = accounts_db.get_default_account_id(conn, code)
            row = accounts_db.get_account(conn, account_id) if account_id is not None else None
        finally:
            conn.close()
        if row:
            name = str(row["label"] or row["handle"] or "").strip()
    except Exception:  # noqa: BLE001 — a log label must never break a poll
        pass
    out = code if account_id is None else f"{code} #{account_id}"
    return f"{out} {name}"[:80].strip() if name else out


def install() -> None:
    """Wrap the record factory once. Order with log_redaction doesn't matter: each wraps the one before."""
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        tag = current.get()
        if tag and isinstance(record.msg, str):
            # %-escaped: the message is %-formatted with its args later, and a name may hold a %.
            record.msg = "[" + tag.replace("%", "%%") + "] " + record.msg
        return record

    logging.setLogRecordFactory(factory)
    _installed = True
