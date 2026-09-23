"""A read path must never take the write lock to create something that exists (BLOCK2).

Reported from a desktop on 4.32.2: `GET /api/masterpieces/variant-suggestions` →
`OperationalError: database is locked` in `variant_suggest.ensure_dismiss_table`. Its
`CREATE TABLE IF NOT EXISTS` takes the write lock even when it creates nothing, so the
request queued behind whatever was writing and died on the 30 s busy timeout — the same
shape as `ensure_indexed` in 4.32.2, in a second place.

Each helper below now asks the catalogue first (a read) and returns. The test holds a
write lock open with `timeout=0`, which turns "would have waited" into an immediate
OperationalError.
"""
from __future__ import annotations

import sqlite3

import pytest

import config
from database import accounts as adb
from database import image_hash, variant_suggest
from database.db import get_connection


def _locked_writer():
    w = sqlite3.connect(str(config.DB_PATH), timeout=0)
    w.execute("BEGIN IMMEDIATE")
    return w


@pytest.mark.parametrize("ensure", [
    pytest.param(lambda c: variant_suggest.ensure_dismiss_table(c), id="not-variant dismissals"),
    pytest.param(lambda c: adb.ensure_accounts_table(c), id="accounts"),
    pytest.param(lambda c: image_hash.ensure_table(c), id="image hashes"),
])
def test_a_second_call_never_waits_on_a_writer(ensure):
    setup = get_connection()
    ensure(setup)                      # first call creates it, as any install has by now
    setup.commit()
    setup.close()

    writer = _locked_writer()
    reader = sqlite3.connect(str(config.DB_PATH), timeout=0)
    try:
        ensure(reader)                 # this raised OperationalError: database is locked
    finally:
        reader.close()
        writer.rollback()
        writer.close()


def test_the_dismissal_read_still_works_while_something_writes():
    """The route that broke: reading the dismissed pairs during a write."""
    setup = get_connection()
    variant_suggest.ensure_dismiss_table(setup)
    setup.commit()
    setup.close()

    writer = _locked_writer()
    reader = sqlite3.connect(str(config.DB_PATH), timeout=0)
    reader.row_factory = sqlite3.Row
    try:
        assert variant_suggest.not_variant_pairs(reader) == set()
    finally:
        reader.close()
        writer.rollback()
        writer.close()


def test_the_first_real_creation_still_takes_the_lock(tmp_path):
    """The guard must skip the DDL, not hide a failure: with the table genuinely absent
    and another connection holding the write lock, the create must still raise."""
    db = tmp_path / "fresh.db"
    sqlite3.connect(str(db)).close()
    writer = sqlite3.connect(str(db), timeout=0)
    writer.execute("BEGIN IMMEDIATE")
    blocked = sqlite3.connect(str(db), timeout=0)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            variant_suggest.ensure_dismiss_table(blocked)
    finally:
        blocked.close()
        writer.rollback()
        writer.close()
