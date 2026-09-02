"""Mirror API — server → desktop replication of the canonical stores.

Stage 1 of ``docs/specs/desktop_server_mirroring.md``. Logic lives in
``mirror/core.py``; this module is the HTTP shell.

**The endpoints split by direction, and the split is the whole design.** The
*serving* half (``/manifest``, ``/artwork/{name}``, ``/posts-media``,
``/db-snapshot``) answers questions about this install's stores and is what the
server exposes. The *driving* half (``/pull``, ``/pull/status``) reaches out to
a remote install, and is what the desktop runs. Both halves mount on both
machines because ``dashboard.py`` mounts every router unconditionally — that is
a fact about this codebase, not an oversight — so the driving half refuses to
run where it would be wrong (§ownership, 3.5.4) rather than relying on nobody
clicking it.

Transfer is per-folder rather than one archive. The reasoning is in
``mirror/core.py``'s docstring; the short version is that 173 MB in a
``BytesIO`` on an e2-micro is a memory risk, a dropped connection costs the
whole transfer, and the steady state should be "fetch the three folders that
changed" instead of a full re-download.
"""
from __future__ import annotations

import asyncio
import logging
import tarfile
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

import config
from database import posting_queries
from mirror import core, handoff, registry, shr
from mirror import watcher as _watcher

logger = logging.getLogger(__name__)

mirror_router = APIRouter(prefix="/api/mirror", tags=["mirror"])

# Progress for the long-running pull. A pull moves ~173 MB on first run, which
# is minutes on a domestic uplink — far too long to hold an HTTP request open,
# so /pull starts a task and the UI polls /pull/status. Mirrors how the pollers
# report progress elsewhere in this codebase.
_pull_state: dict = {"running": False, "phase": "idle", "message": "",
                     "started_at": None, "finished_at": None, "result": None}
_pull_lock = asyncio.Lock()


def _artwork_root() -> Path:
    from posting import artwork_reader
    return artwork_reader.get_artwork_archive_path()


def _posts_media_root() -> Path:
    return config.DATA_DIR / "posts_media"


def _story_root() -> Path:
    from posting import story_reader
    return story_reader.get_archive_path()


# The story archive's own exclusion rule. Stories carry derived-but-needed
# trees the artwork rule would strip, and drop `Backups/` the artwork rule
# would keep — see `mirror.core.is_mirrored_story_file`.
def _story_include():
    return core.is_mirrored_story_file


def _set_phase(phase: str, message: str = "", **extra) -> None:
    _pull_state["phase"] = phase
    _pull_state["message"] = message
    _pull_state.update(extra)


# ── Serving half (the source install answers these) ───────────

@mirror_router.get("/manifest")
def mirror_manifest(detail: bool = False):
    """Describe every mirrorable store on this install.

    ``detail=false`` (the default) returns one line per artwork folder — name,
    file count, bytes, digest. That is a few KB for 163 folders and is all the
    puller needs to decide what to fetch. ``detail=true`` additionally lists
    every file with its own hash, which is for diagnosing a folder that will
    not converge, not for routine use.
    """
    db_path = Path(config.DB_PATH)
    try:
        artwork = core.build_manifest(_artwork_root(), detail=detail)
        stories = core.build_manifest(_story_root(), detail=detail,
                                      include=_story_include())
        posts_media = core.build_flat_manifest(_posts_media_root(), detail=detail)
    except OSError as e:
        raise HTTPException(500, detail=f"Cannot read local stores: {e}")

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "app_version": config.APP_VERSION,
        "artwork": artwork,
        # 3.19.0. Absent from older servers, so the puller treats a missing
        # key as "this server does not mirror stories" rather than "the server
        # has no stories" — the same 404-ambiguity lesson as 3.18.1.
        "stories": stories,
        "posts_media": posts_media,
        "database": {
            "exists": db_path.exists(),
            "bytes": db_path.stat().st_size if db_path.exists() else 0,
            "excluded_tables": list(core.SNAPSHOT_EXCLUDE_TABLES),
        },
    }


@mirror_router.get("/artwork/{name}")
def mirror_artwork_folder(name: str):
    """Stream one artwork folder as .tar.gz.

    ``name`` is a folder name, never a path. It is resolved and re-checked
    against the archive root, so ``..%2F..%2Fetc`` cannot walk out even if a
    proxy has already decoded it.
    """
    root = _artwork_root().resolve()
    folder = (root / name).resolve()
    if folder != root and root not in folder.parents:
        raise HTTPException(400, detail="Invalid artwork name")
    if not folder.is_dir():
        raise HTTPException(404, detail=f"Artwork folder not found: {name}")

    payload = core.pack_folder(folder, arcname=folder.name)
    return Response(
        content=payload,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{folder.name}.tar.gz"'},
    )


