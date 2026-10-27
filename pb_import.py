"""Bring logins — and since 4.88.0 (spec 060) the submission library — over from PostyBirb.

Desktop only, and strictly read-only on PostyBirb's files: each one is copied to a
temp folder and the copy is opened (``mode=ro``). PostyBirb 3 ("postybirb-plus")
keeps its accounts in a NeDB file — one JSON object per line — and each account's
browser cookies in its own Electron partition, unencrypted. ``scan()`` reports
what it found with no secret values; ``apply(ids)`` re-reads the ticked ones and
writes them into the same per-account keys each Accounts form saves.

Only an empty default account or a brand-new account is ever written, so an
account that already has a login (a friend's included) is never touched. The log
gets site names and counts, never values.

Spec 060: every reader takes ``base`` — an unpacked upload on a server (``pb_upload``)
laid out as ``<base>/docs`` (the PostyBirb folder) and ``<base>/app`` (its Partitions).
Every path a record names is re-anchored by file name inside that folder (``_inside``),
so a record can never point at another file on the machine (FR-SEC-001). Logins and
per-site parts of a Never-post account are left out (FR-SEC-003).

ponytail: PostyBirb 4 (SQLite ``database-production.sqlite``) is reported as found
but not imported — its cookie store wasn't verified against real data. Add a
reader when someone has v4 accounts to test with.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from html.parser import HTMLParser
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


_PB_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")     # a PostyBirb id, before it is used in a path
_NEDB_CAP = 64 * 1024 * 1024                      # a PostyBirb data file is never read past this


def _dirs(base: Path | None = None) -> tuple[Path, Path]:
    if base is not None:
        return Path(base) / "docs", Path(base) / "app"
    docs = os.environ.get("PAWPOLLER_PB_DIR") or str(Path.home() / "Documents" / "PostyBirb")
    app = os.environ.get("PAWPOLLER_PB_APPDATA") or str(Path(os.environ.get("APPDATA") or Path.home()) / "postybirb-plus")
    return Path(docs), Path(app)


def present(base: Path | None = None) -> bool:
    docs, _ = _dirs(base)
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


def _inside(root: Path, recorded) -> Path | None:
    """The file a record names, found by its name inside *root*, or None (FR-SEC-001).

    PostyBirb stores absolute paths from the computer it ran on. Only the last part is
    used, and the result must resolve inside *root*: ``..``, another drive or a link out
    all come back None ("file missing")."""
    if not isinstance(recorded, str) or not recorded:
        return None
    name = recorded.replace("\\", "/").rsplit("/", 1)[-1]
    if name in ("", ".", "..") or name != name.strip():
        return None
    try:
        root_r = root.resolve()
        cand = (root / name).resolve()
    except (OSError, ValueError):
        return None
    if cand.parent != root_r or not cand.is_file():
        return None
    return cand


def _records(tmp: str, name: str = "accounts", base: Path | None = None) -> list[dict]:
    docs, _ = _dirs(base)
    path = docs / "data" / f"{name}.db"
    if not path.exists():
        return []
    if path.stat().st_size > _NEDB_CAP:
        raise ValueError("too big")
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


def _cookies(pb_id: str, host: str, tmp: str, base: Path | None = None) -> dict:
    if not _PB_ID.fullmatch(pb_id or ""):
        return {}
    _, app = _dirs(base)
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


def _read(rec: dict, tmp: str, base: Path | None = None) -> dict:
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
            jar = _cookies(str(rec["_id"]), host, tmp, base)
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


def _guarded_names(conn) -> set[str]:
    """Lower-cased names of every Never-post account: label, handle and the stored user
    name fields. A PostyBirb account with one of these names is someone else's (FR-SEC-003)."""
    from database import accounts as adb
    guard = adb.never_post_ids()
    if not guard:
        return set()
    names = set()
    for row in adb.list_accounts(conn):
        if int(row["account_id"]) not in guard:
            continue
        vals = [row.get("label"), row.get("handle")]
        creds = config.resolve_account_credentials(row["platform"], int(row["account_id"]), bool(row.get("is_default")))
        vals += [v for k, v in creds.items() if k.endswith(("username", "target_user", "user", "handle"))]
        names |= {str(v).strip().lstrip("@").lower() for v in vals if v}
    return names


