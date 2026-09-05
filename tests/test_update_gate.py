"""The startup update gate (4.9.0): check and apply BEFORE the window opens.

Contracts: only packaged builds, only when `auto_update` is on; the release
check is timeboxed and abandoned; a skipped version is honoured; a newer
build is downloaded, applied and the process asked to exit; every failure
falls open to the installed version; main() runs the gate before anything
else; the preferences route carries the two settings; the splash's Tk is
pinned in the spec.
"""
from __future__ import annotations

import sys
import time

import pytest

import update_gate as gate


@pytest.fixture(autouse=True)
def _no_splash(monkeypatch):
    """Never open a Tk window in the suite: run the work plainly."""
    monkeypatch.setattr(gate, "_with_splash", lambda work, title: work(lambda *_a: None))


def _frozen(monkeypatch, value=True):
    monkeypatch.setattr(sys, "frozen", value, raising=False)


class TestEnabled:
    def test_only_packaged_builds(self):
        assert gate.enabled({}, frozen=False) == (False, "not a packaged build")
        assert gate.enabled({}, frozen=True) == (True, "")

    def test_the_switch(self):
        assert gate.enabled({"auto_update": False}, frozen=True) == (False, "turned off in Settings")
        assert gate.enabled({"auto_update": "off"}, frozen=True)[0] is False
        assert gate.enabled({"auto_update": True}, frozen=True)[0] is True
        assert gate.enabled({"auto_update": None}, frozen=True)[0] is True, "unset = on"


class TestCheck:
    def test_a_slow_check_is_abandoned(self):
        def slow():
            time.sleep(1.0)
            return {"available": True}
        t0 = time.monotonic()
        assert gate.check(timeout=0.1, checker=slow) is None
        assert time.monotonic() - t0 < 0.8

    def test_a_failing_check_is_no_update(self):
        def boom():
            raise ConnectionError("offline")
        assert gate.check(timeout=1.0, checker=boom) is None

    def test_an_answer_comes_back(self):
        assert gate.check(timeout=1.0, checker=lambda: {"available": False})["available"] is False


class TestDecide:
    def test_none_when_nothing_newer(self):
        assert gate.decide(None, {}) == "none"
        assert gate.decide({"available": False}, {}) == "none"
        assert gate.decide({"available": True, "latest": "5.0.0"}, {}) == "none", "no download url = nothing to apply"

    def test_skip_honours_one_version_only(self):
        info = {"available": True, "latest": "5.0.0", "download_url": "https://github.com/x"}
        assert gate.decide(info, {"update_skip_version": "5.0.0"}) == "skip"
        assert gate.decide(info, {"update_skip_version": "4.9.9"}) == "apply"
        assert gate.decide(info, {}) == "apply"


class TestRun:
    INFO = {"available": True, "current": "4.8.0", "latest": "4.9.0",
            "download_url": "https://github.com/knaughtykat01-prog/pawpoller-public/releases/download/v4.9.0/PawPoller-windows-x64.zip"}

    def test_off_from_source(self, monkeypatch):
        _frozen(monkeypatch, False)
        assert gate.run({}) == "off:not a packaged build"

    def test_applies_then_asks_the_process_to_exit(self, monkeypatch):
        _frozen(monkeypatch)
        calls = []
        out = gate.run({}, checker=lambda: dict(self.INFO),
                       downloader=lambda url: calls.append(("download", url)) or "C:/tmp/x.zip",
                       applier=lambda p: calls.append(("apply", p)),
                       exit_fn=lambda code: calls.append(("exit", code)))
        assert out == "applied"
        assert calls == [("download", self.INFO["download_url"]), ("apply", "C:/tmp/x.zip"), ("exit", 0)]

    def test_a_failed_download_falls_open(self, monkeypatch):
        _frozen(monkeypatch)
        exited = []

        def bad(url):
            raise OSError("disk full")
        out = gate.run({}, checker=lambda: dict(self.INFO), downloader=bad,
                       applier=lambda p: None, exit_fn=lambda c: exited.append(c))
        assert out.startswith("failed:disk full") and exited == []

    def test_a_failed_apply_falls_open(self, monkeypatch):
        _frozen(monkeypatch)
        exited = []

        def bad(p):
            raise RuntimeError("swap refused")
        out = gate.run({}, checker=lambda: dict(self.INFO), downloader=lambda u: "x.zip",
                       applier=bad, exit_fn=lambda c: exited.append(c))
        assert out.startswith("failed:swap refused") and exited == []

    def test_timeout_starts_the_installed_version(self, monkeypatch):
        _frozen(monkeypatch)

        def slow():
            time.sleep(1.0)
            return dict(self.INFO)
        assert gate.run({}, checker=slow, timeout=0.05, exit_fn=lambda c: pytest.fail("must not exit")) == "timeout"

    def test_skip_and_none(self, monkeypatch):
        _frozen(monkeypatch)
        assert gate.run({"update_skip_version": "4.9.0"}, checker=lambda: dict(self.INFO),
                        exit_fn=lambda c: pytest.fail("must not exit")) == "skip"
        assert gate.run({}, checker=lambda: {"available": False}, exit_fn=lambda c: pytest.fail("must not exit")) == "none"

    def test_the_switch_wins_before_any_network(self, monkeypatch):
        _frozen(monkeypatch)
        assert gate.run({"auto_update": False}, checker=lambda: pytest.fail("no check when off")) == "off:turned off in Settings"


class TestWiring:
    def test_main_runs_the_gate_before_the_database(self):
        src = open("main.py", encoding="utf-8").read()
        i = src.index("def main():")
        body = src[i:]
        assert body.index("update_gate.run()") < body.index("init_db()")

    def test_preferences_carry_the_two_settings(self):
        src = open("routes/api.py", encoding="utf-8").read()
        assert 'update["auto_update"] = bool(body["auto_update"])' in src
        assert '"update_skip_version"' in src and '"auto_update": settings.get("auto_update", True)' in src

    def test_settings_page_has_the_switch_and_skip(self):
        app = open("frontend/js/app.js", encoding="utf-8").read()
        assert 'id="pref-auto-update"' in app and 'id="skip-update-btn"' in app

    def test_the_splash_toolkit_is_pinned_for_the_build(self):
        spec = open("pawpoller.spec", encoding="utf-8").read()
        assert "'tkinter'" in spec and "'tkinter.ttk'" in spec
