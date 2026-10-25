"""Sign in for a server: one-time codes for a desktop app's hand-over (spec 059).

A web page can't read another site's cookies, so a Docker install, an installed server or a
hosted seat can't sign in to FurAffinity on its own. The person's desktop app can: the
server's site page shows a short code, the desktop's "Sign in for a server" screen takes the
server's address and that code, opens the site's own sign-in page in a PawPoller window, and
hands the result over here.

Two routes face strangers (auth-exempt by exact path in dashboard.py):

* ``POST /api/signin-helper/lookup`` ``{code}`` → which site and account the code is for;
* ``POST /api/signin-helper/result`` ``{code, cookies, extra_fields}`` → the hand-over.

The code is the only credential, so everything about it is narrow (plan.md Security Design):

* six characters with no look-alikes (~30 bits), ten minutes, one site, one named account,
  one use, hashed in memory, only ever in a request body (never a URL, so never a log);
* a code is claimed under the lock before anything else, and never goes back to waiting;
* wrong codes count per address (IPv6 by /64) and across everyone: 20 wrong in ten minutes
  voids every live code and blocks new ones for 15 minutes. Helper failures never feed the
  dashboard's own sign-in lockout, so a stranger can't lock the owner out of the dashboard;
* the hand-over saves only that site's own sign-in fields (``browser_login.check_creds``)
  and only after the site's own account check says it is the stored account; the
  account-name fields themselves are refused from the body (the server keeps its own);
* Never-post accounts are refused when a code is made and again at hand-over.

DeviantArt gets no codes: nothing in PawPoller can yet ask DeviantArt whose sign-in a
cookie is (its `check_cookie` only says signed in or out), and a signed-in-only check would
let a code holder plant their own account (spec 059 T031). DA keeps desktop Sign in and
Other ways to connect. Check-only sites (Inkbunny, Weasyl, AO3, SquidgeWorld) keep nothing,
so they get no codes either.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
import uuid

from fastapi import APIRouter, HTTPException, Request

import config
from routes.body_caps import read_capped

logger = logging.getLogger(__name__)

signin_helper_router = APIRouter()

CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = 6
CODE_TTL = 600                 # seconds a code lives
MAX_LIVE = 20                  # live codes per install
FAIL_BUDGET = (20, 600)        # wrong tries from anyone in the window → every code voided
BLOCK_SECS = 900               # …and no new code for this long
PER_ADDR = (10, 600)           # helper requests per address (IPv6 /64) per window
BODY_CAP = 16 * 1024
CODE_PLATFORMS = ("fa", "tw", "ng", "ik")   # see the module docstring for DA and the check-only sites

NOT_VALID = "That code isn't valid or has run out."
NEEDS_TLS = "Your server needs https or Tailscale for this."
VOIDED = "Too many wrong codes were tried. Make a new one in 15 minutes."

_SALT = secrets.token_bytes(16)    # per process: a restart drops every code anyway
_lock = threading.Lock()
_codes: dict[str, dict] = {}        # id → {hash, platform, account_id, expires, state, handle, message}
_fails: list[float] = []
_blocked_until = 0.0
_addr_hits: dict[str, list[float]] = {}

_TAILSCALE = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def _reset_state() -> None:
    """Tests only."""
    global _blocked_until
    with _lock:
        _codes.clear()
        _fails.clear()
        _addr_hits.clear()
        _blocked_until = 0.0


# ── codes ────────────────────────────────────────────────────────────────────

def _norm(code) -> str:
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def _hash(code: str) -> str:
    return hashlib.sha256(_SALT + _norm(code).encode()).hexdigest()


def _sweep(now: float) -> None:
    """Expire what has run out; forget finished codes after a further ten minutes."""
    for cid, c in list(_codes.items()):
        if c["state"] == "waiting" and now >= c["expires"]:
            c["state"] = "expired"
        if c["state"] != "waiting" and now >= c["expires"] + CODE_TTL:
            _codes.pop(cid, None)


def _find(code: str, now: float) -> dict | None:
    """The waiting code matching *code*, comparing every live hash in constant time."""
    h = _hash(code)
    found = None
    for c in _codes.values():
        if hmac.compare_digest(c["hash"], h) and c["state"] == "waiting" and now < c["expires"]:
            found = c
    return found


def _fail(now: float) -> None:
    """One wrong try, counted across everyone. At the budget every live code is voided."""
    global _blocked_until
    cutoff = now - FAIL_BUDGET[1]
    _fails[:] = [t for t in _fails if t > cutoff]
    _fails.append(now)
    if len(_fails) >= FAIL_BUDGET[0]:
        for c in _codes.values():
            if c["state"] == "waiting":
                c["state"], c["message"] = "voided", VOIDED
        _blocked_until = now + BLOCK_SECS
        _fails.clear()
        logger.warning("Sign-in helper: too many wrong codes; every live code voided for %d min", BLOCK_SECS // 60)


def _addr_key(host: str) -> str:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host or "?"
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def _addr_ok(host: str, now: float) -> bool:
    key = _addr_key(host)
    limit, window = PER_ADDR
    for k in [k for k, v in _addr_hits.items() if not v or now - v[-1] >= window]:
        _addr_hits.pop(k, None)
    hits = [t for t in _addr_hits.get(key, []) if now - t < window]
    hits.append(now)
    _addr_hits[key] = hits
    return len(hits) <= limit


def _peer(request: Request) -> str:
    return request.client.host if request.client else ""


def _peer_trusted(host: str) -> bool:
    """Loopback or Tailscale: the two ways plain http is fine (as `config.is_trusted_transport`)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost"
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or any(ip in n for n in _TAILSCALE if n.version == ip.version)


