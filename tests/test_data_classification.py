"""Data classification (spec 011): every table, setting and data file has a class, and the rules hold.

`datamap.py` is the one list. These tests are what make it a practice rather than a document:
add a table, a setting or a data file without classifying it and the suite fails, naming it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import config
import datamap

ROOT = Path(__file__).resolve().parent.parent
# App code only: tests, the marketing site, one-off scripts and the release tooling hold no
# data of their own.
_SKIP_DIRS = {"tests", "site", "_archive", "build", "dist", "node_modules", "prototype", "qa", "scripts",
              "scripts_utils", "deploy", "__pycache__", ".specify", ".plan", "testing", "server-update",
              "specs", "docs", "installer", "assets", "data", "logs", ".git", ".venv", "venv"}


def _app_files(*suffixes):
    for p in ROOT.rglob("*"):
        if p.suffix not in suffixes or not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in _SKIP_DIRS for part in rel.parts[:-1]):
            continue
        yield rel, p.read_text(encoding="utf-8", errors="replace")


def _fresh_tables():
    from database.db import get_connection, init_db
    init_db()
    conn = get_connection()
    try:
        return datamap._tables(conn)
    finally:
        conn.close()


# ── US1: everything has a class ──────────────────────────────────────────────

def test_every_table_a_fresh_install_makes_is_classified():
    missing = [t for t in _fresh_tables() if not datamap.table_class(t)]
    assert not missing, f"classify these tables in datamap.py: {missing}"


def test_the_spec_examples_land_in_the_agreed_class():
    s, t = datamap.setting_class, datamap.table_class
    for k in ("fa_cookie_a", "tw_auth_token", "bsky_app_password", "auth_password_hash", "auth_totp_secret",
              "auth_session_secret", "auth_api_keys", "discord_webhook_url", "telegram_bot_token", "tg_bot_token",
              "acct_7_fa_cookie_b", "fa_username", "acct_7_fa_username"):
        assert s(k) == "restricted", k
    for name in ("accounts", "personas", "share_tokens", "session_cache"):
        assert t(name) == "restricted", name          # persona↔account links + live secrets
    for name in ("artists", "artist_handles", "watchers", "faving_users", "fa_watchers", "ao3_kudos_users",
                 "comments", "platform_comments", "commissions", "post_contacts", "post_mentions", "trello_cards",
                 "posts"):
        assert t(name) == "confidential", name
    for name in ("snapshots", "fa_snapshots", "poll_log", "yt_poll_log", "posting_queue", "posting_log"):
        assert t(name) == "internal", name
    for name in ("submissions", "fa_submissions", "ao3_submissions", "podcast_feeds"):
        assert t(name) == "public", name
    assert datamap.path_class("data/tech_pending.json") == "confidential"      # error reports
    assert datamap.path_class("logs/app.log") == "internal"                    # the log, after redaction
    assert datamap.path_class("data/settings.vault.json") == "restricted"
    assert datamap.path_class("data/artwork/some/file.png") == "confidential"  # a folder covers its contents
    assert s("not_a_real_setting") is None and t("not_a_real_table") is None   # unknown stays unknown


def test_containers_are_at_least_their_highest_content():
    """FR-005. The database, and the copies of it, hold every table — so they take the top class.
    (The log is the one documented exception: your own handles stay legible there by design,
    and every secret is masked, so it is Internal — datamap.HANDLING['restricted']['in_logs'].)"""
    top = max(datamap.rank(datamap.table_class(t)) for t in _fresh_tables())
    for p in ("data/pawpoller.db", "data/pawpoller.db-wal", "data/backups/"):
        assert datamap.rank(datamap.path_class(p)) >= top, p
    assert datamap.rank(datamap.path_class("data/settings.json")) == datamap.rank("restricted")


# ── US2: enforced, so it can't lapse ─────────────────────────────────────────

# A real statement names the table and opens its column list; prose in comments
# ("…gets the column in its CREATE TABLE too") doesn't.
_CREATE = re.compile(r"""CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["'`\[]?([a-z_][a-z0-9_]*)["'`\]]?\s*\(""")
# Built on the fly by the code that owns them, never stored: a migration's scratch copy
# ("<table>_new" renamed straight back) and the temp tables of a single query.
_NOT_STORED = re.compile(r"^(_|tmp_|temp_)|_new$|_old$")


def test_every_create_table_in_the_source_is_classified():
    """Catches tables made lazily, outside init_db (share_tokens, inbox_state and pp_meta are made that way)."""
    missing = {}
    for rel, src in _app_files(".py", ".sql"):
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith(("#", "--")))
        for name in _CREATE.findall(code):
            if _NOT_STORED.search(name) or datamap.table_class(name):
                continue
            missing.setdefault(name, str(rel))
    assert not missing, f"classify these tables in datamap.py: {missing}"


