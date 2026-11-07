"""The connected desktop's local agent (SYNCTRUTH phases 1–2, 4.13.0).

A *connected* install is a window onto its server. This module is everything that still has
to happen on the desktop, and the write-behind queue that carries it to the server:

* **browser login** — the pywebview popup that harvests platform cookies only exists where
  there is a screen (`auth.browser_login`); the result is handed to the server's
  ``POST /api/settings/browser-login/result``.
* **file picking** — the native dialog returns a *local* path, which means nothing to the
  server; the agent uploads the file to ``POST /api/media/upload`` and hands the page the
  server-side path instead, so the Artwork hub's existing "create from path" flow works
  unchanged.
* **the queue** — ``data/agent_queue.json``: if the server is unreachable, results wait here
  and a daemon thread delivers them with backoff. Items are idempotent on the server
  (cookies overwrite; uploads are content-addressed by sha256).

Nothing here touches a database. The desktop in this mode has none. Design of record:
docs/specs/server_truth_sync.md.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

import config

logger = logging.getLogger(__name__)

QUEUE_PATH = config.DATA_DIR / "agent_queue.json"
MAX_QUEUE = 200
DRAIN_INTERVAL = 15.0
REACH_INTERVAL = 5.0
HTTP_TIMEOUT = 20.0
UPLOAD_TIMEOUT = 300.0
BACKOFF_STEPS = (30, 60, 120, 300, 900)


# ── mode selection (pure; main.py calls these) ───────────────────────────────

def decide_mode(argv: list[str], settings: dict) -> str:
    """``standalone`` or ``connected`` — CLI flags win, then the saved setup_mode."""
    if "--standalone" in argv:
        return "standalone"
    if "--connect" in argv:
        return "connected"
    return "connected" if settings.get("setup_mode") == config.SETUP_MODE_CONNECTED else "standalone"


def connect_target(argv: list[str], settings: dict) -> tuple[str, str]:
    """(server_url, api_key) for connected mode; ``--connect URL`` overrides the saved URL.

    072 (FR-SEC-006): the saved key was checked against the saved address and goes nowhere else,
    so an override that names a different address comes back with no key.
    """
    saved = (settings.get("posting_server_url") or "").rstrip("/")
    url = saved
    if "--connect" in argv:
        i = argv.index("--connect")
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            url = argv[i + 1].rstrip("/")
    key = settings.get("posting_server_api_key") or ""
    return url, (key if url == saved else "")


# ── migration: a paired desktop's old database ───────────────────────────────

def retire_local_database(data_dir: Path, now=None) -> list[str]:
    """First connected start after a migration: the old `pawpoller.db` (and its -wal/-shm) are
    renamed `pawpoller.db.retired-<date>` — never deleted, never opened again. Returns what moved."""
    import datetime as _dt
    stamp = (now or _dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    moved = []
    for suffix in ("", "-wal", "-shm"):
        src = Path(data_dir) / f"pawpoller.db{suffix}"
        if src.exists():
            dst = Path(data_dir) / f"pawpoller.db.retired-{stamp}{suffix}"
            try:
                os.replace(src, dst)
                moved.append(dst.name)
            except OSError as e:
                logger.warning("could not retire %s: %s", src.name, e)
    if moved:
        logger.info("Connected mode: retired the local database (%s)", ", ".join(moved))
    return moved


# ── the queue ────────────────────────────────────────────────────────────────

SECRET_KINDS = ("cookies", "helper")


def server_tag(server: str) -> str:
    """Which server a queued item is for, without writing the address into the file (059 rule)."""
    return hashlib.sha256(str(server).rstrip("/").encode()).hexdigest()[:16]


def _vault_cipher():
    from cryptography.fernet import Fernet
    return Fernet(config._get_vault_key())


class Queue:
    """Persistent FIFO of things to deliver. One JSON file, rewritten atomically.

    Items that carry a site sign-in (``cookies``, ``helper``) keep their payload encrypted
    with the install's vault key (spec 059): the file holds ``{"enc": …}``, never a cookie,
    code or server address. Plaintext items left by an older version are re-written on load.
    """

    def __init__(self, path: Path | None = None, cipher=None):
        self.path = Path(path) if path else QUEUE_PATH
        self._lock = threading.RLock()
        self._cipher = cipher
        self._items: list[dict] = self._load()
        if any(i.get("kind") in SECRET_KINDS and "enc" not in (i.get("payload") or {}) for i in self._items):
            for i in self._items:
                if i.get("kind") in SECRET_KINDS and "enc" not in (i.get("payload") or {}):
                    i["payload"] = self._seal(i.get("payload") or {})
            self._save()

    def _c(self):
        if self._cipher is None:
            self._cipher = _vault_cipher()
        return self._cipher

    def _seal(self, payload: dict) -> dict:
        return {"enc": self._c().encrypt(json.dumps(payload).encode()).decode()}

    def _open(self, item: dict) -> dict:
        out = dict(item)
        p = item.get("payload") or {}
        if "enc" in p:
            try:
                out["payload"] = json.loads(self._c().decrypt(p["enc"].encode()))
            except Exception:
                out["payload"] = {}          # a key that changed: the item can't be sent, drain drops it
        return out

    def _load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._items, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.debug("agent queue save failed: %s", e)

    def add(self, kind: str, payload: dict, dedup_key: str | None = None, server: str | None = None) -> str:
        with self._lock:
            if dedup_key:
                self._items = [i for i in self._items if i.get("dedup_key") != dedup_key]
            stored = self._seal(payload) if kind in SECRET_KINDS else payload
            item = {"id": uuid.uuid4().hex[:12], "kind": kind, "payload": stored, "dedup_key": dedup_key,
                    "created": time.time(), "attempts": 0, "next_at": 0.0, "last_error": ""}
            if server:
                item["server"] = server_tag(server)
            self._items.append(item)
            if len(self._items) > MAX_QUEUE:
                self._items = self._items[-MAX_QUEUE:]
            self._save()
            return item["id"]

    def pending(self) -> list[dict]:
        with self._lock:
            return [self._open(i) for i in self._items]

    def pending_count(self) -> int:
        with self._lock:
            return len(self._items)

    def due(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        with self._lock:
            return [self._open(i) for i in self._items if float(i.get("next_at") or 0) <= now]

    @staticmethod
    def _yours(item: dict, server: str | None) -> bool:
        """072: an agent only settles items still meant for its own server (a re-tagged one moved on)."""
        return server is None or item.get("server") in (None, server_tag(server))

    def remove(self, item_id: str, server: str | None = None) -> None:
        with self._lock:
            self._items = [i for i in self._items if not (i["id"] == item_id and self._yours(i, server))]
            self._save()

    def retag(self, ids, server: str) -> None:
        """072: the person chose to send these to their new server; they start over there."""
        ids, tag = set(ids), server_tag(server)
        with self._lock:
            for i in self._items:
                if i["id"] in ids:
                    i.update(server=tag, attempts=0, next_at=0.0, last_error="")
            self._save()

    def drop(self, ids) -> None:
        ids = set(ids)
        with self._lock:
            self._items = [i for i in self._items if i["id"] not in ids]
            self._save()

    def keep_only(self, server: str) -> None:
        """072: after a change, anything not meant for the new server was never shown to the person."""
        tag = server_tag(server)
        with self._lock:
            self._items = [i for i in self._items if i.get("server") == tag]
            self._save()

    def failed(self, item_id: str, error: str, now: float | None = None, server: str | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            for i in self._items:
                if i["id"] == item_id and self._yours(i, server):
                    i["attempts"] = int(i.get("attempts") or 0) + 1
                    step = BACKOFF_STEPS[min(i["attempts"] - 1, len(BACKOFF_STEPS) - 1)]
                    i["next_at"] = now + step
                    i["last_error"] = str(error)[:200]
            self._save()

    def clear(self) -> None:
        with self._lock:
            self._items = []
            self._save()


# ── the agent ────────────────────────────────────────────────────────────────

_PERMANENT = ("HTTP 400", "HTTP 404", "HTTP 409", "HTTP 413", "HTTP 415", "HTTP 422")


def permanent_failure(err: str) -> bool:
    """A refusal that a retry cannot fix. 401/403 (key) and 429/5xx (server) are NOT permanent."""
    return any((err or "").startswith(code) for code in _PERMANENT) or (err or "") == "file no longer exists"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Agent:
    def __init__(self, server_url: str, api_key: str, http=None, queue: Queue | None = None, login_fn=None):
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.queue = queue or Queue()
        self._http = http
        self._login_fn = login_fn            # injectable for tests; default = the pywebview popup
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_contact: float = 0.0
        self.last_error: str = ""

    # -- http ----------------------------------------------------------------
    def _client(self):
        if self._http is not None:
            return self._http
        import httpx
        return httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=False,
                            headers={"Authorization": f"Bearer {self.api_key}",
                                     "User-Agent": f"PawPoller-desktop/{config.APP_VERSION}"})

    def _close(self, client) -> None:
        if client is not self._http:
            client.close()

    def server_reachable(self) -> bool:
        c = self._client()
        try:
            r = c.get(f"{self.server_url}/api/health", timeout=3.0)
            ok = r.status_code == 200
            # An Instagram relay answers /api/health too but has no app behind it: the window
            # would open onto a bare {"detail":"Not Found"}. Treat it as not a PawPoller.
            if ok:
                try:
                    is_relay = (r.json() or {}).get("mode") == "relay"
                except Exception:
                    is_relay = False
                if is_relay:
                    self.last_error = "relay"
                    return False
            if ok:
                self.last_contact = time.time()
            return ok
        except Exception as e:
            self.last_error = type(e).__name__
            return False
        finally:
            self._close(c)

    def when_reachable(self, callback, interval: float = REACH_INTERVAL) -> None:
        def _poll():
            while not self._stop.is_set():
                if self.server_reachable():
                    try:
                        callback()
                    except Exception:   # was a return-in-finally that hid the error entirely
                        logger.exception("agent reach callback failed")
                    return
                self._stop.wait(interval)
        threading.Thread(target=_poll, daemon=True, name="agent-reach").start()

    # -- delivery ------------------------------------------------------------
    def send_cookies(self, payload: dict) -> tuple[bool, str]:
        c = self._client()
        try:
            r = c.post(f"{self.server_url}/api/settings/browser-login/result", json=payload)
            if r.status_code == 200:
                self.last_contact = time.time()
                return True, ""
            return False, f"HTTP {r.status_code}: {r.text[:400]}"
        except Exception as e:
            return False, type(e).__name__
        finally:
            self._close(c)

    def send_upload(self, payload: dict) -> tuple[bool, str, str]:
        """Upload ``payload['path']`` unless the server already has that sha256. Returns
        (ok, error, server_path)."""
        path = Path(payload["path"])
        if not path.is_file():
            return False, "file no longer exists", ""
        sha = payload.get("sha256") or sha256_of(path)
        c = self._client()
        try:
            r = c.get(f"{self.server_url}/api/media/exists/{sha}")
            if r.status_code == 200 and r.json().get("exists"):
                self.last_contact = time.time()
                return True, "", r.json().get("path", "")
            with open(path, "rb") as f:
                r = c.post(f"{self.server_url}/api/media/upload", timeout=UPLOAD_TIMEOUT,
                           data={"kind": payload.get("kind", "inbox"), "sha256": sha},
                           files={"file": (path.name, f, "application/octet-stream")})
            if r.status_code == 200:
                self.last_contact = time.time()
                return True, "", r.json().get("path", "")
            return False, f"HTTP {r.status_code}: {r.text[:120]}", ""
        except Exception as e:
            return False, type(e).__name__, ""
        finally:
            self._close(c)

    def drain(self, max_items: int = 10, now: float | None = None) -> int:
        sent = 0
        for item in [i for i in self.queue.due(now) if Queue._yours(i, self.server_url)][:max_items]:
            if self._stop.is_set():
                break                           # 072: a change of server stops this pass between items
            if item["kind"] == "cookies":
                ok, err = self.send_cookies(item["payload"])
            elif item["kind"] == "upload":
                ok, err, _ = self.send_upload(item["payload"])
            else:
                ok, err = False, f"unknown kind {item['kind']}"
                self.queue.remove(item["id"], server=self.server_url)
                continue
            if ok:
                self.queue.remove(item["id"], server=self.server_url)
                sent += 1
            elif permanent_failure(err):
                # The server understood and refused (bad account, oversize, malformed): retrying
                # can never succeed, so the item is dropped rather than kept for ever.
                logger.warning("Agent: dropping %s item after a permanent refusal: %s", item["kind"], err)
                self.queue.remove(item["id"], server=self.server_url)
                self.last_error = err
            else:
                self.queue.failed(item["id"], err, now, server=self.server_url)
                self.last_error = err
                break                           # the server is away (or the key is wrong); try later
        return sent

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(DRAIN_INTERVAL)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                if self.queue.pending_count():
                    self.drain()
            except Exception as e:
                logger.debug("agent drain failed: %s", e)

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="desktop-agent")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -- the two desktop-only jobs -------------------------------------------
    def login(self, platform: str, extra_fields: dict | None = None, account_id: int | None = None) -> dict:
        """Run the local browser-login popup, then hand the cookies to the server."""
        if self._stop.is_set():
            return {"ok": False, "message": MSG_CS_SWITCHED}
        fn = self._login_fn
        if fn is None:
            from auth.browser_login import login_via_browser
            # Keywords (059, L7): positionally `account_id` landed in the `timeout` slot, and the
            # popup also saved a copy on this desktop. The server is where the login belongs.
            fn = lambda p, e, a: login_via_browser(p, e, account_id=a, save=False)  # noqa: E731
        try:
            creds = fn(platform, extra_fields or {}, account_id)
        except Exception as e:
            return {"ok": False, "message": str(e)}
        if not creds:
            return {"ok": False, "message": "Login cancelled."}
        payload = {"platform": platform, "cookies": dict(creds), "account_id": account_id,
                   "extra_fields": dict(extra_fields or {})}
        if self._stop.is_set():                 # 072: the server changed while the popup was open
            return {"ok": False, "message": MSG_CS_SWITCHED}
        ok, err = self.send_cookies(payload)
        if ok:
            return {"ok": True, "message": "Connected — saved on your server."}
        if permanent_failure(err):
            # SEC4885 (2): a refusal (e.g. the wrong account) is final — say why, don't queue it as "unreachable".
            body = err.split(": ", 1)[1] if ": " in err else ""
            try:
                reason = str(json.loads(body).get("detail") or "")
            except (ValueError, AttributeError):
                reason = ""
            return {"ok": False, "message": reason or "Your server refused this sign-in, so it wasn't saved."}
        if self._stop.is_set():
            return {"ok": False, "message": MSG_CS_SWITCHED}
        self.queue.add("cookies", payload, dedup_key=f"cookies:{platform}:{account_id}", server=self.server_url)
        self._wake.set()
        return {"ok": True, "queued": True,
                "message": "Logged in. Your server is unreachable right now; the login will be sent when it is back."}

    def upload(self, path: str, kind: str = "inbox") -> dict:
        """Upload a local file now; if the server is away, queue it and say so."""
        p = Path(path)
        if self._stop.is_set():
            return {"ok": False, "message": MSG_CS_SWITCHED}
        if not p.is_file():
            return {"ok": False, "message": "File not found."}
        sha = sha256_of(p)
        payload = {"path": str(p), "kind": kind, "sha256": sha}
        ok, err, server_path = self.send_upload(payload)
        if ok:
            return {"ok": True, "path": server_path, "sha256": sha}
        if self._stop.is_set():
            return {"ok": False, "message": MSG_CS_SWITCHED}
        self.queue.add("upload", payload, dedup_key=f"upload:{sha}", server=self.server_url)
        self._wake.set()
        return {"ok": False, "queued": True, "sha256": sha,
                "message": "Your server is unreachable; the file will be uploaded when it is back."}

    def status(self) -> dict:
        return {"server_url": self.server_url, "pending": self.queue.pending_count(),
                "last_contact": self.last_contact, "last_error": self.last_error,
                "version": config.APP_VERSION}


# ── Sign in for a server (spec 059) ──────────────────────────────────────────
# The desktop half of the sign-in helper: a server's code → that site's own sign-in page in a
# PawPoller window → the result handed to the server. It never touches the Agent above, whose
# client carries this desktop's pairing key (H3): helper requests go out with no Authorization
# header, no redirects followed and certificates always checked, and waiting hand-overs live
# in their own encrypted queue with their own sender, which runs in standalone mode too.

HELPER_QUEUE_PATH = config.DATA_DIR / "helper_queue.json"
HELPER_TTL = 600
HOME_NETWORKS = tuple(__import__("ipaddress").ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10"))

MSG_TLS = "Your server needs https or Tailscale for this. Other ways to connect still work."
MSG_BAD_CODE = "That code isn't valid or has run out. Make a new one on your server."
MSG_AWAY = "Your server isn't answering. Try again when it is."
MSG_CANCELLED = "Sign-in cancelled."


def helper_client():
    import httpx
    return httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=False, verify=True,
                        headers={"User-Agent": f"PawPoller-desktop/{config.APP_VERSION}"})


def _origin(server: str) -> str:
    from urllib.parse import urlsplit
    p = urlsplit((server or "").strip())
    host = (p.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{p.scheme.lower()}://{host}" + (f":{p.port}" if p.port else "")


def _on_home_list(ip) -> bool:
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_unspecified:
        return False
    return any(ip in n for n in HOME_NETWORKS if n.version == ip.version)


def home_address(server: str, resolve=None) -> str | None:
    """For a plain-http *server*: the origin to connect to when its address is on this home
    network, else None. An IP literal must be on the explicit home list (not
    ``ipaddress.is_private``, which also says yes to 203.0.113.x and 0.0.0.0). A ``.local`` name
    is resolved ONCE, every address it gives must be on the list, and the returned origin uses
    that address, so a DNS change between this check and the send can't redirect it. Any other
    name is refused."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    p = urlsplit((server or "").strip())
    host = (p.hostname or "").lower()
    if p.scheme != "http" or not host:
        return None
    port = f":{p.port}" if p.port else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return _origin(server) if _on_home_list(ip) else None
    if not host.endswith(".local"):
        return None
    try:
        infos = (resolve or socket.getaddrinfo)(host, p.port or 80)
        addrs = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}
    except (OSError, ValueError):
        return None
    if not addrs or not all(_on_home_list(a) for a in addrs):
        return None
    a = sorted(addrs, key=lambda x: (x.version, str(x)))[0]
    return f"http://{a}{port}" if a.version == 4 else f"http://[{a}]{port}"


