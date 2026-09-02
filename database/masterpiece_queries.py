"""Masterpieces — membership CRUD + the live rollup that pools a Masterpiece's
cross-site uploads into merged analytics, locations, tags and persona(s).

A Masterpiece is the master record for ONE image (the image analog of a story's
MASTER.md). Its canonical metadata lives on disk as ``masterpiece.json`` (see
posting/artwork_reader.py); the DB side is a thin NAME-keyed index
(``masterpieces``) plus this membership table (``masterpiece_members``) recording
which platform uploads are the same image.

Stat pooling deliberately reuses collections_queries' per-platform normalisation
(``_location_from_submission`` / ``_stats_from_row`` / ``_METRICS``) so a
Masterpiece and a Collection pool stats identically — one source of truth for
"the same piece across N sites". See docs/specs/masterpieces.md.
"""
from __future__ import annotations

import sqlite3

from database.collections_queries import (
    _acct_to_persona, _location_from_submission, _location_from_row,
    _submission_row, _submission_rows_bulk,
)


# ── Index + membership CRUD ──────────────────────────────────────

def ensure_indexed(conn: sqlite3.Connection, name: str, *,
                   source_link_id: int | None = None) -> None:
    """Register a Masterpiece name in the thin ``masterpieces`` index (idempotent).
    The disk masterpiece.json remains the source of truth; this just gives us a
    stable row for fast listing + migration provenance."""
    conn.execute(
        "INSERT OR IGNORE INTO masterpieces (name, source_link_id) VALUES (?, ?)",
        (name, source_link_id))
    if source_link_id is not None:
        conn.execute(
            "UPDATE masterpieces SET source_link_id = ?, updated_at = datetime('now') "
            "WHERE name = ? AND source_link_id IS NULL",
            (source_link_id, name))


def set_status(conn: sqlite3.Connection, name: str, status: str) -> None:
    """Mark a Masterpiece as junk (``'junk'``) or restore it (``''``).

    Junk = kept-but-hidden: the folder + members survive untouched, the grid just
    stops showing it outside the Junk view. Works for index-only names too (the
    13-junk-tweets case) — we ensure the index row exists first.
    """
    ensure_indexed(conn, name)
    conn.execute(
        "UPDATE masterpieces SET status = ?, updated_at = datetime('now') WHERE name = ?",
        (status, name))


def get_status(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute("SELECT status FROM masterpieces WHERE name = ?", (name,)).fetchone()
    return (row[0] or "") if row else ""


def statuses(conn: sqlite3.Connection) -> dict[str, str]:
    """name -> status for every indexed Masterpiece (one query, for the list)."""
    return {r[0]: (r[1] or "") for r in conn.execute("SELECT name, status FROM masterpieces")}


def get_members(conn: sqlite3.Connection, name: str,
                variant_key: str | None = None) -> list[dict]:
    """Members of a Masterpiece; pass ``variant_key`` to filter to one variant
    (''=primary/unattributed). None = the whole cohort (default, unchanged)."""
    sql = ("SELECT masterpiece_name, platform, submission_id, account_id, role, "
           "linked_via, variant_key, added_at FROM masterpiece_members "
           "WHERE masterpiece_name = ?")
    params: list = [name]
    if variant_key is not None:
        sql += " AND variant_key = ?"
        params.append(variant_key)
    sql += " ORDER BY added_at, platform"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def add_member(conn: sqlite3.Connection, name: str, platform: str, submission_id,
               *, account_id: int | None = None, role: str = "crosspost",
               linked_via: str = "manual", variant_key: str = "") -> None:
    """Link one platform upload to a Masterpiece (idempotent on the PK). Ensures
    the name is indexed first so a Masterpiece always has an index row."""
    ensure_indexed(conn, name)
    conn.execute(
        "INSERT OR IGNORE INTO masterpiece_members "
        "(masterpiece_name, platform, submission_id, account_id, role, linked_via, variant_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, platform, str(submission_id), account_id, role or "crosspost",
         linked_via or "manual", variant_key or ""))
    conn.execute("UPDATE masterpieces SET updated_at = datetime('now') WHERE name = ?",
                 (name,))


def set_member_variant(conn: sqlite3.Connection, name: str, platform: str,
                       submission_id, variant_key: str) -> None:
    """Attribute an existing member to a variant (''=primary)."""
    conn.execute(
        "UPDATE masterpiece_members SET variant_key = ? WHERE masterpiece_name = ? "
        "AND platform = ? AND submission_id = ?",
        (variant_key or "", name, platform, str(submission_id)))


