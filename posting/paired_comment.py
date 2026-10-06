"""Paired comments (spec 021) — a reply from the same account under your own post.

The link-in-the-reply habit: the post goes up clean, and a second post from the same
account replies straight under it with the link, the credit, the content note or the
"full story here". Instagram's version is the first comment.

Shape of it:

* **One row per (owner, site)** in ``paired_comments`` holds the text and how it went
  (``database/comment_queries.py``). It is written before posting, so whichever road
  the post finally takes — Post now, a scheduled slot, a retry row, a desktop
  hand-off — the comment is found by what it hangs under.
* **A comment pass at the end of each publish** (``comment_pass_posts`` /
  ``comment_pass_pieces``), never inline: an X comment whose ``{link}`` points at the
  FA post made in the same run needs FA to have landed first. The manager already
  posts announcers last for the same reason.
* **A comment never changes its post's result** (FR-006). It reports on its own
  activity line and its own row.
* **Automatic retry** (FR-017): a failure that can pass on its own is tried again
  after 5 min, 30 min and 2 h by the scheduler tick (``run_due``); a failure that
  needs a person (permission, login, deleted post, never-post) is not.

No LLM anywhere: placeholders are plain substitution from stored data.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import config
from database.db import get_connection
from database import accounts as accounts_db
from database import comment_queries as cq

logger = logging.getLogger(__name__)

# Sites whose posting API can reply to our own post. Tumblr can't (no reply-as-post).
REPLY_PLATFORMS = ("bsky", "mast", "tw", "thr", "tg", "ig")
# Proven live end to end. The rest are built to each site's documented reply call and
# say so in the preview until the operator's live check (quickstart §3) passes.
PROVEN = frozenset({"bsky", "mast"})
# Instagram comments cap at 2,200 like captions; everything else uses the post limit.
_COMMENT_LIMITS = {"ig": 2200}

PLACEHOLDERS = ("link", "links", "site:<code>", "title", "artist")
_TOKEN_RE = re.compile(r"\{([a-z]+(?::[a-z0-9]+)?)\}")

RETRY_DELAYS = (300, 1800, 7200)          # 5 min, 30 min, 2 h (operator, 2026-10-05)
MAX_TEMPLATES, MAX_TEMPLATE_NAME, MAX_TEMPLATE_TEXT = 40, 60, 2000

_BSKY_LABELS = {"mature": ["sexual"], "adult": ["porn"]}
_SENSITIVE = ("mature", "adult")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def label(platform: str) -> str:
    from posting.post_publisher import LABELS
    return LABELS.get(platform, platform)


def comment_limit(platform: str) -> int | None:
    from posting.post_publisher import text_limit
    return _COMMENT_LIMITS.get(platform) or text_limit(platform)


# ── Placeholders ──────────────────────────────────────────────────────────────

def tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text or "")


def unknown_tokens(text: str) -> list[str]:
    out = []
    for t in tokens(text):
        if t in ("link", "links", "title", "artist") or (t.startswith("site:") and len(t) > 5):
            continue
        out.append(t)
    return out


def fill(text: str, ctx: dict | None, platform: str, settings: dict | None = None) -> tuple[str, list[str]]:
    """Fill a comment's placeholders for one site. Returns (text, empty_tokens).

    ``ctx`` (all optional): ``extra`` — the dict ``announce.resolve_links`` reads
    (links_by_platform, run_links, link_mode, link_platforms); ``title``; ``artist``
    (the registry row). ``{link}`` is the first link the site's link mode picks, so a
    piece whose owner said "no links off-site from X" gives the comment none either.
    An empty token is reported, never posted raw (FR-010 / FR-016)."""
    ctx = ctx or {}
    found = tokens(text)
    if not found:
        return text, []
    from posting import announce
    extra = ctx.get("extra") or {}
    picked = announce.resolve_links(SimpleNamespace(extra=extra), settings, platform)
    everything = announce.resolve_links(SimpleNamespace(extra={**extra, "link_mode": "all"}), settings, "")
    by_site: dict[str, str] = {}
    for key in ("links_by_platform", "run_links"):
        for item in extra.get(key) or []:
            if isinstance(item, (list, tuple)) and len(item) == 2 and item[1]:
                by_site.setdefault(str(item[0]), str(item[1]))
    artist_txt = ""
    if ctx.get("artist"):
        from posting import artist_credit
        artist_txt = artist_credit.render(ctx["artist"], platform, prefix="").strip()
    values = {"link": picked[0] if picked else "", "links": "\n".join(everything),
              "title": str(ctx.get("title") or ""), "artist": artist_txt}
    empty: list[str] = []

    def sub(m):
        t = m.group(1)
        v = by_site.get(t[5:], "") if t.startswith("site:") else values.get(t)
        if v is None:
            return m.group(0)       # unknown: left for the caller's unknown_tokens check
        if not v:
            empty.append(t)
        return v
    return _TOKEN_RE.sub(sub, text), empty


# ── Templates + per-site defaults (settings) ─────────────────────────────────

def templates(settings: dict | None = None) -> tuple[list[dict], dict]:
    s = settings if settings is not None else config.get_settings()
    t = [x for x in (s.get("comment_templates") or []) if isinstance(x, dict) and x.get("name")]
    d = {k: v for k, v in (s.get("comment_defaults") or {}).items() if isinstance(v, str)}
    return t, d


def validate_templates(tpls, defaults) -> tuple[list[dict], dict]:
    """Clean + check what the Settings page saves. Raises ValueError with a plain reason."""
    if not isinstance(tpls, list) or not isinstance(defaults or {}, dict):
        raise ValueError("templates must be a list and defaults a mapping")
    if len(tpls) > MAX_TEMPLATES:
        raise ValueError(f"at most {MAX_TEMPLATES} templates")
    out, seen = [], set()
    for t in tpls:
        if not isinstance(t, dict):
            raise ValueError("each template needs a name and text")
        name = str(t.get("name") or "").strip()
        text = str(t.get("text") or "")
        if not name:
            raise ValueError("a template has no name")
        if len(name) > MAX_TEMPLATE_NAME:
            raise ValueError(f"template names are at most {MAX_TEMPLATE_NAME} characters")
        if len(text) > MAX_TEMPLATE_TEXT:
            raise ValueError(f"“{name}” is over {MAX_TEMPLATE_TEXT} characters")
        if not text.strip():
            raise ValueError(f"“{name}” has no text")
        if name.lower() in seen:
            raise ValueError(f"two templates are called “{name}”")
        bad = unknown_tokens(text)
        if bad:
            raise ValueError(f"“{name}” uses {{{bad[0]}}}, which PawPoller doesn't know — "
                             "use {link}, {links}, {site:fa}, {title} or {artist}")
        seen.add(name.lower())
        out.append({"name": name, "text": text})
    names = {t["name"] for t in out}
    clean_defaults = {}
    for plat, name in (defaults or {}).items():
        if plat not in REPLY_PLATFORMS:
            raise ValueError(f"{plat} can't take a comment")
        if not name:
            continue
        if name not in names:
            raise ValueError(f"{label(plat)}'s default “{name}” isn't a saved template")
        clean_defaults[plat] = name
    return out, clean_defaults


def default_text(platform: str, settings: dict | None = None) -> tuple[str, str]:
    """(text, template_name) of this site's default template, or ('', '')."""
    tpls, defs = templates(settings)
    name = defs.get(platform) or ""
    t = next((x for x in tpls if x["name"] == name), None)
    return (t["text"], name) if t else ("", "")


