"""Bring things over from PostyBirb (4.69.0 logins; 4.88.0 spec 060: the library, and uploads).

Every route needs the dashboard session (the middleware) and the person in a browser: a
paired install's API key is refused (FR-SEC-004). Desktop routes read PostyBirb's own folder
and are 404 on a server or for anyone but the person at the machine. Upload routes are the
server/seat way in: the person sends a zip of the folder (pb_upload), over https, from the
machine itself or over Tailscale only (FR-SEC-005).
"""
import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from routes.move_api import _person_only

pb_import_router = APIRouter(prefix="/api/pb-import", tags=["pb-import"], dependencies=[Depends(_person_only)])

_SECURE = "Uploads that carry logins need a secure (https) address."
_JOB: dict = {"state": "idle"}           # the one library import at a time, polled by the page
_JOB_LOCK = threading.Lock()


def _desktop_only(request: Request) -> None:
    from dashboard import _client_is_loopback
    from posting.scheduler import detect_runtime_mode
    if detect_runtime_mode() == "server" or not _client_is_loopback(request):
        raise HTTPException(404, "Not found")


def _ids(body: dict) -> list[str]:
    if body.get("all") is True:
        return ["*"]
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise HTTPException(400, "ids must be a list")
    if not ids and body.get("extras") is not True:
        raise HTTPException(400, "Tick at least one.")
    return [str(i) for i in ids][:5000]


@pb_import_router.get("/scan")
def pb_scan(request: Request):
    _desktop_only(request)
    import pb_import
    return pb_import.scan()


@pb_import_router.post("/apply")
def pb_apply(request: Request, body: dict):
    _desktop_only(request)
    import pb_import
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise HTTPException(400, "ids must be a list")
    return {"results": pb_import.apply([str(i) for i in ids][:100])}


@pb_import_router.post("/skip")
def pb_skip(request: Request):
    """Not now: the sign-up screen isn't shown again (Settings → Accounts still has the button)."""
    _desktop_only(request)
    import config
    config.save_settings({"pb_import_asked": True})
    return {"ok": True}


# ── the library (desktop reads its own folder; a server reads an upload) ──────

def _start_job(ids: list[str], base=None, uid: str | None = None, extras: bool = False) -> dict:
    import pb_import
    import pb_upload
    with _JOB_LOCK:
        if _JOB.get("state") == "running":
            raise HTTPException(409, "An import is already running.")
        if uid is not None and not pb_upload.LOCK.acquire(blocking=False):
            raise HTTPException(409, "Busy. Try again in a moment.")
        _JOB.clear()
        _JOB.update(state="running", done=0, total=0, results=[])

    def _progress(n, total):
        _JOB.update(done=n, total=total)

    def _run():
        outcome: dict = {"state": "failed", "error": "stopped"}
        try:
            saved = pb_import.apply_extras(base) if extras else None      # US3: saved descriptions, tag groups
            results = pb_import.apply_submissions(ids, base=base, progress=_progress) if ids else []
            outcome = {"state": "done", "results": results, "extras": saved}
        except Exception as e:                    # the page shows it; the thread must not die silently
            outcome = {"state": "failed", "error": type(e).__name__}
        finally:
            if uid is not None:
                pb_upload.delete(uid)             # the zip's logins never outlive the import (FR-SEC-008)
                pb_upload.LOCK.release()
            _JOB.update(outcome)                  # "finished" only once the upload is gone
    threading.Thread(target=_run, daemon=True, name="pb-library-import").start()
    return {"started": True}


@pb_import_router.get("/submissions")
def pb_submissions(request: Request, page: int = 0):
    _desktop_only(request)
    import pb_import
    return pb_import.scan_submissions(page)


@pb_import_router.post("/submissions/apply")
def pb_submissions_apply(request: Request, body: dict):
    _desktop_only(request)
    return _start_job(_ids(body), extras=body.get("extras") is True)


@pb_import_router.get("/submissions/status")
def pb_submissions_status():
    return dict(_JOB)


@pb_import_router.get("/thumb/{sid}")
def pb_thumb(request: Request, sid: str):
    _desktop_only(request)
    import pb_import
    return _image(pb_import.thumb_path(sid))


def _image(path):
    from posting import media_kinds
    if path is None or media_kinds.kind_of(path.name) != "image":
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type=media_kinds.mime_for(path.name),
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