def allow_http(settings: dict | None = None) -> bool:
    """The owner's plain-http opt-in, never on a hosted seat whatever the file says."""
    s = config.get_settings() if settings is None else settings
    return bool(s.get("signin_helper_allow_http")) and os.environ.get("PAWPOLLER_HOSTED") != "1"


# ── accounts ─────────────────────────────────────────────────────────────────

def _identity_field(platform: str) -> str:
    from auth import browser_login as bl
    return sorted(bl.identity_keys(platform))[0]


def _account(account_id: int) -> dict | None:
    from database.db import get_connection
    from database import accounts as adb
    conn = get_connection()
    try:
        return adb.get_account(conn, int(account_id))
    finally:
        conn.close()


def _default_account_id(platform: str) -> int | None:
    from database.db import get_connection
    from database import accounts as adb
    conn = get_connection()
    try:
        return adb.get_default_account_id(conn, platform, create=True, settings=config.get_settings())
    finally:
        conn.close()


def _stored_name(acct: dict, settings: dict | None = None) -> str:
    s = config.get_settings() if settings is None else settings
    key = config.account_setting_key(int(acct["account_id"]), _identity_field(acct["platform"]),
                                     bool(acct.get("is_default")))
    return str(s.get(key) or "").strip()


def _never_post(account_id: int) -> bool:
    from database import accounts as adb
    return int(account_id) in adb.never_post_ids()


# ── whose sign-in is it (FR-SEC-004) ─────────────────────────────────────────

async def _who(platform: str, creds: dict, stored: str) -> tuple[bool, str, str]:
    """(ok, handle the site reported, message). ``ok`` only when the site names the
    signed-in account and it is *stored*; an old, dead or unnamed session is never ok."""
    if platform == "fa":
        from clients.fa.client import FAClient, FAChallenged, _same_fa_user
        client = FAClient(username=stored, cookie_a=creds.get("fa_cookie_a", ""), cookie_b=creds.get("fa_cookie_b", ""))
        try:
            out = await client.validate_session()
        except FAChallenged:
            return False, "", "FurAffinity is asking for a bot check right now. Try again later."
        finally:
            await client.close()
        who = out.get("username") or ""
        if not out.get("logged_in"):
            return False, "", "FurAffinity didn't accept that sign-in."
        if not who:
            return False, "", "FurAffinity didn't say which account is signed in."
        return _same_fa_user(who, stored), who, ""
    if platform == "tw":
        from clients.tw.client import TWClient
        client = TWClient(creds.get("tw_auth_token", ""), creds.get("tw_ct0", ""), stored)
        try:
            owners = await client.session_owner()
        finally:
            await client.close()
        if not owners:
            return False, "", "X didn't accept that sign-in."
        # The ACTIVE account, never "anywhere in the list": a post goes out as owners[0].
        return owners[0].lower() == stored.lower(), owners[0], ""
    if platform == "ng":
        from clients.ng.client import NgClient, same_ng_user
        client = NgClient(username=stored, cookie=creds.get("ng_cookie", ""))
        try:
            out = await client.validate_session()
        finally:
            await client.close()
        who = out.get("username") or ""
        if not who:
            return False, "", "Newgrounds didn't accept that sign-in."
        return same_ng_user(who, stored), who, ""
    if platform == "ik":
        from clients.ik.client import IKClient
        client = IKClient(target_user=stored)
        try:
            out = await client.validate_token(creds.get("ik_auth_token", ""))
        finally:
            await client.close()
        who = out.get("username") or ""
        if out.get("status") != "ok":
            return False, "", "Itaku didn't accept that sign-in."
        if not who:
            return False, "", "Itaku didn't say which account is signed in."
        return who.lower() == stored.lower(), who, ""
    return False, "", "This site can't be signed in for this way."


