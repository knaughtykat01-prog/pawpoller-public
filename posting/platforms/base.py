"""Abstract base class for platform posting implementations.

Each platform poster wraps the existing PawPoller client (e.g. InkbunnyClient,
BskyClient) and adds upload/edit/replace methods. The base class enforces a
consistent interface so the PostingManager can treat all platforms the same.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class PostResult:
    """Result of a posting operation (upload, edit, or file replace)."""
    success: bool
    external_id: str = ""
    external_url: str = ""
    error: str | None = None
    duration_seconds: float = 0.0


@dataclass
class StoryUploadPackage:
    """Everything needed to post one chapter/story to one platform."""
    story_name: str
    chapter_index: int              # 0 = full story, 1+ = chapter number
    chapter_title: str
    platform: str                   # 'ib', 'fa', 'ws', 'sf', 'bsky'
    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    rating: str = ""                # Platform-specific rating value
    file_path: str | None = None    # Absolute path to format file
    file_type: str = ""             # 'bbcode', 'pdf', 'html', 'text'
    word_count: int = 0
    thumbnail_path: str | None = None
    extra: dict = field(default_factory=dict)
    # 4.18.0 (MEDIATYPES): the primary's kind ('' for a story) and what the browser measured, so a
    # poster can check a site's duration / size cap without decoding. The poster image of a
    # video / audio piece travels as thumbnail_path, exactly like a story's cover.
    media_kind: str = ""
    duration_s: float | None = None
    width: int = 0
    height: int = 0


# The three-step rating ladder every poster maps its own vocabulary onto (4.21.0). The
# words are the app's canonical ratings; the aliases are what packages have carried
# historically (each poster's _rating_to_* accepts the same set).
RATING_WORD = ("general", "mature", "adult")
_RATING_ALIASES = {
    "general": 0, "safe": 0, "sfw": 0, "g": 0, "s": 0, "e": 0, "everyone": 0, "": 0,
    "mature": 1, "questionable": 1, "m": 1, "q": 1, "teen": 1, "t": 1,
    "adult": 2, "explicit": 2, "nsfw": 2, "x": 2, "a": 2, "porn": 2,
}


def rating_rank(rating: str | None) -> int:
    """0 general · 1 mature · 2 adult; an unknown word is treated as adult (never leaks
    something explicit onto an SFW site by mislabelling)."""
    key = str(rating or "").strip().lower()
    if key in _RATING_ALIASES:
        return _RATING_ALIASES[key]
    return 2


class PlatformPoster(ABC):
    """Base class for all platform posting implementations."""

    platform_id: str = ""
    platform_name: str = ""
    supports_edit: bool = False
    # Whether ``edit()`` can push ARTWORK metadata, not only story/literature.
    #
    # Split out from `supports_edit` because DeviantArt's edit is literature
    # ONLY: its API exposes `deviation/literature/update/{id}` and nothing
    # equivalent for an image deviation. `supports_edit = True` was therefore
    # true of stories and quietly wrong for every artwork sync — `edit()` fell
    # through to the literature path and tried to read a JPEG as UTF-8 text.
    #
    # Default True: for every other editable poster the same call carries both
    # content types. `manager.update_artwork` checks this and reports a False
    # as skipped/post-only rather than attempting an edit that cannot work —
    # which matters because a failed edit records `status='failed'` against a
    # live, correctly-posted submission.
    supports_artwork_edit: bool = True
    supports_file_replace: bool = False
    min_post_interval: int = 5      # Seconds between consecutive posts
    max_file_size: int = 0          # Bytes (0 = no limit)
    accepted_file_types: list[str] = []
    # 4.18.0: what this site takes per media kind — {'image': [...], 'video': [...], 'audio': [...]}.
    # Derived from accepted_file_types unless the poster declares it. Read by media_refusal()
    # (the manager's pre-network gate) and by GET /api/platforms/media (the pickers grey out).
    accepted_media: dict | None = None
    # 4.21.0 (MEDIAPLATS §2): the highest rating this site takes — "general", "mature" or
    # "adult". A piece rated above it is refused by rating_refusal() before the network and
    # greyed in the pickers with the reason, which is what makes an SFW-only site (YouTube,
    # SoundCloud) honest rather than hidden. Every art site takes adult work, so the default
    # changes nothing for the existing posters.
    max_rating: str = "adult"
    # "any", "desktop", or "server" — which instance may execute this platform's
    # posts. The scheduler filters on it in SQL (posting_queries.get_pending_queue),
    # and manager re-queues a job with requires='desktop' when the server can't do
    # it, so a wrong value here doesn't just misroute — it can strand work in the
    # queue forever. Default "any" is inherited SILENTLY, so an override is a
    # deliberate statement that a platform is unreachable from the other side,
    # NOT merely slower or flakier there.
    #
    # ⚠ **No platform declares "desktop" any more (3.26.0).** FurAffinity was
    # the last, on the grounds that its "datacenter-IP block is absolute (even
    # /controls/ pages come back as an empty shell with valid cookies)". That
    # empty shell was the LOGGED-OUT shell — an expired session, not an IP block
    # — and a server-side FA post has since completed in full (view/66103446).
    #
    # ⚠ Before setting this to "desktop" again, note it does TWO jobs, and the
    # second is easy to miss: besides the manager's post-failure handoff, it is
    # the value stamped into `posting_queue.requires` when a job is SCHEDULED,
    # and the scheduler filters on that in SQL. So "desktop" does not mean "try
    # the server, fall back" for scheduled work — it means the server never
    # looks at the job at all. That stranded twelve FA jobs, one for a month.
    # Platforms that are *sometimes* blocked from a datacenter IP — AO3,
    # SoFurry, DeviantArt — stay "any" and route through the CF Worker proxy
    # instead (polling/cf_proxy.py).
    requires_mode: str = "any"
    # Which account to post as. Set by manager._get_poster; None = the
    # platform's default account. Account-aware posters (IB, FA) read it in
    # _ensure_client to authenticate as the right account.
    account_id: int | None = None

    def _resolve_creds(self, platform: str, settings: dict | None = None) -> dict:
        """Return this poster's account's credentials, keyed by canonical field.

        Resolves ``self.account_id`` (set by manager._get_poster; None → the
        platform's default account) to its credential set via
        ``config.resolve_account_credentials``. Posters call this in
        ``_ensure_client`` instead of reading flat ``settings.get(...)`` so they
        authenticate as the selected account.
        """
        import config
        from database.db import get_connection
        from database import accounts as _accts
        conn = get_connection()
        try:
            acct_id = self.account_id
            if acct_id is None:
                acct_id = _accts.get_default_account_id(conn, platform, create=True)
                self.account_id = acct_id
            acct = _accts.get_account(conn, acct_id)
            is_default = bool(acct["is_default"]) if acct else True
        finally:
            conn.close()
        return config.resolve_account_credentials(platform, acct_id, is_default, settings)

    def _save_creds(self, platform: str, values: dict[str, str]) -> None:
        """Persist credential fields for THIS poster's account.

        The write-side mirror of :meth:`_resolve_creds`, and the only correct
        way for a poster to store a rotated OAuth token.

        Refresh tokens are single-use. The call that hands back an access token
        also consumes the refresh token it was given and issues a replacement,
        so *where that replacement is written* decides whether the account can
        ever authenticate again. It has to go back to the key it was read from:
        the default account's bare ``da_refresh_token``, or another account's
        ``acct_<id>_da_refresh_token``.

        Writing the bare key unconditionally is not a cosmetic slip — it kills
        BOTH accounts at once. The non-default account's own key keeps the
        now-consumed token, so it can never refresh again; the default
        account's key is overwritten with a token belonging to a *different*
        OAuth application, so it either fails to refresh or, while the stolen
        token is briefly alive, authenticates as the other account and posts
        into ITS gallery. Both DeviantArt accounts died exactly this way on
        2026-08-19, and publication 173 — recorded against account 7 — landed
        in account 27's gallery on the way down.
        """
        if not values:
            return
        import config
        from database.db import get_connection
        from database import accounts as _accts
        conn = get_connection()
        try:
            acct_id = self.account_id
            if acct_id is None:
                acct_id = _accts.get_default_account_id(conn, platform, create=True)
                self.account_id = acct_id
            acct = _accts.get_account(conn, acct_id)
            is_default = bool(acct["is_default"]) if acct else True
        finally:
            conn.close()
        config.save_settings({
            config.account_setting_key(acct_id, field, is_default): value
            for field, value in values.items()
        })

    @abstractmethod
    async def post(self, package: StoryUploadPackage) -> PostResult:
        """Upload a new submission to the platform."""
        ...

    @abstractmethod
    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        """Edit metadata on an existing submission."""
        ...

    @abstractmethod
    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        """Replace the file on an existing submission."""
        ...

    async def probe_exists(self, external_id: str) -> bool | None:
        """Check whether a previously-posted submission still exists on the platform.

        Returns:
            True  — confirmed still present
            False — confirmed deleted / missing
            None  — probe not implemented for this platform, caller should
                    not draw conclusions from the result
        """
        return None

    async def probe_draft_state(self, external_id: str) -> bool | None:
        """Check whether a previously-posted submission is sitting as a draft.

        "Draft" semantics vary by platform: FA has no real drafts, so its
        implementation reads the Scraps flag (hidden from gallery/browse/
        search but still on the profile + visible to watchers). IB exposes
        an explicit visibility/hold state. SF flags works as published or
        unpublished. AO3/SQW have a `posted: false` state.

        Returns:
            True  — confirmed draft / not publicly listed
            False — confirmed live / publicly listed
            None  — probe not implemented for this platform
        """
        return None

    def media_accepts(self) -> dict:
        from posting import media_kinds
        return self.accepted_media or media_kinds.accepted_from_types(self.accepted_file_types)

    def media_refusal(self, package: StoryUploadPackage) -> str | None:
        """One sentence when this site does not take the package's media kind / extension, else
        None. Stories (media_kind '') are never refused here. Checked by the manager BEFORE
        validate() and before any network call (4.18.0)."""
        from posting import media_kinds
        kind = (package.media_kind or "").lower()
        if not kind or not package.file_path:
            return None
        accepts = self.media_accepts()
        ok = {str(t).lower() for t in accepts.get(kind, [])}
        if (package.file_type or "").lower() in ok:
            return None
        return media_kinds.refusal(self.platform_name or self.platform_id, accepts, kind, (package.file_type or "").lower())

    def rating_refusal(self, package: StoryUploadPackage) -> str | None:
        """One sentence when the package's rating is above what this site takes (4.21.0),
        else None. Checked by the manager beside media_refusal(), before any network call."""
        have = rating_rank(package.rating)
        allowed = rating_rank(self.max_rating)
        if have <= allowed:
            return None
        return (f"{self.platform_name or self.platform_id} doesn't take {RATING_WORD[have]} work — "
                f"this piece is rated {RATING_WORD[have]}; it takes work up to {RATING_WORD[allowed]}.")

    def refusal(self, package: StoryUploadPackage) -> str | None:
        """The pre-network gate the manager runs (4.21.0): the media-kind refusal, else the
        rating refusal, else None."""
        return self.media_refusal(package) or self.rating_refusal(package)

    def validate(self, package: StoryUploadPackage) -> list[str]:
        """Validate a package before posting. Returns list of errors (empty = OK)."""
        errors = []
        refusal = self.media_refusal(package) or self.rating_refusal(package)
        if refusal:
            errors.append(refusal)
        if not package.title:
            errors.append("Title is required")
        if not package.tags:
            errors.append("At least one tag is required")
        if package.file_path:
            import os
            if not os.path.isfile(package.file_path):
                errors.append(f"File not found: {package.file_path}")
            elif self.max_file_size > 0:
                size = os.path.getsize(package.file_path)
                if size > self.max_file_size:
                    errors.append(
                        f"File too large: {size / 1024 / 1024:.1f}MB "
                        f"(max {self.max_file_size / 1024 / 1024:.1f}MB)"
                    )
        return errors

    async def _rate_limit(self) -> None:
        """Sleep for the platform's minimum post interval."""
        import asyncio
        await asyncio.sleep(self.min_post_interval)

    @staticmethod
    def _start_timer() -> float:
        """Start a timer. Call _elapsed(start) to get seconds elapsed."""
        return time.monotonic()

    @staticmethod
    def _elapsed(start: float) -> float:
        """Return seconds elapsed since _start_timer()."""
        return time.monotonic() - start
