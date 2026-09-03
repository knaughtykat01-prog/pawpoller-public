"""The table ownership registry — mirroring Stage 3's allow-list.

§1 of ``docs/specs/desktop_server_mirroring.md`` says every table gets exactly
one class and that "the implementation must carry it as a literal registry and
fail closed on any table not listed". This module is that registry.

**Why an allow-list and not a deny-list.** ``SYNC_EXCLUDE`` in ``config.py`` is
a deny-list, and that is precisely how ``auth_api_keys`` shipped as a live
lockout in 3.5.3: a new key was synced because nobody remembered to exclude it.
A deny-list fails open, and failing open here means a table nobody thought
about crossing machines. So an unregistered table is never synced, and
:func:`audit` turns "somebody added a table and forgot this file" into a test
failure rather than a data incident.

## The classes

``SRV``   server-authoritative. Travels server → desktop only, and wholesale —
          the desktop cannot originate a poll snapshot, so there is nothing to
          merge. Stage 1 already carries these inside the database snapshot.

``SHR``   shared. Both installs can legitimately write these. Down is covered
          by Stage 1's wholesale snapshot; **up** is this stage's natural-key
          upsert (``mirror/shr.py``).

``DER``   derived. Recomputed locally from data that does travel, so shipping
          it is pure cost.

``LOC``   local-only. Machine-specific state where sharing is actively harmful.

``HANDOFF`` crosses only through Stage 2's claim protocol, never in bulk. This
          class is **not in the spec** — see below.

## The one place this file departs from the spec

§1 lists ``posting_queue`` and ``posting_log`` as SHR. §0.2 of the same
document says, of the queue: *"mirroring ``posting_queue`` would double-post to
live platforms"*. Both cannot be true, and §0.2 is the one backed by a
mechanism: both installs run the posting scheduler, so a queue row present on
both boxes is a row two schedulers will try to execute. Stage 0's atomic claim
makes that safe *within* one database; it cannot help across two, which is
exactly why Stage 2 moves queue rows one at a time behind a claim taken on the
server. A bulk upsert would also resurrect cancelled jobs (cancellation is a
row state, and additive-only sync cannot express it) and re-offer rows already
claimed.

``posting_log`` fails differently but as decisively: it is append-only history
with no unique constraint, so a bulk upsert duplicates every row on every push
— §0.5's snapshot-duplication problem in miniature — and Stage 2's
``apply_result`` already writes the server-side log row when a desktop result
lands, so the rows that matter arrive by the narrow channel anyway.

Both therefore get their own class rather than being quietly dropped: the point
of a fail-closed registry is that "not synced" is a decision with a reason
attached, not an omission.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# The 19 platforms that own a submissions/snapshots/poll_log trio. Inkbunny is
# the unprefixed one — the app began as an Inkbunny analytics tool and those
# tables kept their original names.
PLATFORM_PREFIXES = (
    "", "ao3_", "bsky_", "da_", "e621_", "fa_", "fbr_", "fn_", "ig_", "ik_",
    "mast_", "pix_", "sf_", "sqw_", "thr_", "tum_", "tw_", "wp_", "ws_",
)

SRV = "SRV"
SHR = "SHR"
DER = "DER"
LOC = "LOC"
HANDOFF = "HANDOFF"

# How a delete on this table is treated when it happens locally.
#   additive  — deletes are not expressed at all; the row simply stays upstream
#   tombstone — a DELETE trigger records it and the push replays it (§D5)
#   surface   — reported to the operator for confirmation, never auto-applied
ADDITIVE = "additive"
TOMBSTONE = "tombstone"
SURFACE = "surface"

# How an incoming row is applied on the receiving (server) side.
#   upsert      — insert new rows, update existing ones
#   insert_only — insert rows whose natural key is absent; never overwrite
UPSERT = "upsert"
INSERT_ONLY = "insert_only"


@dataclass(frozen=True)
class TableRule:
    """One table's place in the mirror. ``reason`` is not decoration — it is
    the thing a future reader needs when they are deciding whether a new table
    belongs in the same class."""

    name: str
    ownership: str
    reason: str
    key: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    deletes: str = ADDITIVE
    upward: str = UPSERT
    lazy: bool = False  # created on first use; absence is not an error

    @property
    def syncs_upward(self) -> bool:
        return self.ownership == SHR


def _srv(name: str, reason: str, **kw) -> TableRule:
    return TableRule(name, SRV, reason, **kw)


_RULES: list[TableRule] = []

# ── SRV (68) — server-authoritative telemetry ─────────────────
# Nothing here merges row-wise. ~33,500 of the ~56,000 rows in a live database
# are `*_snapshots` with no unique constraint, so a row-wise merge would
# duplicate every one of them on every sync and permanently distort the
# analytics the app exists to produce (§0.5).
for _p in PLATFORM_PREFIXES:
    _rows = f"{_p}submissions" if _p else "submissions"
    _snap = f"{_p}snapshots" if _p else "snapshots"
    _log = f"{_p}poll_log" if _p else "poll_log"
    _RULES += [
        _srv(_rows, "Poll output. The desktop cannot originate a submission row."),
        _srv(_snap, "Time series with no unique constraint — merging duplicates history."),
        _srv(_log, "Per-install poll bookkeeping; the two installs poll at different times."),
    ]

_RULES += [
    _srv("comments", "Poll output (Inkbunny)."),
    _srv("fa_comments", "Poll output (FurAffinity)."),
    _srv("platform_comments", "Poll output, all platforms."),
    _srv("faving_users", "Poll output."),
    _srv("watchers", "Poll output (Inkbunny)."),
    _srv("fa_watchers",
         "Poll output. `notified` is notification-dedup state (fa_queries.py) — "
         "a merged notified=0 row re-notifies the user for a watcher they have "
         "already seen, so it never crosses even inside a wholesale snapshot.",
         exclude=("notified",)),
    _srv("sf_watchers", "Poll output (SoFurry)."),
    _srv("ao3_kudos_users", "Poll output (AO3)."),
    _srv("sqw_kudos_users", "Poll output (SquidgeWorld)."),
    _srv("fa_profile_stats", "Poll output (FurAffinity profile counters)."),
    _srv("account_follower_snapshots", "Time series; same reasoning as *_snapshots."),
]

# ── SHR (24) — both installs write these ──────────────────────
# Down is Stage 1's wholesale snapshot. Up is mirror/shr.py, keyed on content.
# `key` names the natural key as it travels, which is not always a column list:
# where a surrogate id is part of the local key it is replaced by the referent's
# own natural key (`account_id` → the account's handle, `post_id` → the post's
# content key). Nothing in `key` is ever a surrogate id.
_RULES += [
    TableRule(
        "accounts", SHR,
        "The identity table. Travels as (platform, handle) — never as account_id, "
        "which spans ~75 columns, sits inside publications' UNIQUE key, and is "
        "allocated independently by seed_default_accounts on each box (the "
        "2026-08-12 corruption).",
        key=("platform", "handle"), exclude=("account_id", "persona_id"),
    ),
    TableRule(
        "personas", SHR,
        "User-authored grouping. `name` is how the UI identifies one.",
        key=("name",), exclude=("persona_id",),
    ),
    TableRule(
        "tg_submissions", SHR,
        "Telegram posts PawPoller sent. NOT poll output, which is why it cannot "
        "join the PLATFORM_PREFIXES loop: every other *_submissions table is "
        "filled by asking a site what we published, so the desktop can never "
        "originate a row. Here the desktop CAN — it writes one whenever it posts "
        "to a channel — and a wholesale server->desktop pull would destroy any "
        "row the desktop made and never sent. The natural key is stable across "
        "installs (a chat id plus a message id are Telegram's own, not ours), so "
        "this travels upward safely. Insert-only: reaction counts are owned by "
        "whichever machine holds the update stream, so an upward update could "
        "only ever overwrite fresher counts with staler ones.",
        key=("chat_id", "message_id"),
        exclude=("account_id",), upward=INSERT_ONLY,
    ),
    _srv(
        "tg_snapshots",
        "Reaction time series with no unique constraint — merging duplicates "
        "history, exactly as for every other *_snapshots table. Safe as SRV "
        "because reaction ingest is ownership-gated to one machine (4.0.10), so "
        "only the polling owner ever writes these.",
    ),
    _srv(
        "tg_poll_log",
        "Per-install poll bookkeeping, same as every other *_poll_log — the two "
        "installs poll at different times, so merging them would produce a "
        "history neither machine actually had. Registered by hand rather than "
        "through PLATFORM_PREFIXES because Telegram's submissions table is SHR, "
        "not SRV, so the trio cannot be generated as one.",
    ),
    TableRule(
        "publications", SHR,
        "The registry of what is posted where. Insert-only upward: after a Stage 1 "
        "pull the desktop's copy is the server's, so an upward *update* can only "
        "ever be a stale copy overwriting fresher analytics. Genuinely "
        "desktop-originated rows already arrive through Stage 2's apply_result; "
        "this catches anything that channel missed.",
        key=("content_type", "story_name", "chapter_index", "platform", "account"),
        exclude=("pub_id", "account_id"), upward=INSERT_ONLY,
    ),
    TableRule(
        "posts", SHR,
        "Microblog content, authored on either box. No natural key exists in the "
        "schema, so identity is (created_at, sha256(body)) — the two facts about a "
        "post that its author fixes at creation and never edits together.",
        key=("created_at", "body_sha256"), exclude=("post_id", "parent_post_id"),
    ),
    TableRule(
        "post_media", SHR,
        "Ordered attachments of a post; `path` names a file Stage 1 already mirrors.",
        key=("post", "ordinal"), exclude=("id", "post_id"),
    ),
    TableRule(
        "post_publications", SHR,
        "Where a post went live. Insert-only for the same reason as publications.",
        key=("post", "platform", "account"), exclude=("id", "post_id", "account_id"),
        upward=INSERT_ONLY,
    ),
    TableRule(
        "post_mentions", SHR,
        "Alias→contact bindings. `contact_id` travels as the contact's name.",
        key=("post", "token"), exclude=("id", "post_id", "contact_id"),
    ),
    TableRule(
        "post_contacts", SHR,
        "The address book. `name` is the @alias the author types, so it is the key.",
        key=("name",), exclude=("id",),
    ),
    TableRule(
        "masterpieces", SHR,
        "`name` is UNIQUE and content-derived. `source_link_id` points at the "
        "legacy submission_links surrogate and is dropped rather than remapped.",
        key=("name",), exclude=("id", "source_link_id"),
    ),
    TableRule(
        "masterpiece_members", SHR,
        "Already naturally keyed — the spec calls this the model for the rest. "
        "Deletes are RECORDED but SURFACED, not applied: unlinking a platform "
        "upload changes what a piece is recorded as being, and the standing "
        "project rule is that anything in that neighbourhood shows the list and "
        "waits for a yes (§D5's 'no automatic propagation of an artwork "
        "deletion'). The push carries them; the operator confirms them.",
        key=("masterpiece_name", "platform", "submission_id"),
        exclude=("account_id",), deletes=SURFACE,
    ),
    TableRule(
        "masterpiece_not_duplicate", SHR,
        "A human judgment that two pieces are distinct. Losing it re-suggests a "
        "merge the operator already refused.",
        key=("name_a", "name_b"),
    ),
    TableRule(
        "masterpiece_not_variant", SHR,
        "As above, for variant grouping. Created lazily by variant_suggest.py, so "
        "it may be absent on either side — the registry tolerates that and the "
        "exporter treats absence as an empty table.",
        key=("name_a", "name_b"), lazy=True,
    ),
    TableRule(
        "collections", SHR,
        "User-authored container. Keyed on `name`; the schema does not enforce "
        "uniqueness, so an ambiguous name is skipped and reported rather than "
        "guessed at.",
        key=("name",), exclude=("id", "source_link_id"),
    ),
    TableRule(
        "collection_members", SHR,
        "member_ref holds a stringified post_id when member_type='post' — one of "
        "the two integer FKs hiding in TEXT columns (§D2). It is rewritten to the "
        "post's content key on the way out and back on the way in.",
        key=("collection_name", "member_type", "member_ref"),
        exclude=("collection_id",), deletes=TOMBSTONE,
    ),
    TableRule(
        "submission_groups", SHR, "User-authored grouping; `name` identifies it.",
        key=("name",), exclude=("group_id",),
    ),
    TableRule(
        "submission_group_members", SHR, "Membership, keyed through the group's name.",
        key=("group_name", "platform", "submission_id"), exclude=("id", "group_id"),
    ),
    TableRule(
        "submission_links", SHR,
        "Legacy (superseded by collections and masterpieces). The table is nothing "
        "but an id and a timestamp, so a link has no natural key of its own — its "
        "identity IS its member set, and that is what travels.",
        key=("members",), exclude=("link_id",),
    ),
    TableRule(
        "submission_link_members", SHR,
        "Carried inside its parent link rather than separately; a member row has "
        "no meaning apart from the set it belongs to.",
        key=("link", "platform", "submission_id"), exclude=("id", "link_id"),
    ),
    TableRule(
        "tags", SHR, "`name` is UNIQUE.", key=("name",), exclude=("tag_id",),
    ),
    TableRule(
        "submission_tags", SHR, "Keyed through the tag's name, not its id.",
        key=("tag_name", "platform", "submission_id"), exclude=("id", "tag_id"),
    ),
    TableRule(
        "artists", SHR,
        "The artist registry (3.10.0). Reference data the user maintains, and "
        "`artist_key` is already a natural key — it is derived from the name, "
        "not allocated. Deletes are NOT recorded: losing an artist row loses "
        "every handle researched for them, and a stale artist credits nobody "
        "wrongly, it just sits there.",
        key=("artist_key",),
    ),
    TableRule(
        "artist_handles", SHR,
        "Where each artist is on each platform (3.10.0). Naturally keyed on "
        "(artist_key, platform). Additive by design: a handle added by hand on "
        "one box for a platform the lookup never resolved must survive a sync "
        "from the other, which is the same merge rule `upsert_artist` enforces.",
        key=("artist_key", "platform"),
    ),
    TableRule(
        "ignored_submissions", SHR,
        "Already naturally keyed. Un-ignoring is meaningful, so it tombstones.",
        key=("platform", "submission_id"), deletes=TOMBSTONE,
    ),
    TableRule(
        "inbox_state", SHR,
        "Which comments have been handled. Already naturally keyed; un-handling is "
        "meaningful, so it tombstones.",
        key=("platform", "comment_id"), deletes=TOMBSTONE,
    ),
    TableRule(
        "commissions", SHR,
        "User-authored records. No natural key in the schema; (client_name, "
        "created_at) is the pair fixed at creation. Deletes are not carried: "
        "`archived` is how a finished commission leaves the board, so a genuine "
        "delete is rare enough that additive-only costs nothing and saves a "
        "fifth trigger.",
        key=("client_name", "created_at"), exclude=("id",),
    ),
    TableRule(
        "goals", SHR,
        "A target the operator set. Two goals with the same platform, scope, "
        "subject, metric and target are the same goal.",
        key=("platform", "scope", "submission_id", "metric", "target_value"),
        exclude=("goal_id",),
    ),
]

# ── HANDOFF (2) — Stage 2's channel, never bulk ───────────────
_RULES += [
    TableRule(
        "posting_queue", HANDOFF,
        "Both installs run the scheduler, so a queue row on both boxes is a row "
        "two schedulers execute — §0.2's double-post. Stage 2 moves these one at a "
        "time behind a claim taken on the server. A bulk upsert would also "
        "resurrect cancelled jobs, which additive sync cannot express.",
    ),
    TableRule(
        "posting_log", HANDOFF,
        "Append-only history with no unique constraint: a bulk upsert duplicates "
        "every row on every push. Stage 2's apply_result already writes the "
        "server-side row when a desktop result lands.",
    ),
]

# ── DER (1) ───────────────────────────────────────────────────
_RULES += [
    TableRule(
        "image_hashes", DER,
        "Fully recomputed by hash_masterpieces, which also prunes. Drop and "
        "rebuild locally; never merge.",
    ),
]

# ── LOC (4, plus this stage's own outbox) ─────────────────────
_RULES += [
    TableRule(
        "session_cache", LOC,
        "Live platform session ids. Inkbunny binds a sid to the IP that created "
        "it, so two installs presenting the same sid invalidate each other.",
    ),
    TableRule(
        "share_tokens", LOC,
        "Only resolvable on the box serving /share/{token}.",
    ),
    TableRule(
        "pp_meta", LOC,
        "Migration guards. Syncing one either suppresses a backfill this install "
        "still needs or re-runs a destructive one it has already done.",
    ),
    TableRule(
        "sqlite_sequence", LOC,
        "SQLite's own AUTOINCREMENT allocator. Syncing it corrupts both sides.",
    ),
    TableRule(
        "mirror_tombstones", LOC,
        "This install's outbox of deletes awaiting delivery. It describes rows "
        "that left THIS database; sending it as data would replay one install's "
        "outbox as the other's.",
    ),
]

REGISTRY: dict[str, TableRule] = {r.name: r for r in _RULES}

# Applied in this order so a row's referents exist before it does. Getting this
# wrong shows up as a member row skipped for a missing parent, which is
# recoverable on the next push but reports as noise in the meantime.
SHR_ORDER: tuple[str, ...] = (
    "personas", "accounts", "tags", "post_contacts",
    "posts", "post_media", "post_publications", "post_mentions",
    "masterpieces", "masterpiece_members",
    "masterpiece_not_duplicate", "masterpiece_not_variant",
    "publications",
    # After accounts: a tg row carries its account by natural key.
    "tg_submissions",
    "collections", "collection_members",
    "submission_groups", "submission_group_members",
    "submission_links", "submission_link_members",
    "submission_tags", "ignored_submissions", "inbox_state",
    "commissions", "goals",
    # Artists before handles: a handle row is meaningless without its artist.
    "artists", "artist_handles",
)

# Deletes are RECORDED for both of these classes — you cannot surface a
# deletion you never noticed — and differ only in what the receiver does with
# one. Keep this in step with ``mirror/tombstones._TOMBSTONED``, which creates
# the triggers; the test suite asserts the two agree.
TOMBSTONE_TABLES: tuple[str, ...] = tuple(
    r.name for r in _RULES if r.deletes in (TOMBSTONE, SURFACE)
)

AUTO_DELETE_TABLES: tuple[str, ...] = tuple(
    r.name for r in _RULES if r.deletes == TOMBSTONE
)


class UnregisteredTable(Exception):
    """A table exists in the database but not in this registry.

    Raised rather than defaulted, because every default is wrong: defaulting to
    "sync it" is the 3.5.3 lockout, and defaulting to "don't" hides a table
    that should have been carried.
    """


def rule_for(table: str) -> TableRule:
    try:
        return REGISTRY[table]
    except KeyError:
        raise UnregisteredTable(
            f"{table!r} is not in the mirror registry. Classify it in "
            f"mirror/registry.py before it can cross machines — see §1 of "
            f"docs/specs/desktop_server_mirroring.md."
        ) from None


def tables_in_class(ownership: str) -> tuple[str, ...]:
    return tuple(r.name for r in _RULES if r.ownership == ownership)


def audit(conn: sqlite3.Connection) -> dict:
    """Compare a live database against the registry.

    ``unregistered`` is the one that matters: a table the code creates and this
    file has never heard of. It is returned rather than raised so a caller can
    report every one of them at once, and the test suite asserts it is empty.

    ``missing`` (registered but absent) is informational — the platform tables
    are created on demand and ``masterpiece_not_variant`` is created lazily by
    design, so an absence is normal on a young database.
    """
    live = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }
    registered = set(REGISTRY)
    unregistered = sorted(t for t in live - registered if not t.startswith("sqlite_"))
    # sqlite_sequence is registered deliberately (the spec names it) but any
    # other sqlite_* table is SQLite's own bookkeeping and not ours to classify.
    missing = sorted(t for t in registered - live if not REGISTRY[t].lazy)
    return {
        "live": len(live),
        "registered": len(registered),
        "unregistered": unregistered,
        "missing": missing,
        "counts": {c: len(tables_in_class(c))
                   for c in (SRV, SHR, DER, LOC, HANDOFF)},
    }
