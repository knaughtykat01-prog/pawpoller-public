"""Moving a whole PawPoller into another install, e.g. a hosted seat (spec 035).

Two "installs" live in one test: each is a data folder with its own database, settings, vault key
and media folders. `use()` points config at one of them, the way the app would see it.
"""
import io
import json
import sqlite3
import time
import zipfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from routes import move_api

PHRASE = "correct horse battery"


class Install:
    def __init__(self, root: Path, works=("Sample Story",)):
        self.root = root
        self.data = root / "data"
        (self.data / "artwork").mkdir(parents=True)
        (root / "stories").mkdir()
        self.key = Fernet.generate_key().decode()
        db = sqlite3.connect(self.data / "pawpoller.db")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE fa_submissions (id INTEGER PRIMARY KEY, title TEXT)")
        db.executemany("INSERT INTO fa_submissions (title) VALUES (?)", [(w,) for w in works])
        db.commit()
        db.close()
        (self.data / "settings.json").write_text("{}", encoding="utf-8")

    def titles(self):
        db = sqlite3.connect(self.data / "pawpoller.db")
        try:
            return [r[0] for r in db.execute("SELECT title FROM fa_submissions ORDER BY id")]
        finally:
            db.close()


@pytest.fixture
def two(tmp_path, monkeypatch):
    old, new = Install(tmp_path / "old", works=("Sample Story", "Second Piece")), Install(tmp_path / "new", works=())
    current = {}

    def use(inst):
        current["i"] = inst
        monkeypatch.setattr(config, "DATA_DIR", inst.data)
        monkeypatch.setattr(config, "DB_PATH", inst.data / "pawpoller.db")
        monkeypatch.setattr(config, "SETTINGS_PATH", inst.data / "settings.json")
        monkeypatch.setattr(config, "VAULT_PATH", inst.data / "settings.vault.json")
        monkeypatch.setenv("PAWPOLLER_VAULT_KEY", inst.key)
        monkeypatch.setattr(config, "_vault_unreadable", False)

    from posting import artwork_reader, story_reader
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: current["i"].data / "artwork")
    monkeypatch.setattr(story_reader, "get_archive_path", lambda: current["i"].root / "stories")
    monkeypatch.setattr(move_api, "_in_container", lambda: False)

    # The old install: a library, two site logins, its own sign-in and machine settings.
    use(old)
    (old.data / "artwork" / "Piece").mkdir()
    (old.data / "artwork" / "Piece" / "img.png").write_bytes(b"PNG-old")
    (old.root / "stories" / "Sample Story").mkdir()
    (old.root / "stories" / "Sample Story" / "story.md").write_text("Once.", encoding="utf-8")
    config.save_settings({
        "fa_cookie_a": "fa-secret-a", "ws_api_key": "ws-secret", "theme": "dark", "polling_paused": False,
        "posting_enabled": True, "auth_username": "olduser", "auth_password_hash": "OLD-HASH",
        "auth_email": "owner@example.com", "auto_backup_enabled": False, "auto_backup_keep": 7,
        "posting_server_url": "https://paired.example", "posting_story_archive_path": "C:/elsewhere",
    })

    # The new install (a seat): its own sign-in, backups on, nothing else.
    use(new)
    (new.data / "artwork" / "SeatOnly").mkdir()
    (new.data / "artwork" / "SeatOnly" / "keep.png").write_bytes(b"PNG-seat")
    config.save_settings({"auth_username": "seatuser", "auth_password_hash": "SEAT-HASH",
                          "auth_email": "seat@example.com", "auto_backup_enabled": True, "auto_backup_keep": 14,
                          "legal_accepted": {"terms": 2}})

    app = FastAPI()
    app.include_router(move_api.move_router)
    return old, new, use, TestClient(app)


def _make(old, use, tmp_path, phrase=PHRASE):
    use(old)
    dest = tmp_path / "move.zip"
    move_api.write_move_zip(dest, phrase)
    return dest


