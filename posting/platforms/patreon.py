"""Patreon poster — spec 084. General and Mature work, behind the tiers the owner picks.

Patreon has no post API; this posts the way Patreon's own web app does, through the owner's signed-in session
(``clients/pat/client.py``). The order is the whole safety story (FR-SEC-002, research R4):

1. ``refusal()`` (no network, so scheduled posts and retries meet it too): Adult or unrated work is refused
   (``effective_rating``: empty / unknown / e / s → adult); an audience is required and must be one of the saved
   choices; Mature work goes to paid tiers only; the pictures, the cover and the story files must all be resolved
   by the reader, rated no higher than the post, and the cover General while ``BLURRED_COVER_PROVED`` is false.
2. ``post()`` reads the page fresh (identity, tiers, 18+ flag), refreshes the saved values and runs the refusal
   again on them, so a tier deleted or made free since the form was filled stops it before anything is made.
3. A draft is made, the files are uploaded and ready, then body + audience + pictures + cover + collection are
   saved with ``tags.publish = false``. The audience is read back and compared with what was chosen.
   Only a match is published (the audience and the switch sent again); it is read back once more.
4. A mismatch before publishing leaves a draft (said so, with what Patreon showed). A mismatch after publishing
   says it's live with a different audience, with the link: PawPoller never unpublishes on its own.

Nothing PawPoller sends is a teaser: a locked post's body starts with the members-only marker in the shape
Patreon's own editor sends, and it publishes only when the stored free preview reads back empty (FR-SEC-010). The title, tags and cover are public on Patreon whatever the tier;
the form says so.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

import config
from clients.pat import account as pat_account
from clients.pat import editor_json
from clients.pat.client import (ALT_MAX, CAPTION_MAX, PatAuthError, PatChallenged, PatClient, PatUnavailable, parse_time,
                               patreon_time, plain_text, post_link, same_instant)
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage, effective_rating

logger = logging.getLogger(__name__)

# False until the live check (L3) proves Patreon blurs a custom cover on a locked post. Never a setting: flipped
# only by a code change that cites that check (plan cover row).
BLURRED_COVER_PROVED = False
# False until L3b proves creating a collection on the operator's own page (a collection is public at once). Same
# rule as above: flipped only by a code change that cites that check.
COLLECTION_CREATE_PROVED = False
# False until L3 shows what people who haven't paid see of a locked post whose pictures are in its text (FR-SEC-019).
INLINE_PICTURES_PROVED = False
# False until L3 proves a post Patreon publishes itself keeps its audience, free preview and paid pictures: nothing
# reads it back once it goes live (FR-SEC-020).
SCHEDULED_PUBLISH_PROVED = False

SCHEDULE_MAX_DAYS = 365
# One Patreon post at a time in the whole process (FR-080): live posts and the scheduler run on different event
# loops, so an asyncio lock wouldn't hold. Waiters poll it rather than block their loop.
_turn_lock = threading.Lock()
_turn = {"last": 0.0}
TURN_POLL_S = 1.0

IMAGE_TYPES = ("png", "jpg", "jpeg", "gif", "webp")
MAX_TIERS = 50
MAX_TAGS = 50
MAX_TAG_LEN = 25   # shortcut: PostyBirb's limit, not proven on Patreon; L3 sends a 26-character tag to check it
_RANK = {"general": 0, "mature": 1, "adult": 2}
_STORY_TEXT_MAX = 2 * 1024 * 1024

ADULT_REFUSED = "Patreon only takes General and Mature work from PawPoller. This piece is rated {r}."
NO_AUDIENCE = "Pick who can see this on Patreon (its Patreon options)."
GONE_TIER = "One of the chosen Patreon tiers: this tier no longer exists. Pick the tiers again."
MATURE_PAID = "Mature work can only go to paid tiers on Patreon."
NO_CAMPAIGN = "Sign in to Patreon again: PawPoller doesn't know which page this is."
COVER_HIGHER = "This cover is rated higher than the post. Pick a cover rated no higher than the post."
COVER_GENERAL = "Pick a General cover for Patreon: covers show to everyone, and PawPoller hasn't yet proved Patreon blurs them."
PICTURE_HIGHER = "One of the pictures is rated higher than the post, so it can't go in this Patreon post."
LEFT_DRAFT = "It's left as a draft on Patreon"
BAD_TIME = "The {what} isn't a date and time PawPoller can read. Pick it again."
PAST_TIME = "The {what} has passed or is too soon. Pick a time at least a few minutes from now."
FAR_TIME = "The {what} is more than a year away. Pick an earlier time."
EARLY_GENERAL = "Only General work can be made public later on Patreon: this post, its pictures and cover must all be General."
EARLY_LOCKED = "Making it public later needs a members-only post first. Pick tiers or all paying members."
EARLY_AFTER = "The date it goes public must be after it's published on Patreon."
TEXT_MATURE = ("Mature pictures can't go in a Patreon post's text yet: PawPoller hasn't proved Patreon keeps them from "
               "people who haven't paid. Put them in the gallery.")
SCHEDULE_MATURE = ("Mature work can't be handed to Patreon's own schedule yet, because nothing checks it once Patreon "
                   "publishes it. Clear the Patreon time; PawPoller's own schedule still works.")
DATES_ART_ONLY = ("A story chapter can't carry Patreon's own dates: every chapter sent with them would go up in the same "
                  "minute. Clear them; PawPoller's own schedule still works.")
LONG_CAPTION = f"The picture's caption is longer than {CAPTION_MAX} characters, Patreon's limit. Shorten it."
NOT_PUBLISHED = "Patreon didn't publish it."


def _rank(rating) -> int:
    return _RANK[effective_rating(rating)]


def _times(choices: dict, files: dict, rating: str, now: datetime | None = None):
    """(publish_at, public_at, refusal) from the post's choices (FR-078, FR-079, FR-SEC-018)."""
    now = now or datetime.now(timezone.utc)
    soonest = now + timedelta(minutes=2)
    out = []
    for key, what in (("publish_at", "time to publish on Patreon"), ("public_at", "date it goes public")):
        raw = str(choices.get(key) or "").strip()
        when = parse_time(raw) if raw else None
        if raw and (when is None or not _zoned(raw)):      # a time with no zone could be anyone's clock
            return None, None, BAD_TIME.format(what=what)
        out.append(when)
    pub, vis = out
    if pub is not None:
        if pub < soonest:
            return None, None, PAST_TIME.format(what="time to publish on Patreon")
        if pub > now + timedelta(days=SCHEDULE_MAX_DAYS):
            return None, None, FAR_TIME.format(what="time to publish on Patreon")
    if vis is not None:
        if effective_rating(rating) != "general" or any(effective_rating(r) != "general" for _, r in
                                                        (files.get("pictures") or [])) or \
                (files.get("cover") and effective_rating(files["cover"][1]) != "general"):
            return None, None, EARLY_GENERAL
        if str(choices.get("audience") or "").strip() == "public":
            return None, None, EARLY_LOCKED
        start = pub or soonest
        if vis <= start:
            return None, None, EARLY_AFTER if pub else PAST_TIME.format(what="date it goes public")
        if vis > start + timedelta(days=SCHEDULE_MAX_DAYS):
            return None, None, FAR_TIME.format(what="date it goes public")
    return pub, vis, None


