"""The 18+ step and what it locks for under-18s (LEGALPAGES, 4.58.0).

Decided 2026-10-04 (docs/research/legal_privacy_terms_2026-10-04.md): under-18s may use PawPoller for
safe-for-work work. Someone who tells the app they're under 18 gets:

  * no Mature or Adult ratings — every post and publish of a rated piece is refused before the network;
  * no adults-only sites (the ones whose own terms are 18+);
  * safe mode on for good (the 18+ pill can't be turned off; the page enforces it).

It's a self-declared step, which is the proportionate standard for software people run themselves.
Stored as ``age_band``: ``"adult"`` / ``"under18"`` / unset (asked once).

``SITE_AGES`` is each connected site's OWN minimum age, from its terms, looked up 2026-10-08 — shown in
Settings so nobody has to guess, and the source of the adults-only list. ``checked=False`` marks what
couldn't be confirmed from the site's own terms; those say "check the site's terms" rather than guess.
"""
from __future__ import annotations

import config

ADULT = "adult"
UNDER_18 = "under18"

# code: (minimum age, adults only?, note, confirmed from the site's own terms?)
SITE_AGES: dict[str, dict] = {
    "fa":   {"min": 18, "adult_only": True,  "checked": True,
             "note": "18+; 13–17 only through a parent's or guardian's account, supervised"},
    "ib":   {"min": 18, "adult_only": True,  "checked": True, "note": "Adults only"},
    "e621": {"min": 18, "adult_only": True,  "checked": True, "note": "Adults only (e926 too)"},
    "ws":   {"min": 13, "adult_only": False, "checked": True, "note": "13+; Mature and Explicit work 18+"},
    "da":   {"min": 13, "adult_only": False, "checked": True, "note": "13+; mature content 18+"},
    "ao3":  {"min": 13, "adult_only": False, "checked": True,
             "note": "13+; 16 where the law sets data consent at 16 (much of the EU)"},
    "wp":   {"min": 13, "adult_only": False, "checked": True, "note": "13+; mature stories 17+"},
    "bsky": {"min": 13, "adult_only": False, "checked": True,
             "note": "13+ and old enough locally to agree to terms; some regions need age assurance"},
    "tum":  {"min": 13, "adult_only": False, "checked": True,
             "note": "13+; 16 in Australia, the EU and the UK; 14 in South Korea"},
    "pix":  {"min": 13, "adult_only": False, "checked": False, "note": "Under-18s allowed; R-18 work 18+"},
    "ng":   {"min": 13, "adult_only": False, "checked": False,
             "note": "13–17 with a parent's permission; Adult work 18+"},
    "sc":   {"min": 13, "adult_only": False, "checked": True, "note": "13+; 16 in the EU and the UK"},
    "tg":   {"min": 16, "adult_only": False, "checked": True,
             "note": "18+ in the EU, the UK and Australia; 16 elsewhere"},
    "mast": {"min": 16, "adult_only": False, "checked": True,
             "note": "Set by each server — mastodon.social is 16+; some furry servers are 18+"},
    "thr":  {"min": 13, "adult_only": False, "checked": True, "note": "13+; 16 in Australia"},
    "ig":   {"min": 13, "adult_only": False, "checked": True, "note": "13+; higher in some places (16 in Australia)"},
    "fb":   {"min": 13, "adult_only": False, "checked": False, "note": "13+ (Meta's terms, as Instagram); 16 in Australia"},
    "tw":   {"min": 13, "adult_only": False, "checked": True, "note": "13+; 16 in Australia"},
    "yt":   {"min": 13, "adult_only": False, "checked": True, "note": "13+; 16 in Australia for an account"},
    "sf":   {"min": 18, "adult_only": False, "checked": False, "note": "Check SoFurry's terms"},
    "ik":   {"min": 13, "adult_only": False, "checked": False, "note": "Check Itaku's terms"},
    "fbr":  {"min": 18, "adult_only": False, "checked": False, "note": "Check Furbooru's terms"},
    "r34":  {"min": 18, "adult_only": True, "checked": False, "note": "An adults-only site"},
    "fn":   {"min": 18, "adult_only": False, "checked": False, "note": "Check FurryNetwork's terms"},
    "sqw":  {"min": 13, "adult_only": False, "checked": False, "note": "Check SquidgeWorld's terms"},
    "pic":  {"min": 18, "adult_only": False, "checked": False,
             "note": "Check Picarto's terms (reported 18+, younger streamers supervised)"},
    "pod":  {"min": 0,  "adult_only": False, "checked": True, "note": "Your own podcast feed"},
}


def band(settings: dict | None = None) -> str:
    s = settings if settings is not None else config.get_settings()
    v = str(s.get("age_band") or "").strip().lower()
    return v if v in (ADULT, UNDER_18) else ""


def is_under_18(settings: dict | None = None) -> bool:
    return band(settings) == UNDER_18


def set_band(value: str) -> None:
    if value not in (ADULT, UNDER_18):
        raise ValueError("age_band must be 'adult' or 'under18'")
    config.save_settings({"age_band": value})


def refusal(platform: str, rating: str | None, *, site_name: str = "",
            settings: dict | None = None) -> str | None:
    """One sentence when an under-18 may not post this here, else None. Checked before the network."""
    if not is_under_18(settings):
        return None
    name = site_name or platform
    info = SITE_AGES.get(platform) or {}
    if info.get("adult_only"):
        return f"{name} is for adults only (18+), so it's locked on this copy of PawPoller."
    from posting.platforms.base import rating_rank
    if rating_rank(rating) > 0:
        return "Mature and adult ratings are locked for under-18s. Rate it General to post it."
    return None


def table() -> list[dict]:
    from database.accounts import PLATFORM_NAMES
    return [{"code": c, "name": PLATFORM_NAMES.get(c, c), **v} for c, v in SITE_AGES.items()]
