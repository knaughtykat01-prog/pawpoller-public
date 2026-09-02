"""CRUD functions for submission groups (cross-platform tagging/grouping).

Groups allow the user to manually tag submissions from different platforms
(Inkbunny, FurAffinity, Weasyl) together under a common label. This is
a user-defined organisational concept -- unlike cross-platform links
(in analytics_queries.py) which are 1:1 mappings of the same content
across platforms, groups are arbitrary collections (e.g. "My Best Work",
"Commission Pieces", "Series: Story Name").

The group system uses two tables:
- submission_groups: The group metadata (name, description).
- submission_group_members: Junction table mapping (group_id, platform,
  submission_id) with a UNIQUE constraint to prevent duplicate membership.

Stats aggregation (get_group_stats) dynamically looks up each member's
platform-specific submissions table to sum views/faves/comments across
all platforms in the group.
"""

from __future__ import annotations
import logging
import sqlite3
from typing import Any

from database import platform_metrics

logger = logging.getLogger(__name__)


def create_group(conn: sqlite3.Connection, name: str, description: str = "") -> int:
    """Create a new submission group. Returns the new group_id."""
    cur = conn.execute(
        "INSERT INTO submission_groups (name, description) VALUES (?, ?)",
        (name, description),
    )
    conn.commit()
    return cur.lastrowid


def get_group(conn: sqlite3.Connection, group_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM submission_groups WHERE group_id = ?", (group_id,)).fetchone()
    return dict(row) if row else None


def get_all_groups(conn: sqlite3.Connection) -> list[dict]:
    """Returns all groups with their member lists eagerly loaded.

    For each group, performs a secondary query to fetch its members from the
    junction table. This is an N+1 query pattern but acceptable here because
    the number of groups is expected to be small (tens, not thousands).
    """
    rows = conn.execute("SELECT * FROM submission_groups ORDER BY created_at DESC").fetchall()
    groups = []
    for r in rows:
        g = dict(r)
        # Eagerly load members for this group from the junction table.
        members = conn.execute(
            "SELECT * FROM submission_group_members WHERE group_id = ?", (g["group_id"],)
        ).fetchall()
        g["members"] = [dict(m) for m in members]
        g["member_count"] = len(members)
        groups.append(g)
    return groups


def update_group(conn: sqlite3.Connection, group_id: int, name: str | None = None, description: str | None = None) -> None:
    # Dynamic UPDATE construction: only includes SET clauses for non-None
    # parameters, allowing partial updates (e.g. rename without changing
    # description, or vice versa).
    updates = []
    params: list[Any] = []
    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if description is not None:
        updates.append("description = ?")
        params.append(description)
    if not updates:
        return
    params.append(group_id)
    conn.execute(f"UPDATE submission_groups SET {', '.join(updates)} WHERE group_id = ?", params)
    conn.commit()


def delete_group(conn: sqlite3.Connection, group_id: int) -> None:
    # Deleting the group row cascades to delete junction table entries
    # (submission_group_members) via ON DELETE CASCADE in the schema.
    conn.execute("DELETE FROM submission_groups WHERE group_id = ?", (group_id,))
    conn.commit()


def add_group_member(conn: sqlite3.Connection, group_id: int, platform: str, submission_id: int) -> bool:
    """Add a submission to a group. Returns True if added, False if already exists.

    The platform string ("ib", "fa", "ws") identifies which platform's
    submissions table the submission_id belongs to. The UNIQUE constraint on
    (group_id, platform, submission_id) prevents duplicates.
    """
    try:
        conn.execute(
            "INSERT INTO submission_group_members (group_id, platform, submission_id) VALUES (?, ?, ?)",
            (group_id, platform, submission_id),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # This (group, platform, submission) combination already exists.
        return False


def remove_group_member(conn: sqlite3.Connection, group_id: int, platform: str, submission_id: int) -> None:
    conn.execute(
        "DELETE FROM submission_group_members WHERE group_id = ? AND platform = ? AND submission_id = ?",
        (group_id, platform, submission_id),
    )
    conn.commit()


def get_group_stats(conn: sqlite3.Connection, group_id: int) -> dict:
    """Get aggregate stats for all submissions in a group across platforms.

    Dynamically resolves which database table to query based on the platform
    string stored in the junction table. The platform-to-table mapping is:
      "ib" -> submissions (Inkbunny, the primary/default platform)
      "fa" -> fa_submissions (FurAffinity)
      "ws" -> ws_submissions (Weasyl)

    This allows a single group to contain submissions from any combination
    of platforms and produce a unified total for views, faves, and comments.
    """
    members = conn.execute(
        "SELECT platform, submission_id FROM submission_group_members WHERE group_id = ?",
        (group_id,),
    ).fetchall()

    total_views = 0
    total_score = 0
    total_faves = 0
    total_comments = 0
    submissions = []

    # Table + metric columns come from the canonical registry
    # (database/platform_metrics.py). The local copy this replaces covered only
    # 11 platforms — a group containing an e621, Pixiv, Instagram, Mastodon,
    # Threads, Tumblr, FurryNetwork or Furbooru submission silently skipped it,
    # so the group total was short by however many of those it held.
    for m in members:
        platform = m["platform"]
        sub_id = m["submission_id"]
        spec = platform_metrics.get(platform)
        if not spec:
            # Unknown platform -- skip this member gracefully.
            continue
        try:
            row = conn.execute(
                f"SELECT * FROM {spec.table} WHERE submission_id = ?",
                (sub_id,),
            ).fetchone()
        except sqlite3.Error as e:
            logger.warning("group stats: %s unavailable (%s): %s", platform, spec.table, e)
            continue
        if row:
            r = dict(row)
            # Tag each result with its source platform for display purposes.
            r["platform"] = platform
            total_views += (r.get(spec.views, 0) or 0) if spec.views else 0
            total_score += (r.get(spec.score, 0) or 0) if spec.score else 0
            total_faves += (r.get(spec.faves, 0) or 0) if spec.faves else 0
            total_comments += (r.get(spec.comments, 0) or 0) if spec.comments else 0
            submissions.append(r)

    return {
        "total_views": total_views,
        "total_score": total_score,
        "total_favorites": total_faves,
        "total_comments": total_comments,
        "submissions": submissions,
    }
