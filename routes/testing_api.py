"""Diagnostics & testing API.

Endpoints under /api/testing/* power the Settings > Diagnostics tab.

  GET  /api/testing/tests                   List all registered tests
  GET  /api/testing/last-results            Last persisted run summary + results
  GET  /api/testing/active                  Returns {run_id} if a run is in-flight
  POST /api/testing/run/{test_id}           Run one test
  POST /api/testing/run-category/{cat}      Run all tests in a category
  POST /api/testing/run-suite               Run full suite
  GET  /api/testing/stream/{run_id}         SSE stream of events for a run
  POST /api/testing/stop/{run_id}           Request graceful cancellation
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from testing import runner, store, streamer
from testing.registry import REGISTRY, registry_snapshot, sorted_categories

logger = logging.getLogger(__name__)
testing_router = APIRouter(prefix="/api/testing")


class RunSuiteBody(BaseModel):
    include_destructive: list[str] = []
    skip_categories: list[str] = []
    only_failed: bool = False  # If true, only re-run last run's failures


class RunCategoryBody(BaseModel):
    include_destructive: list[str] = []


class RunOneBody(BaseModel):
    confirm_destructive: bool = False


@testing_router.get("/tests")
async def list_tests() -> dict[str, Any]:
    """Catalog of registered tests + latest known status per test."""
    last_by_id = store.latest_results_by_test()
    items = []
    for entry in registry_snapshot():
        prior = last_by_id.get(entry["test_id"])
        items.append(
            {
                **entry,
                "last_status": prior.get("status") if prior else None,
                "last_duration_ms": prior.get("duration_ms") if prior else None,
                "last_message": prior.get("message") if prior else None,
            }
        )
    return {
        "tests": items,
        "categories": sorted_categories(),
        "summary": store.latest_summary(),
    }


@testing_router.get("/last-results")
async def last_results() -> dict[str, Any]:
    """Most recent run summary plus per-test results."""
    data = store.load()
    runs = data.get("runs", [])
    if not runs:
        return {"summary": None, "results": [], "started_at": None, "run_id": None}
    last = runs[-1]
    return {
        "run_id": last.get("run_id"),
        "started_at": last.get("started_at"),
        "summary": last.get("summary"),
        "results": last.get("results", []),
    }


@testing_router.get("/active")
async def get_active() -> dict[str, Any]:
    return {"run_id": runner.active_run_id()}


@testing_router.post("/run/{test_id}")
async def run_one(test_id: str, body: RunOneBody = Body(default_factory=RunOneBody)) -> dict[str, Any]:
    if test_id not in REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown test_id: {test_id}")
    try:
        run_id = await runner.run_one(test_id, confirm_destructive=body.confirm_destructive)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except RuntimeError as exc:
        if str(exc) == "run_already_active":
            raise HTTPException(
                status_code=409,
                detail={"error": "run_already_active", "active_run_id": runner.active_run_id()},
            )
        raise
    return {"run_id": run_id}


@testing_router.post("/run-category/{category}")
async def run_category(category: str, body: RunCategoryBody = Body(default_factory=RunCategoryBody)) -> dict[str, Any]:
    if category not in sorted_categories():
        raise HTTPException(status_code=404, detail=f"unknown category: {category}")
    try:
        run_id = await runner.run_category(category, include_destructive=body.include_destructive)
    except RuntimeError as exc:
        if str(exc) == "run_already_active":
            raise HTTPException(
                status_code=409,
                detail={"error": "run_already_active", "active_run_id": runner.active_run_id()},
            )
        raise
    return {"run_id": run_id}


@testing_router.post("/run-suite")
async def run_suite(body: RunSuiteBody = Body(default_factory=RunSuiteBody)) -> dict[str, Any]:
    only_failed_from = None
    if body.only_failed:
        data = store.load()
        runs = data.get("runs", [])
        if runs:
            last = runs[-1]
            only_failed_from = {
                r["test_id"]: r["status"]
                for r in last.get("results", [])
                if r.get("status") in ("failed", "error")
            }
        else:
            only_failed_from = {}
    try:
        run_id = await runner.run_suite(
            include_destructive=body.include_destructive,
            skip_categories=body.skip_categories,
            only_failed_from=only_failed_from,
        )
    except RuntimeError as exc:
        if str(exc) == "run_already_active":
            raise HTTPException(
                status_code=409,
                detail={"error": "run_already_active", "active_run_id": runner.active_run_id()},
            )
        raise
    return {"run_id": run_id}


@testing_router.get("/stream/{run_id}")
async def stream(run_id: str) -> StreamingResponse:
    """SSE stream of events for a run.

    Late subscribers receive replay of buffered events. Heartbeat
    every 15s. Auto-closes on suite_complete.
    """
    return StreamingResponse(
        streamer.sse_for(run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # nginx: don't buffer
            "Connection": "keep-alive",
        },
    )


@testing_router.post("/stop/{run_id}")
async def stop(run_id: str) -> dict[str, Any]:
    ok = runner.request_cancel(run_id)
    return {"ok": ok}