def clear_variant_members(conn: sqlite3.Connection, name: str, variant_key: str) -> None:
    """Re-key a deleted variant's members back to primary ('')."""
    conn.execute(
        "UPDATE masterpiece_members SET variant_key = '' WHERE masterpiece_name = ? "
        "AND variant_key = ?", (name, variant_key))


def rename_variant_key(conn: sqlite3.Connection, name: str, old_key: str,
                       new_key: str) -> int:
    """Re-key a variant's members so per-variant stat attribution follows a
    rename (2.189.0). Returns rows moved. The caller edits masterpiece.json."""
    cur = conn.execute(
        "UPDATE masterpiece_members SET variant_key = ? WHERE masterpiece_name = ? "
        "AND variant_key = ?", (new_key or "", name, old_key))
    conn.execute("UPDATE masterpieces SET updated_at = datetime('now') WHERE name = ?",
                 (name,))
    return cur.rowcount


def move_variant_members(conn: sqlite3.Connection, from_name: str, variant_key: str,
                         to_name: str) -> int:
    """Move ONE variant's members out to their own Masterpiece, re-keyed to the
    primary '' (they're that record's own uploads now). The inverse of
    :func:`merge_as_variant`'s member half — so folding a piece in is no longer a
    one-way door. Returns members moved; the caller handles the on-disk split
    (new folder + image + variants-entry removal). 2.189.0."""
    ensure_indexed(conn, to_name)
    moved = 0
    for m in get_members(conn, from_name, variant_key):
        cur = conn.execute(
            "INSERT OR IGNORE INTO masterpiece_members "
            "(masterpiece_name, platform, submission_id, account_id, role, linked_via, variant_key) "
            "VALUES (?, ?, ?, ?, ?, ?, '')",
            (to_name, m["platform"], m["submission_id"], m["account_id"],
             m["role"], m["linked_via"]))
        moved += cur.rowcount
    conn.execute(
        "DELETE FROM masterpiece_members WHERE masterpiece_name = ? AND variant_key = ?",
        (from_name, variant_key))
    conn.execute("UPDATE masterpieces SET updated_at = datetime('now') WHERE name IN (?, ?)",
                 (from_name, to_name))
    return moved


def merge_as_variant(conn: sqlite3.Connection, keep: str, absorb: str,
                     keymap) -> int:
    """Fold ``absorb`` into ``keep`` as variant(s): its members move over, each
    re-keyed via ``keymap`` (its stats stay attributed), and absorb's index row
    is removed. Unlike :func:`merge_masterpieces` (same image, amnesia) this is
    for different renders of one piece — nothing is discarded except PK-colliding
    members already on ``keep``.

    ``keymap`` maps *absorb*'s ``variant_key`` → *keep*'s ``variant_key``. When
    absorb has its OWN sub-variants, this carries each across as a distinct
    variant on keep instead of flattening them (2.189.2). A member whose key
    isn't in the map falls back to the primary mapping (``''``). Passing a bare
    string is back-compat for the old "all members → this one key" flatten.

    The caller handles the on-disk side (copy absorb's variant images into keep's
    folder + append the variants entries in masterpiece.json) before deleting
    absorb's folder. Returns members moved."""
    if isinstance(keymap, str):
        keymap = {"": keymap}
    default = keymap.get("", next(iter(keymap.values()), ""))
    ensure_indexed(conn, keep)
    moved = 0
    for m in get_members(conn, absorb):
        dest = keymap.get(m["variant_key"], default)
        cur = conn.execute(
            "INSERT OR IGNORE INTO masterpiece_members "
            "(masterpiece_name, platform, submission_id, account_id, role, linked_via, variant_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (keep, m["platform"], m["submission_id"], m["account_id"],
             m["role"], m["linked_via"], dest or ""))
        moved += cur.rowcount
    conn.execute("DELETE FROM masterpiece_members WHERE masterpiece_name = ?", (absorb,))
    conn.execute("DELETE FROM masterpieces WHERE name = ?", (absorb,))
    conn.execute("UPDATE masterpieces SET updated_at = datetime('now') WHERE name = ?", (keep,))
    return moved


def remove_member(conn: sqlite3.Connection, name: str, platform: str, submission_id) -> None:
    conn.execute(
        "DELETE FROM masterpiece_members WHERE masterpiece_name = ? AND platform = ? "
        "AND submission_id = ?", (name, platform, str(submission_id)))
    conn.execute("UPDATE masterpieces SET updated_at = datetime('now') WHERE name = ?",
                 (name,))


