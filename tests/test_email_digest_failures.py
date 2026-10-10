"""A weekly email that fails to send says so (DIGESTFAIL, 2026-10-07).

The operator never received a digest. Whatever the cause, nothing told them: a failed
send only reached the server log, the Settings tab kept saying "Not sent yet", nothing
reached the bell, and it was retried silently on every poll.
"""
from __future__ import annotations

import smtplib

import pytest

import config
from polling import email_digest as ed


def _configure(**extra):
    config.save_settings({
        "email_digest_enabled": True, "email_digest_recipients": "me@example.com",
        "smtp_host": "smtp.zoho.com", "smtp_port": 587, "smtp_username": "me@example.com",
        "smtp_password": "pw", **extra})


class _RefusingSMTP:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def ehlo(self): pass
    def starttls(self, **k): pass
    def login(self, u, p): raise smtplib.SMTPAuthenticationError(535, b"Authentication Failed")
    def sendmail(self, *a): raise AssertionError("not reached")


class _OkSMTP(_RefusingSMTP):
    sent = []
    def login(self, u, p): pass
    def sendmail(self, frm, to, msg): _OkSMTP.sent.append(to)


def test_a_refused_password_is_recorded_in_plain_words(monkeypatch):
    _configure()
    monkeypatch.setattr(smtplib, "SMTP", _RefusingSMTP)
    with pytest.raises(smtplib.SMTPAuthenticationError):
        ed.send_weekly_email_digest()
    s = config.get_settings()
    assert "app password" in s["last_email_digest_error"]
    assert s["last_email_digest_error_at"]
    assert not s.get("last_email_digest_sent_at")      # the weekly clock didn't move


def test_the_failure_reaches_the_bell_and_settings(monkeypatch):
    from routes import api as api_routes
    _configure(last_email_digest_error="smtp.zoho.com refused the username or password.",
               last_email_digest_error_at="2026-10-07T01:00:00+00:00")
    items = api_routes.get_notifications(40)["items"]
    digest = [i for i in items if i.get("kind") == "digest"]
    assert digest and digest[0]["status"] == "error"
    assert "refused" in digest[0]["detail"]
    status = api_routes.digest_status()
    assert status["last_error"].startswith("smtp.zoho.com refused")


def test_a_success_clears_the_error(monkeypatch):
    _configure(last_email_digest_error="old", last_email_digest_error_at="2026-10-07T01:00:00+00:00")
    monkeypatch.setattr(smtplib, "SMTP", _OkSMTP)
    assert ed.send_weekly_email_digest()["sent"] is True
    s = config.get_settings()
    assert s["last_email_digest_error"] == "" and s["last_email_digest_sent_at"]


def test_switched_on_with_no_recipients_is_an_error_not_silence():
    _configure(email_digest_recipients="")
    assert ed.send_weekly_email_digest() == {"sent": False, "reason": "no recipients"}
    assert "no recipients" in config.get_settings()["last_email_digest_error"]


def test_unreachable_server_names_host_and_port():
    msg = ed.explain_send_error(ConnectionRefusedError(111, "Connection refused"),
                                {"smtp_host": "smtp.example.com", "smtp_port": 25})
    assert "smtp.example.com" in msg and "25" in msg and "587" in msg
