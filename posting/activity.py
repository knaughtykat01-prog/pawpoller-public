"""What PawPoller is doing right now — the jobs behind the activity pill (spec 017).

One in-memory registry. A *job* is one thing the operator started (publish a story to five
sites, a batch, a scheduled slot firing); it has one *line* per site (or per piece × site in a
batch) with the step that line is on. The posting code reports through module functions and a
context variable, so no posting signature changes and every call is a no-op outside a job —
tests, the CLI and API callers that don't ask for a job see exactly the old behaviour.

    jid = activity.start("publish", "Sample Story — ch 3", ["fa", "ib"])
    with activity.bound(jid):          # or activity.run_in_job(...) for a background task
        activity.step("fa", "Uploading")
        activity.line_done("fa", ok=True, url="https://…")
    activity.finish(jid)

Nothing is stored: a restart forgets every job (the bell and the queue keep the record).
Finished jobs stay visible for an hour, at most 50 of them. A lock guards the registry — the
scheduler fires on its own loop/thread.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import threading
import time
import uuid
from typing import Any, Awaitable, Callable

KEEP_SECONDS = 3600
KEEP_MAX = 50

_lock = threading.Lock()
_jobs: dict[str, dict] = {}                       # insertion-ordered: oldest first
_retry: dict[str, Callable[[str], Awaitable]] = {}  # job id → re-run one line (never serialised)
_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("pp_activity_job", default=None)
_tasks: set = set()   # strong refs: the loop only holds tasks weakly, and a publish must not vanish mid-way


def _line(key: str, label: str, now: float) -> dict:
    return {"key": key, "label": label, "state": "waiting", "step": "Waiting", "detail": "",
            "pct": None, "url": "", "error": "", "at": now}


def start(kind: str, title: str, lines, *, ref: dict | None = None,
          retry: Callable[[str], Awaitable] | None = None) -> str:
    """A new running job. `lines` = keys, or (key, label) pairs. `ref` = where the job's thing
    lives (e.g. {"route": "#/story/X"}); `retry(key)` re-runs one line (Retry in the tray)."""
    now = time.time()
    jid = uuid.uuid4().hex[:12]
    pairs = [(x, x) if isinstance(x, str) else (str(x[0]), str(x[1])) for x in lines]
    job = {"id": jid, "kind": kind, "title": title, "ref": dict(ref or {}), "state": "running",
           "started": now, "finished": None, "cancel": False,
           "lines": {k: _line(k, lab, now) for k, lab in pairs}}
    with _lock:
        _jobs[jid] = job
        if retry:
            _retry[jid] = retry
        _prune(now)
    return jid


def reopen(job_id: str) -> bool:
    """Running again (a scheduled slot's next row joins the job its first row started)."""
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return False
        job.update(state="running", finished=None, cancel=False)
        return True


def current() -> str | None:
    return _current.get()


@contextlib.contextmanager
def bound(job_id: str | None):
    """Report into `job_id` inside this block (None = report nowhere)."""
    token = _current.set(job_id)
    try:
        yield job_id
    finally:
        _current.reset(token)


def _job(job_id: str | None) -> dict | None:
    return _jobs.get(job_id) if job_id else None


def add_line(key: str, label: str | None = None, job_id: str | None = None, *,
             aside: bool = False) -> None:
    """A line discovered late (a batch learns its sites as it plans).

    ``aside`` (spec 021): a follow-up to a site's post — its paired comment. Shown in the
    tray, but not a site: it never counts toward "N of M sites" or the job's state, so a
    comment that didn't go up can't make a post that did look failed (FR-006)."""
    with _lock:
        job = _job(job_id or _current.get())
        if job and key not in job["lines"]:
            job["lines"][key] = _line(key, label or key, time.time())
            if aside:
                job["lines"][key]["aside"] = True


def step(key: str, step: str, detail: str | None = None, pct: float | None = None) -> None:
    """Line `key` is now on `step` ("Preparing", "Uploading", "Part 2 of 4"…). No-op outside a job."""
    with _lock:
        job = _job(_current.get())
        if not job:
            return
        ln = job["lines"].get(key)
        if ln is None:
            ln = job["lines"][key] = _line(key, key, time.time())
        if ln["state"] in ("done", "failed", "cancelled"):
            return
        ln.update(state="active", step=step, at=time.time())
        if detail is not None:
            ln["detail"] = detail
        ln["pct"] = pct


def line_done(key: str, ok: bool, *, url: str = "", error: str = "", step: str | None = None,
              retryable: bool = True) -> None:
    with _lock:
        job = _job(_current.get())
        if not job:
            return
        ln = job["lines"].get(key)
        if ln is None:
            ln = job["lines"][key] = _line(key, key, time.time())
        ln.update(state="done" if ok else "failed", step=step or ("Done" if ok else "Failed"),
                  url=url or "", error=(error or "")[:400], pct=None, at=time.time(),
                  retryable=bool(retryable) and not ok)


def record(key: str, result: dict | None) -> None:
    """Close a line from a manager result dict ({"status": "success"|"error"|…, "url", "error"})."""
    r = result or {}
    st = r.get("status")
    if st in ("success", "updated", "posted", "queued", "skipped"):
        line_done(key, True, url=r.get("url") or r.get("external_url") or "",
                  step={"queued": "Queued", "skipped": "Skipped"}.get(st))
    else:
        line_done(key, False, error=r.get("error") or r.get("message") or "Failed")


def cancelled() -> bool:
    """True once the operator pressed "Cancel the rest" — checked before each next site."""
    with _lock:
        job = _job(_current.get())
        return bool(job and job["cancel"])


def cancel(job_id: str) -> bool:
    """Stop before the next site. A site already uploading finishes (can't be un-sent)."""
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["state"] != "running":
            return False
        job["cancel"] = True
        return True


def finish(job_id: str | None = None, *, error: str = "") -> None:
    """Close the job: lines never reached become 'cancelled' (or 'failed' on a crash)."""
    with _lock:
        jid = job_id or _current.get()
        job = _job(jid)
        if not job or job["finished"]:
            return
        if not job["lines"]:            # nothing was ever sent (e.g. a row another instance claimed)
            _jobs.pop(jid, None)
            _retry.pop(jid, None)
            return
        now = time.time()
        for ln in job["lines"].values():
            # A crash belongs to the site that was in progress; sites never reached were
            # not sent — pinning the crash on them told the operator the wrong thing.
            if ln["state"] == "active" and error:
                ln.update(state="failed", step="Failed", error=error[:400], at=now)
            elif ln["state"] in ("waiting", "active"):
                ln.update(state="cancelled", step="Not sent", at=now)
        if error:
            job["error"] = error[:400]
        states = [ln["state"] for ln in job["lines"].values() if not ln.get("aside")] or \
            [ln["state"] for ln in job["lines"].values()]
        job["state"] = ("failed" if "failed" in states or error else "cancelled" if job["cancel"] or "cancelled" in states
                        else "done")
        job["finished"] = now
        _prune(now)


def run_in_job(job_id: str, coro_fn: Callable[[], Awaitable[Any]]) -> asyncio.Task:
    """Run `coro_fn()` as a background task reporting into `job_id`; the job finishes when it
    returns (or crashes — the crash becomes the unfinished lines' error)."""
    async def _runner():
        with bound(job_id):
            try:
                return await coro_fn()
            except Exception as e:   # the job shows it; the task must not die silently
                finish(job_id, error=str(e) or e.__class__.__name__)
                return None
            finally:
                finish(job_id)
    task = asyncio.ensure_future(_runner())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def launch(kind: str, title: str, lines, coro_fn: Callable[[], Awaitable[Any]], *,
           ref: dict | None = None, retry: Callable[[str], Awaitable] | None = None) -> str:
    """start() + run_in_job(): a publish route's `background: true` path. Returns the job id
    at once; the work carries on after the HTTP reply (the pill and tray follow it)."""
    jid = start(kind, title, lines, ref=ref, retry=retry)
    run_in_job(jid, coro_fn)
    return jid


async def retry(job_id: str, key: str) -> bool:
    """Re-run one line of a finished job, reporting into the same job."""
    with _lock:
        job = _jobs.get(job_id)
        fn = _retry.get(job_id)
        if not job or not fn or key not in job["lines"]:
            return False
        # Only a FAILED line of a FINISHED job (release review, Medium): a second tab, a stale
        # tray or a double click must not post a live copy twice, or race a retry in progress.
        ln = job["lines"][key]
        # …and only one the posting code called safe to re-send (release review, Medium): not
        # when an automatic retry is already queued, the site says it already has it, or part of
        # the line (a chapter, a render) went live — re-running the site would post those again.
        if job["state"] == "running" or ln["state"] != "failed" or not ln.get("retryable", True):
            return False
        job.update(state="running", finished=None, cancel=False)
        job["lines"][key].update(state="waiting", step="Waiting", error="", url="", at=time.time())
    run_in_job(job_id, lambda: fn(key))
    return True


def snapshot() -> list[dict]:
    """Every job, newest first, as plain data for GET /api/activity."""
    with _lock:
        _prune(time.time())
        out = []
        for job in reversed(list(_jobs.values())):
            j = {k: v for k, v in job.items() if k != "lines"}
            j["lines"] = [dict(ln) for ln in job["lines"].values()]
            j["can_retry"] = job["id"] in _retry
            out.append(j)
        return out


def _prune(now: float) -> None:
    """Caller holds the lock. Drop finished jobs past KEEP_SECONDS, then the oldest finished
    ones beyond KEEP_MAX. Running jobs are never dropped."""
    for jid in [j for j, job in _jobs.items() if job["finished"] and now - job["finished"] > KEEP_SECONDS]:
        _jobs.pop(jid, None)
        _retry.pop(jid, None)
    finished = [j for j, job in _jobs.items() if job["finished"]]
    for jid in finished[:max(0, len(finished) - KEEP_MAX)]:
        _jobs.pop(jid, None)
        _retry.pop(jid, None)


def _reset_for_tests() -> None:
    with _lock:
        _jobs.clear()
        _retry.clear()