@mirror_router.post("/artwork/{name}/files")
def mirror_artwork_files(name: str, body: dict):
    """Stream only the named files from one artwork folder as .tar.gz (3.18.0).

    Body: ``{"paths": ["masterpiece.json", ...]}`` — folder-relative posix
    paths, taken from the manifest's per-file detail.

    The manifest has always carried a sha256 per file and the puller has always
    asked for it, but the only way to GET anything was `/artwork/{name}`, the
    whole folder. So a 3 KB metadata edit re-downloaded the 29 MB image beside
    it. Measured on the live pair before this existed: **158.6 MB moved to
    deliver 0.2 MB of change**, with 277 identical files re-fetched.

    Whole-folder fetch is retained and is still correct for a folder the client
    does not have at all — there is nothing to diff against.
    """
    root = _artwork_root().resolve()
    folder = (root / name).resolve()
    if folder != root and root not in folder.parents:
        raise HTTPException(400, detail="Invalid artwork name")
    if not folder.is_dir():
        raise HTTPException(404, detail=f"Artwork folder not found: {name}")

    paths = body.get("paths")
    if not isinstance(paths, list) or not paths:
        raise HTTPException(400, detail="paths must be a non-empty list")
    if len(paths) > 5000:
        raise HTTPException(400, detail="Too many paths in one request")
    if not all(isinstance(p, str) for p in paths):
        raise HTTPException(400, detail="paths must be strings")

    try:
        payload = core.pack_folder_files(folder, paths, arcname=folder.name)
    except core.MirrorSecurityError as e:
        # The same refusal the extractor would give, on the way OUT. A request
        # naming a file is where traversal arrives; it does not get its own
        # private check (3.17.4 had to remove three copies of one).
        raise HTTPException(400, detail=str(e))

    return Response(
        content=payload,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{folder.name}-partial.tar.gz"'},
    )


@mirror_router.get("/story/{name}")
def mirror_story_folder(name: str):
    """Stream one story folder as .tar.gz (3.19.0).

    The story archive was the one canonical store the mirror did not carry:
    artwork, post media and the database all crossed, while stories moved only
    through `pawsync`/`pawpull`, two maintainer shell scripts. A desktop
    restored from the server therefore came back unable to POST anything —
    which is exactly what happened after an uninstall wiped
    `%APPDATA%/PawPoller/story-archive` and a queued FurAffinity job failed
    with "Story folder not found".
    """
    root = _story_root().resolve()
    folder = (root / name).resolve()
    if folder != root and root not in folder.parents:
        raise HTTPException(400, detail="Invalid story name")
    if not folder.is_dir():
        raise HTTPException(404, detail=f"Story folder not found: {name}")
    payload = core.pack_folder(folder, arcname=folder.name, include=_story_include())
    return Response(
        content=payload,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{folder.name}.tar.gz"'},
    )


@mirror_router.post("/story/{name}/files")
def mirror_story_files(name: str, body: dict):
    """Stream only the named files from one story folder (3.19.0).

    Same per-file contract as the artwork endpoint, and the same guard: every
    requested path goes through `_reject_foreign_absolute` plus a resolved
    containment check inside `pack_folder_files`. It does not get its own copy.
    """
    root = _story_root().resolve()
    folder = (root / name).resolve()
    if folder != root and root not in folder.parents:
        raise HTTPException(400, detail="Invalid story name")
    if not folder.is_dir():
        raise HTTPException(404, detail=f"Story folder not found: {name}")

    paths = body.get("paths")
    if not isinstance(paths, list) or not paths:
        raise HTTPException(400, detail="paths must be a non-empty list")
    if len(paths) > 5000:
        raise HTTPException(400, detail="Too many paths in one request")
    if not all(isinstance(p, str) for p in paths):
        raise HTTPException(400, detail="paths must be strings")
    try:
        payload = core.pack_folder_files(folder, paths, arcname=folder.name,
                                         include=_story_include())
    except core.MirrorSecurityError as e:
        raise HTTPException(400, detail=str(e))
    return Response(
        content=payload,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{folder.name}-partial.tar.gz"'},
    )


@mirror_router.get("/posts-media")
def mirror_posts_media():
    """Stream the whole ``posts_media`` store as .tar.gz (it is ~1.6 MB)."""
    root = _posts_media_root()
    if not root.is_dir():
        raise HTTPException(404, detail="No posts_media directory on this install")
    payload = core.pack_files(root)
    return Response(
        content=payload,
        media_type="application/gzip",
        headers={"Content-Disposition": 'attachment; filename="posts_media.tar.gz"'},
    )


@mirror_router.get("/db-snapshot")
def mirror_db_snapshot():
    """Return a transactionally consistent copy of this install's database.

    Snapshotted with ``Connection.backup()``, never copied — see
    ``mirror.core.snapshot_database``. ``session_cache`` is stripped before the
    bytes leave.
    """
    db_path = Path(config.DB_PATH)
    if not db_path.exists():
        raise HTTPException(404, detail="No database on this install")

    tmp = db_path.with_name(f"{db_path.name}.snapshot.{int(time.time())}")
    try:
        info = core.snapshot_database(db_path, tmp)
        payload = tmp.read_bytes()
    except Exception as e:
        logger.error("DB snapshot failed: %s", e, exc_info=True)
        raise HTTPException(500, detail=f"Snapshot failed: {e}")
    finally:
        tmp.unlink(missing_ok=True)

    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="pawpoller-snapshot.db"',
            "X-Mirror-Tables": str(info["tables"]),
        },
    )


# ── Driving half (the receiving install runs these) ───────────

