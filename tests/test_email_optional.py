"""Email is optional when you host PawPoller yourself (spec 058, SELFHOSTMAIL).

Hosted seats keep today's rules; everything else may have no email, and then sends
nothing to the mail service. Without an email the way back is the console reset, so its
guards are pinned here too: it opens its own console on the Windows desktop build,
ends every sign-in, and refuses while PawPoller is open.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config

ROOT = Path(__file__).resolve().parents[1]
_KEYS = ["auth_username", "auth_password_hash", "auth_email", "auth_email_pending", "dashboard_password",
         "auth_code_confirm", "auth_code_reset", "legal_accepted", "legal_history"]
_PW = "correct horse"
_GOOD = {"username": "SecondFur", "email": "", "password": _PW, "confirm": _PW}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    import account_mail
    import dashboard
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("PAWPOLLER_HOSTED", raising=False)
    saved = {k: v for k, v in config.get_settings().items() if k in _KEYS}
    config.delete_settings_keys(_KEYS)
    config.invalidate_auth_required_cache()
    dashboard._auth_failures.clear()
    dashboard._global_failures.clear()
    mail = []
    monkeypatch.setattr(account_mail, "_post", lambda to, purpose, code="": mail.append((purpose, to)) or True)
    monkeypatch.setattr(account_mail, "ready", lambda: True)
    monkeypatch.setattr(account_mail, "notice", lambda purpose, to: mail.append((purpose, to)) if to else None)
    yield mail
    config.delete_settings_keys(_KEYS)
    if saved:
        config.save_settings(saved)
    config.invalidate_auth_required_cache()
    dashboard._auth_failures.clear()
    dashboard._global_failures.clear()


def _client(ip="127.0.0.1"):
    import dashboard
    return TestClient(dashboard.app, client=(ip, 50000))


def _signed_up(email="", ip="127.0.0.1"):
    c = _client(ip)
    r = c.post("/api/auth/dashboard-setup", json={**_GOOD, "email": email})
    assert r.status_code == 200, r.text
    return c


# ── US1: sign-up ─────────────────────────────────────────────────────────────

def test_self_hosted_signup_without_email_sends_nothing(_fresh):
    c = _signed_up()
    s = config.get_settings()
    assert s.get("auth_password_hash") and not s.get("auth_email_pending") and not s.get("auth_email")
    assert _fresh == []                                      # SC-003: nothing to the mail service
    st = c.get("/api/auth/signup-status").json()
    assert st["email"] == "none" and st["email_required"] is False


def test_hosted_signup_still_needs_an_email(monkeypatch):
    monkeypatch.setenv("PAWPOLLER_HOSTED", "1")
    r = _client().post("/api/auth/dashboard-setup", json=_GOOD)
    assert r.status_code == 400 and r.json()["detail"]["field"] == "email"
    assert _client().get("/api/auth/signup-status").json()["email_required"] is True


def test_a_typed_but_wrong_email_is_still_refused():
    # Optional means blank is fine, not that a typo silently becomes "no email".
    r = _client().post("/api/auth/dashboard-setup", json={**_GOOD, "email": "not-an-email"})
    assert r.status_code == 400 and r.json()["detail"]["field"] == "email"


def test_an_email_given_works_as_before(_fresh):
    _signed_up("Owner@Example.com")
    assert config.get_settings()["auth_email_pending"] == "owner@example.com"
    assert ("confirm", "owner@example.com") in _fresh


# ── US2: the way back ────────────────────────────────────────────────────────

def test_status_says_how_to_reset(monkeypatch):
    import routes.dashboard_auth as da
    _signed_up()
    st = _client().get("/api/auth/dashboard-status").json()
    assert st["reset_email"] is False and st["reset_available"] is False
    for env, kind in [({"hosted": True}, "hosted"), ({"desktop": True}, "desktop"), ({"docker": True}, "docker"),
                      ({"service": True}, "service"), ({}, "other")]:
        monkeypatch.setattr(da, "_kind_signals", lambda env=env: env)
        assert _client().get("/api/auth/dashboard-status").json()["install_kind"] == kind


def test_hosted_wins_over_docker(monkeypatch):
    import routes.dashboard_auth as da
    monkeypatch.setattr(da, "_kind_signals", lambda: {"hosted": True, "docker": True})
    assert da._install_kind() == "hosted"


def test_a_confirmed_email_offers_the_emailed_code():
    _signed_up()
    config.save_settings({"auth_email": "owner@example.com"})
    st = _client().get("/api/auth/dashboard-status").json()
    assert st["reset_email"] is True and st["reset_available"] is True


def test_no_email_change_password_and_reset_request_send_nothing(_fresh):
    c = _signed_up()
    assert c.post("/api/auth/dashboard-change-password",
                  json={"current_password": _PW, "new_password": "another one", "confirm": "another one"}).status_code == 200
    _client().post("/api/auth/reset-request", json={"who": "SecondFur"})
    assert _fresh == []                                      # SC-003 beyond sign-up


def test_the_in_app_steps_match_the_setup_guide():
    # The Forgot page and docs/setup/AFTER_INSTALL.md give the same commands (T009 / T026; SETUPSPLIT moved them).
    app = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "setup" / "AFTER_INSTALL.md").read_text(encoding="utf-8")
    for cmd in ("docker compose -f docker-compose.image.yml stop pawpoller",
                "docker compose -f docker-compose.image.yml run --rm pawpoller python server.py --reset-password",
                "docker compose -f docker-compose.image.yml start pawpoller",
                "Reset PawPoller password", "--reset-password",
                'schtasks /end /tn "PawPoller Server"', "sudo systemctl stop pawpoller-server",
                "set PAWPOLLER_APPDATA_DIR=C:\\ProgramData\\PawPoller-Server\\data",
                '"%LOCALAPPDATA%\\Programs\\PawPoller\\PawPoller.exe" --reset-password'):
        assert cmd in app.replace("\\\\", "\\").replace("\\'", "'").replace("&amp;", "&") and cmd in guide, cmd
    assert "API keys" in app and "API keys" in guide


# ── US3: Settings → Security ────────────────────────────────────────────────

def test_remove_email_tells_the_old_address_and_clears_codes(_fresh):
    c = _signed_up()
    config.save_settings({"auth_email": "owner@example.com", "auth_email_pending": "new@example.com",
                          "auth_code_reset": {"x": 1}, "auth_code_confirm": {"x": 1}})
    r = c.post("/api/auth/email/remove", json={"password": _PW})
    assert r.status_code == 200 and r.json() == {"email": "none"}
    s = config.get_settings()
    assert not any(s.get(k) for k in ("auth_email", "auth_email_pending", "auth_code_reset", "auth_code_confirm"))
    assert ("email_removed", "owner@example.com") in _fresh   # the confirmed address only
    assert ("email_removed", "new@example.com") not in _fresh


def test_a_reset_code_sent_before_removal_no_longer_works():
    import account_mail
    c = _signed_up()
    config.save_settings({"auth_email": "owner@example.com"})
    sent = {}
    account_mail._post = lambda to, purpose, code="": sent.setdefault("code", code) or True
    assert account_mail.send_code("reset", "owner@example.com")
    assert c.post("/api/auth/email/remove", json={"password": _PW}).status_code == 200
    r = _client().post("/api/auth/reset", json={"code": sent["code"], "password": "brand new pw", "confirm": "brand new pw"})
    assert r.status_code == 400


def test_remove_needs_the_password_and_is_not_for_hosted_seats(monkeypatch):
    c = _signed_up()
    config.save_settings({"auth_email": "owner@example.com"})
    assert c.post("/api/auth/email/remove", json={"password": "wrong one"}).status_code == 401
    monkeypatch.setenv("PAWPOLLER_HOSTED", "1")
    assert c.post("/api/auth/email/remove", json={"password": _PW}).status_code == 403
    assert config.get_settings().get("auth_email") == "owner@example.com"


@pytest.mark.parametrize("path, body", [
    ("/api/auth/email/remove", {"password": "wrong one"}),
    ("/api/auth/email", {"email": "a@example.com", "current_password": "wrong one"}),
    ("/api/auth/dashboard-change-password", {"current_password": "wrong one", "new_password": "another one",
                                             "confirm": "another one"}),
])
def test_wrong_passwords_count_toward_the_lockout(path, body, monkeypatch):
    import dashboard
    monkeypatch.setenv("PAWPOLLER_ALLOW_OPEN_SETUP", "1")   # a non-local address, so the lockout is its own
    c = _signed_up(ip="10.9.8.7")
    for _ in range(dashboard._AUTH_FAIL_MAX):
        assert c.post(path, json=body).status_code == 401
    assert c.post(path, json=body).status_code == 429      # FR-SEC-004


# ── the console reset ────────────────────────────────────────────────────────

def _console_reset(monkeypatch, running=False):
    import getpass
    import server
    answers = iter(["a fresh password", "a fresh password"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    monkeypatch.setattr(server, "_app_running", lambda *ports: running)
    return server._reset_password()


def test_console_reset_ends_every_sign_in(monkeypatch, capsys):
    _signed_up()
    before = config.get_settings().get("auth_session_secret")
    assert _console_reset(monkeypatch) == 0
    s = config.get_settings()
    assert config.verify_password("a fresh password", s["auth_password_hash"])
    assert s.get("auth_session_secret") and s["auth_session_secret"] != before
    assert "API keys" in capsys.readouterr().out


def _with_2fa():
    config.save_settings({"auth_totp_secret": "JBSWY3DPEHPK3PXP", "auth_totp_enabled": True,
                          "auth_totp_backup_codes": ["x"], "auth_totp_pending_secret": "KRSXG5CTMVRXEZLU"})


def test_console_reset_can_turn_two_step_off(monkeypatch, capsys):
    """A desktop install that lost its authenticator app and its backup codes had no way back in: the reset kept
    2FA on. It now asks, and y turns it off (every 2FA key goes); the password changes either way."""
    import server
    _signed_up()
    _with_2fa()
    asked = []
    monkeypatch.setattr(server, "_ask_yes", lambda prompt: asked.append(prompt) or True)
    assert _console_reset(monkeypatch) == 0
    s = config.get_settings()
    assert len(asked) == 1 and "Two-step sign-in is on" in asked[0]
    assert not any(s.get(k) for k in ("auth_totp_secret", "auth_totp_enabled", "auth_totp_pending_secret", "auth_totp_backup_codes"))
    assert config.verify_password("a fresh password", s["auth_password_hash"])
    assert "Two-step sign-in is off" in capsys.readouterr().out


def test_console_reset_keeps_two_step_unless_asked(monkeypatch, capsys):
    import server
    _signed_up()
    _with_2fa()
    monkeypatch.setattr(server, "_ask_yes", lambda prompt: False)
    assert _console_reset(monkeypatch) == 0
    s = config.get_settings()
    assert s.get("auth_totp_enabled") and s.get("auth_totp_secret") == "JBSWY3DPEHPK3PXP"
    assert "stays on" in capsys.readouterr().out


def test_console_reset_only_asks_about_two_step_when_it_is_on(monkeypatch):
    import server
    _signed_up()
    monkeypatch.setattr(server, "_ask_yes", lambda prompt: pytest.fail("asked with 2FA off"))
    assert _console_reset(monkeypatch) == 0


def test_the_yes_no_question_reads_the_windows_console_itself(monkeypatch, capsys):
    """The Windows build is a windowed program: no stdin, so input() would fail. It reads keys as getpass does,
    and only y or yes is yes (Ctrl+C is no; a backspace takes the last key back)."""
    import sys
    import types
    import server
    for keys, want in (("y\r", True), ("YES\r", True), ("\r", False), ("n\r", False), ("x\by\r", True), ("y\x03", False)):
        monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(getwche=iter(keys).__next__))
        monkeypatch.setattr(server.sys, "platform", "win32")
        monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("input() on Windows"))
        assert server._ask_yes("? ") is want, keys


def test_the_desktop_build_makes_no_console_log_handler_without_a_console():
    """The windowed build starts with sys.stdout None. A StreamHandler made on it failed on every record, and once
    --reset-password opened a console, Python printed each failure there: the real 4.92.3 window showed a
    "Logging error" traceback when the reset turned two-step sign-in off."""
    import ast
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    call = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "basicConfig")
    handlers = next(k.value for k in call.keywords if k.arg == "handlers")
    stream = [e for e in handlers.elts if "StreamHandler" in ast.unparse(e)]
    assert stream and all(isinstance(e, ast.Starred) and "if sys.stdout" in ast.unparse(e) for e in stream), ast.unparse(handlers)


def test_console_reset_refuses_while_pawpoller_is_open(monkeypatch, capsys):
    _signed_up()
    before = config.get_settings()["auth_password_hash"]
    assert _console_reset(monkeypatch, running=True) == 1
    assert config.get_settings()["auth_password_hash"] == before
    assert "still open" in capsys.readouterr().out


class _Kernel32:
    def __init__(self):
        self.calls = []

    def AllocConsole(self):
        self.calls.append("AllocConsole")
        return 1

    def AttachConsole(self, pid):
        self.calls.append("AttachConsole")
        return 1


def test_the_desktop_reset_opens_its_own_console_and_keeps_typing_hidden(monkeypatch, tmp_path):
    import main
    k = _Kernel32()
    opened = []
    monkeypatch.setattr(main, "_console_open", lambda name, mode: opened.append(name) or open(tmp_path / "out", mode))
    stdin = sys.stdin
    assert main._open_reset_console(k, frozen_windows=True) is True
    assert k.calls == ["AllocConsole"]                       # never the shell's console
    assert opened == ["CONOUT$", "CONOUT$"]                  # stdout + stderr only
    assert sys.stdin is stdin                                # getpass hides typing only while stdin is untouched
    sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__


def test_the_console_only_opens_for_a_frozen_windows_build():
    import main
    k = _Kernel32()
    assert main._open_reset_console(k, frozen_windows=False) is False and k.calls == []


def test_the_installer_adds_the_reset_shortcut():
    iss = (ROOT / "installer" / "PawPoller.iss").read_text(encoding="utf-8", errors="replace")
    line = next(l for l in iss.splitlines() if "Reset {#MyAppName} password" in l)
    assert re.search(r'Parameters:\s*"--reset-password"', line) and '{app}\\{#MyAppExeName}' in line
    import main
    assert main.RESET_SHORTCUT == "Reset {#MyAppName} password.lnk".replace("{#MyAppName}", "PawPoller")


def _setup_install(tmp_path):
    """A Setup copy after an update: no unins000.exe (updates deleted it until 4.92.5), PawPoller.lnk in the menu."""
    app = tmp_path / "app"
    app.mkdir(parents=True)
    (app / "PawPoller.exe").write_bytes(b"")
    menu = tmp_path / "menu"
    menu.mkdir(parents=True)
    (menu / "PawPoller.lnk").write_bytes(b"")
    return app / "PawPoller.exe", menu


def test_a_setup_install_that_updated_itself_gets_the_reset_item_back(tmp_path):
    """Updates inside the app never re-run the installer, so a copy installed before 4.88.0 had no
    "Reset PawPoller password" in its Start menu, and the sign-in page's steps pointed at nothing.
    4.92.4 looked for Inno's unins000.exe first, which those updates had deleted, so it never ran (4.92.5)."""
    import main
    calls = []

    def run(cmd, env):
        calls.append((cmd, env))
        Path(env["PP_LNK"]).write_bytes(b"")          # what PowerShell does when PawPoller.lnk opens this copy

    exe, menu = _setup_install(tmp_path)
    made = main._repair_start_menu(exe, [tmp_path / "no-such-menu", menu], run)
    assert made == str(menu / "Reset PawPoller password.lnk")
    cmd, env = calls[0]
    assert env["PP_LNK"] == made and env["PP_EXE"] == str(exe) and "'--reset-password'" in cmd[-1]
    assert env["PP_MAIN"] == str(menu / "PawPoller.lnk") and "($env:PP_MAIN).TargetPath -eq $env:PP_EXE" in cmd[-1]
    assert str(tmp_path) not in " ".join(cmd), "paths reach PowerShell as variables, never as command text"
    assert cmd[0].lower().endswith("\\system32\\windowspowershell\\v1.0\\powershell.exe"), cmd[0]   # never a bare name
    assert main._repair_start_menu(exe, [menu], run) is None and len(calls) == 1      # already there


