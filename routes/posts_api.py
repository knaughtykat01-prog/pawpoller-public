"""Posts module (microblog publishing) REST API — 2.49.0.

Compose short-form posts and publish them to microblog platforms (Bluesky +
Mastodon in Phase 2). Mirrors the shape of the Artwork hub API: a library list,
create (multipart so an optional image can ride along), publish, delete, and a
query-param image server.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import json

from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import FileResponse

import config
from database.db import get_connection
from database import posts_queries
from posting import post_publisher

logger = logging.getLogger(__name__)
posts_router = APIRouter(prefix="/api/posts")

_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_ALLOWED_RATINGS = {"general", "mature", "adult"}
_MAX_IMAGE_BYTES = 25 * 1024 * 1024
_MAX_IMAGES = 4        # X / Bluesky / Mastodon all cap a post at 4 images


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _to_utc_sql(scheduled_at: str) -> str:
    """Parse an ISO 8601 string → UTC 'YYYY-MM-DD HH:MM:SS' (SQLite shape).

    Naive datetimes are treated as UTC; a time >30s in the past is rejected
    (30s grace for clock skew). Same contract as the story/artwork schedulers,
    so a post scheduled for 8pm local fires at 8pm local. Raises HTTPException.
    """
    try:
        dt = datetime.fromisoformat(scheduled_at)
    except ValueError:
        raise HTTPException(400, detail="Invalid datetime format — use ISO 8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if (dt - datetime.now(timezone.utc)).total_seconds() < -30:
        raise HTTPException(400, detail="Scheduled time must be in the future")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _media_dir() -> Path:
    d = config.DATA_DIR / "posts_media"
    d.mkdir(parents=True, exist_ok=True)
    return d


@posts_router.get("")
def list_posts(limit: int = Query(100, ge=1, le=500), status: str | None = Query(None),
               persona_id: int | None = Query(None), q: str | None = Query(None, max_length=200)):
    """The Posts feed — newest first, each post with its numbers (spec 018).

    Every publication on the page is resolved against the platforms' stored rows in ONE
    batched pass (one query per platform, however many posts), so the feed shows each
    site's likes / reposts / replies / views without a request per post. Filters:
    ``status`` scheduled | failed, ``persona_id``, ``q`` (literal text search)."""
    status = status if status in ("scheduled", "failed") else None
    conn = get_connection()
    try:
        posts = posts_queries.list_posts(conn, limit=limit, status=status,
                                         persona_id=persona_id, q=q)
        personas = _attach_numbers(conn, posts)
        return {"posts": posts, "counts": posts_queries.post_counts(conn),
                "personas": personas}
    finally:
        conn.close()


def _attach_numbers(conn, posts: list[dict]) -> list[dict]:
    """Resolve every listed post's publications in one pass; add totals, the sites that
    failed, and the persona each post went out as. Returns the persona list (for the filter)."""
    from database import collections_queries as cq
    from database import personas as personas_db
    flat = [pub for p in posts for pub in p.get("publications") or []]
    resolved = _resolve_post_publications(conn, flat) if flat else []
    a2p = cq._acct_to_persona(conn)
    personas = {pp["persona_id"]: {"persona_id": pp["persona_id"], "name": pp["name"],
                                   "color": pp.get("color") or ""}
                for pp in personas_db.list_personas(conn)}
    i = 0
    for p in posts:
        n = len(p.get("publications") or [])
        p["publications"] = resolved[i:i + n]
        i += n
        p["totals"] = _post_totals(p["publications"])
        p["failed_sites"] = _failed_sites(p["publications"])
        p["persona"] = _persona_of(p, a2p, personas)
    return list(personas.values())


def _failed_sites(pubs: list[dict]) -> list[str]:
    """Sites where the post failed and did not post on any account (FR-002)."""
    posted = {p["platform"] for p in pubs if p.get("status") == "posted"}
    out: list[str] = []
    for p in pubs:
        if p.get("status") == "failed" and p["platform"] not in posted and p["platform"] not in out:
            out.append(p["platform"])
    return out


def _persona_of(post: dict, a2p: dict, personas: dict) -> dict | None:
    """The persona a post went out (or is scheduled to go out) as; None = no persona."""
    for pub in post.get("publications") or []:
        pid = a2p.get(pub.get("account_id"))
        if pid in personas:
            return personas[pid]
    for s in post.get("scheduled") or []:
        pid = s.get("persona_id") or a2p.get(s.get("account_id"))
        if pid in personas:
            return personas[pid]
    return None


@posts_router.get("/summary")
def posts_summary():
    """The header strip and the side column (spec 018 US3, FR-003) — stored data only.

    Months run in the operator's time zone. "Likes" are the likes the month's posts hold
    now, not likes received during the month (no site reports those per day)."""
    conn = get_connection()
    try:
        return _summary(conn)
    finally:
        conn.close()


def _summary(conn) -> dict:
    from database import platform_metrics
    fmt = "%Y-%m-%d %H:%M:%S"
    now_local = datetime.now(config.display_zone())
    start_this = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_last = (start_this - timedelta(days=1)).replace(day=1)
    t_this = start_this.astimezone(timezone.utc).strftime(fmt)
    t_last = start_last.astimezone(timezone.utc).strftime(fmt)
    t_30 = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(fmt)
    since = min(t_last, t_30)

    recent: dict[int, tuple[str, str]] = {}
    for r in conn.execute("SELECT post_id, body, created_at FROM posts "
                          "WHERE COALESCE(parent_post_id, 0) = 0").fetchall():
        when = platform_metrics.normalize_posted(r["created_at"])
        if when and when >= since:
            recent[r["post_id"]] = (when, r["body"] or "")
    ids = list(recent)
    pubs: list[dict] = []
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        pubs += [dict(x) for x in conn.execute(
            f"SELECT * FROM post_publications WHERE post_id IN ({','.join('?' * len(chunk))})",
            chunk).fetchall()]
    by_post: dict[int, list] = {}
    for pub in (_resolve_post_publications(conn, pubs) if pubs else []):
        by_post.setdefault(pub["post_id"], []).append(pub)

    s = {"posts_this_month": 0, "posts_last_month": 0, "likes_this_month": 0,
         "likes_last_month": 0, "reposts_this_month": 0, "best_site": None,
         "best_post": None, "by_site_30d": [], "zone": str(config.display_zone())}
    site_month: dict[str, int] = {}
    site_30: dict[str, int] = {}
    best = None
    for pid, (when, body) in recent.items():
        plist = by_post.get(pid, [])
        if not any(p.get("status") == "posted" for p in plist):
            continue                                   # a draft or a failure is not a post
        tot = _post_totals(plist)
        if when >= t_this:
            s["posts_this_month"] += 1
            s["likes_this_month"] += tot["favorites"]
            s["reposts_this_month"] += tot["reposts"]
            if tot["favorites"] and (best is None or tot["favorites"] > best["favorites"]):
                best = {"post_id": pid, "body": " ".join(body.split())[:140],
                        "favorites": tot["favorites"], "reposts": tot["reposts"]}
        elif t_last <= when < t_this:
            s["posts_last_month"] += 1
            s["likes_last_month"] += tot["favorites"]
        for p in plist:
            fav = (p.get("stats") or {}).get("favorites")
            if fav is None:
                continue
            if when >= t_this:
                site_month[p["platform"]] = site_month.get(p["platform"], 0) + int(fav)
            if when >= t_30:
                site_30[p["platform"]] = site_30.get(p["platform"], 0) + int(fav)
    s["best_post"] = best
    if site_month and max(site_month.values()) > 0:
        s["best_site"] = max(site_month, key=site_month.get)
    s["by_site_30d"] = [{"platform": k, "favorites": v}
                        for k, v in sorted(site_30.items(), key=lambda kv: -kv[1])]

    # Coming up: the next three scheduled posts, one line each.
    nxt: dict[int, dict] = {}
    for row in posts_queries.pending_post_schedules(conn):
        e = nxt.get(row["post_id"])
        if e is None:
            if len(nxt) >= 3:
                continue
            e = nxt[row["post_id"]] = {"post_id": row["post_id"],
                                       "scheduled_at": row["scheduled_at"], "platforms": []}
        if row["platform"] not in e["platforms"]:
            e["platforms"].append(row["platform"])
    for e in nxt.values():
        post = posts_queries.get_post(conn, e["post_id"]) or {}
        e["body"] = " ".join((post.get("body") or "").split())[:90]
        e["thread_count"] = len(posts_queries.get_thread_parts(conn, e["post_id"]))
    s["coming_up"] = list(nxt.values())
    return s


@posts_router.get("/rules")
def posts_rules():
    """Each site's character limit and media rules — the composer's one source (FR-005)."""
    return post_publisher.rules()


@posts_router.post("/preview")
def preview_post(payload: dict):
    """How a draft comes out on each chosen site, with every reason one would refuse or
    change it (FR-004). Nothing is stored or sent."""
    body = str(payload.get("body") or "")[:20000]
    platforms = [str(p) for p in (payload.get("platforms") or [])][:12]
    parts = [str(p)[:20000] for p in (payload.get("parts") or [])][:24]
    mentions = payload.get("mentions") if isinstance(payload.get("mentions"), list) else []
    mentions = mentions[:50]
    ids = payload.get("account_ids") if isinstance(payload.get("account_ids"), dict) else {}
    account_ids = {}
    for k, v in ids.items():
        try:
            account_ids[str(k)] = int(v)
        except (TypeError, ValueError):
            pass
    try:
        image_count = max(0, min(int(payload.get("image_count") or 0), _MAX_IMAGES))
    except (TypeError, ValueError):
        image_count = 0
    return {"sites": post_publisher.preview(body, platforms, mentions, image_count, parts,
                                            account_ids)}


@posts_router.get("/image")
def get_post_image(post_id: int = Query(...), idx: int = Query(0, ge=0)):
    """Serve one of a post's attached images (traversal-safe: path derives from
    the post's own stored media, never from user input). `idx` selects which
    image (0-based); it defaults to 0 so existing single-image links still work."""
    conn = get_connection()
    try:
        post = posts_queries.get_post(conn, post_id)
    finally:
        conn.close()
    media = (post or {}).get("media") or []
    if not post or idx >= len(media) or not media[idx].get("path"):
        raise HTTPException(404, "No image for this post")
    p = Path(media[idx]["path"]).resolve()
    if _media_dir().resolve() not in p.parents or not p.is_file():
        raise HTTPException(404, "Image not found")
    return FileResponse(str(p))


# ── Handle-book (contacts) ─────────────────────────────────────────
# Defined BEFORE the "/{post_id}" routes so "/contacts" isn't captured as a
# post id. A contact carries a person's per-platform @handle so the composer can
# tag them with one alias and the publisher expands it per network.

_CONTACT_KEYS = ("name", "alias", "handle_bsky", "handle_tw", "handle_mast", "handle_thr",
                 "handle_tum")


@posts_router.get("/contacts")
def list_contacts():
    conn = get_connection()
    try:
        return {"contacts": posts_queries.list_contacts(conn)}
    finally:
        conn.close()


@posts_router.get("/contacts/suggest")
def suggest_contacts():
    """@names from the operator's own posts that no contact answers to yet (spec 018 US5)."""
    conn = get_connection()
    try:
        return {"suggestions": posts_queries.suggest_contacts(conn)}
    finally:
        conn.close()


