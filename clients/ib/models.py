"""Pydantic response models for Inkbunny API.

IMPORTANT NOTE ON STRING TYPES:
The Inkbunny API returns almost all numeric fields (submission_id, user_id, views,
favorites_count, etc.) as JSON *strings*, not integers. For example:
    {"submission_id": "123456", "views": "42", "favorites_count": "7"}

This is a quirk of the Inkbunny API -- it serialises everything as strings regardless
of the underlying data type. We therefore declare these fields as `str` in the Pydantic
models to match the raw API response, and perform int conversion only when needed
(e.g. in to_db_dict() for database storage).
"""

from __future__ import annotations
from pydantic import BaseModel, ConfigDict, Field


class LoginResponse(BaseModel):
    """Response from api_login.php.

    The SID (session ID) is the authentication token used for all subsequent API calls.
    ratingsmask indicates which content ratings the session is currently allowed to see.
    """
    sid: str
    user_id: int = 0
    ratingsmask: str = ""


class SearchSubmission(BaseModel):
    """A single submission entry from api_search.php results.

    All numeric-looking fields (submission_id, user_id, views, etc.) are strings
    because that's how the Inkbunny API serialises them. See module docstring.
    """
    submission_id: str
    title: str = ""
    username: str = ""
    user_id: str = ""
    create_datetime: str = ""
    type_name: str = ""             # e.g. "Picture/Pinup", "Writing - Document", etc.
    rating_id: str = "0"            # 0=General, 1=Mature, 2=Adult
    rating_name: str = ""           # Human-readable rating label
    thumbnail_url_medium_noncustom: str = Field("", alias="thumbnail_url_medium_noncustom")
    views: str = "0"
    favorites_count: str = "0"
    comments_count: str = "0"

    # populate_by_name allows fields to be set using either their Python attribute
    # name or their alias. This is needed because Pydantic v2 by default only
    # accepts the alias when parsing from dict/JSON. With this enabled, both
    # `thumbnail_url_medium_noncustom` and the alias work as field names.
    # (ConfigDict, not the class-based `class Config:` — that form is deprecated
    # in Pydantic v2 and removed in v3.)
    model_config = ConfigDict(populate_by_name=True)


class SearchResponse(BaseModel):
    """Paginated response from api_search.php.

    pages_count, page, and results_count_all are typed as str|int because Inkbunny
    sometimes returns these as strings and sometimes as integers depending on the
    endpoint version and parameters used. Accepting both avoids parse failures.
    """
    sid: str = ""
    results_count_all: str | int = "0"  # Total matching submissions across all pages
    pages_count: str | int = "1"        # Total number of result pages
    page: str | int = "1"               # Current page number
    submissions: list[SearchSubmission] = []


class Keyword(BaseModel):
    """A single keyword/tag attached to a submission."""
    keyword_id: str = ""
    keyword_name: str = ""


class SubmissionDetail(BaseModel):
    """Full submission details from api_submissions.php.

    Contains richer data than SearchSubmission, including multiple thumbnail URL
    variants at different resolutions, the full description HTML, and keywords.
    """
    submission_id: str
    title: str = ""
    username: str = ""
    user_id: str = ""
    create_datetime: str = ""
    type_name: str = ""
    rating_id: str = "0"
    rating_name: str = ""
    # Inkbunny provides thumbnails at several resolutions. Not all are always populated --
    # custom thumbnails may only appear in certain URL fields, so we store all variants
    # and pick the best available one in to_db_dict().
    thumbnail_url_medium_noncustom: str = ""  # System-generated medium thumb (always exists)
    thumbnail_url_huge: str = ""              # Largest available thumbnail
    thumbnail_url_large: str = ""             # Large thumbnail
    thumbnail_url_medium: str = ""            # Medium thumbnail (may be custom)
    pagecount: str = "1"                      # Number of pages/files in the submission
    views: str = "0"
    favorites_count: str = "0"
    comments_count: str = "0"
    description: str = ""                     # Full description HTML
    keywords: list[Keyword] = []              # Tags/keywords attached to the submission
    # IB exposes "public" as "yes" / "no" — submissions held / under review
    # / set to friends-only return "no". Used by the draft-state probe to
    # surface unpublished works in the publish-check matrix.
    public: str = ""

    def to_db_dict(self) -> dict:
        """Normalise the API response into a clean dict suitable for database storage.

        This method handles two key transformations:
        1. STRING-TO-INT CONVERSION: The Inkbunny API returns numeric fields as strings
           (e.g. "42" instead of 42). We convert them to proper integers here so the
           database schema can use integer columns for sorting and aggregation.

        2. THUMBNAIL FALLBACK CHAIN: Not all thumbnail URLs are populated for every
           submission. We prefer the highest resolution available, falling back through:
             huge -> large -> medium -> medium_noncustom
           The noncustom variant is the guaranteed fallback (system-generated).
        """
        # Select the best available thumbnail resolution (highest quality first)
        thumb = (self.thumbnail_url_huge or self.thumbnail_url_large
                 or self.thumbnail_url_medium or self.thumbnail_url_medium_noncustom)
        return {
            "submission_id": int(self.submission_id),
            "title": self.title,
            "username": self.username,
            "user_id": int(self.user_id) if self.user_id else None,
            "create_datetime": self.create_datetime,
            "type_name": self.type_name,
            "rating_id": int(self.rating_id),
            "rating_name": self.rating_name,
            "thumb_url": thumb,
            "url": f"https://inkbunny.net/s/{self.submission_id}",
            "description": self.description,
            # Flatten Keyword objects to a plain list of tag name strings
            "keywords": [k.keyword_name for k in self.keywords],
            "page_count": int(self.pagecount),
            "views": int(self.views),
            "favorites_count": int(self.favorites_count),
            "comments_count": int(self.comments_count),
        }


class FavingUser(BaseModel):
    """A user who has favourited a submission."""
    user_id: str       # String because Inkbunny API returns it as a string
    username: str = ""


class FavingUsersResponse(BaseModel):
    """Response from api_submissionfavingusers.php."""
    favingusers: list[FavingUser] = []