def add_not_duplicate(conn: sqlite3.Connection, names: list[str]) -> int:
    """Remember that these Masterpieces are NOT the same image (the de-dup finder
    flagged them but the user said no). Records every pair in the group so none of
    them get re-grouped. Returns the number of new pairs stored."""
    uniq = sorted({n for n in (names or []) if n})
    added = 0
    for i in range(len(uniq)):
        for j in range(i + 1, len(uniq)):
            cur = conn.execute(
                "INSERT OR IGNORE INTO masterpiece_not_duplicate (name_a, name_b) "
                "VALUES (?, ?)", (uniq[i], uniq[j]))
            added += cur.rowcount
    conn.commit()
    return added


def not_duplicate_pairs(conn: sqlite3.Connection) -> set[tuple]:
    """Every user-confirmed 'not the same image' pair, normalised (a < b). The
    de-dup finder skips these edges so dismissed look-alikes never regroup."""
    return {
        (r["name_a"], r["name_b"])
        for r in conn.execute("SELECT name_a, name_b FROM masterpiece_not_duplicate")
    }


def merge_masterpieces(conn: sqlite3.Connection, keep: str, drop: str) -> int:
    """Fold ``drop``'s site-members into ``keep`` and remove ``drop``'s index row.

    Used to collapse two Masterpieces of the SAME image (2.144.0 de-dup). Members
    already on ``keep`` (same platform+submission_id) are discarded as duplicates.
    The caller is responsible for deleting ``drop``'s on-disk folder (its image is
    identical to ``keep``'s). Returns the number of members actually moved."""
    ensure_indexed(conn, keep)
    moved = 0
    for m in get_members(conn, drop):
        cur = conn.execute(
            "INSERT OR IGNORE INTO masterpiece_members "
            "(masterpiece_name, platform, submission_id, account_id, role, linked_via) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (keep, m["platform"], str(m["submission_id"]), m.get("account_id"),
             m.get("role") or "crosspost", "merge"))
        moved += cur.rowcount
    conn.execute("DELETE FROM masterpiece_members WHERE masterpiece_name = ?", (drop,))
    conn.execute("DELETE FROM masterpieces WHERE name = ?", (drop,))
    conn.execute("UPDATE masterpieces SET updated_at = datetime('now') WHERE name = ?", (keep,))
    conn.commit()
    return moved


