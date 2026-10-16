"""Small security Lows closed in 4.78.0 (SECLOW466, REVIEW4760)."""
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]


def test_rule34_connect_never_echoes_the_error(monkeypatch):
    from routes import r34_api

    class Rule34Client:
        def __init__(self, *a):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def validate_session(self):
            raise RuntimeError("GET https://api.example/?api_key=SECRET failed")
    monkeypatch.setattr("clients.r34.client.Rule34Client", Rule34Client)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(r34_api.r34_connect({"username": "SecondFur", "api_key": "k", "user_id": "1"}))
    assert exc.value.status_code == 502 and "SECRET" not in exc.value.detail


def test_export_filenames_keep_only_safe_characters():
    from routes import api, r34_api
    for mod in (api, r34_api):
        r = mod._csv_response([], 'r34_snapshots_a"; x=\r\nb.csv')
        assert r.headers["content-disposition"] == 'attachment; filename="r34_snapshots_a___x___b.csv"'


@pytest.mark.parametrize("body", [{"platform": "fa\nFAKE LINE", "comment_id": "1"}, {"platform": "FA", "comment_id": "1"}])
def test_inbox_refuses_an_odd_platform(body):
    from routes import inbox_api
    with pytest.raises(HTTPException):
        inbox_api.set_handled(body)
    with pytest.raises(HTTPException):
        inbox_api.set_all_handled({"platform": body["platform"]})


@pytest.mark.repo_only
def test_installer_stage_goes_through_env_and_leaves_no_trailing_space():
    yml = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
    assert "/DMyAppStage=$env:APP_STAGE" in yml and '/DMyAppStage="${{' not in yml
    iss = (ROOT / "installer/PawPoller.iss").read_text(encoding="utf-8")
    assert '#if MyAppStage == ""\nAppVerName={#MyAppName} {#MyAppVersion}\n' in iss
