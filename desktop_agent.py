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
import json
import logging
import os
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
    """(server_url, api_key) for connected mode; ``--connect URL`` overrides the saved URL."""
    url = settings.get("posting_server_url") or ""
    if "--connect" in argv:
        i = argv.index("--connect")
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            url = argv[i + 1]
    return (url or "").rstrip("/"), settings.get("posting_server_api_key") or ""


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

class Queue:
    """Persistent FIFO of things to deliver. One JSON file, rewritten atomically."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else QUEUE_PATH
        self._lock = threading.RLock()
        self._items: list[dict] = self._load()

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

    def add(self, kind: str, payload: dict, dedup_key: str | None = None) -> str:
        with self._lock:
            if dedup_key:
                self._items = [i for i in self._items if i.get("dedup_key") != dedup_key]
            item = {"id": uuid.uuid4().hex[:12], "kind": kind, "payload": payload, "dedup_key": dedup_key,
                    "created": time.time(), "attempts": 0, "next_at": 0.0, "last_error": ""}
            self._items.append(item)
            if len(self._items) > MAX_QUEUE:
                self._items = self._items[-MAX_QUEUE:]
            self._save()
            return item["id"]

    def pending(self) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self._items]

    def pending_count(self) -> int:
        with self._lock:
            return len(self._items)

    def due(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        with self._lock:
            return [dict(i) for i in self._items if float(i.get("next_at") or 0) <= now]

    def remove(self, item_id: str) -> None:
        with self._lock:
            self._items = [i for i in self._items if i["id"] != item_id]
            self._save()

    def failed(self, item_id: str, error: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            for i in self._items:
                if i["id"] == item_id:
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
                    finally:
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
            return False, f"HTTP {r.status_code}: {r.text[:120]}"
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
        for item in self.queue.due(now)[:max_items]:
            if item["kind"] == "cookies":
                ok, err = self.send_cookies(item["payload"])
            elif item["kind"] == "upload":
                ok, err, _ = self.send_upload(item["payload"])
            else:
                ok, err = False, f"unknown kind {item['kind']}"
                self.queue.remove(item["id"])
                continue
            if ok:
                self.queue.remove(item["id"])
                sent += 1
            elif permanent_failure(err):
                # The server understood and refused (bad account, oversize, malformed): retrying
                # can never succeed, so the item is dropped rather than kept for ever.
                logger.warning("Agent: dropping %s item after a permanent refusal: %s", item["kind"], err)
                self.queue.remove(item["id"])
                self.last_error = err
            else:
                self.queue.failed(item["id"], err, now)
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
        fn = self._login_fn
        if fn is None:
            from auth.browser_login import login_via_browser
            fn = login_via_browser
        try:
            creds = fn(platform, extra_fields or {}, account_id)
        except Exception as e:
            return {"ok": False, "message": str(e)}
        if not creds:
            return {"ok": False, "message": "Login cancelled."}
        payload = {"platform": platform, "cookies": dict(creds), "account_id": account_id,
                   "extra_fields": dict(extra_fields or {})}
        ok, err = self.send_cookies(payload)
        if ok:
            return {"ok": True, "message": "Connected — saved on your server."}
        self.queue.add("cookies", payload, dedup_key=f"cookies:{platform}:{account_id}")
        self._wake.set()
        return {"ok": True, "queued": True,
                "message": "Logged in. Your server is unreachable right now; the login will be sent when it is back."}

    def upload(self, path: str, kind: str = "inbox") -> dict:
        """Upload a local file now; if the server is away, queue it and say so."""
        p = Path(path)
        if not p.is_file():
            return {"ok": False, "message": "File not found."}
        sha = sha256_of(p)
        payload = {"path": str(p), "kind": kind, "sha256": sha}
        ok, err, server_path = self.send_upload(payload)
        if ok:
            return {"ok": True, "path": server_path, "sha256": sha}
        self.queue.add("upload", payload, dedup_key=f"upload:{sha}")
        self._wake.set()
        return {"ok": False, "queued": True, "sha256": sha,
                "message": "Your server is unreachable; the file will be uploaded when it is back."}

    def status(self) -> dict:
        return {"server_url": self.server_url, "pending": self.queue.pending_count(),
                "last_contact": self.last_contact, "last_error": self.last_error,
                "version": config.APP_VERSION}


# ── pywebview bridge ─────────────────────────────────────────────────────────

class AgentApi:
    """Exposed to the server-served page as ``window.pywebview.api`` (4.13.0).

    Keeps the desktop build's ``open_image_dialog`` name so the Artwork hub's picker works as
    it always did — except that the path it returns now lives on the server.
    """

    def __init__(self, agent: Agent, picker=None):
        self._agent = agent
        self._picker = picker

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
            paths = self._pick()
            if not paths:
                return []
            res = self._agent.upload(paths[0], "inbox")
            if res.get("ok"):
                return [res["path"]]
            logger.warning("Upload via agent failed: %s", res.get("message"))
            return []
        except Exception as e:
            logger.error("open_image_dialog (connected) failed: %s", e)
            return []

    def agent_login(self, platform, extra_fields=None, account_id=None):
        try:
            return self._agent.login(str(platform), dict(extra_fields or {}),
                                     int(account_id) if account_id not in (None, "") else None)
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def agent_status(self):
        return self._agent.status()


def offline_page(server_url: str, agent: Agent | None = None) -> str:
    """What the window shows while the server cannot be reached; it swaps to the server itself."""
    import html
    pending = agent.queue.pending_count() if agent else 0
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>PawPoller</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;background:#0f1216;color:#e6e9ee;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.card{{max-width:480px;padding:28px 32px;border:1px solid #262c35;border-radius:12px;background:#171b21}}h1{{font-size:20px;margin:0 0 8px}}
p{{color:#8b95a5;margin:8px 0}}code{{color:#6cb2ff}}</style></head><body><div class="card">
<h1>Your server isn't reachable yet</h1>
<p>PawPoller is trying to open <code>{html.escape(server_url)}</code> and will switch over the moment it answers.</p>
<p>If it's a machine at home, check it's on and that Tailscale is connected on both ends.</p>
<p>{pending} item{'s' if pending != 1 else ''} waiting to be sent.</p>
</div></body></html>"""