# ── owner routes (signed in) ─────────────────────────────────────────────────

@signin_helper_router.post("/api/settings/signin-codes")
async def make_code(body: dict):
    """A one-time code for the desktop app's "Sign in for a server" (spec 059 FR-004)."""
    from auth import browser_login as bl
    platform = str((body or {}).get("platform") or "").strip().lower()
    if platform not in CODE_PLATFORMS:
        raise HTTPException(400, "This site doesn't sign in through a browser."
                            if platform not in bl.PLATFORM_LOGIN or not bl.saved_keys(platform)
                            else "This site can't be signed in for from the desktop app yet. "
                                 "Use Other ways to connect.")
    raw_id = (body or {}).get("account_id")
    try:
        account_id = int(raw_id) if raw_id not in (None, "") else _default_account_id(platform)
    except (TypeError, ValueError):
        raise HTTPException(400, "That isn't an account.")
    acct = _account(account_id) if account_id is not None else None
    if not acct or acct.get("platform") != platform:
        raise HTTPException(403, "This account can't be signed in for.")
    if _never_post(account_id):
        raise HTTPException(403, "This account can't be signed in for.")
    stored = _stored_name(acct)
    typed = str((body or {}).get("username") or "").strip()
    if not stored and typed:
        # The owner, signed in, says which account this is; the site's answer is then checked
        # against it. Never the other way round (no trust in whatever signs in first).
        if len(typed) > 100:
            raise HTTPException(400, "That username is too long.")
        config.save_settings({config.account_setting_key(account_id, _identity_field(platform),
                                                         bool(acct.get("is_default"))): typed})
        stored = typed
    if not stored:
        raise HTTPException(400, "Enter this account's username first.")
    now = time.time()
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    with _lock:
        _sweep(now)
        if now < _blocked_until:
            raise HTTPException(429, VOIDED)
        for c in list(_codes.values()):
            if c["platform"] == platform and c["account_id"] == account_id and c["state"] == "waiting":
                c["state"] = "expired"             # a new code replaces the old one
        if sum(1 for c in _codes.values() if c["state"] == "waiting") >= MAX_LIVE:
            raise HTTPException(429, "Too many codes are open. Wait a few minutes.")
        cid = uuid.uuid4().hex
        _codes[cid] = {"id": cid, "hash": _hash(code), "platform": platform, "account_id": account_id,
                       "expires": now + CODE_TTL, "state": "waiting", "handle": "", "message": ""}
    logger.info("Sign-in helper: code made for %s account %s", platform, account_id)
    return {"id": cid, "code": code, "expires_in": CODE_TTL}


@signin_helper_router.get("/api/settings/signin-codes/{cid}")
async def code_state(cid: str):
    with _lock:
        _sweep(time.time())
        c = _codes.get(cid)
        if not c:
            return {"state": "expired"}
        out = {"state": c["state"]}
        if c["handle"]:
            out["handle"] = c["handle"]
        if c["message"]:
            out["message"] = c["message"]
        return out


@signin_helper_router.put("/api/settings/signin-helper-http")
async def set_allow_http(request: Request, body: dict):
    """The owner's opt-in to plain-http hand-overs on the home network (FR-SEC-007)."""
    if not bool((body or {}).get("allow")):
        config.save_settings({"signin_helper_allow_http": False})
        return {"allow_http": False}
    if os.environ.get("PAWPOLLER_HOSTED") == "1":
        raise HTTPException(403, "Hosted seats always use https.")
    from dashboard import _is_rate_limited, _record_auth_failure
    ip = _peer(request) or "unknown"
    if _is_rate_limited(ip):
        raise HTTPException(429, "Too many attempts. Try again later.")
    s = config.get_settings()
    stored_hash = s.get("auth_password_hash") or ""
    if not stored_hash:
        raise HTTPException(400, "Set a password first (Settings → Security).")
    if not config.verify_password(str((body or {}).get("password") or ""), stored_hash):
        _record_auth_failure(ip)      # the owner's own session: this one does count (unlike the helper)
        raise HTTPException(403, "That password isn't right.")
    if (body or {}).get("accept_risk") is not True:
        raise HTTPException(400, "Tick the box to say you understand the risk.")
    config.save_settings({"signin_helper_allow_http": True})
    logger.info("Sign-in helper: plain-http hand-overs switched on by the owner")
    return {"allow_http": True}