def absorb_publications(conn: sqlite3.Connection, keep: str, drop: str) -> dict:
    """Carry ``drop``'s publications over to ``keep`` when the two are folded.

    **The ghost-publication bug (3.16.0).** Folding moved `masterpiece_members`
    and deleted the folder, but never touched `publications` — so every fold left
    rows whose `story_name` names a work that no longer exists on disk. Nothing
    renders those: every works list is built from folders, so the record became
    invisible *and* its views/faves stopped pooling into anything. A prod audit
    found 41 of 86 artwork work-names in `publications` had no folder.

    It bit hardest on IMPORTED art, because `artwork_importer` writes a
    publication and **no member row** — so folding an imported piece moved
    nothing at all and lost the link entirely. That is how FA 37056160
    ("Embarrassed") ended up orphaned while the same picture sat in the
    catalogue as *Growing Into It*.

    Two things happen per publication, and the order matters:

    1. **A member row on ``keep`` is ensured first.** That is what actually makes
       the stats pool, and it is the structure designed to hold "one image, N
       site uploads" — `publications` cannot, because its UNIQUE key allows only
       one row per (work, chapter, platform, account).
    2. **Then the publication is re-pointed**, if that does not collide. When
       both works were posted to the same platform on the same account the
       UNIQUE key blocks it; the row is left alone rather than deleted, because
       it still carries `first_posted_at` and the posted title. Step 1 already
       fixed the visible harm, and the leftover shows up in Unfiled Posts for a
       human to resolve.

    Never deletes a publication. Returns ``{moved, members_added, blocked}``.
    """
    import sqlite3 as _sqlite3

    rows = conn.execute(
        "SELECT pub_id, platform, external_id, account_id FROM publications "
        "WHERE story_name = ?", (drop,)).fetchall()
    moved = members_added = blocked = 0
    for r in rows:
        ext = str(r["external_id"] or "").strip()
        if ext:
            cur = conn.execute(
                "INSERT OR IGNORE INTO masterpiece_members "
                "(masterpiece_name, platform, submission_id, account_id, role, linked_via) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (keep, r["platform"], ext, r["account_id"], "crosspost", "merge"))
            members_added += cur.rowcount
        # ⚠ Commit the member BEFORE risking the update. `with conn:` rolls the
        # whole open transaction back on IntegrityError, which would undo the
        # INSERT above — so on exactly the collision path, the piece would end up
        # pooling nothing, which is the harm this function exists to prevent.
        # Caught by test_a_colliding_publication_is_kept_not_destroyed.
        conn.commit()
        try:
            conn.execute(
                "UPDATE publications SET story_name = ? WHERE pub_id = ?",
                (keep, r["pub_id"]))
            conn.commit()
            moved += 1
        except _sqlite3.IntegrityError:
            # `keep` already has a publication for this platform+account. Leave
            # the row; the member above is what makes the stats pool.
            conn.rollback()
            blocked += 1
    return {"moved": moved, "members_added": members_added, "blocked": blocked}


def orphan_publications(conn: sqlite3.Connection, existing_names: set) -> list[dict]:
    """Publications whose work no longer exists on disk — "unfiled posts".

    ``existing_names`` is every work name the archives actually have; the caller
    supplies it because this module does not read the filesystem.

    These are invisible everywhere else by construction: works lists are built
    from folders, and the discovered list excludes anything holding a publication
    row. So a post can be known, recorded, and completely unreachable.
    """
    out = []
    for r in conn.execute(
            "SELECT pub_id, content_type, story_name, platform, account_id, "
            "external_id, external_url, title_used, status, first_posted_at "
            "FROM publications ORDER BY story_name"):
        if r["story_name"] in existing_names:
            continue
        d = dict(r)
        owner = conn.execute(
            "SELECT masterpiece_name FROM masterpiece_members "
            "WHERE platform = ? AND submission_id = ?",
            (d["platform"], str(d["external_id"]))).fetchone()
        d["linked_to"] = owner["masterpiece_name"] if owner else ""
        out.append(d)
    return out


def member_pairs(conn: sqlite3.Connection, name: str) -> list[tuple]:
    """`(platform, submission_id)` pairs for a Masterpiece — feeds the combined
    snapshot chart via analytics_queries.get_combined_snapshots."""
    return [(m["platform"], str(m["submission_id"])) for m in get_members(conn, name)]


def all_member_pairs(conn: sqlite3.Connection) -> set[tuple]:
    """Every `(platform, submission_id)` that belongs to ANY Masterpiece.

    Used by the Artwork hub's discovered list to drop tiles that are already
    Masterpiece members — a piece bundled into a Masterpiece shouldn't reappear
    as a duplicate discovered tile (2.140.0)."""
    return {
        (r["platform"], str(r["submission_id"]))
        for r in conn.execute(
            "SELECT platform, submission_id FROM masterpiece_members")
    }


# ── Rollup ───────────────────────────────────────────────────────

def _publication_fallbacks(conn: sqlite3.Connection, name: str) -> dict:
    """``{(platform, external_id): {"url", "title"}}`` for one work's posts.

    A member links the instant a post succeeds (``linked_via='publication'``),
    but the *stats* for it come from the platform's own submission table, which
    only exists once the poller has run. When the two are keyed differently —
    DeviantArt records the publish call's **UUID** while `da_submissions` keys on
    the **numeric** deviation id — they never join, and before 3.9.11
    ``_location_from_row`` returned ``None`` for the unmatched member and the
    rollup silently dropped it. A piece really was on DeviantArt, and "Published
    to" said it wasn't.

    The publication row already holds a working link and the title that was
    posted, so it is the natural fallback: the row appears immediately, with
    counts blank until the poller fills them in.
    """
    out: dict[tuple, dict] = {}
    try:
        rows = conn.execute(
            "SELECT platform, external_id, external_url, title_used FROM publications "
            "WHERE story_name = ? AND status = 'posted'", (name,)).fetchall()
    except Exception:
        return out
    for r in rows:
        eid = str(r["external_id"] or "")
        if eid:
            out[(r["platform"], eid)] = {"url": r["external_url"] or "",
                                         "title": r["title_used"] or ""}
    return out


def rollup_members(conn: sqlite3.Connection, name: str,
                   variant_key: str | None = None) -> dict:
    """Resolve a Masterpiece's members into live locations and pool the stats.

    Mirrors collections_queries.rollup_collection: sum non-None metrics, union
    tags, collect the personas + platforms spanned. Returns pooled data ONLY —
    the canonical masterpiece.json (title/desc/rating/characters) is merged in by
    the API layer. ``variant_key`` filters the rollup to ONE variant's members
    (2.158.0 — per-variant stats); None = the whole cohort, unchanged."""
    a2p = _acct_to_persona(conn)
    members = get_members(conn, name, variant_key)

    pubs = _publication_fallbacks(conn, name)

    locations: list[dict] = []
    for m in members:
        pub = pubs.get((m["platform"], str(m["submission_id"]))) or {}
        loc = _location_from_submission(
            conn, m["platform"], m["submission_id"],
            account_id=m.get("account_id"), source="masterpiece",
            url=pub.get("url", ""))
        if loc is None:
            # A linked member must NEVER be invisible. Without a polled row and
            # without a recorded URL there is nothing to link to, but the piece
            # is still on that platform and saying otherwise is the bug.
            loc = {"platform": m["platform"], "submission_id": str(m["submission_id"]),
                   "url": "", "title": "", "account_id": m.get("account_id"),
                   "stats": {"views": None, "favorites": None, "comments": None},
                   "keywords": [], "source": "masterpiece"}
        if not loc.get("title"):
            loc["title"] = pub.get("title", "")
        loc["role"] = m.get("role") or "crosspost"
        loc["linked_via"] = m.get("linked_via") or "manual"
        locations.append(loc)

    tot = {"views": 0, "favorites": 0, "comments": 0}
    tags: set[str] = set()
    persona_ids: set[int] = set()
    platforms: set[str] = set()
    for loc in locations:
        for k in tot:
            val = loc["stats"].get(k)
            if val:
                tot[k] += val
        tags.update(loc.get("keywords") or [])
        platforms.add(loc["platform"])
        aid = loc.get("account_id")
        if aid in a2p:
            persona_ids.add(a2p[aid])

    return {
        "members": members,
        "locations": locations,
        "totals": {**tot, "platforms": len(platforms), "locations": len(locations)},
        "tags": sorted(tags),
        "persona_ids": sorted(persona_ids),
    }


def summarize(conn: sqlite3.Connection, name: str) -> dict:
    """Light rollup for the Library grid: pooled totals + personas + member count
    + platforms + an auto-cover (first member location that has a thumbnail)."""
    roll = rollup_members(conn, name)
    locs = roll["locations"]
    cover_thumb, cover_platform = "", ""
    for l in locs:
        if l.get("thumbnail_url"):
            cover_thumb, cover_platform = l["thumbnail_url"], l["platform"]
            break
    return {
        "totals": roll["totals"],
        "persona_ids": roll["persona_ids"],
        "member_count": len(roll["members"]),
        "platforms": sorted({l["platform"] for l in locs}),
        "cover_thumb": cover_thumb,
        "cover_platform": cover_platform,
    }


# ── Batched rollup for the grid (perf guardrail) ─────────────────
# summarize() per masterpiece was O(members) queries EACH — the "live rollup × N"
# cost the list endpoint paid on every load (plus a write per name). These fold
# the whole grid into a handful of queries: one bulk member fetch, one
# _submission_rows_bulk (one query per platform table), one persona map.

def get_members_bulk(conn: sqlite3.Connection, names) -> dict:
    """All members for many Masterpieces in one query → ``{name: [member dicts]}``.

    Each name's members stay in ``added_at, platform`` order (a name never spans
    two chunks, so its slice is fully ordered) — cover selection depends on it.
    """
    names = list(names)
    out: dict[str, list] = {n: [] for n in names}
    for i in range(0, len(names), 900):
        chunk = names[i:i + 900]
        ph = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT masterpiece_name, platform, submission_id, account_id, role, "
            "linked_via, variant_key, added_at FROM masterpiece_members "
            f"WHERE masterpiece_name IN ({ph}) ORDER BY added_at, platform", chunk).fetchall()
        for r in rows:
            d = dict(r)
            out.setdefault(d["masterpiece_name"], []).append(d)
    return out