def test_the_reset_item_is_only_added_beside_the_installers_own_item(tmp_path):
    import main
    calls = []
    exe, menu = _setup_install(tmp_path / "removed")
    (menu / "PawPoller.lnk").unlink()                                                          # they removed it
    assert main._repair_start_menu(exe, [menu], lambda cmd, env: calls.append(cmd)) is None
    assert calls == []
    exe, menu = _setup_install(tmp_path / "other")
    # PowerShell made nothing: the menu's PawPoller item opens another copy (this one is portable)
    assert main._repair_start_menu(exe, [menu], lambda cmd, env: calls.append(cmd)) is None and len(calls) == 1


def _shortcut(lnk, target):
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:PP_LNK); $s.TargetPath = $env:PP_T; $s.Save()"],
                   env={**__import__("os").environ, "PP_LNK": str(lnk), "PP_T": str(target)}, timeout=60, check=True)


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell.exe"), reason="Windows shortcuts")
def test_the_reset_item_really_is_made_on_windows(tmp_path):
    import main
    exe, menu = _setup_install(tmp_path / "Program Files (x86) & 'quotes'")
    _shortcut(menu / "PawPoller.lnk", exe)
    made = main._repair_start_menu(exe, [menu])
    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                          "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:PP_LNK); "
                          "$s.TargetPath + '|' + $s.Arguments"], env={**__import__("os").environ, "PP_LNK": made},
                         capture_output=True, text=True, timeout=60, check=True).stdout.strip()
    assert out == f"{exe}|--reset-password", out


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell.exe"), reason="Windows shortcuts")
def test_a_portable_copy_beside_a_setup_copy_adds_nothing_on_windows(tmp_path):
    import main
    setup_exe, menu = _setup_install(tmp_path / "setup")
    _shortcut(menu / "PawPoller.lnk", setup_exe)
    portable = tmp_path / "unzipped" / "PawPoller.exe"
    portable.parent.mkdir()
    portable.write_bytes(b"")
    assert main._repair_start_menu(portable, [menu]) is None
    assert not (menu / "Reset PawPoller password.lnk").exists()


