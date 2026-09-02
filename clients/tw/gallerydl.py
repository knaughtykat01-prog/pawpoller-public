"""gallery-dl subprocess backend for the X/Twitter (TW) poll path.

Why this exists
---------------
X/Twitter has no usable public API, so PawPoller's TWClient scrapes the internal
GraphQL timeline with hardcoded query IDs (clients/tw/client.py). Those IDs
rotate whenever X ships a web-client bundle, and keeping up with them is a
recurring maintenance tax. gallery-dl is a widely-maintained downloader that
tracks X's changes for us, so we offload the READ (poll) path to it.

Licence isolation (important)
-----------------------------
gallery-dl is **GPL-2.0**; PawPoller is MIT. We therefore invoke gallery-dl
ONLY as a separate operating-system process (``subprocess`` / ``asyncio`` exec)
and NEVER ``import`` it. Shelling out to a GPL program from a non-GPL program is
mere aggregation, not a derivative work (see the GPL FAQ on exec/pipe), so our
MIT licence is unaffected. Do not add ``import gallery_dl`` anywhere.

Scope
-----
READ ONLY. gallery-dl cannot post, so tweet posting stays entirely on the
GraphQL client. This module only reproduces the timeline scrape, and only when
gallery-dl is actually available on the machine. TWClient calls in here first
and falls back to GraphQL when we return ``None`` — so X polling can never
regress below today's behaviour, it can only get more robust.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Any

import config

logger = logging.getLogger(__name__)

# Only photo attachments feed the artwork importer; videos/GIFs give a still
# preview, not an importable image, so we key off the file extension.
_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp"}

# Snowflake epoch (2010-11-04T01:42:54.657Z) — X tweet ids encode creation time
# in their high bits; used as a fallback when gallery-dl's `date` is missing.
_TWITTER_EPOCH_MS = 1288834974657

# Follower count captured during a fetch, keyed by lowercased handle. gallery-dl
# attaches the author's user object (incl. followers_count) to every tweet, so a
# poll that already scraped the timeline exposes the follower total for FREE —
# get_follower_count() reads this instead of spending a billed official-API
# user-lookup. Warmed (and invalidated) by fetch_tweets().
_LAST_FOLLOWERS: dict[str, int] = {}


# -- Discovery ---------------------------------------------------------------

def find_gallerydl(settings: dict | None = None) -> str | None:
    """Absolute path to the gallery-dl executable, or ``None`` if unavailable.

    Resolution order: an explicit ``tw_gallerydl_path`` setting, then a PATH
    lookup for the console script (``gallery-dl`` / ``gallery-dl.exe``). We
    deliberately do NOT fall back to ``python -m gallery_dl`` — resolving the
    console script keeps frozen desktop builds from importing anything and keeps
    the GPL boundary a clean process boundary.
    """
    settings = settings if settings is not None else config.get_settings()
    explicit = (settings.get("tw_gallerydl_path") or "").strip()
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        logger.warning("TW: tw_gallerydl_path is set but not an executable file: %s", explicit)
        # fall through to a PATH lookup rather than failing outright
    return shutil.which("gallery-dl") or shutil.which("gallery-dl.exe")


def is_enabled(settings: dict | None = None) -> bool:
    """Whether the gallery-dl poll backend should be attempted.

    ``tw_polling_backend`` (a plain setting, not a secret):
      * ``"auto"`` (default) — gallery-dl is the PRIMARY poll path when present;
                               the official API becomes the paid fallback.
      * ``"gallerydl"``      — gallery-dl only (drops the paid fallback).
      * ``"official"``       — force the paid official API first; gallery-dl is
                               disabled so it can't preempt the chosen backend.
      * ``"graphql"``        — force the legacy GraphQL scrape (skip gallery-dl).
    """
    settings = settings if settings is not None else config.get_settings()
    backend = (settings.get("tw_polling_backend") or "auto").strip().lower()
    if backend in ("graphql", "official"):
        return False
    return find_gallerydl(settings) is not None


# -- Small helpers (kept local to avoid a client.py <-> gallerydl.py cycle) ---

def _safe_int(val: Any) -> int:
    if val is None:
        return 0
    try:
        if isinstance(val, str):
            val = val.replace(",", "").strip()
        return int(val)
    except (ValueError, TypeError):
        return 0


def _snowflake_to_utc(tweet_id: Any) -> str:
    """'YYYY-MM-DD HH:MM:SS' (UTC) from a Snowflake tweet id, or '' if unusable."""
    try:
        ms = (int(tweet_id) >> 22) + _TWITTER_EPOCH_MS
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return ""
    if dt.year < 2006:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _normalize_date(val: Any) -> str:
    """gallery-dl serialises its `date` datetime to a string in `-j` output.
    Normalise both 'YYYY-MM-DD HH:MM:SS' and ISO 'YYYY-MM-DDTHH:MM:SS[.f][+tz]'
    down to 'YYYY-MM-DD HH:MM:SS'. Returns '' for anything unparseable.
    """
    if not val or not isinstance(val, str):
        return ""
    s = val.strip().replace("T", " ")
    s = s.split(".")[0].split("+")[0].strip()
    return s[:19]


def _write_cookies(auth_token: str, ct0: str, path: str) -> None:
    """Write a Netscape cookie jar for .x.com that gallery-dl can read via
    ``--cookies``. The leading comment line is required by MozillaCookieJar."""
    expiry = 2000000000  # ~2033; X cookies outlive any single poll cycle
    lines = ["# Netscape HTTP Cookie File\n"]
    for name, value in (("auth_token", auth_token), ("ct0", ct0)):
        # domain  include_subdomains  path  secure  expiry  name  value
        lines.append(f".x.com\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


# -- Parsing -----------------------------------------------------------------

def _build_detail(kw: dict, target_user: str) -> dict:
    """Turn one gallery-dl tweet kwdict into TWClient's detail-dict shape
    (identical keys to clients.tw.client.TWClient._extract_tweet_stats)."""
    tid = str(kw.get("tweet_id"))
    text = kw.get("content") or ""
    author = kw.get("author") or kw.get("user") or {}
    username = ""
    if isinstance(author, dict):
        username = author.get("name") or ""
    username = username or (target_user or "").lstrip("@")

    # Content-type detection mirrors the GraphQL path's ordering.
    if kw.get("reply_id"):
        content_type = "reply"
    elif kw.get("retweet_id"):
        content_type = "retweet"
    elif kw.get("quote_id"):
        content_type = "quote"
    else:
        content_type = "tweet"

    posted_at = _normalize_date(kw.get("date")) or _snowflake_to_utc(tid)

    hashtags = kw.get("hashtags") or []
    keywords = [h for h in hashtags if isinstance(h, str) and h]

    return {
        "tweet_id": tid,
        "title": (text[:80] + "...") if len(text) > 80 else text,
        "username": username,
        "posted_at": posted_at,
        "content_type": content_type,
        "rating": "General",
        "description": text,
        "keywords": keywords,
        "link": f"https://x.com/{username}/status/{tid}",
        "thumbnail_url": "",
        "media_urls": [],
        "views": _safe_int(kw.get("view_count")),
        "likes": _safe_int(kw.get("favorite_count")),
        "retweets": _safe_int(kw.get("retweet_count")),
        "replies": _safe_int(kw.get("reply_count")),
        "quotes": _safe_int(kw.get("quote_count")),
        "bookmarks": _safe_int(kw.get("bookmark_count")),
    }


def _parse_dump_json(raw: str, target_user: str) -> list[dict]:
    """Parse ``gallery-dl -j`` stdout into a list of detail dicts.

    The dump is a JSON array whose elements are message tuples. For a media file
    the element is ``[type, url, kwdict]``; for a metadata-only entry (e.g. a
    text tweet) it is ``[type, kwdict]``. In every case the tweet metadata is the
    LAST element when it's a dict, so we key off that and stay robust to the
    message-type integers (which have shifted across gallery-dl versions).

    Multiple media on one tweet yield multiple elements sharing a ``tweet_id`` —
    we collapse them into one detail dict and accumulate the photo URLs. Order of
    first appearance is preserved.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []

    tweets: dict[str, dict] = {}
    order: list[str] = []

    for el in data:
        if not (isinstance(el, list) and el and isinstance(el[-1], dict)):
            continue
        kw = el[-1]
        tid = kw.get("tweet_id")
        if tid is None:
            continue
        tid = str(tid)

        url = el[1] if len(el) >= 3 and isinstance(el[1], str) else ""

        detail = tweets.get(tid)
        if detail is None:
            detail = _build_detail(kw, target_user)
            tweets[tid] = detail
            order.append(tid)

        if url:
            ext = str(kw.get("extension") or url.rsplit(".", 1)[-1].split("?")[0]).lower()
            if ext in _IMAGE_EXTS and url not in detail["media_urls"]:
                detail["media_urls"].append(url)

    result = [tweets[t] for t in order]
    for detail in result:
        if detail["media_urls"] and not detail["thumbnail_url"]:
            detail["thumbnail_url"] = detail["media_urls"][0]
    return result


