"""The installed server's self-update (HOSTFREE §3, 4.12.0).

Contracts: the right asset per platform; nothing happens unless the installer's
environment marks the process as managed; the `auto_update` switch is honoured; a
release is unpacked beside the running one, checksum-verified, then `current` is
flipped and the process asks to be restarted; old releases are pruned but never the
current one; the archive guard rejects escapes.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

import server_updater as su

EXE = "PawPoller-Server.exe" if sys.platform == "win32" else "PawPoller-Server"


def _archive(tmp: Path, version: str, tag: str, top_level: bool = True) -> Path:
    """A fake release archive: <top>/PawPoller-Server[.exe] + _internal/x."""
    name = su.asset_name(version, tag)
    path = tmp / name
    prefix = f"PawPoller-Server/" if top_level else ""
    files = {f"{prefix}{EXE}": b"#!/bin/sh\necho " + version.encode(), f"{prefix}_internal/x": b"x"}
    if name.endswith(".zip"):
        with zipfile.ZipFile(path, "w") as z:
            for n, data in files.items():
                z.writestr(n, data)
    else:
        with tarfile.open(path, "w:gz") as t:
            for n, data in files.items():
                info = tarfile.TarInfo(n)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
    return path


@pytest.mark.parametrize("system,machine,tag", [
    ("linux", "x86_64", "linux-x86_64"), ("linux", "aarch64", "linux-arm64"), ("linux", "arm64", "linux-arm64"),
    ("win32", "AMD64", "windows-x64"), ("win32", "ARM64", "windows-arm64"),
    ("darwin", "arm64", "darwin-arm64"), ("darwin", "x86_64", "darwin-x86_64"),
])
def test_platform_tag(system, machine, tag):
    assert su.platform_tag(system, machine) == tag


def test_asset_naming_and_picking():
    assert su.asset_name("4.12.0", "linux-arm64") == "PawPoller-Server-4.12.0-linux-arm64.tar.gz"
    assert su.asset_name("4.12.0", "windows-x64") == "PawPoller-Server-4.12.0-windows-x64.zip"
    assets = [
        {"name": "PawPoller-4.12.0-x86_64.AppImage", "browser_download_url": "u0"},
        {"name": "PawPoller-Server-4.12.0-linux-x86_64.tar.gz", "browser_download_url": "u1"},
        {"name": "PawPoller-Server-4.12.0-linux-x86_64.tar.gz.sha256", "browser_download_url": "u1s"},
        {"name": "PawPoller-Server-4.12.0-linux-arm64.tar.gz", "browser_download_url": "u2"},
        {"name": "PawPoller-windows-x64.zip", "browser_download_url": "u3"},
    ]
    assert su.pick_asset(assets, "linux-x86_64") == ("u1", "u1s")
    assert su.pick_asset(assets, "linux-arm64") == ("u2", None)
    assert su.pick_asset(assets, "windows-x64") == (None, None)


def test_managed_requires_flag_and_existing_root(tmp_path):
    assert su.managed({}) is False
    assert su.managed({su.MANAGED_ENV: "1"}) is False
    assert su.managed({su.MANAGED_ENV: "1", su.ROOT_ENV: str(tmp_path / "nope")}) is False
    assert su.managed({su.MANAGED_ENV: "1", su.ROOT_ENV: str(tmp_path)}) is True


def test_stage_switch_prune(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    tag = su.platform_tag()
    a1 = _archive(tmp_path, "4.12.0", tag)
    d1 = su.stage(a1, "4.12.0", root)
    assert (d1 / EXE).exists() and (d1 / "_internal" / "x").exists()     # top-level folder flattened
    su.switch(root, "4.12.0")
    assert su.current_version(root) == "4.12.0"
    assert (su.current_link(root) / EXE).read_bytes().endswith(b"4.12.0")
    a2 = _archive(tmp_path, "4.12.1", tag, top_level=False)
    su.stage(a2, "4.12.1", root)
    su.switch(root, "4.12.1")
    assert su.current_version(root) == "4.12.1"
    assert (su.current_link(root) / EXE).read_bytes().endswith(b"4.12.1")
    su.stage(_archive(tmp_path, "4.12.2", tag), "4.12.2", root)
    os.utime(su.releases_dir(root) / "4.12.0", (1, 1))                 # oldest
    removed = su.prune(root, keep=2)
    assert removed == ["4.12.0"] and su.current_version(root) == "4.12.1"
    assert (su.releases_dir(root) / "4.12.2").is_dir()
    with pytest.raises(FileNotFoundError):
        su.switch(root, "9.9.9")


def test_stage_rejects_escapes_and_missing_binary(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    bad = tmp_path / "PawPoller-Server-1.0.0-linux-x86_64.tar.gz"
    with tarfile.open(bad, "w:gz") as t:
        info = tarfile.TarInfo("../../escape")
        info.size = 1
        t.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        su.stage(bad, "1.0.0", root)
    empty = tmp_path / "PawPoller-Server-1.0.1-linux-x86_64.tar.gz"
    with tarfile.open(empty, "w:gz") as t:
        info = tarfile.TarInfo("readme.txt")
        info.size = 1
        t.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(FileNotFoundError):
        su.stage(empty, "1.0.1", root)
    assert not (su.releases_dir(root) / "1.0.1").exists()


def test_parse_sha256_file():
    h = "a" * 64
    assert su.parse_sha256_file(f"{h}  PawPoller-Server-4.12.0-linux-x86_64.tar.gz\n") == h
    assert su.parse_sha256_file(h.upper()) == h
    assert su.parse_sha256_file("nothing here") == ""


class FakeHttp:
    """Stands in for httpx.Client: serves the release JSON, the archive and its checksum."""

    def __init__(self, release: dict, files: dict[str, bytes]):
        self.release, self.files, self.calls = release, files, []

    def get(self, url):
        self.calls.append(url)

        class R:
            status_code = 200

            def raise_for_status(self_):
                pass

            def json(self_):
                return self.release
        return R()

    def stream(self, method, url):
        self.calls.append(url)
        data = self.files[url]
        fake = self

        class S:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def raise_for_status(self_):
                pass

            def iter_bytes(self_):
                yield data
        return S()

    def close(self):
        pass


def _release(version: str, tag: str, archive: Path, good_sum: bool = True):
    name = archive.name
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if not good_sum:
        digest = "0" * 64
    return ({"tag_name": f"v{version}", "assets": [
        {"name": name, "browser_download_url": f"https://x/{name}"},
        {"name": name + ".sha256", "browser_download_url": f"https://x/{name}.sha256"},
    ]}, {f"https://x/{name}": archive.read_bytes(), f"https://x/{name}.sha256": f"{digest}  {name}\n".encode()})


def test_run_once_gates(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    env = {su.MANAGED_ENV: "1", su.ROOT_ENV: str(root)}
    assert su.run_once(env={}, settings={}) == "unmanaged"
    assert su.run_once(env=env, settings={"auto_update": False}) == "off"


def test_run_once_stages_verifies_and_switches(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    tag = su.platform_tag()
    env = {su.MANAGED_ENV: "1", su.ROOT_ENV: str(root)}
    # installed 4.12.0, release says 4.12.1
    su.stage(_archive(tmp_path, "4.12.0", tag), "4.12.0", root)
    su.switch(root, "4.12.0")
    archive = _archive(tmp_path / "srv", "4.12.1", tag) if (tmp_path / "srv").mkdir() is None else None
    release, files = _release("4.12.1", tag, archive)
    out = su.run_once(env=env, settings={}, http=FakeHttp(release, files), tag=tag)
    assert out == "staged:4.12.1" and su.current_version(root) == "4.12.1"
    # same release again → nothing to do
    assert su.run_once(env=env, settings={}, http=FakeHttp(release, files), tag=tag) == "none"
    # a bad checksum never switches
    (tmp_path / "bad").mkdir()
    archive2 = _archive(tmp_path / "bad", "4.12.2", tag)
    release2, files2 = _release("4.12.2", tag, archive2, good_sum=False)
    out = su.run_once(env=env, settings={}, http=FakeHttp(release2, files2), tag=tag)
    assert out.startswith("failed:checksum") and su.current_version(root) == "4.12.1"
    assert not (su.releases_dir(root) / "4.12.2").exists()


def test_loop_exits_for_restart_after_staging(monkeypatch):
    calls = []
    monkeypatch.setattr(su, "run_once", lambda: "staged:4.12.1")
    su.loop(exit_fn=lambda code: calls.append(code), sleep=lambda s: None)
    assert calls == [su.RESTART_EXIT_CODE]


def test_request_restart_only_when_managed(monkeypatch):
    monkeypatch.delenv(su.MANAGED_ENV, raising=False)
    assert su.request_restart() is False


@pytest.mark.parametrize("plat", ["win32", "linux"])
def test_desktop_updater_never_picks_a_server_asset(monkeypatch, plat):
    """The release now carries PawPoller-Server-* archives; on Windows both builds end in .zip."""
    import updater
    monkeypatch.setattr(sys, "platform", plat)
    assets = [
        {"name": "PawPoller-Server-4.12.0-windows-x64.zip", "browser_download_url": "server-zip"},
        {"name": "PawPoller-Server-4.12.0-linux-x86_64.tar.gz", "browser_download_url": "server-tgz"},
        {"name": "PawPoller-Setup-4.12.0.exe", "browser_download_url": "setup"},
        {"name": "PawPoller-windows-x64.zip", "browser_download_url": "desktop-zip"},
        {"name": "PawPoller-4.12.0-x86_64.AppImage", "browser_download_url": "desktop-appimage"},
    ]
    picked = updater._pick_update_asset(assets)
    assert picked == ("desktop-zip" if plat == "win32" else "desktop-appimage")
