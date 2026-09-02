"""Credentials must not reach a log handler (2.193.1, redesigned in 2.193.2).

Found on the production VM: `docker compose logs pawpoller` printed live access
tokens at INFO, because httpx logs the full request URL and several platforms
carry credentials in the query string or path.

Every token literal below is **fully synthetic** — deliberately not built by
keeping a real prefix and swapping the tail, because a real prefix plus a real
bot id is still real information committed to a repo for no benefit. The URL
STRUCTURE is what these tests need; the bytes are invented.

Three of these tests exist because 2.193.1 shipped and did NOT work in
production, despite passing its own tests:

* ``test_record_factory_redacts_with_no_handler_filters`` — the original relied
  on ``handler.addFilter``. Something in the running app meant those filters were
  not applied to its records. The factory does not depend on handler wiring.
* ``test_concurrent_logging_does_not_deadlock`` — the original took its own lock
  and then ``config._settings_lock``, which deadlocks against a thread holding
  them in the opposite order. It hung a local reproduction.
* ``test_scrub_never_touches_config`` — the root cause of both: logging must
  never call into config. Secrets are pushed in, not pulled.
"""
import logging
import threading

import config
import log_redaction
from log_redaction import MASK, SecretRedactingFilter


def _formatted(msg, *args):
    """Build a record through the real factory, then format it as a handler would."""
    factory = logging.getLogRecordFactory()
    rec = factory("httpx", logging.INFO, __file__, 1, msg, args or None, None)
    return logging.Formatter("%(message)s").format(rec)


def _via_filter(msg, *args):
    """Same, but exercising the belt-and-braces handler filter explicitly."""
    rec = logging.LogRecord("httpx", logging.INFO, __file__, 1, msg, args or None, None)
    assert SecretRedactingFilter().filter(rec) is True     # never drops a record
    return logging.Formatter("%(message)s").format(rec)


# ── pattern layer ─────────────────────────────────────────────

def test_query_string_access_token_is_masked_from_args():
    """httpx passes the URL as an ARG, not in msg — the easy thing to get wrong."""
    url = ("https://graph.instagram.com/refresh_access_token"
           "?grant_type=ig_refresh_token&access_token=SYNTHETIC0igtoken0value0aaaa")
    out = _via_filter('HTTP Request: %s %s "%s"', "GET", url, "200 OK")
    assert "SYNTHETIC0igtoken0value0aaaa" not in out
    assert MASK in out
    # Non-secret context survives, or the log is useless.
    assert "graph.instagram.com" in out
    assert "grant_type=ig_refresh_token" in out
    assert "200 OK" in out


def test_telegram_bot_token_in_url_path_is_masked():
    """Telegram puts the token in the PATH, so query-param matching misses it."""
    url = "https://api.telegram.org/bot1111100000:SYNTHETICbottoken0value/getUpdates?offset=1"
    out = _via_filter("HTTP Request: GET %s", url)
    assert "1111100000:SYNTHETICbottoken0value" not in out
    assert "api.telegram.org" in out
    assert "/getUpdates" in out


def test_tumblr_api_key_and_threads_token_masked():
    out = _via_filter("HTTP Request: GET %s",
                      "https://api.tumblr.com/v2/blog/x/info?api_key=SYNTHETIC0tumblr0key0")
    assert "SYNTHETIC0tumblr0key0" not in out
    out = _via_filter("HTTP Request: GET %s",
                      "https://graph.threads.net/refresh_access_token"
                      "?grant_type=th_refresh_token&access_token=SYNTHETIC0threads0token0")
    assert "SYNTHETIC0threads0token0" not in out


def test_bearer_and_token_auth_headers_masked():
    assert "abcdef1234567890xyz" not in _via_filter(
        "sending Authorization: Bearer abcdef1234567890xyz")
    # Itaku uses the DRF "Token <t>" scheme.
    assert "ik_tok_abcdef123456" not in _via_filter(
        "header Authorization: Token ik_tok_abcdef123456")


def test_cookie_header_masked():
    assert "abcdef1234567890" not in _via_filter("Cookie: sessionid=abcdef1234567890")


def test_ordinary_lines_are_left_alone():
    """Over-redaction would make the logs worthless — check the common cases."""
    for line in (
        "session check complete: {'ao3': 'valid', 'sf': 'valid'}",
        "SqW: Successfully logged in as KnaughtyKat",
        "Skipping startup poll - last cycle was recent, next in 100 min",
        "HTTP Request: GET https://e621.net/favorites.json?limit=1",
        "Submission 12345: scraping comments (count=3, force=False)",
    ):
        assert _via_filter("%s", line) == line, line


# ── the 2.193.1 failure modes ─────────────────────────────────

def test_record_factory_redacts_with_no_handler_filters():
    """The reason 2.193.1 failed in prod: it depended on handler filters.

    Here the handler has NO filter at all, and the line must still come out
    masked — the factory scrubs at record creation, before any handler exists.
    """
    import io
    log_redaction.install()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    assert not handler.filters                     # deliberately unfiltered
    lg = logging.getLogger("factory-probe")
    lg.propagate = False
    lg.setLevel(logging.INFO)
    lg.addHandler(handler)
    try:
        lg.info("HTTP Request: GET %s",
                "https://api.telegram.org/bot2222200000:SYNTHETICfactory0tok/getUpdates")
        out = buf.getvalue()
    finally:
        lg.removeHandler(handler)
    assert "SYNTHETICfactory0tok" not in out
    assert MASK in out


