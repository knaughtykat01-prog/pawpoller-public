"""Native perceptual image hashing — pixel-based "same artwork?" detection with
NO AI, no ML model, no embeddings, no external service. Pure Pillow (already a
dependency), runs locally.

Used to suggest folding lookalike submissions into a Collection (Phase 4 of the
linking/picker overhaul). The algorithm is **dHash** (difference hash): shrink to
a tiny greyscale grid and record, per pixel, whether it is brighter than its right
neighbour. Same image at different resolutions → near-identical hash (dHash is
resize-invariant because both are shrunk to the same grid first), so a full-res
upload and a platform thumbnail of the same art land a small **Hamming distance**
apart. Different images → large distance.

Two populators feed the `image_hashes` table, both keyed by `(platform,
submission_id)` so they line up with the per-platform submission tables:
  1. Local artwork archive — zero network, always safe (`hash_local_artworks`).
  2. A bounded, allowlist-guarded thumbnail scan (`hash_scan`) — only fetches
     from a hardcoded set of public, hotlink-friendly platform CDNs (same
     security posture as the /thumb proxy: https + host-suffix allowlist, so it
     can never be pointed at an internal host).
"""
from __future__ import annotations

import io
import logging
import sqlite3
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Near-duplicate threshold: dHash is 64 bits, so ≤ 8 bits differing (~12%) is a
# strong "same image" signal while tolerating recompression / thumbnailing.
HAMMING_THRESHOLD = 8

# Public, hotlink-friendly platform CDNs the thumbnail scan may fetch from. Kept
# deliberately narrow — pixiv (referer-gated), e621 (UA policy) and per-instance
# Mastodon/Fediverse hosts are intentionally excluded. Suffix match with a dot
# boundary, https only → this can never resolve to an internal/loopback host.
CDN_ALLOWLIST = (
    "metapix.net",        # Inkbunny
    "facdn.net",          # FurAffinity
    "furaffinity.net",    # FurAffinity
    "weasyl.com",         # Weasyl
    "cdn.bsky.app",       # Bluesky
    "media.tumblr.com",   # Tumblr
    "twimg.com",          # X / Twitter
    "wixmp.com",          # DeviantArt
)

# platform code -> its submissions table (mirrors collections_queries._TABLE_MAP)
_TABLE_MAP = {
    "ib": "submissions", "fa": "fa_submissions", "ws": "ws_submissions",
    "sf": "sf_submissions", "sqw": "sqw_submissions", "ao3": "ao3_submissions",
    "da": "da_submissions", "wp": "wp_submissions", "ik": "ik_submissions",
    "bsky": "bsky_submissions", "tw": "tw_submissions", "mast": "mast_submissions",
    "tum": "tum_submissions", "pix": "pix_submissions", "thr": "thr_submissions",
    "ig": "ig_submissions", "e621": "e621_submissions",
}

_MAX_IMAGE_BYTES = 8 * 1024 * 1024   # 8 MB cap per fetched thumbnail


# ── Pure perceptual-hash primitives ──────────────────────────────────────

def dhash_from_image(img, hash_size: int = 8) -> str:
    """dHash of a PIL image → 16-char hex string (64 bits)."""
    from PIL import Image
    small = img.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    px = list(small.getdata())
    w = hash_size + 1
    bits = 0
    for row in range(hash_size):
        base = row * w
        for col in range(hash_size):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return f"{bits:0{hash_size * hash_size // 4}x}"


def dhash_from_bytes(data: bytes) -> str | None:
    """dHash of raw image bytes, or None if it can't be decoded."""
    if not data:
        return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return dhash_from_image(im)
    except Exception:
        return None


def dhash_from_path(path) -> str | None:
    """dHash of an image file on disk, or None on any failure."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return dhash_from_image(im)
    except Exception:
        return None


def hamming(a: str, b: str) -> int:
    """Bit distance between two hex hashes. Max distance (64) on parse failure so
    a bad hash never masquerades as a match."""
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return 64


def similarity(a: str, b: str) -> float:
    """0.0–1.0 similarity from Hamming distance over a 64-bit hash."""
    return 1.0 - hamming(a, b) / 64.0


def is_allowed_thumb_url(url: str) -> bool:
    """True only for https URLs on an allowlisted public CDN host — the SSRF
    guard for the thumbnail scan."""
    try:
        p = urlparse(url or "")
    except Exception:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname.lower()
    return any(host == s or host.endswith("." + s) for s in CDN_ALLOWLIST)


# ── Storage ──────────────────────────────────────────────────────────────

def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS image_hashes (
            platform       TEXT NOT NULL,
            submission_id  TEXT NOT NULL,
            phash          TEXT NOT NULL,
            source         TEXT DEFAULT '',
            computed_at    TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (platform, submission_id)
        )""")