@signin_helper_router.get("/api/settings/signin-helper-http")
async def get_allow_http():
    return {"allow_http": allow_http(), "hosted": os.environ.get("PAWPOLLER_HOSTED") == "1"}


# ── stranger-facing routes (auth-exempt, code-gated) ─────────────────────────

async def _gate(request: Request) -> dict:
    """Per-address limit, the global block, the body cap, then the parsed JSON body."""
    now = time.time()
    with _lock:
        if not _addr_ok(_peer(request), now):
            raise HTTPException(429, "Too many tries. Wait ten minutes.")
        if now < _blocked_until:
            raise HTTPException(429, VOIDED)
    raw = await read_capped(request, BODY_CAP, too_big="That's too big for a sign-in.")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "Send JSON.")
    if not isinstance(body, dict):
        raise HTTPException(400, "Send JSON.")
    return body


@signin_helper_router.post("/api/signin-helper/lookup")
async def helper_lookup(request: Request):
    body = await _gate(request)
    now = time.time()
    with _lock:
        _sweep(now)
        c = _find(body.get("code"), now)
        if c is None:
            _fail(now)
            raise HTTPException(404, NOT_VALID)
        platform, account_id, left = c["platform"], c["account_id"], int(c["expires"] - now)
    acct = _account(account_id) or {}
    label = str((acct.get("label") or acct.get("handle") or _stored_name(acct)) if acct else "")
    return {"platform": platform, "account_label": label[:60], "allow_http": allow_http(), "expires_in": left}


def _finish(c: dict, state: str, message: str = "", handle: str = "") -> None:
    with _lock:
        c["state"], c["message"], c["handle"] = state, message, handle


@signin_helper_router.post("/api/signin-helper/result")
async def helper_result(request: Request):
    from auth import browser_login as bl
    body = await _gate(request)
    now = time.time()
    with _lock:
        _sweep(now)
        c = _find(body.get("code"), now)
        if c is None:
            _fail(now)
            raise HTTPException(404, NOT_VALID)
        if request.url.scheme == "http" and not _peer_trusted(_peer(request)) and not allow_http():
            # Checked before the claim; the code crossed the network in clear, so it dies too.
            c["state"], c["message"] = "failed", NEEDS_TLS
            raise HTTPException(403, NEEDS_TLS)
        c["state"] = "claimed"                   # one hand-over per code, whatever happens next
        platform, account_id = c["platform"], c["account_id"]

    creds = {}
    for part in ("cookies", "extra_fields"):
        val = body.get(part) or {}
        if not isinstance(val, dict):
            _finish(c, "failed", "That isn't a sign-in for this site.")
            raise HTTPException(400, "That isn't a sign-in for this site.")
        creds.update(val)
    if not creds:
        _finish(c, "failed", "No sign-in in the result.")
        raise HTTPException(400, "No sign-in in the result")
    if set(creds) & bl.identity_keys(platform):
        _finish(c, "failed", "That isn't a sign-in for this site.")
        raise HTTPException(400, "That isn't a sign-in for this site (the server keeps the account name).")
    try:
        bl.check_creds(platform, creds)
    except ValueError as e:
        _finish(c, "failed", str(e))
        raise HTTPException(400, str(e))
    acct = _account(account_id)
    if not acct or _never_post(account_id):
        _finish(c, "failed", "This account can't be signed in for.")
        raise HTTPException(403, "This account can't be signed in for.")
    stored = _stored_name(acct)
    site = bl.PLATFORM_LOGIN[platform]["name"]
    try:
        ok, who, why = await _who(platform, creds, stored)
    except Exception as e:
        logger.warning("Sign-in helper: %s account check failed: %s", platform, type(e).__name__)
        ok, who, why = False, "", f"{site} didn't answer. Try again in a minute."
    if not ok:
        msg = why or (f"That's signed in as {who}, not {stored}." if who else f"{site} didn't accept that sign-in.")
        _finish(c, "failed", msg)
        logger.info("Sign-in helper: hand-over for %s account %s refused (%s)", platform, account_id,
                    "another account" if who else "not accepted")
        raise HTTPException(409, msg)
    bl._save_browser_creds(platform, creds, account_id)
    _finish(c, "done", handle=who)
    logger.info("Sign-in helper: hand-over for %s account %s saved", platform, account_id)
    return {"ok": True, "handle": who}
