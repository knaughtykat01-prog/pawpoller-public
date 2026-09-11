"""Per-account credential Test — the probe that routes the Accounts-page Test button to each
platform's existing validator (spec 001-account-cred-test).

No live network: every client class is monkeypatched. These lock down the verdict vocabulary, the
rate-limit-is-not-expiry rule, Newgrounds' wrong-account passthrough, that fa/da/tw are left to their
own branches, that a poller singleton is never built, and — the one that can do harm — that a rotating
refresh token (SoundCloud, FurryNetwork) is persisted after a successful test.
"""
import asyncio

# Import the pollers we touch BEFORE any test patches a client class, so their module-level
# `from clients.x.client import XClient` binds the REAL class. Otherwise a per-test patch of
# clients.sc.client.ScClient would be captured by sc_poller's first import and never revert.
import polling.sc_poller  # noqa: F401
import polling.mast_poller  # noqa: F401
from polling import account_probe as ap


class _FakeStr:
    """validate_session() -> configurable str/None, or raises a configured exception."""
    result = "someuser"        # str, None, or an Exception instance to raise
    def __init__(self, **kw):
        self.closed = False
    async def validate_session(self):
        r = type(self).result
        if isinstance(r, Exception):
            raise r
        return r
    async def close(self):
        self.closed = True


class IgAuthError(Exception):
    pass


class FnAuthError(Exception):
    pass


def _probe(platform, creds, account_id=1, is_default=True):
    return asyncio.run(ap.probe_account(platform, creds, account_id, is_default, {}))


def _patch(monkeypatch, module, cls):
    """Replace the client class *inside* its module, since the builders do
    `from clients.x.client import XClient` at call time."""
    monkeypatch.setattr(f"{module}.{cls.__name__}", cls, raising=False)


# ── the core verdicts ─────────────────────────────────────────
def test_ok_returns_username(monkeypatch):
    _FakeStr.result = "inkwolf"
    _patch(monkeypatch, "clients.mast.client", type("MastClient", (_FakeStr,), {}))
    v = _probe("mast", {"mast_instance_url": "https://m.example", "mast_access_token": "t"})
    assert v["status"] == "ok" and v["username"] == "inkwolf"


def test_invalid_when_validator_returns_none(monkeypatch):
    Cls = type("MastClient", (_FakeStr,), {"result": None})
    _patch(monkeypatch, "clients.mast.client", Cls)
    v = _probe("mast", {"mast_instance_url": "https://m.example", "mast_access_token": "t"})
    assert v["status"] == "invalid"


def test_unconfigured_makes_no_client(monkeypatch):
    # empty creds -> gate fails -> unconfigured, and no client is ever built
    class E621Client:
        def __init__(self, **k):
            raise AssertionError("client must not be built for an unconfigured account")
        async def close(self): pass
    _patch(monkeypatch, "clients.e621.client", E621Client)
    v = _probe("e621", {"e621_username": "", "e621_api_key": ""})
    assert v["status"] == "unconfigured"


def test_block_is_error_not_invalid(monkeypatch):
    # AO3 raises RuntimeError on a shields-up / rate-limit block -> temporary, never "expired"
    Cls = type("AO3Client", (_FakeStr,), {"result": RuntimeError("AO3 is temporarily blocking us")})
    _patch(monkeypatch, "clients.ao3.client", Cls)
    v = _probe("ao3", {"ao3_session_cookie": "abc"})
    assert v["status"] == "error" and "block" in v["detail"].lower()


def test_ig_app_block_is_error(monkeypatch):
    Cls = type("IgClient", (_FakeStr,), {"result": IgAuthError("API access blocked")})
    _patch(monkeypatch, "clients.ig.client", Cls)
    v = _probe("ig", {"ig_access_token": "t"})
    assert v["status"] == "error"


def test_fn_auth_failure_is_invalid(monkeypatch):
    Cls = type("FnClient", (_FakeStr,), {"result": FnAuthError("bad refresh token")})
    _patch(monkeypatch, "clients.fn.client", Cls)
    v = _probe("fn", {"fn_username": "u", "fn_refresh_token": "r"})
    assert v["status"] == "invalid"


