"""Operator announcements (spec 089).

The operator publishes one small signed file; every install fetches it, checks it, and shows the most
serious current announcement as a bar under the top bar. Everything that decides whether a set is
trusted is in this file, so it can be read end to end.

The rules, in the order they bite:

* Installs hold PUBLIC keys only (``PUBLIC_KEYS``). Nothing here, in settings or in the environment
  can make an announcement, change the keys or the address, or skip ``verify``. Retiring a key is
  deleting its line in a release.
* While ``PUBLIC_KEYS`` is empty the feature is inert: no request is made and nothing can show.
* The fetch says nothing about this copy: one fixed address, no redirects, a size cap, a generic
  client name.
* Nothing that has not passed ``verify`` is stored, shown or logged; a failure logs one fixed word.
* The stored file is checked again on every read, so a file placed by a backup, a sync or by hand is
  trusted no more than one from the network.
* The text is words only. There is no field for a link, a button or a picture, and unknown fields are
  dropped.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# A separate repository from the one releases come from (updater.GITHUB_REPO): the credential that can
# write this file must not be able to reach anything the updater trusts. The raw host answers directly
# (no redirect) and does not spend the update check's hourly allowance.
FEED_URL = "https://raw.githubusercontent.com/knaughtykat01-prog/pawpoller-announcements/main/announcements.json"

# key id -> the Ed25519 public key (base64 of its 32 bytes). Made by deploy/announce_keygen.py.
PUBLIC_KEYS: dict[str, str] = {
    "k1": "78csuMh4kxbfOTaC82/dGOYDfug/NZsK8V3l7ZKEKnw=",   # 16 Nov 2026
}

PREFIX = b"pawpoller-announcements-v1\n"   # signed with the payload, so the signature is good for nothing else
MAX_BYTES = 64 * 1024
MAX_ITEMS = 10
MAX_DAYS = 30
LEVELS = ("urgent", "heads-up", "info")    # most serious first
FETCH_EVERY_S = 3600      # hourly (operator, 17 Nov): six hours was too slow for "reach everyone fast"

_ID = re.compile(r"[a-z0-9-]{1,40}\Z")
_TIME = "%Y-%m-%dT%H:%M:%SZ"
_VERSION = re.compile(r"[0-9]{1,6}(\.[0-9]{1,6}){0,3}\Z")   # bounded: int() of it must be cheap
# Control characters, invisible format characters (direction overrides among them) and line/paragraph
# separators: any of these can make text read differently from what was signed.
_HIDDEN = ("Cc", "Cf", "Zl", "Zp", "Cs")   # Cs: a lone surrogate can't be sent back to the page


class Refused(Exception):
    """A set or a fetch that is not accepted. ``reason`` is one fixed word, safe to log."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ── Checking ─────────────────────────────────────────────────────────────────

def _text(value, limit: int, multiline: bool) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise Refused("bad_item")
    for ch in value:
        if ch == "\n" and multiline:
            continue
        if unicodedata.category(ch) in _HIDDEN:
            raise Refused("bad_item")
    return value


def _when(value) -> datetime:
    if not isinstance(value, str):
        raise Refused("bad_item")
    try:
        when = datetime.strptime(value, _TIME).replace(tzinfo=timezone.utc)
    except ValueError:
        raise Refused("bad_item") from None
    # Exactly the canonical form. Dates are compared as TEXT below (issued_at against the newest
    # accepted, starts/ends against now), and text order is time order only then: strptime also
    # takes "2026-9-1T3:4:5Z" and non-ASCII digits, which sort wrongly and would outlive the
    # 30-day cap or lock out every later set.
    if when.strftime(_TIME) != value:
        raise Refused("bad_item")
    return when


def _version(value) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not _VERSION.match(value):
        raise Refused("bad_item")
    return value


def validate(items) -> list[dict]:
    """The announcements of a set, with only the known fields. One bad one refuses the whole set."""
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise Refused("bad_item")
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            raise Refused("bad_item")
        ann_id = it.get("id")
        if not isinstance(ann_id, str) or not _ID.match(ann_id) or ann_id in seen:
            raise Refused("bad_item")
        seen.add(ann_id)
        if it.get("level") not in LEVELS:
            raise Refused("bad_item")
        starts, ends = _when(it.get("starts")), _when(it.get("ends"))
        if not starts < ends <= starts + timedelta(days=MAX_DAYS):
            raise Refused("bad_item")
        out.append({"id": ann_id, "level": it["level"],
                    "title": _text(it.get("title"), 120, multiline=False),
                    "body": _text(it.get("body"), 1000, multiline=True),
                    "starts": it["starts"], "ends": it["ends"],
                    "min_version": _version(it.get("min_version")),
                    "max_version": _version(it.get("max_version"))})
    return out


def verify(raw: bytes, newest: str = "", now: datetime | None = None) -> tuple[list[dict], str]:
    """Check a feed file. Returns (announcements, issued_at) or raises Refused.

    ``newest`` is the issued_at of the newest set already accepted: an older one is a replay."""
    try:
        return _verify(raw, newest, now or datetime.now(timezone.utc))
    except Refused:
        raise
    except Exception:           # anything odd in the bytes (bad base64, nesting too deep) is the same answer
        raise Refused("malformed") from None


