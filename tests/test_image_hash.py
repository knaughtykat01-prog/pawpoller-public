"""Phase 4 — native perceptual-hash (dHash) image-similarity suggestions (no AI).

All offline: images are generated in-memory with Pillow and the thumbnail
fetcher is injected, so nothing touches the network.
"""
import io

from PIL import Image

from database.db import get_connection
from database import image_hash as ih
from database import collections_queries as cq


def _gradient(size=(64, 64), shift=0):
    img = Image.new("RGB", size)
    px = img.load()
    w, h = size
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 4 + shift) % 256, (y * 4) % 256, ((x + y) * 2) % 256)
    return img


def _png(img):
    b = io.BytesIO()
    img.save(b, format="PNG")
    return b.getvalue()


# ── Pure primitives ──────────────────────────────────────────────────────

def test_dhash_is_resize_invariant_and_discriminating():
    big = _gradient((64, 64))
    small = big.resize((16, 16))
    h_big = ih.dhash_from_bytes(_png(big))
    h_small = ih.dhash_from_bytes(_png(small))
    assert h_big and h_small
    # Same image at different resolutions → near-identical hash.
    assert ih.hamming(h_big, h_small) <= ih.HAMMING_THRESHOLD
    # A visibly different image → far apart.
    other = _gradient((64, 64), shift=128).transpose(Image.FLIP_LEFT_RIGHT)
    h_other = ih.dhash_from_bytes(_png(other))
    assert ih.hamming(h_big, h_other) > ih.HAMMING_THRESHOLD


def test_dhash_bad_input_returns_none():
    assert ih.dhash_from_bytes(b"") is None
    assert ih.dhash_from_bytes(b"not an image") is None


def test_hamming_and_similarity():
    assert ih.hamming("0000000000000000", "0000000000000000") == 0
    assert ih.hamming("0000000000000000", "ffffffffffffffff") == 64
    assert ih.similarity("0000000000000000", "0000000000000000") == 1.0
    # Unparseable → max distance, never a false match.
    assert ih.hamming("zzz", "0000000000000000") == 64


def test_is_allowed_thumb_url():
    assert ih.is_allowed_thumb_url("https://d.facdn.net/art/x/thumb.jpg")
    assert ih.is_allowed_thumb_url("https://cdn.bsky.app/img/feed_thumbnail/x.jpg")
    assert not ih.is_allowed_thumb_url("http://d.facdn.net/x.jpg")          # not https
    assert not ih.is_allowed_thumb_url("https://evil.example.com/x.jpg")     # not allowlisted
    assert not ih.is_allowed_thumb_url("https://127.0.0.1/x.jpg")            # internal host
    assert not ih.is_allowed_thumb_url("https://facdn.net.evil.com/x.jpg")   # suffix spoof
    assert not ih.is_allowed_thumb_url("")


# ── Storage + scan ───────────────────────────────────────────────────────

def test_missing_thumb_targets_filters_allowlist_and_existing():
    conn = get_connection()
    try:
        conn.execute("INSERT INTO fa_submissions (submission_id, title, thumbnail_url) "
                     "VALUES (100, 'A', 'https://d.facdn.net/a.jpg')")          # allowlisted
        conn.execute("INSERT INTO fa_submissions (submission_id, title, thumbnail_url) "
                     "VALUES (101, 'B', 'https://evil.example.com/b.jpg')")     # not allowlisted
        conn.execute("INSERT INTO fa_submissions (submission_id, title, thumbnail_url) "
                     "VALUES (102, 'C', 'https://d.facdn.net/c.jpg')")          # allowlisted, already hashed
        ih.store(conn, "fa", "102", "0000000000000000", source="thumb")
        conn.commit()

        targets = ih.missing_thumb_targets(conn, limit=50)
        ids = {(t["platform"], t["submission_id"]) for t in targets}
        assert ("fa", "100") in ids
        assert ("fa", "101") not in ids   # host not allowlisted
        assert ("fa", "102") not in ids   # already hashed
    finally:
        conn.close()


