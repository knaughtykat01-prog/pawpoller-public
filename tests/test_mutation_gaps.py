"""Behaviour no test noticed breaking (MUTGAPS, 27 Oct mutation run).

Each test here exists because deliberately breaking that line left the whole suite green.
"""
import pytest

import pb_import
from posting import e621_tags, journals, tag_budget


# ── journals: ratings must never fall to a looser one ─────────────

@pytest.mark.parametrize("given,want", [
    ("explicit", "adult"), ("NSFW", "adult"), ("adult", "adult"),
    ("questionable", "mature"), ("mature", "mature"), ("teen", "mature"),   # as posting/platforms/base.py
    ("general", "general"), (None, "general"), ("", "general"),
    ("something-new", "adult"),                      # unknown: the strictest, never the loosest
])
def test_journal_rating_words(given, want):
    assert journals._rating({"rating": given}) == want


def test_journal_title_rules():
    assert journals.title_problem("fa", "") and journals.title_problem("fa", "   ")
    assert journals.title_problem("fa", "x" * 60) == ""
    assert "60" in journals.title_problem("fa", "x" * 61)
    assert journals.title_problem("da", "x" * 50) == "" and journals.title_problem("da", "x" * 51)
    assert journals.title_problem("ws", "x" * 100) == "" and journals.title_problem("ws", "x" * 101)


def test_journal_tag_list_tidies_and_dedupes():
    assert journals.tag_list("#Red Fox, red  fox,#art") == ["Red", "Fox", "art"]   # a string splits on , and spaces
    assert journals.tag_list(["Red Fox", "red fox", "#art", ""]) == ["Red_Fox", "art"]


def test_journal_html_blocks():
    out = journals.to_html("# News\n\n- one\n- *two*\n\nline a\nline b [site](https://example.com/a) https://example.com/b")
    assert "<h3>News</h3>" in out
    assert "<ul><li>one</li><li><em>two</em></li></ul>" in out
    assert "<p>line a<br>line b " in out
    assert '<a href="https://example.com/a">site</a>' in out
    assert '<a href="https://example.com/b">https://example.com/b</a>' in out


def test_journal_bbcode_blocks():
    out = journals.to_bbcode("# News\n\n- one\n- two\n\nsee [site](https://example.com/a) and https://example.com/b")
    assert out.startswith("[b]News[/b]\n\n")
    assert "• one\n• two" in out
    assert "[url=https://example.com/a]site[/url]" in out and "[url]https://example.com/b[/url]" in out


# ── PostyBirb descriptions: formatting must come across ───────────

def test_markdown_marks_breaks_and_headings():
    out = pb_import._markdown("<h2>Title</h2><p><b>bold</b> and <i>it</i> and <strong>s</strong> <em>e</em></p>"
                              "<p>two<br>lines</p><hr><div>block</div>")
    assert "## Title" in out
    assert "**bold**" in out and "*it*" in out and "**s**" in out and "*e*" in out
    assert "two\nlines" in out and "---" in out and "block" in out
    assert "\n\n" in out.split("**bold**")[0] or out.startswith("## Title")


def test_markdown_whole_document_shape():
    out = pb_import._markdown('<h2>Title</h2><p>one <a href="https://example.com/x">link</a> &amp; <a>bare</a></p>'
                              "<div>d1</div><div>d2</div><ul><li>a</li><li>b</li></ul><p>x<br>y</p><hr>"
                              "<p>end</p><p></p><p></p><p>gap</p>")
    assert out == ("## Title\n\none [link](https://example.com/x) & bare\n\nd1\n\nd2\n\n- a\n- b\n\n"
                   "x\ny\n\n---\n\nend\n\ngap")


def test_markdown_drops_scripts_and_styles():
    out = pb_import._markdown("<p>keep</p><script>bad(<p>fake</p>)</script><style>.x{}</style>"
                              "<SCRIPT>worse()</SCRIPT><p>after</p>")
    assert "keep" in out and "after" in out
    assert "bad" not in out and "fake" not in out and ".x" not in out and "worse" not in out


# ── per-site tag limits: the exact edges ──────────────────────────

