"""The shipped server-update tooling is present, LF, syntactically valid, and opt-in for auto (spec 002).

Structural guards — they don't run docker/systemd; they stop the scripts from silently going missing,
turning CRLF (a broken interpreter on Linux), losing a compose path, or making auto-update non-opt-in.
"""
import os
import shutil
import subprocess

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
        return  # no bash here (bare Windows) — CI on Linux still checks it
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
    assert "update.sh" in _read("docs/SELF_HOSTING.md")


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
