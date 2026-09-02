"""Diagnostics & testing tools.

Top-level package for the in-app diagnostics suite surfaced in
Settings > Diagnostics. Live tests for every subsystem (DB, settings,
vault, platform auth, polling discovery, editor/converter, story
reader, posting dry-run, dashboard auth, external services,
scheduling, notifications, archive) plus a pytest-suite runner.

Module structure:

  testing.registry — TestResult dataclass + @register_test decorator
  testing.runner   — async runners (one / category / suite) + cancel
  testing.streamer — per-run SSE event queue
  testing.store    — last-run / history persistence to data/
  testing.tests.*  — concrete tests, each module groups one category

`testing.tests` is imported on startup so every @register_test
decorator fires and populates the REGISTRY before the first
GET /api/testing/tests.
"""