_KEY = r"""["']([a-z][a-z0-9_]{1,60})["']"""
_ASSIGN = re.compile(r"\b(\w+)\s*(?::\s*dict\s*)?=\s*(?:config\.)?(?:get_settings|_load_settings)\(\)")
_PASSED = re.compile(r"save_settings\(\s*(\w+)\s*\)")
_DIRECT = re.compile(r"(?:config\.)?get_settings\(\)\s*(?:\.get\(|\[)\s*" + _KEY)
_SAVE_LIT = re.compile(r"save_settings\(\s*\{(.*?)\}\s*\)", re.S)
_DICT_KEY = re.compile(_KEY + r"\s*:")
_DELETE = re.compile(r"delete_settings_keys\(\s*\[(.*?)\]", re.S)
# Same idiom, not a setting: routes/ig_api.py reuses `s` for a submission row it decorates
# with deltas before returning it.
_NOT_SETTINGS = {"comments_delta", "keywords", "likes_delta", "reach_delta", "saved_delta", "shares_delta",
                 "submission_id", "title", "views_delta"}


def _setting_keys_in(src: str) -> set:
    out = set(_DIRECT.findall(src))
    passed = set(_PASSED.findall(src))
    names = (set(_ASSIGN.findall(src)) | passed | {"settings"}) - {"_"}
    for n in names:
        out |= set(re.findall(rf"\b{re.escape(n)}(?:\.get\(|\.setdefault\(|\.pop\(|\[)\s*" + _KEY, src))
        if n in passed:
            for m in re.finditer(rf"\b{re.escape(n)}\s*(?::\s*dict\s*)?=\s*\{{(.*?)\}}", src, re.S):
                out |= set(_DICT_KEY.findall(m.group(1)))
    for m in _SAVE_LIT.finditer(src):
        out |= set(_DICT_KEY.findall(m.group(1)))
    for m in _DELETE.finditer(src):
        out |= set(re.findall(_KEY, m.group(1)))
    return out


def _code_setting_keys() -> dict:
    found = {}
    for rel, src in _app_files(".py"):
        if "get_settings" not in src and "save_settings" not in src:
            continue
        for k in _setting_keys_in(src):
            found.setdefault(k, str(rel))
    for k in config.CREDENTIAL_FIELDS:
        found.setdefault(k, "config.CREDENTIAL_FIELDS")
    for fields in config.PLATFORM_CREDENTIAL_FIELDS.values():
        for k in fields:
            found.setdefault(k, "config.PLATFORM_CREDENTIAL_FIELDS")
    return found


def test_every_setting_the_code_reads_is_classified():
    # ponytail: a literal-key scan. A key built at run time from a shape datamap doesn't know
    # (not <P>_… or acct_<id>_…) slips past it; the instance check (Settings → Privacy,
    # `python -m datamap check`) lists it on any real install that holds one.
    found = _code_setting_keys()
    assert len(found) > 200, "the scan found almost nothing — its idioms no longer match the code"
    missing = {k: f for k, f in found.items() if k not in _NOT_SETTINGS and not datamap.setting_class(k)}
    assert not missing, f"classify these settings in datamap.py: {missing}"


_PATH_LIT = re.compile(r"""\b(DATA_DIR|LOGS_DIR|APPDATA_DIR)\)?\s*/\s*["']([^"'/]+)["']""")


def test_every_data_path_is_classified():
    prefix = {"DATA_DIR": "data/", "LOGS_DIR": "logs/", "APPDATA_DIR": ""}
    missing = {}
    for rel, src in _app_files(".py"):
        for base, name in _PATH_LIT.findall(src):
            if base == "APPDATA_DIR" and name in ("data", "logs"):
                continue
            p = prefix[base] + name
            if not (datamap.path_class(p) or datamap.path_class(p + "/")):
                missing.setdefault(p, str(rel))
    assert not missing, f"classify these data paths in datamap.py: {missing}"


