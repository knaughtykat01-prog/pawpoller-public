"""Podcast feeds — the authenticated management API and the public feed surface.

MEDIAPLATS §4 (4.21.1). Two routers:

``podcast_router`` (``/api/podcasts/*``, behind login): feeds (create / edit / delete, feed
art), episodes (publish a piece / edit / unpublish), the feeds a piece is in, the
directories' last fetches.

``feed_router`` (``/feed/*``, **auth-exempt**, script-free CSP with ``media-src 'self'`` —
see dashboard.py): the RSS document, feed and episode art, each episode's audio streamed
with Range from the artwork archive, and a small HTML page per episode. The public paths
carry a feed slug and an episode GUID; nothing else in the archive is reachable through them.

Creating a feed also creates the ``pod`` **account** that holds it (label = the feed's
title, handle = the slug, credential ``pod_feed_slug``) — that account is how the publish
pickers offer the feed as a target, one row per feed like Telegram's one row per channel.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from xml.sax.saxutils import escape

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import config
from database import accounts as adb
from database import podcasts as pdb
from database.db import get_connection
from posting import podcast_feed, poster_fit
from posting.platforms.podcast import public_base

logger = logging.getLogger(__name__)

podcast_router = APIRouter(prefix="/api/podcasts")
feed_router = APIRouter()

FEED_ART_SIDE = 1400          # Apple: 1400–3000 px square
EPISODE_ART_SIDE = 1400


def podcasts_dir() -> Path:
    d = config.DATA_DIR / "podcasts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_dir() -> Path:
    d = podcasts_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── pieces ───────────────────────────────────────────────────────────────────

def _piece(name: str) -> dict | None:
    """What the feed needs to know about an audio piece, or None when it is not one."""
    from posting import artwork_reader as ar
    from posting import media_kinds
    try:
        art = ar.load_artwork(name)
    except Exception:
        return None
    path = art.image_path
    if not path or not os.path.isfile(path) or media_kinds.kind_of(path) != "audio":
        return None
    media = art.media or {}
    return {"file": path, "bytes": os.path.getsize(path), "duration_s": media.get("duration_s"),
            "poster": art.thumbnail_path if art.thumbnail_path and os.path.isfile(art.thumbnail_path) else None,
            "has_poster": bool(art.thumbnail_path and os.path.isfile(art.thumbnail_path)),
            "title": art.title}


def _pieces_for(episodes: list[dict]) -> dict[str, dict]:
    out = {}
    for e in episodes:
        if e["artwork_name"] not in out:
            p = _piece(e["artwork_name"])
            if p:
                out[e["artwork_name"]] = p
    return out


# ── fetch log (which directory read the feed, and when) ──────────────────────

def _fetch_log_path() -> Path:
    return podcasts_dir() / "fetches.json"


def _read_fetch_log() -> dict:
    try:
        return json.loads(_fetch_log_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def record_fetch(slug: str, user_agent: str) -> None:
    name = podcast_feed.crawler_name(user_agent)
    if not name:
        return
    try:
        log = _read_fetch_log()
        entry = log.setdefault(slug, {}).setdefault(name, {"count": 0, "last_at": ""})
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp = _fetch_log_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(log, indent=1), encoding="utf-8")
        os.replace(tmp, _fetch_log_path())
    except Exception as e:                       # never let bookkeeping break a fetch
        logger.debug("podcast fetch log: %s", e)


# ── the management API ───────────────────────────────────────────────────────

def _public(feed: dict, base: str) -> dict:
    d = dict(feed)
    d["feed_url"] = podcast_feed.feed_url(base, feed["slug"]) if base else ""
    d["art_url"] = podcast_feed.feed_art_url(base, feed["slug"]) if base and (feed.get("artwork_name") or feed.get("artwork_file")) else ""
    d["art_from_episode"] = False
    if base and not d["art_url"]:
        conn = get_connection()
        try:
            eps = pdb.list_episodes(conn, feed["feed_id"])
        finally:
            conn.close()
        if podcast_feed.newest_episode_with_poster(eps, _pieces_for(eps)):
            d["art_url"] = podcast_feed.feed_art_url(base, feed["slug"])
            d["art_from_episode"] = True
    d.pop("owner_email", None)                   # vaulted-in-spirit: never echoed to the page
    d["has_owner_email"] = bool(feed.get("owner_email"))
    return d


@podcast_router.get("")
def list_feeds():
    base = public_base()
    conn = get_connection()
    try:
        feeds = pdb.list_feeds(conn)
        out = []
        for f in feeds:
            d = _public(f, base)
            d["episode_count"] = len(pdb.list_episodes(conn, f["feed_id"]))
            out.append(d)
    finally:
        conn.close()
    return {"feeds": out, "public_base": base, "fetches": _read_fetch_log(),
            "categories": list(podcast_feed.CATEGORIES),
            "directories": [
                {"name": "Pocket Casts", "url": "https://pocketcasts.com/submit/", "note": "no review — the quickest proof"},
                {"name": "Apple Podcasts Connect", "url": "https://podcastsconnect.apple.com/", "note": "needs an Apple ID; reviewed"},
                {"name": "Spotify for Creators", "url": "https://creators.spotify.com/", "note": "add an existing RSS feed"},
                {"name": "Amazon Music for Podcasters", "url": "https://podcasters.amazon.com/", "note": "add an existing RSS feed"},
            ]}


class FeedIn(BaseModel):
    title: str
    slug: str | None = None
    description: str = ""
    author: str = ""
    owner_email: str = ""
    language: str = "en"
    category: str = "Arts"
    explicit: bool = False
    link: str = ""
    artwork_name: str | None = None


@podcast_router.post("")
def create_feed(body: FeedIn):
    slug = (body.slug or "").strip().lower() or pdb.slugify(body.title)
    if not pdb.valid_slug(slug):
        raise HTTPException(400, "A feed address is lowercase letters, digits and hyphens (up to 64)")
    if body.category and body.category not in podcast_feed.CATEGORIES:
        raise HTTPException(400, "Pick one of Apple's podcast categories")
    conn = get_connection()
    try:
        if pdb.get_feed_by_slug(conn, slug):
            raise HTTPException(409, f"A feed at '{slug}' already exists")
        try:
            feed_id = pdb.create_feed(conn, slug=slug, title=body.title, description=body.description,
                                      author=body.author, owner_email=body.owner_email, language=body.language,
                                      category=body.category or "Arts", explicit=body.explicit, link=body.link,
                                      artwork_name=body.artwork_name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        # The account that holds this feed — one per feed, like a Telegram channel.
        adb.ensure_accounts_table(conn)
        acct_id = adb.create_account(conn, "pod", body.title.strip(), handle=slug, enabled=True,
                                     is_default=adb.get_default_account_id(conn, "pod") is None)
        acct = adb.get_account(conn, acct_id)
        is_default = bool(acct["is_default"]) if acct else False
    finally:
        conn.close()
    config.save_settings({config.account_setting_key(acct_id, "pod_feed_slug", is_default): slug})
    return {"status": "ok", "feed_id": feed_id, "slug": slug, "account_id": acct_id}


@podcast_router.get("/{feed_id}")
def get_feed(feed_id: int):
    base = public_base()
    conn = get_connection()
    try:
        feed = pdb.get_feed(conn, feed_id)
        if not feed:
            raise HTTPException(404, "No such feed")
        eps = pdb.list_episodes(conn, feed_id)
    finally:
        conn.close()
    pieces = _pieces_for(eps)
    for e in eps:
        p = pieces.get(e["artwork_name"])
        e["piece_ok"] = bool(p)
        e["duration_s"] = p.get("duration_s") if p else None
        e["page_url"] = podcast_feed.episode_page_url(base, feed["slug"], e["guid"]) if base else ""
    return {"feed": _public(feed, base), "episodes": eps, "fetches": _read_fetch_log().get(feed["slug"], {})}


class FeedPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    author: str | None = None
    owner_email: str | None = None
    language: str | None = None
    category: str | None = None
    explicit: bool | None = None
    link: str | None = None
    artwork_name: str | None = None


@podcast_router.patch("/{feed_id}")
def patch_feed(feed_id: int, body: FeedPatch):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "category" in fields and fields["category"] not in podcast_feed.CATEGORIES:
        raise HTTPException(400, "Pick one of Apple's podcast categories")
    conn = get_connection()
    try:
        if not pdb.get_feed(conn, feed_id):
            raise HTTPException(404, "No such feed")
        pdb.update_feed(conn, feed_id, **fields)
    finally:
        conn.close()
    _invalidate_art(feed_id=feed_id)
    return {"status": "ok"}


@podcast_router.delete("/{feed_id}")
def delete_feed(feed_id: int):
    """Removes the feed, its episode rows and its account. The pieces are untouched."""
    conn = get_connection()
    try:
        feed = pdb.get_feed(conn, feed_id)
        if not feed:
            raise HTTPException(404, "No such feed")
        pdb.delete_feed(conn, feed_id)
        stale_keys = []
        for a in adb.list_accounts(conn, "pod"):
            if a.get("handle") == feed["slug"]:
                stale_keys.append(config.account_setting_key(a["account_id"], "pod_feed_slug", bool(a["is_default"])))
                adb.delete_account(conn, a["account_id"])
    finally:
        conn.close()
    # The slug setting goes with the account. Left behind, the bare key would make the
    # boot-time seeder recreate a "Podcast feed (default)" account pointing at a feed
    # that no longer exists (seen on a scratch instance before this line existed).
    if stale_keys:
        config.delete_settings_keys(stale_keys)
    _invalidate_art(feed_id=feed_id)
    return {"status": "ok"}


@podcast_router.post("/{feed_id}/artwork")
async def upload_feed_art(feed_id: int, file: UploadFile = File(...)):
    """A designed cover for the feed (square, ≥ 1400 px after fitting)."""
    from posting import media_kinds
    if media_kinds.kind_of(file.filename or "") != "image":
        raise HTTPException(415, "Feed art is a PNG or JPEG")
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "Feed art over 20 MB")
    conn = get_connection()
    try:
        if not pdb.get_feed(conn, feed_id):
            raise HTTPException(404, "No such feed")
        ext = ".png" if (file.filename or "").lower().endswith(".png") else ".jpg"
        name = f"feed_{feed_id}{ext}"
        (podcasts_dir() / name).write_bytes(data)
        pdb.update_feed(conn, feed_id, artwork_file=name)
    finally:
        conn.close()
    _invalidate_art(feed_id=feed_id)
    return {"status": "ok", "artwork_file": name}


class EpisodeIn(BaseModel):
    artwork_name: str
    title: str | None = None
    notes: str | None = None
    explicit: bool | None = None
    season: int | None = None
    episode_number: int | None = None
    published_at: str | None = None


@podcast_router.post("/{feed_id}/episodes")
def add_episode(feed_id: int, body: EpisodeIn):
    piece = _piece(body.artwork_name)
    if not piece:
        raise HTTPException(400, "Only an audio piece from the Library can be an episode")
    conn = get_connection()
    try:
        if not pdb.get_feed(conn, feed_id):
            raise HTTPException(404, "No such feed")
        ep = pdb.add_episode(conn, feed_id=feed_id, artwork_name=body.artwork_name,
                             title=body.title or piece["title"] or body.artwork_name, notes=body.notes or "",
                             explicit=bool(body.explicit), season=body.season, episode_number=body.episode_number,
                             published_at=body.published_at)
    finally:
        conn.close()
    _invalidate_art(feed_id=feed_id)          # the newest episode's poster may now stand in for the feed art
    return {"status": "ok", "episode": ep}


class EpisodePatch(BaseModel):
    title: str | None = None
    notes: str | None = None
    explicit: bool | None = None
    season: int | None = None
    episode_number: int | None = None
    published_at: str | None = None


@podcast_router.patch("/episodes/{episode_id}")
def patch_episode(episode_id: int, body: EpisodePatch):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    conn = get_connection()
    try:
        if not pdb.get_episode(conn, episode_id):
            raise HTTPException(404, "No such episode")
        pdb.update_episode(conn, episode_id, **fields)
    finally:
        conn.close()
    return {"status": "ok"}


@podcast_router.delete("/episodes/{episode_id}")
def remove_episode(episode_id: int):
    conn = get_connection()
    try:
        ep = pdb.get_episode(conn, episode_id)
        if not ep:
            raise HTTPException(404, "No such episode")
        pdb.remove_episode(conn, episode_id)
    finally:
        conn.close()
    _invalidate_art(feed_id=ep["feed_id"], guid=ep["guid"])
    return {"status": "ok"}


@podcast_router.get("/piece/{name:path}")
def piece_episodes(name: str):
    conn = get_connection()
    try:
        return {"episodes": pdb.episodes_for_piece(conn, name)}
    finally:
        conn.close()


# ── the public surface ───────────────────────────────────────────────────────

_404 = Response(content="Not found", media_type="text/plain", status_code=404)


def _feed_and_base(slug: str):
    base = public_base()
    conn = get_connection()
    try:
        feed = pdb.get_feed_by_slug(conn, slug)
        eps = pdb.list_episodes(conn, feed["feed_id"]) if feed else []
    finally:
        conn.close()
    return feed, eps, base


@feed_router.get("/feed/{name}")
def public_feed(name: str, request: Request):
    """``/feed/{slug}.xml`` — the RSS document, with an ETag so a directory's conditional
    fetch answers 304 when nothing changed."""
    if not name.endswith(".xml"):
        return _404
    slug = name[:-4]
    if not pdb.valid_slug(slug):
        return _404
    feed, eps, base = _feed_and_base(slug)
    if not feed:
        return _404
    body = podcast_feed.render_feed(feed, eps, _pieces_for(eps), base or str(request.base_url).rstrip("/"))
    import hashlib
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    record_fetch(slug, request.headers.get("user-agent", ""))
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(content=body, media_type="application/rss+xml; charset=utf-8",
                    headers={"ETag": etag, "Cache-Control": "public, max-age=300"})


def _fitted(source: str | None, cache_name: str, side: int) -> Path | None:
    """A square JPEG of `source`, cached under the podcasts cache until the source changes."""
    if not source or not os.path.isfile(source):
        return None
    out = _cache_dir() / cache_name
    try:
        if out.exists() and out.stat().st_mtime >= os.path.getmtime(source):
            return out
    except OSError:
        pass
    tmp = poster_fit.square(source, side)
    if not tmp:
        return None
    os.replace(tmp, out)
    return out


def _invalidate_art(feed_id: int | None = None, guid: str | None = None) -> None:
    try:
        if feed_id is not None:
            (_cache_dir() / f"feed_{feed_id}.jpg").unlink(missing_ok=True)
        if guid:
            (_cache_dir() / f"ep_{guid}.jpg").unlink(missing_ok=True)
    except OSError:
        pass


@feed_router.get("/feed/{slug}/{name}")
def public_feed_item(slug: str, name: str):
    """``art.jpg`` → the feed's art; ``{guid}.{ext}`` → the episode's audio (Range);
    ``{guid}`` → the episode's page."""
    if not pdb.valid_slug(slug):
        return _404
    feed, eps, base = _feed_and_base(slug)
    if not feed:
        return _404
    if name == "art.jpg":
        source = None
        if feed.get("artwork_file"):
            p = podcasts_dir() / feed["artwork_file"]
            source = str(p) if p.is_file() else None
        if not source and feed.get("artwork_name"):
            piece = _piece(feed["artwork_name"])
            source = (piece or {}).get("poster")
            if not source:
                try:
                    from posting import artwork_reader as ar
                    art = ar.load_artwork(feed["artwork_name"])
                    from posting import media_kinds
                    source = art.thumbnail_path or (art.image_path if media_kinds.kind_of(art.image_path or "") == "image" else None)
                except Exception:
                    source = None
        if not source:
            # No feed art of its own: the newest episode's poster (see podcast_feed.feed_has_art).
            ep = podcast_feed.newest_episode_with_poster(eps, _pieces_for(eps))
            source = (_piece(ep["artwork_name"]) or {}).get("poster") if ep else None
        fitted = _fitted(source, f"feed_{feed['feed_id']}.jpg", FEED_ART_SIDE)
        return FileResponse(str(fitted), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"}) if fitted else _404
    guid, ext = (name.rsplit(".", 1) + [""])[:2] if "." in name else (name, "")
    ep = next((e for e in eps if e["guid"] == guid), None)
    if not ep:
        return _404
    piece = _piece(ep["artwork_name"])
    if not piece:
        return _404
    from posting import media_kinds
    if ext:
        if ext.lower() != media_kinds.ext_of(piece["file"]).lstrip("."):
            return _404
        return FileResponse(piece["file"], media_type=media_kinds.mime_for(piece["file"]),
                            headers={"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=3600"})
    return Response(content=_episode_page(feed, ep, piece, base), media_type="text/html; charset=utf-8")


@feed_router.get("/feed/{slug}/{guid}/art.jpg")
def public_episode_art(slug: str, guid: str):
    if not pdb.valid_slug(slug):
        return _404
    feed, eps, _base = _feed_and_base(slug)
    ep = next((e for e in eps if e["guid"] == guid), None) if feed else None
    if not ep:
        return _404
    piece = _piece(ep["artwork_name"])
    fitted = _fitted((piece or {}).get("poster"), f"ep_{guid}.jpg", EPISODE_ART_SIDE)
    return FileResponse(str(fitted), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"}) if fitted else _404


def _episode_page(feed: dict, ep: dict, piece: dict, base: str) -> str:
    from posting import media_kinds
    ext = media_kinds.ext_of(piece["file"]).lstrip(".")
    audio = podcast_feed.enclosure_url(base, feed["slug"], ep["guid"], ext) if base else f"/feed/{feed['slug']}/{ep['guid']}.{ext}"
    art = podcast_feed.episode_art_url(base, feed["slug"], ep["guid"]) if (base and piece.get("has_poster")) else (f"/feed/{feed['slug']}/{ep['guid']}/art.jpg" if piece.get("has_poster") else "")
    notes = escape(ep.get("notes") or "").replace("\n", "<br>")
    return f"""<!doctype html>
<html lang="{escape(feed.get('language') or 'en')}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(ep.get('title') or '')} — {escape(feed.get('title') or '')}</title>
<style>
body{{margin:0;background:#faf7f2;color:#1b1a17;font:16px/1.5 Georgia,serif}}
main{{max-width:680px;margin:0 auto;padding:2rem 1.2rem}}
img{{max-width:100%;border-radius:12px;display:block;margin:0 auto 1.2rem}}
h1{{font-size:1.6rem;margin:.2rem 0}} .feed{{color:#6b665c;font-size:.9rem}}
audio{{width:100%;margin:1rem 0}} .notes{{white-space:normal}}
a{{color:#7a4a1e}}
</style></head><body><main>
{f'<img src="{escape(art)}" alt="">' if art else ''}
<div class="feed"><a href="{escape(podcast_feed.feed_url(base, feed['slug']) if base else '/feed/' + feed['slug'] + '.xml')}">{escape(feed.get('title') or feed['slug'])}</a></div>
<h1>{escape(ep.get('title') or '')}</h1>
<audio controls preload="metadata" src="{escape(audio)}"></audio>
<div class="notes">{notes}</div>
</main></body></html>"""