def test_the_in_app_uninstall_removes_the_reset_item_the_app_added(tmp_path):
    import uninstall
    plan = uninstall.UninstallPlan(uninstall.InstallType.WINDOWS_INSTALLER, str(tmp_path), str(tmp_path / "data"),
                                   "", False)
    bat = uninstall._build_windows_script(plan, tmp_path, remove_data=False, remove_app=True).read_text(encoding="utf-8")
    for base in ("%APPDATA%", "%ProgramData%"):
        assert f'del "{base}\\Microsoft\\Windows\\Start Menu\\Programs\\Reset PawPoller password.lnk"' in bat


def test_an_emailed_reset_still_shows_the_way_past_two_step():
    """With an email, the reset page leads with the emailed code, which changes only the password. The way to turn
    two-step off sat in a closed section whose label never said so, and the sign-in hint sent people there (4.92.5)."""
    js = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    label = "Locked out by two-step sign-in"
    assert f"<summary>{label}" in js and f"then <b>{label}</b>" in js


# ── implement-stage security review (High): the server's own email gate ──────

def test_a_self_hosted_account_with_no_email_is_not_locked_out(monkeypatch):
    import dashboard
    import legal
    monkeypatch.setattr(dashboard, "SIGNUP_GATES", True)
    c = _signed_up()
    legal.accept(legal.current()["terms"], legal.current()["privacy"])
    assert c.get("/api/settings/preferences").status_code == 200
    monkeypatch.setenv("PAWPOLLER_HOSTED", "1")
    c_ = legal.current()   # the seat's own Terms
    legal.accept(c_["terms"], c_["privacy"], c_["hosted"])
    r = c.get("/api/settings/preferences")
    assert r.status_code == 403 and r.json().get("error") == "email"


