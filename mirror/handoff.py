"""The desktop↔server work handoff — mirroring Stage 2.

Closes the one genuinely desktop-originated write path. Some posts cannot run
on the server at all: FurAffinity blocks the datacenter IP outright, so even
``/controls/`` pages come back as an empty shell with valid cookies. The server
already recognises this — ``manager.post_story`` catches the failure and
re-queues the job with ``requires='desktop'`` — and the server's own scheduler
then correctly skips it.

**And that is where it stopped.** The two installs have separate databases and
nothing ever carried the row between them, so ``requires='desktop'`` was in
practice a dead-letter status. Verified on prod: queue item #3709 (``Chosen``
ch1 on FurAffinity) sat ``pending`` from 2026-07-24 until this was written. The
spec's claim that the handoff was "already 90% built, only the result coming
back is missing" was too generous — the job going *out* was missing too.

## Shape

Rather than build a second execution path on the desktop, a claimed job is
**imported into the desktop's own queue** and run by the ordinary scheduler,
which already drives everything from natural keys (``story_name``,
``chapter_index``, ``platform``, ``content_type``) plus a locally-resolved
``account_id``. Nothing about posting had to be reimplemented, and a job that
arrives this way takes exactly the same code path as one queued locally —
including retries, cancellation and the 3.5.4 atomic claim.

    server                          desktop
    ------                          -------
    queue row (requires=desktop)
      -> GET  /handoff/jobs         natural keys, no surrogate ids but the
                                    opaque origin handle
      <- POST /handoff/claim        atomic claim_queue_item on the SERVER, so
                                    two desktops cannot take the same job
                                    import a local queue row
                                    (origin_server, origin_queue_id)
                                    ... ordinary scheduler posts it ...
      <- POST /handoff/result       natural keys + outcome
    upsert_publication()            server allocates its OWN pub_id
    update_queue_status()

## Two rules this module exists to enforce

**Only natural keys carry meaning.** ``account_id`` is meaningless across the
boundary (the two installs allocate independently — the 2026-08-12 corruption),
so an account travels as ``{platform, handle}`` and each side resolves it
locally via ``accounts.resolve_account_by_identity``. ``origin_queue_id`` is the
single exception, and only as an *opaque handle to the other side's row*: it is
echoed back untouched, never interpreted locally, never joined on.

**A result is delivered exactly once.** Reporting is a sweep over rows that are
finished but unreported, not an inline call from the scheduler. A crash between
posting and reporting therefore retries rather than losing the outcome, the
scheduler never blocks on the network, and ``origin_reported_at`` makes the
delivery idempotent — the failure mode that matters is double-*reporting* a
successful post, which would double-count a publication.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from database import accounts as accounts_db
from database import posting_queries

logger = logging.getLogger(__name__)

# Reported outcomes we accept. Anything else is a bug on the caller's side and
# is rejected rather than written through to the publications registry.
_TERMINAL = ("completed", "failed")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_get(row, key, default=None):
    """sqlite3.Row has no .get(), and these columns are migration-added."""
    try:
        return row[key] if key in row.keys() else default
    except (IndexError, KeyError):
        return default


# ── Server half: describe and settle jobs ─────────────────────

def describe_jobs(conn, limit: int = 20) -> list[dict]:
    """List this install's desktop-only jobs, rendered in natural keys.

    Only ``pending`` rows: a row already ``processing`` has been claimed by
    someone, and re-offering it is how the same post goes out twice.
    """
    rows = conn.execute(
        "SELECT * FROM posting_queue WHERE requires = 'desktop' AND status = 'pending' "
        "ORDER BY priority DESC, created_at ASC LIMIT ?",
        (limit,),
    ).fetchall()

    jobs = []
    for row in rows:
        account_id = _row_get(row, "account_id") or None
        identity = None
        if account_id:
            account = accounts_db.get_account(conn, account_id)
            if account:
                identity = {"platform": account["platform"],
                            "handle": account.get("handle") or ""}
        jobs.append({
            # Opaque handle back to OUR row. Meaningless on the far side; it is
            # echoed back with the result and never interpreted there.
            "origin_queue_id": row["queue_id"],
            "action": row["action"],
            "content_type": _row_get(row, "content_type", "story") or "story",
            "story_name": row["story_name"],
            "chapter_index": row["chapter_index"],
            "platform": row["platform"],
            "account": identity,
            "priority": _row_get(row, "priority", 0),
            "created_at": _row_get(row, "created_at"),
            "overrides": {
                k: _row_get(row, k)
                for k in ("title_override", "description_override", "tags_override",
                          "rating_override")
                if _row_get(row, k) is not None
            },
        })
    return jobs


def apply_result(conn, result: dict) -> dict:
    """Record a desktop-executed post on this (server) install.

    Goes through ``upsert_publication`` — the same write path a local post
    uses — so the server allocates its own ``pub_id`` and every downstream
    consumer sees an ordinary publication rather than a special mirrored one.
    """
    required = ("origin_queue_id", "platform", "story_name", "success")
    missing = [k for k in required if k not in result]
    if missing:
        raise ValueError(f"result is missing {', '.join(missing)}")

    queue_id = int(result["origin_queue_id"])
    platform = result["platform"]
    story_name = result["story_name"]
    chapter_index = int(result.get("chapter_index") or 0)
    content_type = result.get("content_type") or "story"
    success = bool(result["success"])

    row = conn.execute(
        "SELECT * FROM posting_queue WHERE queue_id = ?", (queue_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no queue item {queue_id} on this install")

    # The natural keys must match the row being settled. Without this a
    # malformed or replayed result could mark an unrelated job complete, and
    # the queue_id alone cannot detect that because it is opaque by design.
    if row["platform"] != platform or row["story_name"] != story_name:
        raise ValueError(
            f"result does not match queue item {queue_id}: "
            f"reported {story_name!r}/{platform}, row holds "
            f"{row['story_name']!r}/{row['platform']}"
        )
    if row["status"] == "cancelled":
        # Cancelling is a user action and outranks a late result.
        return {"applied": False, "reason": "cancelled", "queue_id": queue_id}
    if row["status"] == "completed":
        return {"applied": False, "reason": "already completed", "queue_id": queue_id}

    identity = result.get("account") or {}
    account_id = accounts_db.resolve_account_by_identity(
        conn, platform, identity.get("handle"))

    pub_id = None
    if success:
        tags = result.get("tags_used") or []
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except ValueError:
                tags = [t.strip() for t in tags.split(",") if t.strip()]
        pub_id = posting_queries.upsert_publication(
            conn, story_name, chapter_index, platform,
            account_id=account_id,
            content_type=content_type,
            external_id=str(result.get("external_id") or ""),
            external_url=str(result.get("external_url") or ""),
            title_used=str(result.get("title_used") or ""),
            tags_used=tags,
            rating_used=str(result.get("rating_used") or ""),
            status="posted",
        )

    posting_queries.log_posting_action(
        conn, platform, story_name, chapter_index,
        action=row["action"],
        status="success" if success else "failed",
        account_id=account_id or 0,
        content_type=content_type,
        pub_id=pub_id,
        queue_id=queue_id,
        external_id=result.get("external_id"),
        external_url=result.get("external_url"),
        error_message=result.get("error"),
    )
    posting_queries.update_queue_status(
        conn, queue_id, "completed" if success else "failed",
        error=result.get("error"), pub_id=pub_id,
    )
    conn.commit()

    logger.info("Handoff: settled queue item #%d from the desktop (%s)",
                queue_id, "success" if success else "failed")
    return {"applied": True, "queue_id": queue_id, "pub_id": pub_id,
            "account_id": account_id, "success": success}


# ── Desktop half: import jobs, report outcomes ────────────────

def import_job(conn, job: dict, server_url: str) -> dict:
    """Create a local queue row mirroring a claimed remote job.

    The account is re-resolved from its identity here rather than trusting any
    id in the payload. If the platform is unknown locally the job is refused:
    posting it as the wrong account is worse than not posting it, and this is
    the exact class of mistake that corrupted four rows on 2026-08-12.
    """
    origin_queue_id = int(job["origin_queue_id"])

    existing = conn.execute(
        "SELECT queue_id FROM posting_queue WHERE origin_server = ? AND origin_queue_id = ?",
        (server_url, origin_queue_id),
    ).fetchone()
    if existing:
        # Re-importing would post the same job twice.
        return {"imported": False, "reason": "already imported",
                "queue_id": existing["queue_id"], "origin_queue_id": origin_queue_id}

    identity = job.get("account") or {}
    platform = job["platform"]
    account_id = accounts_db.resolve_account_by_identity(
        conn, platform, identity.get("handle"))
    if account_id is None:
        raise LookupError(
            f"no local account for {platform} "
            f"(handle {identity.get('handle')!r}) — refusing to guess")

    overrides = job.get("overrides") or {}
    queue_id = posting_queries.add_to_queue(
        conn, job["story_name"], int(job.get("chapter_index") or 0), platform,
        job.get("action", "post"),
        account_id=account_id,
        content_type=job.get("content_type") or "story",
        title_override=overrides.get("title_override"),
        description_override=overrides.get("description_override"),
        tags_override=overrides.get("tags_override"),
        rating_override=overrides.get("rating_override"),
    )
    conn.execute(
        "UPDATE posting_queue SET origin_server = ?, origin_queue_id = ?, requires = 'any' "
        "WHERE queue_id = ?",
        (server_url, origin_queue_id, queue_id),
    )
    conn.commit()
    logger.info("Handoff: imported %s job #%d from %s as local #%d",
                platform, origin_queue_id, server_url, queue_id)
    return {"imported": True, "queue_id": queue_id,
            "origin_queue_id": origin_queue_id, "account_id": account_id}


def pending_reports(conn, server_url: str | None = None, limit: int = 50) -> list[dict]:
    """Finished imported jobs whose outcome has not reached the server yet."""
    sql = ("SELECT * FROM posting_queue WHERE origin_queue_id IS NOT NULL "
           "AND origin_reported_at IS NULL AND status IN (?, ?)")
    params: list = list(_TERMINAL)
    if server_url:
        sql += " AND origin_server = ?"
        params.append(server_url)
    sql += " ORDER BY queue_id ASC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def build_report(conn, row: dict) -> dict:
    """Turn a finished local queue row into a natural-key result payload."""
    account = accounts_db.get_account(conn, row.get("account_id") or 0)
    content_type = row.get("content_type") or "story"
    success = row.get("status") == "completed"

    payload = {
        "origin_queue_id": row["origin_queue_id"],
        "platform": row["platform"],
        "story_name": row["story_name"],
        "chapter_index": row.get("chapter_index") or 0,
        "content_type": content_type,
        "success": success,
        "error": row.get("last_error"),
        "account": ({"platform": account["platform"], "handle": account.get("handle") or ""}
                    if account else None),
    }

    # Attach what was actually published, read back from this install's own
    # registry. The queue row records that the post happened; only the
    # publication knows the external id and URL the platform handed back.
    if success and content_type != "post":
        pub = posting_queries.get_publication_by_story(
            conn, row["story_name"], row.get("chapter_index") or 0, row["platform"],
            row.get("account_id") or None, content_type=content_type,
        )
        if pub:
            payload.update({
                "external_id": pub.get("external_id") or "",
                "external_url": pub.get("external_url") or "",
                "title_used": pub.get("title_used") or "",
                "tags_used": pub.get("tags_used") or [],
                "rating_used": pub.get("rating_used") or "",
            })
    return payload


def mark_reported(conn, queue_id: int) -> None:
    conn.execute(
        "UPDATE posting_queue SET origin_reported_at = ? WHERE queue_id = ?",
        (_now(), queue_id),
    )
    conn.commit()
