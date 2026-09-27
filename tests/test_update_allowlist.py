"""UPDALLOW (4.40.1): the in-app updater installs only official releases.

POST /api/update/apply downloads a build and RUNS it. It used to take the URL
from the request and check only that the host was GitHub — but anyone can host
a file on github.com / codeload / raw.githubusercontent.com, and the Windows
server package serves this route on 0.0.0.0. The route now derives the URL from
its own check against the pinned public repo; the request cannot choose it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config
import updater

OFFICIAL = ("https://github.com/knaughtykat01-prog/pawpoller-public/releases/download/"
            "v9.9.9/PawPoller-windows-x64.zip")


class TestOfficialAsset:

    def test_the_official_release_asset_is_accepted(self):
        assert updater.is_official_asset(OFFICIAL)

    @pytest.mark.parametrize("url", [
        # Real GitHub hosts, someone else's content — the old allowlist took all of these.
        "https://github.com/someone/evil/releases/download/v1/PawPoller-windows-x64.zip",
        "https://codeload.github.com/someone/evil/zip/refs/heads/main",
        "https://raw.githubusercontent.com/someone/evil/main/PawPoller-windows-x64.zip",
        "https://objects.githubusercontent.com/github-production-release-asset/1/x",
        # Look-alikes of the real prefix.
        "http://github.com/knaughtykat01-prog/pawpoller-public/releases/download/v1/a.zip",
        "https://github.com/knaughtykat01-prog/pawpoller-public/releases/download/../../../someone/evil/releases/download/v1/a.zip",
        "https://github.com/knaughtykat01-prog/pawpoller-public-evil/releases/download/v1/a.zip",
        "https://github.com/knaughtykat01-prog/pawpoller-public/releases/download/v1/a.zip?x=https://evil",
        "https://github.com.evil.example/knaughtykat01-prog/pawpoller-public/releases/download/v1/a.zip",
        "", None,
    ])
    def test_anything_else_is_refused(self, url):
        assert not updater.is_official_asset(url)

    def test_the_downloader_refuses_too(self):
        with pytest.raises(ValueError):
            updater.download_update("https://github.com/someone/evil/releases/download/v1/x.zip")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
    monkeypatch.setattr(config, "validate_api_key", lambda token: token == "pp_test")
    monkeypatch.delenv("PAWPOLLER_SERVER_MANAGED", raising=False)
    import dashboard
    return TestClient(dashboard.app, raise_server_exceptions=False,
                      headers={"Authorization": "Bearer pp_test"})


@pytest.fixture
def installs(monkeypatch):
    """Record what would be downloaded; never download, apply or exit."""
    got = []
    monkeypatch.setattr(updater, "check_for_update",
                        lambda: {"available": True, "download_url": OFFICIAL})
    monkeypatch.setattr(updater, "download_update", lambda url: got.append(url) or "zip")
    monkeypatch.setattr(updater, "apply_update", lambda path: None)
    import threading
    monkeypatch.setattr(threading, "Timer", lambda *a, **k: type("T", (), {"start": lambda s: None})())
    return got


class TestTheRoute:

    def test_it_installs_the_official_release_it_found_itself(self, client, installs):
        r = client.post("/api/update/apply", json={})
        assert r.status_code == 200 and installs == [OFFICIAL]

    def test_the_request_cannot_choose_the_file(self, client, installs):
        r = client.post("/api/update/apply", json={
            "download_url": "https://github.com/someone/evil/releases/download/v1/PawPoller-windows-x64.zip"})
        assert r.status_code == 400 and installs == []

    def test_the_same_official_url_from_the_frontend_still_works(self, client, installs):
        """The frontend sends back the URL /update/check gave it."""
        assert client.post("/api/update/apply", json={"download_url": OFFICIAL}).status_code == 200

    def test_nothing_to_install_is_a_400_not_a_download(self, client, installs, monkeypatch):
        monkeypatch.setattr(updater, "check_for_update", lambda: {"available": False, "download_url": None})
        assert client.post("/api/update/apply", json={}).status_code == 400 and installs == []

    def test_an_installed_server_package_is_sent_to_its_own_updater(self, client, installs, monkeypatch):
        import server_updater
        monkeypatch.setattr(server_updater, "managed", lambda env=None: True)
        r = client.post("/api/update/apply", json={})
        assert r.status_code == 400 and "server" in r.json()["detail"].lower() and installs == []


def test_an_open_instance_refuses_remote_callers():
    import dashboard
    assert "/api/update/apply" in dashboard._SENSITIVE_WHEN_OPEN_PREFIXES


# ── 4.40.2: checksums on desktop downloads, no token on update traffic ─────

import hashlib
import httpx

PAYLOAD = b"PK\x03\x04 a pretend update zip"
GOOD = hashlib.sha256(PAYLOAD).hexdigest()


def _fake_github(monkeypatch, sha_text=None, sha_status=200, seen=None):
    """Serve OFFICIAL and OFFICIAL.sha256 from a MockTransport; record requests."""
    def handler(request):
        if seen is not None:
            seen.append(request)
        if str(request.url).endswith(".sha256"):
            return httpx.Response(sha_status, text=sha_text if sha_text is not None
                                  else f"{GOOD}  PawPoller-windows-x64.zip")
        return httpx.Response(200, content=PAYLOAD)
    real = httpx.Client
    monkeypatch.setattr(updater.httpx, "Client",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))


class TestChecksums:

    def test_a_matching_download_is_returned(self, monkeypatch):
        _fake_github(monkeypatch)
        path = updater.download_update(OFFICIAL)
        assert path.read_bytes() == PAYLOAD

    def test_a_mismatch_is_refused_and_nothing_is_kept(self, monkeypatch):
        _fake_github(monkeypatch, sha_text="0" * 64 + "  PawPoller-windows-x64.zip")
        with pytest.raises(RuntimeError, match="Checksum mismatch"):
            updater.download_update(OFFICIAL)

    def test_a_release_without_a_checksum_is_refused(self, monkeypatch):
        """Fail closed: no published checksum, no in-app install."""
        _fake_github(monkeypatch, sha_status=404)
        with pytest.raises(RuntimeError, match="no checksum"):
            updater.download_update(OFFICIAL)

    def test_an_unreadable_checksum_is_refused(self, monkeypatch):
        _fake_github(monkeypatch, sha_text="not a hash")
        with pytest.raises(RuntimeError, match="unreadable"):
            updater.download_update(OFFICIAL)

    def test_ci_publishes_a_checksum_beside_every_desktop_asset(self):
        wf = open(".github/workflows/build.yml", encoding="utf-8").read()
        for asset in ("dist/PawPoller-windows-x64.zip.sha256",
                      "installer/Output/PawPoller-Setup-*.exe.sha256",
                      "installer/Output/PawPoller-*-x86_64.AppImage.sha256"):
            assert asset in wf, asset


class TestNoTokenOnUpdateTraffic:

    def test_the_download_sends_no_authorization(self, monkeypatch):
        seen = []
        monkeypatch.setattr(config, "get_settings", lambda: {"github_pat": "ghp_secret"})
        _fake_github(monkeypatch, seen=seen)
        updater.download_update(OFFICIAL)
        assert seen and all("authorization" not in r.headers for r in seen)

    def test_the_check_sends_no_authorization(self, monkeypatch):
        seen = []
        monkeypatch.setattr(config, "get_settings", lambda: {"github_pat": "ghp_secret"})
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"tag_name": "v0.0.1", "assets": [], "body": ""})
        real = httpx.Client
        monkeypatch.setattr(updater.httpx, "Client",
                            lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
        updater.check_for_update()
        assert seen and "authorization" not in seen[0].headers
        assert "github_pat" not in open("updater.py", encoding="utf-8").read().replace(
            "a stored `github_pat` has no business", "")


def test_an_encoded_dotdot_is_refused():
    assert not updater.is_official_asset(OFFICIAL.replace("v9.9.9", "%2e%2e"))
