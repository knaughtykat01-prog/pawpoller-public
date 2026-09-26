"""Process-wide state shared by the mirror's two threads, its routes and the
webhook: which boards need re-reading, when the outbox may send again, and the
last thing that went wrong. In memory on purpose — all of it is recomputed by
the next poll after a restart."""
from __future__ import annotations

import threading
import time

poll_wake = threading.Event()        # a webhook or "refresh now" wants a read soon
outbox_wake = threading.Event()      # a route queued an edit

_lock = threading.Lock()
_dirty_boards: set = set()
_dirty_cards: dict = {}              # card_id -> board_id (comments to re-read)
_outbox_paused_until = 0.0
state = {"last_poll_at": "", "last_error": "", "import": {"done": 0, "total": 0,
                                                             "running": False}}


def mark_board_dirty(board_id: str) -> None:
    if board_id:
        with _lock:
            _dirty_boards.add(board_id)
        poll_wake.set()


def mark_card_dirty(card_id: str, board_id: str) -> None:
    if card_id:
        with _lock:
            _dirty_cards[card_id] = board_id
        poll_wake.set()


def take_dirty() -> tuple[set, dict]:
    with _lock:
        b, c = set(_dirty_boards), dict(_dirty_cards)
        _dirty_boards.clear()
        _dirty_cards.clear()
    return b, c


def pause_outbox(seconds: float) -> None:
    global _outbox_paused_until
    with _lock:
        _outbox_paused_until = max(_outbox_paused_until, time.time() + seconds)


def outbox_paused() -> float:
    return max(0.0, _outbox_paused_until - time.time())


def set_error(message: str) -> None:
    state["last_error"] = message or ""


def kick_outbox() -> None:
    outbox_wake.set()
