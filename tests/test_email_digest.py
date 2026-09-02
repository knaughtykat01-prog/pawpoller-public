"""Weekly email digest (gap-wave-6) — deterministic, templated, NO AI.

Locks the four things that matter for a recap email built entirely from your
own poll data:
  1. BUILD   — pools 7-day deltas + current totals per platform into a dict.
  2. RENDER  — the HTML/text parts carry the real numbers.
  3. GUARD   — send_email refuses to send without full SMTP config / recipients.
  4. DELIVER — send_email drives smtplib correctly (login + sendmail to all
     recipients) without ever touching a real network (smtplib is monkeypatched).
"""
import smtplib

from database.db import get_connection
from polling import email_digest as ed


def _seed_ib(conn):
    """One IB piece at 100 views now, with a week-old snapshot baseline of 60 →
    a +40 view delta over the 7-day window."""
    conn.execute("INSERT INTO submissions (submission_id, title, views, "
                 "favorites_count, comments_count) VALUES (1,'Wolf',100,10,2)")
    conn.execute("INSERT INTO snapshots (submission_id, views, favorites_count, "
                 "comments_count, polled_at) "
                 "VALUES (1,60,4,1,datetime('now','-8 days'))")
    conn.commit()


def test_build_pools_deltas_and_totals():
    conn = get_connection()
    try:
        _seed_ib(conn)
        data = ed.build_weekly_digest_data(conn, days=7)
        ib = next((p for p in data["platforms"] if p["code"] == "ib"), None)
        assert ib is not None
        assert ib["views_total"] == 100
        assert ib["views_delta"] == 40          # 100 - 60
        assert data["combined"]["views_delta"] >= 40
        assert data["combined"]["views_total"] >= 100
        assert data["has_activity"] is True
    finally:
        conn.close()


def test_render_html_and_text_carry_numbers():
    conn = get_connection()
    try:
        _seed_ib(conn)
        data = ed.build_weekly_digest_data(conn, days=7)
        html = ed.render_weekly_digest_html(data)
        text = ed.render_weekly_digest_text(data)
        assert html.strip().startswith("<!DOCTYPE")
        assert "PawPoller Weekly Digest" in html
        assert "+40" in html                    # the view delta shows up
        assert "100" in text and "+40" in text
    finally:
        conn.close()


def test_parse_recipients_dedupes_and_splits():
    s = {"email_digest_recipients": "a@x.com, b@y.com; a@x.com  c@z.com\nnot-an-email"}
    assert ed.parse_recipients(s) == ["a@x.com", "b@y.com", "c@z.com"]
    assert ed.parse_recipients({"email_digest_recipients": ""}) == []


def test_send_email_requires_full_config():
    import pytest
    # No recipients
    with pytest.raises(ValueError):
        ed.send_email({"smtp_host": "h", "smtp_username": "u", "smtp_password": "p"},
                      "s", "<b>x</b>")
    # Recipients but no password
    with pytest.raises(ValueError):
        ed.send_email({"email_digest_recipients": "a@x.com",
                       "smtp_host": "h", "smtp_username": "u"}, "s", "<b>x</b>")


class _FakeSMTP:
    """Records the smtplib calls a send makes, without any network."""
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.tls = False
        self.logged_in = None
        self.sent = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, pw):
        self.logged_in = (user, pw)

    def sendmail(self, sender, recipients, body):
        self.sent = {"from": sender, "to": recipients, "body": body}


def test_send_email_drives_smtp(monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    settings = {
        "email_digest_recipients": "one@x.com, two@y.com",
        "smtp_host": "smtp.example.com", "smtp_port": 587,
        "smtp_username": "me@example.com", "smtp_password": "secret",
        "smtp_from": "me@example.com", "smtp_use_tls": True,
    }
    ed.send_email(settings, "Subject", "<b>hi</b>", "hi")
    assert len(_FakeSMTP.instances) == 1
    s = _FakeSMTP.instances[0]
    assert s.host == "smtp.example.com" and s.port == 587
    assert s.tls is True                                   # STARTTLS negotiated
    assert s.logged_in == ("me@example.com", "secret")
    assert s.sent["to"] == ["one@x.com", "two@y.com"]      # both recipients
    assert "Subject" in s.sent["body"]