def _extract_follower_count(raw: str, handle: str) -> int | None:
    """Pull the tracked account's follower count out of a ``gallery-dl -j`` dump.

    gallery-dl attaches the tweet author's user object (including
    ``followers_count``) to every tweet kwdict, so the timeline we already
    fetched carries the follower total at no extra cost. Prefer the author whose
    handle matches the tracked account; fall back to the first author that
    reports a count. Returns ``None`` when the dump has no usable count (e.g. an
    account with no tweets, or an older gallery-dl that omits the field).
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    want = (handle or "").lstrip("@").lower()
    fallback: int | None = None
    for el in data:
        if not (isinstance(el, list) and el and isinstance(el[-1], dict)):
            continue
        author = el[-1].get("author") or el[-1].get("user") or {}
        if not isinstance(author, dict) or "followers_count" not in author:
            continue
        count = _safe_int(author.get("followers_count"))
        name = str(author.get("name") or "").lstrip("@").lower()
        if want and name == want:
            return count
        if fallback is None:
            fallback = count
    return fallback


# -- Subprocess --------------------------------------------------------------

async def _run(exe: str, url: str, cookie_path: str, settings: dict,
               range_spec: str | None = None) -> tuple[int, str, str]:
    """Run gallery-dl in metadata-dump mode and return (returncode, stdout, stderr).

    ``-j`` dumps metadata without downloading media bytes; ``-q`` keeps stdout to
    pure JSON; ``--sleep-request`` throttles politely; ``text-tweets`` includes
    text-only posts; ``retweets=false`` keeps the account's own posts (a pure
    retweet's engagement belongs to the original author). ``videos=false`` avoids
    resolving video variants we'd discard anyway.
    """
    delay = getattr(config, "TW_REQUEST_DELAY_SECONDS", 2.0)
    args = [
        exe, "-j", "-q",
        "--cookies", cookie_path,
        "--sleep-request", str(delay),
        "-o", "extractor.twitter.text-tweets=true",
        "-o", "extractor.twitter.retweets=false",
        "-o", "extractor.twitter.videos=false",
    ]
    if range_spec:
        args += ["--range", range_spec]
    args.append(url)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as e:
        logger.warning("TW: could not launch gallery-dl (%s): %s", exe, e)
        return 1, "", str(e)

    timeout = getattr(config, "TW_GALLERYDL_TIMEOUT_SECONDS", 480)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        logger.warning("TW: gallery-dl timed out after %ss", timeout)
        return 1, "", "timeout"

    return (
        proc.returncode if proc.returncode is not None else 1,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


_AUTH_ERROR_MARKERS = (
    "authrequired", "401", "403", "unauthorized",
    "not authorized", "login", "could not authenticate",
)


async def fetch_tweets(auth_token: str, ct0: str, target_user: str,
                       settings: dict | None = None) -> list[dict] | None:
    """Fetch the target user's tweets as detail dicts via gallery-dl.

    Returns:
      * ``list[dict]`` — the tweets (possibly empty for an account with none).
        An empty list is authoritative; the caller should NOT fall back.
      * ``None`` — gallery-dl is unavailable/disabled, or the run failed. The
        caller should fall back to the GraphQL scrape.
    """
    settings = settings if settings is not None else config.get_settings()
    if not is_enabled(settings):
        return None
    exe = find_gallerydl(settings)
    handle = (target_user or "").lstrip("@")
    # A fresh attempt invalidates any prior cycle's cached follower count, so a
    # failure this cycle can't hand get_follower_count() a stale number — it will
    # fall through to the paid/scrape path instead.
    _LAST_FOLLOWERS.pop(handle.lower(), None)
    if not exe or not (auth_token and ct0 and handle):
        return None

    url = f"https://x.com/{handle}/tweets"
    tmpdir = tempfile.mkdtemp(prefix="pp_tw_gdl_")
    cookie_path = os.path.join(tmpdir, "cookies.txt")
    try:
        _write_cookies(auth_token, ct0, cookie_path)
        rc, out, err = await _run(exe, url, cookie_path, settings)
        if rc != 0:
            logger.warning("TW: gallery-dl exited %s — falling back to GraphQL: %s",
                           rc, (err or "").strip()[:300])
            return None
        tweets = _parse_dump_json(out, handle)
        # Ride the timeline scrape for the follower count too (free), so the
        # poll's capture_followers step needn't spend a billed API call.
        fc = _extract_follower_count(out, handle)
        if fc is not None:
            _LAST_FOLLOWERS[handle.lower()] = fc
        logger.info("TW: gallery-dl returned %d tweets for %s", len(tweets), handle)
        return tweets
    except Exception as e:
        logger.warning("TW: gallery-dl fetch failed — falling back to GraphQL: %s", e)
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def get_follower_count(target_user: str, settings: dict | None = None) -> int | None:
    """Follower count captured during this cycle's :func:`fetch_tweets`, or ``None``.

    A pure cache read — no network, no subprocess. Because gallery-dl's timeline
    metadata already carries ``author.followers_count``, a poll that fetched
    tweets via gallery-dl exposes the follower total for free; TWClient prefers
    this over the billed official-API user-lookup. Returns ``None`` when
    gallery-dl isn't the active backend or nothing was captured for this handle
    this cycle (fetch failed, or the account had no tweets), so the caller falls
    through to the paid API / GraphQL scrape.
    """
    settings = settings if settings is not None else config.get_settings()
    if not is_enabled(settings):
        return None
    h = (target_user or "").lstrip("@").lower()
    if not h:
        return None
    return _LAST_FOLLOWERS.get(h)


async def validate(auth_token: str, ct0: str, target_user: str,
                   settings: dict | None = None) -> bool | None:
    """Lightweight cookie validation via a single-item gallery-dl fetch.

    Returns ``True``/``False`` when gallery-dl can give a definitive answer, or
    ``None`` when it's unavailable or the failure was ambiguous (network etc.) —
    in which case the caller should fall back to GraphQL validation.
    """
    settings = settings if settings is not None else config.get_settings()
    if not is_enabled(settings):
        return None
    exe = find_gallerydl(settings)
    if not exe or not (auth_token and ct0 and target_user):
        return None

    handle = target_user.lstrip("@")
    url = f"https://x.com/{handle}/tweets"
    tmpdir = tempfile.mkdtemp(prefix="pp_tw_gdlv_")
    cookie_path = os.path.join(tmpdir, "cookies.txt")
    try:
        _write_cookies(auth_token, ct0, cookie_path)
        rc, out, err = await _run(exe, url, cookie_path, settings, range_spec="1-1")
        if rc == 0:
            return True
        low = (err or "").lower()
        if any(marker in low for marker in _AUTH_ERROR_MARKERS):
            return False
        return None  # ambiguous — let the caller try GraphQL
    except Exception:
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
