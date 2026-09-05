"""Tech Centre client (4.10.0) — technical errors reach the operator, with consent.

Spec: ``docs/specs/tech_centre.md``. The service side lives in its own repo
(``syncopates-techcentre``); this module is everything PawPoller does:

* **Capture** — a logging handler takes ERROR records that carry a traceback
  (or an explicit ``extra={"tech": {...}}`` marker), decides whether the
  problem is *technical* (ours to fix) or *user-fixable* (an expired cookie,
  no network, a full disk — theirs), and queues the technical ones.
* **Consent** — nothing leaves the machine until the user has said yes. New
  installs are asked in the first-run flow; existing installs are asked at
  their first technical error (the report is held as ``prompt`` for the UI).
* **Send** — a daemon thread posts the queue to the Tech Centre with backoff.
  The answer carries the issue's triage (``status``/``note``/``fixed_in``),
  cached per fingerprint so the app can say "known — fixed in 4.10.1", and
  ``user_error`` stops that fingerprint from ever being sent again.
* **Scrub** — tokens, cookies, emails, home directories, @handles, URL paths
  and every account handle in settings are removed before a report is even
  queued. The service scrubs again on receipt.

Everything here is deterministic; no report is inspected by anything but the
operator. No LLM, no vision (the PawPoller rule).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform as _platform
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger("techcentre")

APP = "pawpoller"
TECH_CENTRE_URL = os.environ.get("PAWPOLLER_TECH_CENTRE_URL", "https://tech.syncopates.app").rstrip("/")
MAX_PENDING = 50                 # queue cap on disk; oldest dropped
DEDUP_SECONDS = 3600             # one report per fingerprint per hour
SEND_INTERVAL = 20.0             # sender thread cadence
HTTP_TIMEOUT = 8.0
KEEP_LAST = 5                    # shown in Settings → Diagnostics
LIMITS = {"message": 500, "traceback": 4096, "log_tail": 6144, "note": 500, "where": 160, "error_class": 80}
KINDS = ("exception", "platform_response", "api_500", "frontend", "update", "test")
_ROOT = Path(__file__).resolve().parent

PENDING_PATH = config.DATA_DIR / "tech_pending.json"
STATE_PATH = config.DATA_DIR / "tech_state.json"

_lock = threading.RLock()
_wake = threading.Event()
_thread: threading.Thread | None = None
_handler: "TechHandler | None" = None


# ── consent ──────────────────────────────────────────────────────────────────

def consent(settings: dict | None = None) -> bool | None:
    """True = send, False = never, None = not asked yet."""
    s = settings if settings is not None else config.get_settings()
    v = s.get("tech_reports")
    if v is None or v == "":
        return None
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def set_consent(value: bool) -> None:
    config.save_settings({"tech_reports": bool(value)})
    with _lock:
        st = _state()
        st["prompt"] = None
        _save_state(st)
        if not value:
            _save_pending([])
    if value:
        _wake.set()


# ── classification ───────────────────────────────────────────────────────────

# Exception classes that mean "the user's machine or account", not our code.
_USER_CLASSES = {
    "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException",
    "NetworkError", "RemoteProtocolError", "ProxyError", "SSLError", "SSLCertVerificationError",
    "CertificateError", "gaierror", "ConnectionRefusedError", "ConnectionResetError", "ConnectionAbortedError",
    "BrokenPipeError", "TimeoutError", "socket.timeout", "PermissionError", "FileNotFoundError",
    "IsADirectoryError", "NotADirectoryError", "FileExistsError", "KeyboardInterrupt", "SystemExit",
    "CancelledError", "asyncio.CancelledError", "LoginRequired", "SessionExpired", "CredentialsMissing",
}
_USER_PATTERNS = re.compile(
    r"(?i)(cookie|expired|log ?in again|login required|not logged in|sign in|captcha|rate.?limit|too many requests"
    r"|unauthori[sz]ed|forbidden|invalid (token|credential|api key|password|session)|wrong password"
    r"|no space left|disk full|permission denied|access is denied|name resolution|getaddrinfo"
    r"|network is unreachable|connection (refused|reset|aborted)|timed? ?out|certificate verify"
    r"|no such file|file not found|is not a directory|read-only file system|quota exceeded)"
)
_USER_STATUSES = {401, 402, 403, 404, 407, 410, 429}


def classify(error_class: str, message: str, status: int | None = None) -> str:
    """``"technical"`` (ours) or ``"user"`` (theirs to fix)."""
    cls = (error_class or "").rsplit(".", 1)[-1]
    if cls in _USER_CLASSES:
        return "user"
    if status is not None:
        if status in _USER_STATUSES:
            return "user"
        if status >= 500:
            return "technical"
    if _USER_PATTERNS.search(message or ""):
        return "user"
    return "technical"


# ── scrub ────────────────────────────────────────────────────────────────────

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "<bot-token>"),
    (re.compile(r"(?i)\b(bearer|token|auth_token|ct0|access_token|refresh_token|api_key|apikey|password|passwd"
                r"|secret|cookie|session)(\s*[=:]\s*|\s+)[\"']?[A-Za-z0-9%._~+/=-]{8,}"), r"\1=<redacted>"),
    (re.compile(r"\b[A-Za-z0-9_-]{48,}\b"), "<redacted>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"(?i)([A-Z]:\\Users\\)[^\\\s]+"), r"\1<user>"),
    (re.compile(r"(/home/|/Users/)[^/\s]+"), r"\1<user>"),
    (re.compile(r"(?<![\w/])@[A-Za-z0-9_.]{2,}"), "@…"),
    (re.compile(r"(https?://[^/\s]+)/[^\s\"']*"), r"\1/…"),
]
_HANDLE_KEY = re.compile(r"(?i)(username|identifier|handle|author|screen_name|login|account_name|display_name)")
_handles_cache: tuple[float, list[str]] = (0.0, [])


def _handles(settings: dict | None = None) -> list[str]:
    """Every account handle the settings know about — never let one into a report."""
    global _handles_cache
    now = time.time()
    if settings is None and now - _handles_cache[0] < 60:
        return _handles_cache[1]
    s = settings if settings is not None else config.get_settings()
    out: list[str] = []
    for k, v in s.items():
        if isinstance(v, str) and len(v.strip()) >= 3 and _HANDLE_KEY.search(k) and "url" not in k.lower():
            out.append(v.strip())
    out.sort(key=len, reverse=True)
    if settings is None:
        _handles_cache = (now, out)
    return out


def scrub(text, limit: int | None = None, settings: dict | None = None) -> str:
    if text is None:
        return ""
    out = str(text)
    try:
        import log_redaction
        out = log_redaction.scrub(out)
    except Exception:
        pass
    for pat, rep in _PATTERNS:
        out = pat.sub(rep, out)
    for h in _handles(settings):
        if h in out:
            out = out.replace(h, "<handle>")
    if limit is not None and len(out) > limit:
        out = out[: limit - 1] + "…"
    return out


# ── fingerprint (identical to the service's, so the known-issue cache lines up) ──

_QUOTED = re.compile(r"(['\"]).*?\1")
_HEX = re.compile(r"\b(?:0x)?[0-9a-fA-F]{8,}\b")
_NUM = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def normalise(message: str) -> str:
    m = _QUOTED.sub("'…'", message or "")
    m = _HEX.sub("HEX", m)
    m = _NUM.sub("0", m)
    return _WS.sub(" ", m).strip().lower()[:300]


def fingerprint(kind: str, where: str, error_class: str, message: str) -> str:
    key = "|".join([APP, kind.strip().lower(), where.strip().lower(), error_class.strip(), normalise(message)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


# ── environment ──────────────────────────────────────────────────────────────

def runtime() -> str:
    try:
        from auth.browser_login import is_browser_login_available
        return "desktop" if is_browser_login_available() else "server"
    except Exception:
        return "desktop" if getattr(sys, "frozen", False) else "unknown"


def _os_string() -> str:
    try:
        return f"{_platform.system()} {_platform.release()} ({_platform.machine()})"[:80]
    except Exception:
        return "unknown"


def install_id() -> str:
    with _lock:
        st = _state()
        if not st.get("install_id"):
            st["install_id"] = str(uuid.uuid4())
            _save_state(st)
        return st["install_id"]


def where_from_tb(tb) -> str:
    """``relative/path.py:function`` of the innermost frame inside PawPoller."""
    try:
        frames = traceback.extract_tb(tb)
    except Exception:
        return ""
    for fr in reversed(frames):
        try:
            p = Path(fr.filename).resolve()
            rel = p.relative_to(_ROOT)
        except Exception:
            continue
        if rel.parts and rel.parts[0] in ("tests", ".plan"):
            continue
        return f"{rel.as_posix()}:{fr.name}"[: LIMITS["where"]]
    if frames:
        fr = frames[-1]
        return f"{Path(fr.filename).name}:{fr.name}"[: LIMITS["where"]]
    return ""


def log_tail(max_bytes: int = LIMITS["log_tail"]) -> str:
    p = config.LOGS_DIR / "app.log"
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            f.seek(max(0, size - max_bytes))
            raw = f.read(max_bytes)
        text = raw.decode("utf-8", errors="replace")
        if size > max_bytes:
            text = text.split("\n", 1)[-1]
        return scrub(text, LIMITS["log_tail"])
    except Exception:
        return ""


# ── reports ──────────────────────────────────────────────────────────────────

def build(kind: str, where: str, error_class: str, message: str, tb: str = "", platform: str = "",
          context: dict | None = None, note: str = "", log_tail_ok: bool = True) -> dict:
    kind = kind if kind in KINDS else "exception"
    where = scrub(where, LIMITS["where"])
    error_class = scrub(error_class, LIMITS["error_class"])
    message = scrub(message, LIMITS["message"])
    ctx = {}
    for k, v in (context or {}).items():
        if isinstance(v, (bool, int, float)) or v is None:
            ctx[str(k)[:32]] = v
        else:
            ctx[str(k)[:32]] = scrub(str(v), 80)
    return {
        "app": APP, "version": config.APP_VERSION, "runtime": runtime(), "os": _os_string(),
        "python": _platform.python_version(), "install_id": install_id(), "kind": kind, "where": where,
        "platform": (platform or "")[:16], "error_class": error_class, "message": message,
        "traceback": scrub(tb, LIMITS["traceback"]),
        "log_tail": log_tail() if (log_tail_ok and kind != "test") else "",
        "context": ctx, "note": scrub(note, LIMITS["note"]),
        "occurred_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fingerprint": fingerprint(kind, where, error_class, message),
    }


def _title(r: dict) -> str:
    head = (r.get("message") or "").strip().splitlines()[0][:90] if r.get("message") else ""
    t = ": ".join(p for p in (r.get("error_class", ""), head) if p) or "(no message)"
    return f"{t} — {r['where']}" if r.get("where") else t


# ── on-disk state ────────────────────────────────────────────────────────────

def _read_json(p: Path, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(p: Path, data) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception as e:
        logger.debug("tech centre state write failed: %s", e)


def _state() -> dict:
    st = _read_json(STATE_PATH, {})
    if not isinstance(st, dict):
        st = {}
    st.setdefault("install_id", "")
    st.setdefault("known", {})
    st.setdefault("last", [])
    st.setdefault("recent", {})
    st.setdefault("prompt", None)
    st.setdefault("backoff_until", 0.0)
    st.setdefault("failures", 0)
    return st


def _save_state(st: dict) -> None:
    _write_json(STATE_PATH, st)


def _load_pending() -> list:
    items = _read_json(PENDING_PATH, [])
    return items if isinstance(items, list) else []


def _save_pending(items: list) -> None:
    _write_json(PENDING_PATH, items)


def _remember(st: dict, r: dict, sent: bool, result: dict | None = None) -> None:
    entry = {"fingerprint": r["fingerprint"], "title": _title(r), "kind": r["kind"], "at": r["occurred_at"],
             "sent": sent}
    if result:
        entry.update({k: result.get(k, "") for k in ("status", "note", "fixed_in")})
    last = [e for e in st.get("last", []) if e.get("fingerprint") != r["fingerprint"]]
    last.insert(0, entry)
    st["last"] = last[:KEEP_LAST]


def known(fp: str) -> dict | None:
    with _lock:
        return _state()["known"].get(fp)


def _cache_known(st: dict, fp: str, result: dict) -> None:
    st["known"][fp] = {"status": result.get("status", ""), "note": result.get("note", ""),
                       "fixed_in": result.get("fixed_in", ""), "checked_at": time.time()}
    if len(st["known"]) > 200:
        for k in sorted(st["known"], key=lambda k: st["known"][k].get("checked_at", 0))[:50]:
            st["known"].pop(k, None)


# ── queue ────────────────────────────────────────────────────────────────────

def enqueue(report: dict) -> str:
    """Returns what happened: queued | deduped | prompt | prompt_pending | off | user_error | disabled."""
    if not TECH_CENTRE_URL:
        return "disabled"
    c = consent()
    if c is False:
        return "off"
    fp = report["fingerprint"]
    with _lock:
        st = _state()
        k = st["known"].get(fp)
        if k and k.get("status") == "user_error":
            _remember(st, report, sent=False, result=k)
            _save_state(st)
            return "user_error"
        if c is None:
            if st.get("prompt"):
                return "prompt_pending"
            st["prompt"] = report
            _save_state(st)
            return "prompt"
        now = time.time()
        recent = {f: t for f, t in st["recent"].items() if now - float(t) < DEDUP_SECONDS}
        if fp in recent:
            st["recent"] = recent
            _save_state(st)
            return "deduped"
        recent[fp] = now
        st["recent"] = recent
        _save_state(st)
        items = _load_pending()
        items.append(report)
        if len(items) > MAX_PENDING:
            items = items[-MAX_PENDING:]
        _save_pending(items)
    _wake.set()
    return "queued"


def report(kind: str, where: str, error_class: str, message: str, tb: str = "", platform: str = "",
           context: dict | None = None, note: str = "", status: int | None = None) -> str:
    """Classify, build and queue. Returns the enqueue outcome or ``"user"``."""
    if classify(error_class, message, status) != "technical":
        return "user"
    try:
        return enqueue(build(kind, where, error_class, message, tb, platform, context, note))
    except Exception as e:
        logger.debug("tech centre report failed: %s", e)
        return "error"


def report_exception(where: str, exc: BaseException, kind: str = "exception", platform: str = "",
                     context: dict | None = None) -> str:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return report(kind, where or where_from_tb(exc.__traceback__), type(exc).__name__, str(exc), tb, platform, context)


# ── sending ──────────────────────────────────────────────────────────────────

def wire_body(r: dict) -> dict:
    """What goes over the wire: the report minus the local-only fingerprint (the service computes its own)."""
    return {k: v for k, v in r.items() if k != "fingerprint"}


def _send(r: dict) -> dict:
    """POST one report. Returns ``{"ok": True, ...service answer}`` or ``{"ok": False, "status", "error"}``."""
    import httpx
    body = wire_body(r)
    try:
        resp = httpx.post(f"{TECH_CENTRE_URL}/api/v1/reports", json=body, timeout=HTTP_TIMEOUT,
                          headers={"X-Syncopates-App": APP, "User-Agent": f"PawPoller/{config.APP_VERSION}"})
    except Exception as e:
        return {"ok": False, "status": 0, "error": type(e).__name__}
    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            data = {}
        return {"ok": True, **{k: data.get(k, "") for k in ("issue_id", "fingerprint", "status", "note", "fixed_in", "new")}}
    return {"ok": False, "status": resp.status_code, "error": resp.text[:200]}


def send_now(r: dict) -> dict:
    """Synchronous send used by *Send just this one* and *Send a test report*."""
    res = _send(r)
    with _lock:
        st = _state()
        if res.get("ok"):
            _cache_known(st, r["fingerprint"], res)
        _remember(st, r, sent=bool(res.get("ok")), result=res if res.get("ok") else None)
        _save_state(st)
    return res


def flush(max_items: int = 10) -> int:
    """Send queued reports (consent permitting). Returns how many were sent."""
    if not TECH_CENTRE_URL or consent() is not True:
        return 0
    with _lock:
        st = _state()
        if time.time() < float(st.get("backoff_until") or 0):
            return 0
        items = _load_pending()
    sent = 0
    for r in list(items)[:max_items]:
        res = _send(r)
        with _lock:
            st = _state()
            if res.get("ok"):
                sent += 1
                items.remove(r)
                _cache_known(st, r["fingerprint"], res)
                _remember(st, r, sent=True, result=res)
                st["failures"] = 0
                _save_pending(items)
                _save_state(st)
                continue
            code = res.get("status", 0)
            if code in (400, 403, 413):          # the service will never take this one; drop it
                items.remove(r)
                _remember(st, r, sent=False)
                _save_pending(items)
                _save_state(st)
                logger.debug("tech centre refused a report (%s): %s", code, res.get("error"))
                continue
            failures = int(st.get("failures") or 0) + 1
            st["failures"] = failures
            st["backoff_until"] = time.time() + (600 if code == 429 else min(900, 60 * 2 ** (failures - 1)))
            _save_state(st)
            break
    return sent


def _loop() -> None:
    while True:
        _wake.wait(SEND_INTERVAL)
        _wake.clear()
        if os.environ.get("PAWPOLLER_TECH_CENTRE_THREAD", "1") == "0":
            continue                      # paused (the test-suite switch)
        try:
            flush()
        except Exception as e:
            logger.debug("tech centre flush failed: %s", e)


def start() -> None:
    global _thread
    if not TECH_CENTRE_URL or os.environ.get("PAWPOLLER_TECH_CENTRE_THREAD", "1") == "0":
        return
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="techcentre", daemon=True)
            _thread.start()


# ── capture ──────────────────────────────────────────────────────────────────

_SKIP_LOGGERS = ("techcentre", "httpx", "httpcore", "uvicorn.access")


def capture_record(record: logging.LogRecord) -> str | None:
    """Turn one ERROR log record into a report if it is technical. Returns the outcome or None."""
    exc = record.exc_info if record.exc_info and record.exc_info[0] is not None else None
    marker = getattr(record, "tech", None)
    if exc is None and not isinstance(marker, dict):
        return None
    marker = marker if isinstance(marker, dict) else {}
    msg = record.getMessage()
    if exc is not None:
        error_class = exc[0].__name__
        message = str(exc[1]) or msg
        if msg and message != msg:
            message = f"{msg} | {message}"
        tb = "".join(traceback.format_exception(*exc))
        where = marker.get("where") or where_from_tb(exc[2]) or record.name
    else:
        error_class = marker.get("error_class") or "PlatformResponse"
        message = msg
        tb = ""
        where = marker.get("where") or f"{record.module}.{record.funcName}"
    kind = marker.get("kind") or ("platform_response" if marker.get("status") is not None else "exception")
    return report(kind, where, error_class, message, tb, platform=str(marker.get("platform") or ""),
                  context=marker.get("context"), status=marker.get("status"))


class TechHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self._tls = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._tls, "busy", False) or record.name.startswith(_SKIP_LOGGERS):
            return
        self._tls.busy = True
        try:
            capture_record(record)
        except Exception:
            pass
        finally:
            self._tls.busy = False


def install() -> None:
    """Attach the capture handler to the root logger (once) and start the sender."""
    global _handler
    if not TECH_CENTRE_URL:
        return
    root = logging.getLogger()
    if _handler is None or _handler not in root.handlers:
        _handler = TechHandler()
        root.addHandler(_handler)
    start()


# ── UI surface ───────────────────────────────────────────────────────────────

def _prompt_summary(p: dict | None) -> dict | None:
    if not p:
        return None
    return {"title": _title(p), "kind": p.get("kind"), "where": p.get("where"), "error_class": p.get("error_class"),
            "message": p.get("message"), "at": p.get("occurred_at"), "fingerprint": p.get("fingerprint"),
            "preview": {k: p.get(k, "") for k in ("version", "runtime", "os", "python", "platform", "context")}}


def status() -> dict:
    with _lock:
        st = _state()
        pending = len(_load_pending())
        last = []
        for e in st.get("last", []):
            k = st["known"].get(e.get("fingerprint"), {})
            last.append({**e, "status": e.get("status") or k.get("status", ""), "note": e.get("note") or k.get("note", ""),
                         "fixed_in": e.get("fixed_in") or k.get("fixed_in", "")})
        prompt = _prompt_summary(st.get("prompt"))
    c = consent()
    return {"enabled": bool(TECH_CENTRE_URL), "url": TECH_CENTRE_URL, "consent": c, "asked": c is not None,
            "install_id": install_id() if TECH_CENTRE_URL else "", "runtime": runtime(), "pending": pending,
            "last": last, "prompt": prompt}


def resolve_prompt(decision: str) -> dict:
    """*always* → consent on + send the held report; *once* → send it, ask again next time;
    *never* → consent off, drop it."""
    with _lock:
        st = _state()
        held = st.get("prompt")
    if decision == "always":
        set_consent(True)
        if held:
            outcome = enqueue(held)
            _wake.set()
            return {"consent": True, "outcome": outcome}
        return {"consent": True, "outcome": "none"}
    if decision == "never":
        set_consent(False)
        return {"consent": False, "outcome": "dropped"}
    if decision == "once":
        with _lock:
            st = _state()
            st["prompt"] = None
            _save_state(st)
        if not held:
            return {"consent": None, "outcome": "none"}
        res = send_now(held)
        return {"consent": None, "outcome": "sent" if res.get("ok") else "failed", "result": res}
    raise ValueError("decision must be always, once or never")


# One deliberately fake report per kind, so the operator can see every shape the Tech Centre
# will receive — and so a user can prove the pipeline works from Settings → Diagnostics.
# Content is synthetic on purpose (no log tail, no real paths); the note says so.
SAMPLE_NOTE = "Sample report sent on purpose from Settings → Diagnostics"
SAMPLES: dict[str, dict] = {
    "exception": dict(
        where="polling/fa_poller.py:poll_account", error_class="KeyError", message="'submissions' (sample)",
        tb=("Traceback (most recent call last):\n"
            "  File \"polling/fa_poller.py\", line 212, in poll_account\n"
            "    subs = payload[\"submissions\"]\n"
            "KeyError: 'submissions'\n"),
        platform="fa", context={"account_kind": "fa", "count": 3}),
    "platform_response": dict(
        where="posting/platforms/instagram.py:publish", error_class="PlatformResponse",
        message="Instagram answered 500: (#2) Service temporarily unavailable (sample)",
        platform="ig", context={"http_status": 500, "phase": "container"}),
    "api_500": dict(
        where="routes/api.py:list_works", error_class="TypeError",
        message="'NoneType' object is not subscriptable (sample)",
        tb=("Traceback (most recent call last):\n"
            "  File \"routes/api.py\", line 880, in list_works\n"
            "    total = rows[0][\"n\"]\n"
            "TypeError: 'NoneType' object is not subscriptable\n"),
        context={"route": "/api/works", "method": "GET"}),
    "frontend": dict(
        where="frontend:app.js:1234", error_class="TypeError",
        message="Cannot read properties of undefined (reading 'title') (sample)",
        tb="TypeError: Cannot read properties of undefined (reading 'title')\n    at renderLibrary (app.js:1234:17)\n",
        context={"page": "#/library", "browser": "Sample/1.0"}),
    "update": dict(
        where="update_gate.run", error_class="UpdateFailed",
        message="failed: download timed out after 3.0 s (sample)", context={"phase": "download"}),
    "test": dict(where="techcentre:test_report", error_class="TestReport",
                 message="Test report from Settings → Diagnostics"),
}


def sample_report(kind: str) -> dict:
    if kind not in SAMPLES:
        raise ValueError(f"unknown sample kind: {kind}")
    spec = dict(SAMPLES[kind])
    return build(kind, note="" if kind == "test" else SAMPLE_NOTE, log_tail_ok=False, **spec)


def send_sample(kind: str) -> dict:
    """Send one synthetic report of *kind* right now (an explicit user action, so no consent gate)."""
    return send_now(sample_report(kind))


def test_report() -> dict:
    return send_sample("test")


def refresh_known(fp: str) -> dict | None:
    import httpx
    if not TECH_CENTRE_URL or not re.fullmatch(r"[0-9a-f]{24}", fp or ""):
        return None
    try:
        resp = httpx.get(f"{TECH_CENTRE_URL}/api/v1/issues/{APP}/{fp}", timeout=HTTP_TIMEOUT)
    except Exception:
        return known(fp)
    if resp.status_code != 200:
        return known(fp)
    data = resp.json()
    with _lock:
        st = _state()
        _cache_known(st, fp, data)
        _save_state(st)
    return st["known"][fp]
