"""What an image-board account follows: its uploads, its artist tag, its characters (spec 028).

On e621 and Furbooru most of an artist's presence is uploaded by other people and carries the artist's
tag; art of a writer's characters carries the character's tag. Each board account gets track settings
in the ``board_track`` setting, keyed by account id; an account with no entry tracks its own uploads
only, exactly as before this existed.

One search covers every ticked box (e621 ``~`` OR, Furbooru ``||``). Each post records why it matched
(``match_reasons``) and whether the account uploaded it (``uploaded_by_me``). When the account's
**Count others' uploads** is off, posts someone else uploaded live in ``{code}_found`` /
``{code}_found_snapshots`` (mirrors built in ``database/db.py``), which no total reads.
"""
from __future__ import annotations

import json
import re
import sqlite3

import config

KEY = "board_track"
BOARDS = ("e621", "fbr", "r34")
E621_TAG_LIMIT = 38          # e621 allows 40 tags a search; leave room
MAX_TAGS = 10
_TAG_RE = re.compile(r"^[a-z0-9_().'\-:~ !?&+/]{1,64}$")

DEFAULTS = {"artist_tags": [], "artist_verified": False, "characters": False, "count_others": True}


def norm_tag(code: str, tag: str) -> str:
    """A tag in the board's own form: e621 lower case with underscores; Furbooru lower case with
    spaces, artist tags as ``artist:<name>``."""
    t = " ".join(str(tag or "").strip().lower().split())
    if code in ("e621", "r34"):
        return t.replace(" ", "_")
    t = t.replace("_", " ")
    return t


def validate_tags(code: str, tags) -> list[str]:
    out: list[str] = []
    for raw in tags or []:
        t = norm_tag(code, raw)
        if not t:
            continue
        if not _TAG_RE.match(t):
            raise ValueError(f"'{raw}' isn't a tag this site uses")
        if code == "fbr" and ":" not in t:
            t = "artist:" + t
        if t not in out:
            out.append(t)
    if len(out) > MAX_TAGS:
        raise ValueError(f"At most {MAX_TAGS} artist tags")
    return out


def get(account_id: int, settings: dict | None = None) -> dict:
    """The account's settings with defaults filled in. ``saved`` says the owner has chosen."""
    s = settings if settings is not None else config.get_settings()
    raw = (s.get(KEY) or {}).get(str(account_id)) or {}
    out = {**DEFAULTS, **raw}
    out["saved"] = bool(raw.get("saved_at"))
    return out


def update(account_id: int, **changes) -> dict:
    s = config.get_settings()
    allt = dict(s.get(KEY) or {})
    cur = dict(allt.get(str(account_id)) or {})
    cur.update(changes)
    allt[str(account_id)] = cur
    config.save_settings({KEY: allt})
    return cur


