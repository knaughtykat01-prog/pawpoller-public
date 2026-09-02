"""Mirroring you can actually use: status, restart, watcher, wizard (3.18.0).

The operator, after reinstalling the desktop: *"this is not user friendly… the entire
point of mirroring is for it to be seamless."* He was right. Syncing meant:

```powershell
$env:PAWPOLLER_APPDATA_DIR = "$env:APPDATA\\PawPoller"
python scripts/mirror_pull.py
```

…from a **source checkout**, with the app closed, and omitting the environment
variable silently targeted the checkout's own `data/` instead of the install —
which is exactly what happened on the first attempt.

Only one thing in that was a real constraint: SQLite cannot be replaced under a
live app, so the snapshot stages and `init_db()` applies it on the way up. That
stays. What changes is that it becomes a button instead of an instruction.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from dashboard import app
    return TestClient(app)


# ── status: instant, and says WHERE ──────────────────────────────

def test_status_reports_the_resolved_roots():
    """The mistake that actually happened. Artwork does NOT live under
    %APPDATA% by default — on the reference install it resolves to
    `m_x/Archives/Artwork` while `%APPDATA%/PawPoller/data/artwork` sits empty
    beside it, and reading the empty one as "the sync never ran" cost three
    commands to unpick. A status panel that doesn't say what it compared is not
    a status panel."""
    from routes import mirror_api
    st = mirror_api.mirror_status()
    for key in ("artwork", "posts_media", "database", "pending_database"):
        assert key in st["roots"] and st["roots"][key]


def test_status_needs_no_network_and_no_hashing():
    """It must render the page immediately; the expensive comparison is
    `/drift`, called separately. Asserted structurally — status must not reach
    for the manifest builder or an HTTP client."""
    import inspect
    from routes import mirror_api
    src = inspect.getsource(mirror_api.mirror_status)
    assert "build_manifest" not in src
    assert "AsyncClient" not in src


def test_status_says_whether_a_restart_is_owed():
    from routes import mirror_api
    assert "pending_database" in mirror_api.mirror_status()


def test_status_carries_the_watchers_last_answer():
    from routes import mirror_api
    assert "watch" in mirror_api.mirror_status()


# ── drift: one code path, shared ─────────────────────────────────

def test_the_route_and_the_watcher_use_the_same_drift_function():
    """A watcher with its own idea of "out of date" is how the badge and the
    page come to disagree. Same shape as 3.12.1 / 3.13.0 / 3.17.0 / 3.17.4."""
    import inspect
    from mirror import watcher
    assert "compute_drift" in inspect.getsource(watcher.check_once)


def test_drift_refuses_politely_when_not_paired(monkeypatch):
    import asyncio
    import config
    from routes import mirror_api
    from fastapi import HTTPException

    monkeypatch.setattr(config, "get_settings", lambda: {})
    with pytest.raises(HTTPException) as e:
        asyncio.run(mirror_api.compute_drift())
    assert e.value.status_code == 400
    assert "not paired" in e.value.detail


# ── the watcher detects; it must never apply ─────────────────────

def test_the_watcher_never_calls_the_pull():
    """The load-bearing property. Auto-sync was disabled after pairing
    corrupted four accounts; detection is safe, application is a decision."""
    import inspect
    from mirror import watcher
    src = inspect.getsource(watcher)
    for forbidden in ("_run_pull", "startMirrorPull", "start_pull", "extract_bytes"):
        assert forbidden not in src, f"the watcher must not {forbidden}"


def test_the_watcher_is_off_unless_explicitly_enabled(monkeypatch):
    import config
    from mirror import watcher
    monkeypatch.setattr(config, "get_settings",
                        lambda: {"posting_server_url": "https://x"})
    assert watcher._should_run() is False


def test_the_watcher_needs_a_paired_server(monkeypatch):
    import config
    from mirror import watcher
    monkeypatch.setattr(config, "get_settings", lambda: {"mirror_auto_check": True})
    assert watcher._should_run() is False


def test_the_watcher_does_not_run_on_the_server(monkeypatch):
    """The server is the source of truth — there is nothing above it to be out
    of date with."""
    import config
    from mirror import watcher
    monkeypatch.setattr(config, "get_settings",
                        lambda: {"mirror_auto_check": True, "posting_server_url": "https://x"})
    monkeypatch.setattr("posting.scheduler.detect_runtime_mode", lambda: "server")
    assert watcher._should_run() is False


def test_the_watcher_runs_when_all_three_conditions_hold(monkeypatch):
    import config
    from mirror import watcher
    monkeypatch.setattr(config, "get_settings",
                        lambda: {"mirror_auto_check": True, "posting_server_url": "https://x"})
    monkeypatch.setattr("posting.scheduler.detect_runtime_mode", lambda: "desktop")
    assert watcher._should_run() is True


def test_the_check_interval_has_a_floor(monkeypatch):
    """A five-minute floor: the thing being watched changes when a person acts
    on the server, and a tight loop would hash 171 MB for nothing."""
    import config
    from mirror import watcher
    monkeypatch.setattr(config, "get_settings",
                        lambda: {"mirror_check_interval_minutes": 1})
    assert watcher._interval_seconds() >= 300


def test_a_failed_check_never_raises(monkeypatch):
    import asyncio
    from mirror import watcher

    async def _boom():
        raise RuntimeError("server down")
    monkeypatch.setattr("routes.mirror_api.compute_drift", _boom)
    out = asyncio.run(watcher.check_once())          # must not raise
    assert out["error"]


# ── restart: a button, not an instruction ────────────────────────

def test_restart_is_refused_on_the_server(monkeypatch):
    from fastapi import HTTPException
    from routes import mirror_api
    monkeypatch.setattr("posting.scheduler.detect_runtime_mode", lambda: "server")
    with pytest.raises(HTTPException) as e:
        mirror_api.mirror_restart()
    assert e.value.status_code == 409
    assert "docker" in e.value.detail.lower()


def test_restart_is_refused_in_dev(monkeypatch):
    """`sys.executable` is the interpreter in dev, so "start it again" would
    launch a bare REPL and the app would simply be gone."""
    from fastapi import HTTPException
    from routes import mirror_api
    monkeypatch.setattr("posting.scheduler.detect_runtime_mode", lambda: "desktop")
    with pytest.raises(HTTPException) as e:
        mirror_api.mirror_restart()
    assert e.value.status_code == 400
    assert "frozen" in e.value.detail


def test_the_relauncher_reuses_the_updaters_platform_knowledge():
    """It lives beside `_build_update_bat` / `_apply_update_linux` because that
    module already knows how to relaunch this app on each platform. A second
    copy elsewhere is how the two drift — the lesson 3.17.4 paid for."""
    import updater
    assert hasattr(updater, "spawn_relauncher")


def test_the_relauncher_replaces_no_files():
    """It is `apply_update`'s restart shorn of the update. If it ever grows a
    file-replacing command it has become an updater and belongs elsewhere.

    The DOCSTRING is stripped before checking, because it names the very
    commands being forbidden ("no zip, no robocopy") — the first version of
    this test matched its own subject's prose and failed on correct code.
    """
    import ast
    import inspect
    import updater
    tree = ast.parse(inspect.getsource(updater.spawn_relauncher).strip())
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]                       # drop the docstring
    code = ast.unparse(fn)
    for forbidden in ("robocopy", "mv -f", "extractall"):
        assert forbidden not in code, f"relauncher must not {forbidden}"


# ── the settings toggle has its own endpoint ─────────────────────

def test_auto_check_is_not_routed_through_the_preferences_allowlist():
    """`/settings/preferences` accepts a fixed key list and silently discards
    anything else — a toggle sent there would appear to work and never
    persist."""
    import inspect
    from routes import api as api_routes
    src = inspect.getsource(api_routes.save_preferences)
    assert "mirror_auto_check" not in src


def test_auto_check_round_trips(monkeypatch):
    saved = {}
    import config
    from routes import mirror_api
    monkeypatch.setattr(config, "save_settings", lambda d: saved.update(d))
    assert mirror_api.mirror_auto_check({"enabled": True})["enabled"] is True
    assert saved["mirror_auto_check"] is True
    assert mirror_api.mirror_auto_check({})["enabled"] is False


# ── the frontend actually calls all of it ────────────────────────

def _js(name: str) -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "frontend" / "js" / name).read_text(
        encoding="utf-8", errors="replace")


@pytest.mark.parametrize("helper", [
    "getMirrorStatus", "getMirrorDrift", "startMirrorPull",
    "getMirrorPullStatus", "mirrorRestart", "setMirrorAutoCheck",
])
def test_every_mirror_api_helper_exists(helper):
    assert helper in _js("api.js")


@pytest.mark.parametrize("used", [
    "API.getMirrorStatus", "API.getMirrorDrift", "API.startMirrorPull",
    "API.mirrorRestart", "API.setMirrorAutoCheck",
])
def test_the_settings_panel_uses_them(used):
    assert used in _js("app.js")


def test_the_panel_shows_the_resolved_roots_to_the_user():
    """Not just returned by the API — actually rendered, because this is the
    detail whose absence caused a wrong diagnosis."""
    src = _js("app.js")
    assert "roots.artwork" in src and "roots.database" in src


def test_the_wizard_offers_a_sync_on_its_last_step():
    """A fresh pair must not end with an empty-looking app and no visible way
    to fill it."""
    src = _js("app.js")
    assert "setup-sync-now" in src
    assert "syncOffer" in src


def _strip_js_comments(src: str) -> str:
    """Drop /* */ and // comments.

    Necessary because the code comments explaining WHY the runbook was replaced
    quote the runbook verbatim. Checking raw source made this test match its
    own subject's commentary — the same self-reference that broke two earlier
    tests this cycle.
    """
    import re
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", src, flags=re.MULTILINE)


def test_the_frontend_never_tells_anyone_to_run_the_script():
    """The runbook is the thing being replaced. If a UI string still names the
    CLI or the environment variable, the terminal has not gone away."""
    src = _strip_js_comments(_js("app.js"))
    assert "mirror_pull.py" not in src
    assert "PAWPOLLER_APPDATA_DIR" not in src


def test_drift_completes_against_a_reachable_server(monkeypatch, tmp_path):
    """The happy path, which the refusal test above does NOT cover.

    The first live call to `compute_drift` failed with
    `name 'httpx' is not defined` — httpx is imported per-function throughout
    this module and the new code assumed module scope. Every unit test passed,
    because they only ever exercised the not-paired 400. A test that only
    covers the refusal branch proves the function refuses, not that it works.
    """
    import asyncio
    import config
    from mirror import core
    from routes import mirror_api

    (tmp_path / "Piece").mkdir()
    (tmp_path / "Piece" / "masterpiece.json").write_text("{}", encoding="utf-8")
    remote = {"artwork": core.build_manifest(tmp_path, detail=True)}

    class _Resp:
        status_code = 200
        def __init__(self, payload): self._p = payload
        def json(self): return self._p

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **kw):
            if "manifest" in url:
                return _Resp(remote)
            return _Resp({"version": config.APP_VERSION})

    monkeypatch.setattr(config, "get_settings",
                        lambda: {"posting_server_url": "https://example",
                                 "posting_server_api_key": "pp_x"})
    monkeypatch.setattr(mirror_api, "_artwork_root", lambda: tmp_path)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    d = asyncio.run(mirror_api.compute_drift())
    assert d["in_sync"] is True
    assert d["version_match"] is True
    assert "bytes_if_whole_folders" in d and "bytes_to_fetch" in d


def test_drift_reports_version_skew(monkeypatch, tmp_path):
    """Verified live: server 3.17.4 vs desktop 3.18.0 must surface, not hide."""
    import asyncio
    import config
    from mirror import core
    from routes import mirror_api

    tmp_path.mkdir(exist_ok=True)
    remote = {"artwork": core.build_manifest(tmp_path, detail=True)}

    class _Resp:
        status_code = 200
        def __init__(self, p): self._p = p
        def json(self): return self._p

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **kw):
            return _Resp(remote if "manifest" in url else {"version": "0.0.1"})

    monkeypatch.setattr(config, "get_settings",
                        lambda: {"posting_server_url": "https://example"})
    monkeypatch.setattr(mirror_api, "_artwork_root", lambda: tmp_path)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    d = asyncio.run(mirror_api.compute_drift())
    assert d["remote_version"] == "0.0.1"
    assert d["version_match"] is False


def test_the_sync_panel_only_calls_methods_that_exist():
    """⚠ Caught a real bug: the panel called `this._toast(...)`, an idiom
    borrowed from `masterpieces.js`, which app.js does not define. It would
    have thrown "this._toast is not a function" at exactly the moment a restart
    or a settings save failed — i.e. only on the error path, where nobody looks
    until it matters.

    JavaScript resolves method names at call time, so nothing catches this
    before a user hits it: `node --check` parses it happily and the Python
    tests never load the file. Scoped to the mirror functions rather than the
    whole 15k-line file, because other objects legitimately live in there.
    """
    import re
    src = _js("app.js")
    start = src.index("async _loadMirrorSync()")
    end = src.index("/* \u2500\u2500 Security Tab Helpers")
    region = src[start:end]
    # Strip comments so prose mentioning a method does not count as a call.
    region = _strip_js_comments(region)

    defined = set(re.findall(r"^\s{4}(?:async\s+)?(\w+)\s*\(", src, re.MULTILINE))
    called = set(re.findall(r"this\.(\w+)\s*\(", region))
    missing = sorted(called - defined)
    assert missing == [], f"sync panel calls undefined methods: {missing}"


def test_the_relauncher_quotes_the_executable_path():
    """⚠ A regression against a convention this very file documents.

    `_apply_update_linux`, ~80 lines below `spawn_relauncher`, carries the
    comment: "shlex.quote both paths: $APPIMAGE is the user's chosen filename
    and may legally contain quotes/backticks/$ — unquoted they'd break out of
    the mv/exec lines". `spawn_relauncher` was written as that function's
    relaunch half and dropped the hardening.

    Inside a bash double-quoted string, `$(…)`, backticks and `$VAR` still
    expand, so an unquoted path is executed rather than run. Low real risk —
    the install path is chosen by the local user, who has already won — but
    diverging silently from a documented convention is how the next author
    concludes it was never needed.
    """
    import inspect
    import updater
    src = inspect.getsource(updater.spawn_relauncher)
    assert "shlex.quote" in src, "the bash branch must quote the exe path"
    # Windows: `%` is legal in an NTFS filename and cmd expands `%VAR%` even
    # inside quotes, so quoting is not enough — doubling is what escapes it.
    assert 'replace("%", "%%")' in src, "the batch branch must escape percent"


def test_the_relauncher_never_interpolates_a_raw_path():
    """The generated scripts must contain no bare `{exe}` interpolation."""
    import inspect
    import updater
    src = inspect.getsource(updater.spawn_relauncher)
    assert '"{exe}"' not in src and "exec \"{exe}\"" not in src
