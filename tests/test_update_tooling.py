"""The shipped server-update tooling is present, LF, syntactically valid, and opt-in for auto (spec 002).

Structural guards — they don't run docker/systemd; they stop the scripts from silently going missing,
turning CRLF (a broken interpreter on Linux), losing a compose path, or making auto-update non-opt-in.
"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _bytes(rel):
    with open(os.path.join(ROOT, rel), "rb") as f:
        return f.read()


def test_update_sh_exists_with_bash_shebang():
    assert _read("update.sh").startswith("#!/usr/bin/env bash")


def test_shipped_shell_and_units_are_lf():
    for rel in [
        "update.sh",
        "server-update/install.sh",
        "server-update/pawpoller-update-poll.service",
        "server-update/pawpoller-update-poll.timer",
        "server-update/pawpoller-auto-update.service",
        "server-update/pawpoller-auto-update.timer",
    ]:
        assert b"\r\n" not in _bytes(rel), rel


def test_update_sh_syntax_ok():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash here (bare Windows) — CI on Linux still checks it")
    for rel in ["update.sh", "server-update/install.sh"]:
        r = subprocess.run([bash, "-n", os.path.join(ROOT, rel)], capture_output=True, text=True)
        assert r.returncode == 0, (rel, r.stderr)


def test_update_sh_covers_both_install_types_and_flags():
    s = _read("update.sh")
    assert "docker compose up -d --build" in s          # build-from-source path
    assert "docker-compose.image.yml" in s              # prebuilt-image path
    for flag in ("--check", "--quiet", "--if-requested"):
        assert flag in s
    assert "--ff-only" in s                              # never clobber local changes (FR-005)


def test_update_sh_is_documented_for_self_hosters():
    assert "update.sh" in _read("docs/SETUP.md")


def test_units_call_update_sh():
    assert "update.sh --if-requested" in _read("server-update/pawpoller-update-poll.service")
    assert "update.sh --quiet" in _read("server-update/pawpoller-auto-update.service")


def test_scheduled_auto_update_is_opt_in():
    inst = _read("server-update/install.sh")
    # The button's poll timer is enabled by a plain install...
    assert "enable --now pawpoller-update-poll.timer" in inst
    # ...but the daily auto-update timer is enabled ONLY inside the --auto branch (SC-006).
    assert 'if [ "$AUTO" = 1 ]' in inst
    assert inst.index('if [ "$AUTO" = 1 ]') < inst.index("enable --now pawpoller-auto-update.timer")


def test_update_sh_backs_up_checks_and_rolls_back():
    """4.43.2: update.sh backs up the database, checks the new commit started, checks the
    database and the startup log, and rolls back with --to. Run for real against fake
    docker/curl in a throwaway repo pair (tests/update_sh_harness.sh)."""
    bash = shutil.which("bash")
    if not bash or not shutil.which("git"):
        pytest.skip("needs bash + git")
    harness = os.path.join(ROOT, "tests", "update_sh_harness.sh")
    assert os.path.isfile(harness), "tests/update_sh_harness.sh is missing (make_public must ship it)"
    r = subprocess.run([bash, harness,
                        os.path.join(ROOT, "update.sh")], capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    parts = {p.split("\n", 1)[0].strip(): p for p in out.split("=== ")[1:]}

    def part(n):
        return next(v for k, v in parts.items() if k.startswith(f"{n}."))
    assert "exit=0" in part(1) and "[healthy, database ok]" in part(1) and "backup script received" in part(1)
    assert "database saved as data/backups/pre-update-" in part(1)
    assert "exit=1" in part(2) and "crashed or failed while starting" in part(2) and "./update.sh --to" in part(2)
    assert "exit=1" in part(3) and "database check failed" in part(3)
    assert "exit=0" in part(4) and "not blocking" in part(4)
    five = part(5)
    assert five.count("exit=0") == 2 and "branch=master" in five, five
    assert "exit=1" in part(6) and "the new build did not start" in part(6)
    assert "exit=1" in part(7) and "No such commit" in part(7)
    assert "exit=1" in part(9) and "No such commit: --orphan" in part(9)
    eight = part(8)          # the checkout was pulled to B, but A is what runs: going back means A
    import re
    want, head = re.search(r"expect-rollback-to=(\w+) head=(\w+)", eight).groups()
    assert want != head and "exit=1" in eight and f"./update.sh --to {want}" in eight, eight


def test_the_commit_reaches_the_image_and_health():
    assert "ARG GIT_SHA" in _read("Dockerfile") and "PAWPOLLER_COMMIT" in _read("Dockerfile")
    assert "GIT_SHA: ${GIT_SHA:-}" in _read("docker-compose.yml")
    assert "GIT_SHA=${{ github.sha }}" in _read(".github/workflows/build.yml")
    assert 'GIT_SHA="$want_commit" docker compose up -d --build' in _read("update.sh")


def test_health_says_which_commit_is_running(monkeypatch):
    import config
    from fastapi.testclient import TestClient
    import dashboard
    monkeypatch.setattr(config, "APP_COMMIT", "abc1234")
    body = TestClient(dashboard.app).get("/api/health").json()
    assert body == {"status": "ok", "version": config.APP_VERSION, "commit": "abc1234"}


def test_the_baked_commit_wins_over_the_checkout(monkeypatch):
    import config
    monkeypatch.setenv("PAWPOLLER_COMMIT", "feedface1234567")
    assert config._app_commit() == "feedface1234"
    monkeypatch.delenv("PAWPOLLER_COMMIT")
    assert len(config._app_commit()) in (0, 7)          # a checkout's short hash, or nothing
