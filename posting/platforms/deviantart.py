"""DeviantArt platform poster via official OAuth2 API.

Uses the DeviantArt OAuth2 literature endpoints (not the undocumented
_napi/_puppy endpoints). This is stable and works from any IP.

Setup required:
  1. Register a DA app at the developer portal → get client_id + client_secret
  2. Do one-time Authorization Code flow in browser → get refresh_token
  3. Store da_client_id, da_client_secret, da_refresh_token in settings
  4. Access tokens auto-refresh (1-hour expiry, 3-month refresh token)

Post flow:
  POST /api/v1/oauth2/deviation/literature/create

Edit flow:
  POST /api/v1/oauth2/deviation/literature/update/{id}

Rating mapping:
  General → is_mature=false
  Mature → is_mature=true, mature_level="moderate"
  Adult → is_mature=true, mature_level="strict", mature_classification=["sexual"]
"""

from __future__ import annotations

import logging
import time

import config
from clients.da.client import (
    DAClient,
    _int_id_from_url,
    description_to_da_html,
)
from posting import tag_budget
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage

logger = logging.getLogger(__name__)


# The file types DA treats as an image deviation (Sta.sh path) rather than
# literature. post() and edit() MUST agree on this — they did not, and the
# disagreement is the 2026-09-02 artwork-sync crash.
_IMAGE_TYPES = ("png", "jpg", "jpeg", "gif", "webp")