def _guarded(rec: dict, names: set[str]) -> bool:
    data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
    vals = (rec.get("alias"), data.get("username"))
    return any(str(v).strip().lstrip("@").lower() in names for v in vals if v)


def scan(base: Path | None = None) -> dict:
    """What PostyBirb has, and what can come over. No secret values in the answer.
    Nothing is ticked for the person (FR-SEC-003): the screen starts every row unticked."""
    if not present(base):
        return {"found": False, "accounts": []}
    docs, _ = _dirs(base)
    if not (docs / "data" / "accounts.db").exists():
        return {"found": True, "version": 4, "accounts": [],
                "note": "PostyBirb 4 was found. Bringing its logins over isn't supported yet."}
    from database.db import get_connection
    tmp = tempfile.mkdtemp(prefix="pp-pb-")
    out = []
    try:
        conn = get_connection()
        try:
            guarded = _guarded_names(conn)
            for rec in _records(tmp, base=base):
                if _guarded(rec, guarded):
                    continue                     # a Never-post account's login is not even listed
                got = _read(rec, tmp, base)
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
    except ValueError:
        return {"found": True, "accounts": [], "note": "PostyBirb's account list is too big to read."}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    per_site: dict = {}
    for r in out:
        per_site[r["site"]] = per_site.get(r["site"], 0) + 1
    for r in out:
        r["check_mine"] = per_site[r["site"]] > 1      # "check each is yours"
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


