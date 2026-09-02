"""ASVS V7.4.3 — rotating the session secret invalidates existing sessions.

A password change calls config.rotate_session_secret(), which must make every
previously-issued session cookie fail verification (the stateless signed cookie
can't be revoked individually, so we rotate the signing key).
"""
import config


def test_rotate_invalidates_old_cookie(monkeypatch):
    # Ensure a clean cache each run.
    monkeypatch.setattr(config, "_session_secret_cache", None, raising=False)

    cookie = config.sign_session({"u": "admin"})
    assert config.verify_session(cookie) == {"u": "admin"}

    config.rotate_session_secret()

    # The cookie signed under the old secret must no longer verify.
    assert config.verify_session(cookie) is None
    # A freshly signed cookie under the new secret works.
    assert config.verify_session(config.sign_session({"u": "admin"})) == {"u": "admin"}


def test_rotate_changes_stored_secret(monkeypatch):
    monkeypatch.setattr(config, "_session_secret_cache", None, raising=False)
    first = config.get_or_create_session_secret()
    config.rotate_session_secret()
    second = config.get_or_create_session_secret()
    assert first != second
    assert len(second) == 64  # token_hex(32)