def test_ng_wrong_account_passthrough(monkeypatch):
    class NgClient:
        def __init__(self, **kw): pass
        async def validate_session(self):
            return {"ok": False, "logged_in": True, "matches": False,
                    "username": "someoneelse", "expected": "inkwolf", "detail": "belongs to someoneelse"}
        async def close(self): pass
    _patch(monkeypatch, "clients.ng.client", NgClient)
    v = _probe("ng", {"ng_cookie": "c", "ng_username": "inkwolf"})
    assert v["status"] == "wrong_account" and v["username"] == "someoneelse" and v["expected"] == "inkwolf"


def test_ib_bad_password_is_invalid(monkeypatch):
    class InkbunnyClient:
        def __init__(self, **kw): pass
        async def login(self): raise RuntimeError("Login failed")
        async def close(self): pass
    _patch(monkeypatch, "clients.ib.client", InkbunnyClient)
    v = _probe("ib", {"username": "u", "password": "p"})
    assert v["status"] == "invalid"


# ── the hazard: rotating tokens must be persisted ─────────────
def test_sc_rotation_is_persisted(monkeypatch):
    class ScClient(_FakeStr):
        result = "scuser"
        def __init__(self, **kw):
            super().__init__(**kw)
            self.tokens_changed = True
            self.refresh_token = "rotated-sc"
    _patch(monkeypatch, "clients.sc.client", ScClient)
    calls = {}
    monkeypatch.setattr("polling.sc_poller._persist_tokens",
                        lambda client, aid, is_default: calls.update(aid=aid, tok=client.refresh_token))
    v = _probe("sc", {"sc_client_id": "i", "sc_client_secret": "s", "sc_refresh_token": "old"}, account_id=7)
    assert v["status"] == "ok"
    assert calls == {"aid": 7, "tok": "rotated-sc"}, "a successful sc test must persist the rotated token"


def test_fn_rotation_is_persisted(monkeypatch):
    class FnClient(_FakeStr):
        result = "fnuser"
        def __init__(self, **kw):
            super().__init__(**kw)
            self.refresh_token = "rotated-fn"
            self.access_token = "acc"
    _patch(monkeypatch, "clients.fn.client", FnClient)
    saved = {}
    monkeypatch.setattr("config.account_setting_key", lambda aid, key, is_default: f"{aid}:{key}")
    monkeypatch.setattr("config.save_settings", lambda upd: saved.update(upd))
    v = _probe("fn", {"fn_username": "u", "fn_refresh_token": "old"}, account_id=3)
    assert v["status"] == "ok"
    assert saved.get("3:fn_refresh_token") == "rotated-fn", "a successful fn test must persist the rotated token"


# ── no clobber, and fa/da/tw stay out ─────────────────────────
def test_probe_never_builds_the_poller_singleton(monkeypatch):
    import polling.mast_poller as mp
    sentinel = object()
    mp._mast_client = sentinel
    _patch(monkeypatch, "clients.mast.client", type("MastClient", (_FakeStr,), {"result": "x"}))
    _probe("mast", {"mast_instance_url": "https://m.example", "mast_access_token": "t"})
    assert mp._mast_client is sentinel, "the probe must not build or replace the poller's cached client"


def test_fa_da_tw_are_not_handled_by_the_probe():
    for p in ("fa", "da", "tw", "tg", "pod"):
        assert asyncio.run(ap.probe_account(p, {}, 1, True, {})) is None


def test_every_registered_credential_platform_is_probeable():
    # every platform with per-account credentials except the ones handled elsewhere must have a probe
    handled_elsewhere = {"fa", "da", "tw", "tg", "pod"}
    import re
    from pathlib import Path
    cfg = (Path(__file__).resolve().parent.parent / "config.py").read_text(encoding="utf-8")
    block = cfg[cfg.index("PLATFORM_CREDENTIAL_FIELDS"):]
    codes = set(re.findall(r'^\s*"(\w+)":\s*\[', block[:block.index("}")], re.M))
    missing = {c for c in codes if c not in handled_elsewhere and c not in ap.PROBES}
    assert not missing, f"platforms with credentials but no Test probe: {missing}"