def my_characters(conn: sqlite3.Connection, account_id: int) -> tuple[list[dict], list[dict]]:
    """(tagged, untagged) characters belonging to the account's persona, or to any of the owner's
    personas when the account has none."""
    row = conn.execute("SELECT persona_id FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
    persona = row[0] if row else None
    if persona:
        rows = conn.execute("SELECT character_key, name, booru_tag FROM characters WHERE persona_id = ?"
                            " ORDER BY name", (persona,)).fetchall()
    else:
        rows = conn.execute("SELECT character_key, name, booru_tag FROM characters"
                            " WHERE persona_id IS NOT NULL ORDER BY name").fetchall()
    tagged = [{"key": r[0], "name": r[1], "tag": r[2].strip()} for r in rows if (r[2] or "").strip()]
    untagged = [{"key": r[0], "name": r[1]} for r in rows if not (r[2] or "").strip()]
    return tagged, untagged


def plan(conn: sqlite3.Connection, code: str, account_id: int, username: str,
         settings: dict | None = None) -> dict:
    """What this check searches for: {queries, artist_tags, char_tags {tag: key}, extras, signature}.

    Uploads only → the exact query PawPoller always used (``queries`` None = the client's default)."""
    t = get(account_id, settings)
    artist = list(t["artist_tags"])
    chars: dict[str, str] = {}
    if t["characters"]:
        for c in my_characters(conn, account_id)[0]:
            chars.setdefault(norm_tag(code, c["tag"]), c["key"])
    extras = bool(artist or chars)
    out = {"artist_tags": artist, "char_tags": chars, "extras": extras, "count_others": t["count_others"],
           "queries": None, "settings": t}
    if extras:
        out["queries"] = (e621_queries(username, artist, list(chars)) if code == "e621"
                          # Rule34: one search per tag; the client adds the uploads search itself.
                          else artist + list(chars) if code == "r34"
                          else [fbr_query(username, artist, list(chars))])
    out["signature"] = json.dumps(sorted(set(artist) | set(chars)))
    return out


def e621_queries(username: str, artist_tags: list[str], char_tags: list[str]) -> list[str]:
    """``~user:<me> ~<artist> ~<char>…`` in chunks under e621's tag limit; a lone term has no ``~``."""
    terms = [f"user:{username}"] + artist_tags + char_tags
    out = []
    for i in range(0, len(terms), E621_TAG_LIMIT):
        chunk = terms[i:i + E621_TAG_LIMIT]
        out.append(chunk[0] if len(chunk) == 1 else " ".join("~" + x for x in chunk))
    return out


def _fbr_term(t: str) -> str:
    return f'"{t}"' if re.search(r'[(),"]', t) else t


def fbr_query(username: str, artist_tags: list[str], char_tags: list[str]) -> str:
    """Philomena OR search: ``uploaded_by:<me> || artist:<name> || <character tag>``."""
    return " || ".join([f"uploaded_by:{username}"] + [_fbr_term(t) for t in artist_tags + char_tags])


def reasons(code: str, detail: dict, p: dict, username: str, my_id: str = "") -> tuple[list[str], int]:
    """Why a post is here, read from the post itself, and whether the account uploaded it."""
    if not p["extras"]:
        return ["upload"], 1
    if code in ("e621", "r34"):
        mine = bool(my_id) and str(detail.get("uploader_id") or "") == str(my_id)
        if code == "r34" and not detail.get("uploader_id"):     # some replies name the owner instead
            mine = (detail.get("uploader_name") or "").lower() == (username or "").lower() != ""
    else:
        mine = (detail.get("uploader_name") or "").lower() == (username or "").lower() != ""
    tags = {norm_tag(code, k) for k in detail.get("keywords") or []}
    out = ["upload"] if mine else []
    out += [f"artist:{a}" for a in p["artist_tags"] if a in tags]
    out += [f"character:{key}" for tag, key in p["char_tags"].items() if tag in tags]
    return out, int(mine)


def where(conn: sqlite3.Connection, code: str, submission_id: str) -> tuple[str, int] | None:
    """The table already holding this post and the account it's credited to, if any."""
    for table in (f"{code}_submissions", f"{code}_found"):
        r = conn.execute(f"SELECT account_id FROM {table} WHERE submission_id = ?", (submission_id,)).fetchone()
        if r:
            return table, r[0]
    return None


def store(conn: sqlite3.Connection, code: str, detail: dict, account_id: int, p: dict,
          username: str, my_id: str, polled_at: str, quiet: bool) -> dict:
    """Write one found post and its snapshot to the right table. Returns {new, mine, table}.

    A post another account already holds stays credited to that account (stored once) and gets no
    second snapshot from this one. ``quiet`` (the first check of a search) never stamps found_at,
    so the bell only hears about posts that appear after tracking settled."""
    import importlib
    q = importlib.import_module(f"database.{code}_queries")
    sid = detail["post_uri"]
    why, mine = reasons(code, detail, p, username, my_id)
    held = where(conn, code, sid)
    if held and held[1] != account_id:
        table = held[0]
        getattr(q, f"upsert_{code}_submission")(conn, detail, held[1], table=table)
        return {"new": False, "mine": mine, "table": table, "shared": True, "reasons": why}
    if held:
        table = held[0]
    else:
        table = f"{code}_found" if (not mine and not p["count_others"]) else f"{code}_submissions"
    getattr(q, f"upsert_{code}_submission")(conn, detail, account_id, table=table)
    found_at = polled_at if (not held and not mine and not quiet) else ""
    conn.execute(f"UPDATE {table} SET match_reasons = ?, uploaded_by_me = ?, uploader_name = ?,"
                 f" found_at = CASE WHEN found_at = '' THEN ? ELSE found_at END WHERE submission_id = ?",
                 (json.dumps(why), mine, detail.get("uploader_name") or "", found_at, sid))
    snaps = table.replace("_submissions", "_snapshots").replace("_found", "_found_snapshots")
    getattr(q, f"insert_{code}_snapshot")(
        conn, account_id, sid, detail.get("score", 0), detail.get("favorites_count", 0),
        detail.get("comments_count", 0), polled_at=polled_at,
        up_score=detail.get("up_score", 0), down_score=detail.get("down_score", 0), table=snaps)
    return {"new": not held, "mine": mine, "table": table, "shared": False, "found_at": found_at, "reasons": why}


async def prepare(conn: sqlite3.Connection, code: str, client, account_id: int, username: str,
                  settings: dict | None = None) -> dict:
    """The plan for this check plus the own uploader id, the quiet flag and light paging."""
    from datetime import datetime, timedelta, timezone
    p = plan(conn, code, account_id, username, settings)
    t = p["settings"]
    p.update(my_id="", known=None, full=True, quiet=False)
    if not p["extras"]:
        return p
    if code == "e621":
        p["my_id"] = t.get("user_id") or await client.get_user_id()
        if p["my_id"] and not t.get("user_id"):
            update(account_id, user_id=p["my_id"])
    p["quiet"] = t.get("seeded") != p["signature"]
    try:
        last_full = datetime.fromisoformat(t.get("full_at") or "")
    except ValueError:
        last_full = None
    p["full"] = p["quiet"] or last_full is None or datetime.now(timezone.utc) - last_full > timedelta(hours=24)
    if not p["full"]:
        p["known"] = known_ids(conn, code, account_id)
    return p


def finish(account_id: int, p: dict) -> None:
    """After a completed check: the search is settled (later finds notify); note a full read."""
    if not p["extras"]:
        return
    from datetime import datetime, timezone
    changes = {"seeded": p["signature"]}
    if p["full"]:
        changes["full_at"] = datetime.now(timezone.utc).isoformat()
    update(account_id, **changes)


async def notify(code: str, found: list[dict]) -> None:
    """One Telegram line per post someone else uploaded that this check found (if Telegram is on)."""
    if not found:
        return
    from html import escape
    from polling import notifications
    s = config.get_settings()
    lines = [escape(describe({"platform": code, **f})) for f in found]
    await notifications.maybe_send_telegram_summary(
        s, f"<b>\U0001f50e Found {len(found)} new post{'s' if len(found) != 1 else ''}</b>", lines,
        log_label=f"{code} found")


def inbox_wanted(result: dict) -> bool:
    """Comments are captured for your uploads and your artist tag's posts, not others' art of your
    characters (those conversations aren't yours to answer)."""
    return bool(result.get("mine")) or any(r.startswith("artist:") for r in result.get("reasons") or [])


def known_ids(conn: sqlite3.Connection, code: str, account_id: int) -> set[str]:
    out: set[str] = set()
    for table in (f"{code}_submissions", f"{code}_found"):
        out |= {r[0] for r in conn.execute(f"SELECT submission_id FROM {table} WHERE account_id = ?",
                                           (account_id,))}
    return out


def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def move(conn: sqlite3.Connection, code: str, account_id: int, to_found: bool) -> int:
    """Move the account's posts by other uploaders, with their snapshots, between the main and found
    tables. Nothing is lost: each row is copied before the source copy goes."""
    main, found = f"{code}_submissions", f"{code}_found"
    msnap, fsnap = f"{code}_snapshots", f"{code}_found_snapshots"
    src, dst, ssrc, sdst = (main, found, msnap, fsnap) if to_found else (found, main, fsnap, msnap)
    ids = [r[0] for r in conn.execute(f"SELECT submission_id FROM {src} WHERE account_id = ? AND uploaded_by_me = 0",
                                      (account_id,))]
    if not ids:
        return 0
    cols = ", ".join(c for c in _cols(conn, src) if c in set(_cols(conn, dst)))
    scols = ", ".join(c for c in _cols(conn, ssrc) if c != "id" and c in set(_cols(conn, sdst)))
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        qs = ",".join("?" * len(part))
        conn.execute(f"INSERT OR REPLACE INTO {dst} ({cols}) SELECT {cols} FROM {src} WHERE submission_id IN ({qs})", part)
        conn.execute(f"INSERT INTO {sdst} ({scols}) SELECT {scols} FROM {ssrc} WHERE submission_id IN ({qs})", part)
        conn.execute(f"DELETE FROM {ssrc} WHERE submission_id IN ({qs})", part)
        conn.execute(f"DELETE FROM {src} WHERE submission_id IN ({qs})", part)
    return len(ids)


def found_summary(conn: sqlite3.Connection, code: str, account_id: int | None = None) -> dict:
    """The "Found on <site>" figure: posts kept apart from the totals."""
    where_sql, args = ("WHERE account_id = ?", (account_id,)) if account_id else ("", ())
    r = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(score), 0), COALESCE(SUM(favorites_count), 0),"
                     f" COALESCE(SUM(comments_count), 0) FROM {code}_found {where_sql}", args).fetchone()
    return {"posts": r[0], "score": r[1], "favorites": r[2], "comments": r[3]}


