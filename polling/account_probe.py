"""Per-account credential test (spec 001-account-cred-test).

Routes the Accounts-page **Test** button to each platform's *existing* credential validator, for every
platform beyond fa/da/tw (which keep their own richer branches in ``routes/settings_api.py``). For one
account it:

- builds a **fresh, throwaway** client from that account's resolved credentials — never a poller's cached
  singleton, which would re-point a live poll at the wrong account (the multi-account offset hazard);
- calls the platform's validator and normalises the answer to the endpoint's status vocabulary
  (``ok`` / ``invalid`` / ``wrong_account`` / ``unconfigured`` / ``error``);
- for the two platforms whose OAuth refresh token *rotates* on use (SoundCloud, FurryNetwork), persists the
  rotated token back to **this** account, so a test can never invalidate the credential it checks — "a rotated
  token that is not written down is a dead token".

A transient block / rate-limit / network failure becomes ``error`` (try again later), never ``invalid``, so a
temporarily-blocked site is never misreported as an expired credential.
"""
from __future__ import annotations

import logging

import config
from polling.cf_proxy import proxy_kwargs

logger = logging.getLogger(__name__)

_LABELS = {
    "ao3": "AO3", "sqw": "SquidgeWorld", "sf": "SoFurry", "e621": "e621", "fbr": "Furbooru",
    "mast": "Mastodon", "tum": "Tumblr", "bsky": "Bluesky", "pix": "Pixiv", "yt": "YouTube",
    "ig": "Instagram", "thr": "Threads", "ng": "Newgrounds", "sc": "SoundCloud", "fn": "FurryNetwork",
    "ik": "Itaku", "wp": "Wattpad", "ws": "Weasyl", "ib": "Inkbunny",
}


# ── verdict helpers ───────────────────────────────────────────
def _ok(username, detail=""):
    username = username or ""
    return {"status": "ok", "username": username,
            "detail": detail or (f"Logged in as {username}." if username else "Credentials valid.")}


def _invalid(detail):
    return {"status": "invalid", "detail": detail}


def _error(detail):
    return {"status": "error", "detail": detail}


def _unconfigured(detail):
    return {"status": "unconfigured", "detail": detail}


def _classify_exc(platform, e) -> dict:
    """Map an exception raised during validation to a verdict.

    A real auth failure (FurryNetwork's ``FnAuthError``) is an expired/invalid credential. Everything
    else — AO3's block ``RuntimeError``, Instagram/Threads' ``IgAuthError`` (app-block / rate-limit, NOT a
    dead token), a network error — is *temporary*: report ``error`` so we never falsely tell the operator a
    working credential has expired.
    """
    name = type(e).__name__
    msg = str(e).strip()
    if name == "FnAuthError":
        return _invalid(msg or "FurryNetwork rejected the credentials — re-enter the refresh token.")
    if name == "IgAuthError":
        return _error(msg or "Temporarily unavailable — try again shortly.")
    if name == "RuntimeError":
        return _error(msg or "The site is temporarily refusing the check — try again later.")
    logger.debug("%s test: unexpected %s during validation", platform, name, exc_info=True)
    return _error(msg or f"Could not complete the check ({name}).")


# ── fresh client builders (import inside so tests can monkeypatch the class) ──
def _b_ao3(c, pk):
    from clients.ao3.client import AO3Client
    return AO3Client(username=c.get("ao3_username", ""), password=c.get("ao3_password", ""),
                     target_user=c.get("ao3_target_user", ""), session_cookie=c.get("ao3_session_cookie", ""), **pk)


def _b_sqw(c, pk):
    from clients.sqw.client import SquidgeWorldClient
    return SquidgeWorldClient(username=c.get("sqw_username", ""), password=c.get("sqw_password", ""),
                              target_user=c.get("sqw_target_user", ""), **pk)


def _b_sf(c, pk):
    from clients.sf.client import SoFurryClient
    return SoFurryClient(api_token=c.get("sf_api_token", ""), display_name=c.get("sf_display_name", ""), **pk)


def _b_e621(c, pk):
    from clients.e621.client import E621Client
    return E621Client(username=c.get("e621_username", ""), api_key=c.get("e621_api_key", ""), **pk)


def _b_fbr(c, pk):
    from clients.fbr.client import FurbooruClient
    return FurbooruClient(username=c.get("fbr_username", ""), api_key=c.get("fbr_api_key", ""))


def _b_mast(c, pk):
    from clients.mast.client import MastClient
    return MastClient(instance_url=c.get("mast_instance_url", ""), access_token=c.get("mast_access_token", ""), **pk)


