"""Shared account-scoping helper for read queries + digest deltas.

Single source for the optional ``account_id = ?`` predicate used to scope
per-platform reads (and the notification digests) to one account.
``account_id=None`` means "All accounts" — no predicate, byte-identical to the
pre-scoping behaviour. Every analytics table already carries ``account_id``
(see the migrations in database/db.py), so scoping is pure WHERE-injection.
"""

from __future__ import annotations


def account_clause(account_id: int | None, alias: str = "") -> tuple[str, list]:
    """Return ``(sql_fragment, params)`` for an optional account filter.

    - ``account_id is None`` → ``("", [])`` — All accounts, no filter.
    - ``account_id`` is an int → ``("<alias.>account_id = ?", [account_id])``.

    The fragment is a bare predicate (no leading ``AND``/``WHERE``) so callers
    splice it into their own WHERE/AND context. Pass *alias* when the query
    aliases the table (e.g. ``"s"`` for ``snapshots s``).
    """
    if account_id is None:
        return "", []
    col = f"{alias}.account_id" if alias else "account_id"
    return f"{col} = ?", [account_id]
