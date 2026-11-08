"""074 STANDALONEBRIDGE: the standalone desktop window offers its pages five named actions, nothing else.

The old window handed pywebview a ``js_api`` object; pywebview resolves any dotted name a page sends on it,
private ones included, so a script on a dashboard page could walk from the object to the app's insides. These
tests drive the real ``webview.util.js_bridge_call`` with a stand-in window (072's pattern) and never start the
app: ``run_standalone`` / ``run_connected`` touch real data.
"""
from __future__ import annotations

import ast
import inspect
import re
import sys
import threading
import time
import types
from pathlib import Path

import pytest

wv_util = pytest.importorskip("webview.util")      # desktop-only; the server image has no pywebview

import desktop_gate  # noqa: E402

REAL_BRIDGE = wv_util.js_bridge_call
START = "http://127.0.0.1:8420/"
ROOT = Path(__file__).resolve().parent.parent
WV_DIR = Path(wv_util.__file__).resolve().parent


# ── stand-ins ────────────────────────────────────────────────────────────────

class FakeWindow:
    """What js_bridge_call and the actions use of a pywebview Window."""

    def __init__(self, url=START, js_api=None):
        self._functions: dict = {}
        self._js_api = js_api
        self.url = url
        self.replies: list[str] = []
        self.replied = threading.Event()
        self.picked: list = []
        self.dialogs: list = []

    def expose(self, *funcs):
        for f in funcs:
            self._functions[f.__name__] = f

    def get_current_url(self):
        if isinstance(self.url, Exception):
            raise self.url
        return self.url

    def evaluate_js(self, script):
        self.replies.append(script)
        self.replied.set()

    def create_file_dialog(self, *a, **k):
        return self.picked

    answer = True

    def create_confirmation_dialog(self, title, text):
        self.dialogs.append((title, text))
        return self.answer


def fake_webview(renderer="edgechromium"):
    calls: list = []
    wv = types.SimpleNamespace(
        util=types.SimpleNamespace(js_bridge_call=REAL_BRIDGE),
        renderer=renderer, OPEN_DIALOG=10, windows=[], settings={}, calls=calls,
    )

    def initialize(*a, **k):
        calls.append("initialize")
        return types.SimpleNamespace(renderer=renderer)

    def create_window(*a, **k):
        calls.append(("create_window", k, getattr(wv.util.js_bridge_call, "_pp_guard", False)))
        w = FakeWindow(url=k.get("url") or START, js_api=k.get("js_api"))
        orig_expose = w.expose

        def expose(*funcs):
            calls.append(("expose", tuple(f.__name__ for f in funcs)))
            orig_expose(*funcs)
        w.expose = expose
        w.events = types.SimpleNamespace(closing=_Evt(), closed=_Evt())
        wv.windows.append(w)
        return w

    wv.initialize = initialize
    wv.create_window = create_window
    return wv


class _Evt:
    def __iadd__(self, fn):
        return self


def call(window, name, params=(), value_id="1", bridge=None, wait=True):
    """Send one page call through the bridge; return the reply script ('' when none came)."""
    window.replied.clear()
    n = len(window.replies)
    (bridge or REAL_BRIDGE)(window, name, list(params), value_id)
    if wait:
        window.replied.wait(2)
    else:
        time.sleep(0.05)
    return window.replies[n] if len(window.replies) > n else ""


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    """A clean desktop_gate for every test."""
    monkeypatch.setattr(desktop_gate, "_confirm", None)
    monkeypatch.setattr(desktop_gate, "_last_no", {})
    monkeypatch.setattr(desktop_gate, "_picks", {})
    return desktop_gate


# ── Phase 2: own_page (R2) ───────────────────────────────────────────────────

@pytest.mark.parametrize("url", [START, "http://127.0.0.1:8420/artwork?x=1", "http://127.0.0.1:8420"])
def test_own_page_accepts_the_dashboard(url):
    assert desktop_gate.own_page(url, START)


BAD_URLS = [
    "http://127.0.0.1:84201/", "http://8420.evil.example/", "http://127.0.0.1.evil.example:8420/",
    "http://u@127.0.0.1:8420/", "http://u:p@127.0.0.1:8420/", "https://127.0.0.1:8420/", "http://localhost:8420/",
    "about:blank", "blob:http://127.0.0.1:8420/abc", "data:text/html,hi", "file:///C:/x", None, "",
    "http://127.0.0.1:99999/", 42,
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_own_page_refuses_everything_else(url):
    assert not desktop_gate.own_page(url, START)


def test_own_page_refuses_on_any_exception():
    class Weird(str):
        def __getattribute__(self, n):
            raise RuntimeError("boom")
    assert not desktop_gate.own_page(Weird("http://127.0.0.1:8420/"), START)


# ── Phase 2: one desktop question at a time (R8) ─────────────────────────────

def test_second_confirm_refused_while_one_open():
    entered, release = threading.Event(), threading.Event()

    def slow(title, text):
        entered.set()
        release.wait(5)
        return True
    desktop_gate.set_confirm(slow)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("first", desktop_gate.ask("a", "t", "x")))
    t.start()
    assert entered.wait(2)
    started = time.monotonic()
    assert desktop_gate.ask("b", "t", "x") == "busy"
    assert time.monotonic() - started < 1          # refused at once, never queued
    release.set()
    t.join(5)
    assert out["first"] == "yes"