def _mirror_target(body: dict) -> tuple[str, str]:
    settings = config.get_settings()
    server_url = (body.get("server_url") or settings.get("posting_server_url", "")).rstrip("/")
    api_key = body.get("api_key") or settings.get("posting_server_api_key", "")
    if not server_url:
        raise HTTPException(400, detail="No server URL configured (posting_server_url).")
    # Matches auto_sync.py / settings_api.py: the payload carries a bearer
    # token and, for the database, every row in the install. Plaintext HTTP is
    # only tolerable to a loopback peer.
    if not server_url.startswith("https://") and "127.0.0.1" not in server_url \
            and "localhost" not in server_url:
        raise HTTPException(400, detail="Refusing to mirror over plain HTTP to a non-local server.")
    return server_url, api_key


async def _run_pull(server_url: str, api_key: str, *, dry_run: bool,
                    include_db: bool, include_media: bool, only: list[str] | None,
                    push_first: bool = True, confirm_deletes=()) -> dict:
    import httpx

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    artwork_root = _artwork_root()
    artwork_root.mkdir(parents=True, exist_ok=True)

    summary: dict = {"dry_run": dry_run, "server_url": server_url}

    # ── Shared tables, upward, FIRST (Stage 3) ──
    # Not politeness about ordering: the database phase below stages a snapshot
    # that REPLACES this install's database wholesale, so every shared-table row
    # written here and not yet sent — a renamed persona, a new collection, an
    # ignored submission, an undelivered tombstone — is destroyed by the pull
    # that follows. Pushing first is the correctness condition, so a failed push
    # aborts the pull rather than being reported beside it.
    if push_first and include_db:
        _set_phase("shr", "Pushing local shared-table changes...")
        try:
            summary["shr_push"] = await _run_shr_push(
                server_url, api_key, confirm_deletes=confirm_deletes, dry_run=dry_run)
        except Exception as e:
            logger.error("Mirror: shared-table push failed, aborting pull: %s", e)
            raise RuntimeError(
                f"Shared-table push failed, so the pull was not run: {e}. The pull "
                f"replaces this database wholesale — running it now would discard the "
                f"local changes the push was carrying."
            ) from e

    async with httpx.AsyncClient(timeout=300.0, headers=headers) as client:
        _set_phase("manifest", "Fetching remote manifest...")
        # detail=true on both sides: the diff needs per-file hashes to tell a
        # local file the server lacks (expected — this mirror never deletes)
        # from one that actually diverged. Without it a folder carrying a
        # surplus file re-downloads on every run forever.
        resp = await client.get(f"{server_url}/api/mirror/manifest", params={"detail": "true"})
        if resp.status_code != 200:
            raise RuntimeError(f"Remote manifest returned {resp.status_code}: {resp.text[:200]}")
        remote = resp.json()

        # A dry run must report BOTH stores. Returning after the artwork plan
        # would show a clean story sync that had never been considered — the
        # same shape as treating an absent "stories" key as an empty one.
        def _plan_for(remote_entry: dict, local_root, include=None) -> dict:
            pl = core.diff_manifests(
                remote_entry, core.build_manifest(local_root, detail=True,
                                                  include=include))
            if only:
                keep = set(only)
                pl["fetch"] = [n for n in pl["fetch"] if n in keep]
            return pl

        if dry_run:
            summary["artwork_plan"] = _plan_for(remote["artwork"], artwork_root)
            if "stories" in remote:
                summary["stories_plan"] = _plan_for(
                    remote["stories"], _story_root(), include=_story_include())
            else:
                summary["stories"] = {"skipped": "server does not mirror stories "
                                                 "(needs 3.19.0+)"}
            _set_phase("done", "Dry run — nothing written.")
            return summary

        # ── Folder stores: artwork, then stories ──
        #
        # One loop, two stores (3.19.0). They differ only in root, endpoint and
        # exclusion rule; writing the story pass as a second copy of the
        # artwork pass is how the two would drift, which is precisely the bug
        # `deploy/archive_sync_rules.py` exists to document — pawsync and
        # pawpull each had their own exclude list and a push-then-pull stopped
        # being idempotent.
        #
        # A folder the client LACKS is fetched whole; one that merely DIFFERS
        # fetches only the files that differ.
        async def _sync_folder_store(kind: str, remote_entry: dict, local_root,
                                     url_seg: str, include=None) -> dict:
            local_manifest = core.build_manifest(local_root, detail=True,
                                                 include=include)
            store_plan = core.diff_manifests(remote_entry, local_manifest)
            if only:
                wanted_names = set(only)
                store_plan["fetch"] = [n for n in store_plan["fetch"] if n in wanted_names]

            fetched, failed, fell_back = [], [], []
            plans = store_plan.get("changed_files") or {}
            moved = 0
            local_root.mkdir(parents=True, exist_ok=True)
            for idx, name in enumerate(store_plan["fetch"], 1):
                wanted = plans.get(name)
                _set_phase(kind, f"{kind} {idx}/{len(store_plan['fetch'])}: {name}",
                           progress={"current": idx, "total": len(store_plan["fetch"]),
                                     "name": name})
                try:
                    r = None
                    if wanted:
                        r = await client.post(
                            f"{server_url}/api/mirror/{url_seg}/{name}/files",
                            json={"paths": wanted})
                        if r.status_code == 404:
                            # Ambiguous: folder gone, or server too old for the
                            # per-file route. Settle it with the whole-folder
                            # GET rather than guessing (3.18.1).
                            r = None
                            wanted = None
                            fell_back.append(name)
                    if r is None:
                        r = await client.get(f"{server_url}/api/mirror/{url_seg}/{name}")
                    if r.status_code != 200:
                        failed.append({"name": name, "error": f"HTTP {r.status_code}"})
                        continue
                    core.extract_bytes(r.content, local_root)
                    moved += len(r.content)
                    fetched.append(name)
                except (httpx.HTTPError, core.MirrorSecurityError,
                        tarfile.TarError, OSError) as e:
                    logger.warning("Mirror: %s %s failed: %s", kind, name, e)
                    failed.append({"name": name, "error": str(e)})
            return {
                "plan": store_plan,
                "result": {"fetched": len(fetched), "failed": failed,
                           "left_alone": store_plan["local_only"],
                           "bytes_moved": moved, "fell_back": fell_back,
                           "partial": sum(1 for n in fetched
                                          if plans.get(n) and n not in fell_back),
                           "whole": sum(1 for n in fetched
                                        if not plans.get(n) or n in fell_back)},
            }

        art = await _sync_folder_store("artwork", remote["artwork"], artwork_root,
                                       "artwork")
        summary["artwork_plan"] = art["plan"]
        summary["artwork"] = art["result"]

        # ⚠ A server older than 3.19.0 has no "stories" key at all. Absent is
        # NOT the same as empty: treating it as empty would silently report a
        # clean story sync that never happened. Same lesson as 3.18.1's 404.
        if "stories" in remote:
            st = await _sync_folder_store("stories", remote["stories"], _story_root(),
                                          "story", include=_story_include())
            summary["stories_plan"] = st["plan"]
            summary["stories"] = st["result"]
        else:
            summary["stories"] = {"skipped": "server does not mirror stories "
                                             "(needs 3.19.0+)"}

        # ── posts_media ──
        if include_media:
            _set_phase("posts_media", "Fetching posts_media...")
            try:
                r = await client.get(f"{server_url}/api/mirror/posts-media")
                if r.status_code == 200:
                    media_root = _posts_media_root()
                    media_root.mkdir(parents=True, exist_ok=True)
                    names = core.extract_bytes(r.content, media_root)
                    summary["posts_media"] = {"files": len(names)}
                elif r.status_code == 404:
                    summary["posts_media"] = {"files": 0, "note": "none on server"}
                else:
                    summary["posts_media"] = {"error": f"HTTP {r.status_code}"}
            except Exception as e:
                logger.warning("Mirror: posts_media failed: %s", e)
                summary["posts_media"] = {"error": str(e)}

        # ── Database, staged for the next restart ──
        if include_db:
            _set_phase("database", "Fetching database snapshot...")
            db_path = Path(config.DB_PATH)
            try:
                r = await client.get(f"{server_url}/api/mirror/db-snapshot")
                if r.status_code != 200:
                    summary["database"] = {"error": f"HTTP {r.status_code}"}
                else:
                    # Staged through core so the pending slot's -wal/-shm are
                    # cleared with it; writing the .db alone leaves a previous
                    # attempt's sidecars beside a database they don't belong to.
                    check = core.stage_pending_snapshot(db_path, r.content)
                    if not check["ok"]:
                        summary["database"] = {"error": f"integrity check failed: {check['integrity']}"}
                    else:
                        summary["database"] = {
                            "staged": True, "bytes": len(r.content),
                            "tables": check["tables"], "accounts": check["accounts"],
                            "note": "Applied on next PawPoller restart; "
                                    "the current database is preserved as a .bak file.",
                        }
            except Exception as e:
                logger.error("Mirror: database snapshot failed: %s", e, exc_info=True)
                core.discard_pending_snapshot(db_path)
                summary["database"] = {"error": str(e)}

    if not dry_run:
        # Recorded so the Sync page can answer "when did this last work?"
        # without re-hashing 171 MB. Stored on success only — a failed run must
        # not read afterwards as a clean sync.
        try:
            config.save_settings({"mirror_last_sync": _now_iso()})
        except Exception:  # noqa: BLE001 — bookkeeping never sinks a sync
            logger.debug("Mirror: could not record last-sync time", exc_info=True)

    _set_phase("done", "Mirror pull complete.")
    return summary


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolved_roots() -> dict:
    """Where this install actually keeps each store.

    Surfaced because guessing wrong is the mistake that actually happens: the
    artwork root is CONFIGURABLE and does not live under %APPDATA% by default
    — on the reference install it resolves to `m_x/Archives/Artwork`, while
    `%APPDATA%/PawPoller/data/artwork` sits empty beside it. During the session
    that prompted this page, that mismatch was read as "the sync never ran".
    A status page that does not say which directories it compared is not a
    status page.
    """
    from pathlib import Path as _P
    db = _P(config.DB_PATH)
    return {
        "artwork": str(_artwork_root()),
        "stories": str(_story_root()),
        "posts_media": str(_posts_media_root()),
        "database": str(db),
        "pending_database": str(core.pending_snapshot_path(db)),
    }