def test_concurrent_logging_does_not_deadlock():
    """2.193.1 took its own lock then config's; reversed order deadlocked.

    Scrubbing is now pure, so heavy concurrent logging must simply complete.
    """
    log_redaction.set_secrets(["SYNTHETICconcurrentSECRET1"])
    errors = []

    def hammer():
        try:
            for _ in range(200):
                _via_filter("HTTP Request: GET %s",
                            "https://x/y?access_token=SYNTHETICconcurrentSECRET1")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "logging deadlocked under load"
    assert not errors, errors


def test_scrub_never_touches_config(monkeypatch):
    """The root-cause invariant: log-time scrubbing does no settings I/O.

    A settings read per log record meant file I/O plus a Fernet decrypt on every
    line, and it is what created the deadlock. If this ever regresses, both
    return.
    """
    called = []
    monkeypatch.setattr(config, "get_settings",
                        lambda: called.append(1) or {})
    log_redaction.scrub("https://x/y?access_token=SYNTHETIC0nocfg0value0")
    _via_filter("HTTP Request: GET %s", "https://x/y?api_key=SYNTHETIC0nocfg0value0")
    assert called == [], "scrubbing called config.get_settings()"


# ── value layer (pushed, not pulled) ──────────────────────────

def test_saving_settings_pushes_secrets_to_the_scrubber():
    """config.save_settings feeds the scrubber, so a token is unfindable in any
    shape — an exception repr, a response body, a future f-string."""
    secret = "th_live_SECRET_VALUE_abcdefgh1234"
    config.save_settings({"thr_access_token": secret})
    out = _via_filter("unexpected reply from Threads: %s",
                      "{'error': 'bad token " + secret + " rejected'}")
    assert secret not in out
    assert MASK in out


def test_identity_fields_stay_readable():
    """Usernames live in CREDENTIAL_FIELDS but are not secrets; masking them
    would turn 'Logging in as KnaughtyKat' into noise."""
    config.save_settings({"username": "KnaughtyKatLongEnough",
                          "password": "sup3rsecret_password_value"})
    out = _via_filter("IB: logging in as %s", "KnaughtyKatLongEnough")
    assert "KnaughtyKatLongEnough" in out
    assert "sup3rsecret_password_value" not in _via_filter(
        "creds=%s", "sup3rsecret_password_value")


def test_short_values_never_redacted():
    """A short secret would collide with ordinary words and mangle real lines."""
    log_redaction.set_secrets(["abc"])
    assert _via_filter("%s", "abc def abc") == "abc def abc"


def test_secrets_from_settings_skips_identity_and_short_values():
    picked = log_redaction.secrets_from_settings(
        {"thr_access_token": "SYNTHETIClongsecret1", "fa_username": "someusername",
         "tw_ct0": "short", "display_timezone": "Australia/Sydney"},
        config.is_credential_key)
    assert picked == ["SYNTHETIClongsecret1"]


# ── robustness ────────────────────────────────────────────────

def test_filter_never_raises_or_drops_on_hostile_input():
    filt = SecretRedactingFilter()
    for msg, args in (
        (None, None), (12345, None), (b"bytes", None),
        ("%s", (object(),)), ("no args but %s", None),
    ):
        rec = logging.LogRecord("x", logging.INFO, __file__, 1, msg, args, None)
        assert filt.filter(rec) is True


def test_set_secrets_survives_garbage():
    """Bad input must not wipe protection or raise into a caller."""
    log_redaction.set_secrets(["SYNTHETICkeepme0value0"])
    log_redaction.set_secrets([None, 123, object(), "ok_but_short"])
    # Non-strings dropped, short dropped; no exception.
    out = _via_filter("%s", "https://x?token=SYNTHETICpatternstillworks")
    assert "SYNTHETICpatternstillworks" not in out


def test_url_logging_is_suppressed_so_no_record_exists_to_leak():
    """The only layer that cannot fail in transformation: no record at all.

    Scrubbing was verifiably installed and verifiably correct in isolation, yet
    verifiably ineffective in production twice (2.193.1 handler filters, 2.193.2
    record factory). httpx's INFO request line is where every observed leak came
    from, so its level is raised and the record is never created.
    """
    import io
    log_redaction.install()
    assert logging.getLogger("httpx").level == logging.WARNING

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("httpx")
    lg.propagate = False
    lg.addHandler(handler)
    try:
        lg.info("HTTP Request: GET %s",
                "https://api.telegram.org/bot3333300000:SYNTHETICneverlogged/x")
        assert buf.getvalue() == "", "httpx INFO still emitted a record"
        # A genuine httpx problem must still surface.
        lg.warning("connection pool exhausted")
        assert "connection pool exhausted" in buf.getvalue()
    finally:
        lg.removeHandler(handler)
        lg.propagate = True


def test_request_url_logging_can_be_re_enabled(monkeypatch):
    """Escape hatch for debugging; the scrubbing layers still apply."""
    monkeypatch.setenv("PAWPOLLER_LOG_REQUEST_URLS", "1")
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    log_redaction.silence_url_loggers()
    assert logging.getLogger("httpx").level != logging.WARNING
    assert log_redaction._SILENCED is False
    monkeypatch.delenv("PAWPOLLER_LOG_REQUEST_URLS")
    log_redaction.silence_url_loggers()          # restore for other tests
    assert log_redaction._SILENCED is True


def test_install_is_idempotent():
    log_redaction.install()
    log_redaction.install()
    root = logging.getLogger()
    handler = logging.StreamHandler()
    root.addHandler(handler)
    try:
        log_redaction.install()
        log_redaction.install()
        n = sum(1 for f in handler.filters if isinstance(f, SecretRedactingFilter))
        assert n == 1
    finally:
        root.removeHandler(handler)