def test_restricted_settings_are_vault_held():
    """FR-008: every Restricted secret lives in the encrypted vault and is masked in logs.
    Handles (identity) are the one exception, legible in your own log (HANDLING)."""
    for e in datamap.ENTRIES:
        if e.kind != "setting" or e.cls != "restricted" or e.identity or "<P>" in e.name:
            continue
        assert config.is_credential_key(e.name), e.name
        assert config.is_credential_key(f"acct_9_{e.name}"), e.name
        assert not datamap.is_identity_key(e.name), e.name    # → the log value layer masks it


def test_backup_coverage():
    """FR-010: nothing that can't be rebuilt is silently left out of backups."""
    backlog = ROOT / "docs" / "BACKLOG.md"
    rows = set(re.findall(r"^\| ([A-Z0-9]+) \|", backlog.read_text(encoding="utf-8"), re.M)) if backlog.exists() else None
    for e in datamap.ENTRIES:
        route = e.backup_route
        assert route in datamap.BACKUP_ROUTES or route.startswith("gap:"), (e.name, route)
        if route.startswith("gap:") and rows is not None:          # BACKLOG isn't in the public copy
            assert route[4:] in rows, f"{e.name}: {route} names no backlog row"


def test_backup_dirs_match_the_registry():
    """BACKUPGAPS (4.45.5): every data/ folder the registry routes to "files" is in the backup, and back."""
    from routes import backup_api
    registry = {e.name[len("data/"):-1] for e in datamap.ENTRIES
                if e.kind == "path" and e.name.startswith("data/") and e.name.endswith("/") and e.backup_route == "files"}
    assert registry == set(backup_api._BACKUP_DIRS) - {"story-archive"}, registry ^ set(backup_api._BACKUP_DIRS)


def test_the_registry_is_well_formed_and_holds_no_personal_data():
    seen = set()
    for e in datamap.ENTRIES:
        assert (e.kind, e.name) not in seen, f"duplicate {e.kind} {e.name}"
        seen.add((e.kind, e.name))
        assert e.group in datamap.GROUPS and e.reason.strip(), e
        assert e.kind in ("table", "setting", "path"), e
        # It ships in the public copy: kinds of data, never an account, a handle or a person.
        assert not re.search(r"(^|_)\d+(_|$)", e.name), f"a bare number in {e.name!r} — an account id?"
        assert "@" not in e.why and not re.search(r"\b\d{2,}\b", e.why), e
        if e.names:
            assert e.cls == "confidential" and e.kind == "table", e
    for g in datamap.GROUPS.values():
        assert g[1] in datamap.HANDLING and all(x.strip() for x in g[2:]), g
    for cls, *_ in datamap.CLASSES:
        assert set(datamap.HANDLING[cls]) == {k for k, _ in datamap.SITUATIONS}, cls


def test_platform_prefixes_match_what_init_db_makes():
    tables = set(_fresh_tables())
    for p in datamap.PLATFORM_PREFIXES:
        for fam in ("submissions", "snapshots", "poll_log"):
            assert f"{p}_{fam}" in tables, f"{p}_{fam}: <P> expands to a table that isn't made"
    made = {m.group(1) for t in tables for m in [re.match(r"^([a-z0-9]+)_poll_log$", t)] if m}
    assert made == set(datamap.PLATFORM_PREFIXES), made ^ set(datamap.PLATFORM_PREFIXES)


def test_a_new_unclassified_table_and_setting_are_named(tmp_path):
    """SC-002, without editing the repo: the instance check names what the list doesn't know."""
    from database.db import get_connection
    conn = get_connection()
    try:
        conn.execute("CREATE TABLE scratch_thing (id INTEGER)")
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "mystery.bin").write_bytes(b"x")
        got = datamap.unclassified(conn, {"scratch_setting": "x", "theme": "dark"}, tmp_path)
    finally:
        conn.close()
    assert {"kind": "table", "name": "scratch_thing"} in got
    assert {"kind": "setting", "name": "scratch_setting"} in got
    assert {"kind": "path", "name": "data/mystery.bin"} in got
    assert not any(g["name"] == "theme" for g in got)


# ── US4: the safety nets follow the list ─────────────────────────────────────

_FAKE_HOOK = "https://discord.com/api/webhooks/123456789012345678/FAKEwebhookTOKENabcdefghijklmnopqrstuvwxyz0123"