@posts_router.post("/contacts")
def create_contact(payload: dict):
    fields = {k: str(payload.get(k, "") or "") for k in _CONTACT_KEYS}
    if not fields["name"].strip():
        raise HTTPException(400, "A contact needs a name")
    conn = get_connection()
    try:
        clash = posts_queries.tag_conflict(conn, fields["name"], fields["alias"])
        if clash:
            raise HTTPException(409, clash)
        cid = posts_queries.add_contact(conn, **fields)
        return {"contact": posts_queries.get_contact(conn, cid)}
    finally:
        conn.close()


@posts_router.patch("/contacts/{contact_id}")
def update_contact(contact_id: int, payload: dict):
    fields = {k: str(payload[k]) for k in _CONTACT_KEYS if k in payload}
    conn = get_connection()
    try:
        current = posts_queries.get_contact(conn, contact_id)
        if not current:
            raise HTTPException(404, "Contact not found")
        if "name" in fields and not fields["name"].strip():
            raise HTTPException(400, "A contact needs a name")
        clash = posts_queries.tag_conflict(conn, fields.get("name", current["name"]),
                                           fields.get("alias", current.get("alias") or ""),
                                           exclude_id=contact_id)
        if clash:
            raise HTTPException(409, clash)
        posts_queries.update_contact(conn, contact_id, **fields)
        return {"contact": posts_queries.get_contact(conn, contact_id)}
    finally:
        conn.close()


