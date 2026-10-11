"""Terms of use + Privacy policy acceptance (4.67.0, spec 033 US2).

The documents ship with the app (``frontend/legal/*.html``, built from the website by
``deploy/build_legal.py``); their ``data-version`` is the current version. Accepting
records both versions and the time. A release that ships a newer version makes
``needs_accept`` true again, and the auth middleware then refuses signed-in pages until
the person accepts (the API-key path is exempt: the owner accepted on the install that
issued the key).
"""
from __future__ import annotations

import functools
import re
from datetime import datetime, timezone

import config

DOCS = ("terms", "privacy")
_HISTORY_MAX = 10


@functools.lru_cache(maxsize=1)
def current() -> dict:
    """``{"terms": N, "privacy": N}`` from the bundled documents (read once per process)."""
    out = {}
    for doc in DOCS:
        text = (config.resource_path("frontend") / "legal" / f"{doc}.html").read_text(encoding="utf-8")
        m = re.search(r'data-version="(\d+)"', text)
        out[doc] = int(m.group(1)) if m else 1
    return out


def accepted(settings: dict | None = None) -> dict | None:
    s = settings if settings is not None else config.get_settings()
    a = s.get("legal_accepted")
    return a if isinstance(a, dict) else None


def needs_accept(settings: dict | None = None) -> bool:
    a = accepted(settings)
    cur = current()
    return not a or any(int(a.get(d) or 0) < cur[d] for d in DOCS)


def accept(terms: int, privacy: int) -> dict:
    """Record acceptance of exactly the current versions; ValueError otherwise."""
    cur = current()
    if int(terms) != cur["terms"] or int(privacy) != cur["privacy"]:
        raise ValueError("not the current version")
    rec = {"terms": cur["terms"], "privacy": cur["privacy"],
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    hist = [h for h in (config.get_settings().get("legal_history") or []) if isinstance(h, dict)]
    config.save_settings({"legal_accepted": rec, "legal_history": (hist + [rec])[-_HISTORY_MAX:]})
    return rec


def status(settings: dict | None = None) -> dict:
    s = settings if settings is not None else config.get_settings()
    return {**current(), "accepted": accepted(s), "needs_accept": needs_accept(s),
            "history": s.get("legal_history") or []}
