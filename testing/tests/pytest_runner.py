"""Pytest subprocess runner.

One registered test that, when run, spawns `python -m pytest tests/`
as a subprocess, parses the output, and surfaces summary stats as
detail fields. (The individual per-test child results are surfaced
to the UI via the parsed report in `ctx.detail("tests", [...])` —
the frontend renders them as collapsible children under this row.)

This is heavier than the live diagnostics — full run ~10-30s — so
it's its own category and the suite runner can skip it via
`skip_categories=['Pytest Suite']` when desired.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from testing.registry import TestContext, register_test


# Matches lines like:
#   tests/test_foo.py::TestBar::test_baz PASSED              [ 12%]
#   tests/test_foo.py::test_quux FAILED                       [ 78%]
#   tests/test_foo.py::test_quux SKIPPED (reason)             [ 78%]
_LINE_RE = re.compile(
    r"^(?P<file>tests/[\w/]+\.py)::(?P<name>[\w:\[\]\-\.]+)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"(?:\s*(?:\(.*?\))?)?"
    r"(?:\s+\[\s*\d+%\s*\])?\s*$"
)


_STATUS_MAP = {
    "PASSED": "passed",
    "FAILED": "failed",
    "ERROR": "error",
    "SKIPPED": "skipped",
    "XFAIL": "skipped",
    "XPASS": "passed",
}


@register_test(
    test_id="pytest.suite.run",
    name="Run the pytest suite",
    category="Pytest Suite",
    description=(
        "Spawn `python -m pytest tests/ -v --tb=short -p no:cacheprovider` "
        "as a subprocess and parse the output. Individual test results "
        "are surfaced under `tests` in the details panel."
    ),
    timeout_seconds=300.0,
)
async def t_pytest(ctx: TestContext) -> None:
    repo_root = Path(__file__).resolve().parent.parent.parent
    pytest_dir = repo_root / "tests"
    if not pytest_dir.is_dir():
        raise ctx.skip(f"no tests/ directory at {pytest_dir}")

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(pytest_dir),
        "-v",
        "--tb=short",
        "-p",
        "no:cacheprovider",
        "--color=no",
    ]
    ctx.log(f"spawn: {' '.join(cmd)}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(repo_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=280.0)
    except asyncio.TimeoutError:
        proc.kill()
        raise AssertionError("pytest subprocess timed out")
    stdout = stdout_bytes.decode("utf-8", errors="replace")

    tests: list[dict] = []
    for raw in stdout.splitlines():
        m = _LINE_RE.match(raw.strip())
        if not m:
            continue
        tests.append(
            {
                "test_id": f"pytest.{m['file']}::{m['name']}",
                "name": f"{m['file']}::{m['name']}",
                "status": _STATUS_MAP.get(m["status"], "error"),
                "raw_status": m["status"],
            }
        )

    # Pytest's "X passed, Y failed, Z skipped" summary line can have any
    # ordering. Parse each count independently from the tail of stdout
    # rather than fighting one positional regex.
    summary = {"passed": 0, "failed": 0, "errored": 0, "skipped": 0}
    tail = "\n".join(stdout.splitlines()[-5:]) if stdout else ""
    for key, pattern in (
        ("passed", r"(\d+)\s+passed"),
        ("failed", r"(\d+)\s+failed"),
        ("errored", r"(\d+)\s+error(?!\w)"),
        ("skipped", r"(\d+)\s+skipped"),
    ):
        m = re.search(pattern, tail)
        if m:
            summary[key] = int(m.group(1))
    # Fallback to parsed-line counts if summary regex got nothing
    if not any(summary.values()) and tests:
        for t in tests:
            if t["status"] == "passed":
                summary["passed"] += 1
            elif t["status"] == "failed":
                summary["failed"] += 1
            elif t["status"] == "error":
                summary["errored"] += 1
            elif t["status"] == "skipped":
                summary["skipped"] += 1

    ctx.detail("returncode", proc.returncode)
    ctx.detail("test_count", len(tests))
    ctx.detail("tests", tests)
    ctx.detail("summary", summary)
    # Tail of stdout for context on any failures (capped)
    ctx.detail("output_tail", stdout[-4000:])

    # Pass criterion: exit code 0 AND zero failures/errors parsed.
    failures = summary["failed"] + summary["errored"]
    if proc.returncode != 0 or failures > 0:
        msg = (
            f"{summary['failed']} failed, {summary['errored']} errored, "
            f"{summary['skipped']} skipped, {summary['passed']} passed "
            f"(returncode {proc.returncode})"
        )
        raise AssertionError(msg)
