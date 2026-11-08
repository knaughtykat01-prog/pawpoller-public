"""SEC067 (1): the Mastodon connect form only sends the token to a public https server.

Same rule as the PostyBirb import: https, a DNS name that resolves only to public addresses,
rebuilt as ``https://host``. DNS is stubbed; stand-in hosts only.
"""
import asyncio
import socket

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import config
from database import accounts as adb
from database.db import get_connection
from routes import mast_api, posts_api

_ADDRS = {"pub.example.social": "93.184.216.34", "lan.example.social": "10.0.0.5",
          "meta.example.social": "169.254.169.254"}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    def lookup(host, *a, **k):
        if host not in _ADDRS:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_ADDRS[host], 443))]
    monkeypatch.setattr(posts_api.socket, "getaddrinfo", lookup)


@pytest.mark.parametrize("typed", ["pub.example.social", "https://pub.example.social",
                                   "https://PUB.example.social/", " pub.example.social "])
def test_public_server_accepted_and_rebuilt(typed):
    assert asyncio.run(mast_api._public_instance(typed.strip())) == "https://pub.example.social"


@pytest.mark.parametrize("typed", [
    "http://pub.example.social",            # not https
    "https://pub.example.social:8443",      # a port
    "https://me@pub.example.social",        # a user part
    "https://pub.example.social/api",       # a path
    "lan.example.social",                   # resolves to the LAN
    "meta.example.social",                  # resolves to the cloud metadata address
    "localhost", "127.0.0.1", "[::1]", "nowhere.example.social", "",
])
def test_anything_else_refused(typed):
    with pytest.raises(HTTPException) as e:
        asyncio.run(mast_api._public_instance(typed))
    assert e.value.status_code == 400


def test_connect_refuses_before_sending_the_token(monkeypatch):
    from polling import mast_poller
    monkeypatch.setattr(mast_poller, "_get_or_create_client",
                        lambda *a: pytest.fail("a client was made for a refused address"))
    with pytest.raises(HTTPException) as e:
        asyncio.run(mast_api.mast_connect({"instance_url": "lan.example.social", "access_token": "tok"}))
    assert e.value.status_code == 400


# The Accounts form saves the same address by another route.

@pytest.fixture()
def client():
    from dashboard import app
    return TestClient(app)


def _mast_accounts():
    conn = get_connection()
    try:
        return adb.list_accounts(conn, platform="mast")
    finally:
        conn.close()


def _key(account, field):
    return config.account_setting_key(account["account_id"], field, bool(account["is_default"]))


@pytest.fixture()
def mast_account():
    conn = get_connection()
    try:
        return adb.get_account(conn, adb.create_account(conn, "mast", "Sample", handle="Sample"))
    finally:
        conn.close()


def test_accounts_edit_refuses_a_private_server_and_saves_nothing(client, mast_account):
    r = client.patch(f"/api/accounts/{mast_account['account_id']}", json={
        "label": "Renamed", "credentials": {"mast_instance_url": "http://lan.example.social",
                                            "mast_access_token": "refused-tok"}})
    assert r.status_code == 400
    s = config.get_settings()
    assert s.get(_key(mast_account, "mast_instance_url")) != "http://lan.example.social"
    assert s.get(_key(mast_account, "mast_access_token")) != "refused-tok"
    assert [a["label"] for a in _mast_accounts()
            if a["account_id"] == mast_account["account_id"]] == ["Sample"]


def test_accounts_edit_saves_the_rebuilt_address(client, mast_account):
    r = client.patch(f"/api/accounts/{mast_account['account_id']}",
                     json={"credentials": {"mast_instance_url": " PUB.example.social/ "}})
    assert r.status_code == 200
    assert config.get_settings()[_key(mast_account, "mast_instance_url")] == "https://pub.example.social"


def test_accounts_edit_without_an_address_is_not_checked(client, mast_account):
    r = client.patch(f"/api/accounts/{mast_account['account_id']}",
                     json={"credentials": {"mast_access_token": "new-tok"}})
    assert r.status_code == 200
    assert config.get_settings()[_key(mast_account, "mast_access_token")] == "new-tok"


def test_accounts_create_refuses_a_private_server_before_making_the_account(client):
    before = len(_mast_accounts())
    r = client.post("/api/accounts", json={"platform": "mast", "label": "Sample",
                                           "credentials": {"mast_instance_url": "meta.example.social"}})
    assert r.status_code == 400
    assert len(_mast_accounts()) == before