def clean_label(text, limit: int = 60) -> str:
    """Server text for a native dialog: Qt's message box guesses rich text, so markup
    characters, control, format and line/paragraph separators all go (L-r2-1)."""
    import unicodedata
    s = "".join(ch for ch in str(text or "") if ch not in "<>&"
                and unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp"))
    return " ".join(s.split())[:limit]


def ascii_host(server: str) -> str:
    from urllib.parse import urlsplit
    host = urlsplit((server or "").strip()).hostname or ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host.encode("ascii", "replace").decode()


_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)


def check_server(v) -> str | None:
    """A page's server address, checked before any request or dialog (074 R6): a ``str`` of at most 2048
    characters, http/https, a hostname that is a DNS name after IDNA or an IP literal, no user part, a valid
    port. Returns it stripped (no trailing slash), else None."""
    import ipaddress
    from urllib.parse import urlsplit
    if not isinstance(v, str) or len(v) > 2048:
        return None
    s = v.strip().rstrip("/")
    try:
        p = urlsplit(s)
        host, _port = p.hostname, p.port
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not host or p.username is not None or p.password is not None:
        return None
    try:
        ipaddress.ip_address(host)
        return s
    except ValueError:
        pass
    try:
        a = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if len(a) > 253 or not all(_LABEL.fullmatch(label) for label in a.split(".")):
        return None
    return s


