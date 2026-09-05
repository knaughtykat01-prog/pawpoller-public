"""The dockerless server package — static contracts (HOSTFREE §3, 4.12.0).

These pin the pieces that only CI and a real machine would otherwise exercise: the
server spec leaves the GUI stack out (that is what makes runtime detection say
"server"), the release workflow builds and checksums one archive per platform, the
installers download exactly those names and mark the process as managed, and the
public copy ships the installers.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_server_spec_excludes_the_desktop_stack():
    spec = _read("pawpoller-server.spec")
    assert "['server.py']" in spec
    assert "name='PawPoller-Server'" in spec
    assert "console=True" in spec
    for mod in ("webview", "pystray", "tkinter"):
        assert f"'{mod}'" in spec.split("excludes=")[1].split("]")[0], mod
    assert "tkinter" not in spec.split("hiddenimports=")[1].split("]")[0]


def test_release_workflow_builds_and_checksums_every_platform():
    wf = yaml.safe_load(_read(".github/workflows/build.yml"))
    jobs = wf["jobs"]
    assert {"build-server-linux", "build-server-windows"} <= set(jobs)
    assert jobs["build-server-linux"]["needs"] == "test" and jobs["build-server-windows"]["needs"] == "test"
    tags = {i["tag"] for i in jobs["build-server-linux"]["strategy"]["matrix"]["include"]}
    assert tags == {"linux-x86_64", "linux-arm64"}
    runners = {i["runner"] for i in jobs["build-server-linux"]["strategy"]["matrix"]["include"]}
    assert "ubuntu-24.04-arm" in runners
    text = _read(".github/workflows/build.yml")
    assert "pawpoller-server.spec" in text
    assert text.count(".sha256") >= 4                # one per archive, written and attached
    # the desktop jobs are untouched
    assert {"build-windows", "build-linux", "build-image", "test"} <= set(jobs)


def test_installers_speak_the_same_asset_names():
    sh = _read("installer/server/install.sh")
    ps = _read("installer/server/install.ps1")
    assert 'ASSET="PawPoller-Server-${VERSION}-${TAG}.tar.gz"' in sh
    assert '"$Asset = "PawPoller-Server-$Version-$Tag.zip"' in ps or '$Asset = "PawPoller-Server-$Version-$Tag.zip"' in ps
    for text in (sh, ps):
        assert "PAWPOLLER_SERVER_MANAGED=1" in text
        assert "PAWPOLLER_SERVER_ROOT" in text
        assert "PAWPOLLER_APPDATA_DIR" in text
        assert "PAWPOLLER_AUTO_BACKUP=1" in text and "PAWPOLLER_AUTO_BACKUP_DIR" in text    # 4.14.0
        assert ".sha256" in text
        assert "/api/health" in text
    assert "Restart=always" in sh and "launchctl bootstrap" in sh
    assert "RestartOnFailure" in ps and "mklink /J" in ps
    # binds to the loopback by default — reach it through Tailscale or a proxy
    assert 'BIND="${PAWPOLLER_BIND:-127.0.0.1}"' in sh
    assert "'127.0.0.1'" in ps


def test_restart_exit_code_matches_the_units():
    import server_updater
    assert server_updater.RESTART_EXIT_CODE == 75
    assert "exit 75" in _read("installer/server/install.ps1") or "exit 75" in _read("installer/server/install.sh").lower() or "code 75" in _read("installer/server/install.sh")


def test_server_wires_the_updater_only_when_managed():
    src = _read("server.py")
    assert "server_updater.managed()" in src and "server_updater.loop" in src
    route = _read("routes/mirror_api.py")
    assert "server_updater.request_restart()" in route
