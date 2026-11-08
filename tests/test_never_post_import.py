"""SEC070 (1): work polled from a Never post account is someone else's — never offered for import.

The Discovered list leaves it out (so the Library's and Posts' bulk imports never reach it), and a
single import by id is refused. Stand-in ids only.
"""
import pytest

import config
from database.db import get_connection
from routes.submissions_api import get_discovered_unlinked

FRIEND, MINE = 41, 1


def _add(conn, table, sid, account_id, **extra):
    cols = {"submission_id": sid, "title": f"Piece {sid}", "username": "tester",
            "account_id": account_id, "posted_at": "2026-03-03", **extra}
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(cols.values()))
    conn.commit()


@pytest.fixture
def rows():
    config.save_settings({"never_post_account_ids": [FRIEND]})
    conn = get_connection()
    _add(conn, "fa_submissions", 701, FRIEND, category="Artwork (Digital)", thumbnail_url="http://t/a.jpg")
    _add(conn, "fa_submissions", 702, MINE, category="Artwork (Digital)", thumbnail_url="http://t/b.jpg")
    _add(conn, "tw_submissions", "801", FRIEND, description="Their words.", rating="general")
    yield conn
    conn.close()


def test_discovered_leaves_out_never_post_work(rows):
    ids = {(d["platform"], d["submission_id"]) for d in get_discovered_unlinked(rows)}
    assert ("fa", "702") in ids
    assert ("fa", "701") not in ids and ("tw", "801") not in ids


def test_bulk_imports_never_reach_it(rows, monkeypatch):
    from posting import artwork_importer, post_importer
    from routes import artwork_api
    tried = []
    monkeypatch.setattr(artwork_importer, "import_artwork",
                        lambda p, s: tried.append((p, s)) or {"status": "imported", "name": s})
    artwork_api.import_all_for_platform("fa")
    artwork_api.import_all_discovered_art()
    assert ("fa", "701") not in tried and ("fa", "702") in tried
    assert post_importer.import_all_discovered_posts()["total"] == 0


def test_single_imports_refused(rows):
    from posting import artwork_importer, post_importer
    with pytest.raises(ValueError, match="Never post"):
        artwork_importer.import_artwork("fa", "701")
    with pytest.raises(ValueError, match="Never post"):
        post_importer.import_post("tw", "801")


def test_unmarked_again_it_comes_back(rows):
    config.save_settings({"never_post_account_ids": []})
    ids = {(d["platform"], d["submission_id"]) for d in get_discovered_unlinked(rows)}
    assert ("fa", "701") in ids and ("tw", "801") in ids