def check_code(v) -> str | None:
    """A sign-in code normalised as the server does it, then exactly its alphabet and length (074 R6)."""
    from routes.signin_helper_api import CODE_ALPHABET, CODE_LEN, _norm
    if not isinstance(v, str) or len(v) > 64:
        return None
    c = _norm(v)
    return c if len(c) == CODE_LEN and all(ch in CODE_ALPHABET for ch in c) else None


def http_ok_list(settings: dict | None = None) -> list[str]:
    s = config.get_settings() if settings is None else settings
    return [str(x) for x in (s.get("signin_helper_http_ok") or []) if isinstance(x, str)]


def set_http_ok(server: str, yes: bool) -> list[str]:
    """This desktop's own yes to plain http for *server* (M-r3-1). Kept here, never synced."""
    o = _origin(server)
    items = [x for x in http_ok_list() if x != o]
    if yes:
        items.append(o)
    config.save_settings({"signin_helper_http_ok": items})
    return items


def _detail(r) -> str:
    try:
        d = r.json().get("detail")
        return d if isinstance(d, str) else ""
    except Exception:
        return ""


def helper_signin(server: str, code: str, *, confirm, login_fn, http=None, queue: "Queue | None" = None,
                  settings: dict | None = None, resolve=None, now: float | None = None) -> dict:
    """The whole hand-over (contracts/http.md, desktop side). *confirm(title, message)* is the
    native dialog and only ``True`` goes on; *login_fn(platform)* opens the window and returns
    the creds without saving them here (or None)."""
    from auth.browser_login import PLATFORM_LOGIN, identity_keys
    server = (server or "").strip().rstrip("/")
    trusted, _ = config.is_trusted_transport(server)
    base = _origin(server)              # only scheme://host:port, the host the dialog names
    plain_http = False
    if not trusted:
        pinned = home_address(server, resolve=resolve)
        if not pinned:
            return {"ok": False, "message": MSG_TLS}
        # This computer's own yes first, so the code never crosses plain http without it (SEC059).
        if _origin(server) not in http_ok_list(settings):
            return {"ok": False, "needs_http_ok": True,
                    "message": "Sign-in over plain http needs a yes on this computer first."}
        base, plain_http = pinned, True
    client = http or helper_client()
    try:
        try:
            r = client.post(f"{base}/api/signin-helper/lookup", json={"code": code})
        except Exception:
            return {"ok": False, "message": MSG_AWAY}
        if r.status_code == 404:
            return {"ok": False, "message": MSG_BAD_CODE}
        if r.status_code != 200:
            return {"ok": False, "message": _detail(r) or MSG_AWAY}
        info = r.json() or {}
        platform = str(info.get("platform") or "")
        cfg = PLATFORM_LOGIN.get(platform)
        if not cfg:
            return {"ok": False, "message": "This PawPoller doesn't know that site. Update the desktop app."}
        if plain_http and info.get("allow_http") is not True:
            return {"ok": False, "message": MSG_TLS}
        site, host, label = cfg["name"], clean_label(ascii_host(server), 253), clean_label(info.get("account_label"))
        if plain_http:
            title = "Sign in without encryption?"
            text = (f"Server: {host}\nThis sends your {site} sign-in to {host} without encryption. Anyone else "
                    f"on this network could copy it and post as you. Only continue if you're at home on the "
                    f"same network as your server, not on café, work or shared Wi-Fi.\nAccount: {label}")
        else:
            title = f"Sign in to {site}?"
            text = f"Sign in to {site} on {host} for {label}?"
        if confirm(title, text) is not True:
            return {"ok": False, "message": MSG_CANCELLED}
        creds = login_fn(platform)
        if not creds:
            return {"ok": False, "message": MSG_CANCELLED}
        creds = {k: v for k, v in dict(creds).items() if k not in identity_keys(platform)}
        payload = {"server": base, "code": code, "cookies": creds}
        ok, status, detail = _send_helper(client, payload)
        if ok:
            who = detail
            return {"ok": True, "message": f"Connected as {who}. Saved on your server." if who
                    else "Connected. Saved on your server."}
        if status:
            return {"ok": False, "message": detail or MSG_AWAY}
        if plain_http:
            return {"ok": False, "message": MSG_AWAY}       # never queued over plain http (L-r3-3)
        q = queue or Queue(HELPER_QUEUE_PATH)
        try:   # the server's word, capped: a fake server can't keep a hand-over alive (SEC059)
            ttl = min(max(float(info.get("expires_in") or HELPER_TTL), 0.0), HELPER_TTL)
        except (TypeError, ValueError):
            ttl = HELPER_TTL
        expires = (time.time() if now is None else now) + ttl
        q.add("helper", dict(payload, expires_at=expires))
        return {"ok": True, "queued": True,
                "message": "Signed in. Your server isn't answering; it'll be sent when it is (within the code's 10 minutes)."}
    finally:
        if http is None:
            client.close()


