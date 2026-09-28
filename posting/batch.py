"""Post several artworks at once (4.43.0, spec 010).

The story "drip" (``routes/editor_api.drip_schedule``) applied to pieces instead of
chapters: one decision becomes, after EVERY piece × site has been checked, ordinary
``posting_queue`` rows that share a ``drip_group`` — the scheduler fires them unchanged,
and the Queue page cancels the group as one.

``plan()`` has no side effects. It runs the same checks a single publish runs
(``manager.post_artwork``: persona account, the rating's render, the package build,
the site's own ``refusal`` and ``validate``) plus two a batch needs and a single
publish does not: "already posted there" and "belongs to another persona". Nothing
``plan()`` would skip is ever queued: ``queue()`` only writes what a plan says.

Pacing is the default (a catch-up batch of forty pieces posted back to back floods
every watcher's feed and invites the sites' spam limits); ``gap_minutes = 0`` is
"all now". Deterministic throughout — no LLM (the PawPoller rule).
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

from database import posting_queries
from database.db import get_connection

logger = logging.getLogger(__name__)

MAX_PIECES = 200
MIN_GAP_MINUTES = 15
MAX_GAP_MINUTES = 30 * 24 * 60
ORDERS = ("oldest", "newest", "selected")
ANNOUNCERS = ("tg", "tw", "bsky")          # posted last, and quiet by default in a batch


class BatchError(ValueError):
    """A request the batch refuses as a whole (the route turns it into a 400)."""


def _parse_start(start: str | None, now: datetime) -> datetime:
    if not start:
        return now
    try:
        dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    except ValueError:
        raise BatchError("Start time must be an ISO 8601 date and time")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if (dt - now).total_seconds() < -30:
        raise BatchError("Start time must be now or later")
    return dt.astimezone(timezone.utc)


def _date_key(art) -> str:
    return str(getattr(art, "original_posted_at", "") or getattr(art, "created_at", "") or "")


def _owner_personas(conn, art) -> set[int]:
    """Personas this piece already belongs to: the accounts it is live on, and the
    persona linked to its artist row. Empty = nobody's yet (never posted, no credit)."""
    from database import masterpiece_queries as mq
    from routes.masterpieces_api import _people_view
    owners = set(mq.rollup_members(conn, art.name)["persona_ids"])
    try:
        artist_persona = _people_view(conn, art)[1]
    except Exception:  # noqa: BLE001 — a missing artist table must not block a plan
        artist_persona = None
    if artist_persona is not None:
        owners.add(int(artist_persona))
    return owners


# ── Versions (renders) — per piece, the same choices a single publish offers (4.34.0) ──
# AUTO = the rating's pick for each site (the default, and all a piece without named
# versions ever gets); PRIMARY = the piece's own image even where a version would be
# picked; a named key = that version. Each chosen version is its own submission.
AUTO, PRIMARY = "__auto__", "__primary__"
AUTO_LABEL = "Automatic — the fullest version each site allows"
MAX_RENDERS = 32
SITE_SPACING_MARGIN = 30          # seconds added to a site's own minimum gap between rows


def versions_of(art) -> list[dict]:
    """The version choices for a piece — empty when it has no named versions."""
    named = [v for v in (getattr(art, "variants", None) or [])
             if isinstance(v, dict) and v.get("key") and v.get("image")]
    if not named:
        return []
    return ([{"key": AUTO, "label": AUTO_LABEL},
             {"key": PRIMARY, "label": "The original", "rating": getattr(art, "rating", "") or ""}]
            + [{"key": v["key"], "label": v.get("label") or v["key"], "rating": v.get("rating") or ""}
               for v in named])


def _already_on(conn, name: str) -> tuple[set[str], set[tuple[str, str]]]:
    """Where the piece is live: (sites, (site, version-key) pairs). '' is the original."""
    from database import masterpiece_queries as mq
    pairs = {(m["platform"], m.get("variant_key") or "") for m in mq.get_members(conn, name)}
    pairs |= {(p["platform"], p.get("variant_key") or "") for p in posting_queries.get_publications(
        conn, story_name=name, status="posted", content_type="artwork")}
    return {site for site, _ in pairs}, pairs


