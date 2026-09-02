"""Test registry and result types.

A test is an async function that returns a TestResult. Tests register
themselves via the @register_test decorator at import time. The
runner discovers tests via this module's REGISTRY dict.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

logger = logging.getLogger(__name__)

TestStatus = Literal["passed", "failed", "skipped", "error", "running", "pending"]


@dataclass
class TestResult:
    """Outcome of a single test run."""

    test_id: str
    name: str
    category: str
    status: TestStatus = "pending"
    duration_ms: float = 0.0
    message: str = ""
    details: dict[str, Any] | None = None
    logs: list[str] = field(default_factory=list)
    destructive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "message": self.message,
            "details": self.details,
            "logs": self.logs,
            "destructive": self.destructive,
        }


# A test function takes a TestContext and either returns successfully
# (passed) or raises an exception (failed/error). It can populate
# context.detail() and context.log() while running.
TestFn = Callable[["TestContext"], Awaitable[None]]


@dataclass
class TestSpec:
    """Registry entry for one test."""

    test_id: str
    name: str
    category: str
    description: str
    destructive: bool
    requires_creds: list[str]
    timeout_seconds: float
    fn: TestFn


# Stable category ordering for the UI. Tests outside this list are
# appended at the end in registration order.
CATEGORY_ORDER = (
    "Infrastructure",
    "Dashboard Auth",
    "Platforms — Auth",
    "Platforms — Polling Discovery",
    "Editor / Converter",
    "Story Reader",
    "Posting (Dry-Run)",
    "External Services",
    "Scheduling & Queue",
    "Notifications",
    "Archive",
    "Pytest Suite",
)


REGISTRY: dict[str, TestSpec] = {}


def register_test(
    *,
    test_id: str,
    name: str,
    category: str,
    description: str = "",
    destructive: bool = False,
    requires_creds: list[str] | None = None,
    timeout_seconds: float = 30.0,
):
    """Decorator. Registers an async test function in REGISTRY.

    Args:
        test_id: stable dotted id, e.g. "infra.db.connection".
        name: short human label for the UI.
        category: one of CATEGORY_ORDER (or anything; unknowns sort last).
        description: longer hint shown in the drill-down details panel.
        destructive: if True, the test mutates external state (sends a
            Telegram message, writes to a platform). Run-suite skips it
            unless explicitly enabled; per-test runs require a
            confirm_destructive flag at the API layer.
        requires_creds: optional list of credential keys this test
            needs. If any is missing, the runner marks the test as
            'skipped' before invoking.
        timeout_seconds: per-test timeout (default 30s).
    """

    def decorator(fn: TestFn) -> TestFn:
        if test_id in REGISTRY:
            logger.warning("Test %s registered twice; later wins", test_id)
        REGISTRY[test_id] = TestSpec(
            test_id=test_id,
            name=name,
            category=category,
            description=description,
            destructive=destructive,
            requires_creds=requires_creds or [],
            timeout_seconds=timeout_seconds,
            fn=fn,
        )
        return fn

    return decorator


class TestContext:
    """Context passed to each test fn. Collects logs + details."""

    def __init__(self, spec: TestSpec, on_log: Callable[[str, str], None] | None = None):
        self.spec = spec
        self.logs: list[str] = []
        self.details: dict[str, Any] = {}
        self._on_log = on_log  # callback for live streaming

    def log(self, message: str, level: str = "info") -> None:
        """Append a log line. Also fires the live stream callback."""
        self.logs.append(f"[{level}] {message}")
        if self._on_log is not None:
            try:
                self._on_log(level, message)
            except Exception:  # noqa: BLE001 — never let logging break a test
                pass

    def detail(self, key: str, value: Any) -> None:
        self.details[key] = value

    def skip(self, reason: str) -> "TestSkipped":
        return TestSkipped(reason)


class TestSkipped(Exception):
    """Raise inside a test to mark it skipped with a reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def sorted_categories() -> list[str]:
    """Return all categories present in REGISTRY in display order."""
    seen = {spec.category for spec in REGISTRY.values()}
    ordered = [c for c in CATEGORY_ORDER if c in seen]
    extras = sorted(seen - set(CATEGORY_ORDER))
    return ordered + extras


def registry_snapshot() -> list[dict[str, Any]]:
    """Read-only snapshot for the /api/testing/tests endpoint."""
    out = []
    for cat in sorted_categories():
        for spec in REGISTRY.values():
            if spec.category != cat:
                continue
            out.append(
                {
                    "test_id": spec.test_id,
                    "name": spec.name,
                    "category": spec.category,
                    "description": spec.description,
                    "destructive": spec.destructive,
                    "requires_creds": spec.requires_creds,
                    "timeout_seconds": spec.timeout_seconds,
                }
            )
    return out
