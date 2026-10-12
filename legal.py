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
import os
import re
from datetime import datetime, timezone

import config

DOCS = ("terms", "privacy")
_HISTORY_MAX = 10


def hosted() -> bool:
    """A hosted beta seat (4.70.0, spec 034): set by the home box's seat .env, never by the tester."""
    return os.environ.get("PAWPOLLER_HOSTED") == "1"


def docs() -> tuple:
    """The documents to accept here: a hosted seat adds "What a hosted seat means"."""
    return DOCS + (("hosted",) if hosted() else ())


@functools.lru_cache(maxsize=2)
def _versions(names: tuple) -> dict:
    out = {}
    for doc in names:
        text = (config.resource_path("frontend") / "legal" / f"{doc}.html").read_text(encoding="utf-8")
        m = re.search(r'data-version="(\d+)"', text)
        out[doc] = int(m.group(1)) if m else 1
    return out


def current() -> dict:
    """``{"terms": N, "privacy": N[, "hosted": N]}`` from the bundled documents (read once per process)."""
    return dict(_versions(docs()))


def accepted(settings: dict | None = None) -> dict | None:
    s = settings if settings is not None else config.get_settings()
    a = s.get("legal_accepted")
    return a if isinstance(a, dict) else None


def needs_accept(settings: dict | None = None) -> bool:
    a = accepted(settings)
    cur = current()
    return not a or any(int(a.get(d) or 0) < cur[d] for d in cur)


def accept(terms: int, privacy: int, hosted_version: int | None = None) -> dict:
    """Record acceptance of exactly the current versions; ValueError otherwise.

    On a hosted seat the hosted document's version is required too.
    """
    cur = current()
    if int(terms) != cur["terms"] or int(privacy) != cur["privacy"]:
        raise ValueError("not the current version")
    if "hosted" in cur and int(hosted_version or 0) != cur["hosted"]:
        raise ValueError("not the current version")
    rec = {**cur, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    hist = [h for h in (config.get_settings().get("legal_history") or []) if isinstance(h, dict)]
    config.save_settings({"legal_accepted": rec, "legal_history": (hist + [rec])[-_HISTORY_MAX:]})
    return rec


def status(settings: dict | None = None) -> dict:
    s = settings if settings is not None else config.get_settings()
    return {**current(), "accepted": accepted(s), "needs_accept": needs_accept(s),
            "history": s.get("legal_history") or []}
