"""Journals (spec 027): a Posts-module post with a title, posted to FurAffinity, Weasyl and DeviantArt.

A journal is a ``posts`` row with ``kind = 'journal'`` (title, Markdown body, rating, tags, FA's featured tick); its
sites are ``post_publications`` rows, so scheduling, results, retry and the Ledger come with the Posts module.
Each site gets the body in its own format through the sign-in PawPoller already holds there:

* **FurAffinity** — the ``/controls/journal/`` form (BBCode, rating 0/2/1, ≤ 60-character title, "featured").
* **Weasyl** — ``/submit/journal`` signed by the API key (Markdown, rating 10/30/40, tags).
* **DeviantArt** — the official ``deviation/journal/create`` (HTML, ``is_mature`` + level, tags).

**Announcing a piece** (US2): the publish dialog's journal box is stored as a pending journal linked to the piece
(``store_piece_journal``) and posted by ``journal_pass`` after the gallery posts, its template filled with the links
that same publish made. **Fixing** (US3): ``edit_journal`` re-sends the whole journal; ``remove_journal`` removes it
from Weasyl and says "remove it on the site" for FA and DeviantArt.

No LLM anywhere: templates are plain substitution.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone

import config
from database import accounts as accounts_db
from database import posts_queries
from database.db import get_connection

logger = logging.getLogger(__name__)

# Posted automatically. FurAffinity is NOT here: its journal form demands a CAPTCHA (live, 2026-10-10: "Error posting a
# journal. CAPTCHA verification failed"), and PawPoller never gets past a bot check (constitution VII). FA journals are
# a copy: PawPoller writes the FA version (BBCode), the owner pastes it on FA's page and passes the CAPTCHA there.
JOURNAL_SITES = ("ws", "da")
COPY_SITES = ("fa",)
FA_JOURNAL_PAGE = "https://www.furaffinity.net/controls/journal/"
FA_CAPTCHA = ("FurAffinity asks for a CAPTCHA on every journal, so PawPoller can't post it for you. Use Copy for "
              "FurAffinity on the journal page: it copies the FA version and opens FA's journal form to paste it into.")
LABELS = {"fa": "FurAffinity", "ws": "Weasyl", "da": "DeviantArt"}
TITLE_LIMITS = {"fa": 60, "ws": 100, "da": 50}
REMOVABLE = ("ws",)                      # FA and DA: "remove it on the site" (no delete call trusted yet)

_FA_RATING = {"general": "0", "mature": "2", "adult": "1"}
_WS_RATING = {"general": 10, "mature": 30, "adult": 40}

DEFAULT_TEMPLATES = {
    "art_title": "New art: {title}",
    "art": "New art: {title}! {links}",
    "chapter_title": "{title}: chapter {chapter}",
    "chapter": "Chapter {chapter} of {title} is up: {chapter_title}. Read it here: {links}",
}
EXTRA_TOKENS = ("chapter", "chapter_title")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def label(platform: str) -> str:
    return LABELS.get(platform, platform)


def templates(settings: dict | None = None) -> dict:
    stored = ((settings if settings is not None else config.get_settings()).get("journal_templates") or {})
    return {k: (str(stored.get(k)) if stored.get(k) else v) for k, v in DEFAULT_TEMPLATES.items()}


def tag_list(tags) -> list[str]:
    if isinstance(tags, (list, tuple)):
        raw = tags
    else:
        raw = re.split(r"[,\s]+", str(tags or ""))
    seen, out = set(), []
    for t in raw:
        t = str(t).strip().strip("#").replace(" ", "_")
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


# ── Markdown subset → BBCode / HTML ─────────────────────────────────────────────
# The story converters don't convert links (checked 2026-10-10), so journals carry their own: paragraphs, # headings,
# "- " lists, **bold**, *italic*, [text](url) and bare URLs. Anything else stays as typed.

_INLINE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)|\*\*([^*\n]+)\*\*|(?<![*\w])\*([^*\n]+)\*(?!\*)"
                     r"|(https?://[^\s<>()\[\]]+)")


def _inline(text: str, kind: str) -> str:
    out, pos = [], 0
    esc = html.escape if kind == "html" else (lambda s: s)
    for m in _INLINE.finditer(text):
        out.append(esc(text[pos:m.start()]))
        link_text, url, bold, ital, bare = m.groups()
        if url:
            out.append(f'<a href="{html.escape(url)}">{esc(link_text)}</a>' if kind == "html"
                       else f"[url={url}]{link_text}[/url]")
        elif bold:
            out.append(f"<strong>{esc(bold)}</strong>" if kind == "html" else f"[b]{bold}[/b]")
        elif ital:
            out.append(f"<em>{esc(ital)}</em>" if kind == "html" else f"[i]{ital}[/i]")
        else:
            out.append(f'<a href="{html.escape(bare)}">{esc(bare)}</a>' if kind == "html" else f"[url]{bare}[/url]")
        pos = m.end()
    out.append(esc(text[pos:]))
    return "".join(out)


def _blocks(md: str) -> list[tuple[str, list[str]]]:
    """→ [(kind, lines)]: 'p' paragraph, 'h' heading, 'ul' list."""
    blocks: list[tuple[str, list[str]]] = []
    for chunk in re.split(r"\n\s*\n", (md or "").strip()):
        lines = [ln.rstrip() for ln in chunk.split("\n") if ln.strip()]
        if not lines:
            continue
        if all(re.match(r"\s*[-*] ", ln) for ln in lines):
            blocks.append(("ul", [re.sub(r"^\s*[-*] ", "", ln) for ln in lines]))
        elif len(lines) == 1 and re.match(r"#{1,6} ", lines[0]):
            blocks.append(("h", [lines[0].lstrip("#").strip()]))
        else:
            blocks.append(("p", lines))
    return blocks


def to_bbcode(md: str) -> str:
    parts = []
    for kind, lines in _blocks(md):
        if kind == "h":
            parts.append(f"[b]{_inline(lines[0], 'bb')}[/b]")
        elif kind == "ul":
            parts.append("\n".join(f"• {_inline(ln, 'bb')}" for ln in lines))
        else:
            parts.append("\n".join(_inline(ln, "bb") for ln in lines))
    return "\n\n".join(parts)


def to_html(md: str) -> str:
    parts = []
    for kind, lines in _blocks(md):
        if kind == "h":
            parts.append(f"<h3>{_inline(lines[0], 'html')}</h3>")
        elif kind == "ul":
            parts.append("<ul>" + "".join(f"<li>{_inline(ln, 'html')}</li>" for ln in lines) + "</ul>")
        else:
            parts.append("<p>" + "<br>".join(_inline(ln, "html") for ln in lines) + "</p>")
    return "\n".join(parts)


# ── Clients through each site's own poster (same sign-in, same caching rules) ──────────────────────────────────────

async def _fa(account_id):
    from posting.platforms.furaffinity import FurAffinityPoster
    return await FurAffinityPoster(account_id=account_id)._ensure_client()


async def _ws(account_id):
    from posting.platforms.weasyl import WeasylPoster
    p = WeasylPoster()
    p.account_id = account_id
    return await p._ensure_client()


async def _da(account_id):
    from posting.platforms.deviantart import DeviantArtPoster
    p = DeviantArtPoster()
    p.account_id = account_id
    return await p._ensure_client()          # (client, access_token)


_RATING_WORDS = {"explicit": "adult", "nsfw": "adult", "questionable": "mature", "teen": "general"}


def _rating(post: dict) -> str:
    """general / mature / adult — stories say explicit / teen, which must never fall to general."""
    r = (post.get("rating") or "general").lower()
    r = _RATING_WORDS.get(r, r)
    return r if r in _FA_RATING else "adult"          # unknown: the strictest, never the loosest


def title_problem(platform: str, title: str) -> str:
    limit = TITLE_LIMITS.get(platform)
    if not (title or "").strip():
        return "A journal needs a title"
    if limit and len(title) > limit:
        return f"{label(platform)} titles are at most {limit} characters ({len(title)} here)"
    return ""


async def _send(post: dict, platform: str, account_id, *, external_id: str = "") -> dict:
    """Create (no ``external_id``) or update one site's journal. → {"id", "url"}; raises with a plain reason."""
    title, body, rating = post.get("title") or "", post.get("body") or "", _rating(post)
    tags = tag_list(post.get("tags"))
    if platform == "fa":
        cli = await _fa(account_id)
        return await cli.submit_journal(title, to_bbcode(body), _FA_RATING[rating],
                                        featured=bool(post.get("featured")), journal_id=external_id or "0")
    if platform == "ws":
        cli = await _ws(account_id)
        if external_id:
            return await cli.edit_journal(external_id, title, body, _WS_RATING[rating], " ".join(tags))
        return await cli.submit_journal(title, body, _WS_RATING[rating], " ".join(tags))
    if platform == "da":
        from posting.platforms.deviantart import _rating_to_da
        cli, token = await _da(account_id)
        mature, level, classes = _rating_to_da(rating)
        kw = dict(title=title, body=to_html(body), tags=tags, is_mature=mature, mature_level=level,
                  mature_classification=classes, access_token=token)
        if external_id:
            r = await cli.oauth_update_journal(external_id, **kw)
            return {"id": external_id, "url": (r or {}).get("url", "")}
        r = await cli.oauth_create_journal(**kw)
        return {"id": r["deviationid"], "url": r["url"]}
    raise RuntimeError(f"Journals don't go to {platform}")


