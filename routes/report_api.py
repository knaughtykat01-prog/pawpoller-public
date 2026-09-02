"""Error reports — the "Send to dev" button on the error popup (2.159.0).

When an API action fails, the frontend shows an achievement-style error card
(frontend/js/error_popup.js) with a "Send to dev" button. That button POSTs
here, and the report rides the instance's own Telegram notifier
(polling/telegram.py) — so on this self-hosted model "the dev" is whoever
runs the instance and already receives its poll alerts. No third-party
telemetry, nothing leaves the box unless Telegram is configured; the report
is always written to the server log regardless, so it survives even on
installs with Telegram off.

Field caps keep a hostile/buggy client from flooding the log or blowing
Telegram's 4096-char message limit, and everything user-supplied is
HTML-escaped before hitting sendMessage (parse_mode=HTML).
"""
import html
import logging

from fastapi import APIRouter
from pydantic import BaseModel

import config
from polling.telegram import send_telegram

logger = logging.getLogger(__name__)

report_router = APIRouter(prefix="/api", tags=["report"])


class ErrorReport(BaseModel):
    context: str = ""   # what failed, e.g. "POST /api/artwork/import"
    message: str = ""   # the cleaned, human-readable error message
    detail: str = ""    # raw response body / stack-ish detail
    url: str = ""       # the SPA route (location.hash) when it happened
    version: str = ""   # frontend's idea of the app version
    ua: str = ""        # browser user-agent


def _clip(value, cap: int) -> str:
    return str(value or "").strip()[:cap]


@report_router.post("/report-error")
async def report_error(report: ErrorReport):
    """Log an error report and forward it to the instance's Telegram.

    Returns ``{sent: bool}`` — false when Telegram is off/unconfigured or the
    send failed. The frontend uses that to tell the user whether the report
    actually went anywhere.
    """
    ctx = _clip(report.context, 200)
    msg = _clip(report.message, 500)
    detail = _clip(report.detail, 1200)
    url = _clip(report.url, 200)
    version = _clip(report.version, 40) or config.APP_VERSION
    ua = _clip(report.ua, 200)

    # Always in the server log, even when Telegram never fires.
    logger.error("User error report [%s] at %s (v%s): %s | %s",
                 ctx or "unknown", url or "-", version, msg, detail)

    e = html.escape
    lines = [
        "🚨 <b>PawPoller error report</b>",
        f"<b>Where:</b> {e(ctx)}" if ctx else "",
        f"<b>Screen:</b> {e(url)}" if url else "",
        f"<b>Version:</b> {e(version)}",
        f"<b>Message:</b> {e(msg)}" if msg else "",
        f"<pre>{e(detail)}</pre>" if detail else "",
        f"<i>{e(ua)}</i>" if ua else "",
    ]
    sent = await send_telegram("\n".join(line for line in lines if line))
    return {"sent": sent}
