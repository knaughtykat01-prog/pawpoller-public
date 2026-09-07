"""Media kinds — image, video, audio (MEDIATYPES, 4.18.0; docs/specs/media_types.md §2).

One source of truth for what a Library file may be. Everything that used to spell out
``(".png", ".jpg", …)`` reads these tuples; the kind is always derived from the extension
(``kind_of``) so a hand-edited or imported folder cannot claim to be something it is not.

Deliberately excluded: Flash (swf/flv — dead), MKV/AVI/WMV (no browser plays them), MIDI.
The server never decodes media (no ffmpeg ships); dimensions and durations arrive from the
browser at upload time as a ``media`` block that ``normalise_media`` checks for sanity.
"""
from __future__ import annotations

import os

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v")
AUDIO_EXTENSIONS = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus")
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS + VIDEO_EXTENSIONS + AUDIO_EXTENSIONS
KINDS = ("image", "video", "audio")

# Archive caps per kind — a runaway upload must not fill the disk; the sites enforce their own.
MB = 1024 * 1024
MAX_BYTES = {"image": 50 * MB, "video": 512 * MB, "audio": 100 * MB}

_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".m4v": "video/x-m4v",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".opus": "audio/opus",
}

_KIND_LABEL = {"image": "images", "video": "video", "audio": "audio"}


def ext_of(filename) -> str:
    """Lower-case extension with its dot ('' when none)."""
    return os.path.splitext(str(filename or ""))[1].lower()


def kind_of(filename) -> str | None:
    ext = ext_of(filename)
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    return None


def mime_for(filename) -> str:
    return _MIME.get(ext_of(filename), "application/octet-stream")


def max_bytes_for(filename) -> int:
    return MAX_BYTES.get(kind_of(filename) or "image", MAX_BYTES["image"])


def accepted_from_types(types) -> dict:
    """Group a flat extension list (a poster's ``accepted_file_types``) into kinds.
    Story/text types (txt, html, pdf…) are not media and fall out."""
    out = {"image": [], "video": [], "audio": []}
    for t in types or []:
        k = kind_of("x." + str(t).lstrip(".").lower())
        if k:
            out[k].append(str(t).lstrip(".").lower())
    return out


def accepts_label(accepted: dict) -> str:
    """'png, jpg, jpeg, gif, webp images · mp3 audio' — the site's own list, in words."""
    parts = []
    for kind in KINDS:
        exts = [e for e in (accepted or {}).get(kind, []) if e]
        if exts:
            parts.append(f"{', '.join(exts)} {_KIND_LABEL[kind]}")
    return " · ".join(parts) or "nothing of this kind"


def refusal(platform_name: str, accepted: dict, kind: str, ext: str) -> str:
    """The sentence a picker and validate() both show when a site does not take the file."""
    what = f"{ext} {kind}" if ext else kind
    return f"{platform_name} doesn't take {what} — it takes {accepts_label(accepted)}."


def normalise_media(meta, kind: str, nbytes: int) -> dict:
    """The stored ``media`` block: the kind (re-derived), the byte size (measured here), and
    the browser's numbers when they are sane. Garbage is dropped, never trusted."""
    meta = meta if isinstance(meta, dict) else {}
    out = {"kind": kind, "bytes": int(nbytes)}
    dur = meta.get("duration_s")
    try:
        dur = float(dur)
        if 0 < dur < 24 * 3600:
            out["duration_s"] = round(dur, 3)
    except (TypeError, ValueError):
        pass
    for key in ("width", "height"):
        try:
            v = int(meta.get(key))
            if 0 < v <= 16384:
                out[key] = v
        except (TypeError, ValueError):
            pass
    return out


def format_duration(seconds) -> str:
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if s < 0:
        return ""
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
