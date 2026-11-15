"""Importing a creator's existing Patreon picture posts (spec 087).

``audience()`` turns a post's audience rules into one of the three audiences PawPoller offers, or nothing. It never
widens: an audience it can't reproduce exactly is unknown, listed as "Members", and saves no choice on the piece.
The request side (``resolve``) asks Patreon for one post's pictures at the moment it is imported.
"""
from __future__ import annotations

# Fixed sentences: an import failure never carries an address or a library's own error text (FR-SEC-003).
SIGN_IN = "Patreon needs signing in again."
POST_GONE = "That post is no longer on Patreon."
OTHER_PAGE = "That post isn't on this Patreon page."
NO_PICTURES = "That post has no pictures to bring in."
PICTURE_FAILED = "A picture couldn't be downloaded."
PICTURE_TOO_LARGE = "A picture was larger than the Library takes."
NOT_A_PICTURE = "That file isn't a picture."
BUSY = "Patreon is being read right now. Try again in a minute."
PICK_NOTE = "This post's audience on Patreon couldn't be matched to one PawPoller offers."

LABEL_OTHER = "Members"
_LABELS = {"public": "Everyone", "patrons": "All paying members"}


def audience(rules, tier_ids) -> tuple[str, list[str]] | None:
    """("public" | "patrons" | "tiers", tier ids) when the post's rules are exactly one of the three audiences
    PawPoller offers and every tier named is on *tier_ids* (the page's tiers as last read); else None."""
    rules = [r for r in rules or [] if isinstance(r, dict)]
    kinds = {str(r.get("kind") or "") for r in rules}
    if not rules or len(kinds) != 1:
        return None
    kind = kinds.pop()
    if kind == "public":
        return "public", []
    if kind == "patrons":
        return "patrons", []
    if kind == "tier":
        known = {str(t) for t in tier_ids or []}
        ids = list(dict.fromkeys(str(r.get("tier_id") or "") for r in rules))
        if ids and all(i and i in known for i in ids):
            return "tiers", ids
    return None


def stored(rules, tier_ids) -> dict:
    """What the read keeps for one post: {audience, tier_ids}. Unknown is "other", never public."""
    got = audience(rules, tier_ids)
    return {"audience": got[0], "tier_ids": got[1]} if got else {"audience": "other", "tier_ids": []}


def label(aud: str, ids, tiers) -> str:
    """Who a post is for, in words, for the found list. *tiers* is the page's saved tier list
    ([{tier_id, title}]). Anything unknown is "Members"."""
    if aud in _LABELS:
        return _LABELS[aud]
    if aud == "tiers":
        names = {str(t.get("tier_id")): str(t.get("title") or "") for t in tiers or [] if isinstance(t, dict)}
        got = [names[i] for i in (str(x) for x in ids or []) if names.get(i)]
        if got and len(got) == len(list(ids or [])):
            return ", ".join(got)[:200]
    return LABEL_OTHER


# ── asking Patreon for one post's pictures, at the moment it is imported ───────────────────────────────────────

RUN_CAP = 200                   # Patreon posts in one "Import all" run
POST_BYTES = 200 * 1024 * 1024  # one post's pictures held in memory at once (a seat shares its box)
POST_TOO_LARGE = "That post's pictures are larger than PawPoller brings in at once."
LOCK_WAIT = 30.0                # how long an import waits for a read to finish before saying BUSY
UNREADABLE = "Patreon couldn't be read just now. Try again later."
_last = 0.0


class PatImportError(ValueError):
    """An import that couldn't happen, as a fixed sentence. ``stop``: Patreon turned PawPoller away (bot check,
    rate limit, signed out), so the rest of a run's Patreon posts are not tried."""

    def __init__(self, sentence: str, stop: bool = False):
        super().__init__(sentence)
        self.stop = stop


async def _sleep(seconds: float) -> None:      # its own name so tests can stand in for it
    import asyncio
    await asyncio.sleep(seconds)


async def _before_request() -> None:
    """The stand-down, then the pace: called before EVERY request an import makes, picture downloads included.
    One clock for the process, so a run of many posts keeps Patreon's one-request-a-second pace throughout."""
    import time

    from clients.pat import client as patc
    from polling import fa_standdown
    global _last
    if fa_standdown.remaining("pat") > 0:
        raise PatImportError(patc.PAUSED.format(when=fa_standdown.next_try_text("pat")), stop=True)
    wait = _last + patc.MIN_GAP_S - time.monotonic()
    if wait > 0:
        await _sleep(wait)
    _last = time.monotonic()


def _stand_down() -> PatImportError:
    from clients.pat import client as patc
    from polling import fa_standdown
    fa_standdown.note_challenge("pat")
    return PatImportError(patc.PAUSED.format(when=fa_standdown.next_try_text("pat")), stop=True)


