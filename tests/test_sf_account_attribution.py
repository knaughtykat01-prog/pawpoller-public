"""SoFurry: a display name used as a handle, and a poll list that crossed accounts.

Reported: *"the sofurry tab of submissions … switching to SecondFur where there is
nothing"* — and he was right that it was wrong.

**What the first diagnosis got wrong, and how.** All 17 rows carried
`username = 'SecondFur'` and `account_id = 4` (KnaughtyKat), so it looked like
KnaughtyKat had stolen Kii's submissions, and a migration was written to move
them across on the strength of `username`. That migration would have corrupted
the data, because **neither column means what it looks like**:

* `account_id` records whoever INSERTED the row first;
* `username` is written as `self.display_name` — *the polling client's own
  configured name* — and the `ON CONFLICT` rewrites it, so it records whoever
  POLLED LAST.

Neither is SoFurry's opinion about anything. Asking SoFurry settled it: account
4's authenticated gallery listing returns 28 submissions including all 17
stored, and account 23's returns **zero**. The rows were KnaughtyKat's all
along; the *label* was the lie.

**The actual bug.** `GET /v1/user/{handle}/submissions` is keyed on SoFurry's
`handle`, which is not the display name and need not resemble it. Account 23's
`sf_display_name` was `"SecondFur"` while `/v1/user/me` reports its handle as
`"SecondHandle"` — so every listing 404'd and that account discovered nothing, ever.
Kii's four live SoFurry artworks (PawPoller posted them; the URLs work) had no
rows at all.

`validate_token()` resolves the authoritative handle and its docstring already
promised to "self-heal a mistyped or renamed `sf_display_name`" — but it sat
behind `self.display_name or …`, so it could only run when there was nothing to
heal. **A self-heal that cannot fire.**

With the handle fixed and the poll list scoped, no data repair is needed: the
17 rows keep the account they always belonged to, and KnaughtyKat's next poll
rewrites their `username` back to her own.
"""
from __future__ import annotations

import pytest

import config
from database import accounts as adb, sf_queries
from database.db import get_connection


# ── the poll list must not reach across accounts ─────────────────────

@pytest.fixture()
def two_sf_accounts():
    conn = get_connection()
    try:
        default_id = adb.get_default_account_id(conn, "sf", create=True)
        adb.update_account(conn, default_id, handle="KnaughtyKat")
        other_id = adb.create_account(conn, "sf", "SecondFur", handle="SecondHandle")
    finally:
        conn.close()
    return default_id, other_id


def _sub(sid, username):
    return {"submission_id": sid, "title": f"work {sid}", "username": username,
            "posted_at": "", "content_type": "artwork", "rating": "Adult",
            "thumbnail_url": "", "description": "", "keywords": [],
            "link": f"https://sofurry.com/s/{sid}", "views": 1,
            "favorites_count": 0, "comments_count": 0}


def test_known_ids_are_scoped_to_the_polling_account(two_sf_accounts):
    """Account A's cycle must not be handed account B's submissions.

    Unscoped, account 23's poll re-fetched all 17 of KnaughtyKat's works every
    cycle and stamped its own display name over them — which is what made the
    rows look stolen.
    """
    default_id, other_id = two_sf_accounts
    conn = get_connection()
    try:
        sf_queries.upsert_sf_submission(conn, _sub("kat1", "KnaughtyKat"), default_id)
        sf_queries.upsert_sf_submission(conn, _sub("kat2", "KnaughtyKat"), default_id)
        sf_queries.upsert_sf_submission(conn, _sub("kii1", "SecondHandle"), other_id)
        conn.commit()

        theirs = [r["submission_id"] for r in
                  sf_queries.get_all_sf_submissions(conn, account_id=other_id)]
        assert theirs == ["kii1"], f"account {other_id} was handed foreign rows: {theirs}"
    finally:
        conn.close()


def test_the_poller_passes_the_account_through():
    import ast
    import inspect
    import textwrap
    from polling import sf_poller

    tree = ast.parse(textwrap.dedent(inspect.getsource(sf_poller.run_sf_poll_cycle)))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and "get_all_sf_submissions" in ast.unparse(n.func)]
    assert calls, "the poller no longer lists known ids — update this test"
    for c in calls:
        assert "account_id" in {k.arg for k in c.keywords}, \
            "known-id listing must be scoped, or one account adopts another's rows"


def test_no_poller_lists_known_ids_unscoped():
    import re
    from pathlib import Path

    offenders = []
    for f in (Path(__file__).resolve().parent.parent / "polling").glob("*_poller.py"):
        for m in re.finditer(r"get_all_\w+_submissions\(([^)]*)\)",
                             f.read_text(encoding="utf-8", errors="replace")):
            if "account_id" not in m.group(1):
                offenders.append(f"{f.name}: {m.group(0)}")
    assert offenders == [], f"unscoped known-id listing: {offenders}"


# ── the handle self-heal ─────────────────────────────────────────────

class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)
        self.headers = {}

    def json(self):
        return self._payload


def _client(display_name):
    from clients.sf.client import SoFurryClient
    c = SoFurryClient(api_token="tok", display_name=display_name)
    return c