def ensure_indexed_bulk(conn: sqlite3.Connection, names) -> int:
    """Register any not-yet-indexed names in ONE write (or none). Returns the
    count inserted.

    Replaces the per-name ``ensure_indexed`` loop in the list endpoint, which
    issued N writes — and took an exclusive write lock — on every read. We look
    up which names already exist, then executemany only the missing ones, so a
    steady-state grid load takes zero writes.
    """
    names = list(names)
    if not names:
        return 0
    existing: set = set()
    for i in range(0, len(names), 900):
        chunk = names[i:i + 900]
        ph = ",".join("?" * len(chunk))
        existing.update(r[0] for r in conn.execute(
            f"SELECT name FROM masterpieces WHERE name IN ({ph})", chunk).fetchall())
    missing = [(n,) for n in names if n not in existing]
    if missing:
        conn.executemany("INSERT OR IGNORE INTO masterpieces (name) VALUES (?)", missing)
    return len(missing)


def summarize_many(conn: sqlite3.Connection, names) -> dict:
    """Batched :func:`summarize` for the whole grid → ``{name: summary}``.

    Same per-name output as ``summarize(conn, name)`` but resolves ALL members,
    submission rows and the persona map in O(platforms) queries instead of
    O(total members). Names with no members yield the zeroed summary.
    """
    names = list(names)
    a2p = _acct_to_persona(conn)
    members_by_name = get_members_bulk(conn, names)
    all_pairs = {(m["platform"], str(m["submission_id"]))
                 for ms in members_by_name.values() for m in ms}
    rows = _submission_rows_bulk(conn, all_pairs)

    out: dict[str, dict] = {}
    for name in names:
        members = members_by_name.get(name, [])
        locs: list[dict] = []
        for m in members:
            loc = _location_from_row(
                m["platform"], str(m["submission_id"]),
                rows.get((m["platform"], str(m["submission_id"]))),
                account_id=m.get("account_id"), source="masterpiece")
            if loc:
                locs.append(loc)

        tot = {"views": 0, "favorites": 0, "comments": 0}
        persona_ids: set = set()
        platforms: set = set()
        cover_thumb, cover_platform = "", ""
        for l in locs:
            for k in tot:
                v = l["stats"].get(k)
                if v:
                    tot[k] += v
            platforms.add(l["platform"])
            aid = l.get("account_id")
            if aid in a2p:
                persona_ids.add(a2p[aid])
            if not cover_thumb and l.get("thumbnail_url"):
                cover_thumb, cover_platform = l["thumbnail_url"], l["platform"]

        out[name] = {
            "totals": {**tot, "platforms": len(platforms), "locations": len(locs)},
            "persona_ids": sorted(persona_ids),
            "member_count": len(members),
            "platforms": sorted(platforms),
            "cover_thumb": cover_thumb,
            "cover_platform": cover_platform,
        }
    return out