def _send_helper(client, payload: dict) -> tuple[bool, int, str]:
    """(ok, HTTP status or 0 when unreachable, handle on success / the server's sentence)."""
    try:
        r = client.post(f"{payload['server']}/api/signin-helper/result",
                        json={"code": payload["code"], "cookies": payload.get("cookies") or {}})
    except Exception:
        return False, 0, ""
    if r.status_code == 200:
        return True, 200, str((r.json() or {}).get("handle") or "")
    if r.status_code == 404:
        return False, 404, MSG_BAD_CODE
    return False, r.status_code, _detail(r)


class HelperSender:
    """Delivers waiting hand-overs (kind ``helper``) with the key-less helper client; drops any
    whose code has run out, on a timer, sent or not. Started by the standalone desktop (main.py);
    connected mode never queues a hand-over."""

    def __init__(self, queue: "Queue | None" = None, http=None, interval: float = DRAIN_INTERVAL, notify=None):
        self.queue = queue or Queue(HELPER_QUEUE_PATH)
        self._http = http
        self._interval = interval
        self._notify = notify               # called with a sentence when an item is dropped or delivered
        self._stop = threading.Event()
        self.last_message = ""              # shown on the desktop's helper screen

    def _say(self, message: str) -> None:
        self.last_message = message
        if self._notify:
            self._notify(message)

    def drain(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        sent = 0
        for item in self.queue.pending():
            p = item.get("payload") or {}
            if now >= float(p.get("expires_at") or 0) or not p.get("server"):
                self.queue.remove(item["id"])
                self._say("That code ran out before your server answered. Make a new one.")
                continue
            if float(item.get("next_at") or 0) > now:
                continue
            client = self._http or helper_client()
            try:
                ok, status, detail = _send_helper(client, p)
            finally:
                if self._http is None:
                    client.close()
            if ok:
                self.queue.remove(item["id"])
                sent += 1
                self._say(f"Connected as {detail}. Saved on your server." if detail else "Connected.")
            elif status:
                self.queue.remove(item["id"])        # the server answered and refused: never retried
                self._say(detail or MSG_BAD_CODE)
            else:
                self.queue.failed(item["id"], "unreachable", now)
        return sent

    def start(self) -> None:
        def _loop():
            while not self._stop.wait(self._interval):
                try:
                    if self.queue.pending_count():
                        self.drain()
                except Exception as e:
                    logger.debug("helper drain failed: %s", e)
        threading.Thread(target=_loop, daemon=True, name="signin-helper").start()

    def stop(self) -> None:
        self._stop.set()


# ── Change server (spec 072) ─────────────────────────────────────────────────
# A connected desktop is pointed at a different PawPoller from a small local window: the
# address is checked without the key (a real PawPoller, not a relay), the person confirms the
# host, then the key is checked, and only then are both saved (one vault write) and a fresh
# Agent takes over. Every message the form shows is one of these; nothing a server sends is
# shown except its version, cleaned.

MSG_CS_EMPTY_ADDRESS = "Type your PawPoller's address."
MSG_CS_EMPTY_KEY = "Paste the API key you made on that server."
MSG_CS_BAD_ADDRESS = "That doesn't look like a web address. It should start with https:// and have no spaces."
MSG_CS_USERINFO = "Leave out any name or password before an @ in the address."
MSG_CS_UNSAFE = ("Use an https:// address. Plain http:// only works for this computer or over Tailscale, "
                 "because the key would be sent unprotected.")
MSG_CS_BAD_KEY = ("That doesn't look like a PawPoller API key. Copy it again from the server, with nothing "
                  "before or after it.")
MSG_CS_UNREACHABLE = "Couldn't reach that address. Check it's typed right and that the server is on."
MSG_CS_TIMEOUT = "That address didn't answer in time. Check the server is on, then try again."
MSG_CS_CERT = "That address's security certificate didn't check out, so nothing was sent. Check the address."
MSG_CS_TAILSCALE = " If it's a Tailscale address, check Tailscale is connected on this computer."
MSG_CS_RELAY = "That address only runs the Instagram relay, not PawPoller. Use your PawPoller's own address."
MSG_CS_NOT_PAWPOLLER = "Something answered at that address, but it isn't a PawPoller."
MSG_CS_KEY_REFUSED = ("That server didn't accept the key. Make a new one on that server under "
                      "Settings → Security → API Keys and paste it here.")
MSG_CS_TOO_MANY = "That server has had too many tries. Wait a minute, then try again."
MSG_CS_KEY_ODD = "That server didn't answer the key check the way a PawPoller does. Try again in a minute."
MSG_CS_SAME_KEY_NEW_HOST = ("That's the key for your current server. Make a new key on the new server "
                            "and paste that one.")
MSG_CS_NOT_CHECKED = "Check the address first."
MSG_CS_BUSY = "Still checking. Wait for it to finish."
MSG_CS_STALE = "This window is out of date. Close it and choose Change server… again."
MSG_CS_OPEN_INSTANCE = ("That server has no password, so the key may not have been checked, and anyone who can "
                        "reach it can use it. Set a password on it under Settings → Security.")
MSG_CS_SWITCHED = "This app has just moved to a different server. Try that again."
MSG_CS_FAILED = "Something went wrong, and nothing was changed. Try again."
MSG_CS_SAVE_FAILED = "Couldn't save the new server on this computer, so nothing was changed."
MSG_CS_DONE = "Connected. PawPoller is opening your new server."
MSG_CS_REVOKE = ("If your old server still runs, remove this computer's old key there, under "
                 "Settings → Security → API Keys.")

CS_BODY_CAP = 64 * 1024
CS_DEADLINE = 10.0


def clean_origin(address) -> tuple[str | None, str | None]:
    """(origin, None) or (None, message). One punycode origin, from the same encoder httpx connects
    with, is what the form shows, checks, saves and loads (072 R5: Python's idna codec differs)."""
    import unicodedata
    a = (address if isinstance(address, str) else "").strip()
    if not a:
        return None, MSG_CS_EMPTY_ADDRESS
    if any(ch.isspace() or ch == "\\" or unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") for ch in a):
        return None, MSG_CS_BAD_ADDRESS
    low = a.lower()
    if not low.startswith(("http://", "https://")):
        return None, MSG_CS_BAD_ADDRESS
    authority = a.split("//", 1)[1].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in authority:
        return None, MSG_CS_USERINFO
    try:
        import httpx
        u = httpx.URL(a)
        host = u.raw_host.decode("ascii").lower()
        port = u.port
        scheme = u.scheme
    except Exception:
        return None, MSG_CS_BAD_ADDRESS
    if not host or scheme not in ("http", "https") or u.userinfo or (port is not None and not 0 < port < 65536):
        return None, MSG_CS_BAD_ADDRESS
    if ":" in host:
        host = f"[{host}]"
    origin = f"{scheme}://{host}" + (f":{port}" if port else "")
    ok, _why = config.is_trusted_transport(origin)
    if not ok:
        return None, MSG_CS_UNSAFE
    return origin, None


def valid_key(key) -> bool:
    return isinstance(key, str) and 8 <= len(key) <= 256 and all("\x21" <= ch <= "\x7e" for ch in key)


def _tailscale(origin: str) -> bool:
    return config.is_trusted_transport(origin)[1] in ("tailscale name", "tailscale address")


def _bounded_get(client, url: str, headers: dict | None = None) -> tuple[int, bytes | None, str | None]:
    """(status, body, None) or (status, None, message). No redirects (the client), no compressed
    replies, at most CS_BODY_CAP bytes, within CS_DEADLINE overall: httpx's read timeout restarts
    on every chunk, so a server sending a byte at a time would otherwise hold the form."""
    import ssl
    import httpx
    end = time.monotonic() + CS_DEADLINE
    h = {"Accept-Encoding": "identity", **(headers or {})}
    try:
        with client.stream("GET", url, headers=h, timeout=CS_DEADLINE) as r:
            if (r.headers.get("content-encoding") or "identity").strip().lower() != "identity":
                return r.status_code, None, MSG_CS_NOT_PAWPOLLER
            buf = bytearray()
            for chunk in r.iter_bytes():           # compressed replies were refused above
                buf += chunk
                if len(buf) > CS_BODY_CAP:
                    return r.status_code, None, MSG_CS_NOT_PAWPOLLER
                if time.monotonic() > end:
                    return r.status_code, None, MSG_CS_TIMEOUT
            return r.status_code, bytes(buf), None
    except httpx.TimeoutException:
        return 0, None, MSG_CS_TIMEOUT
    except Exception as e:
        cause = e.__cause__ or e.__context__
        if isinstance(cause, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in repr(e):
            return 0, None, MSG_CS_CERT
        return 0, None, MSG_CS_UNREACHABLE


def probe_server(origin: str) -> dict:
    """The keyless step: is *origin* a PawPoller (not a relay), and does it have a password?"""
    with helper_client() as c:
        status, body, err = _bounded_get(c, f"{origin}/api/health")
        if err:
            hint = MSG_CS_TAILSCALE if err in (MSG_CS_UNREACHABLE, MSG_CS_TIMEOUT) and _tailscale(origin) else ""
            return {"ok": False, "message": err + hint}
        try:
            j = json.loads(body or b"") if status == 200 else None
        except ValueError:
            j = None
        if not isinstance(j, dict):
            return {"ok": False, "message": MSG_CS_NOT_PAWPOLLER}
        if j.get("mode") == "relay":
            return {"ok": False, "message": MSG_CS_RELAY}
        if j.get("status") != "ok" or not isinstance(j.get("version"), str):
            return {"ok": False, "message": MSG_CS_NOT_PAWPOLLER}
        open_instance = False
        st2, body2, err2 = _bounded_get(c, f"{origin}/api/auth/dashboard-status")
        if not err2 and st2 == 200:
            try:
                open_instance = (json.loads(body2 or b"") or {}).get("auth_required") is False
            except (ValueError, AttributeError):
                open_instance = False
        return {"ok": True, "version": clean_label(j["version"], 20), "open_instance": open_instance}


def check_key(origin: str, key: str) -> tuple[bool, str]:
    """The one request that carries the key, after the person confirmed the host."""
    with helper_client() as c:
        status, _body, err = _bounded_get(c, f"{origin}/api/settings/sync/status",
                                          headers={"Authorization": f"Bearer {key}"})
        if err:
            return False, err
        if status == 200:
            return True, ""
        if status in (401, 403):
            return False, MSG_CS_KEY_REFUSED
        if status == 429:
            return False, MSG_CS_TOO_MANY
        return False, MSG_CS_KEY_ODD


def _host_label(url: str) -> str:
    origin, _ = clean_origin(url or "")
    if origin:
        return origin.split("//", 1)[1]
    return clean_label(ascii_host(url or ""), 80)


def _js_str(value: str) -> str:
    """A JS string literal that is safe inside an inline <script>."""
    return json.dumps(str(value)).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


class Connection:
    """Connected mode's window state (072). Its bound methods are exposed to pages by exact name
    with ``window.expose``. No window gets a ``js_api`` object: pywebview resolves any dotted
    attribute path a page sends on one (``util.js_bridge_call``), private names included, so a
    server page could walk from it to the key. Exact-name lookup has nothing to walk.

    The two local pages (offline, Change server) carry a fresh nonce; their functions do nothing
    without it, so a page the window was navigated to (a dropped link) can't use them.
    """

    def __init__(self, agent: "Agent | None", *, queue: "Queue", current_url: str, saved_key: str = "",
                 load_url=None, load_html=None, open_form=None, close_form=None, picker=None,
                 save=None, make_agent=None, wait_loaded=None):
        self.agent = agent
        self.queue = queue
        self.current_url = (current_url or "").rstrip("/")
        self._saved_key = saved_key or (agent.api_key if agent else "")
        self._load_url, self._load_html = load_url, load_html
        self._open_form, self._close_form = open_form, close_form
        self._wait_loaded = wait_loaded         # bounded: the window finished loading the last page
        self._picker = picker
        self._save = save or config.save_settings
        self._make_agent = make_agent or (lambda origin, key, q: Agent(origin, key, queue=q))
        self.offline_nonce = ""
        self.form_nonce = ""
        self._checked_origin = ""
        self._checked_ids: list[str] = []
        self._checked_open = False
        self._cancel = False
        self._busy = threading.Lock()
        self._nav_lock = threading.RLock()
        self._form_lock = threading.Lock()
        self._commit_lock = threading.Lock()

    # -- what pages may call -------------------------------------------------
    def main_functions(self) -> tuple:
        return (self.open_image_dialog, self.agent_login, self.agent_status, self.fa_journal,
                self.open_change_server)

    def form_functions(self) -> tuple:
        return (self.cs_check, self.cs_connect, self.cs_cancel)

    @staticmethod
    def _nonce_ok(given, current: str) -> bool:
        return bool(current) and isinstance(given, str) and given.isascii() and hmac.compare_digest(given, current)

    @staticmethod
    def _call(fn, *args) -> None:
        """A window callable; the window may already be gone."""
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as e:
            logger.debug("window call failed: %s", type(e).__name__)

    # -- the main window's pages ---------------------------------------------
    def offline_html(self) -> str:
        with self._nav_lock:
            self.offline_nonce = secrets.token_urlsafe(24)
            return offline_page(self.current_url, self.agent, self.offline_nonce)

    def unbound_html(self) -> str:
        with self._nav_lock:
            self.offline_nonce = secrets.token_urlsafe(24)
            return unbound_page(self.offline_nonce)

    def show_server(self) -> None:
        with self._nav_lock:
            self.offline_nonce = ""
            self._call(self._load_url, self.current_url)

    def reach_callback(self, agent: "Agent") -> None:
        """The old address answering late must not pull the window back after a change (FR-SEC-008)."""
        with self._nav_lock:
            if agent is self.agent:
                self.show_server()

    # -- main window functions (a server page can call these) ----------------
    def open_change_server(self, nonce=None):
        try:
            if not self._nonce_ok(nonce, self.offline_nonce):
                return False
            self._call(self._open_form)
            return True
        except Exception:
            return False

    def _pick(self) -> list[str]:
        if self._picker is not None:
            return list(self._picker() or [])
        import webview
        win = webview.windows[0] if webview.windows else None
        if win is None:
            return []
        result = win.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("Image files (*.png;*.jpg;*.jpeg;*.gif;*.webp)", "All files (*.*)"))
        return list(result) if result else []

    def open_image_dialog(self):
        try:
            agent = self.agent
            if agent is None:
                return []
            paths = self._pick()
            if not paths:
                return []
            res = agent.upload(paths[0], "inbox")
            if res.get("ok"):
                return [res["path"]]
            logger.warning("Upload via agent failed: %s", res.get("message"))
            return []
        except Exception as e:
            logger.error("open_image_dialog (connected) failed: %s", e)
            return []

    def agent_login(self, platform, extra_fields=None, account_id=None):
        try:
            agent = self.agent
            if agent is None:
                return {"ok": False, "message": MSG_CS_SWITCHED}
            return agent.login(str(platform), dict(extra_fields or {}),
                               int(account_id) if account_id not in (None, "") else None)
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def agent_status(self):
        try:
            agent = self.agent
            if agent is None:
                return {"server_url": "", "pending": self.queue.pending_count(), "last_contact": 0,
                        "last_error": "not connected", "version": config.APP_VERSION}
            return agent.status()
        except Exception:
            return {}

    def fa_journal(self, post_id, title, message, rating):
        """Spec 027: FA's journal page, filled in, in a desktop window (the person does the CAPTCHA)."""
        from auth.fa_journal_window import open_fa_journal
        agent = self.agent

        def _posted(jid, url):
            if not post_id or agent is None:
                return
            c = agent._client()
            try:
                c.post(f"{agent.server_url}/api/posts/{int(post_id)}/journal-record",
                       json={"platform": "fa", "external_id": jid, "external_url": url})
            except Exception as e:
                logger.warning("Couldn't record the FA journal on the server: %s", e)
            finally:
                agent._close(c)
        try:
            return open_fa_journal(str(title or ""), str(message or ""), str(rating or "0"), _posted)
        except Exception as e:
            return {"ok": False, "message": str(e)}

    # -- the Change server form ----------------------------------------------
    def form_html(self) -> str:
        """A fresh form: new nonce, nothing checked, no leftover cancel."""
        with self._commit_lock:
            self.form_nonce = secrets.token_urlsafe(24)
            self._checked_origin, self._checked_ids, self._checked_open = "", [], False
            self._cancel = False
            return change_server_page(self.form_nonce, _host_label(self.current_url))

    def form_closed(self) -> None:
        with self._commit_lock:                 # never mid-save: the switch reads what was checked
            self.form_nonce = ""
            self._checked_origin, self._checked_ids = "", []

    def _waiting(self) -> tuple[list[str], dict]:
        from database.accounts import PLATFORM_NAMES
        ids, signins, uploads = [], [], 0
        for item in self.queue.pending():
            ids.append(item["id"])
            if item.get("kind") == "cookies":
                p = item.get("payload") or {}
                acc = p.get("account_id")
                signins.append({"site": PLATFORM_NAMES.get(str(p.get("platform") or ""), "a site"),
                                "account": f"account {int(acc)}" if isinstance(acc, int) else "main account"})
            elif item.get("kind") == "upload":
                uploads += 1
        return ids, {"signins": signins, "uploads": uploads}

    def cs_check(self, nonce=None, address=None):
        if not self._nonce_ok(nonce, self.form_nonce):
            return {"ok": False, "message": MSG_CS_STALE}
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "message": MSG_CS_BUSY}
        try:
            self._checked_origin, self._checked_ids, self._checked_open = "", [], False
            origin, msg = clean_origin(address)
            if msg or not origin:
                return {"ok": False, "message": msg or MSG_CS_FAILED}
            res = probe_server(origin)
            if not res.get("ok"):
                return {"ok": False, "message": res.get("message") or MSG_CS_FAILED}
            ids, waiting = self._waiting()
            self._checked_origin, self._checked_ids, self._checked_open = origin, ids, bool(res["open_instance"])
            v = res["version"]
            note = "" if v == config.APP_VERSION else (
                f"That server runs PawPoller {v} and this app is {config.APP_VERSION}. That's fine; if "
                f"something looks odd later, update the older one.")
            return {"ok": True, "from_host": _host_label(self.current_url), "to_host": _host_label(origin),
                    "version_note": note, "open_instance": self._checked_open, "waiting": waiting}
        except Exception as e:
            logger.warning("Change server check failed: %s", type(e).__name__)
            return {"ok": False, "message": MSG_CS_FAILED}
        finally:
            self._busy.release()

    def cs_connect(self, nonce=None, key=None, send_waiting=True):
        if not self._nonce_ok(nonce, self.form_nonce):
            return {"ok": False, "message": MSG_CS_STALE}
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "message": MSG_CS_BUSY}
        try:
            origin = self._checked_origin
            if not origin:
                return {"ok": False, "message": MSG_CS_NOT_CHECKED}
            key = key.strip() if isinstance(key, str) else ""
            if not key:
                return {"ok": False, "message": MSG_CS_EMPTY_KEY}
            if not valid_key(key):
                return {"ok": False, "message": MSG_CS_BAD_KEY}
            if (self._saved_key and hmac.compare_digest(key.encode(), self._saved_key.encode())
                    and _host_label(origin) != _host_label(self.current_url)):
                return {"ok": False, "message": MSG_CS_SAME_KEY_NEW_HOST}
            ok, msg = check_key(origin, key)
            if not ok:
                return {"ok": False, "message": msg}
            with self._commit_lock:
                if self._cancel or origin != self._checked_origin:
                    return {"ok": False, "message": MSG_CS_STALE}
                ids = list(self._checked_ids)
                try:
                    # One call: both live in the vault, written with one os.replace (072 R3). Sync off,
                    # or the save would schedule a push of this desktop's settings to the new server.
                    self._save({"posting_server_url": origin, "posting_server_api_key": key,
                                "auto_sync_enabled": False})
                except Exception as e:
                    logger.error("Change server: saving failed: %s", type(e).__name__)
                    return {"ok": False, "message": MSG_CS_SAVE_FAILED}
                self._switch(origin, key, bool(send_waiting), ids)
            logger.info("Change server: this desktop now opens a different server")
            return {"ok": True, "message": MSG_CS_DONE, "revoke_hint": MSG_CS_REVOKE,
                    "open_instance": self._checked_open}
        except Exception as e:
            logger.warning("Change server connect failed: %s", type(e).__name__)
            return {"ok": False, "message": MSG_CS_FAILED}
        finally:
            self._busy.release()

    def _switch(self, origin: str, key: str, send: bool, ids: list[str]) -> None:
        old = self.agent
        if old is not None:
            old.stop()                          # drain stops between items; a stopped agent doesn't queue
        if send:
            self.queue.retag(ids, origin)
        else:
            self.queue.drop(ids)
        self.queue.keep_only(origin)            # anything queued after the check was never shown
        new = self._make_agent(origin, key, self.queue)
        new.start()
        with self._nav_lock:
            # The old server's page stays live until the switching page has loaded: until then its
            # calls reach no agent at all, never the new one (implement review Low).
            self.agent = None
            self.offline_nonce = ""
            self._call(self._load_html, switching_page())
            self._call(self._wait_loaded)
            self.agent = new
            self.current_url = origin
            self._saved_key = key
            self._checked_origin, self._checked_ids = "", []
            self._call(self._load_url, origin)

    def cs_cancel(self, nonce=None):
        if not self._nonce_ok(nonce, self.form_nonce):
            return {"ok": False, "message": MSG_CS_STALE}
        try:
            with self._commit_lock:
                self._cancel = True
            self._call(self._close_form)
            return True
        except Exception:
            return False