def test_webhook_moves_into_the_vault_on_upgrade(caplog):
    """4.44.0: the Discord webhook URL carries its token, but it sat in plaintext settings.json."""
    import json
    import logging
    from posting import discord
    config.SETTINGS_PATH.write_text(json.dumps({"discord_webhook_url": _FAKE_HOOK, "theme": "dark"}), encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        assert config.ensure_vault() >= 1
    on_disk = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
    assert "discord_webhook_url" not in on_disk and on_disk["theme"] == "dark"
    assert _FAKE_HOOK not in config.SETTINGS_PATH.read_text(encoding="utf-8")
    assert _FAKE_HOOK not in config.VAULT_PATH.read_bytes().decode("utf-8", "replace")     # encrypted
    assert discord._webhook_url() == _FAKE_HOOK                                            # still works
    assert _FAKE_HOOK not in caplog.text
    before = config.VAULT_PATH.read_bytes()
    assert config.ensure_vault() == 0 and config.VAULT_PATH.read_bytes() == before         # safe twice


def _plant_people(conn) -> list[str]:
    """A fake person in every column the registry says holds other people's names."""
    planted = []
    conn.execute("PRAGMA foreign_keys = OFF")          # a planted row has no real parent
    for n, (table, col) in enumerate(datamap.name_columns()):
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not info:
            continue
        vals = {}
        for _cid, name, ctype, notnull, default, pk in info:
            if name == col:
                vals[name] = f"Plantedperson{n:02d}"
            elif notnull and default is None and not (pk and "INT" in (ctype or "").upper()):
                vals[name] = 1 if "INT" in (ctype or "").upper() else f"x{n}"
        cols = ", ".join(f'"{c}"' for c in vals)
        conn.execute(f'INSERT INTO "{table}" ({cols}) VALUES ({", ".join("?" * len(vals))})', list(vals.values()))
        planted.append(vals[col])
    conn.execute("INSERT INTO artists (artist_key, name) VALUES ('planted-multi', 'Planted Multiword Person')")
    conn.commit()
    return planted + ["Planted Multiword Person"]


def test_no_planted_secret_escapes():
    """SC-003: a unique fake in every vault-held setting and a fake person in every name column.
    None may reach the log (your own handles excepted, by design), an error report or the check-in."""
    import io
    import json
    import logging

    import log_redaction
    import techcentre
    from database.db import get_connection

    fakes = {k: f"PLANTED{i:03d}{k.replace('_', '')}QZ" for i, k in enumerate(sorted(datamap.vault_fields()))}
    config.save_settings(dict(fakes))
    config.refresh_log_secrets()
    conn = get_connection()
    try:
        people = _plant_people(conn)
    finally:
        conn.close()
    assert len(people) >= 10

    # 1. The log, through the real redacting filter.
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(log_redaction.SecretRedactingFilter())
    lg = logging.getLogger("planted-secrets")
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    try:
        for v in fakes.values():
            lg.info("value %s", v)
            lg.info(f"inline {v}")
            lg.info("HTTP Request: GET %s", f"https://example.test/cb?x=1&token={v}")
            lg.info("failed: %r", ValueError(f"bad value {v!r}"))
            lg.info("failed: %s", ValueError(f"bad value {v}"))
            try:
                raise RuntimeError(f"upload refused for {v}")
            except RuntimeError:
                lg.exception("upload failed")
        lg.info("HTTP Request: POST %s", _FAKE_HOOK)
        lg.info('%s - "%s %s HTTP/1.1" %d', "127.0.0.1", "GET", "/share/PLANTEDsharetokenABCDEFGH0123", 200)
    finally:
        lg.removeHandler(handler)
    text = buf.getvalue()
    for k, v in fakes.items():
        if datamap.is_identity_key(k):
            assert v in text, f"{k}: your own handle should stay readable in your own log"
        else:
            assert v not in text, f"{k} reached the log"
    assert "FAKEwebhookTOKEN" not in text and "PLANTEDsharetoken" not in text

    # 2. Error reports and the check-in: nothing Restricted, no one else's name.
    techcentre._handles_cache = (0.0, [])
    techcentre._names_cache = (0.0, frozenset(), ())
    for v in list(fakes.values()) + people:
        r = techcentre.build("exception", "somewhere", "ValueError", f"failed for {v} while posting",
                             tb=f'File "x.py", line 1\n  raise ValueError({v!r})', log_tail_ok=False)
        body = json.dumps(r)
        assert v not in body, f"{v} reached an error report"
    body = json.dumps(techcentre.checkin_payload())
    assert not any(v in body for v in list(fakes.values()) + people)
    techcentre._names_cache = (0.0, frozenset(), ())


def test_the_registry_masks_at_least_what_the_old_guesses_did():
    """Before 4.44.0 the Tech Centre scrubber and the log redactor each guessed identity from key
    names. The registry replaced both; it may only ever mask MORE, never less."""
    old_handle = re.compile(r"(?i)(username|identifier|handle|author|screen_name|login|account_name|display_name)")
    for k in _code_setting_keys():
        if k in _NOT_SETTINGS or "url" in k.lower():
            continue
        if old_handle.search(k):
            assert datamap.is_identity_key(k) or k in datamap.vault_fields(), f"{k}: was scrubbed from reports"
    # The log: a vault key the old hints kept readable may stay readable; any other stays masked.
    import log_redaction
    for k in config.CREDENTIAL_FIELDS:
        if datamap.is_identity_key(k):
            assert any(h in k.lower() for h in log_redaction._IDENTITY_HINTS), f"{k}: newly left unmasked"


# ── US3: Settings → Privacy ──────────────────────────────────────────────────

def _holdings_client(monkeypatch, auth=False):
    from fastapi.testclient import TestClient

    import dashboard
    from routes import privacy_api
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: auth)
    privacy_api._cache = (0.0, {})
    # From the server's own machine: with no password set, /api/privacy is locked to
    # anyone else (4.44.2, PRIVLOCK — tested in test_release_review_4442.py).
    return TestClient(dashboard.app, client=("127.0.0.1", 50000))


def test_holdings_counts_without_values(monkeypatch):
    """US3: every class and group, real counts, and not one planted value anywhere in the answer."""
    import json

    from database.db import get_connection
    fakes = {k: f"PLANTED{i:03d}{k.replace('_', '')}QZ" for i, k in enumerate(sorted(datamap.vault_fields()))}
    config.save_settings(dict(fakes))
    conn = get_connection()
    try:
        people = _plant_people(conn)
        watchers = conn.execute("SELECT COUNT(*) FROM watchers").fetchone()[0]
    finally:
        conn.close()
    r = _holdings_client(monkeypatch).get("/api/privacy/holdings")
    assert r.status_code == 200
    body = r.text
    for v in list(fakes.values()) + people:
        assert v not in body, f"{v} reached the Privacy page"
    data = json.loads(body)
    assert [c["cls"] for c in data["classes"]] == ["restricted", "confidential", "internal", "public"]
    groups = {g["group"]: g for c in data["classes"] for g in c["groups"]}
    assert "Sign-ins and keys" in groups and "Your audience" in groups
    counted = {i["name"]: i["count"] for i in groups["Your audience"]["items"]}
    assert counted["watchers"] == watchers >= 1
    assert all(len(c["handling"]) == len(datamap.SITUATIONS) for c in data["classes"])


def test_a_newly_classified_table_appears_with_no_page_change(monkeypatch):
    """US3 scenario 2: the page is generated from the list."""
    from database.db import get_connection
    conn = get_connection()
    try:
        conn.execute("CREATE TABLE scratch_newthing (id INTEGER)")
        conn.commit()
    finally:
        conn.close()
    before = _holdings_client(monkeypatch).get("/api/privacy/holdings").json()
    assert {"kind": "table", "name": "scratch_newthing"} in before["unclassified"]
    monkeypatch.setitem(datamap._EXACT, ("table", "scratch_newthing"),
                        datamap.T("scratch_newthing", "Numbers over time", "A test table."))
    after = _holdings_client(monkeypatch).get("/api/privacy/holdings").json()
    assert {"kind": "table", "name": "scratch_newthing"} not in after["unclassified"]
    internal = next(c for c in after["classes"] if c["cls"] == "internal")
    names = [i["name"] for g in internal["groups"] for i in g["items"]]
    assert "scratch_newthing" in names


def test_holdings_needs_the_dashboard_sign_in(monkeypatch):
    """US3 scenario 3: with a dashboard password set, a stranger is refused."""
    assert _holdings_client(monkeypatch, auth=True).get("/api/privacy/holdings").status_code == 401