def _misses(st: dict, chosen, rules, locked: bool, pictured: bool) -> list[str]:
    """What a read-back shows that wasn't asked for: audience, free preview, paid pictures (FR-SEC-002, FR-SEC-020)."""
    out = []
    if not audience_matches(chosen, st["rule_ids"], rules):
        out.append(f"Patreon shows it to {describe(st['rule_ids'], rules)}, not the tiers picked")
    if locked and not editor_json.is_empty_doc(st.get("teaser_json")):
        out.append("Patreon kept part of it as a free preview")
    if locked and pictured and st.get("media_free") is not False:
        out.append("its pictures aren't marked members only")
    return out


def _zoned(raw: str) -> bool:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False




def chosen_rules(choices: dict, rules: list[dict], rating: str) -> tuple[list[str], str | None]:
    """The access-rule ids the piece's choices point at in ``rules`` (the saved or fresh list), or a refusal."""
    audience = str(choices.get("audience") or "").strip()
    mature = effective_rating(rating) == "mature"
    by_kind = {r["kind"]: r for r in rules if r.get("kind") in ("public", "patrons")}
    if audience in ("public", "patrons"):
        if mature:
            return [], MATURE_PAID
        rule = by_kind.get(audience)
        return ([rule["rule_id"]], None) if rule else ([], GONE_TIER)
    if audience != "tiers":
        return [], NO_AUDIENCE
    ids = choices.get("tier_ids")
    if not isinstance(ids, list) or not ids:
        return [], NO_AUDIENCE
    if len(ids) > MAX_TIERS:
        return [], f"Pick up to {MAX_TIERS} Patreon tiers."
    tiers = {r["tier_id"]: r for r in rules if r.get("kind") == "tier"}
    out = []
    for t in ids:
        rule = tiers.get(str(t))
        if rule is None:
            return [], GONE_TIER
        if mature and int(rule.get("amount_cents") or 0) <= 0:
            return [], MATURE_PAID
        out.append(rule["rule_id"])
    return out, None