def _bring_in(c, data: bytes, phrase=PHRASE, piece=None):
    st = c.post("/api/move/uploads", json={"name": "move.zip", "size": len(data)}).json()
    step = piece or move_api.PIECE_BYTES
    for off in range(0, len(data), step):
        r = c.put(f"/api/move/uploads/{st['id']}?offset={off}", content=data[off:off + step])
        assert r.status_code == 200, r.text
    return st["id"], _apply(c, st["id"], phrase)


class _Done:
    """The finished background apply, shaped like a response (200 done, 500 failed)."""
    def __init__(self, body):
        self.status_code, self._body, self.text = (200 if body["state"] == "done" else 500), body, str(body)

    def json(self):
        return self._body


def _apply(c, uid, phrase=PHRASE):
    r = c.post(f"/api/move/uploads/{uid}/apply", json={"phrase": phrase, "confirm": True})
    if r.status_code != 200:
        return r                                   # refused up front (wrong phrase, bad file …)
    for _ in range(600):
        body = c.get("/api/move/apply").json()
        if body["state"] != "applying":
            return _Done(body)
        time.sleep(0.05)
    raise AssertionError("the apply never finished")


def _fingerprint(inst):
    return {p.relative_to(inst.root).as_posix(): p.read_bytes()
            for p in inst.root.rglob("*") if p.is_file() and "move-in" not in p.parts}


# ── US1: the move ────────────────────────────────────────────────────────────

def test_the_moving_file_leaves_the_old_machine_behind(two, tmp_path):
    old, _new, use, _c = two
    z = zipfile.ZipFile(_make(old, use, tmp_path))
    names = set(z.namelist())
    assert {"manifest.json", "logins.sealed", "data/pawpoller.db", "data/settings.json",
            "data/artwork/Piece/img.png", "data/story-archive/Sample Story/story.md"} <= names
    plain = json.loads(z.read("data/settings.json"))
    assert plain["theme"] == "dark"
    for k in ("auth_username", "auth_password_hash", "auth_email", "fa_cookie_a", "posting_server_url",
              "auto_backup_enabled", "posting_story_archive_path"):
        assert k not in plain
    raw = z.read("logins.sealed").decode()
    assert "fa-secret-a" not in raw and "OLD-HASH" not in raw
    assert json.loads(z.read("manifest.json"))["logins"] == "sealed"


def test_round_trip_keeps_the_seats_sign_in_and_brings_the_library(two, tmp_path):
    old, new, use, c = two
    data = _make(old, use, tmp_path).read_bytes()
    use(new)
    _id, r = _bring_in(c, data, piece=4096)   # many small pieces: the >100 MB path in miniature
    assert r.status_code == 200, r.text
    assert r.json()["restarting"] is False and r.json()["safety_copy"].startswith("restore-safety-")

    s = config.get_settings()
    assert s["auth_username"] == "seatuser" and s["auth_password_hash"] == "SEAT-HASH"
    assert s["auth_email"] == "seat@example.com" and s["legal_accepted"] == {"terms": 2}
    assert s["auto_backup_enabled"] is True and s["auto_backup_keep"] == 14
    assert "posting_server_url" not in s and "posting_story_archive_path" not in s
    assert s["fa_cookie_a"] == "fa-secret-a" and s["ws_api_key"] == "ws-secret" and s["theme"] == "dark"
    # The logins are locked under the SEAT's key: the vault opens with it and not with the old one.
    vault = json.loads((new.data / "settings.vault.json").read_text())["encrypted"].encode()
    assert "fa-secret-a" in Fernet(new.key.encode()).decrypt(vault).decode()
    with pytest.raises(InvalidToken):
        Fernet(old.key.encode()).decrypt(vault)

    assert new.titles() == ["Sample Story", "Second Piece"]
    assert (new.data / "artwork" / "Piece" / "img.png").read_bytes() == b"PNG-old"
    assert (new.data / "artwork" / "SeatOnly" / "keep.png").read_bytes() == b"PNG-seat"   # never deleted
    assert (new.root / "stories" / "Sample Story" / "story.md").read_text() == "Once."
    assert not (new.data / "move-in" / _id).exists()


