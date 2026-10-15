"""4.77.1: the DA poster's client must use the official API, not the dead cookie scrape.

Editing an image deviation looks its GUID up by listing the gallery. The poster built its
DAClient without client_id/secret, so ``_use_oauth`` was False and the lookup went to the
``_napi`` gallery endpoint DeviantArt removed (404) — every image edit failed.
"""
import asyncio

from posting.platforms.deviantart import DeviantArtPoster


def test_the_posting_client_uses_the_official_api(monkeypatch):
    creds = {"da_client_id": "71075", "da_client_secret": "s3cret", "da_refresh_token": "r",
             "da_target_user": "Inkwolf", "da_cookie": ""}
    poster = DeviantArtPoster.__new__(DeviantArtPoster)
    poster._client = None
    monkeypatch.setattr(poster, "_resolve_creds", lambda platform, settings: creds, raising=False)

    class _Stop(Exception):
        pass

    def _stop(*a, **k):
        raise _Stop

    monkeypatch.setattr(poster, "_fingerprint", _stop, raising=False)
    try:
        asyncio.run(poster._ensure_client())
    except _Stop:
        pass
    assert poster._client._use_oauth, "without app creds the gallery lookup hits the removed _napi endpoint"
    assert poster._client.target_user == "Inkwolf"
