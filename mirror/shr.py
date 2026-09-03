"""The upward shared-table channel — mirroring Stage 3.

Stage 1 gives the desktop a wholesale copy of the server's database. Stage 2
gives one desktop-executed post a way home. This stage closes the remaining
gap: **everything else the desktop legitimately authors in a shared table.**

## Why this is an upward channel, not a two-way merge

§1 of the spec classes 25 tables SHR — "both" directions — and read on its own
that suggests a symmetric merge. It is not, once Stage 1 exists. Stage 1
replaces the desktop's database wholesale from a server snapshot, so the
downward direction for a shared table is *already carried*, in the one form
that cannot duplicate a snapshot row or misresolve an id. What is missing is
the other half, and it is missing in a way that loses data:

    a Stage 1 pull overwrites anything the desktop wrote and never sent.

A persona renamed on the desktop, a collection built there, a submission
ignored there, an inbox comment marked handled there — every one of those is
discarded by the next pull. So Stage 3 is: **push the shared tables up in
natural keys, then pull.** That ordering is not a convention, it is the
correctness condition, and ``routes/mirror_api.py`` enforces it by making the
push a phase of the pull rather than a button beside it.

## Identity

Nothing crosses as a surrogate id (§D2). Where a local key contains one it is
replaced by the referent's own natural key:

    account_id   →  the account's handle, resolved through
                    accounts.resolve_account_by_identity on arrival
    post_id      →  (created_at, sha256(body)[:16]) — the two facts an author
                    fixes when the post is created
    tag_id       →  the tag's name
    group_id     →  the group's name
    collection_id→  the collection's name
    link_id      →  the link's own member set, because submission_links is
                    nothing but an id and a timestamp: a link has no identity
                    apart from what it links

The two integer FKs that hide inside TEXT columns (§D2) are rewritten
explicitly rather than left to a schema-driven remapper that cannot see them:
``collection_members.member_ref`` holds a stringified ``post_id`` when
``member_type='post'``. (The other, ``posting_queue``/``posting_log``'s
``story_name``, belongs to two tables this stage deliberately does not carry —
see ``mirror/registry.py``.)

## Two application rules, not one

Most tables **upsert**: the desktop is a legitimate co-author and its version
is news. Two are **insert-only** — ``publications`` and ``post_publications``.
After a Stage 1 pull the desktop's copy of those *is* the server's, so an
upward update can only ever be a stale copy landing on top of fresher
analytics; and the rows the desktop genuinely originates already arrive through
Stage 2's ``apply_result``. Insert-only makes this channel a safety net for
those two rather than a second, worse writer.

## Cost

The shared tables are small — the bulk of a live database is the ~33,500
``*_snapshots`` rows, none of which are here. So the push exports the whole
shared set every time and relies on the upsert being idempotent, rather than
carrying change-tracking machinery whose failure mode is a silently missed row.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone

from database import accounts as accounts_db
from mirror import registry, tombstones

logger = logging.getLogger(__name__)

BUNDLE_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def _get(row, key, default=None):
    """sqlite3.Row has no .get(); several of these columns are migration-added."""
    try:
        return row[key] if key in row.keys() else default
    except (IndexError, KeyError):
        return default


def _s(v) -> str:
    return "" if v is None else str(v)


# ── Identity helpers ──────────────────────────────────────────

def post_key(created_at, body) -> list[str]:
    """A post's identity: when it was created and what it says.

    ``post_id`` is AUTOINCREMENT and allocated independently on each install,
    so it means nothing across the boundary. ``created_at`` alone is very
    nearly unique already (second resolution, one author); the body hash
    removes the "nearly" and costs 16 characters. The body is hashed rather
    than sent as the key so a long post does not put its whole text into every
    member row that refers to it.
    """
    digest = hashlib.sha256(_s(body).encode("utf-8")).hexdigest()[:16]
    return [_s(created_at), digest]


def _handle_for(conn: sqlite3.Connection, account_id) -> str:
    """The handle an account travels as. Empty string when it cannot be found —
    the receiver then falls back to the platform default, which is the same
    rule ``resolve_account_by_identity`` applies."""
    if not account_id:
        return ""
    row = conn.execute(
        "SELECT handle FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
    return (row["handle"] or "").strip() if row else ""


def _resolve_account(conn: sqlite3.Connection, platform: str, handle: str):
    return accounts_db.resolve_account_by_identity(conn, platform, handle)


class _Ctx:
    """Local id lookups built during an apply, so a child row can find the
    parent this same bundle just created."""

    def __init__(self) -> None:
        self.posts: dict[tuple, int] = {}
        self.collections: dict[str, int] = {}
        self.groups: dict[str, int] = {}
        self.tags: dict[str, int] = {}
        self.contacts: dict[str, int] = {}
        self.links: dict[tuple, int] = {}
        self.skipped: list[dict] = []

    def skip(self, table: str, why: str, detail=None) -> None:
        self.skipped.append({"table": table, "reason": why, "detail": detail})


# ══ Exporters ═════════════════════════════════════════════════
# Each returns plain JSON-able dicts in natural keys. They never read a
# surrogate id into the output except as an input to resolving one.

def _export_personas(conn):
    return [{
        "name": r["name"], "color": _get(r, "color", ""),
        "sort_order": _get(r, "sort_order", 0),
        "default_platforms": _get(r, "default_platforms", ""),
        "default_rating": _get(r, "default_rating", ""),
        "preferred_post_time": _get(r, "preferred_post_time", ""),
        "created_at": _get(r, "created_at"),
    } for r in _rows(conn, "SELECT * FROM personas ORDER BY sort_order, persona_id")]


def _export_accounts(conn):
    out = []
    for r in _rows(conn, "SELECT * FROM accounts ORDER BY platform, sort_order, account_id"):
        persona = None
        pid = _get(r, "persona_id")
        if pid:
            prow = conn.execute(
                "SELECT name FROM personas WHERE persona_id = ?", (pid,)).fetchone()
            persona = prow["name"] if prow else None
        out.append({
            "platform": r["platform"], "handle": (r["handle"] or "").strip(),
            "label": _get(r, "label", ""), "enabled": int(_get(r, "enabled", 1)),
            "is_default": int(_get(r, "is_default", 0)),
            "sort_order": int(_get(r, "sort_order", 0)),
            "persona": persona, "created_at": _get(r, "created_at"),
        })
    return out


def _export_tags(conn):
    return [{"name": r["name"], "color": _get(r, "color", "")}
            for r in _rows(conn, "SELECT * FROM tags ORDER BY name")]


def _export_post_contacts(conn):
    return [{
        "name": r["name"],
        "handle_bsky": _get(r, "handle_bsky", ""), "handle_tw": _get(r, "handle_tw", ""),
        "handle_mast": _get(r, "handle_mast", ""), "handle_thr": _get(r, "handle_thr", ""),
        "handle_tum": _get(r, "handle_tum", ""), "created_at": _get(r, "created_at"),
    } for r in _rows(conn, "SELECT * FROM post_contacts ORDER BY id")]


def _export_posts(conn):
    rows = _rows(conn, "SELECT * FROM posts ORDER BY parent_post_id, thread_ordinal, post_id")
    by_id = {r["post_id"]: r for r in rows}
    out = []
    for r in rows:
        parent = None
        pid = _get(r, "parent_post_id", 0) or 0
        if pid and pid in by_id:
            parent = post_key(by_id[pid]["created_at"], by_id[pid]["body"])
        out.append({
            "key": post_key(r["created_at"], r["body"]),
            "body": r["body"], "rating": _get(r, "rating", "general"),
            "image_path": _get(r, "image_path", ""), "image_alt": _get(r, "image_alt", ""),
            "created_at": _get(r, "created_at", ""), "updated_at": _get(r, "updated_at", ""),
            "parent": parent, "thread_ordinal": int(_get(r, "thread_ordinal", 0) or 0),
        })
    return out


def _post_keys_by_id(conn) -> dict:
    return {r["post_id"]: post_key(r["created_at"], r["body"])
            for r in _rows(conn, "SELECT post_id, created_at, body FROM posts")}


def _export_post_media(conn):
    keys = _post_keys_by_id(conn)
    out = []
    for r in _rows(conn, "SELECT * FROM post_media ORDER BY post_id, ordinal"):
        key = keys.get(r["post_id"])
        if key is None:
            continue
        out.append({"post": key, "ordinal": int(_get(r, "ordinal", 0) or 0),
                    "path": _get(r, "path", ""), "alt": _get(r, "alt", "")})
    return out


def _export_post_publications(conn):
    keys = _post_keys_by_id(conn)
    out = []
    for r in _rows(conn, "SELECT * FROM post_publications ORDER BY post_id, platform"):
        key = keys.get(r["post_id"])
        if key is None:
            continue
        out.append({
            "post": key, "platform": r["platform"],
            "account": _handle_for(conn, _get(r, "account_id")),
            "status": _get(r, "status", "pending"),
            "external_id": _get(r, "external_id", ""),
            "external_url": _get(r, "external_url", ""),
            "error": _get(r, "error", ""), "created_at": _get(r, "created_at", ""),
        })
    return out


def _export_post_mentions(conn):
    keys = _post_keys_by_id(conn)
    out = []
    for r in _rows(conn, "SELECT * FROM post_mentions ORDER BY post_id, id"):
        key = keys.get(r["post_id"])
        if key is None:
            continue
        crow = conn.execute("SELECT name FROM post_contacts WHERE id = ?",
                            (_get(r, "contact_id"),)).fetchone()
        out.append({"post": key, "token": r["token"],
                    "contact": crow["name"] if crow else ""})
    return out


def _export_masterpieces(conn):
    return [{"name": r["name"], "status": _get(r, "status", ""),
             "created_at": _get(r, "created_at"), "updated_at": _get(r, "updated_at")}
            for r in _rows(conn, "SELECT * FROM masterpieces ORDER BY name")]


def _export_masterpiece_members(conn):
    return [{
        "masterpiece_name": r["masterpiece_name"], "platform": r["platform"],
        "submission_id": _s(r["submission_id"]),
        "account": _handle_for(conn, _get(r, "account_id")),
        "role": _get(r, "role", "crosspost"), "linked_via": _get(r, "linked_via", "manual"),
        "variant_key": _get(r, "variant_key", ""), "added_at": _get(r, "added_at"),
    } for r in _rows(conn, "SELECT * FROM masterpiece_members "
                           "ORDER BY masterpiece_name, platform, submission_id")]


def _export_pairs(conn, table):
    if not _table_exists(conn, table):
        return []
    return [{"name_a": r["name_a"], "name_b": r["name_b"]}
            for r in _rows(conn, f"SELECT name_a, name_b FROM {table} ORDER BY name_a, name_b")]


def _export_publications(conn):
    return [{
        "content_type": _get(r, "content_type", "story") or "story",
        "story_name": r["story_name"], "chapter_index": int(_get(r, "chapter_index", 0) or 0),
        "platform": r["platform"], "account": _handle_for(conn, _get(r, "account_id")),
        "chapter_title": _get(r, "chapter_title", ""),
        "external_id": _get(r, "external_id", ""), "external_url": _get(r, "external_url", ""),
        "format_file": _get(r, "format_file", ""), "file_hash": _get(r, "file_hash", ""),
        "tags_used": _get(r, "tags_used", "[]"), "title_used": _get(r, "title_used", ""),
        "description_used": _get(r, "description_used", ""),
        "rating_used": _get(r, "rating_used", ""), "status": _get(r, "status", "draft"),
        "first_posted_at": _get(r, "first_posted_at"),
        "last_updated_at": _get(r, "last_updated_at"),
        "update_count": int(_get(r, "update_count", 0) or 0),
        "word_count": int(_get(r, "word_count", 0) or 0),
    } for r in _rows(conn, "SELECT * FROM publications ORDER BY pub_id")]


def _export_tg_submissions(conn):
    """Telegram posts this install sent.

    The natural key is Telegram's own — a chat id plus a message id — so unlike
    every other *_submissions table this travels safely between installs
    without carrying a surrogate.

    Reaction columns are exported but applied insert-only on the far side: they
    are owned by whichever machine holds the update stream, so an upward update
    could only ever replace fresher counts with staler ones.
    """
    return [{
        "chat_id": str(_get(r, "chat_id", "")),
        "message_id": int(_get(r, "message_id", 0) or 0),
        "account": _handle_for(conn, _get(r, "account_id")),
        "title": _get(r, "title", ""),
        "posted_at": _get(r, "posted_at", ""),
        "link": _get(r, "link", ""),
        "content_type": _get(r, "content_type", "artwork"),
        "reactions_count": int(_get(r, "reactions_count", 0) or 0),
        "reactions_json": _get(r, "reactions_json", ""),
        "reactions_at": _get(r, "reactions_at"),
    } for r in _rows(conn, "SELECT * FROM tg_submissions ORDER BY chat_id, message_id")]


def _export_collections(conn):
    return [{
        "name": r["name"], "cover_kind": _get(r, "cover_kind", ""),
        "cover_ref": _get(r, "cover_ref", ""), "notes": _get(r, "notes", ""),
        "created_at": _get(r, "created_at"), "updated_at": _get(r, "updated_at"),
    } for r in _rows(conn, "SELECT * FROM collections ORDER BY id")]


def _export_collection_members(conn):
    names = {r["id"]: r["name"] for r in _rows(conn, "SELECT id, name FROM collections")}
    keys = _post_keys_by_id(conn)
    out = []
    for r in _rows(conn, "SELECT * FROM collection_members ORDER BY collection_id, member_type"):
        name = names.get(r["collection_id"])
        if name is None:
            continue
        item = {"collection_name": name, "member_type": r["member_type"],
                "member_ref": r["member_ref"], "role": _get(r, "role", ""),
                "added_at": _get(r, "added_at"), "post": None}
        if r["member_type"] == "post":
            # The hidden FK: member_ref is a stringified post_id here.
            try:
                item["post"] = keys.get(int(r["member_ref"]))
            except (TypeError, ValueError):
                item["post"] = None
            if item["post"] is None:
                continue  # a post that no longer exists locally
            item["member_ref"] = None
        out.append(item)
    return out


def _export_submission_groups(conn):
    return [{"name": r["name"], "description": _get(r, "description", ""),
             "created_at": _get(r, "created_at")}
            for r in _rows(conn, "SELECT * FROM submission_groups ORDER BY group_id")]


def _export_submission_group_members(conn):
    names = {r["group_id"]: r["name"]
             for r in _rows(conn, "SELECT group_id, name FROM submission_groups")}
    out = []
    for r in _rows(conn, "SELECT * FROM submission_group_members ORDER BY group_id"):
        name = names.get(r["group_id"])
        if name is None:
            continue
        out.append({"group_name": name, "platform": r["platform"],
                    "submission_id": _s(r["submission_id"])})
    return out


def _link_members(conn) -> dict:
    members: dict[int, list] = {}
    for r in _rows(conn, "SELECT link_id, platform, submission_id FROM submission_link_members "
                         "ORDER BY platform, submission_id"):
        members.setdefault(r["link_id"], []).append([r["platform"], _s(r["submission_id"])])
    return members


def _export_submission_links(conn):
    """A link travels as its member set, because that is all a link is.

    ``submission_links`` is ``(link_id, created_at)`` and nothing else, so there
    is no column to key on. Two installs that independently linked the same
    three submissions made the same link, and this is the only description under
    which that is true.
    """
    members = _link_members(conn)
    out = []
    for r in _rows(conn, "SELECT * FROM submission_links ORDER BY link_id"):
        mem = sorted(members.get(r["link_id"], []))
        if not mem:
            continue  # an empty link has no identity and nothing to say
        out.append({"members": mem, "created_at": _get(r, "created_at")})
    return out


def _export_submission_tags(conn):
    names = {r["tag_id"]: r["name"] for r in _rows(conn, "SELECT tag_id, name FROM tags")}
    out = []
    for r in _rows(conn, "SELECT * FROM submission_tags ORDER BY tag_id"):
        name = names.get(r["tag_id"])
        if name is None:
            continue
        out.append({"tag_name": name, "platform": r["platform"],
                    "submission_id": _s(r["submission_id"])})
    return out


def _export_artists(conn):
    return [{"artist_key": r["artist_key"], "name": _get(r, "name", ""),
             "aliases": _get(r, "aliases", "[]"), "flags": _get(r, "flags", "[]"),
             "notes": _get(r, "notes", "")}
            for r in _rows(conn, "SELECT * FROM artists ORDER BY artist_key")]


def _export_artist_handles(conn):
    return [{"artist_key": r["artist_key"], "platform": r["platform"],
             "handle": _get(r, "handle", ""), "confidence": _get(r, "confidence", "")}
            for r in _rows(conn, "SELECT * FROM artist_handles "
                                 "ORDER BY artist_key, platform")]


def _export_ignored(conn):
    return [{"platform": r["platform"], "submission_id": _s(r["submission_id"]),
             "ignored_at": _get(r, "ignored_at")}
            for r in _rows(conn, "SELECT * FROM ignored_submissions ORDER BY platform")]


def _export_inbox_state(conn):
    return [{"platform": r["platform"], "comment_id": _s(r["comment_id"]),
             "handled_at": _get(r, "handled_at")}
            for r in _rows(conn, "SELECT * FROM inbox_state ORDER BY platform")]


def _export_commissions(conn):
    return [{
        "client_name": _get(r, "client_name", ""), "created_at": _get(r, "created_at"),
        "description": _get(r, "description", ""), "price": _get(r, "price", 0),
        "currency": _get(r, "currency", "USD"), "status": _get(r, "status", "quote"),
        "due_date": _get(r, "due_date", ""), "artwork_name": _get(r, "artwork_name", ""),
        "deliver_sites": _get(r, "deliver_sites", "[]"), "notes": _get(r, "notes", ""),
        "archived": int(_get(r, "archived", 0) or 0), "updated_at": _get(r, "updated_at"),
    } for r in _rows(conn, "SELECT * FROM commissions ORDER BY id")]


def _export_goals(conn):
    return [{
        "platform": r["platform"], "scope": _get(r, "scope", "account"),
        "submission_id": _s(_get(r, "submission_id")), "metric": r["metric"],
        "target_value": int(_get(r, "target_value", 0) or 0),
        "created_at": _get(r, "created_at"), "completed_at": _get(r, "completed_at"),
    } for r in _rows(conn, "SELECT * FROM goals ORDER BY goal_id")]


_EXPORTERS = {
    "personas": _export_personas,
    "accounts": _export_accounts,
    "tags": _export_tags,
    "post_contacts": _export_post_contacts,
    "posts": _export_posts,
    "post_media": _export_post_media,
    "post_publications": _export_post_publications,
    "post_mentions": _export_post_mentions,
    "masterpieces": _export_masterpieces,
    "masterpiece_members": _export_masterpiece_members,
    "masterpiece_not_duplicate": lambda c: _export_pairs(c, "masterpiece_not_duplicate"),
    "masterpiece_not_variant": lambda c: _export_pairs(c, "masterpiece_not_variant"),
    "publications": _export_publications,
    "tg_submissions": _export_tg_submissions,
    "collections": _export_collections,
    "collection_members": _export_collection_members,
    "submission_groups": _export_submission_groups,
    "submission_group_members": _export_submission_group_members,
    "submission_links": _export_submission_links,
    "submission_link_members": lambda c: [],  # carried inside its parent link
    "submission_tags": _export_submission_tags,
    "artists": _export_artists,
    "artist_handles": _export_artist_handles,
    "ignored_submissions": _export_ignored,
    "inbox_state": _export_inbox_state,
    "commissions": _export_commissions,
    "goals": _export_goals,
}


def export_tombstones(conn: sqlite3.Connection) -> list[dict]:
    """Undelivered deletes, with any local surrogate resolved out.

    ``collection_members`` is the one that needs work: the trigger records
    ``collection_id`` because under ``ON DELETE CASCADE`` there is no Python
    frame to record anything better, and the parent row is already gone by the
    time the child trigger fires. So the name is resolved here, and a tombstone
    whose collection has itself been deleted is dropped — deleting a collection
    is not a delete this channel propagates, so replaying its members' removal
    upstream would describe a state nobody asked for.
    """
    names = {}
    if _table_exists(conn, "collections"):
        names = {r["id"]: r["name"] for r in _rows(conn, "SELECT id, name FROM collections")}
    post_keys = _post_keys_by_id(conn) if _table_exists(conn, "posts") else {}

    out = []
    for item in tombstones.pending(conn):
        table, key = item["table"], item["key"]
        if table == "collection_members":
            try:
                cid = int(key[0])
            except (TypeError, ValueError):
                continue
            name = names.get(cid)
            if name is None:
                continue
            member_type, member_ref = key[1], key[2]
            post = None
            if member_type == "post":
                try:
                    post = post_keys.get(int(member_ref))
                except (TypeError, ValueError):
                    post = None
                if post is None:
                    continue
                member_ref = None
            out.append({"table": table, "raw_key": item["key"],
                        "key": {"collection_name": name, "member_type": member_type,
                                "member_ref": member_ref, "post": post},
                        "deleted_at": item["deleted_at"]})
            continue

        columns = registry.rule_for(table).key
        out.append({"table": table, "raw_key": item["key"],
                    "key": dict(zip(columns, key)), "deleted_at": item["deleted_at"]})
    return out


def export_bundle(conn: sqlite3.Connection, *, include_deletes: bool = True) -> dict:
    """Everything this install has to say about the shared tables."""
    tables: dict[str, list] = {}
    for name in registry.SHR_ORDER:
        rule = registry.rule_for(name)
        if not _table_exists(conn, name):
            if not rule.lazy:
                logger.debug("SHR export: %s absent locally", name)
            continue
        tables[name] = _EXPORTERS[name](conn)

    bundle = {
        "version": BUNDLE_VERSION,
        "generated_at": _now(),
        "tables": tables,
        "tombstones": export_tombstones(conn) if include_deletes else [],
    }
    bundle["row_count"] = sum(len(v) for v in tables.values())
    return bundle


# ══ Appliers ══════════════════════════════════════════════════

def _apply_personas(conn, rows, ctx):
    n = 0
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        existing = conn.execute(
            "SELECT persona_id FROM personas WHERE lower(trim(name)) = ?",
            (name.lower(),)).fetchone()
        cols = ("color", "default_platforms", "default_rating", "preferred_post_time")
        if existing:
            conn.execute(
                "UPDATE personas SET color = ?, default_platforms = ?, default_rating = ?, "
                "preferred_post_time = ? WHERE persona_id = ?",
                (*[r.get(c) or "" for c in cols], existing["persona_id"]))
        else:
            conn.execute(
                "INSERT INTO personas (name, color, sort_order, default_platforms, "
                "default_rating, preferred_post_time) VALUES (?, ?, ?, ?, ?, ?)",
                (name, r.get("color") or "#6c8cff", int(r.get("sort_order") or 0),
                 *[r.get(c) or "" for c in cols[1:]]))
        n += 1
    return n


def _apply_accounts(conn, rows, ctx):
    """Match on (platform, handle); fall back to the platform default only for a
    handle-less row, which is what a seeded-but-unconnected account looks like.

    ``platform`` is never updated — it is the identity, not an attribute — and
    an unmatched row is INSERTed rather than written onto whatever shares its
    position, which is the 3.5.4 rule and the reason the 2026-08-12 corruption
    cannot recur through this path.
    """
    n = 0
    for r in rows:
        platform = r.get("platform")
        if not platform:
            continue
        handle = (r.get("handle") or "").strip()
        target = None
        if handle:
            row = conn.execute(
                "SELECT account_id FROM accounts WHERE platform = ? AND lower(trim(handle)) = ?",
                (platform, handle.lower())).fetchone()
            target = row["account_id"] if row else None
        if target is None and not handle and int(r.get("is_default") or 0):
            row = conn.execute(
                "SELECT account_id FROM accounts WHERE platform = ? AND is_default = 1",
                (platform,)).fetchone()
            target = row["account_id"] if row else None

        persona_id = None
        if r.get("persona"):
            prow = conn.execute("SELECT persona_id FROM personas WHERE lower(trim(name)) = ?",
                                ((r["persona"] or "").strip().lower(),)).fetchone()
            persona_id = prow["persona_id"] if prow else None

        if target is None:
            cur = conn.execute(
                "INSERT INTO accounts (platform, label, handle, enabled, is_default, "
                "sort_order, persona_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (platform, r.get("label") or "", handle, int(r.get("enabled") or 0),
                 0,  # never import a default over an existing one; the partial
                     # unique index would reject it and the local default is the
                     # one this install has been posting with.
                 int(r.get("sort_order") or 0), persona_id))
            target = cur.lastrowid
        else:
            conn.execute(
                "UPDATE accounts SET label = ?, enabled = ?, sort_order = ? WHERE account_id = ?",
                (r.get("label") or "", int(r.get("enabled") or 0),
                 int(r.get("sort_order") or 0), target))
            if persona_id is not None:
                conn.execute("UPDATE accounts SET persona_id = ? WHERE account_id = ?",
                             (persona_id, target))
        n += 1
    return n


def _apply_tags(conn, rows, ctx):
    n = 0
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        conn.execute("INSERT OR IGNORE INTO tags (name, color) VALUES (?, ?)",
                     (name, r.get("color") or "#6c8cff"))
        row = conn.execute("SELECT tag_id FROM tags WHERE name = ?", (name,)).fetchone()
        if row:
            ctx.tags[name] = row["tag_id"]
        n += 1
    return n


def _apply_post_contacts(conn, rows, ctx):
    n = 0
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        cols = ("handle_bsky", "handle_tw", "handle_mast", "handle_thr", "handle_tum")
        existing = conn.execute(
            "SELECT id FROM post_contacts WHERE lower(trim(name)) = ?",
            (name.lower(),)).fetchone()
        if existing:
            conn.execute(
                "UPDATE post_contacts SET handle_bsky = ?, handle_tw = ?, handle_mast = ?, "
                "handle_thr = ?, handle_tum = ? WHERE id = ?",
                (*[r.get(c) or "" for c in cols], existing["id"]))
            ctx.contacts[name.lower()] = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO post_contacts (name, handle_bsky, handle_tw, handle_mast, "
                "handle_thr, handle_tum) VALUES (?, ?, ?, ?, ?, ?)",
                (name, *[r.get(c) or "" for c in cols]))
            ctx.contacts[name.lower()] = cur.lastrowid
        n += 1
    return n


def _find_post(conn, key) -> int | None:
    """Resolve a post's content key to a local post_id.

    Matched on created_at first (indexed by nothing, but the table is small)
    then confirmed by hashing the candidate's body — cheaper than storing a
    hash column and it cannot drift from the data.
    """
    if not key:
        return None
    created_at, digest = key[0], key[1]
    for row in conn.execute("SELECT post_id, body FROM posts WHERE created_at = ?",
                            (created_at,)).fetchall():
        if post_key(created_at, row["body"])[1] == digest:
            return row["post_id"]
    return None


def _apply_posts(conn, rows, ctx):
    """Parents before children: a thread part's ``parent_post_id`` has to point
    at a row that exists here, and the export orders them that way."""
    n = 0
    for r in rows:
        key = tuple(r.get("key") or [])
        local = _find_post(conn, r.get("key"))
        parent_id = 0
        if r.get("parent"):
            parent_id = _find_post(conn, r["parent"]) or ctx.posts.get(tuple(r["parent"])) or 0
        if local is None:
            cur = conn.execute(
                "INSERT INTO posts (body, rating, image_path, image_alt, created_at, "
                "updated_at, parent_post_id, thread_ordinal) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (r.get("body") or "", r.get("rating") or "general",
                 r.get("image_path") or "", r.get("image_alt") or "",
                 r.get("created_at") or "", r.get("updated_at") or "",
                 int(parent_id or 0), int(r.get("thread_ordinal") or 0)))
            local = cur.lastrowid
        else:
            # The body is part of the key, so an update here can only be
            # rating/media/alt — an edit to what the post says makes a
            # different post by construction.
            conn.execute(
                "UPDATE posts SET rating = ?, image_path = ?, image_alt = ?, updated_at = ? "
                "WHERE post_id = ?",
                (r.get("rating") or "general", r.get("image_path") or "",
                 r.get("image_alt") or "", r.get("updated_at") or "", local))
        ctx.posts[key] = local
        n += 1
    return n


def _post_id(conn, ctx, key):
    if not key:
        return None
    return ctx.posts.get(tuple(key)) or _find_post(conn, key)


def _apply_post_media(conn, rows, ctx):
    n = 0
    for r in rows:
        pid = _post_id(conn, ctx, r.get("post"))
        if pid is None:
            ctx.skip("post_media", "post not found", r.get("post"))
            continue
        ordinal = int(r.get("ordinal") or 0)
        existing = conn.execute(
            "SELECT id FROM post_media WHERE post_id = ? AND ordinal = ?",
            (pid, ordinal)).fetchone()
        if existing:
            conn.execute("UPDATE post_media SET path = ?, alt = ? WHERE id = ?",
                         (r.get("path") or "", r.get("alt") or "", existing["id"]))
        else:
            conn.execute(
                "INSERT INTO post_media (post_id, ordinal, path, alt) VALUES (?, ?, ?, ?)",
                (pid, ordinal, r.get("path") or "", r.get("alt") or ""))
        n += 1
    return n


def _apply_post_publications(conn, rows, ctx):
    """Insert-only (§registry). A post_publication that already exists here was
    written by the install that actually posted it."""
    n = 0
    for r in rows:
        pid = _post_id(conn, ctx, r.get("post"))
        if pid is None:
            ctx.skip("post_publications", "post not found", r.get("post"))
            continue
        account_id = _resolve_account(conn, r.get("platform"), r.get("account")) or 0
        cur = conn.execute(
            "INSERT OR IGNORE INTO post_publications (post_id, platform, account_id, status, "
            "external_id, external_url, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, r.get("platform"), account_id, r.get("status") or "pending",
             r.get("external_id") or "", r.get("external_url") or "",
             r.get("error") or "", r.get("created_at") or ""))
        n += cur.rowcount
    return n


def _apply_post_mentions(conn, rows, ctx):
    n = 0
    for r in rows:
        pid = _post_id(conn, ctx, r.get("post"))
        if pid is None:
            ctx.skip("post_mentions", "post not found", r.get("post"))
            continue
        name = (r.get("contact") or "").strip().lower()
        cid = ctx.contacts.get(name)
        if cid is None and name:
            row = conn.execute("SELECT id FROM post_contacts WHERE lower(trim(name)) = ?",
                               (name,)).fetchone()
            cid = row["id"] if row else None
        conn.execute(
            "INSERT OR REPLACE INTO post_mentions (post_id, token, contact_id) VALUES (?, ?, ?)",
            (pid, r.get("token") or "", int(cid or 0)))
        n += 1
    return n


def _apply_masterpieces(conn, rows, ctx):
    n = 0
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        conn.execute("INSERT OR IGNORE INTO masterpieces (name) VALUES (?)", (name,))
        # `status` carries the junk flag — kept-but-hidden, the reversible form
        # of deletion this project uses instead of removing artwork. It has to
        # cross or hiding a piece on one box leaves it visible on the other.
        conn.execute("UPDATE masterpieces SET status = ? WHERE name = ?",
                     (r.get("status") or "", name))
        n += 1
    return n


def _apply_masterpiece_members(conn, rows, ctx):
    n = 0
    for r in rows:
        name, platform = r.get("masterpiece_name"), r.get("platform")
        sid = _s(r.get("submission_id"))
        if not (name and platform and sid):
            continue
        conn.execute("INSERT OR IGNORE INTO masterpieces (name) VALUES (?)", (name,))
        account_id = _resolve_account(conn, platform, r.get("account"))
        conn.execute(
            "INSERT OR IGNORE INTO masterpiece_members (masterpiece_name, platform, "
            "submission_id, account_id, role, linked_via, variant_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, platform, sid, account_id, r.get("role") or "crosspost",
             r.get("linked_via") or "manual", r.get("variant_key") or ""))
        conn.execute("UPDATE masterpieces SET updated_at = datetime('now') WHERE name = ?",
                     (name,))
        n += 1
    return n


def _apply_pairs(table):
    def apply(conn, rows, ctx):
        if not _table_exists(conn, table):
            if table != "masterpiece_not_variant":
                return 0
            # Created lazily by variant_suggest.py. An incoming dismissal is a
            # good enough reason to create it.
            conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ("
                         "  name_a TEXT NOT NULL, name_b TEXT NOT NULL,"
                         "  PRIMARY KEY (name_a, name_b))")
        n = 0
        for r in rows:
            a, b = r.get("name_a"), r.get("name_b")
            if not (a and b):
                continue
            conn.execute(f"INSERT OR IGNORE INTO {table} (name_a, name_b) VALUES (?, ?)", (a, b))
            n += 1
        return n
    return apply


def _apply_tg_submissions(conn, rows, ctx):
    """Insert-only, keyed on Telegram's own (chat_id, message_id).

    A row that already exists is left alone rather than updated: its reaction
    counts belong to whichever machine ingests the update stream, and an
    upward update would overwrite fresher counts with staler ones.
    """
    n = 0
    for r in rows:
        chat_id, message_id = r.get("chat_id"), r.get("message_id")
        if not chat_id or not message_id:
            continue
        account_id = _resolve_account(conn, "tg", r.get("account")) or 0
        cur = conn.execute(
            "INSERT OR IGNORE INTO tg_submissions (submission_id, account_id, chat_id, "
            "message_id, title, posted_at, link, content_type, reactions_count, "
            "reactions_json, reactions_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"{chat_id}:{message_id}", account_id, str(chat_id), int(message_id),
             r.get("title", ""), r.get("posted_at", ""), r.get("link", ""),
             r.get("content_type", "artwork"), int(r.get("reactions_count") or 0),
             r.get("reactions_json", ""), r.get("reactions_at")))
        n += cur.rowcount or 0
    return n


def _apply_publications(conn, rows, ctx):
    """Insert-only. See the module docstring: upward, an update can only be a
    stale copy landing on fresher analytics, and the rows the desktop truly
    originates arrive through Stage 2."""
    n = 0
    for r in rows:
        platform, story = r.get("platform"), r.get("story_name")
        if not (platform and story):
            continue
        account_id = _resolve_account(conn, platform, r.get("account")) or 0
        cur = conn.execute(
            "INSERT OR IGNORE INTO publications (content_type, story_name, chapter_index, "
            "platform, account_id, chapter_title, external_id, external_url, format_file, "
            "file_hash, tags_used, title_used, description_used, rating_used, status, "
            "first_posted_at, last_updated_at, update_count, word_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r.get("content_type") or "story", story, int(r.get("chapter_index") or 0),
             platform, account_id, r.get("chapter_title") or "",
             r.get("external_id") or "", r.get("external_url") or "",
             r.get("format_file") or "", r.get("file_hash") or "",
             r.get("tags_used") or "[]", r.get("title_used") or "",
             r.get("description_used") or "", r.get("rating_used") or "",
             r.get("status") or "draft", r.get("first_posted_at"),
             r.get("last_updated_at"), int(r.get("update_count") or 0),
             int(r.get("word_count") or 0)))
        n += cur.rowcount
    return n


def _apply_collections(conn, rows, ctx):
    """Keyed on ``name``, which the schema does not enforce as unique.

    An ambiguous name is skipped and reported rather than guessed at: picking
    one of two same-named collections would silently move somebody's members
    into the wrong container, and there is no way to tell from here which one
    was meant.
    """
    n = 0
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        matches = conn.execute("SELECT id FROM collections WHERE name = ?", (name,)).fetchall()
        if len(matches) > 1:
            ctx.skip("collections", "ambiguous name (more than one local match)", name)
            continue
        if matches:
            cid = matches[0]["id"]
            conn.execute(
                "UPDATE collections SET cover_kind = ?, cover_ref = ?, notes = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (r.get("cover_kind") or "", r.get("cover_ref") or "",
                 r.get("notes") or "", cid))
        else:
            cur = conn.execute(
                "INSERT INTO collections (name, cover_kind, cover_ref, notes) VALUES (?, ?, ?, ?)",
                (name, r.get("cover_kind") or "", r.get("cover_ref") or "",
                 r.get("notes") or ""))
            cid = cur.lastrowid
        ctx.collections[name] = cid
        n += 1
    return n


def _collection_id(conn, ctx, name):
    if name in ctx.collections:
        return ctx.collections[name]
    rows = conn.execute("SELECT id FROM collections WHERE name = ?", (name,)).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def _apply_collection_members(conn, rows, ctx):
    n = 0
    for r in rows:
        name = (r.get("collection_name") or "").strip()
        cid = _collection_id(conn, ctx, name)
        if cid is None:
            ctx.skip("collection_members", "collection not found or ambiguous", name)
            continue
        member_type = r.get("member_type") or ""
        member_ref = r.get("member_ref")
        if member_type == "post":
            pid = _post_id(conn, ctx, r.get("post"))
            if pid is None:
                ctx.skip("collection_members", "post not found", r.get("post"))
                continue
            member_ref = str(pid)  # the hidden FK, rewritten to LOCAL ids
        if not member_ref:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO collection_members (collection_id, member_type, "
            "member_ref, role) VALUES (?, ?, ?, ?)",
            (cid, member_type, member_ref, r.get("role") or ""))
        conn.execute("UPDATE collections SET updated_at = datetime('now') WHERE id = ?", (cid,))
        n += 1
    return n


def _apply_submission_groups(conn, rows, ctx):
    n = 0
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        row = conn.execute("SELECT group_id FROM submission_groups WHERE name = ?",
                           (name,)).fetchone()
        if row:
            gid = row["group_id"]
            conn.execute("UPDATE submission_groups SET description = ? WHERE group_id = ?",
                         (r.get("description") or "", gid))
        else:
            cur = conn.execute(
                "INSERT INTO submission_groups (name, description) VALUES (?, ?)",
                (name, r.get("description") or ""))
            gid = cur.lastrowid
        ctx.groups[name] = gid
        n += 1
    return n


def _apply_submission_group_members(conn, rows, ctx):
    n = 0
    for r in rows:
        name = (r.get("group_name") or "").strip()
        gid = ctx.groups.get(name)
        if gid is None:
            row = conn.execute("SELECT group_id FROM submission_groups WHERE name = ?",
                               (name,)).fetchone()
            gid = row["group_id"] if row else None
        if gid is None:
            ctx.skip("submission_group_members", "group not found", name)
            continue
        conn.execute(
            "INSERT OR IGNORE INTO submission_group_members (group_id, platform, submission_id) "
            "VALUES (?, ?, ?)", (gid, r.get("platform"), r.get("submission_id")))
        n += 1
    return n


def _apply_submission_links(conn, rows, ctx):
    """Recreate a link only when no local link already holds the same member set.

    Identity by member set is what makes this idempotent: pushing twice does not
    accumulate duplicate links, and two installs that made the same link
    converge on one.
    """
    local = _link_members(conn) if _table_exists(conn, "submission_link_members") else {}
    # Members come back as lists (they are JSON on the wire); normalise both
    # sides to a sorted tuple of tuples so the set membership test is on the
    # same shape and is order-independent.
    existing = {tuple(sorted(tuple(m) for m in members)) for members in local.values()}
    n = 0
    for r in rows:
        members = [tuple(m) for m in (r.get("members") or [])]
        if not members:
            continue
        signature = tuple(sorted(members))
        if signature in existing:
            continue
        cur = conn.execute("INSERT INTO submission_links DEFAULT VALUES")
        link_id = cur.lastrowid
        for platform, sid in members:
            conn.execute(
                "INSERT OR IGNORE INTO submission_link_members (link_id, platform, submission_id) "
                "VALUES (?, ?, ?)", (link_id, platform, sid))
        existing.add(signature)
        n += 1
    return n


def _apply_submission_tags(conn, rows, ctx):
    n = 0
    for r in rows:
        name = r.get("tag_name")
        if not name:
            continue
        tag_id = ctx.tags.get(name)
        if tag_id is None:
            conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
            row = conn.execute("SELECT tag_id FROM tags WHERE name = ?", (name,)).fetchone()
            tag_id = row["tag_id"] if row else None
            ctx.tags[name] = tag_id
        if tag_id is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO submission_tags (tag_id, platform, submission_id) "
            "VALUES (?, ?, ?)", (tag_id, r.get("platform"), r.get("submission_id")))
        n += 1
    return n


def _apply_artists(conn, rows, ctx):
    """Merge, never blank. Same rule as ``artist_queries.upsert_artist``: an
    empty aliases/flags/notes means "not supplied", so a sync from a box that
    has only the name cannot wipe a repost prohibition recorded on the other."""
    n = 0
    for r in rows:
        conn.execute(
            "INSERT INTO artists (artist_key, name, aliases, flags, notes) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(artist_key) DO UPDATE SET "
            "  name = excluded.name, "
            "  aliases = CASE WHEN excluded.aliases = '[]' THEN artists.aliases "
            "            ELSE excluded.aliases END, "
            "  flags   = CASE WHEN excluded.flags   = '[]' THEN artists.flags "
            "            ELSE excluded.flags END, "
            "  notes   = CASE WHEN excluded.notes   = ''   THEN artists.notes "
            "            ELSE excluded.notes END, "
            "  updated_at = datetime('now')",
            (r.get("artist_key"), r.get("name") or "", r.get("aliases") or "[]",
             r.get("flags") or "[]", r.get("notes") or ""))
        n += 1
    return n


def _apply_artist_handles(conn, rows, ctx):
    n = 0
    for r in rows:
        handle = (r.get("handle") or "").strip()
        if not r.get("artist_key") or not r.get("platform") or not handle:
            continue
        conn.execute(
            "INSERT INTO artist_handles (artist_key, platform, handle, confidence) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(artist_key, platform) DO UPDATE SET "
            "  handle = excluded.handle, confidence = excluded.confidence",
            (r.get("artist_key"), r.get("platform"), handle, r.get("confidence") or ""))
        n += 1
    return n


def _apply_ignored(conn, rows, ctx):
    n = 0
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO ignored_submissions (platform, submission_id, ignored_at) "
            "VALUES (?, ?, COALESCE(?, datetime('now')))",
            (r.get("platform"), _s(r.get("submission_id")), r.get("ignored_at")))
        n += 1
    return n


def _apply_inbox_state(conn, rows, ctx):
    n = 0
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO inbox_state (platform, comment_id, handled_at) "
            "VALUES (?, ?, ?)",
            (r.get("platform"), _s(r.get("comment_id")), r.get("handled_at")))
        n += 1
    return n


def _apply_commissions(conn, rows, ctx):
    n = 0
    for r in rows:
        client, created = r.get("client_name") or "", r.get("created_at")
        row = conn.execute(
            "SELECT id FROM commissions WHERE client_name = ? AND created_at IS ?",
            (client, created)).fetchone()
        cols = ("description", "price", "currency", "status", "due_date",
                "artwork_name", "deliver_sites", "notes")
        if row:
            conn.execute(
                "UPDATE commissions SET description = ?, price = ?, currency = ?, status = ?, "
                "due_date = ?, artwork_name = ?, deliver_sites = ?, notes = ?, archived = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (*[r.get(c) for c in cols], int(r.get("archived") or 0), row["id"]))
        else:
            conn.execute(
                "INSERT INTO commissions (client_name, created_at, description, price, "
                "currency, status, due_date, artwork_name, deliver_sites, notes, archived) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (client, created, *[r.get(c) for c in cols], int(r.get("archived") or 0)))
        n += 1
    return n


def _apply_goals(conn, rows, ctx):
    n = 0
    for r in rows:
        sid = r.get("submission_id")
        sid = None if sid in (None, "", "None") else sid
        row = conn.execute(
            "SELECT goal_id FROM goals WHERE platform = ? AND scope = ? AND submission_id IS ? "
            "AND metric = ? AND target_value = ?",
            (r.get("platform"), r.get("scope") or "account", sid,
             r.get("metric"), int(r.get("target_value") or 0))).fetchone()
        if row:
            conn.execute("UPDATE goals SET completed_at = ? WHERE goal_id = ?",
                         (r.get("completed_at"), row["goal_id"]))
        else:
            conn.execute(
                "INSERT INTO goals (platform, scope, submission_id, metric, target_value, "
                "created_at, completed_at) VALUES (?, ?, ?, ?, ?, COALESCE(?, datetime('now')), ?)",
                (r.get("platform"), r.get("scope") or "account", sid, r.get("metric"),
                 int(r.get("target_value") or 0), r.get("created_at"), r.get("completed_at")))
        n += 1
    return n


_APPLIERS = {
    "personas": _apply_personas,
    "accounts": _apply_accounts,
    "tags": _apply_tags,
    "post_contacts": _apply_post_contacts,
    "posts": _apply_posts,
    "post_media": _apply_post_media,
    "post_publications": _apply_post_publications,
    "post_mentions": _apply_post_mentions,
    "masterpieces": _apply_masterpieces,
    "masterpiece_members": _apply_masterpiece_members,
    "masterpiece_not_duplicate": _apply_pairs("masterpiece_not_duplicate"),
    "masterpiece_not_variant": _apply_pairs("masterpiece_not_variant"),
    "publications": _apply_publications,
    "tg_submissions": _apply_tg_submissions,
    "collections": _apply_collections,
    "collection_members": _apply_collection_members,
    "submission_groups": _apply_submission_groups,
    "submission_group_members": _apply_submission_group_members,
    "submission_links": _apply_submission_links,
    "submission_link_members": lambda c, rows, ctx: 0,  # carried inside its link
    "submission_tags": _apply_submission_tags,
    "artists": _apply_artists,
    "artist_handles": _apply_artist_handles,
    "ignored_submissions": _apply_ignored,
    "inbox_state": _apply_inbox_state,
    "commissions": _apply_commissions,
    "goals": _apply_goals,
}


# ── Deletes ───────────────────────────────────────────────────

def apply_tombstones(conn, items, *, confirmed_tables=()) -> dict:
    """Replay the sender's deletes here.

    Tables whose registry entry says ``SURFACE`` are *not* applied unless the
    caller names them in ``confirmed_tables``. ``masterpiece_members`` is the
    one that matters: unlinking a platform upload from a Masterpiece changes
    what a piece is recorded as being, and the standing project rule is that
    anything in that neighbourhood shows the operator the list and waits for a
    yes rather than propagating on a timer.
    """
    applied, surfaced, failed = [], [], []
    confirmed = set(confirmed_tables or ())
    # Keys as THIS install's triggers will have written them, which is not
    # always the key that arrived: collection_members is recorded against the
    # local collection_id, so the sender's raw key would never match here.
    local_keys: list[dict] = []

    for item in items or []:
        table = item.get("table")
        key = item.get("key") or {}
        try:
            rule = registry.rule_for(table)
        except registry.UnregisteredTable as e:
            failed.append({"table": table, "error": str(e)})
            continue
        if rule.deletes == registry.ADDITIVE:
            failed.append({"table": table, "error": "table does not carry deletes"})
            continue
        if rule.deletes == registry.SURFACE and table not in confirmed:
            surfaced.append(item)
            continue
        if not _table_exists(conn, table):
            continue

        try:
            if table == "ignored_submissions":
                parts = (key.get("platform"), _s(key.get("submission_id")))
                conn.execute("DELETE FROM ignored_submissions WHERE platform = ? "
                             "AND submission_id = ?", parts)
                local_keys.append({"table": table, "key": list(parts)})
            elif table == "inbox_state":
                parts = (key.get("platform"), _s(key.get("comment_id")))
                conn.execute("DELETE FROM inbox_state WHERE platform = ? AND comment_id = ?",
                             parts)
                local_keys.append({"table": table, "key": list(parts)})
            elif table == "masterpiece_members":
                parts = (key.get("masterpiece_name"), key.get("platform"),
                         _s(key.get("submission_id")))
                conn.execute("DELETE FROM masterpiece_members WHERE masterpiece_name = ? "
                             "AND platform = ? AND submission_id = ?", parts)
                local_keys.append({"table": table, "key": list(parts)})
            elif table == "collection_members":
                cid = _collection_id(conn, _Ctx(), key.get("collection_name") or "")
                if cid is None:
                    failed.append({"table": table, "error": "collection not found",
                                   "key": key})
                    continue
                member_ref = key.get("member_ref")
                if key.get("member_type") == "post":
                    pid = _find_post(conn, key.get("post"))
                    if pid is None:
                        failed.append({"table": table, "error": "post not found", "key": key})
                        continue
                    member_ref = str(pid)
                parts = (cid, key.get("member_type"), member_ref)
                conn.execute("DELETE FROM collection_members WHERE collection_id = ? "
                             "AND member_type = ? AND member_ref = ?", parts)
                local_keys.append({"table": table, "key": [_s(p) for p in parts]})
            else:
                failed.append({"table": table, "error": "no delete handler"})
                continue
        except sqlite3.Error as e:
            failed.append({"table": table, "error": str(e), "key": key})
            continue

        applied.append({"table": table, "key": item.get("raw_key") or key})

    # A delete applied here fires THIS install's own tombstone triggers, which
    # would queue the same removal in the receiver's outbox. The receiver never
    # pushes, so nothing would come of it — but leaving them makes the outbox
    # count meaningless as a diagnostic, so they are cleared.
    if local_keys:
        tombstones.clear(conn, local_keys, commit=False)

    return {"applied": applied, "surfaced": surfaced, "failed": failed}


# ── Bundle application ────────────────────────────────────────

def apply_bundle(conn: sqlite3.Connection, bundle: dict, *,
                 confirmed_delete_tables=()) -> dict:
    """Apply a pushed bundle. One transaction: a bundle lands whole or not at all.

    Unknown tables are refused rather than ignored (``registry.rule_for``), and
    a table classed anything other than SHR is refused too — a push claiming to
    carry ``session_cache`` or ``posting_queue`` is either a bug or an attempt,
    and neither should be met with a best-effort write.
    """
    version = bundle.get("version")
    if version != BUNDLE_VERSION:
        raise ValueError(f"unsupported bundle version {version!r} "
                         f"(this install speaks {BUNDLE_VERSION})")

    tables = bundle.get("tables") or {}
    for name in tables:
        rule = registry.rule_for(name)  # raises UnregisteredTable
        if rule.ownership != registry.SHR:
            raise ValueError(
                f"{name!r} is classed {rule.ownership}, which does not travel in a "
                f"shared-table push: {rule.reason}")

    ctx = _Ctx()
    stats: dict[str, int] = {}
    try:
        for name in registry.SHR_ORDER:
            rows = tables.get(name)
            if not rows:
                continue
            if not _table_exists(conn, name) and name != "masterpiece_not_variant":
                ctx.skip(name, "table absent on this install")
                continue
            stats[name] = _APPLIERS[name](conn, rows, ctx)

        deletes = apply_tombstones(conn, bundle.get("tombstones"),
                                   confirmed_tables=confirmed_delete_tables)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    total = sum(stats.values())
    logger.info("SHR apply: %d rows across %d tables, %d deletes, %d surfaced, %d skipped",
                total, len(stats), len(deletes["applied"]), len(deletes["surfaced"]),
                len(ctx.skipped))
    return {"rows": total, "tables": stats, "deletes": deletes,
            "skipped": ctx.skipped, "generated_at": bundle.get("generated_at")}