def test_no_then_immediate_retry_refused(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(desktop_gate, "_clock", lambda: now[0])
    asked = []
    desktop_gate.set_confirm(lambda t, x: asked.append(t) or False)
    assert desktop_gate.ask("folder", "t1", "x") == "no"
    assert desktop_gate.ask("folder", "t2", "x") == "no"
    assert asked == ["t1"]                          # the second was refused without a dialog
    assert desktop_gate.ask("other", "t3", "x") == "no" and asked == ["t1"]   # any "no" pauses every kind
    now[0] += 11
    desktop_gate.set_confirm(lambda t, x: True)
    assert desktop_gate.ask("folder", "t4", "x") == "yes"


def test_ask_releases_the_lock_when_confirm_raises():
    def boom(t, x):
        raise RuntimeError("dialog broke")
    desktop_gate.set_confirm(boom)
    assert desktop_gate.ask("a", "t", "x") == "no"
    desktop_gate._last_no.clear()                   # past the pause: the lock itself was released
    desktop_gate.set_confirm(lambda t, x: True)
    assert desktop_gate.ask("b", "t", "x") == "yes"


def test_only_true_counts_as_yes():
    for answer in (None, 1, "yes", [True]):
        desktop_gate.set_confirm(lambda t, x, a=answer: a)
        assert desktop_gate.ask(f"k{answer!r}", "t", "x") == "no"


# ── Phase 2: transport guard (R7) ────────────────────────────────────────────

def _guarded():
    wv = fake_webview()
    desktop_gate.harden(wv)
    return wv, wv.util.js_bridge_call


def _ok(*a):
    return {"ok": True}


@pytest.mark.parametrize("value_id", ['"]);alert(1);//', "1\n", "١٢", "", "1" * 33, ["1"], {"a": 1}, 1, None])
def test_transport_drops_crafted_reply_id(value_id):
    _, guard = _guarded()
    w = FakeWindow()
    w.expose(_ok)
    assert call(w, "_ok", value_id=value_id, bridge=guard, wait=False) == ""


@pytest.mark.parametrize("name", ["pywebviewMoveWindow", "pywebviewEventHandler", "pywebviewAsyncCallback",
                                  "pywebviewStateUpdate", "pywebviewStateDelete", "made_up", "_ok.__globals__",
                                  None, 7, ["_ok"]])
def test_transport_drops_unknown_names(name):
    _, guard = _guarded()
    w = FakeWindow()
    w.expose(_ok)
    assert call(w, name, bridge=guard, wait=False) == ""


def test_transport_passes_an_exposed_name():
    _, guard = _guarded()
    w = FakeWindow()
    w.expose(_ok)
    assert '"ok": true' in call(w, "_ok", value_id="12", bridge=guard)


def test_guard_never_raises():
    _, guard = _guarded()

    class Hostile:
        @property
        def _functions(self):
            raise RuntimeError("no")
    assert guard(Hostile(), "x", [], "1") is None


def test_guard_installed_on_util_before_start(monkeypatch):
    wv = fake_webview()
    desktop_gate.harden(wv)
    guard = wv.util.js_bridge_call
    assert getattr(guard, "_pp_guard", False)
    # a backend imported after harden takes the name from util, as edgechromium/qt do
    late = types.ModuleType("webview.platforms.late_backend")
    late.js_bridge_call = wv.util.js_bridge_call
    assert late.js_bridge_call is guard
    # one already imported is patched by name
    early = types.ModuleType("webview.platforms.early_backend")
    early.js_bridge_call = REAL_BRIDGE
    monkeypatch.setitem(sys.modules, "webview.platforms.early_backend", early)
    wv2 = fake_webview()
    desktop_gate.harden(wv2)
    assert getattr(early.js_bridge_call, "_pp_guard", False)


def test_harden_is_idempotent():
    wv = fake_webview()
    desktop_gate.harden(wv)
    first_guard, first_init = wv.util.js_bridge_call, wv.initialize
    desktop_gate.harden(wv)
    assert wv.util.js_bridge_call is first_guard and wv.initialize is first_init


def _src(name):
    return (WV_DIR / name).read_text(encoding="utf-8")


def test_bridge_patch_targets_exist_in_pywebview():
    assert re.search(r"def js_bridge_call\(\s*window", _src("util.py"))
    for backend in ("platforms/edgechromium.py", "platforms/qt.py"):
        s = _src(backend)
        imp = re.search(r"from webview\.util import\s*\(?(.*?)\)?\n(?!\s)", s, re.S)
        assert imp and "js_bridge_call" in imp.group(1), backend
        assert re.search(r"\bjs_bridge_call\(self\.", s), backend
    assert re.search(r"def on_script_notify\(self,\s*_?\w*,\s*args\)", _src("platforms/edgechromium.py"))
    assert re.search(r"def call\(self,\s*func_name,\s*param,\s*value_id\)", _src("platforms/qt.py"))
    start = _src("__init__.py").split("def start(", 1)[1]
    assert start.index("guilib = initialize(") < start.index("guilib.create_window(windows[0])")


# ── Phase 3 (US1): the five actions, by name, with the same results ──────────

import main  # noqa: E402  (after the stand-ins: main's import is heavy)
import desktop_agent  # noqa: E402


class _Queue:
    def __init__(self):
        self.added = []

    def pending_count(self):
        return 2

    def add(self, kind, payload):
        self.added.append((kind, payload))


@pytest.fixture
def app(monkeypatch):
    """The standalone window as _open_standalone_window builds it, on a fake webview."""
    for name in ("_sa_webview", "_sa_window", "_sa_url", "_helper_sender", "_journal_open"):
        monkeypatch.setattr(main, name, getattr(main, name))
    sender = types.SimpleNamespace(queue=_Queue(), last_message="m")
    monkeypatch.setattr(main, "_helper_sender", sender)
    wv = fake_webview()
    w = main._open_standalone_window(wv, START)
    return wv, w


def test_standalone_exposes_exactly_the_page_calls():
    names = set()
    for p in (ROOT / "frontend" / "js").rglob("*.js"):
        s = p.read_text(encoding="utf-8")
        assert "pywebview.api[" not in s, p
        assert not re.search(r"=\s*(window\.)?pywebview\.api\s*[;,)]", s), p
        names |= set(re.findall(r"pywebview\.api\.([A-Za-z_]\w*)", s))
    names.discard("agent_login")                   # connected mode's own (072)
    offered = {f.__name__ for f in main.STANDALONE_ACTIONS}
    assert offered == names
    assert offered == {"open_image_dialog", "fa_journal", "helper_signin", "helper_http_ok", "helper_status"}
    assert "helper_http_ok_list" not in offered


def test_standalone_window_wiring(app):
    wv, w = app
    kinds = [c if isinstance(c, str) else c[0] for c in wv.calls]
    cw = next(c for c in wv.calls if isinstance(c, tuple) and c[0] == "create_window")
    assert cw[2] is True                           # the guard was in place before the window existed
    assert cw[1].get("js_api") is None
    assert kinds.index("create_window") < kinds.index("expose")
    assert next(c for c in wv.calls if isinstance(c, tuple) and c[0] == "expose")[1] == tuple(
        f.__name__ for f in main.STANDALONE_ACTIONS)
    assert desktop_gate._confirm is not None       # the folder rule is on in the standalone window
    assert w._pp_start_url == START                # WebView2's Source check applies to this window
    assert getattr(wv, "_pp_hardened", False)


def test_connected_hardens_before_its_first_window():
    src = inspect.getsource(main.run_connected)
    assert "desktop_gate.harden(webview)" in src
    assert src.index("desktop_gate.harden(webview)") < src.index("webview.create_window(")


class _Secret:
    def reveal(self):
        return "PLANTED-SECRET"


class _OldStyleApi:
    """The shape of the old window's js_api object: actions plus the app's own state on the same object."""

    def __init__(self):
        self._helper_sender = _Secret()

    def open_image_dialog(self):
        return []


def test_standalone_canary_old_api_object_leaks():
    """The gap was real: through pywebview's own call path, a private name walks to the app's state."""
    w = FakeWindow(js_api=_OldStyleApi())
    assert "PLANTED-SECRET" in call(w, "_helper_sender.reveal")


@pytest.mark.parametrize("probe,params", [
    ("_helper_sender.reveal", []), ("__class__", []), ("open_image_dialog.__globals__.get", ["_PLANTED"]),
    ("helper_signin.__wrapped__.__globals__.get", ["_PLANTED"]), ("helper_signin.__closure__", []),
    ("made_up", []),
])
def test_standalone_bridge_refuses_private_and_dotted_names(app, monkeypatch, probe, params):
    wv, w = app
    monkeypatch.setattr(main, "_PLANTED", "PLANTED-SECRET", raising=False)
    monkeypatch.setattr(main, "_helper_sender", _Secret())
    for bridge in (REAL_BRIDGE, wv.util.js_bridge_call):   # with and without the transport guard
        reply = call(w, probe, params, bridge=bridge, wait=False)
        assert "PLANTED" not in reply
    assert w.dialogs == []


@pytest.mark.parametrize("name,args", [
    ("open_image_dialog", [1]), ("fa_journal", [1]), ("fa_journal", [1, "t", "m", "0", "x"]),
    ("helper_signin", ["https://example.com"]), ("helper_http_ok", []), ("helper_status", [1]),
])
def test_wrong_arity_is_a_plain_refusal(app, name, args):
    assert getattr(main, name)(*args) == main._REFUSALS[name]


def test_no_action_returns_exception_text(app, monkeypatch, caplog):
    import logging

    from auth import fa_journal_window
    wv, w = app

    def boom(*a, **k):
        raise RuntimeError("C:\\secret\\path")
    monkeypatch.setattr(w, "create_file_dialog", boom)
    monkeypatch.setattr(fa_journal_window, "open_fa_journal", boom)
    monkeypatch.setattr(desktop_agent, "helper_signin", boom)
    monkeypatch.setattr(desktop_agent, "set_http_ok", boom)
    monkeypatch.setattr(main._helper_sender.queue, "pending_count", boom)
    with caplog.at_level(logging.DEBUG):
        outs = [main.open_image_dialog(), main.fa_journal(1, "t", "m", "0"),
                main.helper_signin("https://example.com", "ABC234"), main.helper_http_ok("https://example.com", True),
                main.helper_status()]
    assert "secret" not in repr(outs)
    assert "secret" not in caplog.text
    assert outs == [main._REFUSALS[n] for n in
                    ("open_image_dialog", "fa_journal", "helper_signin", "helper_http_ok", "helper_status")]


def test_same_results_as_before(app, monkeypatch):
    from auth import fa_journal_window
    wv, w = app
    w.picked = ("C:\\art\\a.png",)
    assert main.open_image_dialog() == ["C:\\art\\a.png"]
    w.picked = None
    assert main.open_image_dialog() == []
    seen = []
    monkeypatch.setattr(fa_journal_window, "open_fa_journal",
                        lambda *a, **k: seen.append((a, k)) or {"ok": True})
    assert main.fa_journal(5, "T", "M", "2") == {"ok": True}
    assert seen[0][0][:3] == ("T", "M", "2")
    assert main.helper_status() == {"pending": 2, "message": "m"}


def test_http_ok_returns_no_list(app, monkeypatch):
    saved = []
    monkeypatch.setattr(desktop_agent, "set_http_ok", lambda s, y: saved.append((s, y)) or ["x"])
    assert main.helper_http_ok("http://192.168.1.5:8420", True) == {"ok": True}
    assert main.helper_http_ok("http://192.168.1.5:8420", False) == {"ok": True}
    assert saved == [("http://192.168.1.5:8420", True), ("http://192.168.1.5:8420", False)]


def _journal_stub(monkeypatch, result=None, closers=None):
    from auth import fa_journal_window

    def stub(title, message, rating, on_posted=None, on_closed=None):
        if closers is not None:
            closers.append(on_closed)
        if isinstance(result, Exception):
            raise result
        return result or {"ok": True}
    monkeypatch.setattr(fa_journal_window, "open_fa_journal", stub)


def test_one_journal_window_at_a_time(app, monkeypatch):
    closers: list = []
    _journal_stub(monkeypatch, closers=closers)
    outs: list = []
    ts = [threading.Thread(target=lambda: outs.append(main.fa_journal(1, "t", "m", "0"))) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert sum(1 for o in outs if o == {"ok": True}) == 1
    assert all(o == {"ok": False, "message": desktop_gate.MSG_JOURNAL_OPEN} for o in outs if o != {"ok": True})
    closers[0]()                                   # the person closes FA's window
    assert main.fa_journal(1, "t", "m", "0") == {"ok": True}


@pytest.mark.parametrize("result", [RuntimeError("no window"), {"ok": False, "message": "This only works in the app."}])
def test_journal_flag_clears_when_open_fails(app, monkeypatch, result):
    _journal_stub(monkeypatch, result=result)
    first = main.fa_journal(1, "t", "m", "0")
    assert first.get("ok") is False
    _journal_stub(monkeypatch)
    assert main.fa_journal(1, "t", "m", "0") == {"ok": True}


def test_http_ok_needs_the_dialog(app, monkeypatch):
    wv, w = app
    saved = []
    monkeypatch.setattr(desktop_agent, "set_http_ok", lambda s, y: saved.append(y) or [])
    w.answer = False
    assert main.helper_http_ok("http://192.168.1.5:8420", True) == {"ok": False}
    assert saved == [] and len(w.dialogs) == 1
    monkeypatch.setattr(desktop_gate, "_last_no", {})
    w.answer = True
    assert main.helper_http_ok("http://192.168.1.5:8420", True) == {"ok": True}
    assert saved == [True]


class _LookupClient:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, **k):
        self.posts.append((url, json))
        return types.SimpleNamespace(status_code=200, json=lambda: {"platform": "ib", "account_label": "Penwright"})

    def close(self):
        pass


def test_signin_refused_while_folder_question_open(app, monkeypatch):
    client = _LookupClient()
    monkeypatch.setattr(desktop_agent, "helper_client", lambda: client)
    opened = []
    monkeypatch.setattr(main, "_login_via_browser", lambda p: opened.append(p) or {"a": "b"})
    assert desktop_gate._ask_lock.acquire(blocking=False)      # a folder question is open on the desktop
    try:
        out = main.helper_signin("https://example.com", "ABC234")
    finally:
        desktop_gate._ask_lock.release()
    assert out == {"ok": False, "message": desktop_gate.MSG_BUSY}
    assert opened == [] and main._helper_sender.queue.added == []


# ── Phase 4 (US2): inputs, picked files, permissions, folders, restore ───────

BAD_SERVERS = [None, 5, ["https://example.com"], "https://" + "a" * 2050 + ".example", "ftp://example.com",
               "javascript:alert(1)", "https://u:p@example.com", "https://u@example.com", "https://exa mple.com",
               "https://ex_ample.com", "https://-bad.example", "https://", "https://example.com:99999",
               "https://<font color=white>x.example"]
BAD_CODES = [None, 234567, ["ABC234"], "ABC23", "ABC2345", "ABC10O", "!!!!!!"]


@pytest.mark.parametrize("server", BAD_SERVERS)
def test_bad_server_refused_before_any_request(app, monkeypatch, server):
    client = _LookupClient()
    monkeypatch.setattr(desktop_agent, "helper_client", lambda: client)
    wv, w = app
    assert main.helper_signin(server, "ABC234") == {"ok": False, "message": desktop_gate.MSG_BAD_SERVER}
    assert main.helper_http_ok(server, True) == {"ok": False}
    assert client.posts == [] and w.dialogs == []


@pytest.mark.parametrize("code", BAD_CODES)
def test_bad_code_refused_before_any_request(app, monkeypatch, code):
    client = _LookupClient()
    monkeypatch.setattr(desktop_agent, "helper_client", lambda: client)
    assert main.helper_signin("https://example.com", code) == {"ok": False, "message": desktop_gate.MSG_BAD_CODE}
    assert client.posts == []


def test_normalised_code_is_sent(app, monkeypatch):
    client = _LookupClient()
    monkeypatch.setattr(desktop_agent, "helper_client", lambda: client)
    wv, w = app
    w.answer = False
    assert main.helper_signin("https://example.com/", "abc-234")["ok"] is False
    assert client.posts == [("https://example.com/api/signin-helper/lookup", {"code": "ABC234"})]


@pytest.mark.parametrize("args", [
    (True, "t", "m", "0"), (0, "t", "m", "0"), (-1, "t", "m", "0"), ("5", "t", "m", "0"), (1.5, "t", "m", "0"),
    (1, 5, "m", "0"), (1, "t", None, "0"), (1, "t", "x" * 65_537, "0"), (1, "t", "m", 0), (1, "t", "m", "3"),
    (1, "t", "m", None), (1, "t" * 1025, "m", "0"),
])
def test_fa_journal_checks_inputs(app, monkeypatch, args):
    opened: list = []
    _journal_stub(monkeypatch, closers=opened)
    assert main.fa_journal(*args) == {"ok": False, "message": desktop_gate.MSG_FAILED}
    assert opened == []
    assert main.fa_journal(None, "", "x" * 65_536, "1") == {"ok": True}


@pytest.mark.parametrize("yes", ["true", 1, None, [True]])
def test_http_ok_checks_inputs(app, monkeypatch, yes):
    saved: list = []
    monkeypatch.setattr(desktop_agent, "set_http_ok", lambda s, y: saved.append(y))
    wv, w = app
    assert main.helper_http_ok("http://192.168.1.5:8420", yes) == {"ok": False}
    assert saved == [] and w.dialogs == []


def test_dialog_hosts_are_cleaned(app, monkeypatch):
    client = _LookupClient()
    monkeypatch.setattr(desktop_agent, "helper_client", lambda: client)
    monkeypatch.setattr(desktop_agent, "set_http_ok", lambda s, y: [])
    wv, w = app
    w.answer = False
    main.helper_signin("https://bücher.example", "ABC234")
    desktop_gate._last_no.clear()                   # past the pause after that "no"
    main.helper_http_ok("http://bücher.example", True)
    assert len(w.dialogs) == 2
    for title, text in w.dialogs:
        assert "xn--bcher-kva.example" in text and "<" not in title + text and "bücher" not in text
    assert "Penwright" in w.dialogs[0][1]


@pytest.mark.parametrize("renderer", ["mshtml", "gtk", "cocoa", "qtwebkit", "qt", None])
def test_actions_refuse_on_other_renderers(app, monkeypatch, renderer):
    wv, w = app
    wv.renderer = renderer
    w.picked = ("C:\\art\\a.png",)
    for f in main.STANDALONE_ACTIONS:
        args = {"fa_journal": (1, "t", "m", "0"), "helper_signin": ("https://example.com", "ABC234"),
                "helper_http_ok": ("https://example.com", True)}.get(f.__name__, ())
        assert f(*args) == main._REFUSALS[f.__name__]
    assert w.dialogs == [] and desktop_gate._picks == {}


# T020: picked files only (FR-SEC-006)

@pytest.fixture
def art_client(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import posting.scheduler as sched
    from routes import artwork_api
    monkeypatch.setattr(sched, "detect_runtime_mode", lambda: "desktop")
    made: list = []

    def create(**k):
        made.append(k)
        return "Sample_Piece"
    monkeypatch.setattr(artwork_api.artwork_reader, "create_artwork", create)
    monkeypatch.setattr(artwork_api.artwork_reader, "load_artwork",
                        lambda n: types.SimpleNamespace(image="a.png", media_kind="image", thumbnail=""))
    app_ = FastAPI()
    app_.include_router(artwork_api.artwork_router)
    pic = tmp_path / "Pic.png"
    pic.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    other = tmp_path / "Other.png"
    other.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 64)
    return TestClient(app_), made, pic, other


def test_create_from_path_takes_only_a_picked_file(art_client, monkeypatch):
    c, made, pic, other = art_client
    unpicked = c.post("/api/artwork/create-from-path", json={"path": str(other)})
    missing = c.post("/api/artwork/create-from-path", json={"path": str(other.parent / "Nope.png")})
    assert unpicked.status_code == missing.status_code == 400
    assert unpicked.json() == missing.json() == {"detail": desktop_gate.MSG_NOT_PICKED}
    assert made == []
    desktop_gate.remember_pick(str(pic))
    assert c.post("/api/artwork/create-from-path", json={"path": str(pic).upper()}).status_code == 400
    r = c.post("/api/artwork/create-from-path", json={"path": str(pic), "thumbnail_path": str(other)})
    assert r.status_code == 200 and r.json()["name"] == "Sample_Piece"
    assert made[0]["thumbnail_bytes"] is None and made[0]["image_bytes"] == pic.read_bytes()
    assert c.post("/api/artwork/create-from-path", json={"path": str(pic)}).status_code == 400   # used up


def test_create_from_path_reads_nothing_before_the_check(art_client, monkeypatch):
    c, made, pic, other = art_client
    touched: list = []
    monkeypatch.setattr(Path, "is_file", lambda self: touched.append(self) or True)
    monkeypatch.setattr(Path, "read_bytes", lambda self: touched.append(self) or b"")
    assert c.post("/api/artwork/create-from-path", json={"path": str(other)}).status_code == 400
    assert touched == []


@pytest.mark.parametrize("path", [5, ["a"], {"p": 1}, None])
def test_create_from_path_odd_path_is_400(art_client, path):
    c, *_ = art_client
    assert c.post("/api/artwork/create-from-path", json={"path": path}).status_code == 400


def test_picks_are_capped_and_ordered():
    for i in range(60):
        desktop_gate.remember_pick(f"C:\\p{i}.png")
    assert len(desktop_gate._picks) == desktop_gate.MAX_PICKS
    assert not desktop_gate.take_pick("C:\\p0.png") and desktop_gate.take_pick("C:\\p59.png")


# T021: permissions (FR-SEC-007), stand-ins for the backends

class _Event:
    def __init__(self):
        self.handlers: list = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self


class _Task:
    def __init__(self, result):
        self.Result = result
        self.continuations: list = []

    def ContinueWith(self, fn, scheduler):
        self.continuations.append((fn, scheduler))


class _Action:
    def __class_getitem__(cls, item):
        return lambda fn: fn


def _fake_edge_module(monkeypatch):
    states = types.SimpleNamespace(Allow="Allow", Deny="Deny", Default="Default")
    core_mod = types.ModuleType("Microsoft.Web.WebView2.Core")
    core_mod.CoreWebView2PermissionState = states
    for name in ("Microsoft", "Microsoft.Web", "Microsoft.Web.WebView2"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "Microsoft.Web.WebView2.Core", core_mod)
    ec = types.ModuleType("webview.platforms.edgechromium")
    log: list = []

    class EdgeChrome:
        def __init__(self, window=None):
            self.pywebview_window = window
            self.syncContextTaskScheduler = "ui"

        def on_webview_ready(self, sender, args):
            log.append("orig_ready")

        def on_script_notify(self, sender, args):
            log.append(("notify", args))
    ec.EdgeChrome, ec.Action, ec.Task, ec.js_bridge_call, ec.log = EdgeChrome, _Action, object, REAL_BRIDGE, log
    monkeypatch.setitem(sys.modules, "webview.platforms.edgechromium", ec)
    return ec


def _fake_core(stored):
    set_calls: list = []
    profile = types.SimpleNamespace(
        GetNonDefaultPermissionSettingsAsync=lambda: profile.task,
        SetPermissionStateAsync=lambda kind, origin, state: set_calls.append((kind, origin, state)),
        task=_Task(stored))
    core = types.SimpleNamespace(PermissionRequested=_Event(), ScreenCaptureStarting=_Event(),
                                 LaunchingExternalUriScheme=_Event(), Profile=profile)
    return core, set_calls


def test_webview2_handler_subscribed_before_navigation(monkeypatch):
    ec = _fake_edge_module(monkeypatch)
    desktop_gate._patch_edge(ec)
    core, _ = _fake_core([])
    edge = ec.EdgeChrome()
    edge.on_webview_ready(types.SimpleNamespace(CoreWebView2=core), types.SimpleNamespace(IsSuccess=True))
    assert len(core.PermissionRequested.handlers) == 1
    assert ec.log == []                           # navigation waits for the reset; nothing blocks
    args = types.SimpleNamespace(SavesInProfile=True, State=None)
    core.PermissionRequested.handlers[0](None, args)
    assert args.SavesInProfile is False and args.State == "Deny"
    fn, scheduler = core.Profile.task.continuations[0]
    assert scheduler == "ui"
    fn(core.Profile.task)
    assert ec.log == ["orig_ready"]


def test_webview2_resets_remembered_grants(monkeypatch):
    ec = _fake_edge_module(monkeypatch)
    desktop_gate._patch_edge(ec)
    stored = [types.SimpleNamespace(PermissionState="Allow", PermissionKind="Camera", PermissionOrigin="o1"),
              types.SimpleNamespace(PermissionState="Deny", PermissionKind="Mic", PermissionOrigin="o2")]
    core, set_calls = _fake_core(stored)
    ec.EdgeChrome().on_webview_ready(types.SimpleNamespace(CoreWebView2=core), types.SimpleNamespace(IsSuccess=True))
    fn, _ = core.Profile.task.continuations[0]
    fn(core.Profile.task)
    assert set_calls == [("Camera", "o1", "Default")]
    assert ec.log == ["orig_ready"]


def test_webview2_navigates_even_if_the_reset_fails(monkeypatch):
    ec = _fake_edge_module(monkeypatch)
    desktop_gate._patch_edge(ec)
    core = types.SimpleNamespace(PermissionRequested=_Event(), Profile=None)
    ec.EdgeChrome().on_webview_ready(types.SimpleNamespace(CoreWebView2=core), types.SimpleNamespace(IsSuccess=True))
    assert ec.log == ["orig_ready"]


def test_webview2_screen_capture_cancelled(monkeypatch):
    ec = _fake_edge_module(monkeypatch)
    desktop_gate._patch_edge(ec)
    core, _ = _fake_core([])
    ec.EdgeChrome().on_webview_ready(types.SimpleNamespace(CoreWebView2=core), types.SimpleNamespace(IsSuccess=True))
    args = types.SimpleNamespace(Cancel=False)
    core.ScreenCaptureStarting.handlers[0](None, args)
    assert args.Cancel is True


def test_webview2_external_schemes_refused(monkeypatch, caplog):
    """SEC074 (a): a page navigating the window to ms-settings:, search-ms: or any app's scheme launches nothing.
    The app's own mail links open a new window, which goes through the guarded webbrowser.open instead."""
    ec = _fake_edge_module(monkeypatch)
    desktop_gate._patch_edge(ec)
    core, _ = _fake_core([])
    ec.EdgeChrome().on_webview_ready(types.SimpleNamespace(CoreWebView2=core), types.SimpleNamespace(IsSuccess=True))
    [handler] = core.LaunchingExternalUriScheme.handlers
    for uri in ("search-ms:query=x", "ms-settings:privacy", "mailto:owner@example.com", "file:///C:/x"):
        args = types.SimpleNamespace(Cancel=False, Uri=uri)
        handler(None, args)
        assert args.Cancel is True, uri
    assert "owner@example.com" not in caplog.text and "search-ms" not in caplog.text
    handler(None, None)                                    # never raises


def _fake_qt_module(monkeypatch):
    qt = types.ModuleType("webview.platforms.qt")
    log: list = []

    class _Signal:
        def __init__(self):
            self.slots: list = []

        def connect(self, fn):
            self.slots.append(fn)

    class WebPage:
        def __init__(self, parent=None, profile=None):
            log.append(("orig_init", profile))
            self.desktopMediaRequested, self.permissionRequested = _Signal(), _Signal()
            self.set: list = []
            self.page_settings = types.SimpleNamespace(scheme_policy=None)
            self.page_settings.setUnknownUrlSchemePolicy = lambda p: setattr(self.page_settings, "scheme_policy", p)

        def settings(self):
            return self.page_settings

        def onFeaturePermissionRequested(self, url, feature):
            self.setFeaturePermission(url, feature, "GRANTED")

        def setFeaturePermission(self, url, feature, policy):
            self.set.append((url, feature, policy))

    class BrowserView:
        pass
    BrowserView.WebPage = WebPage
    qt.BrowserView, qt.log, qt.js_bridge_call = BrowserView, log, REAL_BRIDGE
    qt.QWebPage = types.SimpleNamespace(PermissionPolicy=types.SimpleNamespace(PermissionDeniedByUser="DENIED"))
    qt.QWebEngineSettings = types.SimpleNamespace(
        UnknownUrlSchemePolicy=types.SimpleNamespace(DisallowUnknownUrlSchemes="NO_UNKNOWN_SCHEMES"))
    monkeypatch.setitem(sys.modules, "webview.platforms.qt", qt)
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    return qt


def test_qt_permission_slot_denies_everything(monkeypatch):
    qt = _fake_qt_module(monkeypatch)
    desktop_gate._patch_qt(qt)
    page = qt.BrowserView.WebPage(None)
    for feature in ("MediaAudioCapture", "MediaVideoCapture", "Geolocation", "Notifications", "ClipboardReadWrite"):
        page.onFeaturePermissionRequested("u", feature)
    assert {p for _, _, p in page.set} == {"DENIED"}


def test_qt_new_signals_denied_when_present(monkeypatch):
    qt = _fake_qt_module(monkeypatch)
    desktop_gate._patch_qt(qt)
    page = qt.BrowserView.WebPage(None)
    req, perm = types.SimpleNamespace(cancelled=[]), types.SimpleNamespace(denied=[])
    req.cancel = lambda: req.cancelled.append(1)
    perm.deny = lambda: perm.denied.append(1)
    page.desktopMediaRequested.slots[0](req)
    page.permissionRequested.slots[0](perm)
    assert req.cancelled == [1] and perm.denied == [1]


def test_qt_stored_grants_reset_and_policy_set(monkeypatch):
    qt = _fake_qt_module(monkeypatch)
    desktop_gate._patch_qt(qt)
    resets: list = []

    class Profile:
        PersistentPermissionsPolicy = types.SimpleNamespace(AskEveryTime="ASK")
        policy = None

        def setPersistentPermissionsPolicy(self, p):
            self.policy = p
            qt.log.append("policy")

        def listAllPermissions(self):
            return [types.SimpleNamespace(reset=lambda i=i: resets.append(i)) for i in range(3)]
    prof = Profile()
    qt.BrowserView.WebPage(None, profile=prof)
    assert prof.policy == "ASK" and resets == [0, 1, 2]
    assert qt.log.index("policy") < qt.log.index(("orig_init", prof))


def test_qt_handler_never_raises(monkeypatch):
    qt = _fake_qt_module(monkeypatch)
    desktop_gate._patch_qt(qt)
    page = qt.BrowserView.WebPage(None)

    def boom(*a):
        raise RuntimeError("x")
    page.setFeaturePermission = boom
    page.onFeaturePermissionRequested("u", "Camera")
    perm = types.SimpleNamespace(deny=boom)
    page.permissionRequested.slots[0](perm)
    qt.BrowserView.WebPage(None, profile=types.SimpleNamespace(listAllPermissions=boom))


def test_qt_unknown_schemes_refused(monkeypatch):
    """SEC074 (a): Qt hands an unknown scheme (ssh:, an app's own) to the desktop on a click; the page refuses them."""
    qt = _fake_qt_module(monkeypatch)
    desktop_gate._patch_qt(qt)
    page = qt.BrowserView.WebPage(None)
    assert page.page_settings.scheme_policy == "NO_UNKNOWN_SCHEMES"
    page = qt.BrowserView.WebPage(None, profile=types.SimpleNamespace())
    assert page.page_settings.scheme_policy == "NO_UNKNOWN_SCHEMES"
    monkeypatch.delattr(qt, "QWebEngineSettings")          # an old Qt without the setting: the window still opens
    desktop_gate._patch_qt(qt)
    qt.BrowserView.WebPage(None)


def test_qt_unhandled_slot_errors_are_logged_not_fatal(monkeypatch):
    qt = _fake_qt_module(monkeypatch)
    desktop_gate._patch_qt(qt)
    assert sys.excepthook is desktop_gate._log_unhandled


def test_unhandled_error_log_says_where_not_what(caplog):
    """SEC074 (c): a Qt slot crash names the module and line, so it can be found; the message (which can carry a
    path or a handle) stays out."""
    def fails():
        raise ValueError("C:\\Users\\owner\\secret.png")
    try:
        fails()
    except ValueError:
        kind, value, tb = sys.exc_info()
    line = fails.__code__.co_firstlineno + 1
    with caplog.at_level("ERROR"):
        desktop_gate._log_unhandled(kind, value, tb)
    assert "ValueError" in caplog.text and f"{__name__}:{line}" in caplog.text
    assert "secret" not in caplog.text and "owner" not in caplog.text
    caplog.clear()
    desktop_gate._log_unhandled(KeyError, KeyError("x"), None)      # no traceback: still a line, never raises
    assert "KeyError" in caplog.text


def test_backend_patch_lands_after_initialize(monkeypatch):
    """pywebview imports its backend inside start()'s initialize(), before the first window: the patches land then."""
    wv = fake_webview()
    holder: dict = {}

    def initialize(*a, **k):
        holder["ec"], holder["qt"] = _fake_edge_module(monkeypatch), _fake_qt_module(monkeypatch)
        return types.SimpleNamespace(renderer="edgechromium")
    wv.initialize = initialize
    desktop_gate.harden(wv)
    guard = wv.util.js_bridge_call
    assert holder == {}                            # harden imported no backend itself
    wv.initialize()
    assert getattr(holder["ec"].EdgeChrome, "_pp_patched", False)
    assert getattr(holder["qt"].BrowserView.WebPage, "_pp_patched", False)
    assert holder["ec"].js_bridge_call is guard and holder["qt"].js_bridge_call is guard


def test_permission_patch_targets_exist_in_pywebview():
    ec, qt = _src("platforms/edgechromium.py"), _src("platforms/qt.py")
    assert "class EdgeChrome" in ec and re.search(r"def on_webview_ready\(self,\s*sender,\s*args\)", ec)
    assert "self.syncContextTaskScheduler" in ec
    assert re.search(r"from System import [^\n]*\bAction\b", ec)
    assert re.search(r"from System\.Threading\.Tasks import [^\n]*\bTask\b", ec)
    assert "class BrowserView" in qt and "class WebPage" in qt
    assert re.search(r"def onFeaturePermissionRequested\(self,\s*url,\s*feature\)", qt)
    assert re.search(r"def __init__\(self,\s*parent=None,\s*profile=None\)", qt)
    assert re.search(r"import [^\n]*\bQWebEngineSettings\b", qt)       # SEC074 (a) reads it off the module


# T022: folder settings (FR-SEC-008)

@pytest.fixture
def folders(monkeypatch):
    import config
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes import artwork_api, backup_api, posting_api
    monkeypatch.setattr(desktop_gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(desktop_gate, "_drive_type", lambda root: 3)
    asked: list = []
    answer = {"v": True}
    desktop_gate.set_confirm(lambda t, x: asked.append((t, x)) or answer["v"])
    saved_before = {k: config.get_settings().get(k) for k in
                    ("posting_story_archive_path", "artwork_archive_path", "auto_backup_dir", "posting_default_rating")}
    app_ = FastAPI()
    for r in (artwork_api.artwork_router, posting_api.posting_router, backup_api.backup_router):
        app_.include_router(r)
    yield TestClient(app_), asked, answer
    config.save_settings(saved_before)


ROUTES = [("/api/posting/settings", "posting_story_archive_path", "posting_story_archive_path"),
          ("/api/artwork/settings", "artwork_archive_path", "artwork_archive_path"),
          ("/api/backup/auto", "dir", "auto_backup_dir")]


@pytest.mark.parametrize("route,field,key", ROUTES)
def test_folder_change_needs_the_dialog(folders, route, field, key):
    import config
    c, asked, answer = folders
    answer["v"] = False
    assert c.post(route, json={field: "D:\\Stories\\New"}).status_code == 400
    assert config.get_settings().get(key) != "D:\\Stories\\New" and len(asked) == 1
    desktop_gate._last_no.clear()
    answer["v"] = True
    assert c.post(route, json={field: "D:\\Stories\\New"}).status_code == 200
    assert config.get_settings().get(key) == "D:\\Stories\\New"
    if key == "auto_backup_dir":
        assert "saved passwords" in asked[-1][1]
    assert "D:\\Stories\\New" in asked[-1][1] and "PawPoller" in asked[-1][0]


@pytest.mark.parametrize("route,field,key", ROUTES)
def test_unchanged_or_blank_folder_asks_nothing(folders, route, field, key):
    import config
    c, asked, _ = folders
    config.save_settings({key: "D:\\Keep"})
    assert c.post(route, json={field: "D:\\Keep"}).status_code == 200
    assert c.post(route, json={field: "  D:\\Keep  "}).status_code == 200
    if key != "auto_backup_dir":               # the backup form ignores a blank folder, as it always did
        assert c.post(route, json={field: ""}).status_code == 200
    assert asked == []


@pytest.mark.parametrize("path", [
    "\\\\host\\share", "//host/share", "/\\host\\share", "\\/host/share", "\\\\?\\UNC\\host\\share",
    "\\\\.\\C:\\x", "\\??\\UNC\\host\\share", "\\foo", "C:foo", "relative\\dir", "~/x", " \\\\host\\s",
    "\\\\?\\C:\\x",
])
def test_network_folder_refused(folders, monkeypatch, path):
    c, asked, _ = folders
    # refused on the text alone: the drive lookup (the rule's only system call) never runs for these
    monkeypatch.setattr(desktop_gate, "_drive_type", lambda root: (_ for _ in ()).throw(AssertionError("fs")))
    for route, field, _key in ROUTES:
        r = c.post(route, json={field: path})
        assert r.status_code == 400, (route, path)
    assert asked == []


def test_mapped_network_drive_refused(folders, monkeypatch):
    c, asked, _ = folders
    monkeypatch.setattr(desktop_gate, "_drive_type", lambda root: 4)        # DRIVE_REMOTE
    assert c.post("/api/posting/settings", json={"posting_story_archive_path": "Z:\\Stories"}).status_code == 400
    assert asked == []


def test_linux_folder_rules(folders, monkeypatch):
    import config
    c, asked, _ = folders
    monkeypatch.setattr(desktop_gate, "_IS_WINDOWS", False)
    assert c.post("/api/posting/settings", json={"posting_story_archive_path": "/home/x/stories/"}).status_code == 200
    assert config.get_settings()["posting_story_archive_path"] == "/home/x/stories"
    for bad in ("//host/share", "relative/dir", "C:\\x", "~/x"):
        assert c.post("/api/posting/settings", json={"posting_story_archive_path": bad}).status_code == 400
    assert len(asked) == 1


def test_dialog_shows_what_is_saved(folders):
    import config
    c, asked, _ = folders
    assert c.post("/api/posting/settings",
                  json={"posting_story_archive_path": "D:\\Art\\..\\Art & Sketches\\.\\x"}).status_code == 200
    assert config.get_settings()["posting_story_archive_path"] == "D:\\Art & Sketches\\x"
    assert "D:\\Art & Sketches\\x" in asked[-1][1]
    n = len(asked)
    for bad in ("D:\\" + "a" * 199, "D:\\a\x07b", "D:\\a<b", "D:\\a\u200db", "D:\\a\u2028b"):
        assert c.post("/api/posting/settings", json={"posting_story_archive_path": bad}).status_code == 400
    assert len(asked) == n


@pytest.mark.parametrize("value", [5, ["D:\\x"], {"p": "D:\\x"}, "D:\\" + "a" * 300])
def test_non_string_folder_refused(folders, value):
    c, asked, _ = folders
    assert c.post("/api/posting/settings", json={"posting_story_archive_path": value}).status_code == 400
    assert c.post("/api/artwork/settings", json={"artwork_archive_path": value}).status_code == 400
    assert asked == []


def test_refused_folder_saves_nothing_else(folders):
    import config
    c, asked, answer = folders
    config.save_settings({"posting_default_rating": "general"})
    r = c.post("/api/posting/settings",
               json={"posting_story_archive_path": "\\\\host\\share", "posting_default_rating": "adult"})
    assert r.status_code == 400
    assert config.get_settings()["posting_default_rating"] == "general"
    answer["v"] = False
    r = c.post("/api/backup/auto", json={"dir": "D:\\B", "keep": 3})
    assert r.status_code == 400


def test_headless_server_with_pywebview_unchanged(folders):
    """No standalone window, no confirm: the folder rule is off (a source install running server.py)."""
    import config
    c, asked, _ = folders
    desktop_gate.set_confirm(None)
    assert c.post("/api/posting/settings", json={"posting_story_archive_path": "\\\\host\\share"}).status_code == 200
    assert config.get_settings()["posting_story_archive_path"] == "\\\\host\\share"
    assert asked == []


# T023: a backup restore keeps this computer's folders and plain-http yes

KEPT = ("posting_story_archive_path", "artwork_archive_path", "auto_backup_dir", "signin_helper_http_ok",
        "tw_gallerydl_path")


def _backup_zip(settings: dict, media_mirror: bool = False) -> bytes:
    import io
    import json
    import zipfile

    from routes import backup_api
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"kind": backup_api.BACKUP_KIND,
                                                **({"media_mirror": backup_api.MEDIA_MIRROR} if media_mirror else {})}))
        z.writestr("data/pawpoller.db", "DB")
        z.writestr("data/settings.json", json.dumps(settings))
    return buf.getvalue()


