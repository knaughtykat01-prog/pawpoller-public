"""Send a PawPoller straight to a hosted seat (4.75.0, spec 036).

Spec 035's move takes two trips: save a moving file, then upload it to the seat in a browser. Here
the seat shows a one-time code, the person types it into their old PawPoller, and the old copy
builds the same moving file and uploads it itself.

The code never crosses the network. Both sides turn it into two keys with scrypt, salted with the
seat's name (research R2):
  * the auth key signs every request (X-Move-Auth: an HMAC of method, path, query and body);
  * the seal key seals every piece with AES-GCM, so Cloudflare (which unwraps HTTPS on the way to a
    seat) sees neither the logins nor the library (clarification Q1).
The seat keeps the two keys, never the code.

A code allows UPLOADING one move and nothing else. Checking, applying and throwing it away stay
spec 035's signed-in routes. From the moment a move has arrived, it is spec 035's code (the seat
supplies the logins phrase, which is derived from the same code). A code lives 30 minutes until a
send starts, works once, and five failed checks void it for 15 minutes.

Logs carry events and sizes only: never the code, a key, a signature or a login.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

import config
from routes import move_api

logger = logging.getLogger(__name__)

ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"     # 32 letters, no 0/O or 1/I
RANDOM_LEN = 12                                  # 60 bits
CODE_TTL = 30 * 60
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
AUTH_HEADER = "X-Move-Auth"
SEAT_NAME = re.compile(r"[a-z][a-z0-9-]{1,30}")  # the box's seat-name rule; always .fullmatch
_BASE_URL = re.compile(r"^https://paw-([a-z][a-z0-9-]{1,30})\.", re.IGNORECASE)
_SALT = "pawpoller-direct-move-v1:"
_PREFIX_RE = re.compile(r"[0-9a-f]{16}")
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}         # the same cost as spec 035's phrase


# ── The code ─────────────────────────────────────────────────────────────────

class BadCode(ValueError):
    pass


def new_code(seat: str) -> str:
    rnd = "".join(secrets.choice(ALPHABET) for _ in range(RANDOM_LEN))
    return f"{seat}-{rnd[0:4]}-{rnd[4:8]}-{rnd[8:12]}"


def parse_code(text) -> tuple[str, str]:
    """(seat, random part). Spaces and case don't matter; the random part is the last 12 letters or
    numbers, so a seat name with dashes still parses."""
    s = re.sub(r"\s+", "", str(text or ""))
    tail, i = [], len(s)
    while i > 0 and len(tail) < RANDOM_LEN:
        i -= 1
        if s[i] != "-":
            tail.append(s[i].upper())
    rnd, seat = "".join(reversed(tail)), s[:i].rstrip("-").lower()
    if not s[:i].endswith("-") or len(rnd) != RANDOM_LEN or any(c not in ALPHABET for c in rnd) or not SEAT_NAME.fullmatch(seat):
        raise BadCode()
    return seat, rnd


def derive(seat: str, rnd: str) -> tuple[bytes, bytes]:
    """(auth key, seal key). Salted with the seat's name: a code typed for one seat is useless at another."""
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    raw = Scrypt(salt=(_SALT + seat).encode(), length=64, **_SCRYPT).derive(rnd.encode())
    return raw[:32], raw[32:]


def logins_phrase(seal_key: bytes) -> str:
    """The "phrase" spec 035 seals the logins with, here derived instead of typed."""
    return base64.urlsafe_b64encode(hmac.new(seal_key, b"logins", hashlib.sha256).digest()).decode()


def sign(auth_key: bytes, method: str, path: str, query: str, body: bytes) -> str:
    msg = "\n".join([method.upper(), path, query or "", hashlib.sha256(body or b"").hexdigest()])
    return hmac.new(auth_key, msg.encode(), hashlib.sha256).hexdigest()