def _site_check(art, platform: str, account_id, persona_id, renders=(AUTO,)):
    """The single-publish checks for one site, per chosen version — no post.

    Returns a string when the SITE is refused (no account for this persona, no poster),
    else one dict per distinct version: ``row_key`` (what the queue row stores),
    ``resolved`` (the real variant key, '' = the original), ``label``, ``reason``
    (None = would post), ``requires`` and ``interval`` (the site's minimum gap)."""
    from posting import artwork_reader, manager
    if persona_id is None and account_id is None:
        # The no-persona path would CREATE a default account row; a plan writes nothing.
        from database import accounts as accounts_db
        conn = get_connection()
        try:
            account_id = accounts_db.get_default_account_id(conn, platform, create=False)
        finally:
            conn.close()
    else:
        try:
            account_id = manager._resolve_account_id(platform, account_id, persona_id)
        except ValueError as e:
            return str(e)
    try:
        poster = manager._get_poster(platform, account_id)
    except Exception as e:  # noqa: BLE001
        return f"{platform} is not available ({type(e).__name__})"
    requires = getattr(poster, "requires_mode", "any") or "any"
    interval = int(getattr(poster, "min_post_interval", 5) or 0)
    variants = {v.get("key"): v for v in (getattr(art, "variants", None) or []) if isinstance(v, dict)}
    out, seen = [], set()
    for key in renders:
        if key == AUTO:
            variant, row_key = artwork_reader.variant_for_rating(art, getattr(poster, "max_rating", "adult")), ""
        elif key == PRIMARY:
            variant, row_key = None, PRIMARY
        else:
            variant, row_key = variants.get(key), key
            if variant is None:
                out.append({"row_key": key, "resolved": key, "label": key, "requires": requires,
                            "interval": interval, "reason": f"no version called {key!r} on this piece"})
                continue
        resolved = (variant or {}).get("key") or ""
        if resolved in seen:                  # Automatic landed on a version also ticked by name
            continue
        seen.add(resolved)
        label = (variant or {}).get("label") or (variant or {}).get("key") or "The original"
        entry = {"row_key": row_key, "resolved": resolved, "label": label, "requires": requires,
                 "interval": interval, "reason": None}
        try:
            package = artwork_reader.build_artwork_package(
                art, platform, variant_key=resolved or None, multi_render=len(renders) > 1,
                account_id=account_id)
        except FileNotFoundError as e:
            entry["reason"] = f"file missing: {e}"
            out.append(entry)
            continue
        except Exception as e:  # noqa: BLE001
            entry["reason"] = f"could not prepare it ({type(e).__name__}: {e})"
            out.append(entry)
            continue
        refusal = poster.refusal(package) if hasattr(poster, "refusal") else None
        errors = [refusal] if refusal else poster.validate(package)
        entry["reason"] = "; ".join(errors) if errors else None
        out.append(entry)
    return out


def _clean_renders(renders) -> dict[str, list[str]]:
    if renders in (None, ""):
        return {}
    if not isinstance(renders, dict):
        raise BatchError("renders must map a piece to the list of versions to post")
    out = {}
    for name, keys in renders.items():
        if not isinstance(keys, list) or not all(isinstance(k, str) and k for k in keys):
            raise BatchError("each piece's versions must be a list of version names")
        if len(keys) > MAX_RENDERS:
            raise BatchError(f"at most {MAX_RENDERS} versions per piece")
        out[str(name)] = list(dict.fromkeys(keys))
    return out