@pytest.fixture
def restore(tmp_path, monkeypatch):
    import config
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes import backup_api
    dd = tmp_path / "data"
    dd.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", dd)

    def no_settings_write(*a, **k):
        raise AssertionError("restore must not go through save_settings (vault re-encrypt, sync push)")
    monkeypatch.setattr(config, "save_settings", no_settings_write)
    if hasattr(config, "delete_settings_keys"):
        monkeypatch.setattr(config, "delete_settings_keys", no_settings_write)
    app_ = FastAPI()
    app_.include_router(backup_api.backup_router)
    return TestClient(app_), dd


def test_backup_import_keeps_folder_settings(restore):
    import json
    c, dd = restore
    here = {"posting_story_archive_path": "D:\\Mine", "artwork_archive_path": "D:\\Art",
            "auto_backup_dir": "D:\\Backups", "signin_helper_http_ok": ["http://192.168.1.5:8420"],
            "tw_gallerydl_path": "C:\\Tools\\gallery-dl.exe", "x": 1}
    (dd / "settings.json").write_text(json.dumps(here), encoding="utf-8")
    zipped = {"posting_story_archive_path": "\\\\evil\\share", "artwork_archive_path": "\\\\evil\\art",
              "auto_backup_dir": "\\\\evil\\b", "signin_helper_http_ok": ["http://203.0.113.9"],
              "tw_gallerydl_path": "data\\inbox\\x.exe", "x": 2}
    r = c.post("/api/backup/import", files={"file": ("b.zip", _backup_zip(zipped), "application/zip")})
    assert r.status_code == 200
    after = json.loads((dd / "settings.json").read_text(encoding="utf-8"))
    assert {k: after.get(k) for k in KEPT} == {k: here[k] for k in KEPT}
    assert after["x"] == 2                                         # everything else is the backup's
    import config
    assert "tw_gallerydl_path" in config.SYNC_EXCLUDE               # a program path never crosses boxes either