def _base_result(platform: str, account_id) -> dict:
    return {"platform": platform, "account_id": account_id or 0, "success": False,
            "external_id": "", "external_url": "", "error": ""}


async def publish_journal(post: dict, platform: str, account_id: int | None, settings: dict | None = None) -> dict:
    """One site's journal; the ``post_publisher._publish_one`` result shape."""
    from posting.post_publisher import _resolve_creds
    account_id, _creds = _resolve_creds(platform, account_id, settings)
    result = _base_result(platform, account_id)
    if platform in COPY_SITES:
        result["error"] = FA_CAPTCHA
        return result
    if platform not in JOURNAL_SITES:
        result["error"] = f"Journals don't go to {label(platform)} yet"
        return result
    if account_id in accounts_db.never_post_ids(settings):          # FRIENDGUARD: never post as someone else
        result["error"] = accounts_db.NEVER_POST_ERROR
        return result
    problem = title_problem(platform, post.get("title") or "")
    if problem:
        result["error"] = problem
        return result
    try:
        r = await _send(post, platform, account_id or None)
        result.update(success=True, external_id=str(r.get("id") or ""), external_url=r.get("url") or "")
    except Exception as e:
        logger.warning("Journal to %s failed: %s", platform, e)
        result["error"] = str(e)
    return result


