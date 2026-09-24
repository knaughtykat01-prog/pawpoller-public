"""Trello REST client — the slice the commissions sync needs, and nothing else.

Spec: `specs/005-trello-commissions/contracts/api.md`.

Auth is a ``key`` + ``token`` pair on **every** request as query parameters. No
OAuth dance, no refresh, no expiry to track — which is why it drops straight into
the existing credential vault and the per-account **Test** button with no new
mechanism.

Synchronous on purpose: the sync runs in a daemon thread beside the posting and
auto-backup schedulers, so there is no event loop to join and nothing to gain from
async here.

⚠ **There is no delete method, and there must never be one.** The feature archives
cards (``closed=true``); it does not delete them. A method that does not exist
cannot be called by mistake, which is a stronger guarantee than a comment. Trello's
`DELETE /cards/{id}` is immediate and the API offers no undo.

⚠ **Nothing in this module may put the key or the token in an exception message, a
log line or a returned error string.** Trello echoes query parameters in some error
bodies, so the upstream body is never passed through verbatim.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.trello.com/1"
HTTP_TIMEOUT = 30.0

# The only card fields this feature reads. Requesting a narrow list is not an
# optimisation — it is what stops an unrequested field drifting into a comparison
# and being reported as a change nobody made.
CARD_FIELDS = "id,name,desc,due,closed,idList,dateLastActivity,shortUrl"


class TrelloError(Exception):
    """Base — something went wrong talking to Trello."""


class TrelloAuthError(TrelloError):
    """The key/token was refused.

    ⚠ Kept distinct from every other failure because of what the sync does next: a
    board read that fails MUST NOT be read as "the board is empty", which would
    unlink every commission on it. See research R7.
    """


class TrelloRetryableError(TrelloError):
    """A rate limit, a timeout or a network error — try again later, change nothing now."""


def _redact(text: str, *secrets: str) -> str:
    """Strip anything secret out of an upstream message before it travels.

    ⚠ Kept deliberately although nothing calls it today: the 4xx handler used to
    quote Trello's body through this, and stopped because redacting the credential
    is not enough -- the body also echoes the card, whose title is the client's
    name. Anything that wants to quote upstream text again needs this AND an
    answer for that.
    """
    out = text or ""
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "***")
    return out


class TrelloClient:
    def __init__(self, key: str, token: str):
        self.key = (key or "").strip()
        self.token = (token or "").strip()
        if not self.key or not self.token:
            raise ValueError("Trello needs both an API key and a token.")

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _auth(self) -> dict:
        return {"key": self.key, "token": self.token}

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 data: dict | None = None):
        url = f"{API_BASE}{path}"
        q = self._auth()
        q.update(params or {})
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT) as client:
                r = client.request(method, url, params=q, data=data or None)
        except httpx.TimeoutException as e:
            raise TrelloRetryableError("Trello did not answer in time.") from e
        except httpx.HTTPError as e:
            raise TrelloRetryableError(
                f"Could not reach Trello: {type(e).__name__}.") from e

        if r.status_code in (401, 403):
            # The body can echo the credential back. Never pass it through.
            raise TrelloAuthError(
                "Trello did not accept that API key and token. Check them in "
                "Settings -> Trello.")
        if r.status_code == 429:
            raise TrelloRetryableError(
                "Trello is rate-limiting this key. The sync stopped; nothing was "
                "half-written.")
        if r.status_code >= 500:
            raise TrelloRetryableError(f"Trello returned a server error ({r.status_code}).")
        if r.status_code >= 400:
            # ⚠ The body is deliberately NOT included. A 4xx from a card write
            # echoes the card back, and a card's title is the CLIENT'S NAME -- so
            # this message, which reaches the sync report and the log, would carry
            # personal data out of the app. The status code plus the operation is
            # what is actionable anyway; 401/403/429/5xx already say more.
            raise TrelloError(f"Trello refused the request ({r.status_code}).")

        if not r.content:
            return None
        try:
            return r.json()
        except ValueError as e:
            # A 200 that is not JSON is not an empty board — it is a broken read,
            # and it must abort rather than look like "no cards".
            raise TrelloRetryableError("Trello returned a response we could not read.") from e

    # ── reads ────────────────────────────────────────────────────────────────

    def me(self) -> dict:
        """Whose credentials these are. The whole of ``POST /api/trello/test``."""
        d = self._request("GET", "/members/me",
                          params={"fields": "id,username,fullName"}) or {}
        return {"id": d.get("id", ""), "username": d.get("username", ""),
                "full_name": d.get("fullName", "")}

    def boards(self) -> list[dict]:
        rows = self._request("GET", "/members/me/boards",
                             params={"fields": "id,name,closed", "filter": "open"}) or []
        return [{"id": b.get("id", ""), "name": b.get("name", ""),
                 "closed": bool(b.get("closed"))} for b in rows]

    def lists(self, board_id: str) -> list[dict]:
        rows = self._request("GET", f"/boards/{board_id}/lists",
                             params={"fields": "id,name,pos", "filter": "open"}) or []
        return [{"id": l.get("id", ""), "name": l.get("name", ""),
                 "pos": l.get("pos", 0)} for l in rows]

    def cards(self, board_id: str) -> list[dict]:
        """Every open card on the board — **one request per sync**, whatever the size.

        Archived cards are excluded from the default filter, which is why an
        archived card reads as absent; the sync treats a linked card's absence as
        an unlink only when the read itself succeeded (research R7).
        """
        rows = self._request("GET", f"/boards/{board_id}/cards",
                             params={"fields": CARD_FIELDS}) or []
        return [self._card(c) for c in rows]

    def card(self, card_id: str) -> dict | None:
        d = self._request("GET", f"/cards/{card_id}", params={"fields": CARD_FIELDS})
        return self._card(d) if d else None

    @staticmethod
    def _card(c: dict) -> dict:
        return {
            "id": c.get("id", ""),
            "name": c.get("name", "") or "",
            "desc": c.get("desc", "") or "",
            "due": c.get("due") or "",
            "closed": bool(c.get("closed")),
            "id_list": c.get("idList", "") or "",
            "last_activity": c.get("dateLastActivity", "") or "",
            "url": c.get("shortUrl", "") or "",
        }

    # ── writes ───────────────────────────────────────────────────────────────

    def create_card(self, *, id_list: str, name: str, desc: str = "",
                    due: str = "") -> dict:
        data = {"idList": id_list, "name": name, "desc": desc}
        if due:
            data["due"] = due
        return self._card(self._request("POST", "/cards", data=data) or {})

    def update_card(self, card_id: str, **fields) -> dict:
        """Write only the fields given.

        ⚠ The caller passes exactly what changed. Sending the full card on a
        status-only move would rewrite ``name`` and ``desc`` with whatever we last
        read, quietly reverting an edit made on the board between syncs (FR-007).
        """
        allowed = {"name", "desc", "due", "idList", "closed"}
        data = {k: v for k, v in fields.items() if k in allowed}
        if not data:
            return {}
        if "closed" in data:
            data["closed"] = "true" if data["closed"] else "false"
        return self._card(self._request("PUT", f"/cards/{card_id}", data=data) or {})

    async def validate_session(self) -> str:
        """For the per-account credential Test (4.30.0) -- the `session_str` probe.

        The client is synchronous because the sync runs in a thread; the probe
        registry is async, so the one call it needs is bridged rather than
        duplicating the client in async form. Returns the username, or "" when the
        credentials are refused.
        """
        import asyncio
        try:
            me = await asyncio.to_thread(self.me)
        except TrelloAuthError:
            return ""
        return me.get("username") or me.get("full_name") or ""

    def archive_card(self, card_id: str) -> dict:
        """Archive — never delete. This is the only removal this client can perform."""
        return self.update_card(card_id, closed=True)