def test_backup_import_drops_keys_this_computer_lacks(restore):
    import json
    c, dd = restore
    (dd / "settings.json").write_text(json.dumps({"x": 1}), encoding="utf-8")
    zipped = {"posting_story_archive_path": "\\\\evil\\share", "auto_backup_dir": "\\\\evil\\b",
              "signin_helper_http_ok": ["http://203.0.113.9"], "artwork_archive_path": "\\\\evil\\a"}
    assert c.post("/api/backup/import", files={"file": ("b.zip", _backup_zip(zipped), "application/zip")}).status_code == 200
    after = json.loads((dd / "settings.json").read_text(encoding="utf-8"))
    assert not any(k in after for k in KEPT)


def test_backup_import_media_folder_is_this_computers(restore, monkeypatch):
    import json

    from routes import backup_api
    c, dd = restore
    (dd / "settings.json").write_text(json.dumps({"auto_backup_dir": "D:\\Backups"}), encoding="utf-8")
    seen: list = []

    def media_dir():
        seen.append(json.loads((dd / "settings.json").read_text(encoding="utf-8")).get("auto_backup_dir"))
        return Path("D:\\Backups")
    monkeypatch.setattr(backup_api, "_auto_backup_dir", media_dir)
    zipped = {"auto_backup_dir": "\\\\evil\\share"}
    r = c.post("/api/backup/import", files={"file": ("b.zip", _backup_zip(zipped, media_mirror=True), "application/zip")})
    assert r.status_code == 200
    assert seen == ["D:\\Backups"]                                 # read before the zip's settings landed


