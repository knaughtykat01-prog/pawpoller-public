"""Furbooru platform poster (4.28.0).

Furbooru runs Philomena, so an upload is one multipart POST to the JSON API with
the account's API key — no browser session. What makes the site different is
its rulebook, and this poster enforces the parts that are checkable before a
byte leaves the machine:

  - **Do-Not-Post list (rule #1).** Each artist tag is looked up; a claim of
    "Artist Upload Only", "With Permission Only", "Other" or "Certain
    Type/Location Only" refuses the post and quotes the artist's own
    conditions, unless the piece carries ``fbr.dnp_ack`` (the operator has
    read the conditions and has the permission — e.g. the commissioner
    clause many entries grant). "No Edits" does not block: PawPoller posts
    the piece as delivered. An artist absent from the list is not cleared by
    that fact — the site says so — but it is not a machine-checkable refusal.
  - **Rating tag (rule #2).** Every image needs one: general → safe,
    mature → questionable, adult → explicit (anything unknown → explicit).
  - **Five or more tags**, with the artist tag as ``artist:name`` and
    ``artist needed`` when there is none. Tags use SPACES ("solo female"),
    so the catalogue's underscores are converted here.
  - **Sources.** Rule #2 wants the URL the image was posted at elsewhere;
    the per-platform ``source`` override supplies it (up to 15).

Not enforced here: reverse-search for duplicates — the API's reverse search
takes a public URL, which a fresh local render does not have.
"""

from __future__ import annotations

import logging
import os

import config
from clients.fbr.client import FurbooruClient
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage, rating_rank

logger = logging.getLogger(__name__)

_MIN_TAGS = 5                      # the upload form's own floor, rating tag included
_RATING_TAG = ("safe", "questionable", "explicit")
# DNP types that mean "not by you, not without asking" — refused unless acknowledged.
_DNP_BLOCKING = {"artist upload only", "with permission only", "other", "certain type/location only"}


class FurbooruPoster(PlatformPoster):

    platform_id = "fbr"
    platform_name = "Furbooru"
    supports_edit = False           # tags are communal and edited on-site; no API edit route
    supports_artwork_edit = False
    supports_file_replace = False
    min_post_interval = 5
    max_file_size = 100 * 1024 * 1024
    accepted_file_types = ["png", "jpg", "jpeg", "gif", "webp", "svg", "webm"]
    requires_mode = "any"

    def __init__(self):
        self._client: FurbooruClient | None = None

    async def _ensure_client(self) -> FurbooruClient:
        settings = config.get_settings()
        creds = self._resolve_creds("fbr", settings)
        username = creds.get("fbr_username", "")
        api_key = creds.get("fbr_api_key", "")
        if not api_key:
            raise RuntimeError("Furbooru posting needs an API key (Settings → Platforms → Furbooru)")
        if self._client is None:
            self._client = FurbooruClient(username=username, api_key=api_key)
        else:
            self._client.update_credentials(username, api_key)
        return self._client

    async def post(self, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        try:
            client = await self._ensure_client()
            tags = build_tag_list(package)
            if not package.extra.get("dnp_ack"):
                refusal = await dnp_refusal(client, tags)
                if refusal:
                    return PostResult(success=False, error=refusal, duration_seconds=self._elapsed(_t))
            source = str(package.extra.get("source", "") or "").strip()
            result = await client.upload_image(
                file_path=package.file_path or "",
                tag_input=", ".join(tags),
                description=package.description or "",
                sources=[source] if source else None,
                anonymous=bool(package.extra.get("anonymous")),
            )
            return PostResult(success=True, external_id=result["image_id"],
                              external_url=result["url"], duration_seconds=self._elapsed(_t))
        except Exception as e:
            logger.error("Furbooru post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        return PostResult(success=False, external_id=external_id,
                          error="Furbooru tags are edited on the site itself")

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        return PostResult(success=False, error="Furbooru does not support file replacement")

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors: list[str] = []
        if not package.file_path:
            errors.append("Furbooru requires an image file")
        elif os.path.isfile(package.file_path) and os.path.getsize(package.file_path) > self.max_file_size:
            errors.append(f"File too large (max {self.max_file_size // (1024 * 1024)}MB)")
        tags = build_tag_list(package)
        if len(tags) < _MIN_TAGS:
            errors.append(f"Furbooru asks for {_MIN_TAGS} or more tags including the rating "
                          f"(got {len(tags)}: {', '.join(tags)})")
        return errors


def rating_tag(rating: str) -> str:
    """general → safe, mature → questionable, adult (and anything unknown) → explicit."""
    return _RATING_TAG[rating_rank(rating)]


def to_site_tag(tag: str) -> str:
    """Catalogue tags carry underscores; Furbooru tags carry spaces. An `artist:`
    or `oc:` namespace keeps its colon."""
    return " ".join(str(tag).replace("_", " ").split()).lower()


def build_tag_list(package: StoryUploadPackage) -> list[str]:
    """The exact tag set sent: rating first, then the piece's tags with the
    artist tag in Furbooru's `artist:name` form (the catalogue injects the bare
    name first; see artwork_reader._ARTIST_TAG_PLATFORMS), `artist needed`
    when there is none, de-duplicated, order kept."""
    out: list[str] = [rating_tag(package.rating)]
    artist_name = to_site_tag(str(package.extra.get("artist_name", "") or ""))
    seen = set(out)
    for raw in package.tags or []:
        t = to_site_tag(raw)
        if not t or t in _RATING_TAG:
            continue
        if artist_name and t == artist_name and not t.startswith("artist:"):
            t = f"artist:{t}"
        if t not in seen:
            seen.add(t)
            out.append(t)
    if not any(t.startswith("artist:") for t in out):
        if artist_name:
            out.insert(1, f"artist:{artist_name}")
        else:
            out.append("artist needed")
    return out


async def dnp_refusal(client: FurbooruClient, tags: list[str]) -> str:
    """'' when no artist tag on the piece carries a blocking DNP claim; otherwise
    the refusal text quoting the artist's conditions."""
    for t in tags:
        if not t.startswith("artist:"):
            continue
        for entry in await client.dnp_entries(t):
            kind = str(entry.get("dnp_type", "") or "").strip()
            if kind.lower() in _DNP_BLOCKING:
                cond = str(entry.get("conditions", "") or "").strip()
                return (f"Furbooru's Do-Not-Post list has {t} as \"{kind}\""
                        + (f": {cond}" if cond else "")
                        + " — not posted. If you have the artist's permission (or are the "
                          "commissioner where their conditions allow it), set dnp_ack in the "
                          "piece's Furbooru options and post again.")
    return ""
