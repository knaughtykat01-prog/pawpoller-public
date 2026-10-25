"""A stand-in PostyBirb 3 folder for the spec 060 tests (made-up content, stand-in names only).

Shapes follow PostyBirb 3's data model (specs/060-postybirb-seed/research.md R1). Built per
test into a temp folder rather than committed, because the Partitions cookie store is SQLite.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
OLD_HOME = "C:\\Users\\SomeoneElse\\Documents\\PostyBirb\\submission\\files\\"


def _nedb(path: Path, recs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")


def _cookie_db(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB)")
    c.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?)", rows)
    c.commit()
    c.close()


def _file_rec(name: str, preview: str = "") -> dict:
    return {"location": OLD_HOME + name, "preview": preview, "name": name, "mimetype": "image/png",
            "size": 72, "type": "IMAGE"}


def _part(pid, sid, acct, site, **data):
    return {"_id": pid, "submissionId": sid, "accountId": acct, "website": site, "isDefault": acct == "default",
            "postStatus": "UNPOSTED", "created": 1,
            "data": {"title": data.get("title"), "description": {"overwriteDefault": False, "value": data.get("desc", "")},
                     "tags": {"extendDefault": True, "value": data.get("tags", [])}, "rating": data.get("rating"),
                     "sources": []}}


def build(root: Path) -> tuple[Path, Path]:
    """Write the folder; returns (docs, app)."""
    docs, app = root / "PostyBirb", root / "postybirb-plus"
    files = docs / "submission" / "files"
    files.mkdir(parents=True)
    (docs / "submission" / "thumbnails").mkdir(parents=True)
    (files / "fox.png").write_bytes(PNG)
    (files / "moon.png").write_bytes(PNG + b"moon")
    (files / "wolf.jpg").write_bytes(JPG)
    (files / "extra.png").write_bytes(PNG + b"extra")
    (files / "spicy.png").write_bytes(PNG + b"spicy")
    (files / "clip.mp4").write_bytes(MP4)
    (docs / "submission" / "thumbnails" / "clip.jpg").write_bytes(JPG + b"poster")
    (files / "draw.svg").write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'><script>x</script></svg>")
    (files / "fake.png").write_bytes(b"<html><script>alert(1)</script></html>")
    (files / "theirs.png").write_bytes(PNG + b"theirs")
    (root / "canary.png").write_bytes(PNG + b"canary")      # outside the folder: must never be read
    subs = [
        {"_id": "s1", "title": "Sample Fox", "type": "FILE", "order": 1, "primary": _file_rec("fox.png"), "schedule": {}},
        {"_id": "s2", "title": "Moon Study", "type": "FILE", "order": 2, "primary": _file_rec("moon.png"),
         "additional": [_file_rec("extra.png")], "schedule": {}},
        {"_id": "s3", "title": "Wolf", "type": "FILE", "order": 3, "primary": _file_rec("wolf.jpg"), "schedule": {}},
        {"_id": "s4", "title": "Spicy", "type": "FILE", "order": 4, "primary": _file_rec("spicy.png"), "schedule": {}},
        {"_id": "s5", "title": "Gone", "type": "FILE", "order": 5, "primary": _file_rec("missing.png"), "schedule": {}},
        {"_id": "s6", "title": "Clip", "type": "FILE", "order": 6, "schedule": {},
         "primary": {**_file_rec("clip.mp4", OLD_HOME.replace("files", "thumbnails") + "clip.jpg"), "type": "VIDEO"}},
        {"_id": "s7", "title": "Vector", "type": "FILE", "order": 7, "primary": _file_rec("draw.svg"), "schedule": {}},
        {"_id": "s8", "title": "Fake", "type": "FILE", "order": 8, "primary": _file_rec("fake.png"), "schedule": {}},
        {"_id": "s9", "title": "Escape", "type": "FILE", "order": 9, "schedule": {},
         "primary": {**_file_rec("x"), "location": "../../canary.png"}},
        {"_id": "s10", "title": "Absolute", "type": "FILE", "order": 10, "schedule": {},
         "primary": {**_file_rec("x"), "location": str(root / "canary.png")}},
        {"_id": "s11", "title": "Theirs", "type": "FILE", "order": 11, "primary": _file_rec("theirs.png"), "schedule": {}},
        {"_id": "n1", "title": "A notice", "type": "NOTIFICATION", "order": 12, "schedule": {}},
    ]
    parts = [
        _part("p1", "s1", "default", "default", title="Sample Fox", desc="A fox, sketched.", tags=["fox", "sketch"],
              rating="general"),
        _part("p2", "s1", "fa-1", "FurAffinity", title="Sample Fox (FA)", desc="FA words", tags=["fa_tag"]),
        _part("p3", "s1", "fa-friend", "FurAffinity", title="Friend title", desc="friend words", tags=["friend"]),
        _part("p4", "s2", "default", "default", desc="Moon.", tags=["moon"], rating="mature"),
        _part("p5", "s4", "default", "default", desc="Spicy.", tags=["x"], rating="extreme"),
        _part("p6", "s11", "default", "default", desc="Not mine.", tags=["y"], rating="general"),
        _part("p7", "s11", "fa-friend", "FurAffinity", title="Theirs"),
    ]
    accounts = [
        {"_id": "fa-1", "website": "FurAffinity", "alias": "Penwright"},
        {"_id": "fa-friend", "website": "FurAffinity", "alias": "SecondHandle"},
        {"_id": "../x", "website": "FurAffinity", "alias": "Sneaky"},
    ]
    _nedb(docs / "data" / "submissions.db", subs)
    _nedb(docs / "data" / "submission-part.db", parts)
    _nedb(docs / "data" / "accounts.db", accounts)
    _nedb(docs / "data" / "description-template.db", [          # US3 (saved descriptions)
        {"_id": "dt1", "title": "Commission info", "content": "<p>Commissions <strong>open</strong>.</p><p>Ask &amp; see.</p>"},
        {"_id": "dt2", "title": "Thanks", "content": "Thanks for looking!"},
        {"_id": "../dt", "title": "Sneaky", "content": "never read"},
    ])
    _nedb(docs / "data" / "tag-group.db", [                     # US3 (tag groups)
        {"_id": "tg1", "alias": "Wolves", "tags": ["wolf", "canine", "Wolf", " "]},
        {"_id": "tg2", "alias": "Moon", "tags": ["moon", "night"]},
    ])
    for pid in ("fa-1", "fa-friend"):
        _cookie_db(app / "Partitions" / pid / "Network" / "Cookies",
                   [(".furaffinity.net", "a", f"fake-a-{pid}", b""), (".furaffinity.net", "b", f"fake-b-{pid}", b"")])
    (app / "Partitions" / "fa-1" / "Local Storage").mkdir(parents=True)
    (app / "Partitions" / "fa-1" / "Local Storage" / "leveldb.log").write_bytes(b"other-site-token")
    return docs, app