# ── Sending one reply ─────────────────────────────────────────────────────────

async def send_reply(platform: str, account_id: int | None, parent: dict, text: str, *,
                     rating: str = "general", mentions: list[dict] | None = None,
                     settings: dict | None = None, idempotency_key: str = "") -> dict[str, Any]:
    """Post `text` as a reply under `parent` on `platform`, as `account_id`. Never raises.

    ``parent``: ``{"id": <external id>}`` — plus, for Bluesky, ``uri``/``cid`` and
    ``root_uri``/``root_cid`` when the caller already has them (thread parts); missing
    refs are read with one getPosts call. Returns ``{success, external_id,
    external_url, error, account_id, refs?}``."""
    from posting.post_publisher import _render_body, _resolve_creds
    out: dict[str, Any] = {"success": False, "external_id": "", "external_url": "",
                           "error": "", "account_id": account_id or 0}
    if platform not in REPLY_PLATFORMS:
        out["error"] = f"{label(platform)} has no replies, so the comment was left off"
        return out
    account_id, creds = _resolve_creds(platform, account_id, settings)
    out["account_id"] = account_id
    if account_id in accounts_db.never_post_ids(settings):
        out["error"] = accounts_db.NEVER_POST_ERROR
        return out
    pid = str(parent.get("id") or parent.get("uri") or "")
    if not pid:
        out["error"] = "the post's id on the site is unknown"
        return out
    mentions = mentions or []
    body = _render_body(text, mentions, platform)
    try:
        if platform == "bsky":
            from clients.bsky.client import BskyClient, _API_BASE as _BSKY_API
            client = BskyClient(identifier=creds.get("bsky_identifier", ""),
                                app_password=creds.get("bsky_app_password", ""))
            try:
                uri, cid = parent.get("uri") or pid, parent.get("cid", "")
                root_uri, root_cid = parent.get("root_uri", ""), parent.get("root_cid", "")
                if not cid or not root_cid:
                    # A stored post keeps only its uri; a reply needs the cid too, and the
                    # thread's root when the parent is itself a reply (a thread's last part).
                    if not await client.ensure_logged_in():
                        out["error"] = "Bluesky sign-in failed (check the app password)"
                        return out
                    data = await client._get_json(f"{_BSKY_API}/app.bsky.feed.getPosts",
                                                  params={"uris": uri})
                    posts = (data or {}).get("posts") or []
                    if not posts:
                        out["error"] = "the post wasn't found on Bluesky (deleted?)"
                        return out
                    cid = cid or posts[0].get("cid", "")
                    rr = (((posts[0].get("record") or {}).get("reply") or {}).get("root") or {})
                    root_uri = root_uri or rr.get("uri") or uri
                    root_cid = root_cid or rr.get("cid") or cid
                handles = [(m.get("handle_bsky") or "").strip() for m in mentions
                           if (m.get("handle_bsky") or "").strip()]
                r = await client.create_post(
                    body, labels=_BSKY_LABELS.get(rating) or None, mention_handles=handles or None,
                    reply={"root": {"uri": root_uri, "cid": root_cid}, "parent": {"uri": uri, "cid": cid}})
            finally:
                await client.close()
            if r and r.get("uri"):
                out.update(success=True, external_id=r["uri"], external_url=r.get("url", ""),
                           refs={"uri": r["uri"], "cid": r.get("cid", ""),
                                 "root_uri": root_uri, "root_cid": root_cid})
            else:
                out["error"] = "Bluesky rejected the comment"
        elif platform == "mast":
            from clients.mast.client import MastClient
            client = MastClient(instance_url=creds.get("mast_instance_url", ""),
                                access_token=creds.get("mast_access_token", ""))
            try:
                r = await client.create_status(body, in_reply_to_id=pid, sensitive=rating in _SENSITIVE,
                                               idempotency_key=idempotency_key)
            finally:
                await client.close()
            if r and r.get("id"):
                out.update(success=True, external_id=str(r["id"]), external_url=r.get("url", ""))
            else:
                out["error"] = "Mastodon rejected the comment"
        elif platform == "tw":
            from clients.tw.client import TWClient
            client = TWClient(auth_token=creds.get("tw_auth_token", ""), ct0=creds.get("tw_ct0", ""),
                              target_user=creds.get("tw_target_user", ""))
            try:
                r = await client.create_tweet(body, reply_to=pid)
            finally:
                await client.close()
            if r and r.get("id"):
                out.update(success=True, external_id=r["id"], external_url=r.get("url", ""))
            else:
                out["error"] = client.last_error or "X rejected the comment and gave no reason"
        elif platform == "thr":
            from clients.thr.client import ThrClient
            client = ThrClient(access_token=creds.get("thr_access_token", ""),
                               user_id=creds.get("thr_user_id", ""))
            try:
                r = await client.create_thread(body, reply_to=pid)
            finally:
                await client.close()
            if r and r.get("id"):
                out.update(success=True, external_id=r["id"], external_url=r.get("url", ""))
            else:
                out["error"] = ("Threads rejected the comment — replying needs the manage-replies "
                                "permission; reconnect the Threads account to grant it")
        elif platform == "tg":
            from clients.tg.client import TgClient
            client = TgClient(bot_token=creds.get("tg_bot_token", ""), channel=creds.get("tg_channel", ""))
            r = await client.create_post(body, reply_to=pid)
            if r and r.get("id"):
                out.update(success=True, external_id=str(r["id"]), external_url=r.get("url", ""))
            else:
                out["error"] = "Telegram rejected the comment (is the post still in the channel?)"
        elif platform == "ig":
            from clients.ig.client import IgClient
            client = IgClient(access_token=creds.get("ig_access_token", ""), user_id=creds.get("ig_user_id", ""))
            try:
                r = await client.create_comment(pid, body)
            finally:
                await client.close()
            out.update(success=True, external_id=r["id"])
    except Exception as e:
        logger.warning("Paired comment on %s failed: %s", platform, e)
        out["error"] = str(e) or type(e).__name__
        if platform == "ig" and "permission" in out["error"].lower():
            out["error"] += " — commenting needs the manage-comments permission; reconnect Instagram"
    return out


