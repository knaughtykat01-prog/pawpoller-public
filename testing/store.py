"""Persistence of last-run results.

Writes to data/diagnostics_results.json so the Diagnostics tab can
show current health on first open after a container restart. Keeps
the last 10 runs as history.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_HISTORY_LIMIT = 10


def _path() -> Path:
    data_dir = config.SETTINGS_PATH.parent / "data"
    data_dir.mkdir(exist_ok=True)
    return data_dir / "diagnostics_results.json"


def load() -> dict[str, Any]:
    """Read the persisted store. Returns {} if missing or corrupt."""
    p = _path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("diagnostics_results.json unreadable, ignoring: %s", e)
        return {}


def latest_results_by_test() -> dict[str, dict[str, Any]]:
    """Map of test_id -> last TestResult dict, from the most recent run."""
    data = load()
    runs = data.get("runs", [])
    if not runs:
        return {}
    last = runs[-1]
    return {r["test_id"]: r for r in last.get("results", [])}


def latest_summary() -> dict[str, Any] | None:
    data = load()
    runs = data.get("runs", [])
    if not runs:
        return None
    return runs[-1].get("summary")


def save_run(run_id: str, started_at: float, summary: dict[str, Any], results: list[dict[str, Any]]) -> None:
    """Append a finished run to the history, trimming to _HISTORY_LIMIT."""
    with _LOCK:
        data = load()
        runs = data.get("runs", [])
        runs.append(
            {
                "run_id": run_id,
                "started_at": started_at,
                "summary": summary,
                "results": results,
            }
        )
        runs = runs[-_HISTORY_LIMIT:]
        data["runs"] = runs
        p = _path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
