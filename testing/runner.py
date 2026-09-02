"""Test runner.

Coordinates execution of one test, one category, or the full suite.
Each run gets its own Streamer for live SSE events. Per-test timeout
enforced via asyncio.wait_for. Cleanup hooks run in finally so partial
state never leaks.

Concurrency: a single global lock prevents two suite runs at once.
Single-test runs share the same lock so a manual click during a
running suite returns 409.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any

import config
from testing import store, streamer
from testing.registry import (
    REGISTRY,
    TestContext,
    TestResult,
    TestSkipped,
    TestSpec,
)

logger = logging.getLogger(__name__)


# Pacing between platform-prefixed tests to avoid burst rate-limits.
# Falls back to 1.0s if config doesn't expose a per-platform constant.
_PLATFORM_DELAY_DEFAULT = 1.0
_PLATFORM_DELAY_OVERRIDES = {
    "ao3": 3.0,
    "sf": 1.5,
    "sqw": 2.0,
    "da": 2.0,
    "tw": 2.0,
}


@dataclass
class _ActiveRun:
    run_id: str
    streamer_: streamer.Streamer
    task: asyncio.Task[Any]


_lock = threading.Lock()
_active: _ActiveRun | None = None


def active_run_id() -> str | None:
    with _lock:
        return _active.run_id if _active is not None else None


def request_cancel(run_id: str) -> bool:
    with _lock:
        if _active is not None and _active.run_id == run_id:
            _active.streamer_.request_cancel()
            return True
    return False


def _missing_creds(spec: TestSpec) -> list[str]:
    """Return list of credential keys this test needs but settings lacks."""
    if not spec.requires_creds:
        return []
    settings = config.get_settings()
    missing = []
    for key in spec.requires_creds:
        v = settings.get(key)
        if not v:
            missing.append(key)
    return missing


async def _execute(spec: TestSpec, s: streamer.Streamer) -> TestResult:
    """Run a single test under a context, capturing logs and timing."""

    started = time.perf_counter()
    result = TestResult(
        test_id=spec.test_id,
        name=spec.name,
        category=spec.category,
        status="running",
        destructive=spec.destructive,
    )

    s.emit(
        "test_start",
        test_id=spec.test_id,
        name=spec.name,
        category=spec.category,
        destructive=spec.destructive,
    )

    def _on_log(level: str, message: str) -> None:
        s.emit(
            "log",
            test_id=spec.test_id,
            level=level,
            message=message,
        )

    ctx = TestContext(spec, on_log=_on_log)

    # Pre-flight credentials check
    missing = _missing_creds(spec)
    if missing:
        elapsed = (time.perf_counter() - started) * 1000.0
        result.status = "skipped"
        result.duration_ms = elapsed
        result.message = f"missing credentials: {', '.join(missing)}"
        result.logs = ctx.logs
        result.details = {"missing_creds": missing}
        s.emit(
            "test_end",
            test_id=spec.test_id,
            status=result.status,
            duration_ms=result.duration_ms,
            message=result.message,
            details=result.details,
        )
        return result

    try:
        await asyncio.wait_for(spec.fn(ctx), timeout=spec.timeout_seconds)
        result.status = "passed"
    except TestSkipped as exc:
        result.status = "skipped"
        result.message = exc.reason
    except asyncio.TimeoutError:
        result.status = "failed"
        result.message = f"timeout after {spec.timeout_seconds}s"
    except AssertionError as exc:
        result.status = "failed"
        result.message = str(exc) or "assertion failed"
    except Exception as exc:  # noqa: BLE001
        result.status = "error"
        result.message = f"{type(exc).__name__}: {exc}"
        ctx.log(traceback.format_exc(), level="error")
    finally:
        result.duration_ms = (time.perf_counter() - started) * 1000.0
        result.logs = ctx.logs
        result.details = ctx.details or None

    s.emit(
        "test_end",
        test_id=spec.test_id,
        status=result.status,
        duration_ms=result.duration_ms,
        message=result.message,
        details=result.details,
    )
    return result


async def _run_list(
    test_ids: list[str],
    s: streamer.Streamer,
    allow_destructive: set[str],
    pace_platforms: bool,
) -> list[TestResult]:
    """Run a sequence of tests, respecting destructive opt-ins and pacing."""

    results: list[TestResult] = []
    started_at = time.perf_counter()
    total = len(test_ids)

    s.emit("suite_start", total=total, started_at=started_at)

    last_platform: str | None = None
    for idx, tid in enumerate(test_ids):
        if s.cancelled:
            s.emit("suite_cancelled", at_index=idx)
            break

        spec = REGISTRY.get(tid)
        if spec is None:
            s.emit("log", test_id=tid, level="warn", message=f"unknown test_id {tid}; skipping")
            continue

        # Destructive guard
        if spec.destructive and spec.test_id not in allow_destructive:
            r = TestResult(
                test_id=spec.test_id,
                name=spec.name,
                category=spec.category,
                status="skipped",
                duration_ms=0.0,
                message="destructive — not opted in",
                destructive=True,
            )
            s.emit("test_start", test_id=spec.test_id, name=spec.name, category=spec.category, destructive=True)
            s.emit("test_end", test_id=spec.test_id, status="skipped", duration_ms=0.0, message=r.message, details=None)
            results.append(r)
            continue

        # Pace platform-prefixed tests so we don't trip rate limits
        if pace_platforms and spec.test_id.startswith("platforms."):
            platform = spec.test_id.split(".")[1] if "." in spec.test_id else None
            if platform and last_platform is not None:
                delay = _PLATFORM_DELAY_OVERRIDES.get(platform, _PLATFORM_DELAY_DEFAULT)
                await asyncio.sleep(delay)
            last_platform = platform

        r = await _execute(spec, s)
        results.append(r)

    elapsed = (time.perf_counter() - started_at) * 1000.0
    summary = {
        "total": total,
        "passed": sum(1 for r in results if r.status == "passed"),
        "failed": sum(1 for r in results if r.status == "failed"),
        "errored": sum(1 for r in results if r.status == "error"),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "duration_ms": elapsed,
        "cancelled": s.cancelled,
    }
    s.emit("suite_complete", summary=summary)
    return results


async def run_suite(
    *,
    include_destructive: list[str] | None = None,
    skip_categories: list[str] | None = None,
    only_failed_from: dict[str, str] | None = None,
) -> str:
    """Kick off a full-suite run. Returns the run_id immediately.

    Args:
        include_destructive: list of destructive test_ids the caller has
            opted into. Empty means destructive tests are skipped.
        skip_categories: list of category names to skip entirely.
        only_failed_from: optional map of test_id -> status, used for
            "Run failed" — only tests in this map (whose status was
            failed or error) are included.

    Raises:
        RuntimeError("run_already_active") if a suite or per-test run
        is currently in flight. Caller should map to HTTP 409.
    """

    test_ids: list[str] = []
    for tid, spec in REGISTRY.items():
        if skip_categories and spec.category in skip_categories:
            continue
        if only_failed_from is not None and tid not in only_failed_from:
            continue
        test_ids.append(tid)

    return await _spawn(test_ids, include_destructive, pace_platforms=True)


async def run_category(category: str, *, include_destructive: list[str] | None = None) -> str:
    test_ids = [tid for tid, spec in REGISTRY.items() if spec.category == category]
    return await _spawn(test_ids, include_destructive, pace_platforms=True)


async def run_one(test_id: str, *, confirm_destructive: bool = False) -> str:
    spec = REGISTRY.get(test_id)
    if spec is None:
        raise KeyError(test_id)
    destructive_optin: list[str] = []
    if spec.destructive:
        if not confirm_destructive:
            raise PermissionError(
                f"test {test_id} is destructive; requires confirm_destructive=true"
            )
        destructive_optin = [test_id]
    return await _spawn([test_id], destructive_optin, pace_platforms=False)


async def _spawn(
    test_ids: list[str],
    include_destructive: list[str] | None,
    *,
    pace_platforms: bool,
) -> str:
    """Allocate streamer + spawn the async runner task."""
    global _active

    with _lock:
        if _active is not None and not _active.task.done():
            raise RuntimeError("run_already_active")
        s = await streamer.new_run()
        loop = asyncio.get_event_loop()
        task = loop.create_task(
            _runner_body(test_ids, s, set(include_destructive or []), pace_platforms)
        )
        _active = _ActiveRun(run_id=s.run_id, streamer_=s, task=task)
    return s.run_id


async def _runner_body(
    test_ids: list[str], s: streamer.Streamer, allow_destructive: set[str], pace: bool
) -> None:
    global _active
    started = time.time()
    try:
        results = await _run_list(test_ids, s, allow_destructive, pace)
        summary = s.events[-1].payload.get("summary") if s.events else None
        if summary is None:
            summary = {"total": len(test_ids), "passed": 0, "failed": 0, "errored": 0, "skipped": 0, "duration_ms": 0.0}
        store.save_run(s.run_id, started, summary, [r.to_dict() for r in results])
        s.close(summary)
    except Exception as exc:  # noqa: BLE001
        logger.exception("runner crashed: %s", exc)
        s.emit("runner_error", message=f"{type(exc).__name__}: {exc}")
        s.close({"total": 0, "passed": 0, "failed": 0, "errored": 1, "skipped": 0, "duration_ms": 0.0, "crashed": True})
    finally:
        with _lock:
            if _active is not None and _active.run_id == s.run_id:
                _active = None
