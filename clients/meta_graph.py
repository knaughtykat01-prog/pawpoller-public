"""Shared helpers for the two Meta Graph clients (Instagram and Threads).

Both clients take a **user id** the app puts straight into the request URL —
``/{user_id}/media``, ``/{user_id}/threads`` — and both got that id from a
Settings box labelled "User ID (optional)". A *handle* typed there was accepted
and stored, and then every poll and every post came back:

    Object with ID '<handle>' does not exist, cannot be loaded due to missing
    permissions, or does not support this operation

…which both clients logged as ``auth error (400)``. So the app reported a
credentials failure for a token that was perfectly good, and the account it was
told to read simply never existed (4.3.6).

The two clients are deliberate siblings — same API shape, same auth, written
one after the other — which is exactly why the mistake is in both. These live
here so the third Meta client cannot inherit it a third time.
"""

from __future__ import annotations

from typing import Any


def numeric_id(value: Any) -> str:
    """A Meta user id, or ``""`` for anything that plainly is not one.

    Meta user ids are numeric strings. A handle is not, and it cannot be turned
    into one locally — but the client does not need to guess, because ``/me``
    returns the real id for whatever token it is holding. Blanking a non-id
    here is what makes ``validate_session()`` fall through to asking.
    """
    text = str(value or "").strip().lstrip("@")
    return text if text.isdigit() else ""


def graph_4xx_message(platform: str, resp) -> str:
    """What Meta actually said, instead of calling every 4xx an auth error.

    Meta answers 400 for "I do not know this object" (error code 803) as
    readily as for a dead token, and both clients described the two the same
    way. Same shape as §67 and §68: one sentence for failures the response
    distinguishes, and the sentence pointed at credentials that worked.
    """
    err: Any = {}
    try:
        err = ((resp.json() or {}).get("error") or {}) if resp.content else {}
    except Exception:
        err = {}
    if not isinstance(err, dict):
        err = {}
    msg = str(err.get("message") or (getattr(resp, "text", "") or "")[:200])
    code = err.get("code")
    if code == 803 or "does not exist" in msg.lower():
        return (f"{platform} does not recognise the account id PawPoller is using "
                f"({msg}). That id comes from Settings and must be the NUMERIC user "
                f"id, not a handle — reconnect {platform} and leave the User ID box "
                f"empty, and PawPoller will read the right id from your token.")
    if code == 190:
        return f"{platform} access token expired or invalid: {msg}"
    return (f"{platform} refused the request "
            f"(HTTP {getattr(resp, 'status_code', '?')}, code {code}): {msg}")