def verify(auth_key: bytes, given, method: str, path: str, query: str, body: bytes) -> bool:
    return isinstance(given, str) and hmac.compare_digest(sign(auth_key, method, path, query, body).encode(),
                                                          given.encode("utf-8", "replace"))


class BadPiece(Exception):
    pass


def _nonce(prefix: bytes, index: int) -> bytes:
    return prefix + index.to_bytes(4, "big")


def _aad(uid: str, index: int, size: int) -> bytes:
    return f"{uid}:{index}:{size}".encode()


def seal_piece(seal_key: bytes, prefix: bytes, index: int, uid: str, size: int, plain: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(seal_key).encrypt(_nonce(prefix, index), plain, _aad(uid, index, size))


def open_piece(seal_key: bytes, prefix: bytes, index: int, uid: str, size: int, sealed: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        return AESGCM(seal_key).decrypt(_nonce(prefix, index), sealed, _aad(uid, index, size))
    except (InvalidTag, ValueError):
        raise BadPiece() from None


def this_seat() -> str | None:
    """This install's seat name, or None when it isn't a hosted seat (homebox.sh sets both)."""
    if os.environ.get("PAWPOLLER_HOSTED") != "1":
        return None
    m = _BASE_URL.match(os.environ.get("PAWPOLLER_PUBLIC_BASE_URL") or "")
    name = m.group(1).lower() if m else ""
    return name if SEAT_NAME.fullmatch(name) else None


def seat_address(seat: str) -> str:
    return f"https://paw-{seat}.{os.environ.get('PAWPOLLER_SEAT_DOMAIN') or 'pawpoller.com'}"


# ── The code's state on the seat (move-code.json) ───────────────────────────

_CODE_LOCK = threading.RLock()


def _code_path() -> Path:
    return Path(config.DATA_DIR) / "move-code.json"


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # never readable by others, even briefly
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data))
    os.replace(tmp, path)


def load_code() -> dict | None:
    try:
        c = json.loads(_code_path().read_text("utf-8"))
        return c if isinstance(c, dict) else None
    except (OSError, ValueError):
        return None


def save_code(c: dict) -> None:
    _write_private(_code_path(), c)


def clear_code() -> None:
    _code_path().unlink(missing_ok=True)