def _wire(client, real_handle, works, calls):
    """Serve /v1/user/me truthfully and the listing only under the REAL handle."""
    async def _get(path, params=None, **kw):
        calls.append(path)
        if path == "/v1/user/me":
            return _Resp(200, {"id": "x", "handle": real_handle,
                               "username": "a display name, not a handle"})
        if path == f"/v1/user/{real_handle}/submissions":
            return _Resp(200, {"data": [{"id": w, "title": w, "privacy": 3} for w in works],
                               "meta": {"last_page": 1}})
        return _Resp(404, {"statusCode": 404, "message": "Resource not found",
                           "description": "The requested resource could not be found"})
    client._api.get = _get


@pytest.mark.asyncio
async def test_a_wrong_handle_is_resolved_and_the_listing_retried():
    """THE fix. A display name in the handle slot 404s; the real handle works."""
    calls = []
    c = _client("SecondFur")               # a display name
    _wire(c, "SecondHandle", ["a1", "a2"], calls)  # the real handle

    got = await c.get_all_gallery_ids()

    assert [g["submission_id"] for g in got] == ["a1", "a2"], (
        "the listing 404'd on the display name and was never retried with the "
        "handle /v1/user/me reports — the account discovers nothing, for ever")
    assert "/v1/user/me" in calls, "the authoritative handle was never asked for"
    assert c.display_name == "SecondHandle", "the corrected handle must stick on the client"


@pytest.mark.asyncio
async def test_a_correct_handle_costs_no_extra_request():
    """The happy path must not pay for the repair: no /v1/user/me round-trip
    when the configured handle already works."""
    calls = []
    c = _client("SecondHandle")
    _wire(c, "SecondHandle", ["a1"], calls)

    got = await c.get_all_gallery_ids()

    assert [g["submission_id"] for g in got] == ["a1"]
    assert "/v1/user/me" not in calls


@pytest.mark.asyncio
async def test_it_heals_once_and_then_gives_up():
    """A 404 that survives the correction must stop, not loop."""
    calls = []
    c = _client("Wrong")
    _wire(c, "AlsoNotServed", [], calls)     # listing 404s under every name

    got = await c.get_all_gallery_ids()

    assert got == []
    assert calls.count("/v1/user/me") == 1, f"resolved more than once: {calls}"
    assert len(calls) <= 4, f"retry loop: {calls}"


@pytest.mark.asyncio
async def test_the_self_heal_is_reachable_at_all():
    """Pins the shape, not just the behaviour.

    `handle = self.display_name or await self.validate_token()` put the resolver
    behind a short-circuit, so the thing documented as self-healing a *wrong*
    display name could only run when the display name was *absent*. Any rewrite
    that restores that shape puts the bug back.
    """
    import inspect
    from clients.sf.client import SoFurryClient

    src = inspect.getsource(SoFurryClient.get_all_gallery_ids)
    body = src.split('"""', 2)[-1]           # drop the docstring
    assert "validate_token" in body, "no handle resolution left in the listing"
    assert "404" in body, (
        "the resolver must be reachable from the 404 path, not only from an "
        "empty display_name")


# ── the corrected handle is persisted to the right key ───────────────

def test_the_poller_persists_the_correction_per_account():
    """It must land on `acct_<id>_sf_display_name`, never the bare key.

    Writing the bare key is how both DeviantArt accounts died in 3.21.0: it
    belongs to the DEFAULT account, so a non-default account's correction would
    overwrite the default's identity.
    """
    import ast
    import inspect
    import textwrap
    from polling import sf_poller

    src = textwrap.dedent(inspect.getsource(sf_poller.run_sf_poll_cycle))
    tree = ast.parse(src)
    saves = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and "save_settings" in ast.unparse(n.func)]
    assert saves, "the corrected handle is never persisted — it re-resolves every cycle"
    assert "account_setting_key" in src, \
        "the poller writes settings without namespacing them to the account"

    # The property that actually matters: no save_settings writes a hard-coded
    # credential key. A literal key IS the default account's key — checking for
    # the absence of literals catches every spelling, where matching one
    # expression only catches the spelling I happened to write.
    for s in saves:
        for arg in s.args:
            if isinstance(arg, ast.Dict):
                literals = [k.value for k in arg.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                assert not literals, (
                    f"save_settings writes literal key(s) {literals} — a bare key "
                    "belongs to the DEFAULT account, which is how the DeviantArt "
                    "tokens were destroyed in 3.21.0")


def test_account_setting_key_keeps_the_accounts_apart():
    assert config.account_setting_key(23, "sf_display_name", False) == "acct_23_sf_display_name"
    assert config.account_setting_key(4, "sf_display_name", True) == "sf_display_name"


# ── the retracted migration must stay retracted ──────────────────────

def test_no_migration_reassigns_sf_rows_by_username():
    """3.24.0 shipped a migration that re-stamped sf_submissions onto the account
    whose handle matched `username`. It was removed before it ever ran: `username`
    holds the POLLING client's display name, not the owner, so the migration would
    have moved 17 of KnaughtyKat's works onto Kii — inventing the theft it claimed
    to repair. Ownership is not recoverable from these columns; only SoFurry's
    authenticated listing knows, and the poller now gets it right at the source.
    """
    from pathlib import Path

    db_src = (Path(__file__).resolve().parent.parent / "database" / "db.py").read_text(
        encoding="utf-8", errors="replace")
    assert "UPDATE sf_submissions" not in db_src, (
        "a migration is rewriting sf_submissions ownership again — `username` "
        "cannot support that inference")