def _verify(raw: bytes, newest: str, now: datetime) -> tuple[list[dict], str]:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if len(raw) > MAX_BYTES:
        raise Refused("too_big")
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get("v") != 1 or not all(
            isinstance(doc.get(k), str) for k in ("key", "payload", "sig")):
        raise Refused("malformed")
    public = PUBLIC_KEYS.get(doc["key"])
    if not public:
        raise Refused("unknown_key")
    payload = base64.b64decode(doc["payload"], validate=True)
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public, validate=True)).verify(
            base64.b64decode(doc["sig"], validate=True), PREFIX + payload)
    except InvalidSignature:
        raise Refused("bad_signature") from None
    body = json.loads(payload)
    if not isinstance(body, dict) or body.get("kind") != "pawpoller-announcements":
        raise Refused("wrong_kind")
    issued = body.get("issued_at")
    if _when(issued) > now + timedelta(days=1):
        raise Refused("future")       # or one bad marker would lock out every later set
    if newest and issued < newest:    # same fixed format, so text order is time order
        raise Refused("older")
    return validate(body.get("items")), str(issued)   # _when() above only passes a string


# ── What this copy holds ─────────────────────────────────────────────────────

def _state_path() -> Path:
    return Path(config.SETTINGS_PATH).parent / "announce_state.json"


def _read_state() -> dict:
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(raw: bytes, issued: str) -> None:
    """Keep an accepted file. The marker of the newest set accepted only ever rises."""
    state = {"file": raw.decode("utf-8"), "issued_at": max(issued, str(_read_state().get("issued_at") or ""))}
    path = _state_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="announce_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_warned = False


def load() -> list[dict]:
    """The accepted announcements, checked again now. Empty when there are none or the check fails."""
    global _warned
    stored = _read_state().get("file")
    if not PUBLIC_KEYS or not isinstance(stored, str):
        return []
    try:
        return verify(stored.encode("utf-8"))[0]
    except Refused:
        if not _warned:
            _warned = True
            logger.info("announcements: stored set failed its check")
        return []


def enabled(settings: dict | None = None) -> bool:
    """The Settings switch. A hosted seat is always on: it is the operator's service."""
    import legal
    if legal.hosted():
        return True
    return (settings if settings is not None else config.get_settings()).get("announcements_enabled") is not False


def _ver(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def _in_range(mine: tuple, lo: str, hi: str) -> bool:
    """Compared over as many parts as the limit has, so "up to 4.91" includes 4.91.3."""
    low, high = (_ver(lo) if lo else ()), (_ver(hi) if hi else ())
    return mine[:len(low)] >= low and mine[:len(high)] <= high


def current(settings: dict | None = None, now: datetime | None = None, version: str | None = None) -> list[dict]:
    """What to show: in its dates, for this version, not closed. Most serious first, then newest."""
    settings = settings if settings is not None else config.get_settings()
    if not enabled(settings):
        return []
    stamp = (now or datetime.now(timezone.utc)).strftime(_TIME)
    mine = _ver(version or config.APP_VERSION)
    closed = settings.get("announce_closed")
    closed = set(closed) if isinstance(closed, list) else set()
    show = [a for a in load()
            if a["starts"] <= stamp < a["ends"] and a["id"] not in closed
            and _in_range(mine, a["min_version"], a["max_version"])]
    show.sort(key=lambda a: a["starts"], reverse=True)
    show.sort(key=lambda a: LEVELS.index(a["level"]))
    return [{k: a[k] for k in ("id", "level", "title", "body")} for a in show]


# ── Fetching ─────────────────────────────────────────────────────────────────

def _http_get(transport=None) -> bytes:
    """One request that says nothing about this copy: no version, no cookies, no proxy secret,
    nothing kept from an earlier answer."""
    import httpx
    with httpx.Client(timeout=10.0, follow_redirects=False, transport=transport,
                      headers={"User-Agent": "PawPoller"}) as client:
        with client.stream("GET", FEED_URL) as resp:
            if resp.status_code != 200:     # a redirect, "too many requests", anything else: a plain failure
                raise Refused("status")
            got = b""
            for chunk in resp.iter_bytes():
                got += chunk
                if len(got) > MAX_BYTES:    # stop reading; never hold a huge answer
                    raise Refused("too_big")
            return got


def fetch_once(get=None) -> str:
    """Ask for the set once. Returns "ok", "off", or the fixed word for why it failed."""
    if not PUBLIC_KEYS or not enabled():
        return "off"
    try:
        raw = (get or _http_get)()
        _, issued = verify(raw, str(_read_state().get("issued_at") or ""))
        _save(raw, issued)
        return "ok"
    except Refused as e:
        reason = e.reason
    except Exception:           # no network, a timeout, a full disk
        reason = "unreachable"
    logger.info("announcements: check failed (%s)", reason)
    return reason


def fetch_soon() -> None:
    """One check in the background, for when announcements are switched back on. Never under test
    (the same switch that keeps the loop off there)."""
    if os.environ.get("PAWPOLLER_ANNOUNCE_LOOP") == "0" or not PUBLIC_KEYS:
        return
    import threading
    threading.Thread(target=fetch_once, name="announce-fetch", daemon=True).start()


async def loop() -> None:
    """Shortly after start-up, then every hour. Off the event loop: the fetch blocks."""
    if not PUBLIC_KEYS:
        return
    await asyncio.sleep(20)
    while True:
        await asyncio.to_thread(fetch_once)
        await asyncio.sleep(FETCH_EVERY_S)
