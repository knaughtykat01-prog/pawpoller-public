"""Trello REST client — the board mirror's whole view of Trello (spec 006).

Specs: `specs/005-trello-commissions/`, `specs/006-trello-board-mirror/`
(research R2–R8 carry the sources for every endpoint below).

Auth is a ``key`` + ``token`` pair on every request as query parameters, except
the attachment **download** route, which refuses query-param auth since
2021-01-25 and needs an ``Authorization: OAuth …`` header (research R8).

Synchronous on purpose: the mirror runs in daemon threads beside the posting and
auto-backup schedulers, so there is no event loop to join.

⚠ **No board, list or card can be deleted through this client.** Trello's
``DELETE /cards/{id}`` is immediate and has no undo the API can reach, so boards,
lists and cards are archived (``closed=true``) instead (FR-024). ``_request``
refuses any DELETE whose path is not on ``_DELETABLE`` — labels, checklists,
check items, comments and webhooks, which Trello gives no archive for (FR-027).
A guard in the one place every request passes is a stronger guarantee than a
convention across twenty methods.

⚠ **Nothing in this module may put the key, the token or the secret in an
exception message, a log line or a returned error string.** Trello echoes query
parameters in some error bodies, and a 4xx on a card write echoes the card —
whose title can be a client's name — so the upstream body is never passed on.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from collections import deque

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.trello.com/1"
HTTP_TIMEOUT = 30.0

# The one nested read that reconciles a whole board (research R2). `labels_limit`
# defaults to 50 and `actions` defaults to `all` on this route, so both are
# passed explicitly — the first would silently drop labels, the second would pull
# the activity feed into every poll.
BOARD_READ = {
    "fields": "id,name,closed,url,dateLastActivity,prefs",
    "lists": "all", "list_fields": "id,name,pos,closed",
    "cards": "all",
    "card_fields": "id,name,desc,pos,closed,idList,idLabels,start,due,dueComplete,"
                   "cover,shortUrl,dateLastActivity,badges",
    "card_attachments": "cover",
    "card_attachment_fields": "id,url,name,fileName,mimeType,bytes",
    "labels": "all", "labels_limit": "1000", "label_fields": "id,name,color",
    "checklists": "all", "checklist_fields": "id,idCard,name,pos",
    "actions": "none",
}

# DELETE is only ever sent to these (FR-027). Boards, lists and cards are absent
# on purpose: they archive.
_DELETABLE = re.compile(
    r"^/(labels|checklists|actions|webhooks)/[^/]+$"
    r"|^/cards/[^/]+/(idLabels|checkItem)/[^/]+$"
    r"|^/checklists/[^/]+/checkItems/[^/]+$")

COVER_COLOURS = ("pink", "yellow", "lime", "blue", "black", "orange", "red",
                 "purple", "sky", "green")


class TrelloError(Exception):
    """Base — something went wrong talking to Trello."""


class TrelloAuthError(TrelloError):
    """The key/token was refused.

    ⚠ Kept distinct from every other failure because of what the mirror does
    next: a board read that fails MUST NOT be read as "the board is empty", which
    would mark every card on it removed (FR-026).
    """


class TrelloRetryableError(TrelloError):
    """A rate limit, a timeout or a network error — try again later, change nothing now."""

    def __init__(self, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = retry_after


class TrelloNotFound(TrelloError):
    """404 — the object is gone on Trello's side."""


# ── rate limiting (research R7) ──────────────────────────────────────────────
#
# Trello documents 100 requests / 10 s per token and 300 / 10 s per key, and
# blocks a key for the rest of the window after 200 429s. One limiter for the
# whole process — the poller, the outbox worker and cover downloads share the
# token — kept at 80 so we never approach the documented ceiling.

class _Limiter:
    def __init__(self, limit: int = 80, window: float = 10.0):
        self.limit, self.window = limit, window
        self._stamps: deque = deque()
        self._lock = threading.Lock()
        self._blocked_until = 0.0

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] > self.window:
                    self._stamps.popleft()
                wait = max(0.0, self._blocked_until - now)
                if not wait and len(self._stamps) < self.limit:
                    self._stamps.append(now)
                    return
                if not wait:
                    wait = self.window - (now - self._stamps[0]) + 0.05
            time.sleep(min(wait, self.window))

    def back_off(self, seconds: float) -> None:
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)


