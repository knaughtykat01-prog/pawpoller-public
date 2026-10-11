"""Bring logins over from PostyBirb (4.69.0, spec 033 phase 8 — was PBIMPORT).

Desktop only, and strictly read-only on PostyBirb's files: each one is copied to a
temp folder and the copy is opened (``mode=ro``). PostyBirb 3 ("postybirb-plus")
keeps its accounts in a NeDB file — one JSON object per line — and each account's
browser cookies in its own Electron partition, unencrypted. ``scan()`` reports
what it found with no secret values; ``apply(ids)`` re-reads the ticked ones and
writes them into the same per-account keys each Accounts form saves.

Only an empty default account or a brand-new account is ever written, so an
account that already has a login (a friend's included) is never touched. The log
gets site names and counts, never values.

ponytail: PostyBirb 4 (SQLite ``database-production.sqlite``) is reported as found
but not imported — its cookie store wasn't verified against real data. Add a
reader when someone has v4 accounts to test with.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# PostyBirb's website name → what PawPoller does with it.
_COOKIE_SITES = {           # site: (platform, cookie host, {cookie name: credential key} or "string" key)
    "FurAffinity": ("fa", "furaffinity.net", {"a": "fa_cookie_a", "b": "fa_cookie_b"}),
    "Newgrounds": ("ng", "newgrounds.com", "ng_cookie"),
    "DeviantArt": ("da", "deviantart.com", "da_cookie"),
}
_NOT_PORTABLE = {
    "SoFurry": "PawPoller signs in to SoFurry with an API token instead. Make one in SoFurry's settings.",
    "Weasyl": "PawPoller needs a Weasyl API key, which PostyBirb doesn't keep.",
    "Itaku": "Itaku's sign-in can't be read from PostyBirb. Paste your Itaku token in Accounts.",
    "Twitter": "Sign in to X from PawPoller's Accounts page.",
    "Instagram": "Connect Instagram from PawPoller's Accounts page.",
    "Mastodon": "Connect Mastodon from PawPoller's Accounts page.",
}


def _dirs() -> tuple[Path, Path]:
    docs = os.environ.get("PAWPOLLER_PB_DIR") or str(Path.home() / "Documents" / "PostyBirb")
    app = os.environ.get("PAWPOLLER_PB_APPDATA") or str(Path(os.environ.get("APPDATA") or Path.home()) / "postybirb-plus")
    return Path(docs), Path(app)


def present() -> bool:
    docs, _ = _dirs()
    return (docs / "data" / "accounts.db").exists() or (docs / "data" / "database-production.sqlite").exists()


class Locked(Exception):
    pass


def _copy(src: Path, tmp: str) -> Path:
    dst = Path(tmp) / f"{len(os.listdir(tmp))}_{src.name}"
    try:
        shutil.copyfile(src, dst)
    except PermissionError as e:
        raise Locked() from e
    return dst


def _records(tmp: str) -> list[dict]:
    docs, _ = _dirs()
    path = docs / "data" / "accounts.db"
    if not path.exists():
        return []
    out = {}
    for line in _copy(path, tmp).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("_id"):
            if o.get("$$deleted"):
                out.pop(o["_id"], None)      # NeDB appends a tombstone line on delete
            else:
                out[o["_id"]] = o            # ...and a whole new line on update: last one wins
    return list(out.values())


def _cookies(pb_id: str, host: str, tmp: str) -> dict:
    _, app = _dirs()
    for sub in ("Network/Cookies", "Cookies"):
        p = app / "Partitions" / pb_id / sub
        if p.exists():
            conn = sqlite3.connect(f"file:{_copy(p, tmp).as_posix()}?mode=ro", uri=True)
            try:
                rows = conn.execute("SELECT host_key, name, value, length(encrypted_value) FROM cookies").fetchall()
            finally:
                conn.close()
            mine = [r for r in rows if r[0].lstrip(".") == host or r[0].endswith("." + host)]
            if any(r[3] and not r[2] for r in mine):
                raise ValueError("encrypted")
            return {r[1]: r[2] for r in mine if r[2]}
    return {}


def _read(rec: dict, tmp: str) -> dict:
    """{platform, creds, handle, sid} for one PostyBirb account, or {reason}."""
    site = str(rec.get("website") or "")
    data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
    name = str(rec.get("alias") or site)
    if site == "Bluesky":
        if data.get("username") and data.get("password"):
            return {"platform": "bsky", "handle": data["username"],
                    "creds": {"bsky_identifier": data["username"], "bsky_app_password": data["password"]}}
        return {"reason": "PostyBirb has no Bluesky password saved for this account."}
    if site == "e621":
        key = data.get("key") or data.get("apiKey") or data.get("api_key")
        if data.get("username") and key:
            return {"platform": "e621", "handle": data["username"],
                    "creds": {"e621_username": data["username"], "e621_api_key": key}}
        return {"reason": "PostyBirb has no e621 API key saved for this account."}
    if site == "Inkbunny":
        if data.get("sid"):
            return {"platform": "ib", "handle": data.get("username") or name,
                    "creds": {"username": data.get("username") or ""}, "sid": data["sid"]}
        return {"reason": "PostyBirb isn't signed in to Inkbunny on this account."}
    if site in _COOKIE_SITES:
        platform, host, keys = _COOKIE_SITES[site]
        try:
            jar = _cookies(str(rec["_id"]), host, tmp)
        except ValueError:
            return {"reason": "PostyBirb's saved sign-in for this site is encrypted and can't be read."}
        if isinstance(keys, dict):
            if not all(jar.get(c) for c in keys):
                return {"reason": "PostyBirb isn't signed in to this site any more. Sign in there, or paste cookies in Accounts."}
            creds = {k: jar[c] for c, k in keys.items()}
        else:
            if not jar:
                return {"reason": "PostyBirb isn't signed in to this site any more."}
            creds = {keys: "; ".join(f"{k}={v}" for k, v in jar.items())}
        return {"platform": platform, "handle": name, "creds": creds}
    return {"reason": _NOT_PORTABLE.get(site, f"PawPoller doesn't use {site}.")}


def _existing(conn, platform: str) -> list[dict]:
    from database import accounts as adb
    return adb.list_accounts(conn, platform)


def _duplicate(rows: list[dict], handle: str) -> bool:
    h = (handle or "").strip().lstrip("@").lower()
    return bool(h) and any(h in ((r.get("handle") or "").lstrip("@").lower(), (r.get("label") or "").lower()) for r in rows)


def scan() -> dict:
    """What PostyBirb has, and what can come over. No secret values in the answer."""
    if not present():
        return {"found": False, "accounts": []}
    docs, _ = _dirs()
    if not (docs / "data" / "accounts.db").exists():
        return {"found": True, "version": 4, "accounts": [],
                "note": "PostyBirb 4 was found. Bringing its logins over isn't supported yet."}
    from database.db import get_connection
    tmp = tempfile.mkdtemp(prefix="pp-pb-")
    out = []
    try:
        conn = get_connection()
        try:
            for rec in _records(tmp):
                got = _read(rec, tmp)
                row = {"pb_id": str(rec["_id"]), "site": str(rec.get("website") or ""),
                       "name": str(rec.get("alias") or ""), "platform": got.get("platform", ""),
                       "portable": "reason" not in got, "reason": got.get("reason", "")}
                if row["portable"] and _duplicate(_existing(conn, got["platform"]), got["handle"]):
                    row.update(portable=False, reason="Already in PawPoller.")
                out.append(row)
        finally:
            conn.close()
    except Locked:
        return {"found": True, "locked": True, "accounts": [],
                "note": "PostyBirb has its files open. Close PostyBirb and try again."}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    logger.info("PostyBirb scan: %d accounts, %d can come over", len(out), sum(r["portable"] for r in out))
    return {"found": True, "version": 3, "accounts": out}


def _target(conn, platform: str, handle: str) -> int:
    """An empty default account to fill, else a new one. Never an account with a login."""
    from database import accounts as adb
    default = adb.get_default_account_id(conn, platform)
    if default is None:
        return adb.create_account(conn, platform, handle, handle=handle, is_default=True)
    if not any(config.resolve_account_credentials(platform, default, True).values()):
        adb.update_account(conn, default, label=handle, handle=handle)
        return default
    return adb.create_account(conn, platform, handle, handle=handle)


def apply(ids: list[str]) -> list[dict]:
    """Bring the ticked accounts over. One result per id: imported / skipped / failed."""
    from database import accounts as adb, queries
    from database.db import get_connection
    want = {str(i) for i in ids or []}
    results = []
    tmp = tempfile.mkdtemp(prefix="pp-pb-")
    try:
        recs = [r for r in _records(tmp) if str(r["_id"]) in want]
        conn = get_connection()
        try:
            for rec in recs:
                res = {"pb_id": str(rec["_id"]), "site": str(rec.get("website") or ""), "name": str(rec.get("alias") or "")}
                got = _read(rec, tmp)
                if "reason" in got:
                    results.append({**res, "status": "failed", "message": got["reason"]})
                    continue
                if _duplicate(_existing(conn, got["platform"]), got["handle"]):
                    results.append({**res, "status": "skipped", "message": "Already in PawPoller."})
                    continue
                aid = _target(conn, got["platform"], got["handle"])
                acct = adb.get_account(conn, aid)
                config.save_settings({config.account_setting_key(aid, k, bool(acct["is_default"])): v
                                      for k, v in got["creds"].items()})
                if got.get("sid"):
                    queries.save_session(conn, aid, got["sid"], got["creds"].get("username", ""), 0)
                results.append({**res, "status": "imported", "account_id": aid, "platform": got["platform"]})
        finally:
            conn.close()
    except Locked:
        return [{"pb_id": i, "status": "failed", "message": "PostyBirb has its files open. Close it and try again."}
                for i in sorted(want)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    config.save_settings({"pb_import_asked": True})
    logger.info("PostyBirb import: %d brought over, %d skipped or failed",
                sum(r["status"] == "imported" for r in results), sum(r["status"] != "imported" for r in results))
    return results
