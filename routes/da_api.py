"""REST API endpoints for the DeviantArt (DA) analytics dashboard.

Polling uses the official OAuth2 API (2.47.0) -- users register a DA app and
provide its client_id + client_secret plus a target username to track. The
legacy cookie path is a fallback only.

Stats tracked: views, favourites, comments, downloads.
Downloads is unique to DeviantArt among PawPoller platforms.
No thumbnail proxy needed (DA images are served with CORS headers).
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from database.db import get_connection
from database import da_queries
from polling.da_poller import run_da_poll_cycle, da_poll_progress
from polling.background import spawn_poll
from clients.da.client import DAClient
import config

logger = logging.getLogger(__name__)
da_router = APIRouter(prefix="/api/da")


# -- DA Auth -----------------------------------------------------------

@da_router.get("/auth/status")
def da_auth_status():
    """Check whether DA credentials exist and whether there is any DA data."""
    settings = config.get_settings()
    has_credentials = bool(
        settings.get("da_client_id")
        and settings.get("da_client_secret")
        and settings.get("da_target_user")
    )
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM da_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_credentials": has_credentials,
        "has_data": has_data,
        "username": settings.get("da_target_user", ""),
    }


@da_router.post("/auth/connect")
async def da_connect(body: dict):
    """Validate DA app credentials and save to settings.

    Auth flow:
      1. Receive client_id + client_secret + target_user from the frontend
      2. Mint a client-credentials token and confirm the target gallery responds
      3. If validation succeeds, save credentials to settings.json
    """
    client_id = body.get("client_id", "").strip()
    client_secret = body.get("client_secret", "").strip()
    target_user = body.get("target_user", "").strip()

    if not client_id or not client_secret:
        raise HTTPException(400, "client_id and client_secret are required")
    if not target_user:
        raise HTTPException(400, "Target user is required (the DA user to track)")

    # Validate with a throwaway client so we never mutate the shared poll
    # singleton mid-cycle (the OAuth path re-reads creds from settings on every
    # cycle, so there's no live session to preserve). Avoids a connect/poll race.
    client = DAClient(client_id=client_id, client_secret=client_secret,
                      target_user=target_user)
    try:
        valid = await client.validate_credentials()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate credentials: {e}")
    finally:
        await client.close()

    if not valid:
        raise HTTPException(401, "Could not authenticate — check the client_id/client_secret "
                                 "and that the username exists.")

    config.save_settings({
        "da_client_id": client_id,
        "da_client_secret": client_secret,
        "da_target_user": target_user,
        "da_notifications_enabled": True,
    })

    return {"status": "success", "message": f"Connected — tracking {target_user}"}


# -- DA posting authorisation (OAuth Authorization Code) ---------------
#
# Polling and posting need DIFFERENT tokens from the same app, which is the
# thing that makes this confusing:
#
#   polling  client-credentials  — app-only, no user, minted on demand from
#                                  client_id + client_secret. `/auth/connect`
#                                  above does it, and nothing else is needed.
#   posting  authorization_code  — acts AS the user, so DeviantArt requires a
#                                  human to approve it in a browser. That is
#                                  the only way a refresh token is ever issued,
#                                  and it is what `da_refresh_token` holds.
#
# Until 3.9.1 there was no way to do the second one from the app, so posting
# failed with "DeviantArt OAuth not configured" no matter how correctly the
# client_id and secret were entered — the missing piece could not be typed in
# because it does not exist until someone clicks Approve.
#
# The scopes posting actually needs, which is not the same as the scopes it
# looks like it needs:
#
#   stash    an IMAGE is uploaded to Sta.sh first (`oauth_stash_submit`) and
#            published from there (`oauth_stash_publish`). 3.9.1 left this out
#            on the reasoning that an unused scope is a permission for nothing —
#            true in general, wrong here, and DeviantArt said so precisely:
#            403 insufficient_scope, "scope":"stash publish".
#   publish  publishing the deviation, image or literature
#   user     whoami / the metadata reads around a post
#   browse   reading deviations back
#
# ⚠ Adding a scope does NOT upgrade an existing token. A token carries the
# scopes it was granted, so anyone authorised before this change must click
# Authorise posting again — which is what DA's "client needs to re-authorize"
# is telling them.
_DA_SCOPES = "browse user stash publish"

# state → {"at": issued-at, "verifier": PKCE code_verifier}. In-process and
# single-user by design: this is a desktop app and a one-box server, so a store
# that survives a restart would be more machinery than the thing it protects.
# An abandoned authorisation simply expires with the process.
_da_oauth_state: dict[str, dict] = {}
_DA_STATE_TTL = 900  # 15 minutes


def _pkce_pair() -> tuple[str, str]:
    """A PKCE ``(verifier, challenge)`` pair.

    DeviantArt **requires** PKCE on the authorization-code flow — an authorize
    call without ``code_challenge`` is refused outright with
    ``invalid_request: The code_challenge parameter is required.`` (observed
    2026-08-19). It is not optional and there is no legacy path.

    The mechanism: we invent a high-entropy secret (the *verifier*), send only
    its SHA-256 hash (the *challenge*) to the authorize endpoint, and present
    the verifier itself at the token exchange. Whoever redeems the code must
    therefore be whoever started the flow — an intercepted code is useless
    without the verifier, which never travels through the browser.

    Base64url **without padding** is what RFC 7636 specifies; sending the `=`
    padding is a common way to get an opaque `invalid_grant` at exchange time.
    """
    import base64
    import hashlib
    import secrets

    verifier = secrets.token_urlsafe(64)  # 86 chars, inside the 43-128 range
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _da_redirect_uri(request) -> str:
    """The callback URL as the *browser* will see it.

    Behind Caddy + Cloudflare the app sees plain http on an internal port,
    while DeviantArt will redirect to whatever the user's browser used. The
    forwarded headers are the only source that agrees with the address bar, and
    getting this wrong produces DA's "redirect_uri mismatch", which reads like a
    whitelist problem and is not one.
    """
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host:
        host = request.url.netloc
    return f"{proto}://{host}/api/da/auth/callback"


def _da_token_key(account_id: int | None) -> str:
    """Which settings key this account's refresh token belongs in.

    The default account owns the bare ``da_refresh_token`` (it holds the legacy
    flat credentials and the pre-multi-account history); everyone else is
    namespaced ``acct_<id>_da_refresh_token``. That is
    ``config.account_setting_key``'s rule, and writing the bare key for a
    non-default account would silently hand its token to the default one.
    """
    if not account_id:
        return "da_refresh_token"
    from database import accounts as _accts
    conn = get_connection()
    try:
        acct = _accts.get_account(conn, int(account_id))
    finally:
        conn.close()
    if not acct:
        raise HTTPException(404, f"No account {account_id}")
    return config.account_setting_key(
        int(account_id), "da_refresh_token", bool(acct["is_default"]))


def _da_app_creds(account_id: int | None) -> tuple[str, str]:
    """The client_id/secret this account authorises against.

    Multi-account DeviantArt can mean several accounts sharing one registered
    app, or one app each. Resolving per-account and falling back to the flat
    keys covers both without making the operator declare which they are doing.
    """
    settings = config.get_settings()
    if account_id:
        from database import accounts as _accts
        conn = get_connection()
        try:
            acct = _accts.get_account(conn, int(account_id))
        finally:
            conn.close()
        if acct:
            creds = config.resolve_account_credentials(
                "da", int(account_id), bool(acct["is_default"]), settings)
            cid = creds.get("da_client_id") or settings.get("da_client_id", "")
            sec = creds.get("da_client_secret") or settings.get("da_client_secret", "")
            return cid, sec
    return settings.get("da_client_id", ""), settings.get("da_client_secret", "")


@da_router.get("/auth/authorize-url")
def da_authorize_url(request: Request, account_id: int | None = Query(None)):
    """Where to send the browser to approve posting, and what to whitelist.

    Returns the redirect URI as well as the link, because DeviantArt only
    redirects to a URI registered on the app and the value depends on how this
    install is reached — a desktop on localhost and the server behind
    syncopates.app need different entries. Both can be whitelisted at once.
    """
    import secrets
    import time as _time
    import urllib.parse

    client_id, _secret = _da_app_creds(account_id)
    if not client_id:
        raise HTTPException(400, "Connect the DA app first (client_id and client_secret).")
    # Resolved now rather than at the callback: if the account is unknown or the
    # key cannot be worked out, the operator should hear about it before being
    # sent to DeviantArt, not after approving there.
    token_key = _da_token_key(account_id)

    # Drop expired states rather than growing the dict for the life of the
    # process; a stale one is only ever a refusal, never a wrong success.
    now = _time.time()
    for old in [s for s, v in _da_oauth_state.items()
                if now - v.get("at", 0) > _DA_STATE_TTL]:
        _da_oauth_state.pop(old, None)

    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()
    _da_oauth_state[state] = {"at": now, "verifier": verifier,
                              "token_key": token_key, "account_id": account_id}
    redirect_uri = _da_redirect_uri(request)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": _DA_SCOPES,
        "state": state,
        "view": "login",
        # Required by DeviantArt — see _pkce_pair. Only the hash goes out; the
        # verifier stays here and is presented at the exchange.
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return {
        "url": f"https://www.deviantart.com/oauth2/authorize?{urllib.parse.urlencode(params)}",
        "redirect_uri": redirect_uri,
        "scopes": _DA_SCOPES,
        "account_id": account_id,
        "token_key": token_key,
    }


@da_router.get("/auth/callback")
async def da_auth_callback(request: Request, code: str = "", state: str = "",
                           error: str = "", error_description: str = ""):
    """DeviantArt redirects the browser here after approval.

    Returns HTML, not JSON: a person is looking at this tab, not a fetch call.
    """
    def _page(title: str, detail: str, ok: bool) -> HTMLResponse:
        """Render the callback result page.

        ⚠ `title` and `detail` are ESCAPED because both carry attacker-reachable
        text (3.17.3). `detail` is fed from the `error_description` QUERY
        PARAMETER on the refusal branch below, from a remote response body, and
        from exception text — none of which this app controls. Worse, the
        refusal branch runs BEFORE the `state` check, so the single-use token
        gates nothing here: `?error=x&error_description=<form action=...>` was
        enough to render attacker markup on the PawPoller origin.

        The CSP stops it becoming script execution (`script-src 'self'`, no
        `unsafe-inline`), but a CSP is not an excuse for an unescaped sink — an
        injected <form> or <meta refresh> needs no script at all, which is why
        `form-action 'self'` went into `_build_csp` alongside this.
        """
        from html import escape
        colour = "#3fb950" if ok else "#f85149"
        title_s, detail_s = escape(title), escape(detail)
        return HTMLResponse(
            f"<html><head><title>{title_s}</title></head>"
            f"<body style='font-family:system-ui,sans-serif;background:#0d1117;color:#c9d1d9;"
            f"padding:48px;line-height:1.6'>"
            f"<h2 style='color:{colour}'>{title_s}</h2><p>{detail_s}</p>"
            f"<p style='color:#8b949e;font-size:13px'>You can close this tab.</p></body></html>",
            status_code=200 if ok else 400)

    if error:
        return _page("Authorisation refused",
                     f"DeviantArt said: {error_description or error}", False)
    if not code:
        return _page("Authorisation failed", "DeviantArt returned no code.", False)
    # Without this check a link crafted elsewhere could plant someone else's
    # authorisation code here and bind posting to their account.
    if state not in _da_oauth_state:
        return _page("Authorisation failed",
                     "That approval did not come from this install, or it expired. "
                     "Start again from Settings.", False)
    # Popped whether or not the rest succeeds: a state is single-use, and
    # leaving it behind on a failed exchange would allow a replay.
    entry = _da_oauth_state.pop(state, {})
    verifier = entry.get("verifier", "")
    token_key = entry.get("token_key") or "da_refresh_token"
    account_id = entry.get("account_id")

    client_id, client_secret = _da_app_creds(account_id)
    if not client_id or not client_secret:
        return _page("Authorisation failed",
                     "The DA app credentials are no longer in settings.", False)

    import httpx
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post("https://www.deviantart.com/oauth2/token", data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": _da_redirect_uri(request),
                # The other half of PKCE. Without it DA rejects the exchange
                # even though the code itself is valid.
                "code_verifier": verifier,
            })
    except httpx.HTTPError as e:
        return _page("Authorisation failed", f"Could not reach DeviantArt: {e}", False)

    if resp.status_code != 200:
        return _page("Authorisation failed",
                     f"Token exchange returned HTTP {resp.status_code}: {resp.text[:200]}", False)

    data = resp.json()
    refresh = data.get("refresh_token", "")
    if not refresh:
        return _page("Authorisation failed",
                     "DeviantArt issued no refresh token. Check the app's client type is "
                     "<strong>Confidential</strong>.", False)

    # ⚠ WHICH DeviantArt account just approved this?
    #
    # Until 3.32.2 nothing asked. DeviantArt authorises whoever the *browser* is
    # signed in as, not the account whose button was pressed — so approving
    # while signed in as someone else stored that person's token under this
    # account's key, showed a green "authorised" page, and left every post from
    # this account landing on the other one. Exactly the one-session-per-browser
    # trap that FurAffinity cookies have (3.31.0), one step earlier in the flow.
    #
    # This is the last place the mistake is cheap. Refusing here costs one more
    # trip through the browser; storing it costs a post to the wrong gallery,
    # and a confused hunt through the poster and the credential keys — which is
    # what it cost. Checked with the access_token the exchange just returned, so
    # it spends nothing: the refresh token is untouched.
    approved_by = ""
    access = data.get("access_token", "")
    if access:
        try:
            async with httpx.AsyncClient(timeout=20.0) as http:
                who = await http.get(
                    "https://www.deviantart.com/api/v1/oauth2/user/whoami",
                    headers={"Authorization": f"Bearer {access}"})
            if who.status_code == 200:
                approved_by = (who.json() or {}).get("username", "")
        except Exception as e:            # never block on a whoami hiccup
            logger.warning("DA: could not confirm who authorised: %s", e)

    expected = _da_target_user(account_id)
    if approved_by and expected and \
            approved_by.strip().lower() != expected.strip().lower():
        logger.warning("DA: refused an authorisation approved by %s for the account "
                       "configured as %s (key=%s)", approved_by, expected, token_key)
        return _page(
            "Wrong DeviantArt account",
            f"That approval came from <strong>{approved_by}</strong>, but this "
            f"account is <strong>{expected}</strong>. Nothing was saved.<br><br>"
            f"DeviantArt authorises whoever the browser is signed in as — not the "
            f"account you clicked. Open a private window, sign in as "
            f"<strong>{expected}</strong>, and try again.", False)

    config.save_settings({token_key: refresh})
    logger.info("DA: stored a refresh token from the authorisation-code flow "
                "(key=%s, approved by %s)", token_key, approved_by or "unconfirmed")
    who_line = (f" It posts as <strong>{approved_by}</strong>."
                if approved_by else
                " DeviantArt did not say which account approved it — check with "
                "<em>Test</em> on the account row.")
    return _page("DeviantArt posting authorised",
                 "The refresh token is saved. PawPoller renews it by itself from "
                 "here." + who_line, True)


def _da_target_user(account_id: int | None) -> str:
    """The DA username this account is configured as, for identity checks."""
    settings = config.get_settings()
    if account_id is None:
        return settings.get("da_target_user", "") or ""
    try:
        from database import accounts as adb
        from database.db import get_connection
        conn = get_connection()
        try:
            acct = adb.get_account(conn, account_id)
        finally:
            conn.close()
        is_default = bool(acct["is_default"]) if acct else True
        return config.resolve_account_credentials(
            "da", account_id, is_default, settings).get("da_target_user", "") or ""
    except Exception as e:                # identity unknown → do not block
        logger.warning("DA: could not resolve the target user for account %s: %s",
                       account_id, e)
        return ""


@da_router.get("/auth/posting-status")
def da_posting_status(account_id: int | None = Query(None)):
    """Whether posting is configured, as distinct from polling.

    Its own endpoint because the two halves fail independently and the existing
    `/auth/status` only ever described polling — which is why posting could be
    broken while Settings showed DeviantArt as connected.
    """
    settings = config.get_settings()
    client_id, client_secret = _da_app_creds(account_id)
    token_key = _da_token_key(account_id)
    return {
        "has_app": bool(client_id and client_secret),
        "has_refresh_token": bool(settings.get(token_key)),
        "account_id": account_id,
        "token_key": token_key,
    }


@da_router.post("/auth/disconnect")
def da_disconnect():
    """Disconnect DA polling.

    Clears the target user (which flips auth/status to disconnected) and the
    legacy cookie, but deliberately KEEPS da_client_id/da_client_secret: those
    are shared with the DA *poster* (which also refreshes tokens from them), so
    deleting them here could break posting. Reconnecting just re-saves them.
    """
    config.delete_settings_keys(["da_target_user", "da_cookie"])
    config.save_settings({"da_notifications_enabled": False})
    return {"status": "success", "message": "DeviantArt disconnected"}


# -- DA Polling --------------------------------------------------------

@da_router.get("/poll/progress")
def get_da_poll_progress():
    return dict(da_poll_progress)


@da_router.post("/poll/trigger")
async def trigger_da_poll():
    """Manual poll trigger for DA."""
    try:
        spawn_poll(run_da_poll_cycle(), "run_da_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in DA poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@da_router.post("/poll/full-resync")
async def da_full_resync():
    """Force full DA resync."""
    try:
        spawn_poll(run_da_poll_cycle(force_full=True), "run_da_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in DA full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- DA Data -----------------------------------------------------------

@da_router.get("/status")
def get_da_status():
    conn = get_connection()
    try:
        last_poll = da_queries.get_da_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM da_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM da_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/da/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@da_router.get("/summary")
def get_da_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = da_queries.get_da_summary(conn, account_id=account_id)
        # growth_rates stays aggregate (unscoped) — mirrors the IB /summary route.
        summary["growth_rates"] = da_queries.get_da_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/da/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@da_router.get("/submissions")
def get_da_submissions(
    sort_by: str = Query("views", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    rating: str = Query("", description="Filter by rating"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = da_queries.get_all_da_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = da_queries.get_da_submission_deltas(conn)

        if search:
            search_lower = search.lower()
            subs = [s for s in subs if search_lower in s["title"].lower() or search_lower in (s.get("keywords") or "").lower()]
        if rating:
            subs = [s for s in subs if (s.get("rating") or "").lower() == rating.lower()]

        for s in subs:
            d = deltas.get(str(s["submission_id"]), {})
            s["views_delta"] = d.get("views_delta", 0)
            s["faves_delta"] = d.get("faves_delta", 0)
            s["comments_delta"] = d.get("comments_delta", 0)
            s["downloads_delta"] = d.get("downloads_delta", 0)

        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/da/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@da_router.get("/submissions/{submission_id}")
def get_da_submission(submission_id: int):
    conn = get_connection()
    try:
        sub = da_queries.get_da_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="DA deviation not found")
        snapshots = da_queries.get_da_snapshots(conn, submission_id)
        growth_rates = da_queries.get_da_submission_growth_rates(conn, submission_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'da' AND st.submission_id = ?",
                (submission_id,),
            ).fetchall()
        except Exception:
            tags = []
        sub_dict = dict(sub) if not isinstance(sub, dict) else sub
        sub_dict["tags"] = [dict(r) for r in tags]
        return {
            "submission": sub_dict,
            "snapshots": snapshots,
            "growth_rates": growth_rates,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/da/submissions/%s: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@da_router.get("/submissions/{submission_id}/snapshots")
def get_da_submission_snapshots(
    submission_id: int,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": da_queries.get_da_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/da/submissions/%s/snapshots: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@da_router.get("/aggregate")
def get_da_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": da_queries.get_da_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/da/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@da_router.get("/comparison")
def get_da_comparison(
    ids: str = Query(..., description="Comma-separated deviation IDs"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    submission_ids = [int(x.strip()) for x in ids.split(",") if x.strip().isdigit()]
    if len(submission_ids) > 10:
        raise HTTPException(400, "Max 10 deviations for comparison")

    conn = get_connection()
    try:
        data = da_queries.get_da_comparison_snapshots(conn, submission_ids, start, end)
        titles = {}
        for sid in submission_ids:
            sub = da_queries.get_da_submission(conn, sid)
            if sub:
                titles[str(sid)] = sub["title"]
        return {"series": data, "titles": titles}
    except Exception as e:
        logger.error("Error in /api/da/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@da_router.get("/poll_log")
def get_da_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": da_queries.get_da_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/da/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- DA CSV Export -----------------------------------------------------

def _sanitize_csv_value(val):
    """Prevent CSV formula injection — prefix dangerous chars with single quote."""
    if isinstance(val, str) and val and val[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + val
    return val


def _csv_response(rows: list[dict], filename: str) -> StreamingResponse:
    if not rows:
        return StreamingResponse(iter(["No data"]), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows({k: _sanitize_csv_value(v) for k, v in r.items()} for r in rows)
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@da_router.get("/export/submissions")
def export_da_submissions():
    conn = get_connection()
    try:
        subs = da_queries.get_all_da_submissions(conn)
        return _csv_response(subs, "deviantart_submissions.csv")
    finally:
        conn.close()


@da_router.get("/export/snapshots")
def export_da_snapshots(id: int | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = da_queries.get_da_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM da_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"da_snapshots{'_' + str(id) if id else ''}.csv")
    finally:
        conn.close()
