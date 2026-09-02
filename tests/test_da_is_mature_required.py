"""DeviantArt requires is_mature on every publish (3.9.7).

    400 invalid_request — {"is_mature": "is_mature is required"}

`is_mature` was only sent when it was true, so DA never saw it on a
general-rated piece. That blocked **every SFW image post** and only those —
which is exactly why it survived: the mature path filled the field in and
worked, so the failure looked situational rather than categorical.

The same block had a second fault: `mature_classification` was assigned to the
key `mature_classification[]` inside a loop, so each value overwrote the last
and only one ever reached DeviantArt.
"""
from __future__ import annotations

import pytest

from clients.da.client import DAClient


class _Capture:
    """Stands in for the HTTP client and keeps the form DA would have received."""

    def __init__(self, payload=None):
        self.sent = None
        self._payload = payload or {"deviationid": "abc", "url": "https://da/x"}

    async def post(self, url, data=None, **kw):
        self.sent = data
        return _Resp(self._payload)


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def client(monkeypatch):
    c = DAClient(target_user="secondfur")
    cap = _Capture()
    monkeypatch.setattr(c, "_http", cap)
    c._capture = cap
    return c


# ── stash/publish — the image path ────────────────────────────

@pytest.mark.asyncio
async def test_a_general_rated_image_still_sends_is_mature(client):
    """The `Blows_a_kiss` failure. Omitting the field is not the same as
    saying false, and DA only accepts the latter."""
    await client.oauth_stash_publish("5283659895292231", is_mature=False,
                                     access_token="tok")
    assert client._capture.sent["is_mature"] == "0"


@pytest.mark.asyncio
async def test_a_mature_image_sends_one(client):
    await client.oauth_stash_publish("123", is_mature=True, mature_level="strict",
                                     access_token="tok")
    sent = client._capture.sent
    assert sent["is_mature"] == "1"
    assert sent["mature_level"] == "strict"


@pytest.mark.asyncio
async def test_every_mature_classification_survives(client):
    """The `[]` form kept only the last one, so a piece marked several ways
    arrived marked as whichever came last."""
    await client.oauth_stash_publish(
        "123", is_mature=True, mature_level="strict",
        mature_classification=["sexual", "nudity", "gore"], access_token="tok")
    sent = client._capture.sent
    values = {v for k, v in sent.items() if k.startswith("mature_classification")}
    assert values == {"sexual", "nudity", "gore"}


@pytest.mark.asyncio
async def test_mature_fields_stay_off_a_general_piece(client):
    """is_mature=0 must not drag mature_level along with it."""
    await client.oauth_stash_publish("123", is_mature=False, mature_level="strict",
                                     mature_classification=["sexual"], access_token="tok")
    sent = client._capture.sent
    assert sent["is_mature"] == "0"
    assert "mature_level" not in sent
    assert not any(k.startswith("mature_classification") for k in sent)


# ── literature/create — same fault, not yet hit ───────────────

@pytest.mark.asyncio
async def test_general_literature_also_sends_is_mature(client):
    """Fixed in step rather than waiting for stories to hit it too."""
    await client.oauth_create_literature(
        title="T", body="<p>b</p>", access_token="tok", is_mature=False)
    assert client._capture.sent["is_mature"] == "0"


@pytest.mark.asyncio
async def test_literature_classifications_are_all_sent(client):
    await client.oauth_create_literature(
        title="T", body="<p>b</p>", access_token="tok", is_mature=True,
        mature_level="moderate", mature_classification=["language", "ideology"])
    sent = client._capture.sent
    values = {v for k, v in sent.items() if k.startswith("mature_classification")}
    assert values == {"language", "ideology"}


@pytest.mark.asyncio
async def test_the_two_create_paths_agree_on_the_field(client, monkeypatch):
    """One rule in two places is how the stash path drifted from literature in
    the first place."""
    await client.oauth_stash_publish("1", is_mature=False, access_token="tok")
    stash = client._capture.sent["is_mature"]
    await client.oauth_create_literature(title="T", body="b", access_token="tok",
                                         is_mature=False)
    assert client._capture.sent["is_mature"] == stash
