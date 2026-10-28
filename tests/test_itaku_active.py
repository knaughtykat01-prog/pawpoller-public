"""Itaku showed as ready to post in the publish matrix for an install that never set it up (ITAKUACTIVE)."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from routes.editor_api import editor_router


def _cells(story_archive, **settings):
    config.save_settings({"posting_story_archive_path": str(story_archive), **settings})
    app = FastAPI()
    app.include_router(editor_router)
    r = TestClient(app).get("/api/editor/stories/Test_Story/publish-check")
    assert r.status_code == 200, r.text
    return r.json()["matrix"][0]["cells"]


def test_itaku_without_a_token_is_not_ready(story_archive):
    assert _cells(story_archive)["ik"]["status"] == "no_credentials"


def test_itaku_with_a_token_is_offered(story_archive):
    assert _cells(story_archive, ik_auth_token="tok")["ik"]["status"] != "no_credentials"
