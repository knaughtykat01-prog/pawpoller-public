"""Every API route, called with generated input, must never answer 500 (APIFUZZ).

Built from the app's own OpenAPI schema with Schemathesis, so a new route is covered the day
it is added. Runs in-process against a throwaway data folder, as the machine itself
(loopback, so the sign-up gate lets it in), with every network connection beyond this
machine refused and nothing launched, opened, exited or written to the registry.

- Read routes (GET): always, a few generated requests each.
- Write routes (POST/PUT/DELETE/PATCH): only with PAWPOLLER_FUZZ_WRITES=1, and best run from a
  throwaway git worktree; a 27 Oct run of all 438 changed no tracked file, but they create,
  rename and delete inside the data folder by design.
"""
import copy
import os
import socket
import subprocess
import tempfile
import threading
import webbrowser
from pathlib import Path

import pytest

pytest.importorskip("hypothesis")
schemathesis = pytest.importorskip("schemathesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402

import config  # noqa: E402

SKIP = ("/stream", "/auth/callback", "/api/testing/")      # endless streams; OAuth hand-backs that call out
WRITES = bool(os.environ.get("PAWPOLLER_FUZZ_WRITES"))


def _ops():
    from dashboard import app
    schema = schemathesis.openapi.from_dict(copy.deepcopy(app.openapi()))
    out = []
    for op in schema.get_all_operations():
        op = op.ok()
        m = op.method.upper()
        if any(s in op.path for s in SKIP) or (m != "GET" and not WRITES):
            continue
        out.append(pytest.param(op, id=f"{m} {op.path}"))
    return out


def _local(host) -> bool:
    host = host.decode() if isinstance(host, bytes) else str(host)
    return host in ("127.0.0.1", "::1", "localhost", "testserver") or host.startswith("127.")


@pytest.fixture(scope="module")
def client():
    """The app on a throwaway data folder, cut off from the network and the desktop."""
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    tmp = Path(tempfile.mkdtemp(prefix="pp_fuzz_"))
    saved_cfg = {k: getattr(config, k) for k in ("DATA_DIR", "LOGS_DIR", "DB_PATH", "SETTINGS_PATH", "VAULT_PATH",
                                                   "save_settings")}
    pins = {"posting_story_archive_path": str(tmp / "stories"), "artwork_archive_path": str(tmp / "artwork"),
            "auto_backup_dir": str(tmp / "backups"), "tw_gallerydl_path": ""}
    for d in ("data", "logs", "stories", "artwork", "backups"):
        (tmp / d).mkdir()
    config.DATA_DIR, config.LOGS_DIR = tmp / "data", tmp / "logs"
    config.DB_PATH = tmp / "data" / "pawpoller.db"
    config.SETTINGS_PATH = tmp / "data" / "settings.json"
    config.VAULT_PATH = tmp / "data" / "settings.vault.json"
    os.environ.setdefault("PAWPOLLER_VAULT_KEY", Fernet.generate_key().decode())
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    real_save = saved_cfg["save_settings"]
    real_save(pins)
    config.save_settings = lambda data: real_save({**data, **{k: v for k, v in pins.items() if k in data}})
    from database.db import init_db
    init_db()

    def refuse(what):
        def f(*a, **k):
            raise OSError(f"fuzz: {what} blocked")
        return f

    real_connect, real_gai = socket.socket.connect, socket.getaddrinfo
    patched = [(socket.socket, "connect", real_connect), (socket, "getaddrinfo", real_gai),
               (subprocess.Popen, "__init__", subprocess.Popen.__init__), (webbrowser, "open", webbrowser.open),
               (os, "system", os.system), (os, "_exit", os._exit)]
    if hasattr(os, "startfile"):
        patched.append((os, "startfile", os.startfile))
    try:
        import winreg
        patched += [(winreg, "SetValueEx", winreg.SetValueEx), (winreg, "DeleteValue", winreg.DeleteValue)]
    except ImportError:
        winreg = None

    def connect(sock, addr):
        if isinstance(addr, tuple) and not _local(addr[0]):
            raise ConnectionRefusedError(f"fuzz: network blocked ({addr[0]})")
        return real_connect(sock, addr)

    def gai(host, *a, **k):
        if not _local(host):
            raise socket.gaierror(f"fuzz: DNS blocked ({host})")
        return real_gai(host, *a, **k)

    socket.socket.connect, socket.getaddrinfo = connect, gai
    subprocess.Popen.__init__ = refuse("Popen")
    webbrowser.open, os.system, os._exit = refuse("browser"), refuse("system"), refuse("_exit")
    if hasattr(os, "startfile"):
        os.startfile = refuse("startfile")
    if winreg:
        winreg.SetValueEx, winreg.DeleteValue = refuse("registry"), refuse("registry")
    # Rate-limit counters live in module globals: wrong passwords from 127.0.0.1 here must not
    # leave a later test on the same worker rate-limited.
    import dashboard
    from routes import dashboard_auth, ig_api, signin_helper_api, trello_hooks
    counters = [dashboard._auth_failures, dashboard._global_failures, dashboard_auth._RESET_ASKS,
                signin_helper_api._fails, signin_helper_api._addr_hits, ig_api._RELAY_HITS, trello_hooks._hits]
    before = [copy.deepcopy(c) for c in counters]
    try:
        yield TestClient(dashboard.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))
    finally:
        for c, b in zip(counters, before):
            c.clear()
            c.update(b) if isinstance(c, dict) else c.extend(b)
        for obj, name, val in patched:
            setattr(obj, name, val)
        for k, v in saved_cfg.items():
            setattr(config, k, v)


def _send(client, case):
    kw = case.as_transport_kwargs(base_url="http://testserver")
    headers = {k: v for k, v in (kw.get("headers") or {}).items()
               if isinstance(v, str) and k.lower() not in ("host", "content-length")}
    out = {}

    def go():
        try:
            out["r"] = client.request(kw["method"], kw["url"], params=kw.get("params"), json=kw.get("json"),
                                      headers=headers, **({"cookies": kw["cookies"]} if kw.get("cookies") else {}),
                                      content=kw.get("data") if isinstance(kw.get("data"), (bytes, str)) else None)
        except Exception as e:  # noqa: BLE001
            out["e"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(30)
    assert not t.is_alive(), f"{kw['method']} {kw['url']} hung for 30 s"
    if "e" in out:
        raise out["e"]
    return out["r"], kw


@pytest.mark.parametrize("op", _ops())
def test_route_never_answers_500(client, op):
    @settings(max_examples=3 if op.method.upper() == "GET" else 5,
              suppress_health_check=list(HealthCheck))
    @given(case=op.as_strategy())
    def run(case):
        r, kw = _send(client, case)
        assert r.status_code < 500, f"{kw['method']} {kw['url']} {kw.get('params')} → {r.status_code} {r.text[:200]}"
    run()


@pytest.mark.parametrize("url", ["/api/summary?account_id=1180591620717411303424",
                                 "/api/submissions/1180591620717411303424",
                                 "/api/posting/log?limit=1180591620717411303424"])
def test_huge_numbers_are_a_bad_request(client, url):
    """APIBIGINT: past 64 bits SQLite refuses the number; that is the request's fault, a 400."""
    assert client.get(url).status_code == 400


def test_a_nul_byte_in_a_story_name_is_a_bad_request(client):
    """APINULBYTE."""
    assert client.get("/api/posting/archive", params={"story": "a\x00b"}).status_code == 400
