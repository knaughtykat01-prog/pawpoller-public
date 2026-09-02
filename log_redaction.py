"""Strip credentials out of log records before they reach any handler.

Found on the production VM: `docker compose logs pawpoller` printed **live access
tokens** at INFO. Nothing was logging them deliberately — httpx logs the complete
request URL for every call, and several platforms carry the secret in the URL::

    HTTP Request: GET https://graph.instagram.com/refresh_access_token?...&access_token=<live token>
    HTTP Request: GET https://api.telegram.org/bot<bot id>:<live token>/getUpdates

Anything with log access read working credentials: the rotating files under
LOGS_DIR, `docker logs`, and the dashboard's own Logs view.

Scrubbing happens in a **LogRecordFactory**, not a handler filter (2.193.2).
The 2.193.1 attempt used ``handler.addFilter`` and, despite redacting correctly
in every isolated test *including inside the production container*, did nothing
for the running app. It also called ``config.get_settings()`` from inside the
filter, which was wrong twice over: a cache guard bug (``if self._values`` is
False for an empty tuple) meant a settings read — file I/O plus Fernet decrypt —
on **every log record**, and taking the filter's lock and then
``config._settings_lock`` while another thread could hold them in the opposite
order is a textbook deadlock (it hung a local reproduction).

So the design is now inverted and much duller:

* **Logging never calls into config.** ``config`` *pushes* its secret values here
  via ``set_secrets()`` whenever settings are loaded or saved. At log time this
  module only reads a module-level tuple — no I/O, no locks, no recursion risk,
  nothing that can block or raise.
* **A record factory sees every record**, however handlers are later
  reconfigured (uvicorn calls ``dictConfig``, which is exactly the kind of thing
  that makes handler-attached state unreliable). Handler filters are still
  attached as a second line of defence, but nothing depends on them.

Two redaction layers, because either alone is insufficient:

1. **Pattern** — sensitive query/form parameters, the Telegram bot path token,
   ``Bearer``/``Token`` schemes, cookie headers. Catches credentials belonging to
   code not yet written, but only in shapes it recognises.
2. **Value** — the real secret values pushed from settings, so a token stays
   unfindable when it appears in no recognisable shape at all: an exception repr,
   a response body, an f-string added in a future change.

Identity fields stay readable: usernames and handles live in
``CREDENTIAL_FIELDS`` but are not secrets, and masking them would turn
"logging in as KnaughtyKat" into noise (see ``_IDENTITY_HINTS``).
"""
from __future__ import annotations

import logging
import re

MASK = "[REDACTED]"

# Query-string / form parameters whose value is a credential.
_PARAM_NAMES = (
    "access_token", "refresh_token", "auth_token", "id_token", "api_key",
    "apikey", "client_secret", "token", "key", "secret", "password", "passwd",
    "pwd", "sid", "session", "session_key", "signature", "sig", "code",
    "cookie", "auth", "credentials", "app_password",
)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # ?access_token=… / &api_key=… — value runs to the next delimiter.
    (re.compile(r"(?i)([?&;](?:" + "|".join(_PARAM_NAMES) + r")=)[^&;\s\"'\\]+"),
     r"\1" + MASK),
    # Telegram carries the bot token in the PATH: /bot<id>:<secret>/getUpdates
    (re.compile(r"(?i)(/bot)\d{5,}:[A-Za-z0-9_\-]{15,}"), r"\1" + MASK),
    # Authorization: Bearer <t> / Token <t>  (Itaku uses the Token scheme)
    (re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=\-]{12,}"),
     r"\1 " + MASK),
    # Header-ish "Authorization: …" / "Cookie: …" in a message body.
    (re.compile(r"(?i)\b(authorization|cookie|set-cookie|x-api-key)"
                r"(\s*[:=]\s*)[^\s,;]{8,}"), r"\1\2" + MASK),
]

# Credential keys that are IDENTITY, not secrets — keep them legible in logs.
_IDENTITY_HINTS = ("username", "identifier", "target_user", "display_name",
                   "instance_url", "own_handle", "_url", "user")

# Never redact a value shorter than this: short strings collide with ordinary
# log text and would mangle unrelated lines.
_MIN_VALUE_LEN = 8

# Secret values, longest first. Written only by set_secrets(); read on every
# record. A plain tuple assignment is atomic under the GIL, so no lock is needed
# and a log call can never block on this.
_SECRETS: tuple[str, ...] = ()


def set_secrets(values) -> int:
    """Replace the secret-value list. Called by ``config``, never by logging.

    Sorted longest-first so a token containing a shorter secret as a substring is
    masked whole instead of leaving a tail behind.
    """
    global _SECRETS
    try:
        vals = sorted({v for v in values
                       if isinstance(v, str) and len(v) >= _MIN_VALUE_LEN},
                      key=len, reverse=True)
        _SECRETS = tuple(vals)
    except Exception:  # noqa: BLE001 — never let bookkeeping break logging
        return len(_SECRETS)
    return len(_SECRETS)