@mirror_router.get("/status")
def mirror_status():
    """Cheap, instant answer to "where do I stand?" — no hashing, no network.

    Deliberately split from `/drift`: this must render the page immediately,
    while the comparison costs a local walk plus a remote manifest fetch. The
    UI shows this first and fills in drift after.
    """
    from pathlib import Path as _P
    from posting.scheduler import detect_runtime_mode

    settings = config.get_settings()
    db = _P(config.DB_PATH)
    pending = core.pending_snapshot_path(db)
    return {
        "mode": detect_runtime_mode(),
        "paired": bool(settings.get("posting_server_url")),
        "server_url": settings.get("posting_server_url", ""),
        "has_api_key": bool(settings.get("posting_server_api_key")),
        "last_sync": settings.get("mirror_last_sync", ""),
        "local_version": config.APP_VERSION,
        "roots": _resolved_roots(),
        # A staged snapshot means a restart is owed. The UI turns this into a
        # button rather than an instruction (§Phase 3).
        "pending_database": pending.exists(),
        "pending_bytes": pending.stat().st_size if pending.exists() else 0,
        "auto_check": bool(settings.get("mirror_auto_check", False)),
        "running": _pull_state["running"],
        # Whatever the background watcher last found, served from memory so a
        # badge costs nothing. `in_sync: null` means it has not checked yet.
        "watch": dict(_watcher.STATE),
    }