def plan(names: list[str], *, platforms: list[str], persona_id: int | None = None,
         account_ids: dict | None = None, start: str | None = None, gap_minutes: int = 360,
         order: str = "oldest", announce: bool = False, renders: dict | None = None,
         now: datetime | None = None) -> dict:
    """What a batch would do — per piece its slot and, per site and version, post or skip + why.

    ``renders`` maps a piece to the versions to post there (``__auto__``, ``__primary__``,
    or named keys); absent = Automatic, the rating's pick per site."""
    from posting import artwork_reader
    now = now or datetime.now(timezone.utc)
    if not isinstance(names, list) or not isinstance(platforms or [], list):
        raise BatchError("names and platforms must be lists")
    if len(names) > MAX_PIECES:
        raise BatchError(f"A batch takes at most {MAX_PIECES} pieces — split it into smaller batches")
    names = list(dict.fromkeys(n for n in names if isinstance(n, str) and n))
    if not names:
        raise BatchError("Pick at least one piece")
    platforms = list(dict.fromkeys(p for p in (platforms or []) if isinstance(p, str) and p))
    if not platforms:
        raise BatchError("Pick at least one site")
    if order not in ORDERS:
        raise BatchError(f"order must be one of {', '.join(ORDERS)}")
    try:
        gap = int(gap_minutes)
    except (TypeError, ValueError):
        raise BatchError("gap_minutes must be a number of minutes")
    if gap and not (MIN_GAP_MINUTES <= gap <= MAX_GAP_MINUTES):
        raise BatchError("The gap between pieces must be between 15 minutes and 30 days (or 0 for all now)")
    start_dt = _parse_start(start, now)
    try:
        account_ids = {str(k): int(v) for k, v in (account_ids or {}).items() if v not in (None, "")}
    except (TypeError, ValueError, AttributeError):
        raise BatchError("account_ids must map a site to an account number")
    chosen_by_piece = _clean_renders(renders)
    if persona_id is not None:
        try:
            persona_id = int(persona_id)
        except (TypeError, ValueError):
            raise BatchError("persona_id must be a persona number")

    conn = get_connection()
    try:
        if persona_id is None and conn.execute(
                "SELECT 1 FROM accounts WHERE enabled = 1 AND persona_id IS NOT NULL LIMIT 1").fetchone():
            # "All accounts" can't say whose art this is — the check that keeps one
            # persona's work off another persona's accounts needs a persona. Required
            # exactly when accounts belong to personas: the same condition under which
            # the picker offers persona chips at all (Components.personaPicker).
            raise BatchError("Choose who these are posted as — a batch is always posted as one persona")

        loaded = []
        for i, name in enumerate(names):
            try:
                art = artwork_reader.load_artwork(name)
            except Exception:  # noqa: BLE001
                art = None
            # Only a piece's EXACT folder name counts. load_artwork also resolves `./Name`,
            # `x/../Name` and (on Windows) `NAME` — but the owner / already-posted lookups and
            # the queue rows key on the name as given, so a variant spelling would slip past
            # both checks and file its posts under a name that isn't the piece (4.43.0 review).
            if art is not None and Path(str(art.path)).name != name:
                art = None
            loaded.append((i, name, art))
        if order != "selected":
            # By the piece's own date; ties keep the order they were picked in; unloadable
            # pieces go last (they are reported, never queued).
            dated = [t for t in loaded if t[2] is not None]
            if order == "oldest":
                dated.sort(key=lambda t: (_date_key(t[2]), t[0]))
            else:
                dated.sort(key=lambda t: (_date_key(t[2]), -t[0]), reverse=True)
            loaded = dated + [t for t in loaded if t[2] is None]

        pieces, k = [], 0
        for _, name, art in loaded:
            entry = {"name": name, "title": name, "slot": None, "posts": [], "skips": [], "skip": None,
                     "versions": [], "chosen": [AUTO]}
            if art is None:
                entry["skip"] = "not found"
                pieces.append(entry)
                continue
            entry["title"] = art.title or name
            entry["versions"] = versions_of(art)
            chosen = chosen_by_piece.get(name, [AUTO]) if entry["versions"] else [AUTO]
            entry["chosen"] = chosen
            if not chosen:
                entry["skip"] = "no version chosen"
                pieces.append(entry)
                continue
            if persona_id is not None:
                owners = _owner_personas(conn, art)
                if owners and int(persona_id) not in owners:
                    entry["skip"] = "belongs to another persona"
                    pieces.append(entry)
                    continue
            live_sites, live_pairs = _already_on(conn, name)
            multi = len(chosen) > 1
            for plat in platforms:
                # Automatic alone keeps the site-level rule: any version already there counts.
                if chosen == [AUTO] and plat in live_sites:
                    entry["skips"].append({"platform": plat, "reason": "already posted there"})
                    continue
                res = _site_check(art, plat, account_ids.get(plat), persona_id, tuple(chosen))
                if isinstance(res, str):
                    entry["skips"].append({"platform": plat, "reason": res})
                    continue
                for r in res:
                    version = r["label"] if (multi or entry["versions"]) else ""
                    already = (plat in live_sites) if r["row_key"] == "" else ((plat, r["resolved"]) in live_pairs)
                    if r["reason"]:
                        entry["skips"].append({"platform": plat, "version": version, "reason": r["reason"]})
                    elif already:
                        entry["skips"].append({"platform": plat, "version": version,
                                               "reason": "already posted there"})
                    else:
                        entry["posts"].append({"platform": plat, "requires": r["requires"],
                                               "account_id": account_ids.get(plat), "variant_key": r["row_key"],
                                               "version": version, "interval": r["interval"]})
            if entry["posts"]:
                entry["slot"] = (start_dt + timedelta(minutes=gap * k)).strftime("%Y-%m-%d %H:%M:%S")
                k += 1
            else:
                entry["skip"] = entry["skip"] or "nothing to post — every site was skipped"
            pieces.append(entry)
    finally:
        conn.close()

    posts = sum(len(p["posts"]) for p in pieces)
    return {
        "pieces": pieces,
        "totals": {"pieces": k, "posts": posts, "skipped_pieces": len(pieces) - k},
        "gap_minutes": gap, "order": order, "announce": bool(announce), "persona_id": persona_id,
        "first_slot": next((p["slot"] for p in pieces if p["slot"]), None),
        "last_slot": next((p["slot"] for p in reversed(pieces) if p["slot"]), None),
    }