def secrets_from_settings(settings: dict, is_credential_key) -> list[str]:
    """Pick the values worth redacting out of a settings dict.

    Takes ``is_credential_key`` as an argument rather than importing config, so
    this module has no dependency on config at all — that dependency is what made
    the 2.193.1 version deadlock-prone.
    """
    out = []
    for key, value in (settings or {}).items():
        if not isinstance(value, str) or len(value) < _MIN_VALUE_LEN:
            continue
        try:
            if not is_credential_key(key):
                continue
        except Exception:  # noqa: BLE001
            continue
        low = key.lower()
        if any(hint in low for hint in _IDENTITY_HINTS):
            continue
        out.append(value)
    return out


def scrub(text: str) -> str:
    """Mask credentials in *text*. Pure, no I/O, no locks — safe at log time."""
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, MASK)
    return text


def _scrub_record(record: logging.LogRecord) -> None:
    """Rewrite a record's msg and args in place.

    httpx logs ``('HTTP Request: %s %s "%s %d %s"', method, url, …)`` — the URL is
    an **argument**, so args must be rewritten too; scrubbing only ``msg`` would
    miss every leaking line.
    """
    try:
        if isinstance(record.msg, str) and record.msg:
            record.msg = scrub(record.msg)
        args = record.args
        if args:
            if isinstance(args, dict):
                record.args = {k: (scrub(v) if isinstance(v, str) else v)
                               for k, v in args.items()}
            elif isinstance(args, tuple):
                record.args = tuple(scrub(a) if isinstance(a, str) else a
                                    for a in args)
    except Exception:  # noqa: BLE001 — a scrub failure must never drop a log line
        pass


class SecretRedactingFilter(logging.Filter):
    """Second line of defence. The record factory is the primary mechanism."""

    def filter(self, record: logging.LogRecord) -> bool:
        _scrub_record(record)
        return True          # always emit; we only rewrite


_installed = False


def install(*_args, **_kwargs) -> None:
    """Install the record factory (primary) + handler filters (belt and braces).

    Idempotent. Safe to call from several entry points.
    """
    global _installed
    if not _installed:
        previous = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            _scrub_record(record)
            return record

        logging.setLogRecordFactory(factory)
        _installed = True

    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(SecretRedactingFilter())

    silence_url_loggers()

    # Announce it, and CANARY-TEST it in the same breath. This is not noise: it
    # is the only evidence, from inside the running process, that the control is
    # actually active. Two previous attempts (2.193.1, 2.193.2) were provably
    # installed — correct factory, filters on both handlers, patterns verified
    # against the real token shape, single process — and still emitted raw
    # tokens for the live app. The failure was never explained. So the control
    # now proves itself on every boot instead of being assumed.
    canary = ("https://api.telegram.org/bot1234500000:"
              "CANARYcanaryCANARYcanary")
    scrubbed = scrub(canary)
    logging.getLogger(__name__).warning(
        "log redaction ACTIVE — factory=%s handlers=%d secrets=%d "
        "url_loggers_silenced=%s canary=%s",
        getattr(logging.getLogRecordFactory(), "__qualname__", "?"),
        len(root.handlers), len(_SECRETS), _SILENCED, scrubbed)


# Loggers that emit full request URLs at INFO. httpx logs
# 'HTTP Request: %s %s "%s %d %s"' for EVERY call, and Threads/Instagram/Tumblr
# put the token in the query string while Telegram puts it in the path — so
# these lines are where every observed leak came from.
#
# Their level is raised so the record is never created. This is deliberately
# blunt, and it is the only layer that does not depend on the record surviving a
# transformation: no record, nothing to leak. It exists because scrubbing was
# demonstrably installed and demonstrably correct in isolation, yet demonstrably
# ineffective in production, twice — and a credential leak is not something to
# keep betting on a mechanism I could not verify end to end.
#
# Cost: you lose per-request INFO lines from httpx. Set
# PAWPOLLER_LOG_REQUEST_URLS=1 to keep them (they are then still scrubbed by the
# factory and filters, which is belt-and-braces rather than a guarantee).
_URL_LOGGERS = ("httpx", "httpcore", "httpx._client")
_SILENCED = False


def silence_url_loggers() -> None:
    global _SILENCED
    import os
    if os.environ.get("PAWPOLLER_LOG_REQUEST_URLS", "").strip() in ("1", "true", "yes"):
        _SILENCED = False
        return
    for name in _URL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    _SILENCED = True