def test_itaku_per_tag_edge():
    kept, dropped = tag_budget.fit_report(["a" * 59, "b" * 60, "c", "d", "e", "f"], "ik")
    assert "a" * 59 in kept and "b" * 60 not in kept
    assert dropped == [{"tag": "b" * 60, "rule": "longer than 59 characters"}]


def test_threads_per_tag_edge():
    assert tag_budget.fit(["a" * 50], "thr") == ["a" * 50]
    assert tag_budget.fit(["a" * 51], "thr") == []


def test_fa_exactly_500_characters_fits():
    tags = ["x" * 99] * 4 + ["x" * 100]          # 4 × 99 + 100 + 4 spaces = 500
    assert tag_budget.fit(tags, "fa") == tags
    assert tag_budget.fit(tags + ["y"], "fa") == tags          # 502: the tail goes


def test_count_limit_names_each_dropped_tag():
    tags = [f"t{i}" for i in range(32)]
    kept, dropped = tag_budget.fit_report(tags, "da")
    assert kept == tags[:30]
    assert dropped == [{"tag": "t30", "rule": "over the 30-tag limit"},
                       {"tag": "t31", "rule": "over the 30-tag limit"}]


def test_describe_lines():
    assert tag_budget.describe("ib") == "no limit"
    assert tag_budget.describe("fa") == "500 characters max"
    assert tag_budget.describe("ik") == "59 chars per tag, at least 5"
    assert tag_budget.describe("thr") == "1 tags max, 50 chars per tag"


# ── e621: de-duplication keeps going, hidden characters, reserved prefixes ──

def test_e621_dedupe_keeps_the_tags_after_a_repeat():
    sent, _ = e621_tags.prepare(["fox", "Fox", "wolf", "artist:fox", "cat"])
    assert sent == ["fox", "wolf", "cat"]


@pytest.mark.parametrize("ch", [" ", " ", "", "͸"])      # Zs, Zs, Co, Cn
def test_e621_hidden_characters(ch):
    assert e621_tags.rule_broken(f"a{ch}b") is not None


def test_e621_reserved_prefix_needs_a_name_after_it():
    assert e621_tags.rule_broken("rating:s")
    assert e621_tags.rule_broken("species:fox")
    assert e621_tags.rule_broken("rating:") is None
    assert e621_tags.rule_broken("notaprefix:x") is None


# ── body size caps (routes/body_caps): the exact edges ────────────

