"""Spec 060 US1: PostyBirb submissions become Library pieces (desktop reader).

Covers FR-SEC-001 (record paths stay inside the folder), FR-SEC-003 (a Never-post account's
logins and parts stay out; logins never come pre-ticked), FR-SEC-007 (only real pictures and
videos), FR-SEC-009 (counts-only logs) and the import mapping. Stand-in data only (pb_fixture).
"""
from __future__ import annotations

import hashlib
import json
import logging

import pytest

import config
import pb_import
from database import accounts as adb
from database.db import get_connection
from tests import pb_fixture


@pytest.fixture
def pb(tmp_path, monkeypatch):
    docs, app = pb_fixture.build(tmp_path)
    monkeypatch.setenv("PAWPOLLER_PB_DIR", str(docs))
    monkeypatch.setenv("PAWPOLLER_PB_APPDATA", str(app))
    # The folder must exist, or get_artwork_archive_path falls through to the real archive.
    (tmp_path / "library").mkdir()
    config.save_settings({"artwork_archive_path": str(tmp_path / "library")})
    from posting import artwork_reader
    assert artwork_reader.get_artwork_archive_path() == tmp_path / "library"
    opened = []
    real_open = open

    def watch(path, *a, **k):                      # FR-SEC-001: the canary is never opened
        opened.append(str(path))
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", watch)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for d in (docs, app) for p in d.rglob("*") if p.is_file()}
    yield {"root": tmp_path, "docs": docs, "opened": opened}
    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for d in (docs, app) for p in d.rglob("*") if p.is_file()}
    assert after == before, "PostyBirb's files must never change (FR-004)"
    assert not any("canary" in o for o in opened), "a record pointed outside the folder and it was read"


def _friend_never_post():
    conn = get_connection()
    try:
        aid = adb.create_account(conn, "fa", "SecondHandle", handle="SecondHandle")
        conn.commit()
    finally:
        conn.close()
    config.save_settings({"never_post_account_ids": [aid]})
    return aid


def _pieces(pb):
    lib = pb["root"] / "library"
    return {d.name: json.loads((d / "masterpiece.json").read_text("utf-8")) for d in lib.iterdir()} if lib.is_dir() else {}


def test_scan_lists_file_submissions_with_reasons(pb):
    r = pb_import.scan_submissions()
    items = {i["id"]: i for i in r["items"]}
    assert r["version"] == 3 and r["total"] == 11 and "n1" not in items      # notifications aren't pieces
    assert items["s1"]["skip"] == "" and items["s1"]["tags"] == 2 and items["s1"]["rating"] == "general"
    assert items["s2"]["extra_files"] == 1
    assert items["s4"]["extreme"] is True and items["s4"]["rating"] == "adult"
    assert items["s5"]["skip"] == "file missing in PostyBirb's folder"
    assert items["s6"]["skip"] == ""                                        # a video with a poster
    for sid in ("s7", "s8"):                                               # FR-SEC-007
        assert items[sid]["skip"] == "not a picture or video PawPoller can hold"
    for sid in ("s9", "s10"):                                              # FR-SEC-001
        assert items[sid]["skip"] == "file missing in PostyBirb's folder"


def test_import_maps_fields_and_skips_repeats(pb):
    _friend_never_post()                       # the friend's FA part is then dropped, leaving one FA part
    res = {r["submission_id"]: r for r in pb_import.apply_submissions(["s1", "s2", "s4", "s6"])}
    assert all(r["status"] == "added" for r in res.values()), res
    pieces = {m["import_source"]["submission_id"]: m for m in _pieces(pb).values()}
    fox = pieces["s1"]
    assert fox["title"] == "Sample Fox" and fox["description"] == "A fox, sketched." and fox["rating"] == "general"
    assert fox["tags"]["core"] == ["fox", "sketch"] and fox["tags"]["fa"] == ["fa_tag"]
    assert fox["titles"] == {"fa": "Sample Fox (FA)"} and fox["descriptions"] == {"fa": "FA words"}
    assert pieces["s2"]["title"] == "Moon Study" and "1 extra file left out" in res["s2"]["note"]
    assert pieces["s4"]["rating"] == "adult" and "extreme" in res["s4"]["note"]
    assert pieces["s6"]["thumbnail"]                                        # the video kept its poster
    again = {r["submission_id"]: r for r in pb_import.apply_submissions(["s1", "s3"])}
    assert again["s1"] == {**again["s1"], "status": "skipped", "reason": "already imported"}
    assert again["s3"]["status"] == "added"


