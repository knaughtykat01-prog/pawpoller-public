"""DeviantArt authorises whoever the BROWSER is signed in as (3.32.2).

The last piece of the wrong-account saga, and the one that started it.

Pressing "Authorise posting" on an account opens DeviantArt's consent screen —
which authorises **whoever that browser is signed in as**, not the account whose
button was clicked. Until now the callback stored whatever token came back
under that account's key and showed a green "authorised" page. Approving while
signed in as someone else therefore:

- stored the other account's token under this account's key,
- reported success,
- and left every post from this account landing on the other one.

The same one-session-per-browser trap as FurAffinity's cookies (3.31.0), one
step earlier in the flow, and the likely origin of the live mismatch that
3.32.0 detected at post time.

**The callback is the last place the mistake is cheap.** Refusing costs one more
trip through the browser. Storing costs a post to the wrong gallery and a hunt
through the poster, the credential keys and the 3.21.0 incident notes — which is
what it did cost.

⚠ The check spends nothing: it uses the `access_token` the code exchange just
returned, so the single-use refresh token is never touched.
"""
from __future__ import annotations

import pytest


def _src():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "routes" /
            "da_api.py").read_text(encoding="utf-8", errors="replace")


def _callback():
    body = _src()
    start = body.index("async def da_auth_callback")
    return body[start:body.index("\ndef _da_target_user")]


# ── the check exists, and refuses ────────────────────────────────────

def test_the_callback_asks_who_approved_it():
    fn = _callback()
    assert "user/whoami" in fn
    assert "approved_by" in fn


def test_a_mismatch_is_refused_and_nothing_is_stored():
    """THE regression. Storing it is what made every later symptom possible."""
    fn = _callback()
    refusal = fn[fn.index("if approved_by and expected"):]
    refusal = refusal[:refusal.index("config.save_settings")]
    assert "Wrong DeviantArt account" in refusal
    assert "Nothing was saved" in refusal
    assert "return _page(" in refusal, "it must return before the save"


def test_the_refusal_names_both_accounts_and_the_cause():
    """"Wrong account" alone sends someone back to the same browser to make the
    same mistake — which is the loop this whole thread was stuck in."""
    fn = _callback()
    assert "{approved_by}" in fn and "{expected}" in fn
    assert "private window" in fn
    assert "signed in as" in fn


def test_the_comparison_is_case_insensitive():
    """`SecondFur` vs `secondfur` is one account; a false refusal would lock
    someone out of authorising their own account."""
    fn = _callback()
    assert ".strip().lower() != " in fn


# ── it must not cost a credential, or block on a hiccup ──────────────

def test_the_check_uses_the_exchange_s_access_token_not_a_refresh():
    """DA refresh tokens are single-use. Spending one to validate the one we
    are about to store would be self-defeating."""
    fn = _callback()
    check = fn[fn.index("approved_by = \"\""):fn.index("expected = _da_target_user")]
    assert 'data.get("access_token"' in check
    assert "oauth_refresh_token" not in check
    assert "grant_type" not in check


def test_a_whoami_failure_does_not_block_authorisation():
    """DA being briefly unreachable must not stop someone connecting an
    account — the poster still checks identity before every post."""
    fn = _callback()
    check = fn[fn.index("approved_by = \"\""):fn.index("expected = _da_target_user")]
    assert "except Exception" in check
    assert "logger.warning" in check


def test_an_unknown_identity_stores_but_says_so():
    """Absent is not wrong. With no name to compare, the token is stored and
    the page points at the per-account Test button rather than claiming a
    verdict it does not have."""
    fn = _callback()
    tail = fn[fn.index("config.save_settings({token_key: refresh})"):]
    assert "did not say which account" in tail
    assert "Test" in tail


def test_a_confirmed_identity_is_named_on_the_success_page():
    fn = _callback()
    tail = fn[fn.index("config.save_settings({token_key: refresh})"):]
    assert "It posts as" in tail


def test_the_stored_key_is_still_the_accounts_own():
    """The 3.21.0 bare-key incident. `token_key` comes from the OAuth state, so
    a non-default account's token can never land on the default key."""
    fn = _callback()
    assert "config.save_settings({token_key: refresh})" in fn
    assert 'config.save_settings({"da_refresh_token"' not in fn


# ── resolving the account's configured name ──────────────────────────

def test_the_target_user_helper_is_account_scoped():
    body = _src()
    fn = body[body.index("def _da_target_user"):]
    fn = fn[:fn.index("@da_router.get")]
    assert "resolve_account_credentials" in fn
    assert "is_default" in fn


def test_an_unresolvable_target_user_does_not_block(monkeypatch):
    """Returning "" disables the comparison rather than refusing everything —
    a lookup failure must not make the account unauthorisable."""
    from routes import da_api
    body = _src()
    fn = body[body.index("def _da_target_user"):]
    fn = fn[:fn.index("@da_router.get")]
    assert 'return ""' in fn
    assert "except Exception" in fn
    assert da_api._da_target_user(99999) == ""
