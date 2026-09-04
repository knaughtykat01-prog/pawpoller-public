"""Posting to X reports what X said, not what we guessed (4.3.4).

A tester connected X successfully — polling found 510 tweets and recorded a
follower count — and then every post from the composer failed with:

    X rejected the post — the cookie session may be expired, or the
    CreateTweet query id/features need refreshing (check logs)

Neither was true. The log held X's actual answer:

    TW: CreateTweet returned no rest_id: {'data': {'create_tweet': {'tweet_results': {}}}}

HTTP 200, no ``errors`` array, and an empty ``tweet_results`` — which is not
what a rotated query id looks like (404) nor a bad feature set (400 with the
offending flag named). Three distinct failures arrived at one ``return None``
and left as one sentence naming two causes, neither of them the likely one.

The same shape as §67: a check that can only fail one way names the wrong
cause with total confidence, and sends the user to replace credentials that
work.
"""
from __future__ import annotations

import pytest

from clients.tw.client import (_create_tweet_reason, _error_codes, _graphql_errors,
                               _http_reason)


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


# The payload from the tester's log, verbatim.
EMPTY_RESULTS = {"data": {"create_tweet": {"tweet_results": {}}}}


class TestTheReportedCase:
    def test_an_empty_result_is_not_blamed_on_the_cookies(self):
        reason = _create_tweet_reason(EMPTY_RESULTS, {})
        assert "duplicate" in reason.lower()
        assert "limit" in reason.lower()
        for wrong in ("query id", "expired", "refresh"):
            assert wrong not in reason.lower(), (
                f"{wrong!r} is the guess that cost a week of re-copying cookies")

    def test_it_tells_the_user_how_to_tell_the_two_apart(self):
        """A duplicate and a limited account need different actions, and the
        user can distinguish them in ten seconds from x.com."""
        reason = _create_tweet_reason(EMPTY_RESULTS, {})
        assert "x.com" in reason.lower()


# The second attempt in the same log, verbatim. X gave its reason here.
CODE_226 = {
    "data": {},
    "errors": [{
        "code": 226,
        "extensions": {"code": 226, "kind": "Permissions",
                       "name": "AuthorizationError", "source": "Client"},
        "kind": "Permissions",
        "locations": [{"column": 3, "line": 18}],
        "message": ("Authorization: This request looks like it might be automated. To protect "
                    "our users from spam and other malicious activity, we can't complete this "
                    "action right now."),
    }],
}


class TestErrorCodesBeatProse:
    """⚠ 4.3.5 fixes a misclassification shipped in 4.3.4 the same day.

    Code 226's message *begins with the word "Authorization"*, so 4.3.4's
    substring test for "auth" filed X's anti-automation block as a credentials
    failure — telling a user to replace cookies that were, in the same log,
    polling 510 tweets successfully. The exact mistake §67 and §68 are about,
    made while fixing it. Numeric codes are checked first because they do not
    move when X rewords the prose.
    """

    def test_the_reported_226_is_not_called_a_credentials_problem(self):
        reason = _create_tweet_reason(CODE_226, {})
        assert "226" in reason
        assert "automated" in reason.lower()
        for wrong in ("credential", "cookie", "expired", "reconnect"):
            assert wrong not in reason.lower(), (
                f"{wrong!r}: 226 is a permissions block, not a login failure — "
                "the same session was polling fine")

    def test_it_says_outright_that_the_login_is_fine(self):
        assert "not a login problem" in _create_tweet_reason(CODE_226, {}).lower()

    @pytest.mark.parametrize("code, must_say", [
        (226, "automated"), (187, "duplicate"), (88, "rate-limiting"),
        (326, "locked"), (64, "suspended"), (186, "too long"), (170, "empty"),
    ])
    def test_each_known_code_gets_its_own_answer(self, code, must_say):
        payload = {"errors": [{"code": code, "message": "whatever X says today"}]}
        assert must_say in _create_tweet_reason(payload, {}).lower()

    def test_a_code_only_in_extensions_is_still_found(self):
        payload = {"errors": [{"extensions": {"code": 187}, "message": "x"}]}
        assert "duplicate" in _create_tweet_reason(payload, {}).lower()

    def test_codes_are_read_from_both_places_without_duplicating(self):
        assert _error_codes(CODE_226) == [226]

    def test_a_malformed_code_is_ignored_not_crashed_on(self):
        assert _error_codes({"errors": [{"code": "eh"}, {"code": None}, "nope"]}) == []

    def test_an_unknown_code_falls_through_to_xs_own_words(self):
        payload = {"errors": [{"code": 99999, "message": "Something new"}]}
        assert "Something new" in _create_tweet_reason(payload, {})