# ── the local pages ──────────────────────────────────────────────────────────

_LOCAL_CSP = ("default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline'; "
              "base-uri 'none'; form-action 'none'")
_LOCAL_CSS = """body{font:15px/1.5 system-ui,sans-serif;background:#0f1216;color:#e6e9ee;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{max-width:480px;padding:28px 32px;border:1px solid #262c35;border-radius:12px;background:#171b21}h1{font-size:20px;margin:0 0 8px}
p{color:#a3acba;margin:8px 0}code,b.host{color:#6cb2ff;font-weight:600}
button{font:inherit;padding:8px 16px;border-radius:8px;border:1px solid #6cb2ff;background:#6cb2ff;color:#0f1216;cursor:pointer;margin:8px 8px 0 0}
button.quiet{background:transparent;color:#e6e9ee;border-color:#3a4350}button:disabled{opacity:.5;cursor:default}
button:focus-visible,input:focus-visible{outline:2px solid #ffd166;outline-offset:2px}
label{display:block;margin:14px 0 4px;color:#e6e9ee}input[type=text],input[type=password]{width:100%;box-sizing:border-box;font:inherit;padding:8px 10px;border-radius:8px;border:1px solid #3a4350;background:#0f1216;color:#e6e9ee}
.msg{color:#ffb4a8;min-height:1.5em}.ok{color:#9be3a8}ul{margin:6px 0;padding-left:20px;color:#a3acba}"""
_NO_DROP = ("document.addEventListener('dragover',function(e){e.preventDefault();});"
            "document.addEventListener('drop',function(e){e.preventDefault();});")