def post_set(p: dict) -> list[list[str]]:
    """The planned (piece, site, version) triples — what the confirm step compares."""
    return sorted([piece["name"], post["platform"], post.get("variant_key") or ""]
                  for piece in p["pieces"] for post in piece["posts"])


def queue(p: dict) -> dict:
    """Write a plan's posts as queue rows. Returns the group id and counts.

    Rows for the SAME site are spaced by that site's own minimum gap (FurAffinity 70 s)
    plus a margin: ``poster._rate_limit`` only spaces uploads inside one publish call, and
    the scheduler fires due rows ~5 s apart — so "All now", or several versions to one
    site, would otherwise hit FurAffinity faster than it accepts."""
    from posting.manager import _announcers_last
    group = uuid.uuid4().hex[:12]
    todo = [piece for piece in p["pieces"] if piece["posts"]]
    total = len(todo)
    rows = 0
    last: dict[str, datetime] = {}
    conn = get_connection()
    try:
        for i, piece in enumerate(todo):
            slot = datetime.strptime(piece["slot"], "%Y-%m-%d %H:%M:%S")
            by_plat: dict[str, list] = {}
            for x in piece["posts"]:
                by_plat.setdefault(x["platform"], []).append(x)
            for plat in _announcers_last(list(by_plat)):
                for x in by_plat[plat]:
                    t = slot
                    if plat in last:
                        t = max(t, last[plat] + timedelta(seconds=int(x.get("interval") or 5) + SITE_SPACING_MARGIN))
                    last[plat] = t
                    posting_queries.add_to_queue(
                        conn, piece["name"], 0, plat, action="post",
                        account_id=x.get("account_id"), content_type="artwork",
                        scheduled_at=t.strftime("%Y-%m-%d %H:%M:%S"), requires=x.get("requires") or "any",
                        drip_group=group, title_override=f"🎨 batch {i + 1}/{total}",
                        variant_key=x.get("variant_key") or "",
                        announce=1 if p.get("announce") else 0,
                    )
                    rows += 1
    finally:
        conn.close()
    logger.info("Batch %s: %d pieces, %d rows, every %d min", group, total, rows, p.get("gap_minutes") or 0)
    return {"drip_group": group, "pieces": total, "rows": rows}