LIMITER = _Limiter()


def verify_webhook(body: bytes, callback_url: str, secret: str, header: str) -> bool:
    """Trello's webhook signature: base64 HMAC-SHA1 of ``body + callbackURL``,
    keyed with the application Secret (research R3). Constant-time compare."""
    if not (secret and header):
        return False
    mac = hmac.new(secret.encode("utf-8"), body + callback_url.encode("utf-8"),
                   hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode("ascii"), header.strip())


class TrelloClient:
    def __init__(self, key: str, token: str):
        self.key = (key or "").strip()
        self.token = (token or "").strip()
        if not self.key or not self.token:
            raise ValueError("Trello needs both an API key and a token.")

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 data: dict | None = None, json_body: dict | None = None,
                 files: dict | None = None):
        if method == "DELETE" and not _DELETABLE.match(path):
            # Not a Trello error: a bug in the caller, refused before the network.
            raise ValueError("Boards, lists and cards are archived, never deleted.")
        q = {"key": self.key, "token": self.token}
        q.update(params or {})
        LIMITER.acquire()
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT) as client:
                r = client.request(method, f"{API_BASE}{path}", params=q,
                                   data=data or None, json=json_body, files=files)
        except httpx.TimeoutException as e:
            raise TrelloRetryableError("Trello did not answer in time.") from e
        except httpx.HTTPError as e:
            raise TrelloRetryableError(
                f"Could not reach Trello: {type(e).__name__}.") from e
        return self._result(r)

    @staticmethod
    def _result(r: httpx.Response):
        if r.status_code in (401, 403):
            # The body can echo the credential back. Never pass it through.
            raise TrelloAuthError(
                "Trello did not accept that API key and token. Check them in "
                "Settings -> Trello.")
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("Retry-After") or 10)
            except ValueError:
                wait = 10.0
            LIMITER.back_off(wait)
            raise TrelloRetryableError("Trello is rate-limiting this key; waiting.",
                                       retry_after=wait)
        if r.status_code >= 500:
            raise TrelloRetryableError(f"Trello returned a server error ({r.status_code}).")
        if r.status_code == 404:
            raise TrelloNotFound("Trello says that item no longer exists.")
        if r.status_code >= 400:
            # ⚠ Body deliberately NOT included — see the module docstring.
            raise TrelloError(f"Trello refused the request ({r.status_code}).")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError as e:
            # A 200 that is not JSON is a broken read, never an empty board.
            raise TrelloRetryableError("Trello returned a response we could not read.") from e

    # ── identity ─────────────────────────────────────────────────────────────

    def me(self) -> dict:
        """Whose credentials these are."""
        d = self._request("GET", "/members/me",
                          params={"fields": "id,username,fullName"}) or {}
        return {"id": d.get("id", ""), "username": d.get("username", ""),
                "full_name": d.get("fullName", "")}

    def inbox_board_id(self) -> str:
        """Trello's personal Inbox board, if the API exposes it (undocumented, R3)."""
        try:
            d = self._request("GET", "/members/me", params={"fields": "inbox"}) or {}
        except TrelloError:
            return ""
        inbox = d.get("inbox") or {}
        return str(inbox.get("idBoard") or "") if isinstance(inbox, dict) else ""

    async def validate_session(self) -> str:
        """For the per-account credential Test (4.30.0). Username, or "" if refused."""
        import asyncio
        try:
            me = await asyncio.to_thread(self.me)
        except TrelloAuthError:
            return ""
        return me.get("username") or me.get("full_name") or ""

    # ── reads ────────────────────────────────────────────────────────────────

    def member_boards(self) -> list[dict]:
        """Every board the member belongs to, open AND closed — one request.

        ``filter=all`` so a board that merely closed still appears (closed), and
        one that is absent from a successful read was deleted (research R2). A
        nested ``/members/me/…`` route, so it does not count against the
        ``/1/members/`` 100-per-900 s limit.
        """
        return self._request("GET", "/members/me/boards", params={
            "filter": "all", "fields": "id,name,closed,url,dateLastActivity,prefs"}) or []

    def board_full(self, board_id: str) -> dict:
        """One request: board, lists, cards, labels, checklists, cover attachments."""
        d = self._request("GET", f"/boards/{board_id}", params=BOARD_READ)
        if not isinstance(d, dict) or d.get("id") != board_id:
            raise TrelloRetryableError("Trello returned a board we could not read.")
        return d

    def board_comments(self, board_id: str, since: str = "") -> list[dict]:
        """``commentCard`` actions, newest first, paged back to ``since``."""
        out: list[dict] = []
        before = ""
        for _ in range(20):                     # 20 000 comments is a ceiling, not a target
            params = {"filter": "commentCard", "limit": "1000",
                      "fields": "id,data,date,idMemberCreator",
                      "memberCreator_fields": "fullName,username"}
            if since:
                params["since"] = since
            if before:
                params["before"] = before
            page = self._request("GET", f"/boards/{board_id}/actions", params=params) or []
            out.extend(page)
            if len(page) < 1000:
                break
            before = page[-1].get("id", "")
        return out

    def card_comments(self, card_id: str) -> list[dict]:
        """A card's comments. Polling cannot see comment edits or deletions on
        the board feed (research R2); an opened card re-reads its own."""
        out: list[dict] = []
        for page in range(19):                  # the documented page ceiling
            rows = self._request("GET", f"/cards/{card_id}/actions", params={
                "filter": "commentCard", "page": str(page),
                "fields": "id,data,date,idMemberCreator",
                "memberCreator_fields": "fullName,username"}) or []
            out.extend(rows)
            if len(rows) < 50:
                break
        return out

    def download(self, url: str, max_bytes: int = 15 * 1024 * 1024) -> bytes:
        """An attachment's bytes. Header auth only — query auth is refused (R8)."""
        # ⚠ The header carries the key AND token. The URL comes from attachment data
        # on the board, and a link attachment's URL can point anywhere — so only
        # Trello's own host ever receives it. (httpx also drops Authorization on a
        # cross-host redirect.)
        host = (httpx.URL(url).host or "").lower() if url.startswith("https://") else ""
        if host not in ("trello.com", "api.trello.com"):
            raise TrelloError("Not a Trello download URL.")
        header = (f'OAuth oauth_consumer_key="{self.key}", '
                  f'oauth_token="{self.token}"')
        LIMITER.acquire()
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
                r = client.get(url, headers={"Authorization": header})
        except httpx.HTTPError as e:
            raise TrelloRetryableError(
                f"Could not reach Trello: {type(e).__name__}.") from e
        if r.status_code >= 400:
            self._result(r)
        if len(r.content) > max_bytes:
            raise TrelloError("That cover image is too large to keep.")
        return r.content

    # ── writes: cards ────────────────────────────────────────────────────────

    def create_card(self, *, id_list: str, name: str, desc: str = "",
                    pos="bottom", due: str = "") -> dict:
        data = {"idList": id_list, "name": name, "desc": desc, "pos": str(pos)}
        if due:
            data["due"] = due
        return self._request("POST", "/cards", data=data) or {}

    def update_card(self, card_id: str, **fields) -> dict:
        """Write only the fields given (Trello names).

        ⚠ The caller passes exactly what changed. Sending the whole card on a move
        would rewrite ``name`` and ``desc`` with what we last read, quietly
        reverting an edit made on the board in between.
        """
        allowed = {"name", "desc", "due", "start", "dueComplete", "idList", "pos",
                   "closed", "idBoard"}
        data = {k: v for k, v in fields.items() if k in allowed}
        if not data:
            return {}
        for k in ("closed", "dueComplete"):
            if k in data:
                data[k] = "true" if data[k] else "false"
        for k in ("due", "start"):
            if k in data and not data[k]:
                data[k] = "null"
        if "pos" in data:
            data["pos"] = str(data["pos"])
        return self._request("PUT", f"/cards/{card_id}", data=data) or {}

    def archive_card(self, card_id: str) -> dict:
        """Archive — never delete."""
        return self.update_card(card_id, closed=True)

    def set_cover(self, card_id: str, cover: dict) -> dict:
        """Colour cover, or ``{}`` to remove. JSON body (research R8, S-C1)."""
        body = {"cover": {k: v for k, v in (cover or {}).items()
                          if k in ("color", "size", "brightness", "idAttachment")}}
        if not body["cover"]:
            body = {"cover": ""}
        return self._request("PUT", f"/cards/{card_id}", json_body=body) or {}

    def upload_cover(self, card_id: str, filename: str, content: bytes, mime: str) -> dict:
        return self._request("POST", f"/cards/{card_id}/attachments",
                             data={"setCover": "true", "name": filename},
                             files={"file": (filename, content, mime)}) or {}

    def add_label(self, card_id: str, label_id: str):
        return self._request("POST", f"/cards/{card_id}/idLabels", params={"value": label_id})

    def remove_label(self, card_id: str, label_id: str):
        return self._request("DELETE", f"/cards/{card_id}/idLabels/{label_id}")

    # ── writes: lists ────────────────────────────────────────────────────────

    def create_list(self, board_id: str, name: str, pos="bottom") -> dict:
        return self._request("POST", "/lists", data={
            "idBoard": board_id, "name": name, "pos": str(pos)}) or {}

    def update_list(self, list_id: str, **fields) -> dict:
        data = {k: v for k, v in fields.items() if k in ("name", "pos", "closed")}
        if "closed" in data:
            data["closed"] = "true" if data["closed"] else "false"
        if "pos" in data:
            data["pos"] = str(data["pos"])
        return self._request("PUT", f"/lists/{list_id}", data=data) or {}

    # ── writes: labels ───────────────────────────────────────────────────────

    def create_label(self, board_id: str, name: str, color) -> dict:
        return self._request("POST", "/labels", params={
            "idBoard": board_id, "name": name, "color": color or "null"}) or {}

    def update_label(self, label_id: str, **fields) -> dict:
        params = {}
        if "name" in fields:
            params["name"] = fields["name"]
        if "color" in fields:
            params["color"] = fields["color"] or "null"
        return self._request("PUT", f"/labels/{label_id}", params=params) or {}

    def delete_label(self, label_id: str):
        return self._request("DELETE", f"/labels/{label_id}")

    # ── writes: checklists ───────────────────────────────────────────────────

    def create_checklist(self, card_id: str, name: str, pos="bottom") -> dict:
        return self._request("POST", "/checklists", params={
            "idCard": card_id, "name": name, "pos": str(pos)}) or {}

    def update_checklist(self, checklist_id: str, **fields) -> dict:
        params = {k: str(v) for k, v in fields.items() if k in ("name", "pos")}
        return self._request("PUT", f"/checklists/{checklist_id}", params=params) or {}

    def delete_checklist(self, checklist_id: str):
        return self._request("DELETE", f"/checklists/{checklist_id}")

    def create_check_item(self, checklist_id: str, name: str, pos="bottom") -> dict:
        return self._request("POST", f"/checklists/{checklist_id}/checkItems",
                             params={"name": name, "pos": str(pos)}) or {}

    def update_check_item(self, card_id: str, item_id: str, **fields) -> dict:
        params = {k: str(v) for k, v in fields.items()
                  if k in ("name", "state", "pos", "idChecklist")}
        return self._request("PUT", f"/cards/{card_id}/checkItem/{item_id}",
                             params=params) or {}

    def delete_check_item(self, checklist_id: str, item_id: str):
        return self._request("DELETE", f"/checklists/{checklist_id}/checkItems/{item_id}")

    # ── writes: comments ─────────────────────────────────────────────────────

    def add_comment(self, card_id: str, text: str) -> dict:
        return self._request("POST", f"/cards/{card_id}/actions/comments",
                             params={"text": text}) or {}

    def edit_comment(self, action_id: str, text: str) -> dict:
        return self._request("PUT", f"/actions/{action_id}", params={"text": text}) or {}

    def delete_comment(self, action_id: str):
        return self._request("DELETE", f"/actions/{action_id}")

    # ── webhooks (research R3) ───────────────────────────────────────────────

    def create_webhook(self, board_id: str, callback_url: str) -> dict:
        return self._request("POST", f"/tokens/{self.token}/webhooks", data={
            "idModel": board_id, "callbackURL": callback_url,
            "description": "PawPoller board mirror"}) or {}

    def list_webhooks(self) -> list[dict]:
        return self._request("GET", f"/tokens/{self.token}/webhooks") or []

    def delete_webhook(self, webhook_id: str):
        return self._request("DELETE", f"/webhooks/{webhook_id}")


def dumps(value) -> str:
    """Stable JSON for stored ``agreed`` values and comparisons."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