def _local_page(body: str, script: str = "") -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{_LOCAL_CSP}"><title>PawPoller</title>'
            f'<style>{_LOCAL_CSS}</style></head><body><div class="card">{body}</div>'
            f'<script>{_NO_DROP}{script}</script></body></html>')


def _change_button(nonce: str) -> tuple[str, str]:
    if not nonce:
        return "", ""
    return ('<button id="cs" type="button">Change server…</button>',
            f"var N={_js_str(nonce)};document.getElementById('cs').addEventListener('click',function(){{"
            "if(window.pywebview&&pywebview.api&&pywebview.api.open_change_server){pywebview.api.open_change_server(N);}"
            "});")


def offline_page(server_url: str, agent: Agent | None = None, nonce: str = "") -> str:
    """What the window shows while the server cannot be reached; it swaps to the server itself."""
    import html
    pending = agent.queue.pending_count() if agent else 0
    if agent and agent.last_error == "relay":
        head = "This address isn't a PawPoller any more"
        body = ("<p><code>{0}</code> only runs the Instagram relay now, so there is nothing here to open.</p>"
                "<p>If your PawPoller moved (to a hosted seat, say), choose <b>Change server…</b> and give "
                "this app its new address and an API key made there.</p>").format(html.escape(server_url))
    else:
        head = "Your server isn't reachable yet"
        body = ("<p>PawPoller is trying to open <code>{0}</code> and will switch over the moment it answers.</p>"
                "<p>If it's a machine at home, check it's on and that Tailscale is connected on both ends. "
                "If your PawPoller has moved, choose <b>Change server…</b>.</p>"
                ).format(html.escape(server_url))
    button, script = _change_button(nonce)
    return _local_page(f"<h1>{head}</h1>{body}"
                       f"<p>{pending} item{'s' if pending != 1 else ''} waiting to be sent.</p>{button}", script)