def _b_tum(c, pk):
    from clients.tum.client import TumClient
    return TumClient(api_key=c.get("tum_api_key", ""), blog=c.get("tum_blog", ""), **pk)


def _b_bsky(c, pk):
    from clients.bsky.client import BskyClient
    return BskyClient(identifier=c.get("bsky_identifier", ""), app_password=c.get("bsky_app_password", ""), **pk)


def _b_pix(c, pk):
    from clients.pix.client import PixClient
    return PixClient(refresh_token=c.get("pix_refresh_token", ""), user_id=c.get("pix_user_id", ""), **pk)


def _b_yt(c, pk):
    from clients.yt.client import YtClient
    try:
        exp = float(c.get("yt_token_expires_at") or 0)
    except (TypeError, ValueError):
        exp = 0.0
    return YtClient(client_id=c.get("yt_client_id", ""), client_secret=c.get("yt_client_secret", ""),
                    access_token=c.get("yt_access_token", ""), refresh_token=c.get("yt_refresh_token", ""),
                    expires_at=exp)


def _b_ig(c, pk):
    from clients.ig.client import IgClient
    return IgClient(access_token=c.get("ig_access_token", ""), user_id=c.get("ig_user_id", ""), **pk)


def _b_thr(c, pk):
    from clients.thr.client import ThrClient
    return ThrClient(access_token=c.get("thr_access_token", ""), user_id=c.get("thr_user_id", ""), **pk)


def _b_ng(c, pk):
    from clients.ng.client import NgClient
    return NgClient(username=c.get("ng_username", ""), cookie=c.get("ng_cookie", ""))


def _b_sc(c, pk):
    from clients.sc.client import ScClient
    try:
        exp = float(c.get("sc_token_expires_at") or 0)
    except (TypeError, ValueError):
        exp = 0.0
    return ScClient(client_id=c.get("sc_client_id", ""), client_secret=c.get("sc_client_secret", ""),
                    access_token=c.get("sc_access_token", ""), refresh_token=c.get("sc_refresh_token", ""),
                    expires_at=exp)


def _b_fn(c, pk):
    from clients.fn.client import FnClient
    return FnClient(username=c.get("fn_username", ""), password=c.get("fn_password", ""),
                    access_token=c.get("fn_access_token", ""), refresh_token=c.get("fn_refresh_token", ""))


def _b_ik(c, pk):
    from clients.ik.client import IKClient
    return IKClient(c.get("ik_target_user", ""), **pk)


def _b_wp(c, pk):
    from clients.wp.client import WPClient
    return WPClient(c.get("wp_target_user", ""), **pk)


def _b_ws(c, pk):
    from clients.weasyl.client import WeasylClient
    return WeasylClient(api_key=c.get("ws_api_key", ""), **pk)


def _b_ib(c, pk):
    from clients.ib.client import InkbunnyClient
    return InkbunnyClient(username=c.get("username", ""), password=c.get("password", ""), **pk)


# platform -> (gate(creds)->bool, build(creds, proxy_kwargs)->client, kind, rotates)
#   kind: how to read the validator's result
#   rotates: refresh token rotates on use -> persist after a successful test
PROBES = {
    "ao3":  (lambda c: bool((c.get("ao3_username") and c.get("ao3_password")) or c.get("ao3_session_cookie")), _b_ao3, "session_str", False),
    "sqw":  (lambda c: bool(c.get("sqw_username") and c.get("sqw_password")), _b_sqw, "session_str", False),
    "sf":   (lambda c: bool(c.get("sf_api_token")), _b_sf, "session_str", False),
    "e621": (lambda c: bool(c.get("e621_username") and c.get("e621_api_key")), _b_e621, "session_str", False),
    "fbr":  (lambda c: bool(c.get("fbr_username")), _b_fbr, "session_str", False),
    "mast": (lambda c: bool(c.get("mast_instance_url") and c.get("mast_access_token")), _b_mast, "session_str", False),
    "tum":  (lambda c: bool(c.get("tum_api_key") and c.get("tum_blog")), _b_tum, "session_str", False),
    "bsky": (lambda c: bool(c.get("bsky_identifier") and c.get("bsky_app_password")), _b_bsky, "session_str", False),
    "pix":  (lambda c: bool(c.get("pix_refresh_token")), _b_pix, "session_str", False),
    "yt":   (lambda c: bool(c.get("yt_client_id") and c.get("yt_client_secret") and c.get("yt_refresh_token")), _b_yt, "session_str", False),
    "ig":   (lambda c: bool(c.get("ig_access_token")), _b_ig, "session_str", False),
    "thr":  (lambda c: bool(c.get("thr_access_token")), _b_thr, "session_str", False),
    "ng":   (lambda c: bool(c.get("ng_cookie")), _b_ng, "session_dict", False),
    "sc":   (lambda c: bool(c.get("sc_client_id") and c.get("sc_client_secret") and c.get("sc_refresh_token")), _b_sc, "session_str", True),
    "fn":   (lambda c: bool(c.get("fn_username") and (c.get("fn_password") or c.get("fn_refresh_token"))), _b_fn, "session_str", True),
    "ik":   (lambda c: bool(c.get("ik_target_user") or c.get("ik_auth_token")), _b_ik, "ik", False),
    "wp":   (lambda c: bool(c.get("wp_target_user")), _b_wp, "wp_user", False),
    "ws":   (lambda c: bool(c.get("ws_api_key")), _b_ws, "ws_key", False),
    "ib":   (lambda c: bool(c.get("username") and c.get("password")), _b_ib, "ib_login", False),
}