async def compute_drift() -> dict:
    """What differs between here and the server, without transferring anything.

    Runs the same `diff_manifests` the pull uses, so the number shown is the
    number that will move — one fact, one code path. Reports BOTH the
    whole-folder cost and the per-file cost, because the gap between them is
    the thing 3.18.0 fixed and the thing a person notices.

    Shared by `GET /drift` and the background watcher deliberately. A watcher
    with its own copy of "what counts as out of date" is how the badge and the
    page come to disagree — the exact shape of bug this release cycle kept
    finding (3.12.1, 3.13.0, 3.17.0, 3.17.4).
    """
    settings = config.get_settings()
    server_url = (settings.get("posting_server_url") or "").rstrip("/")
    api_key = settings.get("posting_server_api_key") or ""
    if not server_url:
        raise HTTPException(400, detail="This install is not paired with a server.")

    # Imported here, matching every other network path in this module — httpx
    # is not a module-level import and assuming it was cost a 502 on the first
    # live call ("name 'httpx' is not defined"), which the unit tests missed
    # because they only ever exercised the not-paired refusal.
    import httpx

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=120.0, headers=headers) as client:
            r = await client.get(f"{server_url}/api/mirror/manifest",
                                 params={"detail": "true"})
            if r.status_code != 200:
                raise HTTPException(502, detail=f"Server returned {r.status_code}")
            remote = r.json()
            health = await client.get(f"{server_url}/api/health")
            remote_version = (health.json() or {}).get("version", "")                 if health.status_code == 200 else ""
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — surfaced, not swallowed
        raise HTTPException(502, detail=f"Could not reach the server: {e}")

    plan = core.diff_manifests(
        remote["artwork"], core.build_manifest(_artwork_root(), detail=True))
    # Stories count too (3.19.0). An older server has no "stories" key, and
    # absent is not empty — reporting "in sync" for a store the server cannot
    # even offer would be a lie the UI then repeats.
    stories_plan = None
    if "stories" in remote:
        stories_plan = core.diff_manifests(
            remote["stories"],
            core.build_manifest(_story_root(), detail=True,
                                include=core.is_mirrored_story_file))

    def _files(pl):
        return sum(len(v) for v in (pl.get("changed_files") or {}).values())

    n_files = _files(plan) + (_files(stories_plan) if stories_plan else 0)
    whole = plan.get("fetch_bytes", 0) + (
        stories_plan.get("fetch_bytes", 0) if stories_plan else 0)
    real = plan.get("fetch_file_bytes", whole) + (
        stories_plan.get("fetch_file_bytes", 0) if stories_plan else 0)
    fetch_count = len(plan["fetch"]) + (len(stories_plan["fetch"]) if stories_plan else 0)
    return {
        "in_sync": not fetch_count,
        "folders_to_fetch": fetch_count,
        "artwork_to_fetch": len(plan["fetch"]),
        "stories_to_fetch": len(stories_plan["fetch"]) if stories_plan else 0,
        "stories_supported": stories_plan is not None,
        "missing": len(plan["missing"]),
        "changed": len(plan["changed"]),
        "unchanged": len(plan["unchanged"]),
        "files_to_fetch": n_files,
        "bytes_to_fetch": real,
        "bytes_if_whole_folders": whole,
        "local_only": plan["local_only"],
        "local_version": config.APP_VERSION,
        "remote_version": remote_version,
        "version_match": (not remote_version) or remote_version == config.APP_VERSION,
        "checked_at": _now_iso(),
    }


@mirror_router.get("/drift")
async def mirror_drift():
    """Thin route over `compute_drift()` — see that function."""
    return await compute_drift()