# ── Phase 5 (US3): only the app's own page ───────────────────────────────────

EVIL = "http://evil.example/"
ACTIONS = ("open_image_dialog", "fa_journal", "helper_signin", "helper_http_ok", "helper_status")


def _effects(app, monkeypatch):
    """Every effect an action can have, recorded: picker, journal window, lookup, login window, saved yes."""
    from auth import fa_journal_window
    wv, w = app
    seen: list = []
    client = _LookupClient()
    monkeypatch.setattr(w, "create_file_dialog", lambda *a, **k: seen.append("picker") or ("C:\\art\\a.png",))
    monkeypatch.setattr(fa_journal_window, "open_fa_journal", lambda *a, **k: seen.append("journal") or {"ok": True})
    monkeypatch.setattr(desktop_agent, "helper_client", lambda: client)
    monkeypatch.setattr(main, "_login_via_browser", lambda p: seen.append("login") or {"a": "b"})
    monkeypatch.setattr(desktop_agent, "set_http_ok", lambda s, y: seen.append("http_ok") or [])
    return w, seen, client


def _run_all():
    return [main.open_image_dialog(), main.fa_journal(1, "t", "m", "0"),
            main.helper_signin("https://example.com", "ABC234"), main.helper_http_ok("http://192.168.1.5:8420", True),
            main.helper_status()]