# ── Handle check (spec 018 T015, optional) ──────────────────────────
# "✓ found" beside a handle the site itself confirms exists. Only Bluesky (handle
# resolution on the public AppView) and Mastodon (WebFinger on the person's own
# instance) can be asked without logging in; every other site stays blank.

_HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_BSKY_HANDLE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_MAST_USER_RE = re.compile(r"^\w{1,64}$")


def _public_host(host: str) -> str | None:
    """`host` when it is a DNS name that resolves only to public addresses, else None.

    The Mastodon check fetches from an instance the operator typed, so it must never be
    pointed at this server's own network (localhost, the cloud metadata address, a LAN).
    IP literals and bare names fail the name pattern before any lookup.
    ponytail: the address is checked here and resolved again by the HTTP client, so a
    host that re-points between the two (DNS rebinding) could slip past; an operator-only
    endpoint behind the dashboard login makes that acceptable for now."""
    h = (host or "").strip().lower().rstrip(".")
    if not _HOST_RE.match(h):
        return None
    try:
        infos = socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return None
    try:
        addrs = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}
    except ValueError:
        return None
    return h if addrs and all(a.is_global for a in addrs) else None


async def _check_bsky(handle: str) -> bool | None:
    """True / False when Bluesky answered, None when it couldn't be asked."""
    import httpx
    if not _BSKY_HANDLE_RE.match(handle or ""):
        return False
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as http:
            r = await http.get("https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle",
                               params={"handle": handle})
    except httpx.HTTPError:
        return None
    if r.status_code == 200:
        try:
            return bool((r.json() or {}).get("did"))
        except ValueError:
            return None
    return False if r.status_code == 400 else None


