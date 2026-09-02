"""A Telegram channel check must say WHICH channel it reached.

From a live failure on 2026-09-03. A user created a PRIVATE channel titled
"Testing", made the bot an admin with "Post Messages" ticked, and typed
``Testing`` into PawPoller. Every part of that was correct, and it failed.

The normaliser prefixes a bare word to ``@Testing``. That username belongs to
an unrelated public channel (id ``-1001063430776``), and ``getChat`` reads any
public channel — so the check PASSED against a stranger's channel and reported
success. The post then failed, and the error blamed the bot's admin rights: the
one thing the user had provably just done.

Two defects, and the second is the dangerous one:

1. ``_ok()`` discarded Telegram's ``description``, so the real reason ("not
   enough rights to send text messages to the chat") was neither shown nor
   logged, and the code guessed out loud instead.
2. A check that confirms the wrong target is worse than one that fails. The
   fix is not to stop prefixing — bare words are legitimate for public channels
   — it is to always report the identity reached, so a wrong one is visible.

A private channel has NO username; it is reachable only by its numeric -100…
id, which Telegram's UI never shows. ``t.me/+hash`` is an invite link, not a
handle, and bots cannot join by invite at all.
"""
from __future__ import annotations

import pytest

from clients.tg.client import TgClient


class TestChannelNormalisation:
    @pytest.mark.parametrize("given,expected", [
        ("-1003908367637", "-1003908367637"),   # numeric id passes through
        ("@mychan", "@mychan"),                 # explicit handle untouched
        ("mychan", "@mychan"),                  # bare word → public username
        ("https://t.me/mychan", "@mychan"),     # public link → username
    ])
    def test_accepted_forms(self, given, expected):
        assert TgClient("tok", given).channel == expected

    @pytest.mark.parametrize("link", [
        "https://t.me/+UwCuJ6BRlJ5kZmE1",
        "https://t.me/joinchat/AbCdEf",
    ])
    def test_private_invite_links_are_refused_by_name(self, link):
        """Splitting these on "/" produced "@+UwCu…", a handle that cannot
        exist. Bots also cannot join by invite link — no Bot API method does
        it — so this can never be made to work and should say so."""
        with pytest.raises(ValueError) as e:
            TgClient("tok", link)
        msg = str(e.value).lower()
        assert "invite" in msg
        assert "-100" in msg, "the error must point at what DOES work"

    def test_a_bare_title_still_becomes_a_public_handle(self):
        """Deliberately unchanged: bare words are correct for public channels.

        The defect was never the prefixing — it was that nothing reported where
        you landed. Pinned so the risky-looking behaviour is not 'fixed' by
        someone who has not read why it stays.
        """
        assert TgClient("tok", "Testing").channel == "@Testing"


class TestFailureReporting:
    def test_ok_records_telegrams_own_reason(self):
        c = TgClient("tok", "@chan")
        assert c._ok({"ok": False, "error_code": 400,
                      "description": "Bad Request: not enough rights"}) is None
        assert "not enough rights" in c.last_error, (
            "Telegram's description must be kept — guessing at the cause is how "
            "a user gets sent to re-check the one thing that was already correct"
        )

    def test_ok_clears_the_reason_on_success(self):
        c = TgClient("tok", "@chan")
        c.last_error = "stale"
        assert c._ok({"ok": True, "result": {"message_id": 1}}) == {"message_id": 1}
        assert c.last_error == ""

    def test_a_malformed_response_still_yields_a_reason(self):
        c = TgClient("tok", "@chan")
        assert c._ok(None) is None
        assert c.last_error, "must never leave the caller with nothing to report"


class TestResolvedIdentity:
    def test_client_exposes_where_a_handle_resolved(self):
        """validate() records the chat so callers can name it. Without this the
        only honest thing a success message could say was 'it worked', which is
        exactly what it said while pointing at someone else's channel."""
        c = TgClient("tok", "@chan")
        assert hasattr(c, "resolved_chat")
        assert c.resolved_chat == {}
