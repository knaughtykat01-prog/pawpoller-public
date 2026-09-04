"""Where Instagram fetches a post's image from — the ladder (4.7.0).

Meta never takes image bytes; it takes a public ``image_url`` and fetches it.
One instance may have a public address (a server with ``ig_public_base_url``);
most desktops have none. So every Instagram publish climbs the same ladder and
stops at the first rung that works:

  1. **This instance's public base** — stash locally, serve from
     ``/api/ig/pubmedia/<token>.jpg`` (the server case since 2.64.0).
  2. **A paired server** — upload to its ``/api/ig/pubmedia`` with the pairing
     key and use the URL it returns (the paired-desktop case since 2.64.0).
  3. **The PawPoller relay** — any PawPoller server with ``ig_relay_open`` on
     accepts an image without a key at ``/api/ig/relay`` and hosts it for
     15 minutes; the project's public instance is the default. On by default,
     ``ig_relay_enabled`` turns it off, ``ig_relay_url`` points elsewhere.
  4. **A temporary tunnel** — ``posting/ig_tunnel.py``: a throwaway public
     address to a tiny local image server, for when the relay is unreachable.
     Needs the helper downloaded once.

Both publish paths (the Posts module and the artwork poster) call
``host_images``; the reasons each rung declined are carried into the error the
user sees, so "it failed" always says what was tried.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import config
from posting import ig_media

logger = logging.getLogger(__name__)


class NoPublicHost(RuntimeError):
    """No rung of the ladder could give Meta a URL. The message says what was tried."""


@dataclass
class Hosted:
    urls: list[str]
    how: str                                   # local | paired | relay | tunnel
    _closers: list[Callable[[], Awaitable[None] | None]] = field(default_factory=list)

    async def close(self) -> None:
        """Release whatever the rung set up (local stashes, the tunnel). Relayed
        images are the host's to sweep; there is nothing to do for them."""
        for fn in self._closers:
            try:
                r = fn()
                if hasattr(r, "__await__"):
                    await r
            except Exception as e:      # cleanup must never fail a finished publish
                logger.debug("IG host cleanup: %s", e)
        self._closers = []


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(v) if v is not None else True


def _short(e: BaseException) -> str:
    s = str(e).strip() or type(e).__name__
    return s if len(s) <= 140 else s[:137] + "…"


async def upload_to_host(endpoint: str, path: str, api_key: str = "", http=None) -> str:
    """POST one image to a PawPoller image host (``…/api/ig/pubmedia`` with a
    pairing key, or ``…/api/ig/relay`` without) and return the public URL it
    answers with. Raises ``RuntimeError`` carrying the host's own sentence."""
    import httpx
    from pathlib import Path
    data = Path(path).read_bytes()
    headers = {"X-PawPoller-Version": config.APP_VERSION}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    own = http is None
    client = http or httpx.AsyncClient(timeout=90.0)
    try:
        resp = await client.post(endpoint, files={"file": (Path(path).name, data, "application/octet-stream")},
                                 headers=headers)
    except Exception as e:
        raise RuntimeError(f"could not reach {_host_of(endpoint)}: {type(e).__name__}") from e
    finally:
        if own:
            await client.aclose()
    if resp.status_code != 200:
        detail = ""
        try:
            detail = (resp.json() or {}).get("detail") or ""
        except Exception:
            detail = resp.text[:120]
        raise RuntimeError(f"{_host_of(endpoint)} answered HTTP {resp.status_code}"
                           + (f": {detail}" if detail else ""))
    url = (resp.json() or {}).get("url")
    if not url:
        raise RuntimeError(f"{_host_of(endpoint)} gave no image URL back")
    return url


def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc or url
    except Exception:
        return url


def relay_url(settings: dict) -> str:
    return (settings.get("ig_relay_url") or config.IG_RELAY_DEFAULT_URL).strip()


async def host_images(paths: list[str], settings: dict | None = None) -> Hosted:
    """Climb the ladder for *paths*; return the public URLs and how they are hosted.

    Raises :class:`NoPublicHost` when no rung works — its message lists every
    rung tried and why it declined, so the user's error is actionable.
    """
    s = settings if settings is not None else config.get_settings()
    tried: list[str] = []

    # 1. this instance is public
    local_base = (s.get("ig_public_base_url") or "").strip()
    if local_base:
        toks = [ig_media.stash_image(p) for p in paths]
        urls = [ig_media.public_url(local_base, t) for t in toks]
        return Hosted(urls, "local", [lambda: [ig_media.cleanup(t) for t in toks]])

    # 2. a paired server
    paired = (s.get("posting_server_url") or "").strip()
    if paired:
        try:
            key = (s.get("posting_server_api_key") or "").strip()
            urls = [await upload_to_host(paired.rstrip("/") + "/api/ig/pubmedia", p, key) for p in paths]
            return Hosted(urls, "paired")
        except Exception as e:
            tried.append(f"your paired server ({_short(e)})")

    # 3. the PawPoller relay
    if _truthy(s.get("ig_relay_enabled", True)):
        endpoint = relay_url(s)
        try:
            urls = [await upload_to_host(endpoint, p) for p in paths]
            return Hosted(urls, "relay")
        except Exception as e:
            tried.append(f"the PawPoller relay ({_short(e)})")
    else:
        tried.append("the PawPoller relay (turned off in Settings → Posting)")

    # 4. a temporary tunnel
    if _truthy(s.get("ig_tunnel_enabled", True)):
        from posting import ig_tunnel
        st = ig_tunnel.helper_status()
        if not st["supported"]:
            tried.append("a temporary tunnel (no helper build for this machine)")
        elif not st["present"]:
            tried.append("a temporary tunnel (helper not downloaded — Settings → Posting → Instagram image host)")
        else:
            try:
                host = await ig_tunnel.open_public_host()
            except Exception as e:
                tried.append(f"a temporary tunnel ({_short(e)})")
            else:
                toks = [ig_media.stash_image(p) for p in paths]
                urls = [f"{host.base_url}/{t}.jpg" for t in toks]

                async def _close(host=host, toks=toks):
                    await host.close()
                    for t in toks:
                        ig_media.cleanup(t)
                return Hosted(urls, "tunnel", [_close])
    else:
        tried.append("a temporary tunnel (turned off in Settings → Posting)")

    raise NoPublicHost(
        "Instagram needs a public address to fetch the image from, and none worked. Tried "
        + "; ".join(tried)
        + ". On a server set IG_PUBLIC_BASE_URL; on the desktop app see Settings → Posting → "
          "Instagram image host.")