def recent_found(conn: sqlite3.Connection, since: str, limit: int = 20) -> list[dict]:
    """Posts by other people first found after tracking settled, newest first (the bell's items)."""
    out = []
    for code in BOARDS:
        for table in (f"{code}_submissions", f"{code}_found"):
            try:
                rows = conn.execute(f"SELECT submission_id, title, link, match_reasons, uploader_name, found_at"
                                    f" FROM {table} WHERE found_at != '' AND found_at >= ?"
                                    f" ORDER BY found_at DESC LIMIT ?", (since, limit)).fetchall()
            except sqlite3.OperationalError:
                continue
            out += [{"platform": code, "submission_id": r[0], "title": r[1], "url": r[2],
                     "reasons": json.loads(r[3] or "[]"), "uploader": r[4], "found_at": r[5]} for r in rows]
    out.sort(key=lambda x: x["found_at"], reverse=True)
    return out[:limit]


def labels(reasons_json: str, names: dict) -> list[str]:
    """Chip words for a post's reasons: "your upload", "your artist tag", "your character: <name>"."""
    try:
        rs = json.loads(reasons_json or "[]")
    except ValueError:
        rs = []
    out = []
    for r in rs:
        if r == "upload":
            out.append("your upload")
        elif r.startswith("artist:"):
            out.append("your artist tag")
        elif r.startswith("character:"):
            key = r.split(":", 1)[1]
            out.append(f"your character: {names.get(key, key)}")
    return list(dict.fromkeys(out))


def add_labels(conn: sqlite3.Connection, rows: list[dict]) -> None:
    names = {r[0]: r[1] for r in conn.execute("SELECT character_key, name FROM characters")}
    for row in rows:
        row["match_labels"] = labels(row.get("match_reasons") or "[]", names)


def describe(item: dict, names: dict | None = None) -> str:
    """Plain words for a found post: "art of ThirdFur" / "your artist tag"."""
    names = names or {}
    site = {"e621": "e621", "fbr": "Furbooru", "r34": "Rule34"}.get(item["platform"], item["platform"])
    chars = [names.get(r.split(":", 1)[1], r.split(":", 1)[1]) for r in item["reasons"] if r.startswith("character:")]
    what = f"art of {', '.join(chars)}" if chars else "a post with your artist tag"
    by = f" uploaded by {item['uploader']}" if item.get("uploader") else ""
    return f"New on {site}: {what}{by}"