def test_same_file_already_in_library_is_skipped(pb):
    from posting import artwork_reader
    artwork_reader.create_artwork(title="Mine", image_filename="mine.png", image_bytes=pb_fixture.PNG)
    res = pb_import.apply_submissions(["s1"])
    assert res == [{"submission_id": "s1", "title": "Sample Fox", "status": "skipped",
                    "reason": "already in your Library (same file)"}]


def test_paths_and_types_never_escape(pb):
    res = {r["submission_id"]: r for r in pb_import.apply_submissions(["s7", "s8", "s9", "s10"])}
    assert res["s7"]["reason"] == res["s8"]["reason"] == "not a picture or video PawPoller can hold"
    assert res["s9"]["reason"] == res["s10"]["reason"] == "file missing in PostyBirb's folder"
    assert _pieces(pb) == {}
    assert pb_import._inside(pb["docs"] / "submission" / "files", "..") is None
    assert pb_import._cookies("../x", "furaffinity.net", str(pb["root"])) == {}


def test_never_post_account_is_left_out(pb):
    _friend_never_post()
    items = {i["id"]: i for i in pb_import.scan_submissions()["items"]}
    assert items["s11"]["others"] is True and items["s1"]["others"] is False
    res = pb_import.apply_submissions(["*"])
    got = {r["submission_id"] for r in res}
    assert "s11" not in got                                                 # tick-all never selects it
    fox = next(m for m in _pieces(pb).values() if m["import_source"]["submission_id"] == "s1")
    assert "friend" not in json.dumps(fox).lower() and fox["titles"] == {"fa": "Sample Fox (FA)"}
    logins = {a["pb_id"] for a in pb_import.scan()["accounts"]}
    assert "fa-friend" not in logins and "fa-1" in logins
    out = pb_import.apply(["fa-friend"])
    assert out[0]["status"] == "skipped"
    assert not any("fake-a-fa-friend" in json.dumps(v) for v in config.get_settings().values())


def test_logins_never_preticked_and_flag_two_on_one_site(pb):
    # No Never-post list at all (a seat): both FA accounts listed, both flagged "check each is yours".
    rows = {a["pb_id"]: a for a in pb_import.scan()["accounts"]}
    assert rows["fa-1"]["check_mine"] is True and rows["fa-friend"]["check_mine"] is True
    assert "../x" not in rows or rows["../x"]["portable"] is False          # a bad id never reaches a path
    assert pb_import.apply([]) == []                                        # nothing ticked → nothing written
    assert not any("fake-a-" in json.dumps(v) for v in config.get_settings().values())


def test_logs_carry_counts_only(pb, caplog):
    caplog.set_level(logging.DEBUG, logger="pb_import")   # the importer's own lines (FR-SEC-009)
    pb_import.scan_submissions()
    pb_import.apply_submissions(["s1", "s2"])
    pb_import.scan()
    text = " | ".join(r.getMessage() for r in caplog.records if r.name == "pb_import")
    for secret in ("Sample Fox", "Moon Study", "Penwright", "SecondHandle", "fake-a-", "fox.png", "SomeoneElse"):
        assert secret not in text, secret


def test_postybirb4_says_so(tmp_path, monkeypatch):
    (tmp_path / "PB4" / "data").mkdir(parents=True)
    (tmp_path / "PB4" / "data" / "database-production.sqlite").write_bytes(b"")
    monkeypatch.setenv("PAWPOLLER_PB_DIR", str(tmp_path / "PB4"))
    r = pb_import.scan_submissions()
    assert r["version"] == 4 and "PostyBirb 3" in r["message"]


def test_two_accounts_on_one_site_keep_neither_sites_text(pb):
    res = pb_import.apply_submissions(["s1"])                # no Never-post list: fa-1 and fa-friend both there
    assert res[0]["status"] == "added" and "more than one account on fa" in res[0]["note"]
    fox = next(iter(_pieces(pb).values()))
    assert "fa" not in fox["titles"] and "fa" not in fox["tags"] and fox["tags"]["core"] == ["fox", "sketch"]


def test_library_files_are_hashed_once_until_they_change(pb, monkeypatch):
    """SEC060: each page of the PostyBirb list used to re-read every Library file."""
    pb_import.apply_submissions(["s1"])
    pb_import._LIB_HASHES.clear()
    calls = []
    real = pb_import._hash
    monkeypatch.setattr(pb_import, "_hash", lambda p: calls.append(p) or real(p))
    first, _ = pb_import._library()
    n = len(calls)
    assert n and pb_import._library()[0] == first and len(calls) == n      # second pass: no re-reads
