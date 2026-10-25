"""The Instagram picture relay on its own (spec 048).

Instagram fetches a post's picture from a public web address, and a desktop has
none, so desktops borrow the PawPoller relay (`config.IG_RELAY_DEFAULT_URL`).
That relay used to be the full PawPoller app on the GCP VM, which was stopped on
2026-10-16 after it double-polled the operator's accounts alongside a seat.

This is the relay without the app: its own FastAPI app with exactly three paths
(the relay, the picture links, a health check), reusing the route functions from
`routes.ig_api` as they are. It never imports `dashboard` or `server`, so there is
no poller, scheduler, bot, digest or dashboard to start by mistake: a guard flag
inside `server.py` would be one missed `if` away from running all of them.

    python relay_server.py [--host 0.0.0.0] [--port 8420]

Needs IG_PUBLIC_BASE_URL (https) and an empty data folder of its own; refuses to
start otherwise. Deployed by deploy/relay/docker-compose.yml.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time

from fastapi import FastAPI

import config
import legal
from posting import ig_media
from routes import ig_api

logger = logging.getLogger("relay")


def build_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _nosniff(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app.add_api_route("/api/ig/relay", ig_api.ig_relay, methods=["POST"])
    app.add_api_route("/api/ig/pubmedia/{token}", ig_api.ig_pubmedia, methods=["GET"])
    app.add_api_route("/api/health", lambda: {"status": "ok", "mode": "relay", "version": config.APP_VERSION},
                      methods=["GET"])
    return app


def refusal() -> str | None:
    """Why this relay must not start, or None. One sentence for the log."""
    base = os.environ.get("IG_PUBLIC_BASE_URL", "").strip()
    if not base.startswith("https://"):
        return "Relay not started: set IG_PUBLIC_BASE_URL to the relay's public https address."
    if legal.hosted():
        return "Relay not started: a hosted seat is never a relay."
    real = "Relay not started: this data folder belongs to a PawPoller app ({}). Give the relay its own empty data folder."
    # A real app's folder always has its database; the relay never makes one (security review, spec 048).
    if config.DB_PATH.exists():
        return real.format(config.DB_PATH.name)
    s = config.get_settings()
    # An app's vault this relay can't open (no key) reads as no credentials, and the first
    # save_settings() would then set that vault aside. Refuse before anything is written.
    if config._vault_unreadable:
        return real.format("a locked credential vault")
    for field in sorted(config.CREDENTIAL_FIELDS):
        if s.get(field):
            # A credential means it was pointed at a real app's data folder. Name it, never its value.
            return (f"Relay not started: this data folder holds account credentials ({field}). "
                    "Give the relay its own empty data folder.")
    return None


def _sweep_forever() -> None:
    # Files leave disk on time even when nobody uploads (path_for already refuses them), and
    # addresses leave memory at the end of their 10-minute window, as the privacy policy says.
    while True:
        time.sleep(60)
        try:
            ig_media.sweep()
            ig_api.forget_old_addresses()
        except Exception:
            logger.exception("relay sweep failed")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PawPoller's Instagram picture relay, on its own.")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8420)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    why = refusal()
    if why:
        logger.error(why)
        return 1
    # The route functions read these from settings; set here, never on a settings screen.
    config.save_settings({"ig_relay_open": True,
                          "ig_public_base_url": os.environ["IG_PUBLIC_BASE_URL"].strip().rstrip("/")})
    threading.Thread(target=_sweep_forever, name="relay-sweep", daemon=True).start()
    import uvicorn
    logger.info("PawPoller relay %s on %s:%d", config.APP_VERSION, args.host, args.port)
    # No access log: it would keep every sender's address, and the privacy policy says the
    # relay holds an address only for its 10-minute rate window.
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="info", access_log=False,
                proxy_headers=True, forwarded_allow_ips=config.DASHBOARD_FORWARDED_IPS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