# ── Retry policy (FR-017) ─────────────────────────────────────────────────────

_COMMENT_PERMANENT = ("permission", "not found", "deleted", "never post", "has nothing to fill",
                      "isn't connected", "no replies", "id on the site is unknown", "unknown")


def is_permanent(error: str) -> bool:
    from posting.manager import _PERMANENT_ERROR_MARKERS
    e = (error or "").lower()
    if accounts_db.NEVER_POST_ERROR.lower() in e:
        return True
    return any(m in e for m in _PERMANENT_ERROR_MARKERS) or any(m in e for m in _COMMENT_PERMANENT)


def _next_try(attempts: int, error: str) -> str:
    if attempts >= len(RETRY_DELAYS) or is_permanent(error):
        return ""
    return (datetime.now(timezone.utc) + timedelta(seconds=RETRY_DELAYS[attempts])).strftime("%Y-%m-%d %H:%M:%S")


def _record(row: dict, res: dict, parent_id: str, *, automatic: bool) -> dict:
    attempts = int(row.get("attempts") or 0) + (1 if automatic else 0)
    conn = get_connection()
    try:
        if res.get("success"):
            cq.mark(conn, row["id"], "posted", now=_now(), account_id=res.get("account_id") or 0,
                    parent_external_id=parent_id, external_id=res.get("external_id", ""),
                    external_url=res.get("external_url", ""), error="", attempts=attempts, next_try_at="")
        else:
            nxt = _next_try(attempts, res.get("error", ""))
            cq.mark(conn, row["id"], "failed", now=_now(), account_id=res.get("account_id") or 0,
                    parent_external_id=parent_id, error=res.get("error", "") or "Failed",
                    attempts=attempts, next_try_at=nxt)
        return cq.get(conn, row["id"])
    finally:
        conn.close()