class DeviantArtPoster(PlatformPoster):

    platform_id = "da"
    requires_mode = "any"   # PROXY_REQUIRED like SF — CF Worker, not a desktop lock
    platform_name = "DeviantArt"
    supports_edit = True
    # ⚠ Was False in 3.34.0 on the grounds that DA had no image-edit endpoint.
    # That was wrong — it was OUR CLIENT that had none. DA exposes
    # `POST /deviation/edit/{id}` for any deviation type, and the description
    # (which that endpoint does NOT carry) goes through the editor's own
    # `_napi/shared_api/deviation/update`. Both are wired below.
    supports_artwork_edit = True
    supports_file_replace = False  # Update endpoint replaces body content
    min_post_interval = 5
    max_file_size = 0  # Literature has no file; Sta.sh caps images ~30 MB (unenforced here)
    accepted_file_types = ["txt", "md", "png", "jpg", "jpeg", "gif", "webp"]

    def __init__(self):
        self._client: DAClient | None = None
        self._access_token: str = ""
        self._token_expires_at: float = 0.0
        # Which account the current access token actually authorises, cached
        # for that token's lifetime so the whoami costs one call per hour
        # rather than one per post.
        self._token_owner: str = ""
        # Fingerprint of the credentials the cached access token was minted
        # from. See _ensure_client — without it, re-authorising an account had
        # no effect until the cached token expired.
        self._cred_fp: str = ""

    async def _ensure_client(self) -> tuple[DAClient, str]:
        """Get client and a valid access token."""
        settings = config.get_settings()
        creds = self._resolve_creds("da", settings)

        client_id = creds.get("da_client_id", "")
        client_secret = creds.get("da_client_secret", "")
        refresh_token = creds.get("da_refresh_token", "")

        if not client_id or not client_secret or not refresh_token:
            raise RuntimeError(
                "DeviantArt OAuth not configured. Set da_client_id, "
                "da_client_secret, and da_refresh_token in settings."
            )

        target_user = creds.get("da_target_user", "")
        if not self._client:
            self._client = DAClient(
                cookie=creds.get("da_cookie", ""),
                target_user=target_user,
            )
        elif self._client.target_user != target_user:
            # Posters are cached per (platform, account_id) for the life of the
            # process, so a renamed target would otherwise stay stale forever.
            self._client.target_user = target_user

        # ⚠ Has the stored credential changed under us?
        #
        # Posters live in a module-level cache, and the access token is good for
        # an hour. Re-authorising an account writes a NEW `da_refresh_token`,
        # but nothing here noticed: the cached access token was still valid, so
        # no refresh happened, and the identity check below kept reporting the
        # account the OLD token belonged to. Reported as "I've reauthorised both
        # accounts but I keep getting the wrong-account error" — the
        # re-authorisation had in fact worked and simply could not take effect
        # for up to an hour.
        #
        # The fingerprint is taken AFTER each refresh, from the rotated token
        # that was just stored. DA rotates on every refresh, so comparing
        # against the token we *sent* would differ on the very next call and
        # force a refresh every time — burning a single-use credential per post.
        fp = self._fingerprint(client_id, client_secret, refresh_token)
        if fp != self._cred_fp:
            if self._cred_fp:
                logger.info("DA: credentials for account %s changed — "
                            "discarding the cached token", self.account_id)
            self._access_token = ""
            self._token_owner = ""
            self._token_expires_at = 0.0

        # Refresh access token if expired or missing
        if not self._access_token or time.time() >= self._token_expires_at:
            data = await self._client.oauth_refresh_token(
                client_id, client_secret, refresh_token,
            )
            self._access_token = data.get("access_token", "")
            self._token_owner = ""      # new token — re-check who it belongs to
            expires_in = data.get("expires_in", 3600)
            self._token_expires_at = time.time() + expires_in - 60  # 1-min buffer

            # DA rotates the refresh token on every refresh, so the new one
            # MUST land on this account's own key. Writing the bare key here
            # (which is what this line did until 3.21.0) handed a non-default
            # account's fresh token to the default account and left the
            # non-default account holding a spent one — killing both. See
            # PlatformPoster._save_creds for the full account of the incident.
            new_refresh = data.get("refresh_token", "")
            if new_refresh and new_refresh != refresh_token:
                self._save_creds("da", {"da_refresh_token": new_refresh})
                logger.info("DA: stored the rotated refresh token for account %s",
                            self.account_id)
            # Fingerprint what is now STORED, so the next call sees no change.
            self._cred_fp = self._fingerprint(
                client_id, client_secret, new_refresh or refresh_token)

        # ⚠ Which account does this token actually post as?
        #
        # `da_refresh_token` is per-user and decides where a post lands;
        # `target_user` is just a stored string. Until 3.32.0 nothing compared
        # them, and they had already diverged: the default DA account held a
        # refresh token authorising a DIFFERENT account — the residue of the
        # 3.21.0 bare-key incident, whose cause was fixed while the credential
        # was left in place. One piece posted "as" the first account landed on
        # the second, silently and successfully.
        #
        # Checked once per access token (they last an hour), and a mismatch
        # raises rather than posting: an upload to the wrong account cannot be
        # taken back, and with a friend's account in the list it is the failure
        # that must never happen. An unreachable whoami is NOT treated as a
        # mismatch — a network blip must not block posting.
        if self._access_token and not self._token_owner:
            who = await self._client.whoami(self._access_token)
            if who and who.get("username"):
                self._token_owner = who["username"]
        target = (self._client.target_user or "").strip()
        if self._token_owner and target and \
                self._token_owner.strip().lower() != target.lower():
            raise RuntimeError(
                f"DeviantArt: this account's stored token posts as "
                f"{self._token_owner}, not {target}. Re-authorise "
                f"{target} from Settings → Accounts → Authorise posting before "
                f"posting again — otherwise the upload lands on the wrong "
                f"account.")

        return self._client, self._access_token

    async def validate_session(self) -> dict:
        """Which account would a post from here actually land on?

        Mirrors ``FAClient.validate_session`` so the Accounts panel can report
        one shape for both. ⚠ Obtaining a posting token **consumes and rotates**
        `da_refresh_token` — DA issues single-use refresh tokens — so this goes
        through ``_ensure_client``, the one path that persists the rotation to
        the right per-account key. Testing through any other route would spend
        the token and leave the account unable to post.
        """
        out = {"ok": False, "logged_in": False, "username": "",
               "expected": "", "matches": False, "detail": ""}
        try:
            client, token = await self._ensure_client()
        except RuntimeError as e:
            # _ensure_client raises on a mismatch too — that message already
            # names both accounts and what to do, so pass it straight through.
            msg = str(e)
            out["detail"] = msg
            if "posts as" in msg:
                out["logged_in"] = True
            return out
        except Exception as e:
            out["detail"] = f"Could not obtain a DeviantArt posting token: {e}"
            return out
        out["expected"] = (client.target_user or "").strip()
        who = await client.whoami(token)
        if not who:
            out["logged_in"] = bool(token)
            out["ok"] = bool(token)
            out["matches"] = True
            out["detail"] = ("A posting token was obtained, but DeviantArt did "
                             "not say which account it belongs to — identity "
                             "unconfirmed.")
            return out
        out["logged_in"] = True
        out["username"] = who.get("username", "")
        out["matches"] = out["username"].strip().lower() == out["expected"].lower()
        out["ok"] = out["matches"]
        if not out["matches"]:
            out["detail"] = (
                f"This account's stored token posts as {out['username']}, not "
                f"{out['expected']}. Re-authorise {out['expected']} from "
                f"Settings → Accounts → Authorise posting.")
        return out

    async def post(self, package: StoryUploadPackage) -> PostResult:
        """Create a deviation on DeviantArt — image (Sta.sh) or literature."""
        _t = self._start_timer()
        try:
            client, token = await self._ensure_client()
            is_mature, mature_level, mature_class = _rating_to_da(package.rating)

            if package.file_type in _IMAGE_TYPES:
                # Image: stash the file, then publish it to the gallery.
                # Fit ONCE and give the same list to both calls — tags are
                # stash metadata on submit and a deviation field on publish,
                # and computing them twice is how they drift apart.
                da_tags = tag_budget.fit(package.tags, self.platform_id)
                stash = await client.oauth_stash_submit(
                    package.file_path,
                    title=package.title[:50],
                    # Paragraph HTML, not raw text: a description made of bare
                    # newlines has no block elements for DA's editor to map onto,
                    # and the owner then cannot open their own description
                    # ("Invalid Input"). It renders either way; this makes it
                    # EDITABLE. Observed live 2026-09-02.
                    artist_comments=description_to_da_html(package.description),
                    tags=da_tags,
                    access_token=token,
                )
                settings = config.get_settings()
                result = await client.oauth_stash_publish(
                    stash["itemid"],
                    is_mature=is_mature,
                    mature_level=mature_level,
                    mature_classification=mature_class,
                    catpath=package.extra.get("catpath", settings.get("artwork_da_catpath", "")),
                    tags=da_tags,
                    access_token=token,
                )
            else:
                # Literature: read story content + create the deviation.
                body = ""
                if package.file_path:
                    with open(package.file_path, "r", encoding="utf-8") as f:
                        body = f.read()
                if not body:
                    body = package.description
                result = await client.oauth_create_literature(
                    title=package.title[:50],
                    body=body,
                    tags=tag_budget.fit(package.tags, self.platform_id),
                    is_mature=is_mature,
                    mature_level=mature_level,
                    mature_classification=mature_class,
                    access_token=token,
                )

            # ⚠ DeviantArt has TWO ids for one deviation and this used to store
            # the wrong one. `deviationid` is the API's GUID; the integer at the
            # end of the public URL is what the rest of PawPoller speaks —
            # `clients/da/client.py` says so in its header ("Deviation IDs stored
            # in the DB are integers … the API's UUID is used only transiently"),
            # the poller writes integers into `da_submissions`, and image hashes,
            # publications and Masterpiece members are all keyed on it.
            #
            # Storing the GUID meant a post made THROUGH PawPoller matched
            # nothing it had ever polled: the auto-link in `manager.post_artwork`
            # wrote a member id that joined to no submission, so the piece kept
            # offering its own upload back under "is this the same image?" —
            # reported as being asked to link up a post we had just made. Three
            # of six DA members and two publications were in this state.
            #
            # The URL always carries the integer, so derive it and keep the GUID
            # only as a last resort — a dangling GUID is still better than no id
            # at all, but say so loudly, because it will misbehave the same way.
            url = result.get("url", "")
            dev_id = _int_id_from_url(url)
            if dev_id is None:
                dev_id = result.get("deviationid", "")
                logger.warning(
                    "DA: no integer id in the returned URL %r — falling back to the "
                    "API GUID %r, which will not match anything the poller stores",
                    url, dev_id)
            return PostResult(
                success=True,
                external_id=str(dev_id),
                external_url=url,
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("DA post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    @staticmethod
    def _fingerprint(*parts: str) -> str:
        """Opaque digest of the credentials a token was minted from.

        Hashed rather than stored so a refresh token never sits in a second
        place in memory, and so a log line or repr can never leak one.
        """
        import hashlib
        return hashlib.sha256("\x00".join(p or "" for p in parts).encode()).hexdigest()

    @staticmethod
    def _deviation_url(client, resp: dict, external_id: str) -> str:
        """The deviation's public URL after an edit.

        This used to be built as ``/{user}/art/{external_id}``, which was never
        a real DeviantArt URL — they are ``/{user}/art/{slug}-{id}`` — so every
        edit overwrote a working link with a 404. Prefer what the API hands
        back, then the gallery cache, and only then "" — which `manager.update_story`
        reads as "keep what is stored" (`result.external_url or pub[...]`, :806),
        so an unknown URL leaves the good one in place instead of replacing it
        with a broken one.
        """
        url = (resp or {}).get("url", "")
        if url:
            return url
        sid = str(external_id or "")
        if sid.isdigit():
            cached = getattr(client, "_gallery_cache", {}).get(int(sid)) or {}
            if cached.get("url"):
                return cached["url"]
        return ""

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        """Update a LITERATURE deviation on DeviantArt.

        ⚠ **Artwork cannot be edited on DA at all.** The API offers
        `deviation/literature/update/{id}` and no image equivalent, so there is
        nothing to call for an image deviation — `supports_artwork_edit = False`
        and `manager.update_artwork` skips DA members as post-only.

        This method still guards the case itself, because the guard is cheap and
        the failure was not: `post()` has always branched images to the Sta.sh
        path, `edit()` did not branch at all, so an artwork sync fell straight
        through to the literature path and tried to read a JPEG as UTF-8 text —
        on prod 2026-09-02, `'utf-8' codec can't decode byte 0xff in position 0`
        (0xFF is a JPEG's SOI marker). Exactly the SoFurry 3.9.5 shape, and with
        the same collateral: the failure was recorded against a live, correctly
        posted deviation, flipping its publication row to `failed`.
        """
        _t = self._start_timer()
        try:
            if package.file_type in _IMAGE_TYPES:
                return await self._edit_artwork(external_id, package, _t)

            client, token = await self._ensure_client()

            body = None
            # Sync-all sets skip_content_refresh on every member; honour it so a
            # metadata-only edit never re-reads the file. FA/IB/SF all did; DA
            # did not, which is how an artwork package's image reached open().
            skip_content = bool(package.extra.get("skip_content_refresh", False))
            if package.file_path and not skip_content:
                with open(package.file_path, "r", encoding="utf-8") as f:
                    body = f.read()

            is_mature, _, _ = _rating_to_da(package.rating)

            # The write endpoints want the API GUID; we store the integer (see
            # `post`). `uuid_for` converts, and passes a GUID through unchanged
            # so rows written before that fix still edit.
            resp = await client.oauth_update_literature(
                await client.uuid_for(external_id),
                title=package.title[:50],
                body=body,
                tags=tag_budget.fit(package.tags, self.platform_id),
                is_mature=is_mature,
                access_token=token,
            )

            return PostResult(
                success=True,
                external_id=external_id,
                external_url=self._deviation_url(client, resp, external_id),
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("DA edit failed for %s: %s", external_id, e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    async def _edit_artwork(self, external_id: str, package: StoryUploadPackage,
                            _t) -> PostResult:
        """Push canonical metadata to a published IMAGE deviation.

        Two endpoints, because DA splits the job and neither half can do the
        other's work:

        * ``POST /deviation/edit/{id}`` (OAuth) — title, tags, mature flags,
          gallery folders. It has **no description parameter at all**.
        * ``POST /_napi/shared_api/deviation/update`` (session + CSRF) — the
          description, as the editor's own structured document.

        The description leg is **best effort**: it needs the `da_cookie`, which
        the official-API migration had been shedding, so an install without one
        still gets title/tags/rating synced and is told the description was left
        alone — rather than the whole edit failing over the one field OAuth
        cannot reach.
        """
        client, token = await self._ensure_client()
        is_mature, mature_level, mature_class = _rating_to_da(package.rating)

        await client.oauth_edit_deviation(
            await client.uuid_for(external_id),
            title=package.title[:50],
            tags=tag_budget.fit(package.tags, self.platform_id),
            is_mature=is_mature,
            mature_level=mature_level,
            mature_classification=mature_class,
            access_token=token,
        )

        warning = None
        if package.description:
            try:
                await client.napi_set_description(external_id, package.description)
            except Exception as e:
                logger.warning("DA: metadata updated but description not set for %s: %s",
                               external_id, e)
                warning = (f"Title, tags and rating updated. Description NOT changed "
                           f"({e}) — DeviantArt's API has no description field, so that "
                           f"part needs a valid da_cookie.")

        return PostResult(
            success=True,
            external_id=external_id,
            external_url=self._deviation_url(client, {}, external_id),
            error=warning,          # non-fatal note, as the Weasyl poster does
            duration_seconds=self._elapsed(_t),
        )

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        """Replace literature body content via the update endpoint.

        Same limit as `edit()` — there is no image equivalent — and the same
        UTF-8 read waiting for an image path, so it refuses one up front.
        """
        _t = self._start_timer()
        try:
            if file_path.rsplit(".", 1)[-1].lower() in _IMAGE_TYPES:
                return PostResult(
                    success=False,
                    external_id=external_id,
                    error="DeviantArt cannot replace an image deviation's file.",
                    duration_seconds=self._elapsed(_t),
                )

            client, token = await self._ensure_client()

            with open(file_path, "r", encoding="utf-8") as f:
                body = f.read()

            resp = await client.oauth_update_literature(
                await client.uuid_for(external_id),
                body=body,
                access_token=token,
            )

            return PostResult(
                success=True,
                external_id=external_id,
                external_url=self._deviation_url(client, resp, external_id),
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("DA file replace failed for %s: %s", external_id, e)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    def validate(self, package: StoryUploadPackage) -> list[str]:
        """Check what will actually be SENT, not what was handed in.

        Every upload path in this class puts the tags through
        `tag_budget.fit(...)` before they reach the API, so a package carrying
        more than 30 tags is not an error — it is the normal case for a richly
        tagged piece, and the trim is the designed behaviour.

        Validating the untrimmed count instead was a real outage (3.17.0): a
        38-tag piece failed here and never reached `upload()`, which would have
        fitted it to 30 and succeeded. Two statements of one limit, applied to
        different lists. The check now runs on the same `fit()` output the
        uploader uses, so the two cannot drift apart again.
        """
        errors = []
        if len(package.title) > 50:
            errors.append(f"DA title max 50 chars (got {len(package.title)})")
        sent = tag_budget.fit(package.tags, self.platform_id)
        if len(sent) > 30:
            errors.append(f"DA max 30 tags (got {len(sent)})")
        return errors


def _rating_to_da(rating: str) -> tuple[bool, str, list[str]]:
    """Convert rating to DA's mature settings.

    Returns (is_mature, mature_level, mature_classification).
    """
    r = rating.lower()
    if r in ("adult", "explicit", "nsfw"):
        return True, "strict", ["sexual"]
    elif r in ("mature", "questionable"):
        return True, "moderate", []
    return False, "", []
