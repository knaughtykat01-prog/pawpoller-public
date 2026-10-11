"""Bring logins over from PostyBirb (4.69.0, spec 033 phase 8). Desktop only.

Both routes need the dashboard session (the middleware) and the person at the
machine; on a server they are 404 — PostyBirb's files are on someone's computer.
"""
from fastapi import APIRouter, HTTPException, Request

pb_import_router = APIRouter(prefix="/api/pb-import", tags=["pb-import"])


def _desktop_only(request: Request) -> None:
    from dashboard import _client_is_loopback
    from posting.scheduler import detect_runtime_mode
    if detect_runtime_mode() == "server" or not _client_is_loopback(request):
        raise HTTPException(404, "Not found")


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
