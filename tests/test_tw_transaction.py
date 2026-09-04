"""X's x-client-transaction-id on the write path (4.6.2, BACKLOG TWTXID).

X refused CreateTweet with error 226 twice, two minutes apart, WITH the 4.3.5
write headers — so those are not the key. What X's own client sends and we
did not is the per-request transaction id. `clients/tw/transaction.py` is a
port of the reference implementation (XClientTransaction, MIT) without its
BeautifulSoup dependency; it was checked byte-for-byte against the reference
on the live x.com page before this file was written, and the synthetic
fixture's golden values below came out of that same comparison.

Three contracts: the arithmetic (golden + structure), the parsing (both meta
attribute orders, the migration hop, the ondemand.s URL, honest errors), and
the client — writes carry the header, reads never do, a failed page fetch
means posting WITHOUT it rather than not posting, and a 226 drops the cached
state so the retry is a fresh test.
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest

from clients.tw import transaction as txn

# ── synthetic page: 48-byte key, 4 SVGs with a 16-row animation table ────────
KEY = "bWVudFUrMXlQejIvSWNOUytRUmFGUlJSK2JhYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5eg=="
_ROWS = [" ".join(str((r * 17 + i * 13) % 250 + 3) for i in range(11)) for r in range(16)]
_D = "M0 0 0 0 " + "".join("C" + row for row in _ROWS)
_SVGS = "".join(
    f'<svg id="loading-x-anim-{i}" viewBox="0 0 24 24"><g><path d="M1 1"/><path d="{_D}"/></g></svg>'
    for i in range(4))
HOME = (f'<html><head><meta name="twitter-site-verification" content="{KEY}"/></head>'
        f'<body>{_SVGS}<script>,42:"ondemand.s",42:"0123abcd"</script></body></html>')
ONDEMAND = "(x[5], 16),(x[2], 16),(x[12], 16),(x[14], 16),(x[7], 16)"
# From the reference implementation on exactly these inputs.
GOLDEN_ANIMATION_KEY = "ffffff070a3d70a3d70a40e66666666666680e6666666666668070a3d70a3d70a400"
GOLDEN_TID = ("xaigq7GQ7vS8lb/36oymi5bulJekg5eXl+6npKemoaCjoq2sr66pqKuqtbS3trGws7K9vL8t"
              "xsXFJuc6OwLBA1+jpvd7q1VGPsY")
GOLDEN_RANDOM_BYTE = 197


class TestArithmetic:
    def test_matches_the_reference_implementation(self):
        ct = txn.ClientTransaction(HOME, ONDEMAND)
        assert (ct.row_index_at, ct.time_indices) == (5, [2, 12, 14, 7])
        assert ct.animation_key == GOLDEN_ANIMATION_KEY
        assert ct.generate("POST", "/i/api/graphql/abc/CreateTweet", time_now=1000,
                           random_byte=GOLDEN_RANDOM_BYTE) == GOLDEN_TID

    def test_the_id_unpacks_to_key_time_hash_and_marker(self):
        """The header is one random byte followed by everything XORed with it."""
        ct = txn.ClientTransaction(HOME, ONDEMAND)
        tid = ct.generate("POST", "/1.1/media/upload.json", time_now=0x01020304, random_byte=9)
        raw = base64.b64decode(tid + "=" * (-len(tid) % 4))
        rnd, body = raw[0], [b ^ raw[0] for b in raw[1:]]
        n = len(ct.key_bytes)
        assert rnd == 9
        assert body[:n] == ct.key_bytes
        assert body[n:n + 4] == [0x04, 0x03, 0x02, 0x01], "time is little-endian seconds since 2023-05-01"
        assert len(body) == n + 4 + 16 + 1 and body[-1] == txn.ADDITIONAL_RANDOM_NUMBER

    def test_method_path_and_time_all_change_the_hash(self):
        ct = txn.ClientTransaction(HOME, ONDEMAND)
        a = ct.generate("POST", "/p", time_now=5, random_byte=0)
        assert ct.generate("GET", "/p", time_now=5, random_byte=0) != a
        assert ct.generate("POST", "/q", time_now=5, random_byte=0) != a
        assert ct.generate("POST", "/p", time_now=6, random_byte=0) != a
        assert ct.generate("POST", "/p", time_now=5, random_byte=0) == a, "deterministic given the random byte"


class TestParsing:
    def test_meta_key_in_either_attribute_order(self):
        assert txn.verification_key(f'<meta name="twitter-site-verification" content="{KEY}">') == KEY
        assert txn.verification_key(f'<meta content="{KEY}" name="twitter-site-verification">') == KEY

    def test_ondemand_url_and_migration_hop(self):
        assert txn.ondemand_file_url(HOME) == \
            "https://abs.twimg.com/responsive-web/client-web/ondemand.s.0123abcda.js"
        assert txn.migration_url('<meta http-equiv="refresh" content="0; url = https://x.com/x/migrate?tok=abc123_-">') \
            == "https://x.com/x/migrate?tok=abc123_-"
        assert txn.migration_url(HOME) == ""

    @pytest.mark.parametrize("html,js,msg", [
        (HOME.replace("twitter-site-verification", "nothing"), ONDEMAND, "verification key"),
        (HOME.replace("loading-x-anim", "gone"), ONDEMAND, "SVGs"),
        (HOME, "no indices here", "indices"),
        (HOME.replace('42:"ondemand.s"', ""), ONDEMAND, "ondemand"),
    ])
    def test_a_changed_page_is_a_named_error_not_a_wrong_id(self, html, js, msg):
        with pytest.raises(txn.TransactionError) as e:
            if "ondemand" in msg:
                txn.ondemand_file_url(html)
            else:
                txn.ClientTransaction(html, js)
        assert msg.lower() in str(e.value).lower()


# ── the client ───────────────────────────────────────────────────────────────

def _client(seen: list, *, home_ok=True, tweet_payload=None):
    """A TWClient whose two HTTP clients are MockTransports: records every
    request (method, url, headers) into `seen`."""
    from clients.tw.client import TWClient

    async def api(req: httpx.Request):
        seen.append((req.method, str(req.url), dict(req.headers)))
        if req.url.path.endswith("/CreateTweet"):
            return httpx.Response(200, json=tweet_payload or {
                "data": {"create_tweet": {"tweet_results": {"result": {"rest_id": "777"}}}}})
        if req.url.path.endswith("/media/upload.json"):
            return httpx.Response(200, json={"media_id_string": "555"})
        return httpx.Response(200, json={"data": {}})

    async def browser(req: httpx.Request):
        seen.append((req.method, str(req.url), dict(req.headers)))
        if not home_ok:
            return httpx.Response(503, text="nope")
        if req.url.path == "/home":
            return httpx.Response(200, text=HOME)
        return httpx.Response(200, text=ONDEMAND)

    c = TWClient("tok", "csrf", "handle")
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(api), headers=c._http.headers)
    c._txn_http = httpx.AsyncClient(transport=httpx.MockTransport(browser))
    return c


class TestClient:
    @pytest.mark.asyncio
    async def test_writes_carry_the_header_and_the_page_is_fetched_once(self):
        seen: list = []
        c = _client(seen)
        assert (await c.create_tweet("hello"))["id"] == "777"
        assert await c.upload_media(__file__) == "555"
        writes = [s for s in seen if s[0] == "POST"]
        assert len(writes) == 2 and all("x-client-transaction-id" in s[2] for s in writes)
        assert all(s[2]["x-twitter-auth-type"] == "OAuth2Session" for s in writes), "4.3.5 headers stay"
        pages = [s for s in seen if "x.com/home" in s[1]]
        assert len(pages) == 1, "built once, then cached"
        # The browser fetch carries the cookies but NOT the API bearer.
        assert "authorization" not in pages[0][2]

    @pytest.mark.asyncio
    async def test_reads_never_send_it(self):
        seen: list = []
        c = _client(seen)
        await c._http.get("https://x.com/i/api/graphql/x/UserTweets")
        assert all("x-client-transaction-id" not in s[2] for s in seen)

    @pytest.mark.asyncio
    async def test_a_broken_page_means_posting_without_it_not_not_posting(self):
        seen: list = []
        c = _client(seen, home_ok=False)
        assert (await c.create_tweet("hello"))["id"] == "777"
        w = [s for s in seen if s[0] == "POST"][0]
        assert "x-client-transaction-id" not in w[2] and w[2]["x-twitter-auth-type"] == "OAuth2Session"
        # …and it does not hammer x.com/home on every write.
        await c.create_tweet("again")
        assert len([s for s in seen if "x.com/home" in s[1]]) == 1

    @pytest.mark.asyncio
    async def test_a_226_drops_the_cached_state_so_the_retry_is_fresh(self):
        seen: list = []
        refused = {"data": {}, "errors": [{"code": 226, "message": "Authorization: This request looks like it might be automated."}]}
        c = _client(seen, tweet_payload=refused)
        assert await c.create_tweet("hello") is None
        assert c._txn is None, "forgotten after a 226 with the header"
        assert "226" in c.last_error
        await c.create_tweet("retry")
        assert len([s for s in seen if "x.com/home" in s[1]]) == 2, "the retry rebuilt from a fresh page"


class TestSources:
    def test_the_header_is_generated_from_the_request_path_only(self):
        src = open("clients/tw/client.py", encoding="utf-8").read()
        assert "urlparse(url).path" in src
        assert src.count("await self._write_headers(") == 3, "CreateTweet, media upload, alt text"
        assert "headers=_WRITE_HEADERS" not in src, "no write bypasses the transaction id"

    def test_no_new_dependency(self):
        for f in ("requirements.txt", "requirements-server.txt"):
            src = open(f, encoding="utf-8").read().lower()
            assert "beautifulsoup" not in src and "xclienttransaction" not in src and "curl_cffi" not in src