async def _check_mast(handle: str) -> bool | None:
    import httpx
    user, _, host = (handle or "").partition("@")
    if not _MAST_USER_RE.match(user):
        return False
    host = await asyncio.to_thread(_public_host, host)
    if not host:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as http:
            r = await http.get(f"https://{host}/.well-known/webfinger",
                               params={"resource": f"acct:{user}@{host}"})
    except httpx.HTTPError:
        return None
    if r.status_code == 200:
        return True
    return False if r.status_code == 404 else None


@posts_router.post("/contacts/{contact_id}/check")
async def check_contact_handles(contact_id: int):
    """Look up the contact's Bluesky and Mastodon handles and cache the answers."""
    conn = get_connection()
    try:
        c = posts_queries.get_contact(conn, contact_id)
    finally:
        conn.close()
    if not c:
        raise HTTPException(404, "Contact not found")
    checks = dict(c.get("checks") or {})
    now = _now()
    for plat, fn in (("bsky", _check_bsky), ("mast", _check_mast)):
        handle = (c.get(f"handle_{plat}") or "").strip()
        if not handle:
            checks.pop(plat, None)
            continue
        checks[plat] = {"handle": handle, "found": await fn(handle), "at": now}
    conn = get_connection()
    try:
        posts_queries.set_contact_checks(conn, contact_id, checks)
    finally:
        conn.close()
    return {"checks": checks}


