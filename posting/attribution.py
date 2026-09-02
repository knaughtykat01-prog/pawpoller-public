"""The "Posted via PawPoller" attribution line (gap-wave-2 §1).

Story + artwork descriptions get a small credit line appended at package-build
time — the two builders (`story_reader.build_package`,
`artwork_reader.build_artwork_package`) are the choke points every posting path
flows through (post, edit, update, retry, scheduler), so one call there covers
every platform.

On by default (`pawpoller_attribution`, absent = ON); the Settings toggle asks
nicely before letting you turn it off. Deliberately NOT applied to microblog
Posts, and skipped for Bluesky even on story/artwork packages: the bsky
"description" is a 295-char announcement post (truncated inside its poster), so
a credit line there would eat the announcement itself.
"""
from __future__ import annotations

import config

# Plain text + bare URL so it survives BBCode (IB/WS), HTML (SF/AO3/SQW) and
# plain-text description fields alike.
ATTRIBUTION_LINE = "🐾 Posted via PawPoller — pawpoller.pages.dev"

# DeviantArt strips off-site URLs out of a description AND removes the
# paragraph they sat in, so the line above doesn't arrive shortened — it
# doesn't arrive. Measured on a live post (2026-08-22, deviation 1371636392):
# the paragraph carrying `pawpoller.pages.dev` was the only one DA dropped.
# DA therefore gets the credit without the domain, rather than losing the line
# altogether. `_MARKER` matches both forms, so idempotency is unaffected.
ATTRIBUTION_LINE_NO_URL = "🐾 Posted via PawPoller"
_NO_URL_PLATFORMS = {"da"}

# Platforms whose package "description" is really a microblog announcement.
_SKIP_PLATFORMS = {"bsky"}

# Idempotency marker — never double-append (edit/update re-builds, or a user
# who typed their own credit line).
_MARKER = "Posted via PawPoller"


def maybe_append(description: str, platform: str, settings: dict | None = None) -> str:
    """Append the attribution line to a description, when enabled and sensible."""
    s = settings if settings is not None else config.get_settings()
    if not s.get("pawpoller_attribution", True):
        return description
    if platform in _SKIP_PLATFORMS:
        return description
    desc = description or ""
    if _MARKER in desc:
        return desc
    line = ATTRIBUTION_LINE_NO_URL if platform in _NO_URL_PLATFORMS else ATTRIBUTION_LINE
    return (desc.rstrip() + "\n\n" + line) if desc.strip() else line