def _upload_meta(uid) -> dict | None:
    if not isinstance(uid, str) or not move_api._ID_RE.fullmatch(uid):
        return None
    try:
        return json.loads((move_api._in_dir() / uid / "upload.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None


def code_state(c: dict | None, now: float) -> str:
    """none · waiting · sending · arrived · expired · locked (data-model.md)."""
    if not c:
        return "none"
    if (c.get("locked_until") or 0) > now:
        return "locked"
    if c.get("upload_id"):
        meta = _upload_meta(c["upload_id"])
        if not meta:
            return "expired"                           # cleared (pruned, thrown away): the code is spent
        return "arrived" if meta.get("arrived_at") else "sending"
    if not c.get("auth_key") or now >= float(c.get("expires_at") or 0):
        return "expired"
    return "waiting"


def _drop_direct_uploads() -> None:
    root = move_api._in_dir()
    for d in (root.iterdir() if root.is_dir() else []):
        meta = _upload_meta(d.name)
        if meta and meta.get("direct"):
            shutil.rmtree(d, ignore_errors=True)


def _file_route_upload() -> bool:
    return any(not (_upload_meta(d.name) or {}).get("direct") for d in move_api._open_uploads())


def _fail(c: dict, now: float) -> None:
    """A failed check against a live code. The fifth voids it and locks new codes for a while."""
    c["failures"] = int(c.get("failures") or 0) + 1
    if c["failures"] >= MAX_FAILURES:
        c.update(locked_until=now + LOCK_SECONDS, auth_key=None, seal_key=None, upload_id=None)
        _drop_direct_uploads()
        logger.warning("Move code locked after %d failed checks.", MAX_FAILURES)
    save_code(c)


# ── Seat: the owner's routes (signed in) ─────────────────────────────────────

owner_router = APIRouter(prefix="/api/move", tags=["move"], dependencies=[Depends(move_api._person_only)])


def _seat_or_404() -> str:
    seat = this_seat()
    if not seat:
        raise HTTPException(404, detail="Only a hosted seat can receive a move this way.")
    return seat


@owner_router.get("/code")
def code_get():
    if not this_seat():
        return {"state": "unavailable"}                 # the Settings card asks on every install; it hides on this
    with _CODE_LOCK:
        c, now = load_code(), time.time()
        st = code_state(c, now)
        out = {"state": st}
        if st == "waiting":
            out["expires_at"] = c["expires_at"]
        elif st == "locked":
            out["locked_until"] = c["locked_until"]
        elif st in ("sending", "arrived"):
            s = move_api._status(move_api._in_dir() / c["upload_id"])
            out.update(upload_id=s["id"], received=s["received"], size=s["size"])
        return out


@owner_router.post("/code")
def code_show(body: dict | None = None):
    seat = _seat_or_404()
    body = body or {}
    move_api._busy()                                    # never under a move being brought in
    try:
        with _CODE_LOCK:
            c, now = load_code(), time.time()
            st = code_state(c, now)
            if st == "locked":
                raise HTTPException(429, detail={"message": "Too many wrong tries. Show a new code after the time shown.",
                                                 "locked_until": c["locked_until"]})
            if st == "arrived" and body.get("replace") is not True:
                raise HTTPException(409, detail={"message": "A move has arrived and is waiting. A new code throws it away.",
                                                 "arrived": True})
            if _file_route_upload():
                raise HTTPException(409, detail="A moving file is part-way in. Cancel it first.")
            _drop_direct_uploads()
            code = new_code(seat)
            auth_key, seal_key = derive(*parse_code(code))
            expires = now + CODE_TTL
            save_code({"seat": seat, "auth_key": auth_key.hex(), "seal_key": seal_key.hex(), "created_at": now,
                       "expires_at": expires, "failures": 0, "locked_until": None, "upload_id": None})
            logger.info("Move code shown.")
            return {"code": code, "expires_at": expires}
    finally:
        move_api._APPLY_LOCK.release()


@owner_router.delete("/code")
def code_cancel():
    _seat_or_404()
    move_api._busy()
    try:
        with _CODE_LOCK:
            c = load_code()
            if c and (c.get("locked_until") or 0) > time.time():
                c.update(auth_key=None, seal_key=None, upload_id=None)   # keep the lock; drop the rest
                save_code(c)
            else:
                clear_code()
            _drop_direct_uploads()
    finally:
        move_api._APPLY_LOCK.release()
    return {"ok": True}


def phrase_for_upload(meta: dict) -> str:
    """spec 035's check/apply ask for the phrase of a direct upload here."""
    with _CODE_LOCK:
        c = load_code()
        if not c or c.get("upload_id") != meta.get("id") or not c.get("seal_key"):
            raise HTTPException(409, detail="The code for this move is gone. Show a new code and send it again.")
        return logins_phrase(bytes.fromhex(c["seal_key"]))


def forget_upload(uid: str) -> None:
    """A direct upload was brought in, thrown away or pruned: its code is spent."""
    with _CODE_LOCK:
        c = load_code()
        if c and c.get("upload_id") == uid:
            clear_code()


# ── Seat: the signed routes (signed out; /api/move/direct/ is auth-exempt) ──

direct_router = APIRouter(prefix="/api/move/direct", tags=["move"])


def _checked(request: Request, body: bytes) -> dict:
    """The live code, if this request is signed with it. Raises otherwise (and counts a failure)."""
    if not this_seat():
        raise HTTPException(404, detail="Not found.")
    c, now = load_code(), time.time()
    st = code_state(c, now)
    if st == "locked":
        raise HTTPException(429, detail="Too many wrong tries. Ask for a new code on the seat in 15 minutes.")
    if st == "none":
        raise HTTPException(404, detail="There's no move code waiting on this seat.")
    if st == "arrived":
        raise HTTPException(409, detail="This code has already been used.")
    if st == "expired":
        raise HTTPException(410, detail="That code has run out. Show a new one on the seat.")
    if not verify(bytes.fromhex(c["auth_key"]), request.headers.get(AUTH_HEADER), request.method,
                  request.url.path, request.url.query, body):
        # Once a send is under way there is nothing left to guess, so a wrong signature only gets
        # turned away. Counting it would let anyone void a half-sent move with five junk requests.
        if not c.get("upload_id"):
            _fail(c, now)
        logger.warning("Direct move: a request was refused (signature).")
        raise HTTPException(403, detail="That code doesn't match this seat.")
    return c


async def _body(request: Request, cap: int) -> bytes:
    buf = bytearray()
    async for chunk in request.stream():                # never buffer more than a piece
        buf += chunk
        if len(buf) > cap:
            raise HTTPException(413, detail="That's too big.")
    return bytes(buf)


@direct_router.post("/open")
async def direct_open(request: Request):
    raw = await _body(request, 4096)
    with _CODE_LOCK:
        c = _checked(request, raw)
        try:
            body = json.loads(raw)
            size, prefix = int(body["size"]), str(body["nonce_prefix"])
            seat, theirs = str(body["seat"]), str(body.get("app_version") or "")
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, detail="That request is damaged.")
        if size <= 0 or not _PREFIX_RE.fullmatch(prefix) or seat != c["seat"]:
            raise HTTPException(400, detail="That request is damaged.")
        if move_api._version_tuple(theirs) > move_api._version_tuple(config.APP_VERSION):
            raise HTTPException(409, detail=f"Your old PawPoller is {theirs} and this seat is {config.APP_VERSION}. "
                                            "Ask for the seat to be updated first.")
        if c.get("upload_id"):                          # single use: only this send may carry on
            meta = _upload_meta(c["upload_id"])
            if meta and meta["size"] == size and meta.get("nonce_prefix") == prefix:
                return move_api._status(move_api._in_dir() / meta["id"])
            raise HTTPException(409, detail="This code has already been used.")
        if move_api._open_uploads():
            raise HTTPException(409, detail="A moving file is part-way into this seat. Cancel it there first.")
        free = shutil.disk_usage(move_api._data_dir()).free
        if free < move_api._room_needed(size):
            logger.warning("Direct move: refused %d bytes with %d free.", size, free)
            raise HTTPException(507, detail=f"Your seat hasn't room for this. It needs about "
                                            f"{move_api._room_needed(size) / 1e9:.1f} GB free and has {free / 1e9:.1f} GB.")
        uid = secrets.token_hex(16)
        d = move_api._in_dir() / uid
        d.mkdir(parents=True)
        (d / "upload.json").write_text(json.dumps({"id": uid, "name": "direct", "size": size, "created_at": time.time(),
                                                   "direct": True, "nonce_prefix": prefix}), "utf-8")
        (d / "file.part").touch()
        c["upload_id"] = uid
        save_code(c)
        logger.info("Direct move: send opened (%d bytes).", size)
        return move_api._status(d)


@direct_router.get("/{uid}")
def direct_status(uid: str, request: Request):
    with _CODE_LOCK:
        c = _checked(request, b"")
        if uid != c.get("upload_id"):
            raise HTTPException(404, detail="No such send.")
        return move_api._status(move_api._in_dir() / uid)


@direct_router.put("/{uid}")
async def direct_piece(uid: str, request: Request, offset: int):
    # Before reading up to a piece into memory: only a send that is under way, for exactly this
    # upload, gets that far. Anyone else is turned away for the cost of a file read.
    with _CODE_LOCK:
        c = load_code()
        if not this_seat() or code_state(c, time.time()) != "sending" or uid != c.get("upload_id"):
            raise HTTPException(404, detail="No such send.")
    sealed = await _body(request, move_api.PIECE_BYTES + 16)
    with _CODE_LOCK:
        c = _checked(request, sealed)
        if uid != c.get("upload_id"):
            raise HTTPException(404, detail="No such send.")
        seal_key = bytes.fromhex(c["seal_key"])
    d = move_api._in_dir() / uid
    meta = _upload_meta(uid)
    if not meta:
        raise HTTPException(404, detail="No such send.")
    st = move_api._status(d)
    if offset != st["received"] or offset % move_api.PIECE_BYTES:
        raise HTTPException(409, detail={"message": "Out of step; carry on from here.", "received": st["received"]})
    index = offset // move_api.PIECE_BYTES
    try:
        plain = open_piece(seal_key, bytes.fromhex(meta["nonce_prefix"]), index, uid, st["size"], sealed)
    except BadPiece:
        with _CODE_LOCK:
            c = load_code()
            if c and c.get("upload_id") == uid:
                _fail(c, time.time())
        logger.warning("Direct move: a piece was refused (it didn't unseal).")
        raise HTTPException(403, detail="That piece doesn't match this seat's code.")
    if len(plain) != min(move_api.PIECE_BYTES, st["size"] - offset):
        raise HTTPException(400, detail="That piece is the wrong size.")
    with move_api._UPLOAD_LOCK:
        now = move_api._status(d)["received"]
        if now != offset:
            raise HTTPException(409, detail={"message": "Out of step; carry on from here.", "received": now})
        with open(d / "file.part", "ab") as f:
            f.write(plain)
        received = offset + len(plain)
        if received == st["size"]:
            meta["arrived_at"] = time.time()
            (d / "upload.json").write_text(json.dumps(meta), "utf-8")
            logger.info("Direct move: arrived (%d bytes).", received)
        else:
            os.utime(d / "upload.json")
    return {"received": received, "arrived": received == st["size"]}


# ── The old copy: sending ───────────────────────────────────────────────────

_SEND: dict = {"state": "idle"}
_SEND_LOCK = threading.Lock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
PATIENCE = 2 * 3600                                     # no progress for this long → interrupted
_BACKOFF = (5, 10, 20, 40, 60)


class _Refused(Exception):
    pass


class _Interrupted(Exception):
    pass


class _Cancelled(Exception):
    pass


def _http() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(120.0, connect=20.0))


