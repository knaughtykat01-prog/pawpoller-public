"""Consent records: what was agreed, when, and to which wording (LEGALPAGES, 4.58.0).

The privacy policy says PawPoller records **when** someone said yes and **to which wording**, and asks
again when the wording changes. Until 4.58.0 the three opt-ins were bare yes/no settings (`tech_reports`,
`tech_usage`) or, for the Instagram relay, on by default with no question at all.

One record per opt-in, kept in settings under ``consent_records``:

    {"tech_usage": {"value": true, "at": "2026-10-08T01:02:03+00:00", "wording": 1}, ...}

A record counts only while its ``wording`` matches ``WORDING`` below. Bump a number whenever the
in-app text for that opt-in changes in substance; every earlier yes then stops counting until the
person answers again (a no stays a no — refusing needs no re-asking).
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import config

_KEY = "consent_records"

# Spec 079: where an answer was given. The app checks it; the page can't add to it. A no from sign-up or setup is a
# box left unticked, not a clear no (techcentre.usage_clear); an answer without one was saved before 079 (unknown).
WHERE = frozenset({"ask", "signup", "setup", "settings"})

# record() rewrites the whole dict, so two kinds answered at once must not lose one.
# Lock order: techcentre._lock → this lock → config._settings_lock; never the other way round.
_lock = threading.Lock()

# The version of the in-app wording each opt-in was answered to. Bump on a change of substance.
WORDING = {
    "tech_reports": 1,   # error reports (Settings → Diagnostics; the first-error prompt; setup)
    "tech_usage": 1,     # check-ins, "count this copy"
    "ig_relay": 1,       # the Instagram picture relay (Settings → Posting → Instagram image host)
}

# What each opt-in is called where the app asks again.
LABELS = {
    "tech_reports": "error reports",
    "tech_usage": "check-ins (count this copy)",
    "ig_relay": "the Instagram picture relay",
}

# The setting each opt-in used before records existed (for "you said yes before; please confirm").
LEGACY_KEYS = {"tech_reports": "tech_reports", "tech_usage": "tech_usage", "ig_relay": "ig_relay_enabled"}


def _all(settings: dict | None = None) -> dict:
    s = settings if settings is not None else config.get_settings()
    recs = s.get(_KEY)
    return recs if isinstance(recs, dict) else {}


def get(kind: str, settings: dict | None = None) -> dict | None:
    rec = _all(settings).get(kind)
    return rec if isinstance(rec, dict) else None


def record(kind: str, value: bool, where: str | None = None) -> dict:
    """Store an answer now, against the current wording. Returns the record.

    Spec 079: `where` (one of WHERE) says where it was given; the date, wording and app version are the app's own."""
    if kind not in WORDING:
        raise ValueError(f"unknown consent kind {kind!r}")
    if where is not None and where not in WHERE:
        raise ValueError(f"unknown consent place {where!r}")
    rec = {"value": bool(value), "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "wording": WORDING[kind], "app_version": config.APP_VERSION}
    if where is not None:
        rec["where"] = where
    with _lock:
        recs = dict(_all())
        recs[kind] = rec
        config.save_settings({_KEY: recs})
    return rec


def valid(kind: str, settings: dict | None = None) -> bool | None:
    """True / False when there's an answer to the CURRENT wording; None when there isn't (ask)."""
    rec = get(kind, settings)
    if rec and rec.get("wording") == WORDING.get(kind):
        return bool(rec.get("value"))
    return None


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def needs_reconfirm(settings: dict | None = None) -> list[str]:
    """Opt-ins someone said yes to before records existed (or to older wording) that have no answer
    to the current wording — they're off until confirmed, and the bell asks once."""
    s = settings if settings is not None else config.get_settings()
    out = []
    for kind, legacy in LEGACY_KEYS.items():
        if valid(kind, s) is not None:
            continue
        rec = get(kind, s)
        said_yes_before = (rec and rec.get("value")) or (legacy in s and _truthy(s.get(legacy)))
        if said_yes_before:
            out.append(kind)
    return out