@pytest.mark.parametrize("url", BAD_URLS + [RuntimeError("no url")])
def test_actions_refuse_off_their_own_page(app, monkeypatch, url):
    w, seen, client = _effects(app, monkeypatch)
    w.url = url
    assert _run_all() == [main._REFUSALS[n] for n in ACTIONS]
    assert seen == [] and w.dialogs == [] and client.posts == []
    assert desktop_gate._picks == {} and main._helper_sender.queue.added == []


def test_page_changed_before_effect_refuses(app, monkeypatch):
    w, seen, client = _effects(app, monkeypatch)

    def leave(then):
        def f(*a, **k):
            w.url = EVIL                             # the page navigates while the picker / dialog is open
            return then
        return f

    # picker returns after the page left: nothing remembered
    monkeypatch.setattr(w, "create_file_dialog", leave(("C:\\art\\a.png",)))
    assert main.open_image_dialog() == []
    assert desktop_gate._picks == {}

    # the yes to plain http comes back after the page left: nothing saved
    w.url = START
    monkeypatch.setattr(w, "create_confirmation_dialog", leave(True))
    assert main.helper_http_ok("http://192.168.1.5:8420", True) == {"ok": False}

    # the sign-in yes comes back after the page left: no login window, nothing queued
    w.url = START
    assert main.helper_signin("https://example.com", "ABC234").get("ok") is False
    assert len(client.posts) == 1                    # the lookup ran (before the question), nothing after it

    # the journal: the page passes the entry check, then has left by the time the window would open
    urls = iter([START, EVIL])
    monkeypatch.setattr(w, "get_current_url", lambda: next(urls, EVIL))
    assert main.fa_journal(1, "t", "m", "0") == main._REFUSALS["fa_journal"]
    assert main._journal_open is False               # the refusal didn't leave the one-window flag set

    assert "http_ok" not in seen and "login" not in seen and "journal" not in seen
    assert main._helper_sender.queue.added == []