# ── server / seat: the uploaded folder ────────────────────────────────────────

def _secure(request: Request) -> None:
    import pb_upload
    if not pb_upload.secure(request.url.scheme, request.client.host if request.client else ""):
        raise HTTPException(403, _SECURE)


def _base(uid: str):
    import pb_upload
    try:
        return pb_upload.base(uid)
    except KeyError:
        raise HTTPException(404, "That upload has gone. Send it again.")


@pb_import_router.post("/uploads")
def up_start(request: Request):
    _secure(request)
    import pb_upload
    if not pb_upload.LOCK.acquire(blocking=False):
        raise HTTPException(409, "Busy. Try again in a moment.")
    try:
        return {"uid": pb_upload.start()}
    finally:
        pb_upload.LOCK.release()


@pb_import_router.post("/uploads/{uid}/files")
def up_file(request: Request, uid: str, body: dict):
    _secure(request)
    import pb_upload
    try:
        size = int(body.get("size"))
    except (TypeError, ValueError):
        raise HTTPException(400, "The file's size is missing.")
    try:
        return pb_upload.add_file(uid, size)
    except KeyError:
        raise HTTPException(404, "That upload has gone. Start again.")
    except OverflowError:
        raise HTTPException(413, "That's more than 2 GB.")
    except pb_upload.Refused as e:
        raise HTTPException(400, str(e))


@pb_import_router.put("/uploads/{uid}/files/{fid}")
async def up_piece(request: Request, uid: str, fid: str, offset: int):
    _secure(request)
    import pb_upload
    buf = bytearray()
    async for chunk in request.stream():            # never hold more than a piece
        buf += chunk
        if len(buf) > pb_upload.PIECE_BYTES:
            raise HTTPException(413, "That piece is too big.")
    try:
        return {"received": pb_upload.write_piece(uid, fid, offset, bytes(buf))}
    except KeyError:
        raise HTTPException(404, "That upload has gone. Start again.")
    except ValueError as e:
        raise HTTPException(409, {"message": "Out of step; carry on from here.", "received": e.args[0]})
    except OverflowError:
        raise HTTPException(413, "That's more than the file's size.")


@pb_import_router.post("/uploads/{uid}/finish")
def up_finish(request: Request, uid: str):
    _secure(request)
    import pb_upload
    if not pb_upload.LOCK.acquire(blocking=False):
        raise HTTPException(409, "Busy. Try again in a moment.")
    try:
        return {"ok": True, "has": pb_upload.take(uid)}
    except KeyError:
        raise HTTPException(404, "That upload has gone. Start again.")
    except pb_upload.Refused as e:
        raise HTTPException(400, str(e))
    finally:
        pb_upload.LOCK.release()


@pb_import_router.get("/uploads/{uid}/submissions")
def up_submissions(uid: str, page: int = 0):
    import pb_import
    return pb_import.scan_submissions(page, base=_base(uid))


@pb_import_router.post("/uploads/{uid}/submissions/apply")
def up_submissions_apply(uid: str, body: dict):
    return _start_job(_ids(body), base=_base(uid), uid=uid, extras=body.get("extras") is True)


@pb_import_router.get("/uploads/{uid}/logins")
def up_logins(uid: str):
    import pb_import
    return pb_import.scan(base=_base(uid))


@pb_import_router.post("/uploads/{uid}/logins/apply")
def up_logins_apply(uid: str, body: dict):
    import pb_import
    import pb_upload
    base = _base(uid)
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise HTTPException(400, "ids must be a list")
    if not pb_upload.LOCK.acquire(blocking=False):
        raise HTTPException(409, "Busy. Try again in a moment.")
    try:
        return {"results": pb_import.apply([str(i) for i in ids][:100], base=base)}
    finally:
        pb_upload.LOCK.release()


@pb_import_router.get("/uploads/{uid}/thumb/{sid}")
def up_thumb(uid: str, sid: str):
    import pb_import
    return _image(pb_import.thumb_path(sid, base=_base(uid)))


@pb_import_router.delete("/uploads/{uid}")
def up_delete(uid: str):
    import pb_upload
    if not pb_upload.LOCK.acquire(blocking=False):
        raise HTTPException(409, "Busy: an import is running. Try again when it finishes.")
    try:
        pb_upload.delete(uid)
    finally:
        pb_upload.LOCK.release()
    return {"ok": True}