def _skip(row: dict, reason: str) -> dict:
    conn = get_connection()
    try:
        cq.mark(conn, row["id"], "skipped", now=_now(), error=reason, next_try_at="")
        return cq.get(conn, row["id"])
    finally:
        conn.close()


def summary(row: dict | None) -> dict | None:
    """The small dict the API and result panels carry for one comment."""
    if not row:
        return None
    return {"id": row["id"], "status": row["status"], "text": row.get("text", ""),
            "external_url": row.get("external_url", ""), "error": row.get("error", ""),
            "next_try_at": row.get("next_try_at", ""), "attempts": row.get("attempts", 0),
            "proven": row["platform"] in PROVEN}


def _mention_dicts(bindings) -> list[dict]:
    if not bindings:
        return []
    from posting.post_publisher import _mentions_for
    conn = get_connection()
    try:
        return _mentions_for(conn, bindings)
    finally:
        conn.close()


async def _send_row(row: dict, parent: dict, text: str, *, rating: str, account_id, settings,
                    automatic: bool = False) -> dict:
    """Send one stored comment and record the outcome on its own activity line."""
    from posting import activity
    key = f"{row['platform']}:comment"
    activity.add_line(key, f"{label(row['platform'])} — comment", aside=True)
    activity.step(key, "Comment")
    res = await send_reply(row["platform"], account_id, parent, text, rating=rating,
                           mentions=_mention_dicts(row.get("mentions")), settings=settings,
                           idempotency_key=f"pp-c{row['id']}-{int(row.get('attempts') or 0)}")
    rec = _record(row, res, str(parent.get("id") or parent.get("uri") or ""), automatic=automatic)
    err = res.get("error", "")
    if not res.get("success") and rec.get("next_try_at"):
        err += " — PawPoller will try again by itself."
    activity.line_done(key, bool(res.get("success")), url=res.get("external_url", ""),
                       error="" if res.get("success") else err, retryable=False)
    return rec