@posts_router.delete("/contacts/{contact_id}")
def delete_contact(contact_id: int):
    conn = get_connection()
    try:
        posts_queries.delete_contact(conn, contact_id)
        return {"status": "deleted"}
    finally:
        conn.close()


# ── Import discovered microblog posts (2.157.0) ───────────────────────────────
# Declared BEFORE the generic `/{post_id}` routes so their literal path segments
# aren't shadowed — same ordering rule the artwork + masterpieces routers follow.
#
# The discovered queue was mostly text tweets, and its only import made an
# *artwork* (downloads an image, mints an artwork folder) — meaningless for a
# post with no image. See posting/post_importer.py for the reasoning.

@posts_router.post("/import/discovered")
def import_all_discovered_posts():
    """Import every discovered text post across the microblog platforms.

    One-click "bring my polled posts in". Per-item failures are collected, not
    fatal. Imported items leave the discovered queue (their `post_publications`
    row is one of its exclusion sets).
    """
    from posting import post_importer
    try:
        return post_importer.import_all_discovered_posts()
    except Exception as e:
        logger.error("Bulk post import failed: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@posts_router.get("/import/count")
def importable_post_count():
    """How many of the operator's own microblog posts the poller found that aren't in
    Posts yet — the feed's "bring them in" banner (spec 018 US3)."""
    from posting import post_importer
    from routes.submissions_api import get_discovered_unlinked
    conn = get_connection()
    try:
        items = get_discovered_unlinked(conn)
    finally:
        conn.close()
    return {"count": sum(1 for it in items if post_importer.is_importable_post(it))}


@posts_router.post("/import/{platform}/{submission_id}")
def import_discovered_post(platform: str, submission_id: str):
    """Import ONE discovered microblog submission as a local post.

    Idempotent — re-importing returns the existing post rather than duplicating.
    """
    from posting import post_importer
    try:
        return post_importer.import_post(platform, submission_id)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.error("Post import failed for %s/%s: %s", platform, submission_id, e,
                     exc_info=True)
        raise HTTPException(500, detail=str(e))


@posts_router.get("/{post_id}")
def get_post(post_id: int):
    """One post, with every place it went and how each is doing.

    4.34.4 (UNIFORMITEM phase 4): a post goes to up to six platforms and each one
    writes a `post_publications` row, but until now there was no item page and the
    answer to "how did that post do" was nothing. `post_publications` records only
    WHERE it went -- the numbers live in each platform's own submissions table, keyed
    by the external_id -- so the publications are resolved against those here, using
    the same batched helper the Collection and Masterpiece rollups use (ONE query per
    platform, not one per publication).
    """
    conn = get_connection()
    try:
        post = posts_queries.get_post(conn, post_id)
        if not post:
            raise HTTPException(404, "Post not found")
        pubs = posts_queries.get_post_publications(conn, post_id)
        post["publications"] = _resolve_post_publications(conn, pubs)
        post["totals"] = _post_totals(post["publications"])
        # Thread parts 2+ are full post rows; the item page shows them with the
        # first so the Record section is the whole thing that went out.
        post["thread_parts"] = posts_queries.get_thread_parts(conn, post_id)
        return post
    finally:
        conn.close()


def _resolve_post_publications(conn, pubs: list[dict]) -> list[dict]:
    """Attach live stats to each publication. Never drops a row.

    A publication with no stored submission row still appears, with null stats --
    "posted, not measured yet" and "not posted" are different states and a page that
    renders them alike is the kind of confident-wrong surface this spec exists to
    remove.
    """
    from database import collections_queries as cq
    live = [(p.get("platform"), str(p.get("external_id") or ""))
            for p in pubs if p.get("external_id")]
    rows = cq._submission_rows_bulk(conn, live) if live else {}
    out = []
    for p in pubs:
        row = dict(p)
        sid = str(p.get("external_id") or "")
        loc = cq._location_from_row(
            p.get("platform"), sid, rows.get((p.get("platform"), sid)),
            url=p.get("external_url") or "", account_id=p.get("account_id"),
            source="post") if sid else None
        row["stats"] = dict((loc or {}).get("stats") or {
            "views": None, "favorites": None, "comments": None})
        row["stats"]["reposts"] = _reposts(p.get("platform"),
                                           rows.get((p.get("platform"), sid)) if sid else None)
        row["thumbnail_url"] = (loc or {}).get("thumbnail_url") or ""
        row["title"] = (loc or {}).get("title") or ""
        out.append(row)
    return out


def _reposts(platform: str, row: dict | None) -> int | None:
    """Reposts / retweets from a site's stored row (spec 018); None when the site doesn't
    count them or the post was never polled."""
    from database import platform_metrics
    spec = platform_metrics.get(platform)
    if not row or not spec:
        return None
    for col in ("reposts", "retweets"):
        if col in spec.extra:
            return int(row.get(col) or 0)
    return None


_TOTAL_KEYS = ("views", "favorites", "comments", "reposts")


def _post_totals(pubs: list[dict]) -> dict:
    """The headline row. Sums only what is known -- a None stays out of the sum
    rather than counting as 0, so "not tracked" never reads as "zero engagement".
    ``tracked`` says which numbers any site reported at all (spec 018: "views not
    tracked on X" instead of a 0)."""
    t = {"views": 0, "favorites": 0, "comments": 0, "reposts": 0, "sites": 0}
    tracked = dict.fromkeys(_TOTAL_KEYS, False)
    for p in pubs:
        if p.get("status") == "posted":
            t["sites"] += 1
        s = p.get("stats") or {}
        for k in _TOTAL_KEYS:
            if s.get(k) is not None:
                t[k] += int(s[k] or 0)
                tracked[k] = True
    t["tracked"] = tracked
    return t


@posts_router.get("/{post_id}/snapshots")
def get_post_snapshots(post_id: int):
    """Combined time-series across every platform this post went to (4.34.4).

    Routing only: `analytics_queries.get_combined_snapshots` already does this for
    Collections over (platform, submission_id) pairs, and a post's publications are
    the same shape. A second implementation would drift from the one the Collection
    chart uses and the two would disagree about the same numbers.
    """
    conn = get_connection()
    try:
        if not posts_queries.get_post(conn, post_id):
            raise HTTPException(404, "Post not found")
        from database import analytics_queries
        pairs = [(p["platform"], str(p["external_id"]))
                 for p in posts_queries.get_post_publications(conn, post_id)
                 if p.get("external_id")]
        if not pairs:
            return {"snapshots": []}
        return {"snapshots": analytics_queries.get_combined_snapshots(conn, pairs)}
    finally:
        conn.close()

@posts_router.post("")
async def create_post(
    body: str = Form(""),
    rating: str = Form("general"),
    image_alt: str = Form(""),
    mentions: str = Form(""),   # JSON [{token, contact_id}] — @alias → contact bindings
    parts: str = Form(""),      # JSON [string] — thread parts 2+ (text-only, gap-wave-3 §4)
    alts: str = Form(""),       # JSON [string] — ALT text per image, in order (spec 018)
    files: list[UploadFile] | None = File(None),
    file: UploadFile | None = File(None),   # legacy single-image field, still accepted
):
    """Create a draft post with up to 4 attached images.

    Accepts the `files` multi-field (current frontend) or a single legacy `file`.
    The first image is also mirrored into the legacy image_path/image_alt columns
    so the feed thumbnail and /image?post_id= (idx 0) keep working unchanged.
    `mentions` binds @alias tokens in the body to handle-book contacts so the
    publisher can expand each alias into the right per-platform handle."""
    body = (body or "").strip()
    rating = rating if rating in _ALLOWED_RATINGS else "general"
    try:
        alt_list = [str(a)[:1500] for a in (json.loads(alts) if alts else [])]
    except (ValueError, TypeError):
        alt_list = []
    if alt_list and not image_alt:
        image_alt = alt_list[0]
    uploads = [f for f in ((files or []) + ([file] if file else [])) if f is not None]
    uploads = uploads[:_MAX_IMAGES]
    if not body and not uploads:
        raise HTTPException(400, "A post needs text or an image")

    conn = get_connection()
    try:
        post_id = posts_queries.create_post(
            conn, body=body, rating=rating, image_alt=image_alt, now=_now())
        # Thread parts (gap-wave-3 §4): each part is a child post row, text-only.
        if parts:
            try:
                part_texts = json.loads(parts)
            except (ValueError, TypeError):
                part_texts = []
            if isinstance(part_texts, list):
                for i, txt in enumerate([t for t in part_texts
                                         if isinstance(t, str) and t.strip()][:24]):
                    posts_queries.create_post(
                        conn, body=txt.strip(), rating=rating, now=_now(),
                        parent_post_id=post_id, thread_ordinal=i + 1)
        bindings = []
        if mentions:
            try:
                parsed = json.loads(mentions)
                if isinstance(parsed, list):
                    bindings = parsed
            except (ValueError, TypeError):
                bindings = []   # malformed → just skip tagging, don't fail the post
        if bindings:
            posts_queries.set_post_mentions(conn, post_id, bindings)
    finally:
        conn.close()

    first_path = ""
    for idx, up in enumerate(uploads):
        ext = Path(up.filename or "").suffix.lower()
        if ext not in _ALLOWED_EXT:
            _cleanup(post_id)
            raise HTTPException(400, "Images must be PNG, JPG, GIF or WebP")
        data = await up.read()
        if len(data) > _MAX_IMAGE_BYTES:
            _cleanup(post_id)
            raise HTTPException(400, "An image is too large (max 25 MB)")
        dest = _media_dir() / f"{post_id}_{idx}{ext}"
        dest.write_bytes(data)
        conn = get_connection()
        try:
            posts_queries.add_post_media(
                conn, post_id=post_id, ordinal=idx, path=str(dest),
                alt=alt_list[idx] if idx < len(alt_list) else (image_alt if idx == 0 else ""))
        finally:
            conn.close()
        if idx == 0:
            first_path = str(dest)

    if first_path:
        conn = get_connection()
        try:
            posts_queries.update_post(conn, post_id, image_path=first_path,
                                      image_alt=image_alt, now=_now())
        finally:
            conn.close()

    return {"post_id": post_id}


@posts_router.post("/{post_id}/publish")
async def publish_post(post_id: int, payload: dict):
    """Publish a composed post to the chosen platforms."""
    platforms = payload.get("platforms") or []
    account_ids = payload.get("account_ids") or {}
    if not platforms:
        raise HTTPException(400, "Pick at least one platform")
    # Live-publish safety guard — mirrors posting_api.post_story so a UI
    # regression can't fire a real, publicly-visible post without an explicit
    # acknowledgement. The story endpoints have carried this since they were
    # written; the artwork/posts/sync ones never did (4.0.11).
    if not payload.get("confirm_live"):
        raise HTTPException(
            400, "publish requires confirm_live=true (live-publish safety guard)")
    if payload.get("background"):
        from posting import activity      # spec 017 — see posting_api.post_story

        def _run(sites):
            return post_publisher.publish_post(post_id, sites, account_ids,
                                               persona_id=payload.get("persona_id"))
        jid = activity.launch("post", "Post", platforms, lambda: _run(platforms),
                              ref={"post": post_id}, retry=lambda site: _run([site]))
        return {"status": "started", "job_id": jid}
    try:
        results = await post_publisher.publish_post(
            post_id, platforms, account_ids, persona_id=payload.get("persona_id"))
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error("publish_post failed: %s", e, exc_info=True)
        raise HTTPException(500, str(e))
    successes = sum(1 for r in results if r.get("success"))
    return {"results": results, "successes": successes, "failures": len(results) - successes}


@posts_router.post("/{post_id}/schedule")
async def schedule_post(post_id: int, payload: dict):
    """Schedule a composed post to publish to the chosen platforms at a future time.

    Body: { "platforms": ["bsky","mast"], "account_ids": {"bsky": 3},
            "scheduled_at": "2026-07-25T20:00:00.000Z" }

    One `posting_queue` row per platform (`content_type='post'`, `story_name` =
    the post_id, a body snippet stashed as `title_override` so the Queue &
    Schedule page shows something readable). The posting-scheduler daemon fires
    each row when due via `post_publisher.publish_post`.
    """
    from posting.manager import get_platform_requires
    from database import posting_queries

    platforms = payload.get("platforms") or []
    account_ids = payload.get("account_ids") or {}
    scheduled_at = payload.get("scheduled_at")
    persona_id = payload.get("persona_id")
    if not platforms:
        raise HTTPException(400, "Pick at least one platform")
    if not scheduled_at:
        raise HTTPException(400, "scheduled_at is required")
    if persona_id is not None:
        # Persona-first schedules are checked now, not when the queue fires
        # (a refusal then would be a silent non-post). Nothing is queued if
        # any platform fails the check.
        from database import personas as personas_db
        conn = get_connection()
        try:
            errs = [personas_db.persona_account_error(conn, p, account_ids.get(p), persona_id)
                    for p in platforms]
        finally:
            conn.close()
        errs = [e for e in errs if e]
        if errs:
            raise HTTPException(400, "; ".join(errs))

    conn = get_connection()
    try:
        post = posts_queries.get_post(conn, post_id)
    finally:
        conn.close()
    if not post:
        raise HTTPException(404, "Post not found")

    scheduled_str = _to_utc_sql(scheduled_at)
    snippet = " ".join((post.get("body") or "").split())[:60] or f"Post #{post_id}"

    queue_ids = []
    conn = get_connection()
    try:
        for platform in platforms:
            qid = posting_queries.add_to_queue(
                conn, str(post_id), 0, platform, action="post",
                account_id=account_ids.get(platform),
                content_type="post",
                scheduled_at=scheduled_str,
                title_override=snippet,
                requires=get_platform_requires(platform),
                persona_id=persona_id,
            )
            queue_ids.append(qid)
    finally:
        conn.close()

    logger.info("Scheduled post #%d to %s at %s (queue %s)",
                post_id, ",".join(platforms), scheduled_str, queue_ids)
    return {"ok": True, "queue_ids": queue_ids, "scheduled_at": scheduled_str}


@posts_router.delete("/{post_id}")
def delete_post(post_id: int):
    conn = get_connection()
    try:
        post = posts_queries.get_post(conn, post_id)
        if not post:
            raise HTTPException(404, "Post not found")
        posts_queries.delete_post(conn, post_id)
    finally:
        conn.close()
    # Best-effort media cleanup — every attached image, de-duplicated (the
    # legacy image_path mirrors media[0]).
    paths = {m.get("path") for m in (post.get("media") or []) if m.get("path")}
    if post.get("image_path"):
        paths.add(post["image_path"])
    for img in paths:
        try:
            p = Path(img).resolve()
            if _media_dir().resolve() in p.parents and p.is_file():
                p.unlink()
        except OSError:
            pass
    return {"status": "deleted"}


def _cleanup(post_id: int) -> None:
    """Delete a just-created post row after an image-validation failure."""
    conn = get_connection()
    try:
        posts_queries.delete_post(conn, post_id)
    finally:
        conn.close()
