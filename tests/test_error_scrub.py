"""ASVS V16.5.1 — server errors must not leak internal detail to the client.

Many routes raise HTTPException(500, detail=str(e)), which without the scrubbing
handler would return raw exception text (filesystem paths, network errors). The
handler in dashboard.py replaces 5xx detail with a generic message while leaving
client-facing 4xx detail intact (intentional validation messages the SPA shows).
"""
import config
from fastapi.testclient import TestClient
from fastapi import HTTPException


def _app(monkeypatch):
    # Open instance so the test routes below aren't auth-gated.
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    import dashboard
    app = dashboard.app
    # Register throwaway routes that raise the two shapes we care about.
    if not any(r.path == "/api/_test_500" for r in app.routes):
        @app.get("/api/_test_500")
        def _boom_500():
            raise HTTPException(500, detail="/srv/app/private/path leaked")

        @app.get("/api/_test_400")
        def _boom_400():
            raise HTTPException(400, detail="Title must be under 50 characters")

        @app.get("/api/_test_unhandled")
        def _boom_unhandled():
            raise RuntimeError("raw traceback text with /paths and secrets")

    return TestClient(app, raise_server_exceptions=False)


def test_5xx_detail_is_scrubbed(monkeypatch):
    r = _app(monkeypatch).get("/api/_test_500")
    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "Internal server error"
    assert "private" not in r.text and "/srv" not in r.text


def test_4xx_detail_is_preserved(monkeypatch):
    # 4xx are operator-facing validation messages — must pass through intact.
    r = _app(monkeypatch).get("/api/_test_400")
    assert r.status_code == 400
    assert r.json()["detail"] == "Title must be under 50 characters"


def test_unhandled_exception_is_generic(monkeypatch):
    r = _app(monkeypatch).get("/api/_test_unhandled")
    assert r.status_code == 500
    assert "traceback" not in r.text and "secret" not in r.text
