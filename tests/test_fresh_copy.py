"""SWSTUCK (4.81.1): a device stuck on an old copy of the app refreshes itself, and /app-reset is the way out.

pwa.js runs in node against a fake browser: what matters is when it clears the saved copy and reloads,
and when it must not (same version, typing, a real outage, a second try for the same version).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
PWA = ROOT / "frontend" / "js" / "pwa.js"


@pytest.fixture
def client():
    import dashboard
    return TestClient(dashboard.app, client=("127.0.0.1", 50000))


def test_the_page_is_always_checked_with_the_server(client):
    assert client.get("/").headers["cache-control"] == "no-cache"


def test_app_reset_clears_the_device_copy_but_not_the_sign_in(client):
    import dashboard
    assert "/app-reset" in dashboard._AUTH_EXEMPT_PATHS
    # As the error popup's link arrives (same-origin). Since 4.85.1 a link from elsewhere, or a bare
    # request with no referrer from here, gets the confirm page instead (spec 054, REVIEW4811).
    r = client.get("/app-reset", headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200 and "Open PawPoller" in r.text
    assert r.headers["clear-site-data"] == '"cache", "storage"'      # never "cookies": stay signed in
    assert "clear-site-data" not in client.get("/app-reset").headers


def test_server_unreachable_offers_the_reset():
    src = (ROOT / "frontend" / "js" / "error_popup.js").read_text(encoding="utf-8")
    assert 'href="/app-reset"' in src and "r.title === 'Server unreachable'" in src
    api = (ROOT / "frontend" / "js" / "api.js").read_text(encoding="utf-8")
    assert api.count("window.PawFresh.networkError()") == 2           # GET and POST network failures


HARNESS = r"""
const fs = require('fs');
const sc = JSON.parse(process.argv[1]);
const log = [];
const listeners = {};
const store = Object.assign({}, sc.session || {});
globalThis.window = globalThis;
window.addEventListener = (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); };
globalThis.document = {
  currentScript: { src: 'https://seat.example/js/pwa.js?v=' + sc.page },
  activeElement: sc.typing ? { tagName: 'TEXTAREA' } : { tagName: 'BODY' },
  visibilityState: 'visible',
  addEventListener: (ev, fn) => { (listeners['doc:' + ev] = listeners['doc:' + ev] || []).push(fn); },
  querySelector: () => null, documentElement: {},
};
Object.defineProperty(globalThis, 'navigator', { value: { serviceWorker: {
  register: () => Promise.resolve(),
  getRegistrations: () => Promise.resolve([{ unregister: () => { log.push('unregister'); return Promise.resolve(true); } }]),
} }, configurable: true });
globalThis.caches = { keys: () => Promise.resolve(['pawpoller-shell-old']),
                      delete: (k) => { log.push('delete ' + k); return Promise.resolve(true); } };
globalThis.sessionStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
globalThis.location = { reload: () => log.push('reload') };
globalThis.fetch = (url) => { log.push('health'); return sc.server
  ? Promise.resolve({ ok: true, json: () => Promise.resolve({ status: 'ok', version: sc.server }) })
  : Promise.reject(new TypeError('Failed to fetch')); };
eval(fs.readFileSync(process.argv[2], 'utf8'));
if (sc.load) (listeners.load || []).forEach(f => f());
for (let i = 0; i < (sc.errors || 0); i++) window.PawFresh.networkError();
setTimeout(() => console.log(JSON.stringify({ log, store })), 50);
"""


def run(**sc):
    r = subprocess.run(["node", "-e", HARNESS, json.dumps(sc), str(PWA)],
                       capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")


@needs_node
def test_a_page_older_than_the_server_clears_its_copy_and_reloads_once():
    out = run(page="4.80.1", server="4.81.1", load=True)
    assert out["log"] == ["health", "unregister", "delete pawpoller-shell-old", "reload"]
    assert out["store"]["pp-fresh-for"] == "4.81.1"
    again = run(page="4.80.1", server="4.81.1", load=True, session=out["store"])
    assert "reload" not in again["log"]                              # a stale edge copy can't loop it


@needs_node
def test_a_current_page_is_left_alone():
    assert run(page="4.81.1", server="4.81.1", load=True)["log"] == ["health"]


@needs_node
def test_it_waits_while_someone_is_typing():
    assert run(page="4.80.1", server="4.81.1", load=True, typing=True)["log"] == []


@needs_node
def test_stuck_requests_refresh_only_when_the_server_answers():
    out = run(page="4.81.1", server="4.81.1", errors=3)
    assert out["log"][-1] == "reload" and "pp-healed-at" in out["store"]
    assert "reload" not in run(page="4.81.1", server="4.81.1", errors=2)["log"]     # two blips aren't stuck
    assert "reload" not in run(page="4.81.1", server=None, errors=3)["log"]         # a real outage
    assert "reload" not in run(page="4.81.1", server="4.81.1", errors=3, session=out["store"])["log"]