async def _run(kind, client, creds) -> dict:
    """Call the validator and normalise. Exceptions propagate to probe_account for classification."""
    if kind == "session_str":
        name = await client.validate_session()
        return _ok(name) if name else _invalid("Not logged in — re-enter this account's credentials.")
    if kind == "session_dict":                     # Newgrounds — the FA-shaped dict, incl. wrong_account
        r = await client.validate_session() or {}
        if r.get("ok"):
            return _ok(r.get("username"), r.get("detail"))
        if r.get("logged_in") and not r.get("matches"):
            return {"status": "wrong_account", "username": r.get("username", ""),
                    "expected": r.get("expected", ""), "detail": r.get("detail", "")}
        return _invalid(r.get("detail") or "The cookie is not signed in.")
    if kind == "ik":
        token = (creds.get("ik_auth_token") or "").strip()
        if token:
            r = await client.validate_token(token) or {}
            if r.get("status") == "ok":
                return _ok(r.get("username"), r.get("detail"))
            return _invalid(r.get("detail") or "Itaku rejected the auth token.")
        name = await client.validate_user()
        return _ok(name, "Tracking-only (no posting token set).") if name else _invalid("Itaku could not find that user.")
    if kind == "wp_user":
        name = await client.validate_user()
        return _ok(name) if name else _invalid("Wattpad could not find that user.")
    if kind == "ws_key":
        name = await client.validate_key()
        return _ok(name) if name else _invalid("Weasyl rejected the API key.")
    if kind == "ib_login":
        try:
            await client.login()                   # raises RuntimeError on a bad username/password
        except RuntimeError as e:
            return _invalid(str(e) or "Inkbunny rejected the username or password.")
        return _ok(creds.get("username", ""), "Inkbunny login OK.")
    raise ValueError(f"unknown probe kind {kind}")


def _persist_rotation(platform, client, account_id, is_default):
    """A successful test that rotated the refresh token must write it back to THIS account."""
    try:
        if platform == "sc":
            from polling.sc_poller import _persist_tokens
            _persist_tokens(client, account_id, is_default)
        elif platform == "fn":
            new_refresh = getattr(client, "refresh_token", "") or ""
            if new_refresh:
                upd = {config.account_setting_key(account_id, "fn_refresh_token", is_default): new_refresh}
                acc = getattr(client, "access_token", "") or ""
                if acc:
                    upd[config.account_setting_key(account_id, "fn_access_token", is_default)] = acc
                config.save_settings(upd)
    except Exception:
        logger.debug("%s token persist after test failed", platform, exc_info=True)


async def probe_account(platform: str, creds: dict, account_id: int, is_default: bool,
                        settings: dict) -> dict | None:
    """Validate ONE account's credentials. Returns the verdict dict, or ``None`` when this platform has no
    per-account credential test (the caller then returns its own ``unsupported``)."""
    spec = PROBES.get(platform)
    if spec is None:
        return None
    gate, build, kind, rotates = spec
    if not gate(creds):
        return _unconfigured(f"No credentials stored for this {_LABELS.get(platform, platform)} account.")
    client = build(creds, proxy_kwargs(settings, platform))
    try:
        verdict = await _run(kind, client, creds)
        if rotates and verdict.get("status") == "ok":
            _persist_rotation(platform, client, account_id, is_default)
        return verdict
    except Exception as e:
        return _classify_exc(platform, e)
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass
