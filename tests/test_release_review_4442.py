"""The four findings of the 4.44.1 release security review, fixed in 4.44.2."""
from __future__ import annotations

import io
import json
import logging

import pytest

import config
import log_redaction

SECRET = "SYNTHETICsecretVALUE0123456789"


# ── VAULTOVERWRITE ───────────────────────────────────────────────────────────

def test_an_unreadable_vault_is_set_aside_never_overwritten():
    """_decrypt_vault() returns {} on any failure; the next save used to REPLACE the vault,
    losing every stored login. Now the unreadable file is kept beside the new one."""
    config.save_settings({"fa_cookie_a": SECRET})
    original = config.VAULT_PATH.read_bytes()
    # The key no longer opens it (a lost keyring, a restored backup, a damaged file).
    config.VAULT_PATH.write_text(json.dumps({"version": 1, "encrypted": "not-a-fernet-token"}), encoding="utf-8")
    damaged = config.VAULT_PATH.read_bytes()
    assert config.get_settings().get("fa_cookie_a") in (None, "")
    config.save_settings({"theme": "dark"})
    kept = list(config.VAULT_PATH.parent.glob(config.VAULT_PATH.name + ".unreadable-*"))
    assert len(kept) == 1 and kept[0].read_bytes() == damaged          # nothing lost
    assert config.VAULT_PATH.exists() and config.VAULT_PATH.read_bytes() != original
    config.save_settings({"theme": "light"})                           # readable again: no more copies
    assert len(list(config.VAULT_PATH.parent.glob(config.VAULT_PATH.name + ".unreadable-*"))) == 1


def test_break_glass_never_deletes_a_vault_it_could_not_read():
    config.save_settings({"fa_cookie_a": SECRET})
    config.VAULT_PATH.write_text(json.dumps({"version": 1, "encrypted": "garbage"}), encoding="utf-8")
    config.migrate_to_cloud()
    assert config.VAULT_PATH.exists()


# ── LOGEXCMSG ────────────────────────────────────────────────────────────────

def _logged(emit) -> str:
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(message)s"))
    h.addFilter(log_redaction.SecretRedactingFilter())
    lg = logging.getLogger("review-4442")
    lg.addHandler(h)
    lg.propagate = False
    try:
        emit(lg)
    finally:
        lg.removeHandler(h)
    return buf.getvalue()


def test_an_exception_logged_as_the_message_is_masked():
    log_redaction.set_secrets([SECRET])
    out = _logged(lambda lg: lg.error(ValueError(f"upload refused, token {SECRET}")))
    assert "upload refused" in out and SECRET not in out


def test_stack_info_is_masked():
    log_redaction.set_secrets([SECRET])
    out = _logged(lambda lg: lg.error("here %s", "x", stack_info=True, extra={"k": SECRET}))
    assert "Stack (most recent call last)" in out

    def with_secret_in_stack(lg):
        rec = lg.makeRecord("review-4442", logging.ERROR, __file__, 1, "boom", (), None,
                            sinfo=f"Stack:\n  token={SECRET}")
        lg.handle(rec)
    assert SECRET not in _logged(with_secret_in_stack)


# ── PRIVLOCK ─────────────────────────────────────────────────────────────────

def test_the_privacy_page_is_locked_on_an_open_instance(monkeypatch):
    from fastapi.testclient import TestClient

    import dashboard
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    remote = TestClient(dashboard.app, raise_server_exceptions=False)
    assert remote.get("/api/privacy/holdings").status_code == 403
    local = TestClient(dashboard.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))
    assert local.get("/api/privacy/holdings").status_code == 200


# ── NAMESCRUBFAIL ────────────────────────────────────────────────────────────

def test_a_failed_name_lookup_is_not_cached(monkeypatch):
    import techcentre
    from database import db
    techcentre._names_cache = (0.0, frozenset(), ())

    def boom():
        raise RuntimeError("database is locked")
    monkeypatch.setattr(db, "get_connection", boom)
    techcentre._other_people()
    assert techcentre._names_cache[0] == 0.0          # next report tries again
    monkeypatch.undo()
    techcentre._other_people()
    assert techcentre._names_cache[0] > 0.0
    techcentre._names_cache = (0.0, frozenset(), ())


# ── 4.45.2 (4.45.1 release review) ───────────────────────────────────────────

def test_the_set_aside_vault_is_classified_restricted_and_never_ships():
    import datamap
    name = "data/settings.vault.json.unreadable-20260930-024500"
    assert datamap.path_class(name) == "restricted"
    assert datamap.public_copy_refused("settings.vault.json.unreadable-20260930-024500")
    assert datamap.public_copy_refused("somewhere/settings.vault.json.unreadable-20260930-024500")
    assert not datamap.public_copy_refused("docs/settings.md")


def test_the_set_aside_vault_is_owner_only(monkeypatch):
    locked = []
    monkeypatch.setattr(config, "_secure_file_permissions", lambda p: locked.append(p.name))
    config.save_settings({"fa_cookie_a": SECRET})
    config.VAULT_PATH.write_text(json.dumps({"version": 1, "encrypted": "garbage"}), encoding="utf-8")
    config.get_settings()
    config.save_settings({"theme": "dark"})
    assert any(n.startswith(config.VAULT_PATH.name + ".unreadable-") for n in locked), locked


def test_two_set_asides_in_the_same_second_both_survive():
    """4.45.3: the set-aside name is per-second; a second one gets a counter instead of replacing it."""
    import datamap
    for _ in range(2):
        config.VAULT_PATH.write_text(json.dumps({"version": 1, "encrypted": "garbage"}), encoding="utf-8")
        config.get_settings()                       # read fails -> flagged unreadable
        config.save_settings({"theme": "dark"})     # -> set aside
    kept = sorted(config.VAULT_PATH.parent.glob(config.VAULT_PATH.name + ".unreadable-*"))
    assert len(kept) == 2, kept
    for k in kept:                                   # the test vault has a test_ prefix; the real one doesn't
        real = "settings.vault.json" + k.name.split(config.VAULT_PATH.name, 1)[1]
        assert datamap.path_class("data/" + real) == "restricted", real
