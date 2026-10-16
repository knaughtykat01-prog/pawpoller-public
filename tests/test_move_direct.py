"""Sending a PawPoller straight to a hosted seat with a one-time code (spec 036).

Two installs live in one test, as in test_move.py: `use()` points config at one of them. The old
copy's sender runs synchronously here, and its HTTP client goes through a transport that switches to
the seat, calls the seat's app, and switches back, so nothing touches the network.
"""
import json
import os
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from routes import move_api
from routes import move_direct_api as md
from tests.test_move import Install, _Done

SEAT = "inkwolf"


@pytest.fixture
def pair(tmp_path, monkeypatch):
    old, new = Install(tmp_path / "old", works=("Sample Story", "Second Piece")), Install(tmp_path / "new", works=())
    current = {}

    def use(inst):
        current["i"] = inst
        monkeypatch.setattr(config, "DATA_DIR", inst.data)
        monkeypatch.setattr(config, "DB_PATH", inst.data / "pawpoller.db")
        monkeypatch.setattr(config, "SETTINGS_PATH", inst.data / "settings.json")
        monkeypatch.setattr(config, "VAULT_PATH", inst.data / "settings.vault.json")
        monkeypatch.setenv("PAWPOLLER_VAULT_KEY", inst.key)
        monkeypatch.setattr(config, "_vault_unreadable", False)

    from posting import artwork_reader, story_reader
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: current["i"].data / "artwork")
    monkeypatch.setattr(story_reader, "get_archive_path", lambda: current["i"].root / "stories")
    monkeypatch.setattr(move_api, "_in_container", lambda: False)
    monkeypatch.setattr(move_api, "PIECE_BYTES", 1024)          # several pieces from a small library
    monkeypatch.setattr(md, "_BACKOFF", (0,))
    monkeypatch.setenv("PAWPOLLER_HOSTED", "1")
    monkeypatch.setenv("PAWPOLLER_PUBLIC_BASE_URL", f"https://paw-{SEAT}.pawpoller.com")
    md._STOP.clear()
    md._SEND.clear()
    md._SEND.update(state="idle")

    use(old)
    (old.data / "artwork" / "Piece").mkdir()
    (old.data / "artwork" / "Piece" / "img.png").write_bytes(os.urandom(3000))
    (old.root / "stories" / "Sample Story").mkdir()
    (old.root / "stories" / "Sample Story" / "story.md").write_text("Once.", encoding="utf-8")
    config.save_settings({"fa_cookie_a": "fa-secret-a", "ws_api_key": "ws-secret", "theme": "dark",
                          "polling_paused": False, "posting_enabled": True, "auth_username": "olduser",
                          "auth_password_hash": "OLD-HASH"})
    use(new)
    config.save_settings({"auth_username": "seatuser", "auth_password_hash": "SEAT-HASH",
                          "auto_backup_enabled": True, "auto_backup_keep": 14})

    app = FastAPI()
    app.include_router(move_api.move_router)
    app.include_router(md.owner_router)
    app.include_router(md.direct_router)
    seat_client = TestClient(app)
    seen = []                                                   # every request body the "network" saw
    hooks = {"before": None, "after": None}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen.append(body)
        if hooks["before"]:
            hooks["before"](request)
        use(new)
        try:
            r = seat_client.request(request.method, request.url.raw_path.decode(), content=body,
                                    headers={k: v for k, v in request.headers.items() if k.lower() != "host"})
        finally:
            use(old)
        resp = httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers.get("content-type", "")})
        return hooks["after"](request, resp) if hooks["after"] else resp

    monkeypatch.setattr(md, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return {"old": old, "new": new, "use": use, "c": seat_client, "seen": seen, "hooks": hooks}


def _show_code(p) -> str:
    p["use"](p["new"])
    r = p["c"].post("/api/move/code", json={})
    p["use"](p["old"])
    assert r.status_code == 200, r.text
    return r.json()["code"]


def _send(p, code, pause=True):
    p["use"](p["old"])
    seat, rnd = md.parse_code(code)
    md._run_send(seat, rnd, md.seat_address(seat), pause)
    return dict(md._SEND)


def _seat_code(p):
    p["use"](p["new"])
    try:
        return p["c"].get("/api/move/code").json()
    finally:
        p["use"](p["old"])


def _signed(auth_key, method, path, body=b"", query=""):
    return {md.AUTH_HEADER: md.sign(auth_key, method, path, query, body), "Content-Type": "application/json"}


# ── The code ─────────────────────────────────────────────────────────────────

def test_codes_parse_forgivingly_and_carry_the_seat():
    code = md.new_code("fox-art")
    seat, rnd = md.parse_code(code)
    assert seat == "fox-art" and len(rnd) == 12 and all(ch in md.ALPHABET for ch in rnd)
    assert md.parse_code(" " + code.lower().replace("-", " - ") + " ") == (seat, rnd)
    for bad in ("", "inkwolf", "inkwolf-AAAA-AAAA-AAA", "inkwolf-0OOO-1III-AAAA", "1bad-AAAA-AAAA-AAAA"):
        with pytest.raises(md.BadCode):
            md.parse_code(bad)


def test_keys_are_seat_salted_and_signatures_check():
    a1, s1 = md.derive("inkwolf", "ABCDEFGHJKLM")
    assert (a1, s1) == md.derive("inkwolf", "ABCDEFGHJKLM")
    a2, s2 = md.derive("penwright", "ABCDEFGHJKLM")
    assert a1 != a2 and s1 != s2 and a1 != s1
    sig = md.sign(a1, "PUT", "/api/move/direct/x", "offset=0", b"body")
    assert md.verify(a1, sig, "PUT", "/api/move/direct/x", "offset=0", b"body")
    assert not md.verify(a1, sig, "PUT", "/api/move/direct/x", "offset=1024", b"body")
    assert not md.verify(a2, sig, "PUT", "/api/move/direct/x", "offset=0", b"body")


def test_pieces_open_only_with_their_key_place_and_upload():
    _a, s = md.derive("inkwolf", "ABCDEFGHJKLM")
    prefix = bytes(8)
    sealed = md.seal_piece(s, prefix, 3, "u1", 9000, b"plain bytes")
    assert md.open_piece(s, prefix, 3, "u1", 9000, sealed) == b"plain bytes"
    for args in ((md.derive("inkwolf", "ZZZZZZZZZZZZ")[1], prefix, 3, "u1"), (s, prefix, 4, "u1"), (s, prefix, 3, "u2")):
        with pytest.raises(md.BadPiece):
            md.open_piece(*args, 9000, sealed)


def test_only_a_hosted_seat_shows_codes(pair, monkeypatch):
    monkeypatch.delenv("PAWPOLLER_HOSTED")
    assert md.this_seat() is None
    pair["use"](pair["new"])
    assert pair["c"].post("/api/move/code", json={}).status_code == 404
    assert pair["c"].get("/api/move/code").json() == {"state": "unavailable"}
    monkeypatch.setenv("PAWPOLLER_HOSTED", "1")
    assert md.this_seat() == SEAT


# ── US1: the send ────────────────────────────────────────────────────────────

def test_a_code_sends_the_whole_library_and_the_owner_brings_it_in(pair):
    p = pair
    code = _show_code(p)
    assert _seat_code(p)["state"] == "waiting"
    result = _send(p, code)
    assert result["state"] == "sent", result
    st = _seat_code(p)
    assert st["state"] == "arrived" and st["received"] == st["size"]
    # The old copy stood down and tidied up.
    assert config.get_settings().get("moved_away_at")
    assert not (p["old"].data / "move-out").exists()
    # Only the signed-in owner brings it in; no phrase is typed.
    p["use"](p["new"])
    info = p["c"].post(f"/api/move/uploads/{st['upload_id']}/check", json={}).json()
    assert info["logins"] == "sealed" and info["files"] >= 2
    r = p["c"].post(f"/api/move/uploads/{st['upload_id']}/apply", json={"confirm": True})
    assert r.status_code == 200, r.text
    for _ in range(600):
        body = p["c"].get("/api/move/apply").json()
        if body["state"] != "applying":
            break
        time.sleep(0.05)
    assert _Done(body).status_code == 200, body
    s = config.get_settings()
    assert p["new"].titles() == ["Sample Story", "Second Piece"]
    assert s["fa_cookie_a"] == "fa-secret-a" and s["auth_username"] == "seatuser"
    assert (p["new"].data / "artwork" / "Piece" / "img.png").is_file()
    assert not (p["new"].data / "move-code.json").exists()        # brought in: the code is spent


def test_the_address_comes_from_the_code_only(pair):
    p = pair
    p["use"](p["old"])
    r = p["c"].post("/api/move/send/preview", json={"code": md.new_code(SEAT)})
    assert r.json() == {"seat": SEAT, "address": f"https://paw-{SEAT}.pawpoller.com"}
    r = p["c"].post("/api/move/send", json={"code": md.new_code(SEAT), "address": "https://paw-other.pawpoller.com"})
    assert r.status_code == 400
    assert p["c"].post("/api/move/send/preview", json={"code": "nonsense"}).status_code == 400


# ── US2: the code can't be misused ───────────────────────────────────────────

def test_nothing_readable_crosses_the_network(pair):
    p = pair
    code = _show_code(p)
    assert _send(p, code)["state"] == "sent"
    keys = json.loads((p["new"].data / "move-code.json").read_text())
    wire = b"".join(p["seen"])
    assert p["seen"] and code.encode() not in wire and md.parse_code(code)[1].encode() not in wire
    assert keys["auth_key"].encode() not in wire and keys["seal_key"].encode() not in wire
    assert b"PK\x03\x04" not in wire and b"fa-secret-a" not in wire and b"Sample Story" not in wire


def test_an_expired_code_refuses_and_a_used_one_refuses(pair):
    p = pair
    code = _show_code(p)
    cp = p["new"].data / "move-code.json"
    c = json.loads(cp.read_text())
    c["expires_at"] = time.time() - 1
    cp.write_text(json.dumps(c))
    r = _send(p, code)
    assert r["state"] == "failed" and "run out" in r["error"]
    assert not config.get_settings().get("moved_away_at")         # a failed send puts the copy back
    code = _show_code(p)
    assert _send(p, code)["state"] == "sent"
    p["use"](p["old"])
    config.save_settings({"moved_away_at": None})
    r = _send(p, code)
    assert r["state"] == "failed" and "already been used" in r["error"]


def test_five_wrong_tries_lock_the_code(pair):
    p = pair
    _show_code(p)
    wrong, _s = md.derive(SEAT, "ZZZZZZZZZZZZ")
    body = json.dumps({"seat": SEAT, "size": 10, "nonce_prefix": "0" * 16, "app_version": "1.0.0"}).encode()
    p["use"](p["new"])
    codes = [p["c"].post("/api/move/direct/open", content=body, headers=_signed(wrong, "POST", "/api/move/direct/open", body)).status_code
             for _ in range(5)]
    assert codes == [403] * 5
    assert p["c"].get("/api/move/code").json()["state"] == "locked"
    assert p["c"].post("/api/move/code", json={}).status_code == 429
    assert p["c"].post("/api/move/direct/open", content=body).status_code == 429


def test_a_piece_sealed_with_another_key_is_refused_and_counted(pair):
    p = pair
    code = _show_code(p)
    auth, _seal = md.derive(*md.parse_code(code))
    p["use"](p["new"])
    body = json.dumps({"seat": SEAT, "size": 100, "nonce_prefix": "0" * 16, "app_version": "1.0.0"}).encode()
    uid = p["c"].post("/api/move/direct/open", content=body, headers=_signed(auth, "POST", "/api/move/direct/open", body)).json()["id"]
    bad = md.seal_piece(md.derive(SEAT, "ZZZZZZZZZZZZ")[1], bytes(8), 0, uid, 100, b"x" * 100)
    path = f"/api/move/direct/{uid}"
    r = p["c"].put(f"{path}?offset=0", content=bad, headers=_signed(auth, "PUT", path, bad, "offset=0"))
    assert r.status_code == 403
    assert json.loads((p["new"].data / "move-code.json").read_text())["failures"] == 1


def test_a_piece_for_no_send_is_turned_away_before_it_is_read(pair):
    p = pair
    p["use"](p["new"])
    uid = "a" * 32
    assert p["c"].put(f"/api/move/direct/{uid}?offset=0", content=b"x" * 2048).status_code == 404   # no code
    _show_code(p)
    p["use"](p["new"])
    assert p["c"].put(f"/api/move/direct/{uid}?offset=0", content=b"x" * 2048).status_code == 404   # code, no send
    assert json.loads((p["new"].data / "move-code.json").read_text())["failures"] == 0


def test_junk_requests_cannot_void_a_send_under_way(pair):
    p = pair
    code = _show_code(p)
    n = {"i": 0}

    def junk_mid_send(req):
        if req.method == "PUT":
            n["i"] += 1
            if n["i"] == 2:
                p["use"](p["new"])
                for _ in range(md.MAX_FAILURES + 1):
                    assert p["c"].get(req.url.path).status_code == 403
                p["use"](p["old"])
    p["hooks"]["before"] = junk_mid_send
    assert _send(p, code)["state"] == "sent"
    assert _seat_code(p)["state"] == "arrived"


def test_a_seat_answer_off_a_piece_boundary_is_refused(pair):
    """A nonce must never seal two different runs of bytes, whatever the seat (or its address) says."""
    p = pair
    code = _show_code(p)
    puts = []

    def off_by_one(req, resp):
        if req.method != "PUT":
            return resp
        puts.append(req)
        b = json.loads(resp.content)
        return httpx.Response(200, json={**b, "received": b["received"] + 1})
    p["hooks"]["after"] = off_by_one
    r = _send(p, code)
    assert r["state"] == "failed" and len(puts) == 1
    assert not config.get_settings().get("moved_away_at")


def test_a_code_cannot_check_apply_or_discard(monkeypatch):
    """Through the real app: the exempt prefix is the signed routes only."""
    import dashboard
    dashboard._auth_failures.clear()
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
    c = TestClient(dashboard.app, raise_server_exceptions=False)
    uid = "0" * 32
    assert c.post(f"/api/move/uploads/{uid}/apply", json={"confirm": True}).status_code == 401
    assert c.post(f"/api/move/uploads/{uid}/check", json={}).status_code == 401
    assert c.delete(f"/api/move/uploads/{uid}").status_code == 401
    assert c.post("/api/move/code", json={}).status_code == 401
    assert c.post("/api/move/direct/open", content=b"{}").status_code != 401   # reaches its own check


# ── US3: drops and restarts ──────────────────────────────────────────────────

def test_a_flaky_line_still_finishes(pair):
    p = pair
    code = _show_code(p)
    n = {"i": 0}

    def flaky(_req):
        n["i"] += 1
        if n["i"] % 3 == 0:
            raise httpx.ConnectError("dropped")
    p["hooks"]["before"] = flaky
    assert _send(p, code)["state"] == "sent"
    assert _seat_code(p)["state"] == "arrived"


def test_an_interrupted_send_carries_on_even_after_the_code_ran_out(pair, monkeypatch):
    p = pair
    code = _show_code(p)
    n = {"i": 0}

    def dies_after_two_pieces(req):
        if req.method == "PUT":
            n["i"] += 1
            if n["i"] > 2:
                raise httpx.ConnectError("gone")
    p["hooks"]["before"] = dies_after_two_pieces
    monkeypatch.setattr(md, "PATIENCE", 0)
    r = _send(p, code)
    assert r["state"] == "interrupted"
    saved = json.loads((p["old"].data / "move-out" / "send.json").read_text())
    assert saved["sent"] == 2 * 1024 and config.get_settings().get("moved_away_at")
    # The page offers Carry on sending after a restart.
    md._SEND.clear()
    md._SEND.update(state="idle")
    assert p["c"].get("/api/move/send").json()["state"] == "interrupted"
    # The code's 30 minutes pass; the send that started in time may still finish.
    cp = p["new"].data / "move-code.json"
    c = json.loads(cp.read_text())
    c["expires_at"] = time.time() - 1
    cp.write_text(json.dumps(c))
    p["hooks"]["before"] = None
    monkeypatch.setattr(md, "PATIENCE", 3600)
    md._run_resume(saved)
    assert md._SEND["state"] == "sent"
    assert _seat_code(p)["state"] == "arrived"


def test_arrived_moves_wait_a_week_and_unsent_ones_a_day(pair):
    p = pair
    code = _show_code(p)
    assert _send(p, code)["state"] == "sent"
    p["use"](p["new"])
    d = next((p["new"].data / "move-in").iterdir())
    two_days = time.time() + 2 * 86400
    assert move_api.prune_stale(now=two_days) == 0 and d.exists()
    assert move_api.prune_stale(now=time.time() + 8 * 86400) == 1 and not d.exists()
    assert not (p["new"].data / "move-code.json").exists()


# ── US4: the old copy stands down ────────────────────────────────────────────

def test_a_send_the_seat_throws_away_puts_the_old_copy_back(pair):
    p = pair
    code = _show_code(p)
    n = {"i": 0}

    def owner_cancels(req):
        if req.method == "PUT":
            n["i"] += 1
            if n["i"] == 2:
                p["use"](p["new"])
                p["c"].delete("/api/move/code")
                p["use"](p["old"])
    p["hooks"]["before"] = owner_cancels
    r = _send(p, code)
    assert r["state"] == "failed"
    s = config.get_settings()
    assert not s.get("moved_away_at") and s.get("posting_enabled") is True and s.get("polling_paused") is False
    assert not (p["old"].data / "move-out").exists()


def test_a_second_piece_while_one_is_in_flight_waits(pair, monkeypatch):
    """REVIEW4750: one 32 MB body in memory at a time; the sender already retries a 425."""
    from routes import move_direct_api

    class _Busy:
        def locked(self):
            return True

    monkeypatch.setattr(move_direct_api, "_PIECE_BUSY", _Busy())
    p = pair
    p["use"](p["new"])
    assert p["c"].put(f"/api/move/direct/{'a' * 32}?offset=0", content=b"x").status_code == 425
