"""Where a rotated OAuth refresh token gets written (3.21.0).

The incident this pins down, in full, because the failure was silent for three
days and the symptom pointed nowhere near the cause.

OAuth refresh tokens are single-use. The call that returns an access token also
consumes the refresh token it was handed and issues a replacement. So the line
that *stores* the replacement decides whether the account can ever authenticate
again — and until 3.21.0 the DeviantArt poster stored it like this::

    config.save_settings({"da_refresh_token": new_refresh})

The bare key. Always. That key belongs to the DEFAULT account; every other
account's lives under ``acct_<id>_da_refresh_token``. So when account 27
(SecondFur, OAuth app 75305) refreshed:

  * account 27's own key kept the token that had just been spent — dead at the
    next refresh, permanently;
  * account 7's key (KnaughtyKat, a DIFFERENT OAuth app, 71075) was overwritten
    with account 27's brand-new token.

Both accounts were now broken, which is why "the DeviantArt token expired"
looked like an act of God rather than a bug: two independent accounts on two
independent OAuth apps failed within minutes of each other, and DA's own
client_credentials grant kept answering 200 for both apps the whole time.

Worse than breakage — misattribution. While account 27's stolen token was still
alive, a post made *as account 7* authenticated as account 27 and published to
its gallery. That really happened: publication 173, "Blows a Kiss", recorded
against account 7, external_url ``deviantart.com/secondfur/art/...``.

`routes/da_api.py` had known the rule all along and even documented it —
"writing the bare key for a non-default account would silently hand its token
to the default one". The knowledge existed in one file and the bug in another,
which is this codebase's recurring shape: one fact, several declarations, no
check. These tests are the check.
"""
from __future__ import annotations

import pytest

import config
from database import accounts as adb
from database.db import get_connection


@pytest.fixture()
def two_da_accounts():
    """The real pairing: a default account and a second one."""
    conn = get_connection()
    try:
        default_id = adb.get_default_account_id(conn, "da", create=True)
        other_id = adb.create_account(conn, "da", "SecondFur", handle="SecondFur")
    finally:
        conn.close()
    assert default_id != other_id
    return default_id, other_id


def _poster(account_id):
    from posting.platforms.deviantart import DeviantArtPoster
    p = DeviantArtPoster()
    p.account_id = account_id
    return p


# ── the write lands where it was read from ───────────────────────────

def test_a_non_default_accounts_token_goes_to_its_own_key(two_da_accounts):
    _default_id, other_id = two_da_accounts
    _poster(other_id)._save_creds("da", {"da_refresh_token": "rotated_for_27"})

    s = config.get_settings()
    assert s.get(f"acct_{other_id}_da_refresh_token") == "rotated_for_27"


def test_the_default_accounts_token_still_uses_the_bare_key(two_da_accounts):
    """Legacy installs keep the flat key — the fix must not migrate anyone."""
    default_id, _other = two_da_accounts
    _poster(default_id)._save_creds("da", {"da_refresh_token": "rotated_for_7"})

    assert config.get_settings().get("da_refresh_token") == "rotated_for_7"


def test_one_accounts_rotation_never_touches_the_others_token(two_da_accounts):
    """THE regression. Account 27 refreshing must leave account 7 untouched.

    Without the fix this fails twice over: account 7's live token is replaced
    by a token minted for a different OAuth app (so account 7 can no longer
    refresh, and while it briefly can, it posts to account 27's gallery), and
    account 27's own key never receives the replacement at all.
    """
    default_id, other_id = two_da_accounts
    config.save_settings({"da_refresh_token": "account_7_own_live_token"})

    _poster(other_id)._save_creds("da", {"da_refresh_token": "account_27_rotated"})

    s = config.get_settings()
    assert s.get("da_refresh_token") == "account_7_own_live_token", (
        "account 27's rotation overwrote account 7's token — this is the bug "
        "that killed both DeviantArt accounts on 2026-08-19")
    assert s.get(f"acct_{other_id}_da_refresh_token") == "account_27_rotated"


def test_an_unset_account_id_resolves_to_the_platform_default(two_da_accounts):
    """A poster built without an account_id must still write somewhere real,
    not invent an `acct_None_` key."""
    default_id, _other = two_da_accounts
    p = _poster(None)
    p._save_creds("da", {"da_refresh_token": "resolved"})

    s = config.get_settings()
    assert s.get("da_refresh_token") == "resolved"
    assert p.account_id == default_id
    assert not any(k.startswith("acct_None") for k in s)


def test_saving_nothing_writes_nothing(two_da_accounts):
    default_id, _other = two_da_accounts
    config.save_settings({"da_refresh_token": "keep"})
    _poster(default_id)._save_creds("da", {})
    assert config.get_settings().get("da_refresh_token") == "keep"


# ── the poster actually uses it ──────────────────────────────────────

def test_the_da_poster_writes_the_rotated_token_through_the_helper():
    """A call site is not evidence of a definition, and a definition is not
    evidence of a call — so pin the poster to the helper, not just to the
    helper's correctness. (`test_account_credential_reentry` learned this the
    embarrassing way with `API.testAccountLogin`.)"""
    import ast
    import inspect
    from posting.platforms import deviantart

    src = inspect.getsource(deviantart.DeviantArtPoster._ensure_client)
    tree = ast.parse(src.lstrip() if not src.startswith("    ") else
                     "\n".join(l[4:] if l.startswith("    ") else l
                               for l in src.splitlines()))
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert any("_save_creds" in c for c in calls), \
        "DA rotates its refresh token — it must persist via _save_creds"
    assert not any("save_settings" in c for c in calls), \
        "config.save_settings in a poster writes the DEFAULT account's key"


def test_no_poster_writes_settings_directly():
    """The general form. `config.save_settings` in a poster cannot know which
    account it is writing for; `_save_creds` is the only thing that can."""
    from pathlib import Path

    offenders = []
    for f in (Path(__file__).resolve().parent.parent / "posting" / "platforms").glob("*.py"):
        if f.name == "base.py":
            continue  # _save_creds itself lives here
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            code = line.split("#", 1)[0]
            if "config.save_settings" in code:
                offenders.append(f"{f.name}:{i}")
    assert offenders == [], (
        "posters must persist credentials via self._save_creds so the write "
        f"lands on their own account's key: {offenders}")


# ── FurryNetwork had the same shape, one step less lethal ────────────

def test_furrynetwork_persists_a_non_default_accounts_tokens_too():
    """FN guarded itself by returning early for non-default accounts. That
    avoided the crossover but still dropped their rotated token on the floor —
    and a dropped rotation is a dead account at the next refresh, because the
    token left on disk has already been spent."""
    conn = get_connection()
    try:
        adb.get_default_account_id(conn, "fn", create=True)
        other_id = adb.create_account(conn, "fn", "Second", handle="second")
    finally:
        conn.close()

    from posting.platforms.furrynetwork import FurryNetworkPoster
    p = FurryNetworkPoster()
    p.account_id = other_id
    p._save_creds("fn", {"fn_refresh_token": "fn_rotated", "fn_access_token": "fn_acc"})

    s = config.get_settings()
    assert s.get(f"acct_{other_id}_fn_refresh_token") == "fn_rotated"
    assert s.get(f"acct_{other_id}_fn_access_token") == "fn_acc"
    assert not s.get("fn_refresh_token"), "must not touch the default account"
