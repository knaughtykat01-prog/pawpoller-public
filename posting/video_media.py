"""Videos and GIFs found on the owner's own posts (spec 047, VIDEOIMPORT).

One entry shape shared by the X, Instagram and Bluesky pollers (stored as JSON in each
``*_submissions.video_media`` column) and the artwork importer that brings the real file in:

    {"url": <download link>, "poster": <the site's preview picture>, "kind": "video" | "gif",
     "width"?: int, "height"?: int, "duration_s"?: float, "bitrate"?: int}

The server never decodes video (MEDIATYPES rule): a file is accepted by its container signature
alone, and its poster is the site's own picture — an entry without one is dropped.
"""
from __future__ import annotations

import ipaddress
import json
import socket

import httpx

from posting import media_kinds

GONE = "The site no longer has this video. Poll again, then import."
POSTER_CAP = 20 * 1024 * 1024
_resolve = socket.getaddrinfo          # tests swap this out (no DNS in the suite)


class NotPublic(ValueError):
    """A stored URL (or a redirect) pointed somewhere other than the public internet."""


def _guard(request: httpx.Request) -> None:
    """Request hook: every hop, redirects included, must be http(s) to public addresses only.
    Stored URLs come from site responses (and a Bluesky user's own PDS), so without this a
    redirect could point a seat at localhost, the cloud metadata address or the home LAN.
    ponytail: checked here and resolved again by httpx, so DNS rebinding between the two could
    slip past; pin the connection to the checked address if that ever matters."""
    url = request.url
    if url.scheme not in ("http", "https") or not url.host:
        raise NotPublic("That link isn't a web address PawPoller can download from.")
    try:
        infos = _resolve(url.host, url.port or 443, proto=socket.IPPROTO_TCP)
        addrs = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}
    except (OSError, UnicodeError, ValueError):
        raise NotPublic(f"Couldn't find {url.host} on the internet.")
    if not addrs or not all(a.is_global for a in addrs):
        raise NotPublic(f"{url.host} isn't on the public internet, so PawPoller won't download from it.")


def public_client(timeout: float = 60.0) -> httpx.Client:
    """The importer's HTTP client: follows redirects, but every hop passes `_guard`."""
    return httpx.Client(follow_redirects=True, timeout=timeout, event_hooks={"request": [_guard]})


def _check_status(resp: httpx.Response) -> None:
    # A plain reason, not httpx's message (which repeats the URL: Instagram's are signed).
    if resp.status_code in (403, 404, 410):
        raise ValueError(GONE)
    if resp.status_code >= 400:
        raise ValueError(f"The site answered {resp.status_code}. Try again later.")


def entry(url, poster, kind="video", *, width=None, height=None, duration_s=None, bitrate=None) -> dict | None:
    """One stored entry, or None when there is nothing to import (no file or no poster)."""
    if not url or not poster or not str(url).startswith("https://"):
        return None
    out = {"url": str(url), "poster": str(poster), "kind": "gif" if kind == "gif" else "video"}
    for key, val, cast in (("width", width, int), ("height", height, int),
                           ("duration_s", duration_s, float), ("bitrate", bitrate, int)):
        try:
            if val is not None and cast(val) > 0:
                out[key] = round(cast(val), 3) if cast is float else cast(val)
        except (TypeError, ValueError):
            pass
    return out


def best_mp4(variants, rate_key: str = "bitrate") -> dict | None:
    """The highest-bitrate mp4 among a site's variants (X lists an m3u8 playlist too)."""
    mp4 = [v for v in (variants or []) if isinstance(v, dict) and v.get("url")
           and (v.get("content_type") or "video/mp4") == "video/mp4"]
    return max(mp4, key=lambda v: v.get(rate_key) or 0) if mp4 else None


def parse(raw) -> list[dict]:
    """The stored column back as a list ('' / garbage → [])."""
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict) and e.get("url")]
    try:
        val = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [e for e in val if isinstance(e, dict) and e.get("url")] if isinstance(val, list) else []


def summary_kind(raw) -> str:
    """'gif' when every entry is a GIF, 'video' when any is a video, '' when none — the found-list badge."""
    entries = parse(raw)
    if not entries:
        return ""
    return "gif" if all(e.get("kind") == "gif" for e in entries) else "video"


def video_ext(data: bytes) -> str:
    """'.mp4' for an ISO-BMFF file (``ftyp`` box), '.webm' for EBML, '' for anything else."""
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    return ""


def download(client: httpx.Client, url: str, cap: int | None = None, *, video: bool = True) -> bytes:
    """The whole file, streamed and capped. Raises ValueError with a plain reason.
    ``video=False`` skips the container check (the poster; the caller checks it's an image).

    ponytail: held in memory (≤ the 512 MB video cap); spool to a temp file if seats ever
    import near the cap often.
    """
    cap = cap or media_kinds.MAX_BYTES["video"]
    limit_mb = cap // media_kinds.MB
    what = "video" if video else "picture"
    with client.stream("GET", url, timeout=httpx.Timeout(300.0, connect=30.0)) as resp:
        _check_status(resp)
        try:
            declared = int(resp.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if declared > cap:
            raise ValueError(f"This {what} is {declared // media_kinds.MB} MB; the Library takes up to {limit_mb} MB.")
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf += chunk
            if len(buf) > cap:
                raise ValueError(f"This {what} is over {limit_mb} MB; the Library takes up to {limit_mb} MB.")
    data = bytes(buf)
    if video and not video_ext(data):
        raise ValueError("The site sent something that isn't a video file.")
    return data


def from_x_graphql(media) -> list[dict]:
    """X's ``extended_entities.media`` (GraphQL / v1.1 shape) → entries. Photos are skipped."""
    out = []
    for m in media or []:
        kind = {"video": "video", "animated_gif": "gif"}.get(m.get("type") or "")
        if not kind:
            continue
        info = m.get("video_info") or {}
        best = best_mp4(info.get("variants"), "bitrate")
        size = m.get("original_info") or {}
        e = entry((best or {}).get("url"), m.get("media_url_https"), kind,
                  width=size.get("width"), height=size.get("height"),
                  duration_s=(info.get("duration_millis") or 0) / 1000 or None,
                  bitrate=(best or {}).get("bitrate"))
        if e:
            out.append(e)
    return out


def from_x_v2(media) -> list[dict]:
    """X API v2 ``includes.media`` items (``variants`` uses ``bit_rate``) → entries."""
    out = []
    for m in media or []:
        kind = {"video": "video", "animated_gif": "gif"}.get(m.get("type") or "")
        if not kind:
            continue
        best = best_mp4(m.get("variants"), "bit_rate")
        e = entry((best or {}).get("url"), m.get("preview_image_url"), kind,
                  width=m.get("width"), height=m.get("height"),
                  duration_s=(m.get("duration_ms") or 0) / 1000 or None,
                  bitrate=(best or {}).get("bit_rate"))
        if e:
            out.append(e)
    return out
