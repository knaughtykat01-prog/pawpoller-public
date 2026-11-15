"""FAFINALIZE: when FurAffinity turns a post down at its last step, say FurAffinity's reason and don't retry.

A tester's picture went to two sites and failed on FurAffinity twice with only an address for a reason
("Finalize may have failed - final URL: .../submit/finalize/"), and PawPoller queued the same form again.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from clients.fa.client import FAClient, _fa_notice
from posting.manager import _schedule_retry

FORM = ('<form id="myform" method="post" action="{action}"><input type="hidden" name="key" value="{key}"></form>'
        '<a href="/logout/">Log out</a>')


def _fa(finalize):
    """FA's three submit pages; *finalize* answers the last one (request -> Response)."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/submit/":
            return httpx.Response(200, text=FORM.format(action="/submit/upload/", key="k1"))
        if request.method == "POST" and path == "/submit/upload/":
            return httpx.Response(200, text=FORM.format(action="/submit/finalize/", key="k2"))
        if request.method == "POST" and path == "/submit/finalize/":
            return finalize(request)
        if path.startswith("/view/"):
            return httpx.Response(200, text="<html>done</html>")
        return httpx.Response(404)
    return handler


def _submit(tmp_path, finalize):
    pic = tmp_path / "pic.png"
    pic.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    async def run():
        cli = FAClient.__new__(FAClient)
        cli._fa_http = httpx.AsyncClient(transport=httpx.MockTransport(_fa(finalize)), follow_redirects=True,
                                         base_url="https://www.furaffinity.net")

        async def http():
            return cli._fa_http
        cli._get_fa_http = http
        try:
            return await cli.submit_visual(str(pic), title="Sample Piece", description="d", keywords="wolf")
        finally:
            await cli._fa_http.aclose()
    return asyncio.run(run())


REFUSED = ('<html><body><section class="notice-message"><div class="section-body alignleft">'
           '<h2>System Message</h2></div></section><div class="redirect-message">You must select a '
           'category &amp; theme for your submission.</div>' + FORM.format(action="/submit/finalize/", key="k3"))


def test_a_refused_form_says_furaffinitys_reason_and_is_not_retried(tmp_path):
    def refuse(request):
        return httpx.Response(200, text=REFUSED)
    with pytest.raises(RuntimeError) as e:
        _submit(tmp_path, refuse)
    msg = str(e.value)
    assert msg.startswith("FurAffinity turned the post down")
    assert "submit/finalize" not in msg              # a reason, not an address
    assert msg.endswith("You must select a category & theme for your submission.")
    assert _schedule_retry("Sample", 0, "fa", "post", msg, content_type="artwork", account_id=1) is False


def test_a_refusal_with_no_message_says_what_to_check(tmp_path):
    with pytest.raises(RuntimeError) as e:
        _submit(tmp_path, lambda r: httpx.Response(200, text=FORM.format(action="/submit/finalize/", key="k3")))
    assert "didn't say why" in str(e.value) and "category" in str(e.value)
    assert _schedule_retry("Sample", 0, "fa", "post", str(e.value), account_id=1) is False


def test_an_unknown_landing_is_not_retried_because_it_may_have_posted(tmp_path):
    def elsewhere(request):
        return httpx.Response(302, headers={"Location": "https://www.furaffinity.net/msg/others/?x=1"})
    with pytest.raises(RuntimeError) as e:
        _submit(tmp_path, elsewhere)
    assert "Check your FurAffinity gallery" in str(e.value) and "?x=1" not in str(e.value)
    assert _schedule_retry("Sample", 0, "fa", "post", str(e.value), account_id=1) is False


def test_a_post_that_lands_on_its_page_still_succeeds(tmp_path):
    def ok(request):
        return httpx.Response(302, headers={"Location": "https://www.furaffinity.net/view/123456/"})
    assert _submit(tmp_path, ok) == {"submission_id": "123456", "url": "https://www.furaffinity.net/view/123456/"}


def test_the_notice_reader_takes_plain_words_only():
    assert _fa_notice(REFUSED) == "You must select a category & theme for your submission."
    assert _fa_notice('<section class="notice-message"><h2>System Message</h2> Error posting. Too soon.</section>')         == "Error posting. Too soon."
    assert _fa_notice('<div class="redirect-message">A <b>bold</b>\n  reason &lt;here&gt;</div>') == "A bold reason <here>"
    assert len(_fa_notice('<div class="redirect-message">' + "x" * 500 + "</div>")) == 200
    assert _fa_notice("<html>nothing here</html>") == "" and _fa_notice(None) == ""