@mirror_router.post("/auto-check")
def mirror_auto_check(body: dict):
    """Turn background drift CHECKING on or off (3.18.0).

    Its own endpoint rather than a new key in `/settings/preferences`, whose
    handler is a per-key allow-list — an unlisted key there is accepted and
    silently discarded, which would have made this toggle appear to work.

    ⚠ This governs DETECTION only. Nothing about it applies a sync. Auto-sync
    was switched off on the desktop after pairing corrupted four server
    accounts through offset `account_id`s; the cause is fixed, but the reason
    for caution was never the specific bug — it was that a silent bidirectional
    process can damage the catalogue faster than a person notices. Telling
    someone what changed is safe; changing it for them is a different decision.
    """
    enabled = bool(body.get("enabled"))
    config.save_settings({"mirror_auto_check": enabled})
    return {"status": "saved", "enabled": enabled}


@mirror_router.post("/restart")
def mirror_restart():
    """Quit and come straight back, applying any staged database (3.18.0).

    SQLite cannot be swapped under a live app on Windows, so a pulled snapshot
    waits as `pawpoller.db.pending` and `init_db()` applies it before any
    connection opens. That constraint is genuine and unchanged — what changes
    here is that it stops being a sentence in a runbook and becomes a button.

    Refused on the SERVER: there, the process lifecycle belongs to Docker, and
    a self-exit would stop the container rather than restart it (`docker
    compose restart` is the tool). Refused in dev for the reason
    `spawn_relauncher` gives: `sys.executable` is the interpreter, so the app
    would exit and not return.
    """
    import threading
    from posting.scheduler import detect_runtime_mode

    if detect_runtime_mode() == "server":
        raise HTTPException(409, detail=(
            "This install runs under Docker, which owns the process lifecycle. "
            "Restart it with `docker compose restart` instead."))

    import updater
    try:
        updater.spawn_relauncher()
    except RuntimeError as e:
        raise HTTPException(400, detail=str(e))

    from pathlib import Path as _P
    pending = core.pending_snapshot_path(_P(config.DB_PATH)).exists()
    # The helper only WAITS; this process has to die inside its grace window or
    # it will be started twice. Same shape as the in-app updater's exit.
    import os as _os
    threading.Timer(1.5, lambda: _os._exit(0)).start()
    return {"status": "restarting", "applying_database": pending}