async def edit_journal(post: dict, pub: dict, settings: dict | None = None) -> dict:
    """Re-send the whole journal to one site where it's already posted (US3)."""
    platform = pub["platform"]
    result = _base_result(platform, pub.get("account_id"))
    if platform in COPY_SITES:               # FA's form needs a CAPTCHA: the person updates it there
        why = f"Update it on {label(platform)} yourself — its journal form needs a CAPTCHA"
        result.update(skipped=True, error=why, reason=why)
        return result
    if (pub.get("account_id") or 0) in accounts_db.never_post_ids(settings):   # FRIENDGUARD, as publish
        result["error"] = accounts_db.NEVER_POST_ERROR
        return result
    problem = title_problem(platform, post.get("title") or "")
    if problem:
        result["error"] = problem
        return result
    try:
        r = await _send(post, platform, pub.get("account_id") or None, external_id=str(pub.get("external_id") or ""))
        result.update(success=True, external_id=str(pub.get("external_id") or ""),
                      external_url=r.get("url") or pub.get("external_url") or "")
    except Exception as e:
        logger.warning("Journal edit on %s failed: %s", platform, e)
        result["error"] = str(e)
    return result


async def remove_journal(pub: dict, settings: dict | None = None) -> dict:
    platform = pub["platform"]
    result = _base_result(platform, pub.get("account_id"))
    if platform not in REMOVABLE:
        why = f"Remove it on {label(platform)} — PawPoller can't remove journals there yet"
        result.update(skipped=True, error=why, reason=why)
        return result
    if (pub.get("account_id") or 0) in accounts_db.never_post_ids(settings):   # FRIENDGUARD, as publish
        result["error"] = accounts_db.NEVER_POST_ERROR
        return result
    try:
        cli = await _ws(pub.get("account_id") or None)
        await cli.remove_journal(str(pub.get("external_id") or ""))
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
    return result