def test_queue_rechecks_the_page(app):
    wv, w = app
    q = main._CheckedQueue(main._helper_sender.queue)
    w.url = EVIL
    with pytest.raises(main._Refused):
        q.add("signin", {"x": 1})
    assert main._helper_sender.queue.added == []


def _edge_window(monkeypatch, marked):
    ec = _fake_edge_module(monkeypatch)
    desktop_gate._patch_edge(ec)
    w = FakeWindow(url=None if not marked else START)
    if marked:
        desktop_gate.mark_standalone(w, START)
    return ec, ec.EdgeChrome(w)


@pytest.mark.parametrize("source", [EVIL, "about:blank", "http://127.0.0.1:84201/", None, RuntimeError])
def test_webview2_source_must_be_own_origin(monkeypatch, source):
    ec, edge = _edge_window(monkeypatch, marked=True)

    class Args:
        @property
        def Source(self):
            if source is RuntimeError:
                raise RuntimeError("gone")
            return source
    edge.on_script_notify(None, Args())
    assert ec.log == []
    ok = types.SimpleNamespace(Source=START + "artwork")
    edge.on_script_notify(None, ok)
    assert ec.log == [("notify", ok)]


def _msg(name, source="https://www.furaffinity.net/controls/journal/"):
    import json as _json
    body = _json.dumps([name, _json.dumps("hi"), "1"])
    return types.SimpleNamespace(Source=source, get_WebMessageAsJson=lambda: body)