# ── Promote (create a Masterpiece from a discovered/imported submission) ──

def promote_from_submission(conn: sqlite3.Connection, platform: str, submission_id) -> dict:
    """Materialise a Masterpiece from a platform submission the pollers already
    discovered, and seed its **primary** member (spec §3.1).

    Reuses ``posting.artwork_importer.import_artwork`` for the heavy lifting
    (download full-res where available, write the folder + ``masterpiece.json`` +
    a publication with the right ``account_id``) — that path is idempotent
    (re-promoting a submission returns the existing folder). Then:
      • ensure the name is indexed,
      • add the source ``(platform, submission_id, account_id)`` as ``role='primary'``,
      • compute + store the canonical image's perceptual hash (feeds same-image
        suggestions) both in ``image_hashes`` and on ``masterpiece.json``.

    Returns ``{name, status, images}``. Raises ValueError on an un-importable
    submission (surfaced by the route as a 4xx). Import runs on its own
    connections and commits before we touch ``conn``.
    """
    from posting import artwork_importer

    res = artwork_importer.import_artwork(platform, str(submission_id))
    name = res["name"]
    ensure_indexed(conn, name)

    row = _submission_row(conn, platform, str(submission_id)) or {}
    add_member(conn, name, platform, submission_id, account_id=row.get("account_id"),
               role="primary", linked_via="manual")

    # Perceptual hash of the canonical image — best-effort, never fails the promote.
    try:
        from database import image_hash
        from posting import artwork_reader
        art = artwork_reader.load_artwork(name)
        if art.image:
            ph = image_hash.dhash_from_path(str(art.path / art.image))
            if ph:
                image_hash.ensure_table(conn)
                image_hash.store(conn, platform, str(submission_id), ph, source="masterpiece")
                artwork_reader.save_artwork_metadata(name, {"phash": ph})
    except Exception:
        pass

    return {"name": name, "status": res.get("status", "imported"),
            "images": res.get("images", 1)}


# ── Same-image suggestions (native pHash, no AI) ─────────────────

def _our_published_pairs(conn: sqlite3.Connection) -> set[tuple]:
    """Every `(platform, external_id)` PawPoller has actually published.

    Deleted rows are excluded: if the upload is gone from the platform, a
    lookalike hash is a genuine candidate again rather than our own post.
    """
    try:
        rows = conn.execute(
            "SELECT platform, external_id FROM publications "
            "WHERE external_id != '' AND status != 'deleted'").fetchall()
    except Exception:
        return set()
    return {(r["platform"], str(r["external_id"])) for r in rows}