def all_paid_tier_ids(rules: list[dict]) -> list[str]:
    """The "all paid tiers" shortcut: every tier with a price."""
    return [r["tier_id"] for r in rules if r.get("kind") == "tier" and int(r.get("amount_cents") or 0) > 0]


def audience_matches(chosen: list[str], seen: list[str], rules: list[dict]) -> bool:
    """Patreon's read-back against the choice (research R4): every chosen rule is there, and anything extra is a
    tier priced above every chosen tier. An extra ``public`` / ``patrons`` / free or cheaper tier, or any id not in
    the fresh allow-listed list (``non_member``, ``min_cents_pledged``, an unknown kind), is a mismatch."""
    by_id = {r["rule_id"]: r for r in rules}
    if not chosen or any(c not in seen for c in chosen):
        return False
    chosen_prices = [int(by_id[c].get("amount_cents") or 0) for c in chosen
                     if c in by_id and by_id[c].get("kind") == "tier"]
    for s in seen:
        if s in chosen:
            continue
        r = by_id.get(s)
        if r is None or r.get("kind") != "tier" or not chosen_prices:
            return False
        if int(r.get("amount_cents") or 0) <= max(chosen_prices):
            return False
    return True


def describe(seen: list[str], rules: list[dict]) -> str:
    by_id = {r["rule_id"]: r for r in rules}
    names = [by_id[s]["title"] if s in by_id else "an audience PawPoller doesn't offer" for s in seen]
    return ", ".join(names) or "nobody"


def choices_for(package: StoryUploadPackage, vals: dict) -> dict:
    """The piece's own Patreon choices; with no audience of its own, the account's defaults fill in who sees it
    and the switch (FR-012). The rating rules apply to the result either way (FR-SEC-003)."""
    extra = dict(package.extra or {})
    if not str(extra.get("audience") or "").strip():
        d = vals.get("default_audience") or {}
        if d.get("audience"):
            extra["audience"], extra["tier_ids"] = d["audience"], list(d.get("tier_ids") or [])
            if "notify" not in extra and "notify" in d:
                extra["notify"] = d["notify"]
    return extra