def _cancel_rest(rows: list[dict]) -> None:
    conn = get_connection()
    try:
        for r in rows:
            cq.mark(conn, r["id"], "cancelled", now=_now(), error="not sent — cancelled")
    finally:
        conn.close()


# ── The comment pass: posts ───────────────────────────────────────────────────

async def comment_pass_posts(post: dict, results: list[dict], settings: dict | None = None) -> None:
    """After a composed post's sites (and thread parts) are done: send each site's
    pending comment under the post — or under the last thread part, so it reads as the
    end of the thread. Attaches ``res["comment"]``; never touches the post's own result."""
    from posting import activity
    post_id = post.get("post_id")
    conn = get_connection()
    try:
        rows = {r["platform"]: r for r in cq.rows_for_owner(conn, "post", post_id)}
    finally:
        conn.close()
    if not rows:
        return
    todo = [(res, rows[res["platform"]]) for res in results
            if res.get("platform") in rows and res.get("success")
            and rows[res["platform"]]["status"] == "pending"]
    for i, (res, row) in enumerate(todo):
        if activity.cancelled():
            _cancel_rest([r for _, r in todo[i:]])
            break
        refs = res.get("_refs") or {}
        if res.get("_last_part"):
            # Under the thread's last part; Bluesky's root stays part 1.
            parent = {"id": res["_last_part"], "uri": res["_last_part"],
                      "root_uri": refs.get("uri", ""), "root_cid": refs.get("cid", "")}
        elif refs:
            parent = {"id": res.get("external_id", ""), "uri": refs.get("uri", ""), "cid": refs.get("cid", ""),
                      "root_uri": refs.get("uri", ""), "root_cid": refs.get("cid", "")}
        else:
            parent = {"id": res.get("external_id", "")}
        rec = await _send_row(row, parent, row["text"], rating=post.get("rating") or "general",
                              account_id=res.get("account_id"), settings=settings)
        res["comment"] = summary(rec)
    for res in results:
        if "comment" not in res and res.get("platform") in rows:
            res["comment"] = summary(rows[res["platform"]])


# ── The comment pass: pieces ──────────────────────────────────────────────────

def piece_context(kind: str, name: str, platform: str, chapter: int = 0,
                  account_id: int | None = None) -> dict:
    """What a piece's placeholders fill from: its live links, its per-site link mode,
    title and artist. Never raises — an empty context just leaves tokens empty."""
    ctx: dict = {"extra": {}, "title": "", "artist": None}
    try:
        if kind == "artwork":
            from posting import artwork_reader
            art = artwork_reader.load_artwork(name)
            ctx["title"] = (art.titles_by_platform or {}).get(platform) or art.title
            opts = (art.categories_by_platform or {}).get(platform) or {}
            ctx["extra"] = {**{k: opts[k] for k in ("link_mode", "link_platforms") if k in opts},
                            **artwork_reader._artwork_links(name, exclude=platform)}
            try:
                artist, self_row, _ = artwork_reader._people_context(art, account_id)
                ctx["artist"] = None if self_row is not None else artist
            except Exception:
                pass
            ctx["rating"] = art.rating
        elif kind == "story":
            from posting import story_reader
            story = story_reader.load_story(name)
            ctx["title"] = getattr(story, "title", "") or name.replace("_", " ")
            ctx["extra"] = story_reader._story_extra(story, platform, chapter)
            ctx["rating"] = getattr(story, "rating", "general")
    except Exception as e:
        logger.warning("Comment context for %s %s: %s", kind, name, e)
    return ctx