def test_datamap_check_cli(tmp_path):
    """`python -m datamap check`: read-only, names only, ASCII, exit 0/1."""
    import json
    import sqlite3
    import subprocess
    import sys
    db = tmp_path / "copy.db"
    src = sqlite3.connect(str(config.DB_PATH))
    dst = sqlite3.connect(str(db))
    src.backup(dst)
    src.close()
    (tmp_path / "settings.json").write_text(json.dumps({"theme": "dark", "fa_username": "x"}), encoding="utf-8")

    def run():
        return subprocess.run([sys.executable, "-m", "datamap", "check", "--db", str(db),
                               "--settings", str(tmp_path / "settings.json")],
                              cwd=ROOT, capture_output=True, timeout=120)
    ok = run()
    assert ok.returncode == 0 and b"[OK]" in ok.stdout, ok.stdout + ok.stderr
    dst.execute("CREATE TABLE scratch_cli (id INTEGER)")
    dst.commit()
    dst.close()
    (tmp_path / "settings.json").write_text(json.dumps({"scratch_cli_key": "SECRETVALUE"}), encoding="utf-8")
    bad = run()
    assert bad.returncode == 1
    assert b"UNCLASSIFIED table scratch_cli" in bad.stdout and b"UNCLASSIFIED setting scratch_cli_key" in bad.stdout
    assert b"SECRETVALUE" not in bad.stdout
    bad.stdout.decode("ascii")                           # ASCII only (Windows pipes)


def _render_privacy(data: dict) -> str:
    import json
    import subprocess
    js = (ROOT / "frontend" / "js" / "privacy.js").read_text(encoding="utf-8")
    harness = """
    global.window = {};
    global.Utils = { escapeHtml: (s) => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') };
    window.Utils = global.Utils;
    %s
    process.stdout.write(window.Privacy.render(%s));
    """ % (js, json.dumps(data))
    r = subprocess.run(["node", "-e", harness], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node is not installed")
def test_privacy_page_renders_every_class_and_escapes():
    from database.db import get_connection
    conn = get_connection()
    try:
        data = datamap.holdings(conn, {"fa_username": "x", "theme": "dark"}, None)
    finally:
        conn.close()
    data["unclassified"] = []
    html = _render_privacy(data)
    for label in ("Restricted", "Confidential", "Internal", "Public"):
        assert f"<h3>{label}</h3>" in html
    assert "privacy-unknown" not in html                      # no notice when everything is known
    data["unclassified"] = [{"kind": "table", "name": "<img src=x onerror=alert(1)>"}]
    html = _render_privacy(data)
    assert "privacy-unknown" in html and "isn't in the list yet" in html
    assert "<img" not in html and "&lt;img" in html            # escaped


# ── Found by the Privacy page on the operator's server (4.44.0) ──────────────

def test_self_test_leftovers_are_removed_on_start_and_only_those():
    import json
    config.SETTINGS_PATH.write_text(json.dumps(
        {"sample": "diagnostic", "n": 42, "_diagnostics_test_marker": "", "theme": "dark"}), encoding="utf-8")
    config.ensure_vault()
    s = config.get_settings()
    assert not {"sample", "n", "_diagnostics_test_marker"} & set(s) and s["theme"] == "dark"
    # Anything that isn't exactly the self-test's own value is left alone.
    config.save_settings({"sample": "mine", "n": 42})
    config.ensure_vault()
    assert config.get_settings()["sample"] == "mine" and config.get_settings()["n"] == 42


def test_the_vault_self_test_never_swaps_the_live_vault_path():
    """It pointed config.VAULT_PATH at a temp file while the app ran; a save in that window leaked
    its payload into settings.json and could strand real credentials in the deleted temp vault."""
    import asyncio

    from testing.tests import infra

    class Ctx:
        def detail(self, *a):
            pass

        def skip(self, msg):
            return pytest.skip.Exception(msg)

    before = config.VAULT_PATH
    seen = []
    orig = config.save_settings
    config.save_settings = lambda d: (seen.append(config.VAULT_PATH), orig(d))
    try:
        asyncio.run(infra.t_vault_crypto(Ctx()))
        asyncio.run(infra.t_settings_roundtrip(Ctx()))
    finally:
        config.save_settings = orig
    assert config.VAULT_PATH == before and all(p == before for p in seen)
    assert "_diagnostics_test_marker" not in config.get_settings()
    src = (ROOT / "testing" / "tests" / "infra.py").read_text(encoding="utf-8")
    assert not re.search(r"config\.[A-Z_]+(PATH|DIR)\s*=", src)
