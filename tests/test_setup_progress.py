"""Setup progress lives on the server (056, ONBOARDEXIT).

A new tab, a reload or a browser sign-in window all see the same step, the sign-in the
wizard is waiting on, and the choices left for later.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from routes import settings_api


@pytest.fixture
def client(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(config, "get_settings", lambda: dict(store))
    monkeypatch.setattr(config, "save_settings", lambda upd: store.update(upd))
    app = FastAPI()
    app.include_router(settings_api.settings_router)
    c = TestClient(app)
    c.store = store
    return c


def _status(c):
    return c.get("/api/settings/setup-status").json()


def test_progress_round_trip(client):
    assert _status(client)["progress"] == {"skipped": [], "return_to": None}
    r = client.post("/api/settings/setup-progress", json={"step": "platforms", "path": "server"})
    assert r.status_code == 200
    client.post("/api/settings/setup-progress", json={"return_to": {"step": "platforms", "site": "fa"}})
    p = _status(client)["progress"]
    assert p["step"] == "platforms" and p["path"] == "server"
    assert p["return_to"] == {"step": "platforms", "site": "fa"}


@pytest.mark.parametrize("body", [
    {"step": "Platforms!"}, {"step": "x" * 33}, {"path": "elsewhere"},
    {"skipped": ["platforms"] * 21}, {"skipped": ["platforms", "Not OK"]},
    {"step": "constructor"}, {"step": "__proto__"}, {"skipped": ["toString"]},
    {"return_to": {"step": "constructor", "site": "fa"}},
    {"return_to": {"step": "platforms", "site": "../fa"}}, {"return_to": "fa"},
])
def test_bad_input_is_refused(client, body):
    assert client.post("/api/settings/setup-progress", json=body).status_code == 400


def test_unknown_keys_dropped(client):
    r = client.post("/api/settings/setup-progress", json={"step": "age", "evil": 1})
    assert "evil" not in r.json()


def test_finish_later_keeps_skipped(client):
    client.post("/api/settings/setup-progress", json={"step": "interval", "return_to": {"step": "platforms", "site": "fa"}})
    r = client.post("/api/settings/setup-complete", json={"skipped": ["interval", "fun", "age"]})
    assert r.status_code == 200
    s = _status(client)
    assert s["setup_complete"] is True
    assert s["progress"]["skipped"] == ["interval", "fun"]      # age is never skipped
    assert s["progress"]["return_to"] is None


def test_complete_without_body_skips_nothing(client):
    assert client.post("/api/settings/setup-complete").status_code == 200
    assert _status(client)["progress"]["skipped"] == []


def test_reset_starts_over(client):
    client.post("/api/settings/setup-complete", json={"skipped": ["interval"]})
    client.post("/api/settings/setup-reset")
    s = _status(client)
    assert s["setup_complete"] is False
    assert s["progress"]["step"] == "welcome" and s["progress"]["skipped"] == []


def test_datamap_classifies_it():
    import datamap
    src = open(datamap.__file__, encoding="utf-8").read()
    assert '"setup_progress"' in src


# ── Frontend (056 T013, T025): the allow-list and the guide buttons ─────────────────────────────
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

APP_JS = open("frontend/js/app.js", encoding="utf-8").read()
GUIDES_JS = open("frontend/js/platform_guides.js", encoding="utf-8").read()
ACCOUNTS_JS = open("frontend/js/accounts.js", encoding="utf-8").read()


def _between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def _node(src: str):
    out = subprocess.run(["node"], input=src, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_gate_holds_the_right_pages():
    """While setup is open, setup's own pages stay reachable and the rest wait (FR-003)."""
    app = "const App = {" + _between(APP_JS, "    SETUP_ALLOWED:", "    // Plain names for the strip") \
        + _between(APP_JS, "    _setupAllowed(parts) {", "    async _refreshSetupStatus()") + "};"
    cases = {"library": False, "start": False, "posts": False, "": False, "settings/general": False,
             "accounts": True, "settings/platforms/fa": True, "help": True, "getting-started": True,
             "setup": True}
    got = _node(app + "console.log(JSON.stringify(%s.map(h => App._setupAllowed(h.split('/').filter(Boolean)))));"
                % json.dumps(list(cases)))
    assert dict(zip(cases, got)) == cases


def test_the_gate_runs_before_any_page_draws():
    route = _between(APP_JS, "    route() {", "const isFullScreen")
    assert "if (this._setupGate(parts)) return;" in route


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_every_site_with_a_guide_gets_a_button_on_its_settings_page():
    """SC-005: every guide code renders a trigger at the top of its page; a code without one renders none."""
    harness = r"""
    const made = [];
    class El { constructor(code){ this.dataset={platform:code}; this.kids=[]; }
      querySelector(sel){ if (sel==='.guide-trigger-top') return this.kids.find(k=>k.className.includes('guide-trigger-top'))||null;
        if (sel===':scope > summary') return { insertAdjacentElement: (_, t) => this.kids.push(t) }; return null; }
      prepend(t){ this.kids.push(t); } }
    const details = CODES.map(c => new El(c));
    global.window = {}; global.document = {
      addEventListener(){}, createElement(){ return { dataset:{}, className:'' }; },
      querySelector(){ return { querySelectorAll: (sel) => sel.startsWith('details') ? details : [] }; } };
    """
    src = harness + GUIDES_JS + """
    window.Guides.injectSettingsButtons(); window.Guides.injectSettingsButtons();
    console.log(JSON.stringify({codes: window.Guides.codes(),
      got: details.map(d => [d.dataset.platform, d.kids.map(k => k.dataset.guide)])}));"""
    probe = _node("global.window={};global.document={addEventListener(){}};" + GUIDES_JS
                  + "console.log(JSON.stringify(window.Guides.codes()))")
    codes = probe + ["nosuchsite"]
    res = _node(src.replace("CODES", json.dumps(codes)))
    for code, triggers in res["got"]:
        assert triggers == ([code] if code in res["codes"] else []), (code, triggers)


def test_the_wizard_card_and_add_account_offer_the_guide():
    card = _between(APP_JS, "const platformCards = chosen.map", "}).join('');")
    assert 'data-guide="${p.key}"' in card and "Guides.has(p.key)" in card
    fields = _between(ACCOUNTS_JS, "    _renderCredFields(", "    _prettyField(")
    assert "Guides.has(platform)" in fields and "data-guide=" in fields


def test_operator_tools_are_off_unless_the_host_turns_them_on(client):
    """The Logs tab's Re-run setup (servers have no other) is the operator's testing tool, not a user feature."""
    assert _status(client)["operator_tools"] is False
    client.store["operator_tools"] = True
    assert _status(client)["operator_tools"] is True


def test_no_route_can_turn_operator_tools_on():
    import pathlib
    hits = [str(p) for p in pathlib.Path("routes").rglob("*.py") if "operator_tools" in p.read_text(encoding="utf-8")]
    assert hits == [str(pathlib.Path("routes/settings_api.py"))]
    src = pathlib.Path("routes/settings_api.py").read_text(encoding="utf-8")
    assert src.count("operator_tools") == 2 and 'bool(settings.get("operator_tools"))' in src   # comment + one read


def test_setup_state_never_rides_a_sync():
    """Sync push would store them without _clean_progress; operator_tools stays on the box it was set on."""
    assert {"setup_progress", "operator_tools"} <= config.SYNC_EXCLUDE