@mirror_router.post("/pull")
async def mirror_pull(body: dict):
    """Pull the canonical stores down from the configured server.

    Refused on the install that *is* the source of truth. Mirroring is
    directional by design (spec D4): the desktop cannot originate a snapshot,
    so a server pulling from a desktop would overwrite the real data with the
    subordinate copy — the single most destructive thing this module could do.

    Body: ``{server_url?, api_key?, dry_run?, include_db?, include_media?, only?[]}``
    """
    from posting.scheduler import detect_runtime_mode

    mode = detect_runtime_mode()
    if mode == "server" and not body.get("force"):
        raise HTTPException(
            409,
            detail="This install is the mirror source, not a target. Pulling here would "
                   "overwrite authoritative data with a copy. Run the pull on the desktop.",
        )

    if _pull_state["running"]:
        raise HTTPException(409, detail="A mirror pull is already running.")

    server_url, api_key = _mirror_target(body)
    dry_run = bool(body.get("dry_run"))
    include_db = body.get("include_db", True)
    include_media = body.get("include_media", True)
    only = body.get("only") or None
    # Defaults to on. Turning it off is for the case where the local database is
    # known to be a stale copy worth discarding — a repair, not a sync — and it
    # is opt-out rather than opt-in because the failure it prevents is silent.
    push_first = body.get("push_first", True)
    confirm_deletes = body.get("confirm_deletes") or ()

    async def _task():
        try:
            result = await _run_pull(server_url, api_key, dry_run=dry_run,
                                     include_db=include_db, include_media=include_media,
                                     only=only, push_first=push_first,
                                     confirm_deletes=confirm_deletes)
            _pull_state["result"] = result
        except Exception as e:
            logger.error("Mirror pull failed: %s", e, exc_info=True)
            _set_phase("error", str(e))
            _pull_state["result"] = {"error": str(e)}
        finally:
            _pull_state["running"] = False
            _pull_state["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    async with _pull_lock:
        if _pull_state["running"]:
            raise HTTPException(409, detail="A mirror pull is already running.")
        _pull_state.update({"running": True, "phase": "starting", "message": "",
                            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "finished_at": None, "result": None})
        asyncio.create_task(_task())

    return {"status": "started", "dry_run": dry_run, "server_url": server_url}


@mirror_router.get("/pull/status")
def mirror_pull_status():
    """Progress of the running (or last) pull."""
    return dict(_pull_state)


# ── Work handoff (Stage 2) ────────────────────────────────────
# Serving half: /handoff/jobs, /handoff/claim, /handoff/result — run by the
# install that OWNS the queue (the server). Driving half: /handoff/pull,
# /handoff/report — run by the install that can actually do the work.

@mirror_router.get("/handoff/jobs")
def handoff_jobs(limit: int = 20):
    """Desktop-only jobs waiting on this install, in natural keys."""
    from database.db import get_connection
    conn = get_connection()
    try:
        return {"jobs": handoff.describe_jobs(conn, limit=limit)}
    finally:
        conn.close()


@mirror_router.post("/handoff/claim")
def handoff_claim(body: dict):
    """Atomically take a job so no second worker can also take it.

    Reuses ``claim_queue_item`` (3.5.4) rather than a status write, because the
    guard that matters is ``status = 'pending'`` — the thing that stops the same
    post going out twice to a live platform.
    """
    from database.db import get_connection
    queue_id = body.get("origin_queue_id")
    if queue_id is None:
        raise HTTPException(400, detail="origin_queue_id is required")
    conn = get_connection()
    try:
        claimed = posting_queries.claim_queue_item(
            conn, int(queue_id), body.get("claimed_by") or "handoff")
    finally:
        conn.close()
    if not claimed:
        raise HTTPException(409, detail="Job is no longer pending (claimed or cancelled).")
    return {"claimed": True, "origin_queue_id": int(queue_id)}


@mirror_router.post("/handoff/result")
def handoff_result(body: dict):
    """Record a desktop-executed post here, through the normal write path."""
    from database.db import get_connection
    conn = get_connection()
    try:
        return handoff.apply_result(conn, body)
    except LookupError as e:
        raise HTTPException(404, detail=str(e))
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    finally:
        conn.close()


def _require_worker_side(body: dict) -> None:
    """The driving half belongs on the install that can do the work."""
    from posting.scheduler import detect_runtime_mode
    if detect_runtime_mode() == "server" and not body.get("force"):
        raise HTTPException(
            409,
            detail="This install is the job source, not the worker. Run the handoff "
                   "on the desktop — the jobs exist precisely because the server "
                   "cannot execute them.",
        )


@mirror_router.post("/handoff/pull")
async def handoff_pull(body: dict):
    """Claim the server's desktop-only jobs and import them into this queue.

    Imported rows are ordinary queue rows — the existing scheduler runs them
    with retries, cancellation and the atomic claim already in place. Nothing
    about posting is reimplemented here.
    """
    import httpx
    from database.db import get_connection

    _require_worker_side(body)
    server_url, api_key = _mirror_target(body)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    limit = int(body.get("limit") or 20)

    imported, skipped, failed = [], [], []
    async with httpx.AsyncClient(timeout=60.0, headers=headers) as client:
        r = await client.get(f"{server_url}/api/mirror/handoff/jobs",
                             params={"limit": limit})
        if r.status_code != 200:
            raise HTTPException(502, detail=f"Remote jobs returned {r.status_code}")
        jobs = r.json().get("jobs", [])

        for job in jobs:
            origin_id = job.get("origin_queue_id")
            # Claim BEFORE importing. The other order would create local work
            # for a job another worker may already hold.
            try:
                c = await client.post(f"{server_url}/api/mirror/handoff/claim",
                                      json={"origin_queue_id": origin_id,
                                            "claimed_by": body.get("claimed_by") or "desktop"})
            except httpx.HTTPError as e:
                failed.append({"origin_queue_id": origin_id, "error": str(e)})
                continue
            if c.status_code == 409:
                skipped.append({"origin_queue_id": origin_id, "reason": "already claimed"})
                continue
            if c.status_code != 200:
                failed.append({"origin_queue_id": origin_id,
                               "error": f"claim HTTP {c.status_code}"})
                continue

            conn = get_connection()
            try:
                result = handoff.import_job(conn, job, server_url)
                (imported if result["imported"] else skipped).append(result)
            except LookupError as e:
                # Claimed but unusable locally. Hand it back rather than
                # stranding it in 'processing' on the server forever.
                failed.append({"origin_queue_id": origin_id, "error": str(e)})
                try:
                    await client.post(
                        f"{server_url}/api/mirror/handoff/result",
                        json={"origin_queue_id": origin_id, "platform": job["platform"],
                              "story_name": job["story_name"],
                              "chapter_index": job.get("chapter_index") or 0,
                              "content_type": job.get("content_type") or "story",
                              "success": False, "error": f"desktop refused: {e}"})
                except httpx.HTTPError:
                    logger.warning("Handoff: could not release job #%s", origin_id)
            finally:
                conn.close()

    return {"offered": len(jobs), "imported": imported,
            "skipped": skipped, "failed": failed}


@mirror_router.post("/handoff/report")
async def handoff_report(body: dict):
    """Send outcomes for finished imported jobs back to their origin server.

    A sweep, not an inline call from the scheduler: a crash between posting and
    reporting retries instead of losing the result, and the scheduler never
    blocks on the network. ``origin_reported_at`` is written only after the
    server acknowledges, so the delivery is at-least-once and made idempotent
    on the far side by ``apply_result`` refusing an already-completed row.
    """
    import httpx
    from database.db import get_connection

    _require_worker_side(body)
    server_url, api_key = _mirror_target(body)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    conn = get_connection()
    try:
        rows = handoff.pending_reports(conn, server_url)
        payloads = [(row["queue_id"], handoff.build_report(conn, row)) for row in rows]
    finally:
        conn.close()

    reported, failed = [], []
    async with httpx.AsyncClient(timeout=60.0, headers=headers) as client:
        for local_id, payload in payloads:
            try:
                r = await client.post(f"{server_url}/api/mirror/handoff/result",
                                      json=payload)
            except httpx.HTTPError as e:
                failed.append({"queue_id": local_id, "error": str(e)})
                continue
            if r.status_code != 200:
                failed.append({"queue_id": local_id,
                               "error": f"HTTP {r.status_code}: {r.text[:120]}"})
                continue
            conn = get_connection()
            try:
                handoff.mark_reported(conn, local_id)
            finally:
                conn.close()
            reported.append({"queue_id": local_id,
                             "origin_queue_id": payload["origin_queue_id"],
                             "success": payload["success"]})

    return {"pending": len(payloads), "reported": reported, "failed": failed}


# ── Shared-table push (Stage 3) ───────────────────────────────
# Receiving half: /shr/apply — runs on the install that OWNS the shared tables
# (the server). Driving half: /shr/push and /shr/preview — run on the desktop.
#
# The direction is the opposite of Stage 1's and that is the point. Stage 1
# replaces the desktop's database wholesale from the server, which carries the
# shared tables DOWN in the one form that cannot duplicate a snapshot row. What
# it also does is overwrite anything the desktop wrote and never sent — a
# persona renamed there, a collection built there, a submission ignored there.
# So the shared tables have to go UP before that happens, which is why the push
# is a phase of the pull below rather than a separate button beside it.

@mirror_router.post("/shr/apply")
def shr_apply(body: dict):
    """Apply a pushed shared-table bundle to this install.

    Refused on a desktop: applying an upward bundle where the upward bundle is
    generated is the mirror image of the server pulling, and just as wrong.
    """
    from database.db import get_connection
    from posting.scheduler import detect_runtime_mode

    if detect_runtime_mode() != "server" and not body.get("force"):
        raise HTTPException(
            409,
            detail="This install is not the shared-table owner. A push is applied on "
                   "the server; applying one here would write a copy over the "
                   "originating data.",
        )

    bundle = body.get("bundle")
    if not isinstance(bundle, dict):
        raise HTTPException(400, detail="bundle is required")

    conn = get_connection()
    try:
        return shr.apply_bundle(
            conn, bundle,
            confirmed_delete_tables=body.get("confirm_deletes") or ())
    except registry.UnregisteredTable as e:
        raise HTTPException(400, detail=str(e))
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    finally:
        conn.close()


@mirror_router.get("/shr/preview")
def shr_preview():
    """What this install would push, without pushing it.

    Read-only and safe to call anywhere — it is the answer to "is there
    unpushed local work?", which is the question worth asking before a pull.
    """
    from database.db import get_connection
    conn = get_connection()
    try:
        bundle = shr.export_bundle(conn)
        return {
            "generated_at": bundle["generated_at"],
            "row_count": bundle["row_count"],
            "tables": {k: len(v) for k, v in bundle["tables"].items() if v},
            "tombstones": bundle["tombstones"],
            "registry": registry.audit(conn),
        }
    finally:
        conn.close()


async def _run_shr_push(server_url: str, api_key: str, *,
                        confirm_deletes=(), dry_run: bool = False) -> dict:
    """Build this install's shared-table bundle and hand it to the server."""
    import httpx
    from database.db import get_connection

    conn = get_connection()
    try:
        bundle = shr.export_bundle(conn)
    finally:
        conn.close()

    if dry_run:
        return {"dry_run": True, "row_count": bundle["row_count"],
                "tables": {k: len(v) for k, v in bundle["tables"].items() if v},
                "tombstones": len(bundle["tombstones"])}

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=180.0, headers=headers) as client:
        r = await client.post(f"{server_url}/api/mirror/shr/apply",
                              json={"bundle": bundle,
                                    "confirm_deletes": list(confirm_deletes)})
    if r.status_code != 200:
        raise RuntimeError(f"Remote apply returned {r.status_code}: {r.text[:200]}")
    result = r.json()

    # Clear only what the server confirmed it applied. A tombstone the server
    # surfaced for confirmation, or failed on, stays queued — the same
    # at-least-once shape as Stage 2's result sweep.
    applied = result.get("deletes", {}).get("applied") or []
    if applied:
        conn = get_connection()
        try:
            from mirror import tombstones
            cleared = tombstones.clear(conn, applied)
        finally:
            conn.close()
        result["tombstones_cleared"] = cleared

    result["sent"] = {"row_count": bundle["row_count"],
                      "tombstones": len(bundle["tombstones"])}
    return result


@mirror_router.post("/shr/push")
async def shr_push(body: dict):
    """Send this install's shared-table rows up to the server.

    Body: ``{server_url?, api_key?, dry_run?, confirm_deletes?[]}``

    ``confirm_deletes`` names the tables whose recorded deletions should
    actually be applied upstream. Tables classed SURFACE — ``masterpiece_members``
    — are otherwise reported back and left undone, because unlinking a piece is
    close enough to deleting art that it gets shown and confirmed rather than
    propagated on a timer.
    """
    from posting.scheduler import detect_runtime_mode

    if detect_runtime_mode() == "server" and not body.get("force"):
        raise HTTPException(
            409,
            detail="This install owns the shared tables; there is nowhere above it to "
                   "push to. Run the push on the desktop.",
        )
    server_url, api_key = _mirror_target(body)
    try:
        return await _run_shr_push(server_url, api_key,
                                   confirm_deletes=body.get("confirm_deletes") or (),
                                   dry_run=bool(body.get("dry_run")))
    except RuntimeError as e:
        raise HTTPException(502, detail=str(e))