def test_site_page_alert_dropped(monkeypatch):
    """SEC4810 (1): FA's journal page or a sign-in page can't open a native Windows message box."""
    ec, edge = _edge_window(monkeypatch, marked=False)
    desktop_gate.mark_site_page(edge.pywebview_window)
    edge.on_script_notify(None, _msg("_pywebviewAlert"))
    assert ec.log == []
    other = _msg("console")
    edge.on_script_notify(None, other)                      # anything else goes on as before
    assert ec.log == [("notify", other)]
    odd = types.SimpleNamespace(Source="x", get_WebMessageAsJson=lambda: "not json")
    edge.on_script_notify(None, odd)
    assert ec.log[-1] == ("notify", odd)


def test_dashboard_alerts_still_shown(monkeypatch):
    """Connected mode's window shows a server's dashboard, which uses alert(): not a site page, so it passes."""
    ec, edge = _edge_window(monkeypatch, marked=False)
    alert = _msg("_pywebviewAlert", source="https://paw.example.social/")
    edge.on_script_notify(None, alert)
    assert ec.log == [("notify", alert)]


def test_site_windows_are_marked():
    for path, call in (("auth/fa_journal_window.py", "desktop_gate.mark_site_page(win)"),
                       ("auth/browser_login.py", "desktop_gate.mark_site_page(window)")):
        assert call in open(path, encoding="utf-8").read()


def test_connected_windows_still_answer(monkeypatch):
    """Connected mode's windows are opened with html= and never marked: their Source isn't the dashboard."""
    ec, edge = _edge_window(monkeypatch, marked=False)
    args = types.SimpleNamespace(Source="about:blank")
    edge.on_script_notify(None, args)
    assert ec.log == [("notify", args)]
    _, guard = _guarded()
    w = FakeWindow(url=None)
    w.expose(_ok)
    assert '"ok": true' in call(w, "_ok", bridge=guard)


@pytest.mark.parametrize("renderer", ["edgechromium", "qtwebengine"])
def test_actions_answer_on_both_patched_renderers(app, renderer):
    """pywebview names Qt's renderer "qtwebengine" (platforms/qt.py), not "qt": Linux is answered too."""
    assert "renderer = 'qtwebengine'" in (WV_DIR / "platforms" / "qt.py").read_text(encoding="utf-8")
    wv, w = app
    wv.renderer = renderer
    assert main.helper_status() == {"pending": 2, "message": "m"}


# ── gap check (handback §7) ──────────────────────────────────────────────────

def test_failed_save_keeps_the_pick(art_client):
    """A save refused after the pick check (bad metadata here) leaves the pick, so Save can be pressed again."""
    c, made, pic, other = art_client
    picked = str(pic)
    desktop_gate.remember_pick(picked)
    bad = c.post("/api/artwork/create-from-path", json={"path": picked, "metadata": "not an object"})
    assert bad.status_code == 400 and bad.json()["detail"] != desktop_gate.MSG_NOT_PICKED
    ok = c.post("/api/artwork/create-from-path", json={"path": picked, "metadata": {}})
    assert ok.status_code == 200, ok.text
    again = c.post("/api/artwork/create-from-path", json={"path": picked, "metadata": {}})
    assert again.json()["detail"] == desktop_gate.MSG_NOT_PICKED       # used once it worked


def test_journal_posted_is_recorded_and_closing_frees_the_button(app, monkeypatch):
    """Through the real open_fa_journal on a stand-in pywebview: FA's posted journal is recorded against the post,
    and closing the window lets the button open a new one."""
    from posting import journals
    recorded = []
    monkeypatch.setattr(journals, "record_manual", lambda *a: recorded.append(a) or True)
    wins = []

    def create_window(*a, **k):
        w = types.SimpleNamespace(events=types.SimpleNamespace(loaded=_Event(), closed=_Event()))
        wins.append(w)
        return w
    monkeypatch.setitem(sys.modules, "webview", types.SimpleNamespace(windows=[object()], create_window=create_window))
    from auth import fa_journal_window
    posted = []
    real = fa_journal_window.open_fa_journal
    monkeypatch.setattr(fa_journal_window, "open_fa_journal",
                        lambda t, m, r, on_posted=None, on_closed=None:
                        posted.append(on_posted) or real(t, m, r, on_posted, on_closed=on_closed))
    assert main.fa_journal(7, "T", "M", "0") == {"ok": True}
    assert main.fa_journal(7, "T", "M", "0") == {"ok": False, "message": desktop_gate.MSG_JOURNAL_OPEN}
    posted[0](123, "https://www.furaffinity.net/journal/123/")
    assert recorded == [(7, "fa", 123, "https://www.furaffinity.net/journal/123/")]
    for h in wins[0].events.closed.handlers:
        h()
    assert main.fa_journal(7, "T", "M", "0") == {"ok": True}