def _req(headers: dict, chunks=(b"",)):
    from starlette.requests import Request
    msgs = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]

    async def receive():
        return msgs.pop(0)
    return Request({"type": "http", "method": "POST", "path": "/",
                    "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}, receive)


def test_declared_length_edges():
    from fastapi import HTTPException
    from routes.body_caps import check_declared
    assert check_declared(_req({"content-length": "10"}), 10) == 10
    assert check_declared(_req({}), 10) is None
    for headers, code in [({"content-length": "11"}, 413), ({"content-length": "-1"}, 400),
                          ({"content-length": "ten"}, 400), ({}, 411),
                          ({"content-length": "5", "transfer-encoding": "chunked"}, 400)]:
        with pytest.raises(HTTPException) as e:
            check_declared(_req(headers), 10, required=True)
        assert e.value.status_code == code, headers


def test_read_capped_counts_what_arrives_not_what_was_said():
    import asyncio
    from fastapi import HTTPException
    from routes.body_caps import read_capped
    assert asyncio.run(read_capped(_req({}, (b"12345", b"67890")), 10)) == b"1234567890"
    with pytest.raises(HTTPException) as e:
        asyncio.run(read_capped(_req({}, (b"12345", b"678901")), 10))
    assert e.value.status_code == 413


# ── PostyBirb upload (pb_upload): the bookkeeping around the unpacking ──

@pytest.fixture
def pbu(tmp_path, monkeypatch):
    import config
    import pb_upload
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    return pb_upload


def _pb_zip(names):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, b"x")
    return buf.getvalue()


def test_upload_refuses_empty_and_unknown_files(pbu):
    uid = pbu.start()
    with pytest.raises(pbu.Refused):
        pbu.add_file(uid, 0)
    fid = pbu.add_file(uid, 3)["file"]
    assert pbu.received(uid, fid) == 0
    for bad in ("3", "x", "", "../1"):
        with pytest.raises(KeyError):
            pbu.received(uid, bad)
    with pytest.raises(ValueError):
        pbu.write_piece(uid, fid, 1, b"x")                       # out of step
    with pytest.raises(OverflowError):
        pbu.write_piece(uid, fid, 0, b"xxxx")                    # more than declared
    assert pbu.write_piece(uid, fid, 0, b"xxx") == 3


def test_a_skipped_name_does_not_stop_the_rest(pbu):
    data = _pb_zip(["PostyBirb/../evil.db", "/abs/data/x.db", "C:/data/y.db",
                    "PostyBirb/data/submissions.db", "PostyBirb/data/accounts.db"])
    uid = pbu.start()
    fid = pbu.add_file(uid, len(data))["file"]
    pbu.write_piece(uid, fid, 0, data)
    has = pbu.take(uid)
    assert has == {"submissions": True, "logins": True}
    got = sorted(p.relative_to(pbu.base(uid)).as_posix() for p in pbu.base(uid).rglob("*") if p.is_file())
    assert got == ["docs/data/accounts.db", "docs/data/submissions.db"]


def test_secure_transport_rules(pbu):
    assert pbu.secure("https", "203.0.113.9")
    assert pbu.secure("http", "127.0.0.1") and pbu.secure("http", "::1") and pbu.secure("http", "localhost")
    assert pbu.secure("http", "100.101.102.103") and pbu.secure("http", "::ffff:127.0.0.1")
    assert not pbu.secure("http", "192.168.1.20") and not pbu.secure("http", "evil.example")

# ── credential vault (config): key sources, recovery flag ──────────

def _no_operator_key(monkeypatch, keyring_get):
    import sys
    import types

    import config
    monkeypatch.setattr(config, "_operator_vault_key", lambda: None)
    store = {}

    def get(service, name):
        return keyring_get(store)

    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(
        get_password=get, set_password=lambda s, n, v: store.update(key=v)))
    return config, store


def test_vault_key_source_names_each_store(monkeypatch):
    config, _ = _no_operator_key(monkeypatch, lambda store: "a-key")
    assert config.vault_key_source() == "keyring"
    config, _ = _no_operator_key(monkeypatch, lambda store: None)
    assert config.vault_key_source() == "keyring"          # empty keyring: the key will be made there

    def broken(store):
        raise RuntimeError("no keyring backend")
    config, _ = _no_operator_key(monkeypatch, broken)
    assert config.vault_key_source() == "dotfile"


def test_a_keyring_key_is_made_once_and_reused(monkeypatch):
    config, store = _no_operator_key(monkeypatch, lambda store: store.get("key"))
    first = config._get_vault_key()
    assert store["key"].encode() == first == config._get_vault_key()


def test_the_dotfile_key_is_made_once_and_reused(monkeypatch, tmp_path):
    def broken(store):
        raise RuntimeError("no keyring backend")
    config, _ = _no_operator_key(monkeypatch, broken)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    first = config._get_vault_key()
    assert (tmp_path / ".vault_key").read_bytes() == first == config._get_vault_key()


def test_a_vault_read_again_after_a_failure_is_never_set_aside(monkeypatch):
    """The unreadable flag must clear on the next good read, or a good vault gets moved aside."""
    import json

    import config
    from cryptography.fernet import Fernet
    config.save_settings({"fa_cookie_a": "secret-a"})
    assert json.loads(config.VAULT_PATH.read_text(encoding="utf-8"))["version"] == 1
    good = config._get_vault_key()
    monkeypatch.setattr(config, "_get_vault_key", lambda: Fernet.generate_key())
    assert config._decrypt_vault() == {} and config._vault_unreadable
    monkeypatch.setattr(config, "_get_vault_key", lambda: good)
    assert config._decrypt_vault()["fa_cookie_a"] == "secret-a" and not config._vault_unreadable
    config.save_settings({"theme": "dark"})
    assert not list(config.VAULT_PATH.parent.glob(config.VAULT_PATH.name + ".unreadable-*"))
    assert config.get_settings()["fa_cookie_a"] == "secret-a"