class TestWriteHeaders:
    """Reads work and writes get 226, so the write path now sends the headers
    X's own client sends on an authenticated write. ⚠ Applied ONLY to writes:
    polling works today and must not be risked on a guess."""

    def test_the_write_path_sends_them_and_polling_does_not(self):
        src = open("clients/tw/client.py", encoding="utf-8").read()
        assert '"x-twitter-auth-type": "OAuth2Session"' in src
        # 4.3.7 added set_media_alt (alt text on an uploaded image): a write.
        # 4.6.2: the three writes build their headers through _write_headers(),
        # which spreads _WRITE_HEADERS and adds the per-request transaction id.
        assert src.count("await self._write_headers(") == 3, (
            "create_tweet, upload_media and set_media_alt — the write path only")
        assert "headers = dict(_WRITE_HEADERS)" in src
        i = src.index("async def _get_json")
        assert "_WRITE_HEADERS" not in src[i:i + 1500], "reads must be left alone"

    def test_the_comment_admits_it_may_not_be_the_cure(self):
        """x-client-transaction-id is computed per request and not reproduced;
        that may be what 226 is really about."""
        src = open("clients/tw/client.py", encoding="utf-8").read()
        i = src.index("_WRITE_HEADERS = {")
        assert "x-client-transaction-id" in src[max(0, i - 900):i]


class TestXSaidWhy:
    def test_a_feature_flag_error_is_ours_to_fix_and_says_so(self):
        payload = {"errors": [{"message": "The following features cannot be null: rweb_video_x"}]}
        reason = _create_tweet_reason(payload, {})
        assert "feature" in reason.lower() and "PawPoller needs updating" in reason
        assert "rweb_video_x" in reason, "name the flag so it can be fixed"

    def test_an_auth_error_points_at_the_credentials(self):
        reason = _create_tweet_reason({"errors": [{"message": "Unauthorized"}]}, {})
        assert "credentials" in reason.lower() and "Unauthorized" in reason

    def test_any_other_graphql_error_is_quoted_verbatim(self):
        reason = _create_tweet_reason({"errors": [{"message": "Tweet needs to be a bit shorter."}]}, {})
        assert "Tweet needs to be a bit shorter." in reason

    def test_a_blocked_write_reports_xs_reason_code(self):
        reason = _create_tweet_reason({}, {"reason": "BounceDeleted"})
        assert "BounceDeleted" in reason

    def test_errors_outrank_a_bare_empty_result(self):
        payload = dict(EMPTY_RESULTS, errors=[{"message": "Rate limit exceeded"}])
        assert "Rate limit exceeded" in _create_tweet_reason(payload, {})

    def test_malformed_error_entries_do_not_crash(self):
        assert _graphql_errors({"errors": [None, {}, {"message": ""}]}) == []
        assert _create_tweet_reason({"errors": []}, {})   # falls through, still a sentence


class TestHttpReasons:
    @pytest.mark.parametrize("code, must_say", [
        (429, "rate-limiting"),
        (401, "expired"),
        (403, "expired"),
        (404, "rotated"),
    ])
    def test_each_status_gets_its_own_diagnosis(self, code, must_say):
        assert must_say in _http_reason("CreateTweet", _Resp(code)).lower()

    def test_429_connects_it_to_polling(self):
        """The tester's log showed a 429 during a 510-tweet poll minutes before
        the failed post — same session, same budget."""
        assert "poll" in _http_reason("CreateTweet", _Resp(429)).lower()

    def test_an_unknown_status_still_quotes_x(self):
        r = _Resp(500, {"errors": [{"message": "Internal error"}]})
        out = _http_reason("CreateTweet", r)
        assert "500" in out and "Internal error" in out

    def test_a_non_json_body_does_not_crash(self):
        assert "502" in _http_reason("CreateTweet", _Resp(502, None, "<html>"))


class TestWiring:
    def test_the_client_carries_the_reason_for_the_poster(self):
        src = open("clients/tw/client.py", encoding="utf-8").read()
        assert "self.last_error" in src
        i = src.index("async def create_tweet")
        block = src[i:i + 3000]
        assert "self.last_error = _create_tweet_reason(" in block
        assert 'self.last_error = ""' in block, "cleared per call, so a stale reason can't leak"

    def test_the_whole_payload_is_logged_not_the_first_300_chars(self):
        """Truncation is why the first report could not be diagnosed from the
        log alone."""
        src = open("clients/tw/client.py", encoding="utf-8").read()
        i = src.index("async def create_tweet")
        block = src[i:i + 3000]
        assert "CreateTweet created nothing: %s" in block
        assert "str(data)[:300]" not in block

    def test_the_poster_shows_it_instead_of_the_old_guess(self):
        src = open("posting/post_publisher.py", encoding="utf-8").read()
        assert "client.last_error" in src
        assert "query id/features need refreshing" not in src, "the guess is still being shown"

    def test_a_failed_image_upload_also_reports_the_status(self):
        src = open("clients/tw/client.py", encoding="utf-8").read()
        i = src.index("async def upload_media")
        assert "_http_reason(" in src[i:i + 2000]