def unbound_page(nonce: str) -> str:
    """Connected mode with no key that belongs to the address (072 FR-SEC-006): nothing is sent."""
    button, script = _change_button(nonce)
    return _local_page("<h1>Connect this app to your server</h1>"
                       "<p>This app was started with a server address it has no key for, so it hasn't "
                       "contacted it.</p><p>Choose <b>Change server…</b> and give it your PawPoller's "
                       f"address and an API key made there.</p>{button}", script)


def switching_page() -> str:
    return _local_page("<h1>Switching server…</h1><p>Opening your new server.</p>")


def change_server_page(nonce: str, current_host: str) -> str:
    """The Change server form: its own local window, its own functions, nothing loaded from outside."""
    body = """<h1>Change server</h1>
<p>Now: <b class="host" id="from"></b></p>
<label for="addr">Your PawPoller's address</label>
<input type="text" id="addr" autocomplete="off" spellcheck="false" placeholder="https://">
<button id="check" type="button">Check</button><button id="cancel" class="quiet" type="button">Cancel</button>
<p class="msg" id="msg1" role="alert"></p>
<div id="step2" hidden>
<p>Move this app from <b class="host" id="from2"></b> to <b class="host" id="to"></b>?</p>
<p>The key below must have been made on <b class="host" id="to2"></b>. Sign-ins you make in this app will go there.</p>
<p id="note"></p>
<div id="wait" hidden><p>Waiting to be sent:</p><ul id="waitlist"></ul>
<label><input type="radio" name="w" id="send" checked> Send them to the new server</label>
<label><input type="radio" name="w" id="discard"> Throw them away</label></div>
<button id="yes" type="button">Yes, that's my server</button>
<label for="key">API key made on that server</label>
<input type="password" id="key" autocomplete="off" disabled>
<p>Make one on your server under Settings → Security → API Keys.</p>
<button id="connect" type="button" disabled>Connect</button>
<p class="msg" id="msg2" role="alert"></p>
</div>
<div id="done" hidden><p class="ok" id="okmsg"></p><p id="open"></p><p id="revoke"></p>
<button id="close" type="button">Close</button></div>"""
    script = (f"var N={_js_str(nonce)},CUR={_js_str(current_host)};" + r"""
function $(i){return document.getElementById(i);}
function api(){return (window.pywebview&&pywebview.api)||{};}
$('from').textContent=CUR;
function reset(){$('step2').hidden=true;$('key').disabled=true;$('connect').disabled=true;$('key').value='';$('msg2').textContent='';}
$('addr').addEventListener('input',reset);
$('check').addEventListener('click',function(){
 reset();$('msg1').textContent='';$('check').disabled=true;
 Promise.resolve(api().cs_check&&api().cs_check(N,$('addr').value)).then(function(r){
  r=r||{};if(!r.ok){$('msg1').textContent=r.message||'';return;}
  $('from2').textContent=r.from_host;$('to').textContent=r.to_host;$('to2').textContent=r.to_host;
  $('note').textContent=r.version_note||'';
  var w=r.waiting||{},ul=$('waitlist');ul.textContent='';
  (w.signins||[]).forEach(function(s){var li=document.createElement('li');li.textContent='Sign-in: '+s.site+', '+s.account;ul.appendChild(li);});
  if(w.uploads){var li=document.createElement('li');li.textContent=w.uploads+' picture'+(w.uploads==1?'':'s')+' to upload';ul.appendChild(li);}
  $('wait').hidden=!ul.childNodes.length;$('step2').hidden=false;$('yes').disabled=false;$('yes').focus();
 }).finally(function(){$('check').disabled=false;});
});
$('yes').addEventListener('click',function(){$('key').disabled=false;$('connect').disabled=false;$('yes').disabled=true;$('key').focus();});
$('connect').addEventListener('click',function(){
 var k=$('key').value;$('key').value='';$('msg2').textContent='';$('connect').disabled=true;
 Promise.resolve(api().cs_connect&&api().cs_connect(N,k,$('send').checked)).then(function(r){
  r=r||{};if(!r.ok){$('msg2').textContent=r.message||'';$('connect').disabled=false;return;}
  $('step2').hidden=true;$('done').hidden=false;$('okmsg').textContent=r.message||'';
  $('open').textContent=r.open_instance?""" + _js_str(MSG_CS_OPEN_INSTANCE) + r""":'';
  $('revoke').textContent=r.revoke_hint||'';$('close').focus();
 });
 k='';
});
function bye(){if(api().cs_cancel){api().cs_cancel(N);}}
$('cancel').addEventListener('click',bye);$('close').addEventListener('click',bye);
""")
    return _local_page(body, script)