def _send_path() -> Path:
    return move_api._out_dir() / "send.json"


def _load_send(path: Path) -> dict | None:
    try:
        s = json.loads(path.read_text("utf-8"))
        return s if isinstance(s, dict) else None
    except (OSError, ValueError):
        return None


def _detail(r: httpx.Response) -> str:
    try:
        d = r.json().get("detail")
    except ValueError:
        d = None
    if isinstance(d, dict):
        d = d.get("message")
    return str(d) if d else f"Your seat said no ({r.status_code})."


def _retrying(fn, state: dict, ok=(200,)):
    """Call fn() until the seat answers with one of `ok`. A dropped line or a seat that's restarting is
    waited out; a definite no is raised as _Refused."""
    tries = 0
    while True:
        if _STOP.is_set():
            raise _Cancelled()
        try:
            r = fn()
            if r.status_code in ok:
                return r
            if r.status_code < 500 and r.status_code not in (408, 425):
                raise _Refused(_detail(r))
        except httpx.TransportError:
            pass
        if time.time() - state["last_progress"] > PATIENCE:
            raise _Interrupted()
        delay = _BACKOFF[min(tries, len(_BACKOFF) - 1)]
        tries += 1
        _STOP.wait(delay)


def _drive(state: dict, path: Path) -> None:
    """Open the send on the seat (or find where it got to) and send every piece."""
    auth_key, seal_key = bytes.fromhex(state["auth_key"]), bytes.fromhex(state["seal_key"])
    prefix, size = bytes.fromhex(state["nonce_prefix"]), int(state["size"])
    state["last_progress"] = time.time()
    _SEND.update(state="sending", size=size, sent=int(state.get("sent") or 0))
    with _http() as client:
        def call(method, p, query="", body=b"", ctype="application/octet-stream"):
            url = state["address"] + p + (f"?{query}" if query else "")
            return client.request(method, url, content=body, headers={
                AUTH_HEADER: sign(auth_key, method, p, query, body), "Content-Type": ctype})

        if state.get("upload_id"):
            r = _retrying(lambda: call("GET", f"/api/move/direct/{state['upload_id']}"), state)
        else:
            opening = json.dumps({"seat": state["seat"], "size": size, "nonce_prefix": state["nonce_prefix"],
                                  "app_version": config.APP_VERSION}).encode()
            r = _retrying(lambda: call("POST", "/api/move/direct/open", body=opening, ctype="application/json"), state)
            state["upload_id"] = r.json()["id"]
        info = r.json()
        piece, uid = int(info["piece_bytes"]), state["upload_id"]
        # The nonce is the piece index, so the seat's counts must land on piece boundaries and the piece
        # size must never change: otherwise one nonce would seal two different runs of bytes, and whoever
        # answers for the seat's address could read both. Anything else is refused, not trusted.
        if not (1024 <= piece <= 64 * 1024 * 1024) or state.setdefault("piece", piece) != piece:
            raise _Refused(_ODD_SEAT)

        def step(n) -> int:
            if not isinstance(n, int) or not 0 <= n <= size or (n % piece and n != size):
                raise _Refused(_ODD_SEAT)
            return n
        sent = step(info["received"])
        with open(state["file"], "rb") as f:
            while sent < size:
                if _STOP.is_set():
                    raise _Cancelled()
                f.seek(sent)
                sealed = seal_piece(seal_key, prefix, sent // piece, uid, size, f.read(min(piece, size - sent)))
                r = _retrying(lambda: call("PUT", f"/api/move/direct/{uid}", f"offset={sent}", sealed), state,
                              ok=(200, 409))
                body = r.json()
                if r.status_code == 409:
                    d = body.get("detail")
                    if not (isinstance(d, dict) and isinstance(d.get("received"), int)):
                        raise _Refused(_detail(r))
                    sent = step(d["received"])          # out of step: carry on from the seat's count
                else:
                    sent = step(body.get("received"))
                state.update(sent=sent, last_progress=time.time())
                _SEND.update(sent=sent)
                _write_private(path, state)


_ODD_SEAT = "Your seat gave an answer this PawPoller doesn't understand. Update both and try again."


def _finish(state: dict, path: Path, e: BaseException | None) -> None:
    if isinstance(e, _Interrupted):
        _SEND.update(state="interrupted", error="Your seat couldn't be reached for a long time. "
                                                "Press Carry on sending when your internet is back.")
        logger.warning("Direct move: send interrupted at %d bytes.", int(state.get("sent") or 0))
        return
    shutil.rmtree(path.parent, ignore_errors=True)      # the moving file and send.json go either way
    if e is None:
        _SEND.update(state="sent", error=None)
        logger.info("Direct move: sent %d bytes.", int(state.get("size") or 0))
        return
    if state.get("paused_now"):
        move_api.resume_this_copy()                     # no move, so this copy carries on as it was
    if isinstance(e, _Cancelled):
        _SEND.update(state="cancelled", error=None)
        logger.info("Direct move: send cancelled.")
    else:
        msg = str(e) if isinstance(e, _Refused) else f"Sending stopped ({type(e).__name__})."
        _SEND.update(state="failed", error=msg)
        logger.error("Direct move: send failed (%s).", type(e).__name__)


def _run_send(seat: str, rnd: str, address: str, pause: bool) -> None:
    path = _send_path()
    state: dict = {"paused_now": False}
    try:
        auth_key, seal_key = derive(seat, rnd)
        shutil.rmtree(path.parent, ignore_errors=True)
        path.parent.mkdir(parents=True)
        # Pause BEFORE the snapshot (spec 035's reason): anything posted during a long send would
        # otherwise still be pending in it, and the seat would post it a second time.
        if pause and not config.get_settings().get("moved_away_at"):
            move_api._pause_this_copy()
            state["paused_now"] = True
        dest = path.parent / f"pawpoller-move-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.zip"
        move_api.write_move_zip(dest, logins_phrase(seal_key),
                                lambda done, total: _SEND.update(built=done, total=total))
        state.update(address=address, seat=seat, auth_key=auth_key.hex(), seal_key=seal_key.hex(), upload_id=None,
                     nonce_prefix=secrets.token_hex(8), file=str(dest), size=dest.stat().st_size, sent=0)
        _write_private(path, state)
        _drive(state, path)
        _finish(state, path, None)
    except BaseException as e:  # noqa: BLE001 — reported to the page, never raised into the thread
        _finish(state, path, e)


def _run_resume(state: dict) -> None:
    path = _send_path()
    try:
        _drive(state, path)
        _finish(state, path, None)
    except BaseException as e:  # noqa: BLE001
        _finish(state, path, e)


def _running() -> bool:
    return bool(_THREAD and _THREAD.is_alive())


def _start(target, *args) -> None:
    global _THREAD
    _STOP.clear()
    _THREAD = threading.Thread(target=target, args=args, name="move-send", daemon=True)
    _THREAD.start()


def _parsed(body: dict) -> tuple[str, str]:
    try:
        return parse_code(body.get("code"))
    except BadCode:
        raise HTTPException(400, detail="That doesn't look like a move code. Check it on your seat's screen.") from None


@owner_router.post("/send/preview")
def send_preview(body: dict):
    seat, _rnd = _parsed(body)
    return {"seat": seat, "address": seat_address(seat)}


@owner_router.post("/send")
def send_start(body: dict):
    seat, rnd = _parsed(body)
    address = seat_address(seat)
    if body.get("address") != address:
        raise HTTPException(400, detail="That address doesn't match the code. Type the code again.")
    with _SEND_LOCK:
        if _running() or move_api._EXPORT.get("state") == "building":
            raise HTTPException(409, detail="A move is already being made here.")
        _SEND.clear()
        _SEND.update(state="building", seat=seat, address=address, sent=0, size=0, error=None)
        _start(_run_send, seat, rnd, address, bool(body.get("pause", True)))
    return {"state": "building"}


@owner_router.get("/send")
def send_status():
    out = {k: _SEND.get(k) for k in ("state", "seat", "address", "sent", "size", "built", "total", "error")}
    if not _running() and out["state"] in ("idle", "building", "sending"):
        saved = _load_send(_send_path())                # after a restart: offer Carry on sending
        if saved and saved.get("file"):
            out.update(state="interrupted", seat=saved.get("seat"), address=saved.get("address"),
                       sent=saved.get("sent", 0), size=saved.get("size", 0))
        elif out["state"] != "idle":
            out["state"] = "idle"
    out["moved_away_at"] = config.get_settings().get("moved_away_at")
    return out


@owner_router.post("/send/resume")
def send_resume():
    with _SEND_LOCK:
        if _running():
            raise HTTPException(409, detail="It's already sending.")
        state = _load_send(_send_path())
        if not state or not state.get("file") or not Path(state["file"]).is_file():
            raise HTTPException(404, detail="There's nothing to carry on with. Start again with a new code.")
        _SEND.clear()
        _SEND.update(state="sending", seat=state.get("seat"), address=state.get("address"),
                     sent=state.get("sent", 0), size=state.get("size", 0), error=None)
        _start(_run_resume, state)
    return {"state": "sending"}


@owner_router.post("/send/cancel")
def send_cancel():
    if _running():
        _STOP.set()                                     # the thread tidies up and puts this copy back
        return {"ok": True}
    state = _load_send(_send_path())
    if state:
        _finish(state, _send_path(), _Cancelled())
    return {"ok": True}
