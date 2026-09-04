"""A temporary public address for a desktop's Instagram images — 4.7.0.

Instagram never accepts image bytes: it takes a public ``image_url`` and Meta's
servers fetch it. A desktop install has no public address, so when the PawPoller
relay is unreachable (or turned off) this module builds one for the few minutes
a publish needs:

  1. a tiny HTTP server on ``127.0.0.1:<random port>`` that serves ONLY stashed
     images by their unguessable token (``ig_media.path_for``) — never the
     dashboard, never anything else on this machine;
  2. a Cloudflare *quick tunnel* (``cloudflared tunnel --url …``) in front of it,
     which needs no Cloudflare account and prints a throwaway
     ``https://<random>.trycloudflare.com`` address;
  3. a readiness probe through that address, because a fresh quick tunnel can
     answer 502/1033 for its first seconds;
  4. teardown once the publish is done.

The helper binary is not bundled (55 MB). It is downloaded once, on request,
from cloudflared's GitHub release pinned by version AND SHA-256 in
``config.IG_TUNNEL_HELPER_ASSETS`` — a download that does not match its pinned
digest is deleted and reported, never installed. Quick tunnels carry no uptime
promise from Cloudflare; that is why they are the last rung of the ladder in
``posting/ig_host.py`` and not the first.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import platform
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
from posting import ig_media

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_PING_PATH = "/__ping"


# ── The helper binary ────────────────────────────────────────────────────────

def helper_asset() -> tuple[str, str] | None:
    """``(asset name, sha256)`` for this machine, or None when there is no build."""
    return config.IG_TUNNEL_HELPER_ASSETS.get((platform.system(), platform.machine()))


def helper_path() -> Path:
    name = "cloudflared.exe" if platform.system() == "Windows" else "cloudflared"
    return Path(config.APPDATA_DIR) / "helpers" / name


def _version_marker(p: Path) -> Path:
    return p.with_name(p.name + ".version")


def helper_status() -> dict:
    asset = helper_asset()
    p = helper_path()
    present = p.exists()
    version = None
    if present:
        try:
            version = _version_marker(p).read_text(encoding="utf-8").strip() or None
        except OSError:
            version = None
    return {
        "supported": asset is not None,
        "asset": asset[0] if asset else None,
        "present": present,
        "version": version,
        "wanted": config.IG_TUNNEL_HELPER_VERSION,
        "path": str(p),
        "size_mb": round(p.stat().st_size / 1e6) if present else None,
    }


async def download_helper(http=None) -> dict:
    """Fetch the pinned cloudflared build, verify its SHA-256, install it.

    *http* may be an ``httpx.AsyncClient`` (tests pass a mock transport). Raises
    ``RuntimeError`` with a plain sentence on any failure; nothing is installed
    unless the digest matches.
    """
    asset = helper_asset()
    if not asset:
        raise RuntimeError("There is no tunnel helper build for this machine "
                           f"({platform.system()} / {platform.machine()}).")
    name, want = asset
    url = config.IG_TUNNEL_HELPER_URL.format(version=config.IG_TUNNEL_HELPER_VERSION, asset=name)
    dest = helper_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")

    import httpx
    own = http is None
    client = http or httpx.AsyncClient(follow_redirects=True,
                                       timeout=httpx.Timeout(60.0, read=180.0))
    digest = hashlib.sha256()
    try:
        async with client.stream("GET", url) as r:
            if r.status_code != 200:
                raise RuntimeError(f"Cloudflare's release download answered HTTP {r.status_code}.")
            with open(part, "wb") as fh:
                async for chunk in r.aiter_bytes(1 << 16):
                    digest.update(chunk)
                    fh.write(chunk)
    except RuntimeError:
        part.unlink(missing_ok=True)
        raise
    except Exception as e:  # network, disk
        part.unlink(missing_ok=True)
        raise RuntimeError(f"The tunnel helper could not be downloaded: {e}") from e
    finally:
        if own:
            await client.aclose()

    got = digest.hexdigest()
    if got != want:
        part.unlink(missing_ok=True)
        raise RuntimeError("The downloaded tunnel helper did not match its pinned checksum "
                           f"(got {got[:12]}…, wanted {want[:12]}…) — nothing was installed.")
    os.replace(part, dest)
    if platform.system() != "Windows":
        dest.chmod(0o755)
    _version_marker(dest).write_text(config.IG_TUNNEL_HELPER_VERSION, encoding="utf-8")
    logger.info("IG tunnel: helper %s %s installed", name, config.IG_TUNNEL_HELPER_VERSION)
    return helper_status()


def remove_helper() -> dict:
    p = helper_path()
    for f in (p, _version_marker(p), p.with_name(p.name + ".part")):
        try:
            f.unlink()
        except OSError:
            pass
    return helper_status()


# ── The tiny local image server ──────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    server_version = "PawPollerImageHost/1"
    sys_version = ""

    def log_message(self, *_args):   # stay quiet; the publish log says what matters
        pass

    def _resolve(self):
        path = self.path.split("?", 1)[0]
        if path == _PING_PATH:
            return "ping", None
        token = path.rsplit("/", 1)[-1]
        p = ig_media.path_for(token) if token else None
        return ("image", p) if p else ("missing", None)

    def do_HEAD(self):
        kind, p = self._resolve()
        if kind == "ping":
            self.send_response(204)
            self.end_headers()
            return
        if kind != "image":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(p.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        kind, p = self._resolve()
        if kind == "ping":
            self.send_response(204)
            self.end_headers()
            return
        if kind != "image":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


class StashServer:
    """Serves ``GET /<token>.jpg`` for stashed images on a random loopback port."""

    def __init__(self):
        self._srv: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._srv.server_address[1] if self._srv else 0

    def start(self) -> int:
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=self._srv.serve_forever, name="ig-stash-server", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._srv:
            try:
                self._srv.shutdown()
                self._srv.server_close()
            except Exception:
                pass
        self._srv = None
        self._thread = None


# ── The tunnel ───────────────────────────────────────────────────────────────

def parse_public_url(text: str) -> str | None:
    m = _URL_RE.search(text)
    return m.group(0) if m else None


class Tunnel:
    def __init__(self, helper: Path, port: int):
        self.helper = helper
        self.port = port
        self.url: str | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self._drain: asyncio.Task | None = None

    async def start(self, timeout: float = 30.0) -> str:
        cmd = [str(self.helper), "tunnel", "--url", f"http://127.0.0.1:{self.port}",
               "--no-autoupdate", "--metrics", "127.0.0.1:0"]
        kw = {}
        if platform.system() == "Windows":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, **kw)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.url is None:
            try:
                line = await asyncio.wait_for(self.proc.stderr.readline(),
                                              timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            if not line:
                break
            self.url = parse_public_url(line.decode("utf-8", "replace"))
        if not self.url:
            await self.stop()
            raise RuntimeError(f"the tunnel helper did not report a public address within {int(timeout)}s")
        self._drain = asyncio.create_task(self._drain_stderr())
        logger.info("IG tunnel: open at %s -> 127.0.0.1:%d", self.url, self.port)
        return self.url

    async def _drain_stderr(self) -> None:
        try:
            while self.proc and self.proc.stderr:
                line = await self.proc.stderr.readline()
                if not line:
                    return
        except Exception:
            return

    async def wait_ready(self, timeout: float = 25.0, http=None) -> None:
        """Poll ``<url>/__ping`` through the tunnel until it answers (a fresh quick
        tunnel can 502/1033 for its first seconds)."""
        import httpx
        own = http is None
        client = http or httpx.AsyncClient(timeout=8.0)
        deadline = time.monotonic() + timeout
        last = "no answer"
        try:
            while time.monotonic() < deadline:
                try:
                    r = await client.get(self.url + _PING_PATH)
                    if r.status_code in (200, 204):
                        return
                    last = f"HTTP {r.status_code}"
                except Exception as e:
                    last = type(e).__name__
                await asyncio.sleep(1.0)
        finally:
            if own:
                await client.aclose()
        raise RuntimeError(f"the tunnel opened but Cloudflare never answered through it ({last})")

    async def stop(self) -> None:
        if self._drain:
            self._drain.cancel()
            self._drain = None
        p, self.proc = self.proc, None
        if p and p.returncode is None:
            try:
                p.terminate()
                try:
                    await asyncio.wait_for(p.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    p.kill()
                    await p.wait()
            except ProcessLookupError:
                pass
        if self.url:
            logger.info("IG tunnel: closed %s", self.url)
        self.url = None


class PublicHost:
    """A running stash server + tunnel; ``base_url + '/<token>.jpg'`` is fetchable
    by Meta until ``close()``."""

    def __init__(self, server: StashServer, tunnel: Tunnel):
        self.server = server
        self.tunnel = tunnel

    @property
    def base_url(self) -> str:
        return self.tunnel.url or ""

    async def close(self) -> None:
        await self.tunnel.stop()
        self.server.stop()


async def open_public_host(timeout: float = 30.0) -> PublicHost:
    st = helper_status()
    if not st["supported"]:
        raise RuntimeError("no tunnel helper build for this machine")
    if not st["present"]:
        raise RuntimeError("the tunnel helper is not downloaded (Settings → Posting → Instagram image host)")
    server = StashServer()
    server.start()
    tunnel = Tunnel(helper_path(), server.port)
    try:
        await tunnel.start(timeout=timeout)
        await tunnel.wait_ready()
    except Exception:
        await tunnel.stop()
        server.stop()
        raise
    return PublicHost(server, tunnel)