def test_console_reset_refuses_where_typing_would_show(monkeypatch, capsys):
    """SEC058 / Security Design row 1: getpass only WARNS when it can't hide the typing, then echoes
    it. The reset turns that warning into a refusal, before anything is read."""
    import getpass
    import server
    _signed_up()
    before = config.get_settings()["auth_password_hash"]

    def echoing(prompt=""):
        import warnings
        warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning)  # noqa: B028
        return "seen on screen"
    monkeypatch.setattr(getpass, "getpass", echoing)
    monkeypatch.setattr(server, "_app_running", lambda *ports: False)
    assert server._reset_password() == 1
    assert config.get_settings()["auth_password_hash"] == before
    assert "can't hide" in capsys.readouterr().out


def test_console_reset_checks_the_port_it_was_given(monkeypatch):
    import server
    seen = []
    monkeypatch.setattr(server, "_app_running", lambda *ports: seen.extend(ports) or True)
    assert server._reset_password(8999) == 1
    assert 8999 in seen and config.DASHBOARD_PORT in seen


def test_the_running_check_counts_an_error_answer_and_nothing_answering(monkeypatch):
    import urllib.error
    import urllib.request
    import server

    def boom(exc):
        def f(*a, **k):
            raise exc
        return f
    monkeypatch.setattr(urllib.request, "urlopen", boom(urllib.error.HTTPError("u", 503, "x", {}, None)))
    assert server._app_running() is True
    monkeypatch.setattr(urllib.request, "urlopen", boom(ConnectionRefusedError()))
    assert server._app_running() is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_the_forgot_page_shows_a_hosted_seat_no_console_steps():
    """SEC058: a seat's person can't reach the box, so its Forgot page names no command. Runs the
    page's own _resetSteps from frontend/js/app.js for each install kind."""
    harness = r"""
const src = require('fs').readFileSync('frontend/js/app.js', 'utf8');
const a = src.indexOf('    _resetSteps(kind) {'), b = src.indexOf('\n    },', a);
const page = eval('({' + src.slice(a, b + 6) + '})');
const out = {};
for (const k of ['hosted', 'docker', 'desktop', 'service', 'other']) out[k] = page._resetSteps(k);
console.log(JSON.stringify(out));
"""
    r = subprocess.run(["node", "-e", harness], capture_output=True, text=True, encoding="utf-8", timeout=60,
                       cwd=str(Path(__file__).resolve().parents[1]))
    assert r.returncode == 0, r.stderr
    steps = json.loads(r.stdout)
    assert "--reset-password" not in steps["hosted"] and "<code>" not in steps["hosted"]
    assert "Ask the person who runs your PawPoller seat" in steps["hosted"]
    for kind in ("docker", "desktop", "service", "other"):
        assert "--reset-password" in steps[kind], kind