class PatreonPoster(PlatformPoster):
    platform_id = "pat"
    platform_name = "Patreon"
    supports_edit = False
    supports_artwork_edit = False
    supports_file_replace = False
    min_post_interval = 90     # FR-080: batches wait this long; post() keeps any two Patreon posts this far apart
    max_file_size = 200 * 1024 * 1024
    accepted_file_types = list(IMAGE_TYPES)
    accepted_media = {"image": list(IMAGE_TYPES)}
    max_rating = "mature"
    requires_mode = "any"
    # Stand-in transports for tests; None = the real network.
    _transport = None
    _upload_transport = None
    _sleep = staticmethod(asyncio.sleep)

    # ── before the network ───────────────────────────────────────────────

    def _values(self) -> dict:
        self._resolve_creds("pat", config.get_settings())       # pins self.account_id to the default if unset
        return pat_account.values(self._aid())

    def _aid(self) -> int:
        """The account id ``_resolve_creds`` pinned (it makes the default when none was given)."""
        if self.account_id is None:
            self._resolve_creds("pat", config.get_settings())
        if self.account_id is None:
            raise PatUnavailable("There's no Patreon account to post with.")
        return self.account_id

    def refusal(self, package: StoryUploadPackage, vals: dict | None = None) -> str | None:
        import age_gate
        rating = effective_rating(package.rating)
        vals = vals if vals is not None else self._values()
        if age_gate.is_under_18() and vals.get("is_nsfw"):
            return "This Patreon page is marked 18+, so it's locked on this copy of PawPoller."
        gate = age_gate.refusal("pat", rating, site_name="Patreon") or self.media_refusal(package)
        if gate:
            return gate
        if rating == "adult":
            return ADULT_REFUSED.format(r=(package.rating or "not rated"))
        if not vals.get("campaign_id"):
            return NO_CAMPAIGN
        _, why = chosen_rules(choices_for(package, vals), vals.get("tiers") or [], rating)
        if why:
            return why
        files = package.pat_files or {}
        if files.get("problems"):
            return files["problems"][0]
        if any(_rank(r) > _rank(rating) for _, r in files.get("pictures") or []):
            return PICTURE_HIGHER
        cover = files.get("cover")
        if cover:
            if _rank(cover[1]) > _rank(rating):
                return COVER_HIGHER
            if not BLURRED_COVER_PROVED and effective_rating(cover[1]) != "general":
                return COVER_GENERAL
        choices = choices_for(package, vals)
        if len(str(choices.get("caption") or "")) > CAPTION_MAX:
            return LONG_CAPTION
        if rating != "general" and choices.get("picture_place") == "text" and files.get("pictures") \
                and not INLINE_PICTURES_PROVED:
            return TEXT_MATURE
        if rating != "general" and str(choices.get("publish_at") or "").strip() and not SCHEDULED_PUBLISH_PROVED:
            return SCHEDULE_MATURE
        if not package.media_kind and (str(choices.get("publish_at") or "").strip()
                                       or str(choices.get("public_at") or "").strip()):
            return DATES_ART_ONLY       # T156: a story's options are shared by every chapter
        return _times(choices, files, rating)[2]

    def validate(self, package: StoryUploadPackage) -> list[str]:
        # A Patreon post needs a title, never a tag; a story chapter has no picture.
        errs = [e for e in super().validate(package) if e != "At least one tag is required"]
        if not package.media_kind:     # a chapter: its body is its built file, never the blurb (lean review)
            errs = [e for e in errs if not e.startswith(("File not found", "File too large"))]
            fp = package.file_path or ""
            if not fp or not os.path.isfile(fp):
                errs.append("This chapter can't go to Patreon: its file wasn't found. Build the story's formats first.")
            elif os.path.getsize(fp) > _STORY_TEXT_MAX:
                errs.append("This chapter is too long for one Patreon post. Split it into shorter chapters first.")
        return errs

    # ── posting ──────────────────────────────────────────────────────────

    def _paths_ok(self, files: dict) -> bool:
        """Every resolved path stays inside the piece's own folder (plan path row), checked again here."""
        from posting.artwork_reader import inside
        root = files.get("piece_dir") or ""
        paths = [p for p, _ in files.get("pictures") or []] + list(files.get("files") or [])
        if files.get("cover"):
            paths.append(files["cover"][0])
        return bool(root) and all(inside(root, p) is not None and os.path.isfile(p) for p in paths)

    def _body_html(self, package: StoryUploadPackage) -> str:
        if not package.media_kind and package.file_path and os.path.isfile(package.file_path):
            with open(package.file_path, encoding="utf-8", errors="replace") as fh:
                return fh.read(_STORY_TEXT_MAX)
        return package.description or ""

    async def _save(self, c: PatClient, draft: str, publish: bool, **kw) -> None:
        """One save with the edited_at read just before it; a POST_CONTENT_OUTDATED re-reads and retries once."""
        for attempt in (0, 1):
            st = await c.read_post(draft)
            try:
                await c.save_post(draft, edited_at=st["edited_at"], publish=publish, **kw)
                return
            except PatUnavailable as e:
                if attempt or e.code != "POST_CONTENT_OUTDATED":
                    raise

    async def post(self, package: StoryUploadPackage) -> PostResult:
        """One Patreon post at a time in the process, ``min_post_interval`` after the last (FR-080). A refused post
        never waits."""
        why = self.refusal(package)
        if why:
            return PostResult(success=False, error=why)
        # Polled, not an asyncio.Event: an Event belongs to one event loop, and Patreon posts come from several.
        while not _turn_lock.acquire(blocking=False):  # noqa: ASYNC110
            await asyncio.sleep(TURN_POLL_S)
        try:
            gap = self.min_post_interval - (time.monotonic() - _turn["last"])
            if _turn["last"] and gap > 0:
                await self._sleep(gap)
            return await self._post_now(package)
        finally:
            _turn["last"] = time.monotonic()
            _turn_lock.release()

    async def _post_now(self, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        fail = lambda msg, **k: PostResult(success=False, error=msg, duration_seconds=self._elapsed(_t), **k)  # noqa: E731
        vals = self._values()
        refusal = self.refusal(package, vals)
        if refusal:
            return fail(refusal)
        from polling import fa_standdown
        if fa_standdown.remaining("pat") > 0:
            from clients.pat.client import PAUSED
            return fail(PAUSED.format(when=fa_standdown.next_try_text('pat')))
        files = package.pat_files or {}
        if not self._paths_ok(files):
            return fail("A file for this Patreon post isn't inside the piece's own folder, so it wasn't sent.")
        creds = self._resolve_creds("pat", config.get_settings())
        cookie = (creds.get("pat_cookie") or "").strip()
        if not cookie:
            return fail("Patreon isn't connected (Accounts → Patreon → Sign in).")
        choices = choices_for(package, vals)
        rating = effective_rating(package.rating)
        draft = None
        async with PatClient(cookie, transport=self._transport, upload_transport=self._upload_transport,
                             sleep=self._sleep) as c:
            try:
                camp = await c.current_campaign()
                if camp["campaign_id"] != vals["campaign_id"]:
                    return fail(f"This Patreon sign-in is for a different page ({camp['vanity'] or camp['campaign_id']}) "
                                f"than the one saved ({vals['campaign_id']}). Sign in to the right page again.")
                rules = await c.audience_rules(camp["campaign_id"])
                pat_account.record_campaign(self._aid(), camp, rules)
                fresh = {**vals, "tiers": rules, "is_nsfw": camp["is_nsfw"]}
                refusal = self.refusal(package, fresh)
                if refusal:
                    return fail(refusal)
                chosen, _ = chosen_rules(choices, rules, rating)
                collection_ids, note, make = await self._collection(c, camp["campaign_id"], choices, package, vals)
                if collection_ids is None:
                    return fail(note)
                publish_at, public_at, late = _times(choices, files, rating)
                if late:
                    return fail(late)

                pictures = files.get("pictures") or []
                up = files.get("upload") or {}      # the cleaned (and watermarked) copies, set by the posting run
                in_text = choices.get("picture_place") == "text" and bool(pictures)
                # shortcut: no alt text on Mature pictures until L3 shows a signed-out visitor can't read it; then
                # send it for both ratings.
                alt = plain_text((package.extra or {}).get("alt_text") or "", ALT_MAX) if rating == "general" else ""
                caption = plain_text(choices.get("caption") or "", CAPTION_MAX)
                draft = await c.create_draft("image_file" if pictures and not in_text else "text_only")
                picture_ids, lead = [], []
                for i, (p, _) in enumerate(pictures):
                    cap = caption if i == 0 else ""
                    if in_text:     # FR-076: below the members-only marker, in the text; nothing in the gallery
                        mid = await c.create_media(draft, up.get(p, p), "inline", "image")
                        lead.append(editor_json.picture_node(await c.picture_address(mid), mid, draft, cap, alt))
                    else:
                        mid = await c.create_media(draft, up.get(p, p), "main", "image")
                        picture_ids.append(mid)
                    if cap or alt:      # FR-077: on the picture itself; Patreon rewrites an in-text node (research)
                        await c.set_media_text(mid, cap, alt)
                cover_id = ""
                if files.get("cover"):
                    cover_id = await c.create_media(draft, up.get(files["cover"][0], files["cover"][0]),
                                                    "custom-thumbnail", "image")
                for f in files.get("files") or []:
                    await c.create_media(draft, f, "attachment", "file")
                if make:        # only now: a lost answer here leaves a draft (no retry), never a second collection
                    collection_ids, note = await self._make_collection(c, camp["campaign_id"], package, vals, make)

                locked = str(choices.get("audience") or "").strip() != "public"
                # Patreon's editor adds this marker in the browser; drafts made here have none (operator, 13 Nov).
                paywall = editor_json.paywall_node(camp["currency"], draft) if locked else None
                attrs = {"title": package.title or "", "post_type": "image_file" if picture_ids else "text_only",
                         "content_json_string": editor_json.to_editor_json(self._body_html(package), paywall=paywall,
                                                                           lead=lead),
                         "teaser_text": "", "is_paid": False}
                if locked and picture_ids:
                    attrs["is_header_media_free"] = False      # the editor's "Paid image" switch, said outright
                if public_at:
                    attrs["change_visibility_at"] = patreon_time(public_at)     # FR-079, read back before publishing
                if rating == "mature":
                    attrs["allow_preview_in_rss"] = False
                if cover_id:
                    cover_general = effective_rating(files["cover"][1]) == "general"
                    attrs["is_preview_blurred"] = not (cover_general and not camp["is_nsfw"])
                tags = [t for t in package.tags or [] if t]
                left_out = [{"tag": t, "rule": f"longer than {MAX_TAG_LEN} characters"} for t in tags if len(t) > MAX_TAG_LEN]
                kw = {"attributes": attrs, "rule_ids": chosen,
                      "tags": [t for t in tags if len(t) <= MAX_TAG_LEN][:MAX_TAGS],
                      "image_order": picture_ids, "cover_media_id": cover_id, "collection_ids": collection_ids,
                      "notify": choices.get("notify", True) is not False}
                await self._save(c, draft, False, **kw)
                back = await c.read_post(draft)
                seen = back["rule_ids"]
                if locked and not editor_json.is_empty_doc(back["teaser_json"]):
                    return fail(f"{LEFT_DRAFT}: Patreon kept part of it as a free preview, so it wasn't published.",
                                extra={"draft_id": draft})
                if locked and picture_ids and back.get("media_free") is not False:
                    return fail(f"{LEFT_DRAFT}: Patreon didn't keep its pictures for members only, so it wasn't "
                                "published.", extra={"draft_id": draft})
                if public_at and not same_instant(back.get("public_at"), public_at):
                    return fail(f"{LEFT_DRAFT}: Patreon didn't keep the date it goes public, so it wasn't published.",
                                extra={"draft_id": draft})
                if not audience_matches(chosen, seen, rules):
                    return fail(f"{LEFT_DRAFT}: Patreon showed it to {describe(seen, rules)}, not the tiers picked.",
                                extra={"draft_id": draft})
                _, _, late = _times(choices, files, rating)      # uploads take minutes: still in the future?
                if late:
                    return fail(f"{LEFT_DRAFT}: {late}", extra={"draft_id": draft})
                why = ""
                if publish_at:      # FR-078: Patreon publishes it itself; set only after every check above passed
                    kw["attributes"] = {**attrs, "scheduled_for": patreon_time(publish_at)}
                try:
                    await self._save(c, draft, not publish_at, **kw)
                except (PatUnavailable, httpx.HTTPError) as e:     # refused, or the answer lost: read back first
                    logger.warning("Patreon publish answer: %s", type(e).__name__)
                    why = str(e) if isinstance(e, PatUnavailable) else ""
                if publish_at:
                    res = await self._scheduled(c, draft, chosen, rules, _t, note, why, publish_at, locked,
                                                bool(picture_ids))
                else:
                    res = await self._published(c, draft, chosen, rules, _t, note, why, locked, bool(picture_ids))
                if left_out:
                    res.extra["tags_left_out"] = left_out
                return res
            except PatChallenged as e:
                return fail(str(e) + (f" {LEFT_DRAFT}." if draft else ""), extra={"draft_id": draft} if draft else {})
            except PatAuthError as e:
                return fail(str(e))
            except Exception as e:  # noqa: BLE001 — a step's own reason, said with where it stopped
                logger.error("Patreon post failed: %s", type(e).__name__)
                msg = str(e)[:300] if isinstance(e, (PatUnavailable, ValueError)) else "Patreon didn't answer as expected."
                return fail(f"{msg} {LEFT_DRAFT}." if draft else msg, extra={"draft_id": draft} if draft else {})

    async def _scheduled(self, c: PatClient, draft: str, chosen, rules, _t, note: str, why: str, when,
                         locked: bool, pictured: bool) -> PostResult:
        """Read the post back after handing Patreon the time to publish it (FR-078, FR-SEC-020). The save sent the
        whole post again, so this re-checks everything. Held by Patreon → success (never retried), with a warning
        when anything differs; no time kept → a draft."""
        link = post_link(draft)
        done = lambda **k: PostResult(success=True, external_id=draft, external_url=link,  # noqa: E731
                                      duration_seconds=self._elapsed(_t), **k)
        try:
            st = await c.read_post(draft)
        except Exception:  # noqa: BLE001 — the answer lost: unconfirmed, never a second post
            return PostResult(success=False, unconfirmed=True, external_id=draft, external_url=link,
                              duration_seconds=self._elapsed(_t),
                              error="PawPoller sent the time to publish but couldn't read the post back. Check Patreon "
                                    "before posting again.")
        wrong = _misses(st, chosen, rules, locked, pictured)
        if st.get("published_at"):
            return done(needs_attention="Patreon published it straight away instead of at the time set"
                                        + (f", and {'; '.join(wrong)}" if wrong else "") + ". Check the post on Patreon.")
        held = parse_time(st.get("scheduled_for"))
        if held is None:
            return PostResult(success=False, external_id=draft, duration_seconds=self._elapsed(_t),
                              error=f"{why or 'Patreon did not keep the time to publish it.'} {LEFT_DRAFT}.",
                              extra={"draft_id": draft})
        try:
            local = held.astimezone(config.display_zone()).strftime("%d %b %Y, %H:%M")
        except (OverflowError, ValueError):     # a time at the calendar's edge: say it in UTC
            local = patreon_time(held) + " UTC"
        if not same_instant(held, when):
            wrong.insert(0, "that isn't the time picked")
        if wrong:
            return done(needs_attention=f"Patreon will publish it at {local}, but {'; '.join(wrong)}. Change it on "
                                        "Patreon before then.")
        msg = f"Patreon will publish it at {local}. Change or cancel it on Patreon."
        return done(extra={"scheduled_for": patreon_time(held), "note": f"{note} {msg}".strip()})

    async def _published(self, c: PatClient, draft: str, chosen, rules, _t, note: str, why: str = "",
                         locked: bool = True, pictured: bool = True) -> PostResult:
        """Read the post back after publishing. Published and matching → success; published with another audience
        → said so with the link; not readable → unconfirmed (never a second draft)."""
        link = post_link(draft)
        try:
            st = await c.read_post(draft)
        except Exception:  # noqa: BLE001
            return PostResult(success=False, unconfirmed=True, external_id=draft, external_url=link,
                              duration_seconds=self._elapsed(_t),
                              error="PawPoller sent the publish but couldn't read the post back. Check Patreon before "
                                    "posting again.")
        if not st.get("published_at"):
            return PostResult(success=False, external_id=draft, duration_seconds=self._elapsed(_t),
                              error=f"{why or NOT_PUBLISHED} {LEFT_DRAFT}.", extra={"draft_id": draft})
        wrong = _misses(st, chosen, rules, locked, pictured)
        if wrong:   # live is live: success with a warning, so the manager never retries it into a second post
            return PostResult(success=True, external_id=draft, external_url=link, duration_seconds=self._elapsed(_t),
                              needs_attention=f"It's live on Patreon, but {'; '.join(wrong)}. Change it on Patreon.")
        return PostResult(success=True, external_id=draft, external_url=link, duration_seconds=self._elapsed(_t),
                          extra={"note": note} if note else {})

    async def _collection(self, c: PatClient, campaign_id: str, choices: dict, package,
                          vals) -> tuple[list | None, str, str]:
        """(ids to send, note, title of a collection to make once the media are ready), or (None, refusal, "").
        A chosen id must be in the list read now."""
        want = str(choices.get("collection") or "none").strip()
        if want in ("", "none"):
            return [], "", ""
        if want == "story":
            saved = str((vals.get("story_collections") or {}).get(package.story_name) or "")
            if saved and saved in {x["id"] for x in await c.collections(campaign_id)}:
                return [saved], "", ""
            if not COLLECTION_CREATE_PROVED:
                return [], "Posted without a collection: PawPoller can't make a story's Patreon collection yet.", ""
            return [], "", plain_text((package.story_name or "").replace("_", " ") or package.title or "", 200)
        if want not in {x["id"] for x in await c.collections(campaign_id)}:
            return None, "The chosen Patreon collection: this collection no longer exists. Pick another.", ""
        return [want], "", ""

    async def _make_collection(self, c: PatClient, campaign_id: str, package, vals, title: str) -> tuple[list, str]:
        """The story's collection (FR-071). One Patreon post at a time (FR-080), so two chapters never both make it;
        one already titled the same is used, so a retry after a lost answer doesn't make a second public one."""
        same = [x["id"] for x in await c.collections(campaign_id) if x["title"] == title]
        new_id = same[0] if same else await c.create_collection(campaign_id, title)
        if not same and new_id not in {x["id"] for x in await c.collections(campaign_id)}:
            return [], "Posted without a collection: Patreon didn't show the new collection."
        pat_account.save(self._aid(), story_collections={
            **(vals.get("story_collections") or {}), package.story_name: new_id})
        return [new_id], ""

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        return PostResult(success=False, error="PawPoller can't edit Patreon posts yet. Edit it on Patreon.")

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        return PostResult(success=False, error="PawPoller can't replace a Patreon post's file.")