async def comment_pass_pieces(kind: str, name: str, results: list[dict],
                              settings: dict | None = None) -> None:
    """After a piece's publish loop: each reply site with a pending comment and a live
    post in this run gets its comment under the FIRST submission there (several
    renders to one site still mean one comment). ``{link}`` sees every link this run
    made, whichever order the sites went in (FR-016); a link that never appeared →
    the comment is skipped with the reason, never posted with a raw ``{link}``."""
    from posting import activity
    from posting.manager import _run_links
    conn = get_connection()
    try:
        rows = [r for r in cq.rows_for_owner(conn, kind, name) if r["status"] == "pending"]
    finally:
        conn.close()
    if not rows:
        return
    done: set[tuple[str, int]] = set()
    todo = []
    for res in results:
        plat, ch = res.get("platform"), int(res.get("chapter_index") or 0)
        if not res.get("success") or res.get("queued_desktop") or (plat, ch) in done:
            continue
        row = next((r for r in rows if r["platform"] == plat and int(r["chapter_index"]) == ch), None)
        if row:
            done.add((plat, ch))
            todo.append((res, row))
    for i, (res, row) in enumerate(todo):
        if activity.cancelled():
            _cancel_rest([r for _, r in todo[i:]])
            break
        plat, ch = row["platform"], int(row["chapter_index"])
        ctx = piece_context(kind, name, plat, ch, res.get("account_id"))
        # This run's links — a story chapter's comment sees that chapter's (and whole-story) posts.
        run = _run_links(results, ch if kind == "story" else None)
        ctx["extra"] = {**ctx["extra"], "run_links": [p for p in run if p[0] != plat]}
        text, empty = fill(row["text"], ctx, plat, settings)
        if empty:
            res["comment"] = summary(_skip(row, f"{{{empty[0]}}} has nothing to fill it — "
                                               "the link it points at doesn't exist yet"))
            continue
        res["comment"] = summary(await _send_row(
            row, {"id": res.get("external_id", "")}, text, rating=ctx.get("rating") or "general",
            account_id=res.get("account_id"), settings=settings))


# ── Writing piece comments (routes) ───────────────────────────────────────────

def store_piece_comments(kind: str, name: str, platforms, comments, *, chapters=(0,),
                         settings: dict | None = None) -> None:
    """The rule every piece route follows (tasks D2): ``comments`` present (even ``{}``)
    is exactly what is stored — a box cleared in the dialog means no comment there;
    ``comments`` absent (schedules, batch, drip, older clients) → each reply site gets
    its default template, if it has one. Validates text length and placeholders;
    raises ValueError with a plain reason."""
    plats = [p for p in (platforms or []) if p in REPLY_PLATFORMS]
    if not plats:
        return
    if chapters is None:                  # "every chapter", as post_story reads it
        chapters = [0]
        if kind == "story":
            try:
                from posting import story_reader
                n = story_reader.load_story(name).total_chapters
                chapters = list(range(1, n + 1)) if n > 0 else [0]
            except Exception:
                pass
    chapters = [int(c) for c in (chapters or [0])]
    if comments is not None and not isinstance(comments, dict):
        raise ValueError("comments must be a mapping of site → text")
    now = _now()
    conn = get_connection()
    try:
        for ch in chapters:
            for p in plats:
                if comments is None:
                    if cq.find(conn, kind, name, ch, p):
                        continue          # an explicit comment already stored wins over a default
                    text, tname = default_text(p, settings)
                    mentions = []
                else:
                    c = comments.get(p)
                    if isinstance(c, dict):
                        text, mentions, tname = str(c.get("text") or ""), c.get("mentions") or [], str(c.get("template") or "")
                    else:
                        text, mentions, tname = str(c or ""), [], ""
                    if not text.strip():
                        cq.clear_unsent(conn, kind, name, [p], chapter=ch)
                        continue
                if not text.strip():
                    continue
                if len(text) > MAX_TEMPLATE_TEXT:
                    raise ValueError(f"the {label(p)} comment is too long")
                bad = unknown_tokens(text)
                if bad:
                    raise ValueError(f"the {label(p)} comment uses {{{bad[0]}}}, which PawPoller doesn't know")
                cq.put_pending(conn, kind, name, ch, p, text, mentions=mentions, template=tname, now=now)
    finally:
        conn.close()


def store_post_comments(post_id: int, comments, *, linked: dict | None = None,
                        settings: dict | None = None) -> None:
    """A composed post's comments, filled NOW (spec US4.2: what was previewed is what
    goes out). Sites missing from ``comments`` lose any unsent comment."""
    if comments is None:
        return
    if not isinstance(comments, dict):
        raise ValueError("comments must be a mapping of site → text")
    now = _now()
    conn = get_connection()
    try:
        keep = []
        for p, c in comments.items():
            if p not in REPLY_PLATFORMS:
                continue
            if isinstance(c, dict):
                text, mentions, tname = str(c.get("text") or ""), c.get("mentions") or [], str(c.get("template") or "")
            else:
                text, mentions, tname = str(c or ""), [], ""
            if not text.strip():
                continue
            bad = unknown_tokens(text)
            if bad:
                raise ValueError(f"the {label(p)} comment uses {{{bad[0]}}}, which PawPoller doesn't know")
            ctx = piece_context(linked["kind"], str(linked["ref"]), p) if linked else None
            filled, empty = fill(text, ctx, p, settings)
            if empty:
                raise ValueError(f"the {label(p)} comment's {{{empty[0]}}} has nothing to fill it")
            if len(filled) > MAX_TEMPLATE_TEXT:
                raise ValueError(f"the {label(p)} comment is too long")
            cq.put_pending(conn, "post", post_id, 0, p, filled, mentions=mentions, template=tname, now=now)
            keep.append(p)
        stale = [p for p in REPLY_PLATFORMS if p not in keep]
        cq.clear_unsent(conn, "post", post_id, stale)
    finally:
        conn.close()


