"""FurAffinity stand-down (4.77.6, spec 040): stop polling FA while its Cloudflare bot check turns us away.

FA's third-party policy (June 2026) asks bots to stop while its DDoS mitigation is on and to use a proper
exponential backoff. When the scheduled FA poll meets the bot check (``clients.fa.client.FAChallenged``) it calls
:func:`note_challenge`: FA polling stops for a wait that starts at 2 hours and doubles on each consecutive bot check,
up to a day. Any good FA answer, from a poll, the Test button or a post, calls :func:`clear`: the pause lifts but the
wait is kept, so a bot check on the very next request still doubles it. A whole clean poll calls
:func:`reset`, which forgets it and sends the one "reachable again" note.

The state is one small file of times, so a restart can't be used to hammer FA. Install-wide, not per account: the
block is on the address. Seats on one box each keep their own; none tells another to stand down (spec 040).
Only the scheduled poller lengthens the wait, so a user pressing Test never pushes the next try further out.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

import config

logger = logging.getLogger(__name__)

START_SECONDS = 2 * 3600     # first wait; seats poll every 4-24 h, so this usually skips just the next round
CEILING_SECONDS = 24 * 3600


def _path():
    return config.DATA_DIR / "fa_standdown.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def until() -> float:
    """When the stand-down ends (unix time), 0 when there is none. Clamped: a damaged file or a clock that jumped
    can't hold FA off for longer than the ceiling."""
    try:
        end = float(_load().get("until") or 0)
    except (TypeError, ValueError):
        return 0.0
    return min(end, time.time() + CEILING_SECONDS)


def remaining() -> float:
    """Seconds left in the stand-down, 0 when FA may be polled."""
    return max(until() - time.time(), 0.0)


def next_try_text() -> str:
    end = until()
    return datetime.fromtimestamp(end, config.display_zone()).strftime("%H:%M") if end else ""


def _save(state: dict) -> None:
    try:
        _path().write_text(json.dumps(state), encoding="utf-8")
    except OSError as e:
        logger.warning("FA stand-down couldn't be saved: %s", e)


def note_challenge() -> float:
    """Start or lengthen the stand-down; returns the time it ends. Scheduled poller only.

    The last wait survives :func:`clear`, so a site that challenges only some pages (the login check passes, a
    gallery page doesn't) still doubles towards the ceiling instead of restarting at 2 h every round (review 4.77.6).
    """
    state = _load()
    try:
        last = int(state.get("wait") or 0)
    except (TypeError, ValueError):
        last = 0
    wait = min(last * 2, CEILING_SECONDS) if last > 0 else START_SECONDS
    now = time.time()
    _save({"until": now + wait, "wait": wait, "since": state.get("since") or now})
    logger.warning("FA stand-down: Cloudflare bot check, no FA polling for %d min", wait // 60)
    return now + wait


def clear() -> bool:
    """An FA request got through: lift the pause now (a Test or post can end it), keep the wait. True if it lifted."""
    state = _load()
    if not state.get("until"):
        return False
    state["until"] = 0
    _save(state)
    logger.info("FA stand-down lifted: FurAffinity answered normally")
    return True


async def reset() -> bool:
    """A whole FA poll got through: forget the stand-down, and say so once. True if there was one."""
    if not _path().exists():
        return False
    try:
        _path().unlink()
    except OSError:
        return False
    logger.info("FA stand-down over: a full poll got through")
    try:
        from polling.telegram import PLATFORM_EMOJI, send_telegram
        await send_telegram(f"<b>{PLATFORM_EMOJI.get('fa', '')} FurAffinity is reachable again</b>\n"
                            "  Its bot check has lifted. FA polling is back to normal.")
    except Exception:  # noqa: BLE001 — a lost note must never break the poll
        logger.debug("FA reachable-again note failed", exc_info=True)
    return True