def copy_text(platform: str, title: str, body: str, rating: str = "general") -> dict:
    """A journal for a site PawPoller can't post to (FA's CAPTCHA): its title within the site's limit, the body in
    the site's own markup, and the page to paste it into."""
    if platform not in COPY_SITES:
        raise ValueError(f"{label(platform)} journals are posted for you, not copied")
    limit = TITLE_LIMITS.get(platform) or 200
    return {"title": (title or "").strip()[:limit], "text": to_bbcode(body or ""), "open_url": FA_JOURNAL_PAGE,
            "title_cut": len((title or "").strip()) > limit, "rating": _FA_RATING[_rating({"rating": rating})]}


def record_manual(post_id: int, platform: str, external_id: str, external_url: str) -> bool:
    """The desktop's filled-in FA window saw FA land on the new journal: record it on the journal's row, as the
    site's default account (the window posts as whoever is signed in there)."""
    if platform not in COPY_SITES or not str(external_id).isdigit():
        return False
    if not str(external_url).startswith("https://www.furaffinity.net/journal/"):
        return False
    conn = get_connection()
    try:
        post = posts_queries.get_post(conn, int(post_id))
        if not post or post.get("kind") != "journal":
            return False
        acct = accounts_db.get_default_account_id(conn, platform, create=False) or 0
        posts_queries.upsert_post_publication(conn, post_id=int(post_id), platform=platform, account_id=acct,
                                              status="posted", external_id=str(external_id),
                                              external_url=external_url, error="", now=_now())
    finally:
        conn.close()
    return True


def preview(title: str, sites: list[str], account_ids: dict | None = None, settings: dict | None = None) -> dict:
    """Each chosen site's warnings before posting (the composer's rows)."""
    from posting.post_publisher import _resolve_creds
    never = accounts_db.never_post_ids(settings)
    need = {"fa": ("fa_cookie_a", "fa_cookie_b"), "ws": ("ws_api_key",), "da": ("da_refresh_token",)}
    out = {}
    for plat in sites:
        if plat not in JOURNAL_SITES:
            continue
        warns = []
        acct, creds = _resolve_creds(plat, (account_ids or {}).get(plat), settings)
        connected = all(creds.get(k) for k in need[plat])
        problem = title_problem(plat, title)
        if problem:
            warns.append({"level": "block", "text": problem})
        if not connected:
            warns.append({"level": "block", "text": f"{label(plat)} isn't connected"})
        elif acct in never:
            warns.append({"level": "block", "text": accounts_db.NEVER_POST_ERROR})
        out[plat] = {"title_limit": TITLE_LIMITS[plat], "connected": connected, "warnings": warns}
    return out


# ── Announcing a piece (US2) ─────────────────────────────────────────────────

def _unknown(text: str) -> list[str]:
    from posting import paired_comment
    return [t for t in paired_comment.unknown_tokens(text) if t not in EXTRA_TOKENS]


def store_piece_journal(kind: str, name: str, journal, rating: str = "general",
                        account_ids: dict | None = None) -> int | None:
    """The dialog's journal box → a pending journal post linked to the piece. None when there is nothing to post.
    Raises ValueError with a plain reason (no sites, no text, an unknown placeholder)."""
    if not journal:
        return None
    if not isinstance(journal, dict):
        raise ValueError("journal must be {sites, title, text, featured}")
    sites = [s for s in (journal.get("sites") or []) if s in JOURNAL_SITES]
    title, text = str(journal.get("title") or "").strip(), str(journal.get("text") or "").strip()
    if not sites:
        return None
    if not title or not text:
        raise ValueError("A journal needs a title and some text")
    for part in (title, text):
        bad = _unknown(part)
        if bad:
            raise ValueError(f"the journal uses {{{bad[0]}}}, which PawPoller doesn't know")
    now = _now()
    conn = get_connection()
    try:
        pid = posts_queries.create_post(conn, body=text[:20000], rating=_rating({"rating": rating}), now=now,
                                        kind="journal",
                                        title=title[:200], tags=" ".join(tag_list(journal.get("tags"))),
                                        featured=bool(journal.get("featured")))
        posts_queries.update_post(conn, pid, linked_kind=kind, linked_ref=name, now=now)
        for s in sites:
            posts_queries.upsert_post_publication(conn, post_id=pid, platform=s,
                                                  account_id=int((account_ids or {}).get(s) or 0),
                                                  status="pending", external_id="", external_url="",
                                                  error="", now=now)
    finally:
        conn.close()
    return pid