# ── Manual + automatic retry ──────────────────────────────────────────────────

def _live_parent(row: dict) -> tuple[dict | None, int | None, str]:
    """(parent, account_id, rating) of the live post this comment belongs under, or
    (None, None, reason) when that post isn't live yet."""
    conn = get_connection()
    try:
        if row["owner_kind"] == "post":
            from database import posts_queries
            pid = int(row["owner_ref"])
            post = posts_queries.get_post(conn, pid) or {}
            pubs = [p for p in posts_queries.get_post_publications(conn, pid)
                    if p["platform"] == row["platform"] and p["status"] == "posted" and p.get("external_id")]
            if not pubs:
                return None, None, f"the post isn't live on {label(row['platform'])} yet"
            parent_id = pubs[0]["external_id"]
            parts = posts_queries.get_thread_parts(conn, pid) if row["platform"] in ("bsky", "mast") else []
            for part in reversed(parts):
                pp = [p for p in posts_queries.get_post_publications(conn, part["post_id"])
                      if p["platform"] == row["platform"] and p["status"] == "posted" and p.get("external_id")]
                if pp:
                    parent_id = pp[0]["external_id"]
                    break
            return {"id": parent_id}, pubs[0]["account_id"], post.get("rating") or "general"
        from database import posting_queries
        pubs = posting_queries.get_publications(conn, story_name=row["owner_ref"], status="posted",
                                                content_type="artwork" if row["owner_kind"] == "artwork" else "story")
        pubs = [p for p in pubs if p.get("platform") == row["platform"] and p.get("external_id")
                and int(p.get("chapter_index") or 0) == int(row["chapter_index"] or 0)]
        if not pubs:
            return None, None, f"the post isn't live on {label(row['platform'])} yet"
        return {"id": pubs[0]["external_id"]}, pubs[0].get("account_id"), ""
    finally:
        conn.close()


async def resend(comment_id: int, settings: dict | None = None, *, automatic: bool = False) -> dict:
    """Send one stored comment under its live post — Retry comment, and the automatic
    retry. Only the comment: the post itself is never posted again (FR-007)."""
    conn = get_connection()
    try:
        row = cq.get(conn, comment_id)
    finally:
        conn.close()
    if not row:
        raise LookupError("no such comment")
    if row["status"] == "posted":
        return summary(row)
    parent, account_id, rating = _live_parent(row)
    if parent is None:
        raise RuntimeError(rating)
    text = row["text"]
    if row["owner_kind"] != "post":
        ctx = piece_context(row["owner_kind"], row["owner_ref"], row["platform"],
                            int(row["chapter_index"] or 0), account_id)
        rating = ctx.get("rating") or "general"
        text, empty = fill(text, ctx, row["platform"], settings)
        if empty:
            return summary(_skip(row, f"{{{empty[0]}}} has nothing to fill it"))
    return summary(await _send_row(row, parent, text, rating=rating or "general",
                                   account_id=account_id, settings=settings, automatic=automatic))


async def run_due(settings: dict | None = None, limit: int = 3) -> int:
    """The scheduler tick's share: send failed comments whose automatic retry is due.
    Each row is claimed first, so two instances on one database never both send it."""
    conn = get_connection()
    try:
        rows = cq.claim_due(conn, _now(), limit)
    finally:
        conn.close()
    sent = 0
    for row in rows:
        try:
            await resend(row["id"], settings, automatic=True)
            sent += 1
        except Exception as e:
            # Post no longer live / row gone: record it, don't schedule again.
            c = get_connection()
            try:
                cq.mark(c, row["id"], "failed", now=_now(), error=str(e), next_try_at="")
            finally:
                c.close()
    return sent