def test_the_live_database_is_replaced_under_an_open_connection(two, tmp_path):
    old, new, use, c = two
    data = _make(old, use, tmp_path).read_bytes()
    use(new)
    held = sqlite3.connect(new.data / "pawpoller.db")     # a poller holding the DB open
    held.execute("SELECT 1").fetchone()
    try:
        assert _bring_in(c, data)[1].status_code == 200
        assert [r[0] for r in held.execute("SELECT title FROM fa_submissions ORDER BY id")] == \
            ["Sample Story", "Second Piece"]
    finally:
        held.close()


def test_a_seat_restarts_itself(two, tmp_path, monkeypatch):
    old, new, use, c = two
    data = _make(old, use, tmp_path).read_bytes()
    use(new)
    restarted = []
    monkeypatch.setattr(move_api, "_in_container", lambda: True)
    monkeypatch.setattr(move_api, "_restart_soon", lambda: restarted.append(1))
    assert _bring_in(c, data)[1].json()["restarting"] is True and restarted == [1]


def test_export_runs_in_the_background_and_can_be_downloaded_again(two, tmp_path):
    old, _new, use, c = two
    use(old)
    assert c.post("/api/move/export", json={"phrase": "short", "pause": False}).status_code == 400
    assert c.post("/api/move/export", json={"phrase": PHRASE, "pause": False}).json()["state"] == "building"
    for _ in range(100):
        st = c.get("/api/move/export").json()
        if st["state"] != "building":
            break
        time.sleep(0.05)
    assert st["state"] == "ready" and st["logins"] == "sealed" and "file" not in st
    r = c.get("/api/move/export/file")
    assert r.status_code == 200 and zipfile.ZipFile(io.BytesIO(r.content)).read("manifest.json")
    assert c.get("/api/move/export/file").content == r.content     # a lost download can be fetched again


def test_an_api_key_cannot_move(two):
    _old, _new, _use, c = two
    r = c.post("/api/move/export", json={"phrase": PHRASE}, headers={"Authorization": "Bearer pp_x"})
    assert r.status_code == 403