def apply(ids: list[str], base: Path | None = None) -> list[dict]:
    """Bring the ticked accounts over. One result per id: imported / skipped / failed."""
    from database import accounts as adb, queries
    from database.db import get_connection
    want = {str(i) for i in ids or []}
    results = []
    tmp = tempfile.mkdtemp(prefix="pp-pb-")
    try:
        recs = [r for r in _records(tmp, base=base) if str(r["_id"]) in want]
        conn = get_connection()
        try:
            guarded = _guarded_names(conn)
            for rec in recs:
                res = {"pb_id": str(rec["_id"]), "site": str(rec.get("website") or ""), "name": str(rec.get("alias") or "")}
                if _guarded(rec, guarded):
                    results.append({**res, "status": "skipped", "message": "That account is marked Never post here."})
                    continue
                got = _read(rec, tmp, base)
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
    except ValueError:
        return [{"pb_id": i, "status": "failed", "message": "PostyBirb's account list is too big to read."}
                for i in sorted(want)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    config.save_settings({"pb_import_asked": True})
    logger.info("PostyBirb import: %d brought over, %d skipped or failed",
                sum(r["status"] == "imported" for r in results), sum(r["status"] != "imported" for r in results))
    return results


# ── spec 060: the submission library ──────────────────────────────────────────
# Shapes (research R1, from PostyBirb 3's own data model): ``submissions.db`` holds
# {_id, title, type: FILE|NOTIFICATION, primary: FileRecord, thumbnail?, additional?: [FileRecord]};
# a FileRecord is {location, preview, name, mimetype, type: IMAGE|VIDEO|AUDIO|TEXT|UNKNOWN}.
# ``submission-part.db`` holds {_id, submissionId, accountId, website, isDefault, postStatus, postedTo,
# data: {title?, description: {value}, tags: {value: [...]}, rating}}; the default part has
# website/accountId "default". Files live in submission/files, previews in submission/thumbnails.

# PostyBirb website name → PawPoller platform code, for per-site text and tags.
_SITE_CODES = {
    "FurAffinity": "fa", "DeviantArt": "da", "Newgrounds": "ng", "Inkbunny": "ib", "e621": "e621",
    "Bluesky": "bsky", "Twitter": "tw", "Itaku": "ik", "Weasyl": "ws", "SoFurry": "sf", "Mastodon": "mast",
    "Tumblr": "tum", "Furbooru": "fbr", "Instagram": "ig", "Telegram": "tg", "Pixiv": "pix",
    "FurryNetwork": "fn", "Picarto": "pic",
}
_RATINGS = {"general": "general", "mature": "mature", "adult": "adult", "extreme": "adult"}
_SIGNATURES = (                                 # first bytes of what PawPoller holds (FR-SEC-007)
    (b"\x89PNG\r\n\x1a\n", (".png",)), (b"\xff\xd8\xff", (".jpg", ".jpeg")),
    (b"GIF87a", (".gif",)), (b"GIF89a", (".gif",)),
)
PAGE = 100
_THUMBS: dict = {}                              # str(base) → {submission id: preview path}, from the last scan


def _kind(path: Path) -> str | None:
    """'image' or 'video' when the extension AND the file's own first bytes agree, else None."""
    ext = path.suffix.lower()
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    for sig, exts in _SIGNATURES:
        if head.startswith(sig) and ext in exts:
            return "image"
    if ext == ".webp" and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image"
    if ext in (".mp4", ".m4v", ".mov") and head[4:8] == b"ftyp":
        return "video"
    if ext == ".webm" and head[:4] == b"\x1a\x45\xdf\xa3":
        return "video"
    return None


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_LIB_HASHES: dict = {}   # Library file → (size, mtime, sha256): a page of the list doesn't re-read every file (SEC060)


def _library() -> tuple[set[str], set[str]]:
    """(sha256 of every Library piece's file, PostyBirb ids already imported). One pass per scan."""
    from posting import artwork_reader
    hashes, ids = set(), set()
    for a in artwork_reader.list_artworks():
        src = a.get("import_source") or {}
        if src.get("platform") == "postybirb" and src.get("submission_id"):
            ids.add(str(src["submission_id"]))
        img = Path(a["path"]) / (a.get("image") or "")
        if a.get("image") and img.is_file():
            try:
                st = img.stat()
                seen = _LIB_HASHES.get(str(img))
                if not seen or seen[:2] != (st.st_size, st.st_mtime_ns):
                    seen = _LIB_HASHES[str(img)] = (st.st_size, st.st_mtime_ns, _hash(img))
                hashes.add(seen[2])
            except OSError:
                pass
    return hashes, ids


def _load(tmp: str, base: Path | None) -> tuple[list[dict], dict, set[str]]:
    """(file submissions, parts by submission id, Never-post PostyBirb account ids)."""
    subs = [r for r in _records(tmp, "submissions", base)
            if r.get("type", "FILE") == "FILE" and isinstance(r.get("primary"), dict)]
    subs.sort(key=lambda r: (r.get("order") if isinstance(r.get("order"), (int, float)) else 0, str(r["_id"])))
    parts: dict = {}
    for prt in _records(tmp, "submission-part", base):
        parts.setdefault(str(prt.get("submissionId") or ""), []).append(prt)
    from database.db import get_connection
    conn = get_connection()
    try:
        names = _guarded_names(conn)
    finally:
        conn.close()
    others = {str(a["_id"]) for a in _records(tmp, "accounts", base) if _guarded(a, names)}
    return subs, parts, others


class _ToMarkdown(HTMLParser):
    """One pass over the HTML, so a crafted description can't stall the server (the story
    importer's regex converter is far slower than linear on unclosed tags)."""
    _MARK = {"b": "**", "strong": "**", "i": "*", "em": "*"}
    _BREAK = {"p": "\n\n", "div": "\n", "br": "\n", "li": "\n- ", "hr": "\n---\n",
              **{f"h{n}": f"\n\n{'#' * n} " for n in range(1, 7)}}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.href: str | None = None
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "a":
            self.href = dict(attrs).get("href") or None
            self.out.append("[" if self.href else "")
        else:
            self.out.append(self._MARK.get(tag) or self._BREAK.get(tag, ""))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag == "a":
            self.out.append(f"]({self.href})" if self.href else "")
            self.href = None
        elif tag in self._MARK:
            self.out.append(self._MARK[tag])
        elif tag in ("p", "div") or re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n" if tag == "div" else "\n\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def _markdown(s: str, limit: int | None = 200_000) -> str:
    """PostyBirb's descriptions are HTML from its rich-text box; PawPoller's are Markdown.
    Story imports (`posting.importer`) use it too, uncapped: those are the owner's own files."""
    if "<" not in s:
        return html.unescape(s)   # plain text still carries entities (&amp;); the parser path decodes them too
    p = _ToMarkdown()
    # A "<" that never closes is text, not a tag: Python 3.10's parser rescans an unclosed tag
    # slowly enough to stall the server on a crafted file.
    p.feed(re.sub(r"<(?![^<>]{0,2000}>)", "&lt;", s[:limit]))   # ponytail: descriptions past 200k chars are cut
    p.close()
    return re.sub(r"\n{3,}", "\n\n", "".join(p.out)).strip()


def _text(v) -> str:
    return _markdown(v["value"]) if isinstance(v, dict) and isinstance(v.get("value"), str) else ""


def _extras(tmp: str, base: Path | None) -> tuple[list[dict], list[dict]]:
    """PostyBirb's description templates and tag groups, shaped as saved descriptions / tag groups (US3)."""
    descs, groups = [], []
    for r in _records(tmp, "description-template", base):
        if _PB_ID.fullmatch(str(r["_id"])):
            text = r.get("content") if isinstance(r.get("content"), str) else ""
            descs.append({"from": f"postybirb:{r['_id']}", "name": str(r.get("title") or "")[:80],
                          "text": _markdown(text)[:20_000]})
    for r in _records(tmp, "tag-group", base):
        if _PB_ID.fullmatch(str(r["_id"])):
            tags = r.get("tags") if isinstance(r.get("tags"), list) else []
            groups.append({"from": f"postybirb:{r['_id']}", "name": str(r.get("alias") or "")[:80],
                           "tags": [t.strip()[:200] for t in tags if isinstance(t, str) and t.strip()][:300]})
    return descs, groups


def _tags(v) -> list[str]:
    vals = v.get("value") if isinstance(v, dict) else None
    return [str(x)[:200] for x in vals if isinstance(x, str) and x.strip()][:300] if isinstance(vals, list) else []


def _describe(rec: dict, parts: list[dict], others: set[str]) -> dict:
    """The piece a submission would become. Parts of a Never-post account are dropped."""
    default = next((p for p in parts if p.get("isDefault") or p.get("accountId") == "default"), None)
    d = default.get("data") if default and isinstance(default.get("data"), dict) else {}
    site_parts = [p for p in parts if p is not default]
    mine = [p for p in site_parts if str(p.get("accountId") or "") not in others]
    titles, descriptions, tags = {}, {}, {}
    seen: dict = {}
    for prt in mine:
        code = _SITE_CODES.get(str(prt.get("website") or ""))
        if code:
            seen[code] = seen.get(code, 0) + 1
    clash = sorted(c for c, n in seen.items() if n > 1)
    for prt in mine:
        code = _SITE_CODES.get(str(prt.get("website") or ""))
        pd = prt.get("data") if isinstance(prt.get("data"), dict) else {}
        if not code or code in clash:
            continue        # two accounts on one site: whose words? neither (FR-SEC-003)
        if isinstance(pd.get("title"), str) and pd["title"].strip():
            titles[code] = pd["title"][:300]
        if _text(pd.get("description")).strip():
            descriptions[code] = _text(pd.get("description"))
        if _tags(pd.get("tags")):
            tags[code] = _tags(pd.get("tags"))
    rating = str(d.get("rating") or "").lower()
    return {
        "title": (d.get("title") if isinstance(d.get("title"), str) and d.get("title").strip() else str(rec.get("title") or ""))[:300],
        "description": _text(d.get("description")),
        "tags": {"core": _tags(d.get("tags")), **tags},
        "titles": titles, "descriptions": descriptions,
        "rating": _RATINGS.get(rating, ""), "extreme": rating == "extreme",
        # tied only to someone else's account: unticked, flagged, never in "tick all" (FR-SEC-003)
        "others": bool(site_parts) and not mine,
        "clash": clash,
    }


def _files(docs: Path, rec: dict) -> tuple[Path | None, Path | None, int]:
    prim = rec.get("primary") or {}
    f = _inside(docs / "submission" / "files", prim.get("location"))
    th = _inside(docs / "submission" / "thumbnails", prim.get("preview"))
    if th is None and isinstance(rec.get("thumbnail"), dict):
        th = _inside(docs / "submission" / "files", rec["thumbnail"].get("location"))
    extra = len(rec.get("additional") or []) if isinstance(rec.get("additional"), list) else 0
    return f, th, extra


def scan_submissions(page: int = 0, base: Path | None = None) -> dict:
    """One page of PostyBirb submissions with what each would become. No file contents."""
    if not present(base):
        return {"version": None, "total": 0, "page": 0, "items": []}
    docs, _ = _dirs(base)
    if not (docs / "data" / "submissions.db").exists():
        if (docs / "data" / "database-production.sqlite").exists():
            return {"version": 4, "total": 0, "page": 0, "items": [],
                    "message": "Only PostyBirb 3 libraries can be read for now."}
        return {"version": 3, "total": 0, "page": 0, "items": []}
    tmp = tempfile.mkdtemp(prefix="pp-pb-")
    try:
        subs, parts, others = _load(tmp, base)
        descs, groups = _extras(tmp, base)
        hashes, done = _library()
        page = max(0, int(page))
        items = []
        thumbs = _THUMBS.setdefault(str(base), {})
        for rec in subs[page * PAGE:(page + 1) * PAGE]:
            sid = str(rec["_id"])
            info = _describe(rec, parts.get(sid, []), others)
            f, th, extra = _files(docs, rec)
            state = ""
            if sid in done:
                state = "already imported"
            elif f is None:
                state = "file missing in PostyBirb's folder"
            elif _kind(f) is None:
                state = "not a picture or video PawPoller can hold"
            elif _hash(f) in hashes:
                state = "already in your Library (same file)"
            pic = th if th is not None and _kind(th) == "image" else (f if f is not None and _kind(f) == "image" else None)
            if pic is not None:
                thumbs[sid] = pic
            items.append({"id": sid, "title": info["title"], "rating": info["rating"], "extreme": info["extreme"],
                          "tags": len(info["tags"]["core"]), "others": info["others"], "extra_files": extra,
                          "thumb": pic is not None, "skip": state})
    except Locked:
        return {"version": 3, "total": 0, "page": 0, "items": [], "locked": True,
                "message": "PostyBirb has its files open. Close PostyBirb and try again."}
    except ValueError:
        return {"version": 3, "total": 0, "page": 0, "items": [], "message": "PostyBirb's library is too big to read."}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    logger.info("PostyBirb library scan: %d submissions, page %d", len(subs), page)
    return {"version": 3, "total": len(subs), "page": page, "page_size": PAGE, "items": items,
            "extras": {"descriptions": len(descs), "tag_groups": len(groups)}}


def thumb_path(sid: str, base: Path | None = None) -> Path | None:
    """The preview image for one submission id (FR-SEC-006): from the scan's map when it has one
    (one page asks for up to 100), else looked up by id; always inside the folder, an image."""
    if not _PB_ID.fullmatch(sid or ""):
        return None
    hit = _THUMBS.get(str(base), {}).get(sid)
    if hit is not None and hit.is_file():
        return hit
    docs, _ = _dirs(base)
    tmp = tempfile.mkdtemp(prefix="pp-pb-")
    try:
        rec = next((r for r in _records(tmp, "submissions", base) if str(r.get("_id")) == sid), None)
    except (Locked, ValueError):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not rec or not isinstance(rec.get("primary"), dict):
        return None
    _, th, _ = _files(docs, rec)
    if th is None or _kind(th) != "image":
        f, _, _ = _files(docs, rec)
        th = f if f is not None and _kind(f) == "image" else None
    return th


def apply_submissions(ids: list[str], base: Path | None = None, progress=None) -> list[dict]:
    """Make a Library piece of each ticked submission. One result row per id (data-model).
    ``ids == ["*"]`` means every submission that isn't skipped or someone else's."""
    from posting import artwork_reader
    docs, _ = _dirs(base)
    want = None if ids == ["*"] else {str(i) for i in ids}
    tmp = tempfile.mkdtemp(prefix="pp-pb-")
    results: list[dict] = []
    try:
        subs, parts, others = _load(tmp, base)
        hashes, done = _library()
        chosen = [r for r in subs if want is None or str(r["_id"]) in want]
        for n, rec in enumerate(chosen):
            sid = str(rec["_id"])
            info = _describe(rec, parts.get(sid, []), others)
            row = {"submission_id": sid, "title": info["title"]}
            if progress:
                progress(n, len(chosen))
            if want is None and info["others"]:
                continue                                  # tick-all never selects someone else's
            f, th, extra = _files(docs, rec)
            if sid in done:
                results.append({**row, "status": "skipped", "reason": "already imported"})
                continue
            kind = _kind(f) if f is not None else None
            if f is None:
                results.append({**row, "status": "skipped", "reason": "file missing in PostyBirb's folder"})
                continue
            if kind is None or (kind == "video" and (th is None or _kind(th) != "image")):
                results.append({**row, "status": "skipped", "reason": "not a picture or video PawPoller can hold"})
                continue
            digest = _hash(f)
            if digest in hashes:
                results.append({**row, "status": "skipped", "reason": "already in your Library (same file)"})
                continue
            try:
                data = f.read_bytes()
                name = artwork_reader.create_artwork(
                    title=info["title"] or f.stem, image_filename=f.name, image_bytes=data,
                    description=info["description"], rating=info["rating"], tags=info["tags"],
                    titles=info["titles"], descriptions=info["descriptions"],
                    thumbnail_filename=th.name if kind == "video" else None,
                    thumbnail_bytes=th.read_bytes() if kind == "video" else None,
                    source={"platform": "postybirb", "submission_id": sid})
            except PermissionError:
                results.append({**row, "status": "skipped",
                                "reason": "PostyBirb was holding the file — close it and try again"})
                continue
            except (OSError, ValueError):
                results.append({**row, "status": "skipped", "reason": "not a picture or video PawPoller can hold"})
                continue
            hashes.add(digest)
            done.add(sid)
            notes = []
            if info["extreme"]:
                notes.append("rated extreme in PostyBirb, adult here")
            if info["clash"]:
                notes.append("more than one account on " + ", ".join(info["clash"]) + ": that site's own text left out")
            if extra:
                # ponytail: extra files are reported, not attached; add them as renders when asked for
                notes.append(f"{extra} extra file{'s' if extra != 1 else ''} left out")
            results.append({**row, "status": "added", "piece": name, "note": "; ".join(notes)})
    except Locked:
        return [{"submission_id": i, "status": "skipped", "reason": "PostyBirb was holding the file — close it and try again"}
                for i in sorted(want or [])]
    except ValueError:
        return [{"submission_id": i, "status": "skipped", "reason": "PostyBirb's library is too big to read"}
                for i in sorted(want or [])]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    logger.info("PostyBirb library import: %d added, %d skipped",
                sum(r["status"] == "added" for r in results), sum(r["status"] != "added" for r in results))
    return results


def apply_extras(base: Path | None = None) -> dict:
    """Bring in every description template and tag group (US3). Text only, no logins; one already
    brought in is skipped, and a name in use gets " (PostyBirb)" (``library_api.merge_imported``)."""
    from routes import library_api
    tmp = tempfile.mkdtemp(prefix="pp-pb-")
    try:
        descs, groups = _extras(tmp, base)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = {"descriptions": library_api.merge_imported("descriptions", descs),
           "tag_groups": library_api.merge_imported("groups", groups)}
    logger.info("PostyBirb saved text: %d descriptions, %d tag groups added",
                out["descriptions"]["added"], out["tag_groups"]["added"])
    return out
