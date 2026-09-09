"""Render a podcast feed — RSS 2.0 with the iTunes namespace — MEDIAPLATS §4 (4.21.1).

Pure: takes the feed row, its episode rows, a lookup for each episode's piece (file size,
duration, poster) and the public base URL; returns bytes. Deterministic for the same rows, so
the ETag the route derives from it is stable and a directory's conditional fetch answers 304.

What the directories require (Apple's feed spec is the strictest and the others accept it):
``<itunes:image>`` square art 1400–3000 px on the channel, ``<itunes:explicit>`` on the
channel and each item, ``<itunes:category>`` from Apple's list, ``<language>``, an
``<enclosure>`` per item with a byte length and MIME, a ``<guid isPermaLink="false">`` that
never changes, an RFC 2822 ``<pubDate>``, ``<itunes:duration>`` in seconds, and an
``<atom:link rel="self">`` naming the feed's own URL.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import quote
from xml.sax.saxutils import escape

from posting import media_kinds

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
ATOM_NS = "http://www.w3.org/2005/Atom"

# Apple's top-level categories (the second level is optional and omitted).
CATEGORIES = ("Arts", "Business", "Comedy", "Education", "Fiction", "Government", "History",
              "Health & Fitness", "Kids & Family", "Leisure", "Music", "News", "Religion & Spirituality",
              "Science", "Society & Culture", "Sports", "Technology", "True Crime", "TV & Film")


def feed_url(base_url: str, slug: str) -> str:
    return f"{base_url.rstrip('/')}/feed/{quote(slug)}.xml"


def episode_page_url(base_url: str, slug: str, guid: str) -> str:
    return f"{base_url.rstrip('/')}/feed/{quote(slug)}/{quote(guid)}"


def enclosure_url(base_url: str, slug: str, guid: str, ext: str) -> str:
    ext = (ext or "").lstrip(".").lower()
    return f"{base_url.rstrip('/')}/feed/{quote(slug)}/{quote(guid)}.{ext}"


def feed_art_url(base_url: str, slug: str) -> str:
    return f"{base_url.rstrip('/')}/feed/{quote(slug)}/art.jpg"


def episode_art_url(base_url: str, slug: str, guid: str) -> str:
    return f"{base_url.rstrip('/')}/feed/{quote(slug)}/{quote(guid)}/art.jpg"


def _rfc2822(iso: str | None) -> str:
    """A SQLite 'YYYY-MM-DD HH:MM:SS' (UTC) or ISO string → RFC 2822, UTC."""
    if not iso:
        dt = datetime.now(timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt.astimezone(timezone.utc))


def _yes(flag) -> str:
    return "true" if flag else "false"


def newest_episode_with_poster(episodes: list[dict], pieces: dict[str, dict]) -> dict | None:
    """The episode whose poster stands in for the feed's art when the feed has none —
    Apple and Spotify refuse a channel without ``<itunes:image>``, so a feed made in a
    hurry still validates the moment its first episode has a poster."""
    for e in sorted(episodes, key=lambda e: e.get("published_at") or "", reverse=True):
        if (pieces.get(e["artwork_name"]) or {}).get("has_poster"):
            return e
    return None


def feed_has_art(feed: dict, episodes: list[dict], pieces: dict[str, dict]) -> bool:
    return bool(feed.get("artwork_name") or feed.get("artwork_file")
                or newest_episode_with_poster(episodes, pieces))


def render_feed(feed: dict, episodes: list[dict], pieces: dict[str, dict], base_url: str) -> bytes:
    """``pieces`` maps an episode's ``artwork_name`` to ``{"file": str, "bytes": int,
    "duration_s": float|None, "has_poster": bool}``; an episode whose piece is missing is
    left out (a directory must never see a dead enclosure)."""
    slug = feed["slug"]
    self_url = feed_url(base_url, slug)
    site = feed.get("link") or base_url.rstrip("/")
    out = []
    w = out.append
    w('<?xml version="1.0" encoding="UTF-8"?>')
    w(f'<rss version="2.0" xmlns:itunes="{ITUNES_NS}" xmlns:content="{CONTENT_NS}" xmlns:atom="{ATOM_NS}">')
    w("<channel>")
    w(f"<title>{escape(feed.get('title') or slug)}</title>")
    w(f"<link>{escape(site)}</link>")
    w(f"<language>{escape(feed.get('language') or 'en')}</language>")
    w(f"<description>{escape(feed.get('description') or '')}</description>")
    w(f'<atom:link href="{escape(self_url)}" rel="self" type="application/rss+xml"/>')
    w(f"<itunes:author>{escape(feed.get('author') or '')}</itunes:author>")
    w(f"<itunes:summary>{escape(feed.get('description') or '')}</itunes:summary>")
    w(f"<itunes:explicit>{_yes(feed.get('explicit'))}</itunes:explicit>")
    w(f'<itunes:category text="{escape(feed.get("category") or "Arts")}"/>')
    w("<itunes:type>episodic</itunes:type>")
    if feed.get("owner_email"):
        w("<itunes:owner>")
        w(f"<itunes:name>{escape(feed.get('author') or feed.get('title') or slug)}</itunes:name>")
        w(f"<itunes:email>{escape(feed['owner_email'])}</itunes:email>")
        w("</itunes:owner>")
    if feed_has_art(feed, episodes, pieces):
        # The URL is the same either way; the route resolves the fallback (newest episode's poster).
        w(f'<itunes:image href="{escape(feed_art_url(base_url, slug))}"/>')
    newest = max((e.get("published_at") or "" for e in episodes), default="")
    w(f"<lastBuildDate>{_rfc2822(newest or None)}</lastBuildDate>")
    for e in episodes:
        piece = pieces.get(e["artwork_name"])
        if not piece or not piece.get("file"):
            continue
        ext = media_kinds.ext_of(piece["file"])
        mime = media_kinds.mime_for(piece["file"])
        w("<item>")
        w(f"<title>{escape(e.get('title') or e['artwork_name'])}</title>")
        w(f'<guid isPermaLink="false">{escape(e["guid"])}</guid>')
        w(f"<link>{escape(episode_page_url(base_url, slug, e['guid']))}</link>")
        w(f"<pubDate>{_rfc2822(e.get('published_at'))}</pubDate>")
        w(f"<description>{escape(e.get('notes') or '')}</description>")
        w(f'<enclosure url="{escape(enclosure_url(base_url, slug, e["guid"], ext))}" length="{int(piece.get("bytes") or 0)}" type="{escape(mime)}"/>')
        dur = piece.get("duration_s")
        if isinstance(dur, (int, float)) and dur > 0:
            w(f"<itunes:duration>{int(round(dur))}</itunes:duration>")
        w(f"<itunes:explicit>{_yes(e.get('explicit'))}</itunes:explicit>")
        if e.get("season"):
            w(f"<itunes:season>{int(e['season'])}</itunes:season>")
        if e.get("episode_number"):
            w(f"<itunes:episode>{int(e['episode_number'])}</itunes:episode>")
        if piece.get("has_poster"):
            w(f'<itunes:image href="{escape(episode_art_url(base_url, slug, e["guid"]))}"/>')
        w("</item>")
    w("</channel>")
    w("</rss>")
    return "\n".join(out).encode("utf-8")


def crawler_name(user_agent: str) -> str | None:
    """Which directory fetched the feed, from its user agent — the only listener truth RSS has."""
    ua = (user_agent or "").lower()
    for needle, name in (("spotify", "Spotify"), ("itms", "Apple Podcasts"), ("applecoremedia", "Apple Podcasts"),
                         ("amazon", "Amazon Music"), ("pocketcasts", "Pocket Casts"), ("pocket casts", "Pocket Casts"),
                         ("overcast", "Overcast"), ("podcastindex", "Podcast Index"), ("castro", "Castro"),
                         ("antennapod", "AntennaPod"), ("youtube", "YouTube Music")):
        if needle in ua:
            return name
    return None
