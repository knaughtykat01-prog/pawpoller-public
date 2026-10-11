"""The account's own email: 6-digit codes and security notices (4.68.0, spec 033 phase 6).

Every copy of PawPoller sends these through the Tech Centre (``techcentre.TECH_CENTRE_URL``
+ ``/api/v1/mail``), which mails them from noreply@pawpoller.com with fixed wording and
keeps nothing. A copy on someone's computer has no mail server of its own, and the
project's mail login can't ship inside the app — so this is the one path, for desktops
and hosted servers alike. ``PAWPOLLER_TECH_CENTRE_URL=""`` turns it off (and the suite
does), and then ``ready()`` is false: no reset offered, the email waits unconfirmed.

A code is six digits, made here; only a salted fingerprint is stored, for 15 minutes and
5 tries. The digits travel to the Tech Centre once, to be emailed — that is in Privacy v2.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time

import httpx

import config

logger = logging.getLogger(__name__)

CODE_TTL = 15 * 60
MAX_TRIES = 5
SEND_LIMIT = (3, 3600)          # per purpose, per install: the Tech Centre has its own limits too
PURPOSES = ("confirm", "reset")
# One lock over read-check-count: /api/auth/reset is reachable signed out and runs in a thread
# pool, so without it parallel guesses each read tries=0 and the 5-try limit is void (4.69.0 review).
# Not config's settings lock — get_settings takes that one, so holding it here would deadlock.
_LOCK = threading.Lock()
NOTICES = ("password_changed", "email_changed", "verified")


def ready() -> bool:
    import techcentre
    return bool(techcentre.TECH_CENTRE_URL)


def _key(purpose: str) -> str:
    return f"auth_code_{purpose}"


def _fp(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}|{code}".encode("utf-8")).hexdigest()


def _post(to: str, purpose: str, code: str = "") -> bool:
    import techcentre
    if not techcentre.TECH_CENTRE_URL:
        return False
    try:
        r = httpx.post(f"{techcentre.TECH_CENTRE_URL}/api/v1/mail", timeout=20,
                       json={"to": to, "purpose": purpose, "code": code},
                       headers={"X-Syncopates-App": "pawpoller", "User-Agent": f"PawPoller/{config.APP_VERSION}"})
    except httpx.HTTPError as e:
        logger.warning("Account mail (%s) not sent: %s", purpose, type(e).__name__)
        return False
    if r.status_code != 200:
        logger.warning("Account mail (%s) refused: HTTP %s", purpose, r.status_code)
        return False
    logger.info("Account mail sent: %s", purpose)
    return True


def _allowed(purpose: str) -> bool:
    """Our own send limit, so one copy can't burn the Tech Centre's allowance for its address."""
    now = time.time()
    log = config.get_settings().get("auth_mail_log") or {}
    hits = [t for t in (log.get(purpose) or []) if isinstance(t, (int, float)) and now - t < SEND_LIMIT[1]]
    if len(hits) >= SEND_LIMIT[0]:
        return False
    log[purpose] = hits + [now]
    config.save_settings({"auth_mail_log": log})
    return True


def send_code(purpose: str, to: str) -> bool:
    """Make a fresh code and email it; once it's sent, the old one stops working. False if not sent.

    The code is saved only after the Tech Centre took it (4.69.1 review, High): saved before, it was
    guessable during the send, and a failed send that got its slot back left it valid for good, so
    anyone who could make sends fail could mint unlimited 5-try reset codes.
    """
    if purpose not in PURPOSES or not to or not ready():
        return False
    with _LOCK:
        if not _allowed(purpose):
            return False
    code = f"{secrets.randbelow(10 ** 6):06d}"
    if not _post(to, purpose, code):
        _refund(purpose)   # a failed send (Tech Centre down, mail refused) mustn't lock the owner out for an hour
        return False
    salt = secrets.token_hex(8)
    rec = {"fp": _fp(salt, code), "salt": salt, "exp": time.time() + CODE_TTL, "tries": 0, "to": to}
    with _LOCK:
        config.save_settings({_key(purpose): rec})
    return True


def _refund(purpose: str) -> None:
    with _LOCK:
        log = config.get_settings().get("auth_mail_log") or {}
        hits = log.get(purpose) or []
        if hits:
            log[purpose] = hits[:-1]
            config.save_settings({"auth_mail_log": log})


def check_code(purpose: str, code: str) -> str | None:
    """The address the code was sent to when *code* is right (and then it's used up), else None.

    Wrong tries count; the fifth wrong one ends the code. Expired codes never pass.
    """
    with _LOCK:
        return _check(purpose, code)


def _check(purpose: str, code: str) -> str | None:
    rec = (config.get_settings() or {}).get(_key(purpose))   # a code record, not the settings
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not isinstance(rec, dict) or len(code) != 6:
        return None
    if time.time() > float(rec.get("exp") or 0) or int(rec.get("tries") or 0) >= MAX_TRIES:
        config.delete_settings_keys([_key(purpose)])
        return None
    if hmac.compare_digest(_fp(str(rec.get("salt") or ""), code), str(rec.get("fp") or "")):
        config.delete_settings_keys([_key(purpose)])
        return str(rec.get("to") or "")
    rec["tries"] = int(rec.get("tries") or 0) + 1
    config.save_settings({_key(purpose): rec})
    return None


def notice(purpose: str, to: str) -> None:
    """A password- or email-changed notice, sent in the background (never holds up the request)."""
    if purpose in NOTICES and to and ready():
        threading.Thread(target=_post, args=(to, purpose), daemon=True, name="account-notice").start()