def test_a_folder_that_is_two_things_travels_once(two, tmp_path, monkeypatch):
    old, _new, use, _c = two
    use(old)
    from posting import story_reader
    (old.data / "stories" / "S").mkdir(parents=True)
    (old.data / "stories" / "S" / "a.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(story_reader, "get_archive_path", lambda: old.data / "stories")
    dest = tmp_path / "m.zip"
    move_api.write_move_zip(dest, PHRASE)
    names = [n for n in zipfile.ZipFile(dest).namelist() if n.endswith("a.md")]
    assert names == ["data/story-archive/S/a.md"]


def test_unreadable_logins_travel_as_none_and_say_so(two, tmp_path, monkeypatch):
    old, _new, use, _c = two
    use(old)
    monkeypatch.setenv("PAWPOLLER_VAULT_KEY", Fernet.generate_key().decode())   # the old key is lost
    dest = tmp_path / "m.zip"
    move_api.write_move_zip(dest, PHRASE)
    z = zipfile.ZipFile(dest)
    assert json.loads(z.read("manifest.json"))["logins"] == "unreadable"
    assert "logins.sealed" not in z.namelist()


# ── US2: failures change nothing ─────────────────────────────────────────────

def test_wrong_phrase_changes_nothing(two, tmp_path):
    old, new, use, c = two
    data = _make(old, use, tmp_path).read_bytes()
    use(new)
    before = _fingerprint(new)
    uid, r = _bring_in(c, data, phrase="not the right phrase")
    assert r.status_code == 403 and "phrase" in r.json()["detail"]
    assert c.post(f"/api/move/uploads/{uid}/check", json={"phrase": "nope nope nope"}).status_code == 403
    assert _fingerprint(new) == before
    # …and the right phrase still works without uploading again.
    ok = c.post(f"/api/move/uploads/{uid}/check", json={"phrase": PHRASE}).json()
    assert ok["logins"] == "sealed" and ok["here_has_work"] is True     # the seat has its own artwork


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in members.items():
            z.writestr(n, b)
    return buf.getvalue()


@pytest.mark.parametrize("members, status", [
    ({"manifest.json": json.dumps({"kind": "pawpoller-backup"}), "data/pawpoller.db": "x"}, 400),
    ({"manifest.json": json.dumps({"kind": "pawpoller-move", "v": 1, "app_version": "4.0.0"}),
      "data/pawpoller.db": "x", "data/settings.json": "{}", "data/artwork/../../evil.txt": "x"}, 400),
    ({"manifest.json": json.dumps({"kind": "pawpoller-move", "v": 1, "app_version": "4.0.0"}),
      "data/pawpoller.db": "x", "data/settings.json": "{}", "data/settings.vault.json": "x"}, 400),
    ({"manifest.json": json.dumps({"kind": "pawpoller-move", "v": 1, "app_version": "99.0.0"}),
      "data/pawpoller.db": "x", "data/settings.json": "{}"}, 409),
])
def test_wrong_files_are_refused_and_change_nothing(two, members, status):
    _old, new, use, c = two
    use(new)
    before = _fingerprint(new)
    _uid, r = _bring_in(c, _zip(members))
    assert r.status_code == status, r.text
    assert _fingerprint(new) == before
    if status == 409:
        assert "99.0.0" in r.json()["detail"] and config.APP_VERSION in r.json()["detail"]


def test_not_a_zip_at_all(two):
    _old, new, use, c = two
    use(new)
    assert _bring_in(c, b"definitely not a zip")[1].status_code == 400


def test_upload_resumes_and_stays_in_step(two):
    _old, new, use, c = two
    use(new)
    data = b"x" * 10000
    st = c.post("/api/move/uploads", json={"name": "a.zip", "size": len(data)}).json()
    c.put(f"/api/move/uploads/{st['id']}?offset=0", content=data[:4000])
    # A repeated piece (the reply was lost) is refused with where to carry on from.
    r = c.put(f"/api/move/uploads/{st['id']}?offset=0", content=data[:4000])
    assert r.status_code == 409 and r.json()["detail"]["received"] == 4000
    # Picking the same file again after a reload finds the same upload.
    again = c.post("/api/move/uploads", json={"name": "a.zip", "size": len(data)}).json()
    assert again["id"] == st["id"] and again["received"] == 4000
    # A different file waits for this one.
    other = c.post("/api/move/uploads", json={"name": "b.zip", "size": 5})
    assert other.status_code == 409 and other.json()["detail"]["id"] == st["id"]
    assert c.put(f"/api/move/uploads/{st['id']}?offset=4000", content=data[4000:] + b"extra").status_code == 413
    c.put(f"/api/move/uploads/{st['id']}?offset=4000", content=data[4000:])
    assert c.get(f"/api/move/uploads/{st['id']}").json()["received"] == len(data)
    assert c.delete(f"/api/move/uploads/{st['id']}").json()["ok"]
    assert c.get(f"/api/move/uploads/{st['id']}").status_code == 404


def test_apply_needs_the_whole_file_and_a_confirm(two):
    _old, new, use, c = two
    use(new)
    st = c.post("/api/move/uploads", json={"name": "a.zip", "size": 100}).json()
    assert c.post(f"/api/move/uploads/{st['id']}/apply", json={"phrase": PHRASE}).status_code == 400
    assert c.post(f"/api/move/uploads/{st['id']}/apply",
                  json={"phrase": PHRASE, "confirm": True}).status_code == 400   # not finished


def test_no_room_is_refused_up_front(two, monkeypatch):
    _old, new, use, c = two
    use(new)
    monkeypatch.setattr(move_api.shutil, "disk_usage", lambda _p: type("U", (), {"free": 10 ** 6})())
    r = c.post("/api/move/uploads", json={"name": "a.zip", "size": 10 ** 9})
    assert r.status_code == 507 and "GB" in r.json()["detail"]


def test_bad_upload_ids_are_not_paths(two):
    _old, _new, _use, c = two
    assert c.get("/api/move/uploads/..%2F..%2Fsettings").status_code == 404


def test_stale_moves_are_cleared_after_a_day(two):
    _old, new, use, c = two
    use(new)
    st = c.post("/api/move/uploads", json={"name": "a.zip", "size": 10}).json()
    assert move_api.prune_stale() == 0
    assert move_api.prune_stale(now=time.time() + move_api.STALE_SECONDS + 60) == 1
    assert not (new.data / "move-in" / st["id"]).exists()


def _export(c):
    for _ in range(200):
        st = c.get("/api/move/export").json()
        if st["state"] != "building":
            return st
        time.sleep(0.05)
    raise AssertionError("the export never finished")


def _hostile(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _upload(c, data: bytes) -> str:
    st = c.post("/api/move/uploads", json={"name": "x.zip", "size": len(data)}).json()
    assert c.put(f"/api/move/uploads/{st['id']}?offset=0", content=data).status_code == 200
    return st["id"]


def test_the_old_copy_pauses_before_the_snapshot(two, monkeypatch):
    """Anything posted while the file is built would still be pending in it and post twice."""
    old, _new, use, c = two
    use(old)
    seen = []
    real = move_api.write_move_zip
    monkeypatch.setattr(move_api, "write_move_zip",
                        lambda *a, **k: (seen.append(config.get_settings().get("posting_enabled")), real(*a, **k))[1])
    c.post("/api/move/export", json={"phrase": PHRASE, "pause": True})
    assert _export(c)["state"] == "ready" and seen == [False]
    plain = json.loads(zipfile.ZipFile(io.BytesIO(c.get("/api/move/export/file").content)).read("data/settings.json"))
    assert plain["posting_enabled"] is True                         # the file carries how it was


def test_a_failed_export_puts_the_old_copy_back(two, monkeypatch):
    old, _new, use, c = two
    use(old)

    def boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(move_api, "write_move_zip", boom)
    c.post("/api/move/export", json={"phrase": PHRASE, "pause": True})
    assert _export(c)["state"] == "failed"
    s = config.get_settings()
    assert s["posting_enabled"] is True and "moved_away_at" not in s


def test_a_different_file_with_the_same_name_is_kept_not_lost(two, tmp_path):
    old, new, use, c = two
    use(new)
    (new.data / "artwork" / "Piece").mkdir()
    (new.data / "artwork" / "Piece" / "img.png").write_bytes(b"PNG-seat-own")
    data = _make(old, use, tmp_path).read_bytes()
    use(new)
    r = _bring_in(c, data)[1]
    assert r.status_code == 200, r.text
    assert (new.data / "artwork" / "Piece" / "img.png").read_bytes() == b"PNG-old"
    kept = new.data / r.json()["safety_copy"] / "media" / "artwork" / "Piece" / "img.png"
    assert kept.read_bytes() == b"PNG-seat-own"


def test_a_post_mid_send_is_settled_not_stuck(two, tmp_path):
    old, new, use, c = two
    db = sqlite3.connect(old.data / "pawpoller.db")
    db.execute("CREATE TABLE posting_queue (queue_id INTEGER PRIMARY KEY, status TEXT, completed_at TEXT, last_error TEXT)")
    db.executemany("INSERT INTO posting_queue (status) VALUES (?)", [("processing",), ("pending",)])
    db.commit()
    db.close()
    data = _make(old, use, tmp_path).read_bytes()
    use(new)
    assert _bring_in(c, data)[1].status_code == 200
    db = sqlite3.connect(new.data / "pawpoller.db")
    rows = db.execute("SELECT status, last_error FROM posting_queue ORDER BY queue_id").fetchall()
    db.close()
    assert rows[0][0] == "failed" and "old PawPoller" in rows[0][1] and rows[1] == ("pending", None)


def test_the_trello_board_follows_the_move(two, tmp_path):
    old, new, use, c = two
    use(old)
    config.save_settings({"trello_instance_tag": "oldtag", "trello": {"owner_tag": "oldtag", "board": "b"}})
    data = _make(old, use, tmp_path).read_bytes()
    use(new)
    config.save_settings({"trello_instance_tag": "seattag"})
    assert _bring_in(c, data)[1].status_code == 200
    s = config.get_settings()
    assert s["trello"]["owner_tag"] == "seattag" and s["trello_instance_tag"] == "seattag"


def test_a_damaged_library_changes_nothing(two, tmp_path):
    old, new, use, c = two
    data = _make(old, use, tmp_path)
    z = zipfile.ZipFile(data)
    members = {n: z.read(n) for n in z.namelist()}
    members["data/pawpoller.db"] = b"SQLite format 3\x00" + b"\x00" * 200
    use(new)
    before = _fingerprint(new)
    r = _bring_in(c, _hostile(members))[1]
    assert r.status_code == 500 and "nothing was changed" in r.json()["error"]
    after = {k: v for k, v in _fingerprint(new).items() if "restore-safety" not in k}
    assert after == before


def test_a_huge_manifest_is_refused_without_reading_it(two):
    _old, new, use, c = two
    use(new)
    uid = _upload(c, _hostile({"manifest.json": b" " * (move_api._JSON_CAP + 1),
                               "data/pawpoller.db": b"", "data/settings.json": b"{}"}))
    assert c.post(f"/api/move/uploads/{uid}/check", json={"phrase": PHRASE}).status_code == 400


def test_a_file_cannot_ask_for_a_costly_unlock(two, tmp_path):
    old, new, use, c = two
    z = zipfile.ZipFile(_make(old, use, tmp_path))
    members = {n: z.read(n) for n in z.namelist()}
    sealed = json.loads(members["logins.sealed"])
    sealed["n"] = 2 ** 20
    members["logins.sealed"] = json.dumps(sealed).encode()
    use(new)
    uid = _upload(c, _hostile(members))
    assert c.post(f"/api/move/uploads/{uid}/check", json={"phrase": PHRASE}).status_code == 403


def test_a_moved_away_copy_leaves_trello_alone(two, monkeypatch):
    from polling import trello_mirror
    old, _new, use, _c = two
    use(old)
    monkeypatch.setattr(trello_mirror.mapping, "is_configured", lambda: True)
    monkeypatch.setattr(trello_mirror.mapping, "owner_state", lambda: (True, ""))
    assert trello_mirror._ready() is True
    move_api._pause_this_copy()
    assert trello_mirror._ready() is False


# ── US3: the old copy stands down ────────────────────────────────────────────

def test_pause_and_resume(two, tmp_path):
    old, new, use, c = two
    use(old)
    move_api._pause_this_copy()
    s = config.get_settings()
    assert s["polling_paused"] is True and s["posting_enabled"] is False and s["moved_away_at"]
    assert s["moved_away_restore"] == {"polling_paused": False, "posting_enabled": True, "auto_sync_enabled": False}
    # A moving file made while paused carries the settings as they were before the pause.
    dest = tmp_path / "again.zip"
    move_api.write_move_zip(dest, PHRASE)
    plain = json.loads(zipfile.ZipFile(dest).read("data/settings.json"))
    assert plain["polling_paused"] is False and plain["posting_enabled"] is True and "moved_away_at" not in plain
    assert c.post("/api/move/resume").json()["ok"]
    s = config.get_settings()
    assert s["polling_paused"] is False and s["posting_enabled"] is True and "moved_away_at" not in s
    assert old.titles() == ["Sample Story", "Second Piece"]      # nothing deleted


def test_every_kept_key_is_in_the_data_map():
    import datamap
    # run_on_startup is read from the OS, never stored, so the data map rightly doesn't list it.
    # auth_* is kept by prefix, including names old versions only listed and never stored.
    missing = [k for k in move_api.KEEP_HERE - {"run_on_startup"}
               if not k.startswith("auth_") and datamap.setting_class(k) is None]
    assert not missing, missing


def test_a_cancelled_upload_is_404_not_500(tmp_path):
    # SEC4880: a piece landing after cancel removed the folder crashed in _status.
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        move_api._status(tmp_path / "gone")
    assert e.value.status_code == 404