def suggestions(conn: sqlite3.Connection, name: str) -> list[dict]:
    """Cross-platform "this same image also lives here?" candidates for a
    Masterpiece — NOT already members (spec §3.1 step 4).

    Anchored, native, no-AI: seed from the perceptual hashes of the Masterpiece's
    existing members **and** a fresh hash of its canonical image, then scan the
    ``image_hashes`` store for rows within ``HAMMING_THRESHOLD`` of any seed. The
    store is warmed by ``POST /api/collections/hash-scan`` (local artwork + an
    allowlisted thumbnail scan); if it is cold this simply returns few/none, so the
    frontend offers a "scan for matches" action first.

    Returns ``[{platform, submission_id, similarity, reason:'image', title,
    thumbnail_url, account_id}, …]`` sorted by similarity, best 20.
    """
    from database import image_hash

    members = set(member_pairs(conn, name))
    ours = _our_published_pairs(conn)
    seeds: set[str] = set()
    for plat, sid in members:
        r = conn.execute(
            "SELECT phash FROM image_hashes WHERE platform = ? AND submission_id = ?",
            (plat, str(sid))).fetchone()
        if r and r["phash"]:
            seeds.add(r["phash"])
    # Fresh hash of the canonical image so suggestions work even when no member
    # has been hashed yet (zero network — local file).
    try:
        from posting import artwork_reader
        art = artwork_reader.load_artwork(name)
        if art.image:
            ph = image_hash.dhash_from_path(str(art.path / art.image))
            if ph:
                seeds.add(ph)
    except Exception:
        pass
    if not seeds:
        return []

    out: dict[tuple, dict] = {}
    for row in image_hash.all_hashes(conn):
        # The synthetic '__mp__' rows are Masterpiece HERO hashes, not platform
        # uploads — without this skip every piece suggests its own hash record
        # (distance 0) and attaching it mints a bogus '__mp__' member (2.159.2).
        if row["platform"] == "__mp__":
            continue
        key = (row["platform"], str(row["submission_id"]))
        if key in members:
            continue
        # ⚠ Membership alone is not enough. Something PawPoller posted ITSELF
        # kept coming back as "is this the same image?" — the complaint being
        # that posting through an item then asks you to link that same post up.
        # Three ways it happened, all of which this one check covers:
        #   • DeviantArt stored the API's GUID while the poller stored the URL's
        #     integer, so the auto-link wrote a member that joined to nothing
        #     (fixed at source in 3.29.0, but old rows and other installs exist);
        #   • a STORY's cover thumbnail hashes to the artwork it was drawn from,
        #     and story publishing never creates Masterpiece members at all;
        #   • the piece was published before publishing started auto-linking.
        # A publication row is proof we put it there, whatever its content type
        # or which item it belongs to, so it is never a "did you also upload
        # this somewhere?" candidate. Suggestions are for uploads that predate
        # PawPoller or were made by hand; the manual paste-a-link path still
        # reaches anything this skips.
        if key in ours:
            continue
        d = min(image_hash.hamming(row["phash"], s) for s in seeds)
        if d > image_hash.HAMMING_THRESHOLD:
            continue
        sim = round(1.0 - d / 64.0, 3)
        cur = out.get(key)
        if cur is not None and cur["similarity"] >= sim:
            continue
        loc = _location_from_submission(conn, key[0], key[1], source="suggestion") or {}
        out[key] = {
            "platform": key[0],
            "submission_id": key[1],
            "similarity": sim,
            "reason": "image",
            "title": loc.get("title", ""),
            "thumbnail_url": loc.get("thumbnail_url", ""),
            "account_id": loc.get("account_id"),
        }
    return sorted(out.values(), key=lambda c: c["similarity"], reverse=True)[:20]


# ── submission_links → Masterpieces migration (Phase 7, §7) ──────