def store(conn: sqlite3.Connection, platform: str, sid, phash: str, source: str = "") -> None:
    conn.execute(
        "INSERT INTO image_hashes (platform, submission_id, phash, source) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(platform, submission_id) DO UPDATE SET phash = excluded.phash, "
        "source = excluded.source, computed_at = datetime('now')",
        (platform, str(sid), phash, source or ""))


def has(conn: sqlite3.Connection, platform: str, sid) -> bool:
    return conn.execute(
        "SELECT 1 FROM image_hashes WHERE platform = ? AND submission_id = ?",
        (platform, str(sid))).fetchone() is not None


def all_hashes(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT platform, submission_id, phash FROM image_hashes").fetchall()]


def missing_thumb_targets(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Submissions that have an allowlisted thumbnail URL but no stored hash yet —
    the work-list for the thumbnail scan. Capped at `limit`."""
    have = {(r["platform"], r["submission_id"])
            for r in conn.execute("SELECT platform, submission_id FROM image_hashes")}
    out: list[dict] = []
    for platform, table in _TABLE_MAP.items():
        if len(out) >= limit:
            break
        try:
            rows = conn.execute(
                f"SELECT submission_id, thumbnail_url FROM {table} "
                f"WHERE thumbnail_url IS NOT NULL AND thumbnail_url != ''").fetchall()
        except Exception:
            continue  # table has no thumbnail_url column
        for r in rows:
            sid = str(r["submission_id"])
            if (platform, sid) in have:
                continue
            if not is_allowed_thumb_url(r["thumbnail_url"]):
                continue
            out.append({"platform": platform, "submission_id": sid,
                        "thumbnail_url": r["thumbnail_url"]})
            if len(out) >= limit:
                break
    return out


# ── Populators ───────────────────────────────────────────────────────────

def hash_scan(conn: sqlite3.Connection, fetch, limit: int = 200) -> dict:
    """Fetch + hash up to `limit` un-hashed allowlisted thumbnails.

    `fetch(url) -> bytes | None` is injected so this is testable offline and so
    the SSRF-guarded fetcher lives at the call site. Returns {scanned, hashed}.
    """
    targets = missing_thumb_targets(conn, limit=limit)
    hashed = 0
    for t in targets:
        try:
            data = fetch(t["thumbnail_url"])
        except Exception:
            data = None
        if not data:
            continue
        ph = dhash_from_bytes(data)
        if not ph:
            continue
        store(conn, t["platform"], t["submission_id"], ph, source="thumb")
        hashed += 1
    conn.commit()
    return {"scanned": len(targets), "hashed": hashed}


def hash_local_artworks(conn: sqlite3.Connection) -> dict:
    """Hash every local artwork image (zero network) and store the hash against
    each platform copy the artwork has been posted to, so a discovered lookalike
    on another platform can later match the user's known art. {scanned, hashed}."""
    from pathlib import Path
    from posting import artwork_reader
    from database import posting_queries
    scanned = hashed = 0
    try:
        artworks = artwork_reader.list_artworks()
    except Exception:
        return {"scanned": 0, "hashed": 0}
    for art in artworks:
        name = art.get("name")
        folder = art.get("path")
        image = art.get("image")  # filename within the artwork folder
        if not name or not folder or not image:
            continue
        img_path = Path(folder) / image
        scanned += 1
        ph = dhash_from_path(img_path)
        if not ph:
            continue
        try:
            pubs = posting_queries.get_publications(conn, story_name=name, content_type="artwork")
        except Exception:
            pubs = []
        for p in pubs:
            ext = p.get("external_id")
            if p.get("platform") and ext:
                store(conn, p["platform"], str(ext), ph, source="artwork")
                hashed += 1
    conn.commit()
    return {"scanned": scanned, "hashed": hashed}


def image_suggestions(conn: sqlite3.Connection, existing: set) -> list[dict]:
    """Cross-platform near-duplicate pairs by Hamming distance, excluding pairs
    already grouped (`existing`, a set of `(platform, str(submission_id))`).

    Returns the same shape as the title suggester:
    `{similarity, reason: 'image', submissions: [{platform, submission_id, title}, ...]}`.
    """
    rows = all_hashes(conn)
    # Resolve a display title lazily per (platform, sid).
    def _title(platform, sid):
        table = _TABLE_MAP.get(platform)
        if not table:
            return ""
        try:
            r = conn.execute(
                f"SELECT title FROM {table} WHERE submission_id = ?", (sid,)).fetchone()
            return (r["title"] if r and "title" in r.keys() else "") or ""
        except Exception:
            return ""

    suggestions = []
    n = len(rows)
    for i in range(n):
        a = rows[i]
        ka = (a["platform"], str(a["submission_id"]))
        if ka in existing:
            continue
        for j in range(i + 1, n):
            b = rows[j]
            if a["platform"] == b["platform"]:
                continue  # same-site cross-post isn't meaningful
            kb = (b["platform"], str(b["submission_id"]))
            if kb in existing:
                continue
            d = hamming(a["phash"], b["phash"])
            if d <= HAMMING_THRESHOLD:
                suggestions.append({
                    "similarity": round(1.0 - d / 64.0, 3),
                    "reason": "image",
                    "submissions": [
                        {"platform": a["platform"], "submission_id": a["submission_id"],
                         "title": _title(a["platform"], a["submission_id"])},
                        {"platform": b["platform"], "submission_id": b["submission_id"],
                         "title": _title(b["platform"], b["submission_id"])},
                    ],
                })
    suggestions.sort(key=lambda x: x["similarity"], reverse=True)
    return suggestions[:20]


# ── Masterpiece de-duplication (2.144.0) ─────────────────────────────────────
# The same image can end up as two separate Masterpieces (e.g. imported from two
# platforms as two folders). We hash each Masterpiece's canonical hero image
# (local, zero network) under a synthetic "__mp__" platform keyed by Masterpiece
# name, then cluster by Hamming distance to surface look-alikes for merging.

_MP_PLATFORM = "__mp__"


def hash_masterpieces(conn: sqlite3.Connection) -> dict:
    """Hash every Masterpiece's local hero image, storing it under the synthetic
    ``__mp__`` platform keyed by name. Prunes hashes for names that no longer
    exist and skips names already hashed. Zero network. {scanned, hashed}."""
    from pathlib import Path
    from posting import artwork_reader
    ensure_table(conn)
    try:
        artworks = artwork_reader.list_artworks()
    except Exception:
        return {"scanned": 0, "hashed": 0}
    current = {a.get("name") for a in artworks if a.get("name")}
    have = {r["submission_id"] for r in conn.execute(
        "SELECT submission_id FROM image_hashes WHERE platform = ?", (_MP_PLATFORM,))}
    # Prune stale (deleted/merged) Masterpiece hashes.
    for stale in have - current:
        conn.execute("DELETE FROM image_hashes WHERE platform = ? AND submission_id = ?",
                     (_MP_PLATFORM, stale))
    scanned = hashed = 0
    for art in artworks:
        name = art.get("name")
        folder = art.get("path")
        image = art.get("image")
        if not name or not folder or not image or name in have:
            continue
        scanned += 1
        ph = dhash_from_path(Path(folder) / image)
        if ph:
            store(conn, _MP_PLATFORM, name, ph, source="mp")
            hashed += 1
    conn.commit()
    return {"scanned": scanned, "hashed": hashed}


def duplicate_masterpiece_groups(conn: sqlite3.Connection,
                                 dismissed: set | None = None) -> list[list[str]]:
    """Clusters of Masterpiece names whose hero images are near-identical (Hamming
    ≤ threshold). Only groups of 2+ are returned. Assumes hash_masterpieces() ran.

    ``dismissed`` is a set of user-confirmed "not the same" name pairs (normalised
    ``(a, b)`` with a < b) — those edges are skipped, so a rejected look-alike pair
    never re-groups (2.145.0)."""
    dismissed = dismissed or set()
    rows = [(r["submission_id"], r["phash"]) for r in conn.execute(
        "SELECT submission_id, phash FROM image_hashes WHERE platform = ?", (_MP_PLATFORM,))]
    n = len(rows)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            pair = (rows[i][0], rows[j][0]) if rows[i][0] < rows[j][0] else (rows[j][0], rows[i][0])
            if pair in dismissed:
                continue  # user said these aren't the same image — don't link them
            if hamming(rows[i][1], rows[j][1]) <= HAMMING_THRESHOLD:
                union(i, j)
    clusters: dict[int, list[str]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(rows[i][0])
    return [g for g in clusters.values() if len(g) >= 2]
