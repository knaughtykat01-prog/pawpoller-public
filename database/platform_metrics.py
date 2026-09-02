"""Canonical platform METRIC registry — the Python twin of frontend/js/platforms.js.

Single source of truth for "which table holds platform X's stats, and what are
its metric columns actually called". Before this module the same knowledge was
hand-declared in **fifteen** places (analytics_queries ×3, collections_queries,
group_queries, posting_queries, routes/api, routes/posting_api, polling/telegram
×2, posting/sync, database/accounts, cli, plus four more blocks in app.js), and
they had drifted badly:

  * ``posting_queries.stat_tables`` asked AO3/SqW for ``hits``/``kudos``. Those
    columns do not exist — the OTW archives store ``views``/``favorites_count``
    like everyone else; only the *site vocabulary* differs. The query raised
    ``no such column: hits``, a bare ``except Exception: continue`` swallowed
    it, and every AO3 + SquidgeWorld publication silently reported no stats.
    That is the incident this module exists to prevent.
  * Six of the fifteen never learned about FurryNetwork/Furbooru (2.200/2.201),
    two never learned about e621/Pixiv/Instagram/Mastodon/Threads/Tumblr, and
    ``database.accounts`` sniffed for ``views``/``favorites_count`` only — so
    personas holding Twitter/e621/Itaku/Bluesky/Mastodon/Tumblr accounts
    under-counted in the "By persona" roll-up.
  * The link/masterpiece/group roll-ups mapped e621's ``score`` into their
    *views* slot, summing net upvotes as if they were page views (and e621
    scores can be NEGATIVE, so the total could go down).

Two rules keep this file honest:

  1. Declare the column names the SCHEMA uses, never the ones the site's UI
     uses. AO3 calls them hits and kudos; the table says views and
     favorites_count. Site vocabulary belongs in ``labels`` — display only,
     never interpolated into SQL.
  2. ``tests/test_platform_metrics.py`` PRAGMA-checks every column declared
     here against a freshly-built schema, and asserts the JS registry lists the
     same codes. A registry deduplicates a WRONG column name just as happily as
     a right one; the test is what makes it safe.

Metric families
---------------
``views``       has a real view/read counter (ib, fa, ws, sf, sqw, ao3, da,
                wp, tw, pix, thr, ig, fn)
``score``       headline metric is a net up−down score that may be negative
                (e621, fbr — the Philomena/booru family). NEVER summed into a
                view total.
``engagement``  no view counter at all; likes/notes are the only signal
                (ik, bsky, mast, tum)

Canonical keys
--------------
Callers should read the four canonical keys — ``views``, ``score``, ``faves``,
``comments`` — rather than raw column names. ``read_stats`` returns those PLUS
the platform's raw column names, so consumers written against the old shape
(``stats["favorites_count"]``) keep working during the migration.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Canonical metric keys every consumer can rely on, whatever the platform.
CANONICAL_KEYS = ("views", "score", "faves", "comments")

# Chunk size for batched id lookups — stays under SQLite's 999 variable cap.
_CHUNK = 900


@dataclass(frozen=True)
class PlatformMetrics:
    """How one platform's stats are stored and what they're called.

    ``views``/``score``/``faves``/``comments`` are COLUMN NAMES (or None when
    the platform has no such metric). ``extra`` lists further metric columns
    worth charting or trending. ``labels`` maps a canonical key to the wording
    the platform's own users expect.
    """
    code: str
    label: str
    table: str
    snapshots: str
    family: str                       # "views" | "score" | "engagement"
    views: str | None = None
    score: str | None = None
    faves: str | None = None
    comments: str | None = None
    id_col: str = "submission_id"
    extra: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def columns(self) -> list[str]:
        """Every metric column this platform stores, deduplicated, in a stable
        order. Safe to interpolate into SQL: these are literals from this file,
        never user input."""
        seen, out = set(), []
        for c in (self.views, self.score, self.faves, self.comments, *self.extra):
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def label_for(self, key: str) -> str:
        """Display wording for a canonical key — "Hits" on AO3, "Notes" on
        Tumblr, "Score" on e621. Falls back to a sensible default."""
        return self.labels.get(key, _DEFAULT_LABELS.get(key, key.title()))

    def canonical(self, row) -> dict:
        """Project a submissions/snapshots row onto the canonical keys, and
        carry the raw column values through for back-compat."""
        r = dict(row) if not isinstance(row, dict) else row
        out = {
            "views": r.get(self.views) if self.views else None,
            "score": r.get(self.score) if self.score else None,
            "faves": r.get(self.faves) if self.faves else None,
            "comments": r.get(self.comments) if self.comments else None,
        }
        for c in self.columns:
            if c in r:
                out[c] = r[c]
        return out


_DEFAULT_LABELS = {
    "views": "Views", "score": "Score", "faves": "Favourites", "comments": "Comments",
}

# ── The registry ──────────────────────────────────────────────────────────────
# Column names verified against the live schema. Inkbunny is the original
# platform, so its tables are the unprefixed `submissions` / `snapshots`.
_REGISTRY: tuple[PlatformMetrics, ...] = (
    PlatformMetrics(
        code="ib", label="Inkbunny", table="submissions", snapshots="snapshots",
        family="views", views="views", faves="favorites_count", comments="comments_count",
    ),
    PlatformMetrics(
        code="fa", label="FurAffinity", table="fa_submissions", snapshots="fa_snapshots",
        family="views", views="views", faves="favorites_count", comments="comments_count",
    ),
    PlatformMetrics(
        code="ws", label="Weasyl", table="ws_submissions", snapshots="ws_snapshots",
        family="views", views="views", faves="favorites_count", comments="comments_count",
    ),
    PlatformMetrics(
        code="sf", label="SoFurry", table="sf_submissions", snapshots="sf_snapshots",
        family="views", views="views", faves="favorites_count", comments="comments_count",
    ),
    # OTW archives (SquidgeWorld + AO3): the SITE says hits/kudos/bookmarks, the
    # SCHEMA says views/favorites_count/bookmarks_count. Declaring the site's
    # vocabulary here is exactly the bug this registry replaces — keep it in
    # `labels`, which never reaches SQL.
    PlatformMetrics(
        code="sqw", label="SquidgeWorld", table="sqw_submissions", snapshots="sqw_snapshots",
        family="views", views="views", faves="favorites_count", comments="comments_count",
        extra=("bookmarks_count",),
        labels={"views": "Hits", "faves": "Kudos"},
    ),
    PlatformMetrics(
        code="ao3", label="AO3", table="ao3_submissions", snapshots="ao3_snapshots",
        family="views", views="views", faves="favorites_count", comments="comments_count",
        extra=("bookmarks_count",),
        labels={"views": "Hits", "faves": "Kudos"},
    ),
    PlatformMetrics(
        code="da", label="DeviantArt", table="da_submissions", snapshots="da_snapshots",
        family="views", views="views", faves="favorites_count", comments="comments_count",
        extra=("downloads",),
    ),
    PlatformMetrics(
        code="wp", label="Wattpad", table="wp_submissions", snapshots="wp_snapshots",
        family="views", views="reads", faves="votes", comments="comments_count",
        labels={"views": "Reads", "faves": "Votes"},
    ),
    PlatformMetrics(
        code="ik", label="Itaku", table="ik_submissions", snapshots="ik_snapshots",
        family="engagement", faves="likes", comments="comments_count",
        labels={"faves": "Likes"},
    ),
    PlatformMetrics(
        code="bsky", label="Bluesky", table="bsky_submissions", snapshots="bsky_snapshots",
        family="engagement", faves="likes", comments="replies",
        extra=("reposts", "quotes"),
        labels={"faves": "Likes", "comments": "Replies"},
    ),
    PlatformMetrics(
        code="tw", label="X / Twitter", table="tw_submissions", snapshots="tw_snapshots",
        family="views", views="views", faves="likes", comments="replies",
        extra=("retweets", "quotes", "bookmarks"),
        labels={"faves": "Likes", "comments": "Replies"},
    ),
    PlatformMetrics(
        code="mast", label="Mastodon", table="mast_submissions", snapshots="mast_snapshots",
        family="engagement", faves="likes", comments="replies",
        extra=("reposts", "quotes"),
        labels={"faves": "Likes", "comments": "Replies"},
    ),
    # Tumblr collapses likes + reblogs + replies into one "notes" number, so it
    # has a faves metric and nothing else.
    PlatformMetrics(
        code="tum", label="Tumblr", table="tum_submissions", snapshots="tum_snapshots",
        family="engagement", faves="notes",
        labels={"faves": "Notes"},
    ),
    PlatformMetrics(
        code="pix", label="Pixiv", table="pix_submissions", snapshots="pix_snapshots",
        family="views", views="views", faves="favorites_count", comments="comments_count",
    ),
    PlatformMetrics(
        code="thr", label="Threads", table="thr_submissions", snapshots="thr_snapshots",
        family="views", views="views", faves="likes", comments="replies",
        extra=("reposts", "quotes"),
        labels={"faves": "Likes", "comments": "Replies"},
    ),
    PlatformMetrics(
        code="ig", label="Instagram", table="ig_submissions", snapshots="ig_snapshots",
        family="views", views="views", faves="likes", comments="comments",
        extra=("reach", "shares"),
        labels={"faves": "Likes"},
    ),
    # Booru family (Philomena/e621): SCORE model, up−down, may be negative.
    # `views` stays None so no aggregate ever folds a score into a view count.
    PlatformMetrics(
        code="e621", label="e621", table="e621_submissions", snapshots="e621_snapshots",
        family="score", score="score", faves="favorites_count", comments="comments_count",
        extra=("up_score", "down_score"),
    ),
    PlatformMetrics(
        code="fn", label="FurryNetwork", table="fn_submissions", snapshots="fn_snapshots",
        family="views", views="views", faves="favorites_count", comments="comments_count",
    ),
    PlatformMetrics(
        code="fbr", label="Furbooru", table="fbr_submissions", snapshots="fbr_snapshots",
        family="score", score="score", faves="favorites_count", comments="comments_count",
        extra=("up_score", "down_score"),
    ),
)

BY_CODE: dict[str, PlatformMetrics] = {p.code: p for p in _REGISTRY}
ALL_CODES: tuple[str, ...] = tuple(p.code for p in _REGISTRY)

# Codes grouped by family — lets aggregates decide what may legitimately be
# summed together instead of guessing from column names.
VIEW_PLATFORMS = tuple(p.code for p in _REGISTRY if p.family == "views")
SCORE_PLATFORMS = tuple(p.code for p in _REGISTRY if p.family == "score")
ENGAGEMENT_PLATFORMS = tuple(p.code for p in _REGISTRY if p.family == "engagement")


def get(code: str) -> PlatformMetrics | None:
    """Registry entry for a platform code, or None if unknown."""
    return BY_CODE.get(code)


def table_for(code: str) -> str | None:
    spec = BY_CODE.get(code)
    return spec.table if spec else None


def snapshots_for(code: str) -> str | None:
    spec = BY_CODE.get(code)
    return spec.snapshots if spec else None


def columns_for(code: str) -> list[str]:
    """Metric columns stored for a platform ([] for an unknown code)."""
    spec = BY_CODE.get(code)
    return spec.columns if spec else []


def metric_triple(code: str) -> tuple[str | None, str | None, str | None]:
    """(views_col, faves_col, comments_col) — the shape the older roll-ups used.

    Score-model platforms return None for views ON PURPOSE: a net score is not
    a view count and must not be summed into one.
    """
    spec = BY_CODE.get(code)
    if not spec:
        return (None, None, None)
    return (spec.views, spec.faves, spec.comments)


def read_stats(conn: sqlite3.Connection, code: str, ids) -> dict[str, dict]:
    """Batched stat lookup: ``{str(submission_id): {canonical + raw keys}}``.

    One query per chunk of ids rather than one per publication (the perf
    guardrail from 2.165.0). Digit-like ids are compared as ints because some
    submission tables store the id as INTEGER.

    A missing table or column is logged at WARNING and yields an empty result —
    never a silent empty dict. The bare ``except: continue`` this replaces is
    what hid the AO3/SqW breakage for months.
    """
    spec = BY_CODE.get(code)
    if not spec:
        logger.warning("read_stats: no registry entry for platform %r", code)
        return {}
    cols = spec.columns
    id_list = [str(i) for i in ids if i not in (None, "")]
    if not cols or not id_list:
        return {}

    out: dict[str, dict] = {}
    col_str = ", ".join(cols)
    for i in range(0, len(id_list), _CHUNK):
        chunk = id_list[i:i + _CHUNK]
        norm = [int(x) if x.isdigit() else x for x in chunk]
        ph = ",".join("?" * len(norm))
        try:
            rows = conn.execute(
                f"SELECT {spec.id_col}, {col_str} FROM {spec.table}"
                f" WHERE {spec.id_col} IN ({ph})",
                norm,
            ).fetchall()
        except sqlite3.Error as e:
            # Loud, and only once per platform per call — a schema/registry
            # mismatch is a bug, not a normal condition.
            logger.warning(
                "read_stats: %s stats unavailable (table=%s cols=%s): %s",
                code, spec.table, col_str, e,
            )
            return out
        for r in rows:
            out[str(r[spec.id_col])] = spec.canonical(r)
    return out


def pooled(entries) -> dict:
    """Sum an iterable of ``(platform_code, stats_dict)`` into one canonical
    total, keeping the metric families apart.

    ``views`` sums only across view-model platforms; ``score`` is carried
    separately so e621/Furbooru never inflate (or deflate) a view count.
    ``faves``/``comments`` are comparable enough across platforms to pool.
    """
    total = {"views": 0, "score": 0, "faves": 0, "comments": 0}
    for code, stats in entries:
        if not stats:
            continue
        spec = BY_CODE.get(code)
        if not spec:
            continue
        if spec.family == "score":
            total["score"] += _value(spec, stats, "score")
        else:
            total["views"] += _value(spec, stats, "views")
        total["faves"] += _value(spec, stats, "faves")
        total["comments"] += _value(spec, stats, "comments")
    return total


def _value(spec: PlatformMetrics, stats: dict, key: str) -> int:
    """Read one canonical metric out of a stats dict.

    Accepts either shape: a canonical dict from ``read_stats``/``canonical``
    (``{"faves": 7}``) or a raw row keyed by the platform's own column names
    (``{"favorites_count": 7}``). The dataclass field for each canonical key IS
    that platform's column name, so one getattr covers both.
    """
    v = stats.get(key)
    if v is None:
        col = getattr(spec, key, None)
        if col:
            v = stats.get(col)
    return v or 0