def migrate_links_to_masterpieces(conn: sqlite3.Connection) -> int:
    """One-time, idempotent, **reversible** fold of each Cross-Platform
    ``submission_link`` (an old art "master") into a Masterpiece (spec §7).

    Mirrors ``collections_queries.migrate_links_to_collections``: each link with
    ≥2 members becomes one Masterpiece — a ``masterpieces`` index row +
    ``masterpiece_members`` (the first title-resolving member is ``role='primary'``,
    ``linked_via='migration'``). The ``submission_links`` rows are left **intact**
    (fully reversible); idempotency + provenance via ``masterpieces.source_link_id``.
    Returns the number newly created.

    **Known limitation (spec §9):** a migrated Masterpiece is *index-only* — it has
    no canonical folder/image yet, so it won't appear in the folder-based Library
    grid (which enumerates `list_artworks()`) until "materialised" via the promote
    flow. This function is therefore provided for explicit invocation, NOT wired to
    startup (so it can't silently mint grid-invisible Masterpieces). On installs
    where ``submission_links`` is empty it is a no-op.
    """
    try:
        link_rows = conn.execute("SELECT link_id FROM submission_links").fetchall()
    except Exception:
        return 0  # link tables absent on this DB — nothing to migrate
    try:
        migrated = {r["source_link_id"] for r in conn.execute(
            "SELECT source_link_id FROM masterpieces WHERE source_link_id IS NOT NULL")}
    except Exception:
        return 0  # masterpieces table / source_link_id not present yet
    n = 0
    for lr in link_rows:
        lid = lr["link_id"]
        if lid in migrated:
            continue
        members = conn.execute(
            "SELECT platform, submission_id FROM submission_link_members WHERE link_id = ?",
            (lid,)).fetchall()
        if len(members) < 2:
            continue  # a link needs 2+ members to be a meaningful master
        # Name from the first member that resolves a title (fallback: link id).
        name = ""
        for m in members:
            row = _submission_row(conn, m["platform"], str(m["submission_id"]))
            if row and (row.get("title") or row.get("full_text")):
                name = (row.get("title") or row.get("full_text") or "").strip()[:120]
                break
        if not name:
            name = f"Linked piece #{lid}"
        # Never collide with an existing Masterpiece/folder name (masterpieces.name
        # is UNIQUE); suffix if needed.
        base, k = name, 2
        while conn.execute("SELECT 1 FROM masterpieces WHERE name = ?", (name,)).fetchone():
            name, k = f"{base} ({k})", k + 1
        conn.execute("INSERT INTO masterpieces (name, source_link_id) VALUES (?, ?)", (name, lid))
        first = True
        for m in members:
            # account_id from the source row → correct persona rollup (§7 preserve).
            row = _submission_row(conn, m["platform"], str(m["submission_id"])) or {}
            conn.execute(
                "INSERT OR IGNORE INTO masterpiece_members "
                "(masterpiece_name, platform, submission_id, account_id, role, linked_via) "
                "VALUES (?, ?, ?, ?, ?, 'migration')",
                (name, m["platform"], str(m["submission_id"]), row.get("account_id"),
                 "primary" if first else "crosspost"))
            first = False
        n += 1
    return n


def canonical_titles(conn: sqlite3.Connection, platform: str,
                     submission_ids: list[str]) -> dict[str, str]:
    """Map each submission_id to its linked Masterpiece's canonical title.

    For the boorus this is the difference between a usable label and a confusing
    one: **e621 and Furbooru posts have no title field at all**, so the clients
    synthesise one from the first line of the description
    (`clients/e621/client.py::_parse_post`). That is what analytics stored, and
    what every "Top scored / Top favourited" row rendered — five rows of prose
    openings that are hard to tell apart and never match what the piece is called
    in the Library.

    The canonical title lives in `masterpiece.json`, not in the DB (the
    `masterpieces` table is a pure index — spec A2), so this resolves the members
    in SQL and reads the titles from disk. It is used on 5-row top-lists, so the
    read count is bounded and small; do not call it per row of an unbounded list.

    Returns only ids that resolved, so callers can `dict.get(id, existing_title)`
    and keep the old value for anything unlinked.
    """
    ids = [str(s) for s in submission_ids if str(s or "").strip()]
    if not ids:
        return {}
    qs = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT submission_id, masterpiece_name FROM masterpiece_members "
        f"WHERE platform = ? AND submission_id IN ({qs})",
        [platform, *ids],
    ).fetchall()
    if not rows:
        return {}

    from posting import artwork_reader     # lazy: db -> posting would be a cycle

    titles: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for r in rows:
        name = r["masterpiece_name"]
        if name not in by_name:
            try:
                by_name[name] = (artwork_reader.load_artwork(name).title or "").strip()
            except (FileNotFoundError, OSError):
                by_name[name] = ""
        if by_name[name]:
            titles[str(r["submission_id"])] = by_name[name]
    return titles


def apply_canonical_titles(conn: sqlite3.Connection, platform: str,
                           *row_lists: list[dict]) -> None:
    """Overlay :func:`canonical_titles` onto summary rows, in place.

    Each row is a dict carrying `submission_id` and `title`. A row whose
    submission is not linked to a Masterpiece keeps whatever title it had —
    never blanked, so an unlinked post still reads as something.
    """
    ids = [str(r.get("submission_id", "")) for rows in row_lists for r in rows]
    found = canonical_titles(conn, platform, ids)
    if not found:
        return
    for rows in row_lists:
        for r in rows:
            t = found.get(str(r.get("submission_id", "")))
            if t:
                r["title"] = t
