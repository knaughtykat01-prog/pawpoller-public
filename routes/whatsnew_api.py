"""What's-new — serves CHANGELOG.md entries the user hasn't seen yet, for the
in-app "what changed after this update" popup (frontend/js/app.js).

The app pops a changelog when the running version differs from the one this
browser last saw. Pure read of the bundled CHANGELOG.md — no state stored here;
the "last seen" version lives in the browser (localStorage) and arrives as the
``since`` query param.

**The popup shows the SUMMARY, never the full entry (2.156.0).** CHANGELOG.md is
the *engineering* record — it names functions, routes and root causes, because
the docs cross-reference entries by version and it carries context across dev
sessions. That is the wrong thing to pop in a user's face after an update, and
until 2.156.0 the modal rendered the entry body verbatim, so a release popped up
talking about ``assemble_works`` and ``GetTickCount64()``.

The convention: **a blockquote directly under the version header is the
plain-language summary**, and it is the only part the popup shows.

    ## [2.155.0] - 2026-07-17 - One works hub
    > Stories and Artwork are no longer separate pages — they're filters inside
    > your Library now.

    Backlog L. `/api/works` has always returned both kinds behind a ...

Entries written before this convention (and any that forget the blockquote) fall
back to their first paragraph — imperfect, but never a wall of internals.
"""
import logging
import re

from fastapi import APIRouter

import config

logger = logging.getLogger(__name__)

whatsnew_router = APIRouter(prefix="/api", tags=["whatsnew"])

# "## [2.133.0] - 2026-07-17 - Title"
_HEADER = re.compile(r"(?m)^##\s*\[(\d+\.\d+\.\d+)\]([^\n]*)\n")

# Cap so a big version jump (e.g. desktop v2.53 → v2.133) doesn't return a wall
# of text; the modal notes when older entries were trimmed.
_MAX_ENTRIES = 12


def _vtuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return ()


def _summarize(body: str) -> str:
    """The user-facing summary for one entry — see the module docstring.

    A leading ``>`` blockquote is the author's deliberate plain-language summary.
    Without one (every entry before 2.156.0), fall back to the first paragraph so
    the popup still says *something* short rather than the whole entry.
    """
    body = (body or "").strip()
    if not body:
        return ""

    quote = []
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith(">"):
            quote.append(s.lstrip("> ").strip())
        elif quote:
            break          # blockquote ended
        elif s:
            break          # entry opens with prose → no summary block
    if quote:
        return " ".join(x for x in quote if x).strip()

    # Fallback: first paragraph, minus any leading markdown heading.
    para = body.split("\n\n", 1)[0].strip()
    para = re.sub(r"(?m)^#{1,6}\s*", "", para)
    return " ".join(para.split())


def _parse_changelog(text: str) -> list[dict]:
    """Every entry as ``{version, header, body, summary}``, newest first (file order)."""
    matches = list(_HEADER.finditer(text or ""))
    out = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = re.sub(r"\n*-{3,}\s*$", "", text[start:end]).strip()   # drop trailing '---'
        out.append({"version": m.group(1), "header": m.group(2).strip(" -\t"),
                    "body": body, "summary": _summarize(body)})
    return out


def _load_changelog() -> str:
    try:
        return config.resource_path("CHANGELOG.md").read_text(encoding="utf-8")
    except Exception as e:  # bundled in the desktop build; present at /app on server
        logger.warning("Could not read CHANGELOG.md for /api/whatsnew: %s", e)
        return ""


@whatsnew_router.get("/whatsnew")
def whatsnew(since: str = ""):
    """CHANGELOG entries newer than ``since`` (the version this browser last saw),
    up to the running version. Returns ``{current, entries, truncated}`` where each
    entry is ``{version, header, summary}``. When ``since`` is empty (first run) or
    already current, ``entries`` is empty so the frontend shows nothing.

    The full ``body`` is deliberately NOT returned: this endpoint exists only to
    feed the update popup, and shipping the engineering detail is what made the
    popup unreadable in the first place. Read CHANGELOG.md for that.
    """
    current = config.APP_VERSION
    since_t, cur_t = _vtuple(since), _vtuple(current)

    if not since or (since_t and since_t >= cur_t):
        return {"current": current, "entries": [], "truncated": False}

    newer = [e for e in _parse_changelog(_load_changelog())
             if since_t < _vtuple(e["version"]) <= cur_t]
    return {
        "current": current,
        "entries": [{"version": e["version"], "header": e["header"],
                     "summary": e["summary"]} for e in newer[:_MAX_ENTRIES]],
        "truncated": len(newer) > _MAX_ENTRIES,
    }