def _forget(post_id: str) -> None:
    """A post Patreon says is gone: hide it from the found list through the existing Ignore list (reversible from
    the Ignored view). Its stored numbers stay, and nothing is deleted."""
    from database import ignored_queries
    from database.db import get_connection
    conn = get_connection()
    try:
        ignored_queries.add_ignored(conn, "pat", post_id)
        conn.commit()
    finally:
        conn.close()


async def resolve(row: dict) -> dict:
    """{title, text, posted_at, public, audience, pictures} for one stored Patreon post. ``pictures`` is a list of
    (bytes, ext) or a fixed sentence per picture; ``audience`` is ``audience()``'s answer.

    Uses the sign-in of the account the row was read under, confirms it is for the saved page as the read does,
    and refuses a post Patreon says belongs to another page. Never runs beside a read: it takes the poller's own
    lock (waiting up to LOCK_WAIT) and the site's turn. Read requests only."""
    import config
    from clients.pat import account as pat_account
    from clients.pat import pictures
    from clients.pat.client import PatAuthError, PatChallenged, PatClient, PatUnavailable
    from database import accounts as accounts_db
    from database.db import get_connection
    from polling import pat_poller
    from polling.site_turns import site_turn

    aid = int(row.get("account_id") or 0)
    vals = pat_account.values(aid)
    if not vals.get("campaign_id"):
        raise PatImportError(SIGN_IN, stop=True)
    conn = get_connection()
    try:
        acct = accounts_db.get_account(conn, aid)
    finally:
        conn.close()
    is_default = bool(acct["is_default"]) if acct else True
    if not pat_poller._lock.acquire(timeout=LOCK_WAIT):
        raise PatImportError(BUSY, stop=True)
    try:
        @site_turn("pat")
        async def run_pat_import():
            creds = config.resolve_account_credentials("pat", aid, is_default, config.get_settings())
            async with PatClient(creds.get("pat_cookie") or "", transport=pat_poller._transport) as client:
                await _before_request()
                camp = await client.current_campaign()
                if camp["campaign_id"] != vals["campaign_id"]:
                    raise PatImportError("This Patreon sign-in is for a different page than the saved one. "
                                         "Sign in to the right page again.", stop=True)
                await _before_request()
                post = await client.post_pictures(str(row.get("submission_id") or ""))
                if post["page_id"] != vals["campaign_id"]:      # Patreon's own answer, never a filter we sent
                    raise PatImportError(OTHER_PAGE)
                held = 0

                async def fetch_one(url):
                    """(bytes, ext), or a fixed sentence."""
                    nonlocal held
                    if not url:                             # listed by Patreon with no address
                        return PICTURE_FAILED
                    await _before_request()
                    try:
                        got = pictures.fetch(url)
                    except pictures.PictureError as e:
                        if e.blocked:
                            raise _stand_down() from None
                        return str(e)
                    held += len(got[0])
                    if held > POST_BYTES:                   # nothing is made: the post stays in the found list
                        raise PatImportError(POST_TOO_LARGE)
                    return got
                out = [await fetch_one(u) for u in post["pictures"]]
                if PICTURE_FAILED in out:
                    # A picture address has a time limit, and a download can simply drop: ask Patreon for the post
                    # once more and try just the pictures that didn't come (spec edge case). Then a plain failure.
                    await _before_request()
                    again = await client.post_pictures(str(row.get("submission_id") or ""))
                    if again["page_id"] == vals["campaign_id"] and len(again["pictures"]) == len(out):
                        for i, u in enumerate(again["pictures"]):
                            if out[i] == PICTURE_FAILED:
                                out[i] = await fetch_one(u)
            tier_ids = {str(t.get("tier_id")) for t in vals.get("tiers") or [] if t.get("tier_id")}
            return {"title": post["title"], "text": post["text"], "posted_at": post["posted_at"],
                    "public": post["public"], "audience": audience(post["rules"], tier_ids), "pictures": out}
        try:
            return await run_pat_import()
        except PatImportError:
            raise
        except PatChallenged:
            raise _stand_down() from None
        except PatAuthError:
            raise PatImportError(SIGN_IN, stop=True) from None
        except PatUnavailable as e:
            if "HTTP 404" in str(e) or "HTTP 410" in str(e):
                _forget(str(row.get("submission_id") or ""))    # gone from Patreon: it leaves the found list
                raise PatImportError(POST_GONE) from None
            raise PatImportError(UNREADABLE) from None
        except Exception as e:
            # A site the operator paused, or a turn that never came: no point trying the run's other posts.
            raise PatImportError(UNREADABLE, stop=type(e).__name__ in ("SitePaused", "TurnTimeout", "TimeoutError")) from None
    finally:
        pat_poller._lock.release()