def _pending_for(kind: str, name: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT p.post_id FROM posts p WHERE p.kind = 'journal' AND p.linked_kind = ? AND p.linked_ref = ? "
            "AND EXISTS (SELECT 1 FROM post_publications pp WHERE pp.post_id = p.post_id AND pp.status = 'pending')",
            (kind, name)).fetchall()
        out = []
        for r in rows:
            post = posts_queries.get_post(conn, r["post_id"])
            post["pending"] = [dict(x) for x in conn.execute(
                "SELECT * FROM post_publications WHERE post_id = ? AND status = 'pending' ORDER BY id",
                (r["post_id"],)).fetchall()]
            out.append(post)
        return out
    finally:
        conn.close()


def fill(text: str, ctx: dict, platform: str, settings: dict | None = None) -> tuple[str, list[str]]:
    """paired_comment.fill + {chapter} / {chapter_title}. An empty link token is DROPPED (with the name returned),
    never posted raw."""
    from posting import paired_comment
    for t in EXTRA_TOKENS:
        text = text.replace("{" + t + "}", str(ctx.get(t) or ""))
    out, empty = paired_comment.fill(text, ctx, platform, settings)
    return re.sub(r"[ \t]{2,}", " ", out).strip(), empty


async def journal_pass(kind: str, name: str, results: list[dict], settings: dict | None = None) -> None:
    """After a piece's publish loop: its pending journals go up, linking what this run made. Nothing goes up when
    nothing in the run landed — the journal waits for a run that does."""
    posted = [r for r in results if r.get("success") and not r.get("queued_desktop")]
    if not posted:
        return
    pending = _pending_for(kind, name)
    if not pending:
        return
    from posting import activity, paired_comment
    from posting.manager import _run_links
    chapter = max((int(r.get("chapter_index") or 0) for r in posted), default=0) if kind == "story" else 0
    chapter_title = next((str(r.get("chapter_title") or "") for r in posted
                          if int(r.get("chapter_index") or 0) == chapter), "")
    for post in pending:
        for pub in post["pending"]:
            plat = pub["platform"]
            if activity.cancelled():
                return
            ctx = paired_comment.piece_context(kind, name, plat, chapter, pub.get("account_id") or None)
            ctx["extra"] = {**ctx["extra"], "run_links": [p for p in _run_links(results, chapter or None)]}
            ctx["chapter"], ctx["chapter_title"] = (chapter or ""), chapter_title
            title, _ = fill(post.get("title") or "", ctx, plat, settings)
            body, empty = fill(post.get("body") or "", ctx, plat, settings)
            filled = {**post, "title": title, "body": body}
            res = await publish_journal(filled, plat, pub.get("account_id") or None, settings)
            if res["success"] and empty:
                res["error"] = f"Left out {{{empty[0]}}}: that link didn't exist yet"
            conn = get_connection()
            try:
                posts_queries.upsert_post_publication(
                    conn, post_id=post["post_id"], platform=plat, account_id=res["account_id"],
                    status="posted" if res["success"] else "failed", external_id=res["external_id"],
                    external_url=res["external_url"], error=res.get("error") or "", now=_now())
                if res["success"]:          # what went out, so a later edit starts from the real text
                    posts_queries.update_post(conn, post["post_id"], title=title, body=body, now=_now())
                if pub.get("account_id") != res["account_id"]:
                    conn.execute("DELETE FROM post_publications WHERE id = ?", (pub["id"],))
                    conn.commit()
            finally:
                conn.close()
            results.append({**res, "journal": True, "label": f"{label(plat)} journal"})