def test_hash_scan_stores_hashes_with_injected_fetch():
    conn = get_connection()
    try:
        conn.execute("INSERT INTO fa_submissions (submission_id, title, thumbnail_url) "
                     "VALUES (100, 'A', 'https://d.facdn.net/a.jpg')")
        conn.commit()
        png = _png(_gradient())

        def fetch(url):
            return png if ih.is_allowed_thumb_url(url) else None

        result = ih.hash_scan(conn, fetch, limit=10)
        assert result == {"scanned": 1, "hashed": 1}
        assert ih.has(conn, "fa", "100")
    finally:
        conn.close()


def test_hash_scan_oversize_is_dropped():
    conn = get_connection()
    try:
        conn.execute("INSERT INTO fa_submissions (submission_id, title, thumbnail_url) "
                     "VALUES (100, 'A', 'https://d.facdn.net/a.jpg')")
        conn.commit()
        # Fetcher returns None (as the real oversize/failed fetch would).
        result = ih.hash_scan(conn, lambda url: None, limit=10)
        assert result == {"scanned": 1, "hashed": 0}
        assert not ih.has(conn, "fa", "100")
    finally:
        conn.close()


# ── Image suggestions + merge with titles ────────────────────────────────

def test_image_suggestions_cross_platform_only_and_excludes_collected():
    conn = get_connection()
    try:
        # Two platforms carry the same picture (near-identical hash), one differs.
        conn.execute("INSERT INTO fa_submissions (submission_id, title) VALUES (100, 'Wolf')")
        conn.execute("INSERT INTO bsky_submissions (submission_id, title) VALUES ('b1', 'wolf art')")
        img = _gradient()
        h = ih.dhash_from_bytes(_png(img))
        h_near = ih.dhash_from_bytes(_png(img.resize((20, 20))))
        ih.store(conn, "fa", "100", h, source="artwork")
        ih.store(conn, "bsky", "b1", h_near, source="thumb")
        # Same-platform second FA row must never be paired with the first.
        ih.store(conn, "fa", "999", h, source="thumb")
        conn.commit()

        sugg = ih.image_suggestions(conn, set())
        pairs = {frozenset((m["platform"], str(m["submission_id"])) for m in s["submissions"]) for s in sugg}
        assert frozenset({("fa", "100"), ("bsky", "b1")}) in pairs
        assert all(s["reason"] == "image" for s in sugg)
        # No same-platform (fa,100)+(fa,999) pairing.
        assert frozenset({("fa", "100"), ("fa", "999")}) not in pairs

        # Once fa:100 is collected, the pair is excluded.
        sugg2 = ih.image_suggestions(conn, {("fa", "100")})
        pairs2 = {frozenset((m["platform"], str(m["submission_id"])) for m in s["submissions"]) for s in sugg2}
        assert frozenset({("fa", "100"), ("bsky", "b1")}) not in pairs2
    finally:
        conn.close()


def test_auto_suggest_collections_merges_title_and_image():
    conn = get_connection()
    try:
        # Title-match pair: identical FA + WS titles.
        conn.execute("INSERT INTO fa_submissions (submission_id, title) VALUES (100, 'Wolf Tale')")
        conn.execute("INSERT INTO ws_submissions (submission_id, title, posted_at) "
                     "VALUES (110, 'Wolf Tale', '2026-01-01')")
        # Give that same FA+WS pair an image match too → should merge to 'both'.
        img = _gradient()
        h = ih.dhash_from_bytes(_png(img))
        ih.store(conn, "fa", "100", h, source="artwork")
        ih.store(conn, "ws", "110", ih.dhash_from_bytes(_png(img.resize((24, 24)))), source="thumb")
        conn.commit()

        sugg = cq.auto_suggest_collections(conn)
        by_pair = {frozenset((m["platform"], str(m["submission_id"])) for m in s["submissions"]): s
                   for s in sugg}
        fa_ws = by_pair.get(frozenset({("fa", "100"), ("ws", "110")}))
        assert fa_ws is not None
        assert fa_ws["reason"] == "both"   # found by title AND pixels, deduped to one row
    finally:
        conn.close()
