"""Tests for the operator-supplied credential-vault key (2.46.1).

On a server the vault key otherwise lands in a `.vault_key` dotfile next to the
ciphertext (no real at-rest protection). An operator can now supply the key
out-of-band via PAWPOLLER_VAULT_KEY or PAWPOLLER_VAULT_KEY_FILE; it takes
priority over the keyring/dotfile, and a malformed key fails fast.
"""
import config
import pytest

pytest.importorskip("cryptography")  # a real dep (requirements*.txt); skip if a bare env lacks it
from cryptography.fernet import Fernet


def test_operator_key_from_env_takes_priority(monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setenv("PAWPOLLER_VAULT_KEY", key.decode())
    assert config._get_vault_key() == key


def test_operator_key_from_file(monkeypatch, tmp_path):
    key = Fernet.generate_key()
    kf = tmp_path / "vault.key"
    kf.write_bytes(key)
    monkeypatch.delenv("PAWPOLLER_VAULT_KEY", raising=False)
    monkeypatch.setenv("PAWPOLLER_VAULT_KEY_FILE", str(kf))
    assert config._get_vault_key() == key


def test_malformed_operator_key_fails_fast(monkeypatch):
    monkeypatch.setenv("PAWPOLLER_VAULT_KEY", "not-a-valid-fernet-key")
    with pytest.raises(Exception):
        config._get_vault_key()


def test_no_operator_key_returns_none(monkeypatch):
    monkeypatch.delenv("PAWPOLLER_VAULT_KEY", raising=False)
    monkeypatch.delenv("PAWPOLLER_VAULT_KEY_FILE", raising=False)
    assert config._operator_vault_key() is None


def test_vault_roundtrip_with_operator_key(monkeypatch, tmp_path):
    # A vault encrypted under the operator key decrypts back to the same creds.
    key = Fernet.generate_key()
    monkeypatch.setenv("PAWPOLLER_VAULT_KEY", key.decode())
    monkeypatch.setattr(config, "VAULT_PATH", tmp_path / "settings.vault.json")
    creds = {"ib_password": "s3cret", "fa_cookie_a": "abc123"}
    with config._settings_lock:
        config._encrypt_vault(creds)
        assert config._decrypt_vault() == creds
