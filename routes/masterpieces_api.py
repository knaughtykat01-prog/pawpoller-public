"""Masterpieces API (read) — a Masterpiece is the master record for ONE image
across every site it was posted to. See docs/specs/masterpieces.md.

Canonical metadata (title / description / rating / tags / characters) lives on
disk as masterpiece.json (posting/artwork_reader.py); cross-site membership +
pooled analytics come from the masterpiece_members table (database/
masterpiece_queries.py). This router merges the two for the Library grid + the
detail view. Phase 1 is READ-ONLY — the promote/link flow that populates members
lands in Phase 3, so a fresh Masterpiece lists with zeroed pooled stats until
then (expected).
"""
import logging
import re

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from database.db import get_connection
from database import artist_queries as aq
from database import masterpiece_queries as mq
from posting import artwork_reader

logger = logging.getLogger(__name__)

# Same archive cap the artwork uploader enforces (routes/artwork_api.py).
_MAX_IMAGE_BYTES = 50 * 1024 * 1024

masterpieces_router = APIRouter(prefix="/api/masterpieces", tags=["masterpieces"])

# Platforms worth previewing a tag budget for — the ones that take tags at
# all. Bluesky has no tag field; Twitter/Mastodon/Tumblr are microblog paths.
_PREVIEW_PLATFORMS = ("fa", "ib", "e621", "sf", "ws", "da", "fn", "ik", "fbr")


@masterpieces_router.get("")
def list_masterpieces(limit: int | None = None, offset: int = 0):
    """Every Masterpiece (one per artwork folder) + a light pooled rollup.

    The canonical fields come from disk (masterpiece.json); ``summary`` carries the
    live cross-site pooling (totals / personas / member count / cover). We adopt
    each name into the thin ``masterpieces`` index so Phase 3's linker always has a
    row to hang members off.

    **Perf guardrail (2.165.0):** the rollup is batched — one bulk member fetch +
    one submission fetch per platform + one persona map + one bulk index-ensure —
    instead of the old per-name fan-out (a rollup query per member AND a write per
    name, on every load). Optional ``limit``/``offset`` cap the response for very
    large libraries; the default returns everything (the grid caches + filters
    client-side), and ``total`` is always the full count.
    """
    conn = get_connection()
    try:
        arts_all = artwork_reader.list_artworks()
        # Preserve the "every artwork has an index row" invariant cheaply: one
        # read + a write only for names not yet indexed (usually none).
        mq.ensure_indexed_bulk(conn, [a["name"] for a in arts_all])
        total = len(arts_all)

        page = arts_all
        if limit is not None:
            start = max(0, offset)
            page = arts_all[start:start + max(0, limit)]

        st = mq.statuses(conn)
        summaries = mq.summarize_many(conn, [a["name"] for a in page])
        out = [{**art, "summary": summaries.get(art["name"], {}),
                "status": st.get(art["name"], "")} for art in page]
        conn.commit()
        return {"masterpieces": out, "total": total}
    finally:
        conn.close()


# ── De-duplication (2.144.0) ──────────────────────────────────────────────────
# NOTE: these MUST be declared before the generic /{name} route, else Starlette
# captures "/duplicates" as name="duplicates".

@masterpieces_router.get("/duplicates")
def masterpiece_duplicates():
    """Groups of Masterpieces whose hero images are near-identical (perceptual
    hash), so the user can merge same-image duplicates. Each group is sorted with
    the best merge-survivor first (most views, then most sites)."""
    from database import image_hash
    conn = get_connection()
    try:
        image_hash.hash_masterpieces(conn)                     # populate + prune
        dismissed = mq.not_duplicate_pairs(conn)               # user "not the same" decisions
        groups = image_hash.duplicate_masterpiece_groups(conn, dismissed=dismissed)
        result = []
        for g in groups:
            items = []
            for name in g:
                try:
                    art = artwork_reader.load_artwork(name)
                except FileNotFoundError:
                    continue
                s = mq.summarize(conn, name)
                items.append({
                    "name": name,
                    "title": art.title or name,
                    "image": art.image,
                    "thumbnail": art.thumbnail or "",
                    "cover_thumb": s.get("cover_thumb", ""),
                    "cover_platform": s.get("cover_platform", ""),
                    "views": (s.get("totals") or {}).get("views", 0),
                    "sites": s.get("member_count", 0),
                })
            if len(items) >= 2:
                items.sort(key=lambda x: (x["views"], x["sites"]), reverse=True)
                result.append(items)
        return {"groups": result}
    finally:
        conn.close()


@masterpieces_router.get("/unfiled-posts")
def list_unfiled_posts():
    """Publications whose work no longer exists on disk — "unfiled posts".

    These are invisible everywhere else **by construction**, which is why they
    went unnoticed: every works list is built from folders on disk, so a
    publication naming a folder that is gone renders nowhere; and the discovered
    list excludes anything that HAS a publication row, so the hand-link picker
    cannot offer it either. A post could be polled, recorded, and completely
    unreachable from the dashboard.

    Causes, in order of how many they produced:

      * **Folding** a piece into another moved its members and deleted its
        folder while leaving publications behind (fixed in 3.16.0, but the rows
        already created stay until someone files them).
      * **Deleting** a local artwork folder deliberately keeps the publication —
        the art still exists on the platform, so the record of having posted it
        is worth keeping. That is defensible, but it needs somewhere to be seen.

    Read-only. `linked_to` says whether the underlying upload is already a
    member of some Masterpiece, which distinguishes "the stats pool fine, only
    the paperwork is stale" from "this post counts for nothing".
    """
    from posting import story_reader
    try:
        names = {a["name"] for a in artwork_reader.list_artworks()}
        names |= {s_["name"] for s_ in story_reader.list_stories()}
    except Exception as e:
        logger.error("unfiled-posts: could not list archives: %s", e, exc_info=True)
        raise HTTPException(500, detail="Could not read the archives")
    conn = get_connection()
    try:
        rows = mq.orphan_publications(conn, names)
    finally:
        conn.close()
    # Group by the vanished work so the UI shows one card per missing piece
    # rather than one row per platform.
    grouped: dict[str, dict] = {}
    for r in rows:
        g = grouped.setdefault(r["story_name"], {
            "story_name": r["story_name"], "content_type": r["content_type"],
            "posts": [], "any_linked": False,
        })
        g["posts"].append(r)
        if r["linked_to"]:
            g["any_linked"] = True
    out = sorted(grouped.values(), key=lambda g: g["story_name"].lower())
    return {"unfiled": out, "works": len(out), "posts": len(rows)}


@masterpieces_router.get("/mislink-audit")
def masterpiece_mislink_audit():
    """Find members whose linked platform image doesn't match ANY local image of
    the Masterpiece — a wrong site-link pooling a different piece's stats. Pure
    perceptual-hash cross-check (native/offline, no model): each member's stored
    hash vs the folder's local image hashes; flag anything past the threshold.
    Members with no stored hash are skipped (can't judge). Returns flagged rows
    with view/thumbnail links so the user can review + unlink. (2.192.0)"""
    from database import image_hash
    from database import collections_queries as cq
    conn = get_connection()
    flagged = []
    try:
        archive = artwork_reader.get_artwork_archive_path()
        for d in sorted(archive.iterdir()):
            if not d.is_dir() or not (d / "masterpiece.json").is_file():
                continue
            name = d.name
            local_hashes = []
            for f in d.iterdir():
                if f.suffix.lower() in artwork_reader.IMAGE_EXTENSIONS:
                    h = image_hash.dhash_from_path(str(f))
                    if h:
                        local_hashes.append(h)
            if not local_hashes:
                continue
            title = ""
            for m in mq.get_members(conn, name):
                row = conn.execute(
                    "SELECT phash FROM image_hashes WHERE platform = ? AND submission_id = ?",
                    (m["platform"], str(m["submission_id"]))).fetchone()
                if not row or not row["phash"]:
                    continue  # no stored hash → can't judge
                best = min((image_hash.hamming(row["phash"], h) for h in local_hashes),
                           default=999)
                if best > image_hash.HAMMING_THRESHOLD:
                    if not title:
                        try:
                            title = artwork_reader.load_artwork(name).title
                        except Exception:
                            title = name.replace("_", " ")
                    loc = cq._location_from_submission(
                        conn, m["platform"], str(m["submission_id"]),
                        source="masterpiece") or {}
                    flagged.append({
                        "name": name,
                        "title": title or name.replace("_", " "),
                        "platform": m["platform"],
                        "submission_id": str(m["submission_id"]),
                        "distance": best,
                        "view_url": loc.get("url", ""),
                        "thumbnail_url": loc.get("thumbnail_url", ""),
                        "member_title": loc.get("title", ""),
                    })
    finally:
        conn.close()
    flagged.sort(key=lambda x: -x["distance"])
    return {"flagged": flagged, "threshold": image_hash.HAMMING_THRESHOLD}


@masterpieces_router.get("/variant-suggestions")
def masterpiece_variant_suggestions():
    """Likely VARIANT families grouped by TITLE — the complement to /duplicates.

    /duplicates finds the same IMAGE cross-posted; this finds the same PIECE in
    different renders (``Foo`` + ``Foo (Rough)``, SFW + NSFW), which hash-matching
    can't — they're different images. Each family suggests a hero + per-member
    variant key/label derived from the title suffix, ready for /merge-as-variant.

    A title heuristic is fuzzy, so this only SUGGESTS; the UI reviews each family.
    """
    from database import variant_suggest
    conn = get_connection()
    try:
        arts = artwork_reader.list_artworks()
        # Views feed hero selection + ordering; summarize is the same source the
        # dup finder uses, so the two review lists rank consistently.
        items = []
        summaries = {}
        for a in arts:
            s = mq.summarize(conn, a["name"])
            summaries[a["name"]] = s
            items.append({"name": a["name"], "title": a.get("title") or a["name"],
                          "views": (s.get("totals") or {}).get("views", 0)})
        dismissed = variant_suggest.not_variant_pairs(conn)
        families = variant_suggest.suggest_families(items, dismissed=dismissed)

        by_name = {a["name"]: a for a in arts}
        out = []
        for fam in families:
            members = []
            for m in fam["members"]:
                art = by_name.get(m["name"], {})
                s = summaries.get(m["name"], {})
                members.append({
                    **m,
                    "image": art.get("image", ""),
                    "cover_thumb": s.get("cover_thumb", ""),
                    "cover_platform": s.get("cover_platform", ""),
                    "sites": s.get("member_count", 0),
                })
            out.append({"base": fam["base"], "members": members})
        return {"families": out}
    finally:
        conn.close()


@masterpieces_router.post("/not-variant")
def masterpiece_not_variant(body: dict):
    """Remember that a suggested family is NOT variants of one piece, so the
    by-title finder stops offering it. Separate from /not-duplicate on purpose."""
    from database import variant_suggest
    names = [n for n in (body.get("names") or []) if isinstance(n, str) and n.strip()]
    if len(names) < 2:
        raise HTTPException(400, detail="names must list 2+ Masterpieces")
    conn = get_connection()
    try:
        added = variant_suggest.add_not_variant(conn, names)
        return {"status": "dismissed", "pairs_added": added}
    finally:
        conn.close()


@masterpieces_router.get("/match")
def masterpiece_match(platform: str, submission_id: str):
    """Does this discovered upload look like an EXISTING Masterpiece?

    Prevents new duplicates forming at promote time (backlog M): before minting a
    fresh Masterpiece from a discovered piece we check its stored thumbnail hash
    against every Masterpiece hero hash. Returns the closest match within the
    near-duplicate threshold, or ``{"match": null}``.

    Deliberately a SUGGESTION, never automatic — near-identical hashes are not
    proof of sameness (an SFW and NSFW edit of one ref sheet hash the same), so
    the caller asks the user before linking instead of creating.
    """
    from database import image_hash
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT phash FROM image_hashes WHERE platform = ? AND submission_id = ?",
            (platform, str(submission_id))).fetchone()
        if not row:
            return {"match": None}          # not hashed yet → no opinion
        image_hash.hash_masterpieces(conn)  # make sure hero hashes are current
        best, best_d = None, 65
        for r in conn.execute(
                "SELECT submission_id, phash FROM image_hashes WHERE platform = '__mp__'"):
            d = image_hash.hamming(row["phash"], r["phash"])
            if d <= image_hash.HAMMING_THRESHOLD and d < best_d:
                best, best_d = r["submission_id"], d
        if not best:
            return {"match": None}
        try:
            art = artwork_reader.load_artwork(best)
            title = art.title or best
        except FileNotFoundError:
            title = best
        return {"match": {"name": best, "title": title,
                          "similarity": round(1.0 - best_d / 64.0, 3)}}
    finally:
        conn.close()


@masterpieces_router.post("/not-duplicate")
def not_duplicate_ep(body: dict):
    """Remember that these Masterpieces are NOT the same image, so the finder stops
    grouping them. Body: {names: [name, name, ...]} (2+)."""
    names = [str(n) for n in (body.get("names") or []) if n]
    if len(names) < 2:
        raise HTTPException(400, detail="names must list at least two Masterpieces")
    conn = get_connection()
    try:
        added = mq.add_not_duplicate(conn, names)
    finally:
        conn.close()
    return {"status": "remembered", "pairs_added": added}


@masterpieces_router.post("/merge")
def merge_masterpieces_ep(body: dict):
    """Merge two Masterpieces of the same image: fold ``drop``'s site-links into
    ``keep`` and delete ``drop`` (its image is identical). Body: {keep, drop}."""
    keep = (body.get("keep") or "").strip()
    drop = (body.get("drop") or "").strip()
    if not keep or not drop or keep == drop:
        raise HTTPException(400, detail="keep and drop (distinct) are required")
    conn = get_connection()
    try:
        artwork_reader.load_artwork(keep)          # 404 if either is missing
        art_drop = artwork_reader.load_artwork(drop)
        # Publications BEFORE the merge: `merge_masterpieces` deletes `drop`'s
        # member rows, and we need to read its publications while the work still
        # exists. Folding used to move members and leave publications behind,
        # pointing at a folder this route is about to delete — the ghost-
        # publication bug (3.16.0).
        pubs = mq.absorb_publications(conn, keep, drop)
        moved = mq.merge_masterpieces(conn, keep, drop)
        # Drop the now-redundant folder (identical image) + its cached hero hash.
        conn.execute("DELETE FROM image_hashes WHERE platform = '__mp__' AND submission_id = ?", (drop,))
        conn.commit()
    except FileNotFoundError:
        raise HTTPException(404, detail="Masterpiece not found")
    finally:
        conn.close()
    import shutil
    try:
        shutil.rmtree(art_drop.path)
    except OSError:
        logger.warning("merge: could not remove folder for %s", drop)
    if pubs["blocked"]:
        logger.warning("merge %s -> %s: %d publication(s) could not be re-pointed "
                       "(same platform+account already published on the target); "
                       "they remain visible under Unfiled Posts",
                       drop, keep, pubs["blocked"])
    return {"status": "merged", "keep": keep, "dropped": drop,
            "members_moved": moved, "publications": pubs}


# ── Variants (2.158.0, spec docs/specs/masterpiece_variants.md) ──────────────
# One piece of art, several renders (SFW/NSFW, censored/clean, dedication...).
# Definitions live in masterpiece.json "variants"; site-uploads carry
# masterpiece_members.variant_key so each variant's stats track separately while
# the cohort total stays the plain all-members rollup.

_VARIANT_KEY_RE = re.compile(r"^[a-z0-9_\-]{1,32}$")


def _raw_variants(name: str) -> list[dict]:
    try:
        raw = artwork_reader.read_raw_metadata(name) or {}
    except FileNotFoundError:
        return []
    out = []
    for v in raw.get("variants") or []:
        if isinstance(v, dict) and v.get("key") is not None and v.get("image"):
            out.append({"key": str(v["key"]), "label": v.get("label") or str(v["key"]),
                        "image": v["image"], "rating": v.get("rating") or ""})
    return out


def _write_variants(name: str, variants: list[dict]) -> None:
    artwork_reader.save_artwork_metadata(name, {"variants": variants})


@masterpieces_router.post("/merge-as-variant")
def merge_as_variant_ep(body: dict):
    """Fold ``absorb`` into ``keep`` as labeled VARIANT(s) of the same piece.
    Body: {keep, absorb, key, label?, rating?}. The dup-finder's third option —
    for different RENDERS of one piece, where /merge (identical image) is wrong.

    If ``absorb`` has its OWN declared variants, ALL of them are carried across
    as distinct variants on ``keep`` (2.189.2) — its images are copied in, its
    variant labels preserved (prefixed with this merge's label), and each
    member re-keyed to its carried variant. Before this, only absorb's hero came
    over and its other variant images + per-variant attribution were discarded."""
    keep = (body.get("keep") or "").strip()
    absorb = (body.get("absorb") or "").strip()
    key = (body.get("key") or "").strip().lower()
    if not keep or not absorb or keep == absorb:
        raise HTTPException(400, detail="keep and absorb (distinct) are required")
    if not _VARIANT_KEY_RE.match(key):
        raise HTTPException(400, detail="key must be a short slug (a-z0-9_-)")
    try:
        art_keep = artwork_reader.load_artwork(keep)
        art_absorb = artwork_reader.load_artwork(absorb)
    except FileNotFoundError:
        raise HTTPException(404, detail="Masterpiece not found")

    from pathlib import Path
    import shutil

    variants = _raw_variants(keep)
    # Seed keep's own primary as an explicit variant on first use, so the set is
    # self-describing (primary '' + whatever we're adding).
    if not variants:
        variants = [{"key": "", "label": body.get("primary_label") or "Primary",
                     "image": art_keep.image, "rating": ""}]

    label = (body.get("label") or key).strip()
    rating0 = (body.get("rating") or "").strip().lower()

    # The renders absorb contributes: its declared variants, or — for a plain
    # piece — a single synthetic primary pointing at its hero.
    b_variants = _raw_variants(absorb)
    b_renders = b_variants or [{"key": "", "label": "",
                                "image": art_absorb.image,
                                "rating": (getattr(art_absorb, "rating", "") or "")}]

    # Collision-free destination keys. Derived keys clash for mundane reasons
    # (the key comes from the typed label, and renames leave keys stale), so we
    # uniquify rather than 409 — a merge only ever APPENDS variants. absorb's
    # sub-variants are namespaced under the base key so they stay grouped.
    taken = {v["key"] for v in variants}

    def _uniq(k: str) -> str:
        k = k[:32] or "variant"
        if k not in taken:
            taken.add(k)
            return k
        base, n = k[:28], 2
        while f"{base}-{n}" in taken:
            n += 1
        k2 = f"{base}-{n}"
        taken.add(k2)
        return k2

    base_key = _uniq(key)
    keymap = {"": base_key}                       # absorb primary → base variant
    prim = next((r for r in b_renders if r["key"] == ""), None)
    plan = [{"image": (prim or {}).get("image") or art_absorb.image,
             "akey": base_key, "label": label,
             "rating": (prim or {}).get("rating") or rating0}]
    for r in b_renders:
        if r["key"] == "":
            continue
        akey = _uniq(f"{base_key}-{r['key']}")
        keymap[r["key"]] = akey
        plan.append({"image": r["image"], "akey": akey,
                     "label": f"{label} — {r['label'] or r['key']}",
                     "rating": (r.get("rating") or "")})

    # Copy each render's image into keep's folder + append its variants entry.
    i = 2
    base_image = ""
    for step in plan:
        src = Path(art_absorb.path) / step["image"]
        if not src.is_file():
            raise HTTPException(422, detail=f"{absorb}: variant image '{step['image']}' is missing")
        while (Path(art_keep.path) / f"image_{i}{src.suffix.lower()}").exists():
            i += 1
        dst = Path(art_keep.path) / f"image_{i}{src.suffix.lower()}"
        shutil.copy2(src, dst)
        i += 1
        if step["akey"] == base_key:
            base_image = dst.name
        variants.append({"key": step["akey"], "label": step["label"],
                         "image": dst.name, "rating": step["rating"]})
    _write_variants(keep, variants)

    conn = get_connection()
    try:
        # Same ghost-publication hazard as the plain fold (3.16.0) — this route
        # also rmtree's the absorbed folder.
        pubs = mq.absorb_publications(conn, keep, absorb)
        moved = mq.merge_as_variant(conn, keep, absorb, keymap)
        conn.execute("DELETE FROM image_hashes WHERE platform = '__mp__' AND submission_id = ?", (absorb,))
        conn.commit()
    finally:
        conn.close()
    try:
        shutil.rmtree(art_absorb.path)
    except OSError:
        logger.warning("merge-as-variant: could not remove folder for %s", absorb)
    if pubs["blocked"]:
        logger.warning("merge-as-variant %s -> %s: %d publication(s) could not be "
                       "re-pointed; they remain visible under Unfiled Posts",
                       absorb, keep, pubs["blocked"])
    return {"status": "merged-as-variant", "keep": keep, "absorbed": absorb,
            "publications": pubs,
            "key": base_key, "variants_added": len(plan), "members_moved": moved,
            "variant_image": base_image}


@masterpieces_router.post("/{name}/variants")
def declare_variant(name: str, body: dict):
    """Declare an EXISTING folder image as a labeled variant.
    Body: {key, image, label?, rating?}. Upgrades a 2.152 unlabeled alt in place."""
    key = (body.get("key") or "").strip().lower()
    image = (body.get("image") or "").strip()
    if not _VARIANT_KEY_RE.match(key):
        raise HTTPException(400, detail="key must be a short slug (a-z0-9_-)")
    try:
        art = artwork_reader.load_artwork(name)
    except FileNotFoundError:
        raise HTTPException(404, detail="Masterpiece not found")
    from pathlib import Path
    target = (Path(art.path) / image)
    if not image or not target.is_file() or \
            target.suffix.lower() not in artwork_reader.IMAGE_EXTENSIONS:
        raise HTTPException(422, detail="image must be an existing image file in this folder")
    variants = _raw_variants(name)
    if any(v["key"] == key for v in variants):
        raise HTTPException(409, detail=f"variant key '{key}' already exists")
    if not variants:
        variants = [{"key": "", "label": "Primary", "image": art.image, "rating": ""}]
    variants.append({"key": key, "label": (body.get("label") or key).strip(),
                     "image": image, "rating": (body.get("rating") or "").strip().lower()})
    _write_variants(name, variants)
    return {"status": "declared", "key": key}


@masterpieces_router.post("/{name}/variants/upload")
async def upload_variant(name: str, file: UploadFile = File(...),
                         label: str = Form(""), rating: str = Form("")):
    """Upload a fresh image straight in as a new labeled variant (2.190.2).

    Complements the other two paths: `POST /variants` declares an image already
    in the folder, `/merge-as-variant` folds another Masterpiece in — this one
    takes a brand-new file. The key is derived from the label (the user never
    types one) and uniquified, so it can't collide (mirrors merge's behaviour)."""
    from pathlib import Path
    ext = Path(file.filename or "").suffix.lower()
    if ext not in artwork_reader.IMAGE_EXTENSIONS:
        raise HTTPException(415, detail=(
            f"Unsupported image type: {ext or '(none)'}. "
            f"Allowed: {', '.join(artwork_reader.IMAGE_EXTENSIONS)}"))
    data = await file.read()
    if not data:
        raise HTTPException(400, detail="Empty image upload")
    if len(data) > _MAX_IMAGE_BYTES:
        raise HTTPException(413, detail="Image exceeds the 50 MB archive cap")

    try:
        art = artwork_reader.load_artwork(name)
    except FileNotFoundError:
        raise HTTPException(404, detail="Masterpiece not found")

    folder = Path(art.path)
    stem = re.sub(r"[^\w.\-]", "_", Path(file.filename or "variant").stem) or "variant"
    target = folder / f"{stem}{ext}"
    n = 1
    while target.exists():
        target = folder / f"{stem}_v{n}{ext}"
        n += 1
    target.write_bytes(data)

    variants = _raw_variants(name)
    if not variants:
        variants = [{"key": "", "label": "Primary", "image": art.image, "rating": ""}]
    label = (label or "").strip()
    base = re.sub(r"[^a-z0-9_-]+", "-", label.lower()).strip("-")[:32] or "variant"
    existing = {v["key"] for v in variants}
    key = base
    if key in existing:
        b, i = base[:28], 2
        while f"{b}-{i}" in existing:
            i += 1
        key = f"{b}-{i}"
    variants.append({"key": key, "label": label or key, "image": target.name,
                     "rating": (rating or "").strip().lower()})
    _write_variants(name, variants)
    return {"status": "added", "key": key, "label": label or key, "image": target.name}


@masterpieces_router.delete("/{name}/variants/{key}")
def delete_variant(name: str, key: str):
    """Demote a variant back to a plain alt image (file stays; members re-key
    to primary '')."""
    variants = _raw_variants(name)
    if not any(v["key"] == key for v in variants):
        raise HTTPException(404, detail="variant not found")
    _write_variants(name, [v for v in variants if v["key"] != key])
    conn = get_connection()
    try:
        mq.clear_variant_members(conn, name, key)
        conn.commit()
    finally:
        conn.close()
    return {"status": "removed", "key": key}


def _artist_from_body(raw) -> dict | None:
    """Turn the editor's artist payload into the stored shape, or None to clear.

    Accepts a bare string as well as ``{name, handles}`` — the same tolerance
    ``artwork_reader._clean_artist`` has, because "just type the name" is the
    common case and demanding an object for it would be pointless ceremony.

    **The registry does the rest.** A name that is already known and arrives
    with no handles is filled in from the 134 verified handles, server-side, so
    it works from a script or a curl as well as from the UI. A name that is NOT
    known is learned, so the registry grows as the catalogue is corrected
    instead of staying frozen at the August lookup. Handles that ARE supplied
    always win — the person editing is looking at the artist's page.
    """
    if raw in (None, "", {}):
        return None
    if isinstance(raw, str):
        name, handles = raw.strip(), {}
    elif isinstance(raw, dict):
        name = str(raw.get("name") or "").strip()
        h = raw.get("handles")
        handles = {str(k).strip().lower(): str(v).strip()
                   for k, v in h.items() if str(v or "").strip()} if isinstance(h, dict) else {}
    else:
        raise HTTPException(400, detail="artist must be a string or an object")
    if not name and not handles:
        return None

    conn = get_connection()
    try:
        known = aq.find_by_name(conn, name) if name else None
        if known and not handles:
            handles = dict(known.get("handles") or {})
            name = name or known["name"]
        if name:
            # Learn it. Merge semantics mean a handle typed here is added to the
            # artist rather than replacing everything already researched.
            aq.upsert_artist(conn, name, handles=handles)
            conn.commit()
    finally:
        conn.close()
    return {"name": name, "handles": handles}


@masterpieces_router.patch("/{name}/variants/{key}")
def rename_variant(name: str, key: str, body: dict):
    """Rename/re-label a variant (2.189.0). Body: {label?, key?, rating?}.

    Renaming used to mean DELETE + re-declare, and DELETE re-keys the variant's
    members to primary — so a cosmetic edit silently threw away every
    per-variant stat attribution. Changing `key` here migrates the members
    instead. The primary ('') may be re-labelled but never re-keyed: '' is the
    anchor the whole variant scheme keys off.
    """
    variants = _raw_variants(name)
    target = next((v for v in variants if v["key"] == key), None)
    if target is None:
        raise HTTPException(404, detail=f"no such variant '{key}' on {name}")

    new_label = body.get("label")
    new_key = body.get("key")
    new_rating = body.get("rating")
    if new_label is None and new_key is None and new_rating is None:
        raise HTTPException(400, detail="one of label, key or rating is required")

    if new_key is not None:
        new_key = str(new_key).strip().lower()
        if key == "":
            raise HTTPException(400, detail="the primary variant's key cannot be changed")
        if not _VARIANT_KEY_RE.match(new_key):
            raise HTTPException(400, detail="key must be a short slug (a-z0-9_-)")
        if new_key != key and any(v["key"] == new_key for v in variants):
            raise HTTPException(409, detail=f"variant key '{new_key}' already exists")

    for v in variants:
        if v["key"] != key:
            continue
        if new_label is not None:
            v["label"] = str(new_label).strip() or v["key"] or "Primary"
        if new_rating is not None:
            v["rating"] = str(new_rating).strip().lower()
        if new_key is not None:
            v["key"] = new_key
        break
    _write_variants(name, variants)

    moved = 0
    if new_key is not None and new_key != key:
        conn = get_connection()
        try:
            moved = mq.rename_variant_key(conn, name, key, new_key)
            conn.commit()
        finally:
            conn.close()
    return {"status": "renamed", "key": new_key if new_key is not None else key,
            "members_rekeyed": moved}


@masterpieces_router.post("/{name}/variants/{key}/split")
def split_variant(name: str, key: str, body: dict | None = None):
    """Separate a variant OUT into its own standalone Masterpiece (2.189.0).

    The true inverse of /merge-as-variant, which deletes the absorbed folder and
    was therefore a one-way door. The variant's image moves to a new folder, its
    members move with it (re-keyed to primary, so stats survive the round-trip),
    and the entry leaves the parent. Body: {new_name?}.
    """
    body = body or {}
    if key == "":
        raise HTTPException(400, detail="the primary variant IS the master — nothing to separate")
    variants = _raw_variants(name)
    target = next((v for v in variants if v["key"] == key), None)
    if target is None:
        raise HTTPException(404, detail=f"no such variant '{key}' on {name}")

    try:
        art = artwork_reader.load_artwork(name)
    except FileNotFoundError:
        raise HTTPException(404, detail="Masterpiece not found")

    from pathlib import Path
    src = Path(art.path) / target["image"]
    if not src.is_file():
        raise HTTPException(422, detail=f"variant image '{target['image']}' is missing on disk")
    # Never cannibalise the parent's hero (a variant should never point at it,
    # but a hand-edited masterpiece.json could).
    if target["image"] == art.image:
        raise HTTPException(422, detail="this variant points at the parent's hero image")

    raw = artwork_reader.read_raw_metadata(name) or {}
    label = target.get("label") or key
    title = (body.get("new_name") or "").strip() or f"{art.title or name} ({label})"
    new_name = artwork_reader.create_artwork(
        title=title,
        image_filename=src.name,
        image_bytes=src.read_bytes(),
        description=raw.get("description", "") or "",
        author=raw.get("author", "") or "",
        rating=(target.get("rating") or raw.get("rating") or ""),
        tags=raw.get("tags") or None,
        characters=raw.get("characters") or None,
    )

    # Members follow the variant, re-keyed to the new record's primary. The new
    # hero is hashed so the de-dup / variant finders see it straight away
    # (mirrors promote_from_submission).
    from database import image_hash
    new_art = artwork_reader.load_artwork(new_name)
    conn = get_connection()
    try:
        moved = mq.move_variant_members(conn, name, key, new_name)
        phash = image_hash.dhash_from_path(str(Path(new_art.path) / new_art.image))
        if phash:
            image_hash.store(conn, "__mp__", new_name, phash, source="split")
        conn.commit()
    finally:
        conn.close()

    # Drop the entry from the parent; a lone leftover primary is meaningless.
    remaining = [v for v in variants if v["key"] != key]
    if len(remaining) <= 1 and all(v["key"] == "" for v in remaining):
        remaining = []
    _write_variants(name, remaining)
    try:
        src.unlink()
    except OSError:
        logger.warning("split-variant: could not remove %s from %s", target["image"], name)

    return {"status": "split", "from": name, "key": key,
            "new_name": new_name, "members_moved": moved}


@masterpieces_router.post("/{name}/images/split")
def split_undeclared_image(name: str, body: dict):
    """Separate an UNDECLARED folder image (a 2.152 "alt") into its own Masterpiece.

    A **multi-image import** attaches every image from one source post to a single
    record, so a piece can end up carrying several unrelated artworks as bare files
    with no `variants` entry at all. Those were unreachable:
    `/variants/{key}/split` needs a variant to name, and the Manage-variants panel
    only renders when `variants` is non-empty — so the UI showed the extra images
    as "Alt 1 / Alt 2" chips with **no way to act on them**, and the only route out
    was hand-editing masterpiece.json.

    This is declare-then-split in ONE call: the image is upgraded to a labeled
    variant (what `POST /{name}/variants` does) and immediately separated by the
    existing `split_variant`. Doing it server-side keeps it atomic from the
    caller's view — chaining the two from the browser can strand a half-declared
    variant if the second call fails.

    Body: {image, label?, new_name?}.

    ⚠ Members do NOT move unless they were attributed to this image. A bare alt has
    never been posted from this record, so its uploads (if any) live on the parent
    with `variant_key = ''` and stay there — the new record starts with no site
    links and is pHash'd on creation so the link suggester can find its real ones.
    That is correct: nothing knows which upload belongs to which image.
    """
    image = (body.get("image") or "").strip()
    try:
        art = artwork_reader.load_artwork(name)
    except FileNotFoundError:
        raise HTTPException(404, detail="Masterpiece not found")

    from pathlib import Path
    target = Path(art.path) / image
    if not image or "/" in image or "\\" in image or not target.is_file() or \
            target.suffix.lower() not in artwork_reader.IMAGE_EXTENSIONS:
        raise HTTPException(422, detail="image must be an existing image file in this folder")
    if image == art.image:
        raise HTTPException(422, detail="that is this piece's primary image — separate an alt instead")

    variants = _raw_variants(name)
    if any(v["image"] == image for v in variants):
        raise HTTPException(
            409,
            detail="that image is already a declared variant — use Separate on it directly",
        )

    # Derive a key the declare-side regex accepts, and uniquify against whatever
    # is already declared (the same collision handling merge-as-variant uses).
    base = re.sub(r"[^a-z0-9_-]+", "-", target.stem.lower()).strip("-")[:32] or "alt"
    taken = {v["key"] for v in variants}
    key = base
    if key in taken or key == "":
        b, i = base[:28] or "alt", 2
        while f"{b}-{i}" in taken:
            i += 1
        key = f"{b}-{i}"

    if not variants:
        variants = [{"key": "", "label": "Primary", "image": art.image, "rating": ""}]
    variants.append({"key": key, "label": (body.get("label") or "").strip() or key,
                     "image": image, "rating": ""})
    _write_variants(name, variants)

    # Hand off to the real thing rather than restate it — one split implementation.
    return split_variant(name, key, {"new_name": body.get("new_name") or ""})


@masterpieces_router.patch("/{name}/members/variant")
def set_member_variant_ep(name: str, body: dict):
    """Attribute a linked site-upload to a variant.
    Body: {platform, submission_id, variant_key} (''=primary)."""
    platform = (body.get("platform") or "").strip()
    sid = str(body.get("submission_id") or "").strip()
    if not platform or not sid:
        raise HTTPException(400, detail="platform and submission_id are required")
    vkey = str(body.get("variant_key") or "")
    if vkey and not any(v["key"] == vkey for v in _raw_variants(name)):
        raise HTTPException(422, detail=f"no such variant '{vkey}' on {name}")
    conn = get_connection()
    try:
        mq.set_member_variant(conn, name, platform, sid, vkey)
        conn.commit()
    finally:
        conn.close()
    return {"status": "attributed", "variant_key": vkey}


# ── Replace the canonical image (2.153.0) ────────────────────────────────────
# "The artist sent the full-res / a fixed version" — swap the image a Masterpiece
# points at WITHOUT losing the record: masterpiece.json (title/desc/rating/tags)
# and every masterpiece_members site-link survive untouched, so pooled stats and
# links carry straight over. Previously the only route was delete + re-import,
# which threw all of that away.
#
# Deliberately NON-DESTRUCTIVE: the old file stays in the folder, so it drops into
# the 2.152 gallery strip as an alternate rather than being overwritten. This only
# ever moves the *hero* pointer (`image`) — it never touches the other images.

@masterpieces_router.post("/{name}/image")
async def replace_masterpiece_image(name: str, file: UploadFile = File(...)):
    """Replace the canonical (hero) image. Keeps metadata, members and the old file."""
    from pathlib import Path

    ext = Path(file.filename or "").suffix.lower()
    if ext not in artwork_reader.IMAGE_EXTENSIONS:
        raise HTTPException(415, detail=(
            f"Unsupported image type: {ext or '(none)'}. "
            f"Allowed: {', '.join(artwork_reader.IMAGE_EXTENSIONS)}"))
    data = await file.read()
    if not data:
        raise HTTPException(400, detail="Empty image upload")
    if len(data) > _MAX_IMAGE_BYTES:
        raise HTTPException(413, detail="Image exceeds the 50 MB archive cap")

    try:
        art = artwork_reader.load_artwork(name)
    except FileNotFoundError:
        raise HTTPException(404, detail="Masterpiece not found")

    folder = Path(art.path)
    # Never clobber an existing file (least of all the current hero) — the old
    # version must survive as a gallery alternate.
    stem = re.sub(r"[^\w.\-]", "_", Path(file.filename or "image").stem) or "image"
    target = folder / f"{stem}{ext}"
    n = 1
    while target.exists():
        target = folder / f"{stem}_v{n}{ext}"
        n += 1
    target.write_bytes(data)

    previous = art.image
    artwork_reader.save_artwork_metadata(name, {"image": target.name})

    # The hero changed, so the cached perceptual hash is stale — drop it and let
    # hash_masterpieces() recompute, or the de-dup finder would compare the OLD
    # pixels forever.
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM image_hashes WHERE platform = '__mp__' AND submission_id = ?",
            (name,))
        conn.commit()
    finally:
        conn.close()

    images = sorted(f.name for f in folder.iterdir()
                    if f.suffix.lower() in artwork_reader.IMAGE_EXTENSIONS)
    logger.info("Masterpiece %s: hero image %s -> %s", name, previous, target.name)
    return {"status": "replaced", "name": name, "image": target.name,
            "previous": previous, "images": images}


# ── Junk status (2.149.0) ─────────────────────────────────────────────────────
# 'junk' = kept-but-hidden: for pulled art that isn't wanted in the grid (memes,
# other people's ads, retired pieces) without deleting the record/folder.
# Reversible — restore sets status back to ''. Softer than /merge (which deletes).

_MP_STATUSES = {"", "junk"}


@masterpieces_router.post("/{name}/status")
def set_masterpiece_status(name: str, body: dict):
    """Set the Masterpiece's junk status. Body: {status: 'junk' | ''}.

    Accepts index-only names (Masterpieces with no folder — e.g. swept-in tweets)
    as well as real folders, since junking is exactly what those need.
    """
    status = str((body or {}).get("status", "")).strip().lower()
    if status not in _MP_STATUSES:
        raise HTTPException(400, detail="status must be 'junk' or '' (restore)")
    conn = get_connection()
    try:
        has_folder = True
        try:
            artwork_reader.load_artwork(name)
        except FileNotFoundError:
            has_folder = False
        if not has_folder and not conn.execute(
                "SELECT 1 FROM masterpieces WHERE name = ?", (name,)).fetchone():
            raise HTTPException(404, detail="Masterpiece not found")
        mq.set_status(conn, name, status)
        conn.commit()
        return {"status": "updated", "name": name, "junk": status == "junk"}
    finally:
        conn.close()


@masterpieces_router.get("/{name}")
def get_masterpiece(name: str):
    """Full detail: canonical metadata (from masterpiece.json) + resolved member
    locations + pooled totals / tags / personas.

    ``canonical_tags`` is the master record's per-platform tag map; ``tags`` is the
    union actually observed on the live member uploads (empty until members exist).

    2.193.0 — this is now the payload behind the UNIFIED art detail page, reached
    from both ``#/masterpieces/{name}`` and ``#/artwork/image/{name}``. The four
    fields the old Artwork-only page needed (``alt_text``, ``publications``,
    ``titles``/``descriptions``/``categories`` for per-platform publish
    overrides) are therefore served here too, so the page is one request rather
    than a merge of two endpoints. Every artwork folder already IS a masterpiece
    — same folder, same ``masterpiece.json``, same ``load_artwork`` — so this is
    filling in a payload gap, not conflating two entities.
    """
    try:
        art = artwork_reader.load_artwork(name)
    except FileNotFoundError:
        raise HTTPException(404, detail="Masterpiece not found")
    # Every image file in the folder, hero first (2.152.0) — multi-image tweet
    # sets and preserved SFW/NSFW variants live beside the hero as image_N.*;
    # the detail view renders them as a gallery strip via /api/artwork/image.
    from pathlib import Path
    images = sorted(f.name for f in Path(art.path).iterdir()
                    if f.suffix.lower() in artwork_reader.IMAGE_EXTENSIONS)
    if art.image in images:
        images.remove(art.image)
        images.insert(0, art.image)
    conn = get_connection()
    try:
        mq.ensure_indexed(conn, name)
        conn.commit()
        roll = mq.rollup_members(conn, name)
        # Per-variant stats (2.158.0): each declared variant's members rolled up
        # alone; the cohort totals below stay the all-members rollup.
        variants = []
        for v in _raw_variants(name):
            vroll = mq.rollup_members(conn, name, v["key"])
            variants.append({**v, "totals": vroll["totals"],
                             "member_count": len(vroll["members"])})
        # Publish/schedule surface (ported from the Artwork detail page, 2.193.0).
        from database import posting_queries as _pq
        pubs = _pq.get_publications_with_stats(
            conn, story_name=name, content_type="artwork")
        return {
            "name": art.name,
            "status": mq.get_status(conn, name),
            "images": images,
            "variants": variants,
            "title": art.title,
            "description": art.description,
            "author": art.author,
            # Who drew it (3.5.2) — `author` is the posting persona, a different
            # thing entirely. The detail page warns when this is absent, because
            # the credit line is built from it at post time.
            "artist": art.artist,
            "artist_status": art.artist_status,
            "rating": art.rating,
            "image": art.image,
            "thumbnail": art.thumbnail,
            "characters": art.characters,
            "platforms": art.platforms,
            "created_at": art.created_at,
            "canonical_tags": art.tags_by_platform,
            "members": roll["members"],
            "locations": roll["locations"],
            "totals": roll["totals"],
            "tags": roll["tags"],
            "persona_ids": roll["persona_ids"],
            # ── unified-detail additions (2.193.0) ──
            "alt_text": art.alt_text,
            "publications": pubs,
            "titles": art.titles_by_platform,
            "descriptions": art.descriptions_by_platform,
            "categories": art.categories_by_platform,
        }
    finally:
        conn.close()


@masterpieces_router.get("/{name}/snapshots")
def get_masterpiece_snapshots(name: str):
    """Combined time-series (summed views/faves/comments) across every site this
    Masterpiece lives on — the same chart a Collection draws, scoped to one image."""
    try:
        artwork_reader.load_artwork(name)
    except FileNotFoundError:
        raise HTTPException(404, detail="Masterpiece not found")
    conn = get_connection()
    try:
        from database import analytics_queries
        pairs = mq.member_pairs(conn, name)
        return {"snapshots": analytics_queries.get_combined_snapshots(conn, pairs)}
    finally:
        conn.close()


@masterpieces_router.get("/{name}/suggestions")
def get_masterpiece_suggestions(name: str):
    """Native (no-AI) same-image candidates not yet linked to this Masterpiece —
    perceptual-hash + title, anchored to the master's members/canonical image.
    Warm the hash store first via POST /api/collections/hash-scan if it's cold."""
    try:
        artwork_reader.load_artwork(name)
    except FileNotFoundError:
        raise HTTPException(404, detail="Masterpiece not found")
    conn = get_connection()
    try:
        return {"suggestions": mq.suggestions(conn, name)}
    finally:
        conn.close()


# ── Write (promote + membership, Phase 3) ────────────────────────

@masterpieces_router.post("")
def promote_masterpiece(body: dict):
    """Promote a discovered/imported submission into a Masterpiece + seed its
    primary member. Body: {from: {platform, submission_id}} (spec §3.1)."""
    src = (body or {}).get("from") or {}
    platform = (src.get("platform") or "").strip()
    sid = str(src.get("submission_id") or "").strip()
    if not platform or not sid:
        raise HTTPException(400, detail="from.platform and from.submission_id are required")
    conn = get_connection()
    try:
        res = mq.promote_from_submission(conn, platform, sid)
        conn.commit()
        return {"status": res.get("status", "imported"), "name": res["name"],
                "images": res.get("images", 1)}
    except ValueError as e:
        # Un-importable submission (no image URL, FA datacenter-IP block, …).
        raise HTTPException(422, detail=str(e))
    finally:
        conn.close()


@masterpieces_router.post("/{name}/members")
def add_masterpiece_member(name: str, body: dict):
    """Attach a site-upload to this Masterpiece. Body: {platform, submission_id,
    account_id?, role?, linked_via?}."""
    try:
        artwork_reader.load_artwork(name)
    except FileNotFoundError:
        raise HTTPException(404, detail="Masterpiece not found")
    platform = (body.get("platform") or "").strip()
    sid = str(body.get("submission_id") or "").strip()
    if not platform or not sid:
        raise HTTPException(400, detail="platform and submission_id are required")
    conn = get_connection()
    try:
        # Default the member's account to the source submission's, so persona
        # rollup stays correct (the "everything lumps under the default" bug).
        acct = body.get("account_id")
        if acct is None:
            from database.collections_queries import _submission_row
            acct = (_submission_row(conn, platform, sid) or {}).get("account_id")
        mq.add_member(conn, name, platform, sid, account_id=acct,
                      role=body.get("role", "crosspost"),
                      linked_via=body.get("linked_via", "manual"))
        conn.commit()
        return {"status": "added"}
    finally:
        conn.close()


@masterpieces_router.post("/{name}/link-url")
def link_member_by_url(name: str, body: dict):
    """Attach a site upload to this Masterpiece from a pasted URL.

    The hand-link picker can only offer *discovered* submissions — posts with no
    publication row. A post that PawPoller already recorded under its own title
    (its FA title, say) is therefore invisible to it, even though that is exactly
    the case a person hits: "I have the link, why can't I attach it".

    **Previews by default** (the artist-rename pattern, 3.11.0). ``confirm: true``
    performs the link. The preview says what the URL resolved to, whether that
    submission is actually stored, and what it is currently attached to — because
    the interesting answer is usually "it IS known, it is just filed under a
    different work", and silently linking would hide that.

    Body: ``{url, confirm?: bool, platform?, submission_id?}``. The explicit
    platform/submission_id pair overrides parsing, for the ambiguous URLs the
    parser deliberately refuses to guess at.
    """
    from posting import submission_urls
    from database.collections_queries import _submission_row

    try:
        artwork_reader.load_artwork(name)
    except FileNotFoundError:
        raise HTTPException(404, detail="Masterpiece not found")

    url = (body.get("url") or "").strip()
    forced_p = (body.get("platform") or "").strip()
    forced_s = str(body.get("submission_id") or "").strip()

    if forced_p and forced_s:
        cands = [(forced_p, forced_s)]
    elif url:
        cands = submission_urls.candidates_for(url)
    else:
        raise HTTPException(400, detail="url (or platform + submission_id) is required")

    if not cands:
        raise HTTPException(422, detail=(
            "That does not look like a submission link I recognise. Supported: "
            + ", ".join(submission_urls.SUPPORTED_PLATFORMS)))

    conn = get_connection()
    try:
        resolved = []
        for plat, sid in cands:
            row = _submission_row(conn, plat, sid) or {}
            owner = conn.execute(
                "SELECT masterpiece_name FROM masterpiece_members "
                "WHERE platform = ? AND submission_id = ?", (plat, str(sid))).fetchone()
            pub = conn.execute(
                "SELECT story_name FROM publications WHERE platform = ? AND external_id = ?",
                (plat, str(sid))).fetchone()
            resolved.append({
                "platform": plat,
                "submission_id": sid,
                # Known = the poller has actually seen this submission. Linking an
                # unknown id would create a member that pools no stats and merely
                # looks attached.
                "known": bool(row),
                "title": row.get("title", "") if row else "",
                "account_id": row.get("account_id") if row else None,
                "linked_to": owner["masterpiece_name"] if owner else "",
                "publication_of": pub["story_name"] if pub else "",
            })

        best = next((r for r in resolved if r["known"]), resolved[0])

        if not body.get("confirm"):
            return {"status": "preview", "name": name, "resolved": resolved,
                    "best": best, "would_link": best["known"] and not best["linked_to"]}

        if not best["known"]:
            raise HTTPException(404, detail=(
                f"No stored {best['platform']} submission {best['submission_id']}. "
                "Poll that platform first — linking an id nobody has would pool no stats."))
        if best["linked_to"] and best["linked_to"] != name:
            raise HTTPException(409, detail=(
                f"That upload is already linked to '{best['linked_to']}'. "
                "Unlink it there first."))

        mq.add_member(conn, name, best["platform"], best["submission_id"],
                      account_id=best["account_id"], role=body.get("role", "crosspost"),
                      linked_via="url")
        conn.commit()
        return {"status": "linked", "name": name, "linked": best}
    finally:
        conn.close()


@masterpieces_router.delete("/{name}/publication")
def forget_masterpiece_publication(name: str, platform: str,
                                   confirm_platform: str = "",
                                   keep_member: bool = False):
    """Forget that this piece was posted to `platform` (3.17.0).

    **PawPoller cannot see a deletion made on the site.** Nothing polls for
    "is this submission still there", and no platform tells us. So when you
    delete an upload upstream — posted from the wrong account, wrong file,
    changed your mind — the local records survive it, and the Masterpiece page
    keeps that platform's checkbox dimmed and disabled ("Already on this site").
    From the user's side that reads as being locked out of re-posting a piece
    that is no longer anywhere.

    Two separate records cause that dimming and BOTH have to go, which is why
    `DELETE /{name}/members` alone was not enough:

      * the `publications` row — "posted here, at this account";
      * the `masterpiece_members` row — the pooled-stats link.

    The story side has had this since the publish-check panel
    (`DELETE /api/editor/stories/{name}/publication`), but that route resolves
    against the STORY archive, so it could never serve an artwork.

    Reversible: the response returns the `external_id` and URL that were
    forgotten, and `POST /{name}/link-url` takes that URL straight back. Pass
    `keep_member=true` to drop only the publication and keep the stats link —
    for a post that still exists but was recorded under the wrong account.

    `confirm_platform` must equal `platform`, matching the story route's shape.
    ⚠ It is a **typo guard, not authorisation** — its required value comes from
    another parameter in the same request, and `api.js` fills it in
    automatically, so nobody ever types it. The real protection is
    `session_auth_middleware` (which covers `/api/masterpieces/*`) plus a CORS
    policy that refuses cross-origin preflight; contrast `settings_api`'s
    uninstall check, where `confirm == "UNINSTALL"` is an out-of-band literal a
    human must actually supply. Do not treat this as a security boundary.
    """
    from database import posting_queries as _pq
    from posting import submission_urls

    if confirm_platform != platform:
        raise HTTPException(400, detail=(
            f"confirm_platform must equal '{platform}' (got '{confirm_platform}')"))

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT external_id, external_url, account_id FROM publications "
            "WHERE content_type = 'artwork' AND story_name = ? AND platform = ? "
            "AND chapter_index = 0", (name, platform)).fetchone()
        members = [dict(r) for r in conn.execute(
            "SELECT submission_id FROM masterpiece_members "
            "WHERE masterpiece_name = ? AND platform = ?", (name, platform))]

        removed_pub = _pq.delete_publication(conn, name, 0, platform,
                                             content_type="artwork")
        removed_members = []
        if not keep_member:
            for m in members:
                mq.remove_member(conn, name, platform, m["submission_id"])
                removed_members.append(m["submission_id"])
        conn.commit()

        if not removed_pub and not removed_members:
            raise HTTPException(404, detail=(
                f"Nothing recorded for {platform} on this piece — it is already "
                f"clear to post."))

        external_id = (row["external_id"] if row else "") or (
            members[0]["submission_id"] if members else "")
        url = (row["external_url"] if row else "") or (
            submission_urls.build_url(platform, external_id) if external_id else "")
        return {
            "status": "forgotten",
            "name": name,
            "platform": platform,
            "publication_removed": bool(removed_pub),
            "members_removed": removed_members,
            "account_id": (row["account_id"] if row else None),
            # Handed back so the action can be undone: paste this into
            # "Link a post by URL" to restore the link.
            "external_id": external_id,
            "external_url": url,
        }
    finally:
        conn.close()


@masterpieces_router.delete("/{name}/members")
def remove_masterpiece_member(name: str, platform: str, submission_id: str):
    """Detach a site-upload (query params: platform, submission_id)."""
    conn = get_connection()
    try:
        mq.remove_member(conn, name, platform, submission_id)
        conn.commit()
        return {"status": "removed"}
    finally:
        conn.close()


# ── Canonical edit + Sync-all (Phase 5) ──────────────────────────

# Canonical rating vocabulary (spec §0-A5) — the poster maps to each site's scale.
_RATINGS = {"general", "mature", "adult"}


@masterpieces_router.patch("/{name}")
def update_masterpiece(name: str, body: dict):
    """Edit the Masterpiece's canonical record (writes ``masterpiece.json``).

    Editable fields: title / description / rating / characters / tags (the
    canonical *default* tag set — per-platform overrides are preserved) /
    alt_text. This is the "edit once" half; pushing it to the live uploads is
    POST /{name}/sync.

    ``alt_text`` joined here in 2.193.0: it was previously editable only on the
    now-merged Artwork detail page, so a piece opened as a Masterpiece had no way
    to set the Bluesky image description.
    """
    try:
        raw = artwork_reader.read_raw_metadata(name)
    except FileNotFoundError:
        raise HTTPException(404, detail="Masterpiece not found")

    updates: dict = {}
    if "title" in body:
        updates["title"] = str(body.get("title") or "").strip()
    if "description" in body:
        updates["description"] = str(body.get("description") or "")
    if "rating" in body:
        r = str(body.get("rating") or "").strip().lower()
        if r and r not in _RATINGS:
            raise HTTPException(400, detail="rating must be general | mature | adult")
        updates["rating"] = r
    if "characters" in body and isinstance(body["characters"], list):
        updates["characters"] = [str(c).strip() for c in body["characters"] if str(c).strip()]
    if "alt_text" in body:
        updates["alt_text"] = str(body.get("alt_text") or "").strip()
    if "artist_status" in body:
        st = str(body.get("artist_status") or "").strip().lower()
        if st not in artwork_reader.ARTIST_STATUSES:
            raise HTTPException(400, detail="artist_status must be '' | own | unknown")
        updates["artist_status"] = st
    if "artist" in body:
        updates["artist"] = _artist_from_body(body.get("artist"))
    if "platform_tags" in body and isinstance(body["platform_tags"], dict):
        # An explicit per-platform list WINS OUTRIGHT over the canonical set
        # (build_artwork_package), so this is "post exactly these to this site".
        # Use it where a platform's budget bites and the automatic tail-trim
        # picks the wrong survivors; leave it unset everywhere else, because a
        # frozen override stops future canonical edits from ever reaching that
        # platform.
        tags = dict(raw.get("tags") or {})
        for platform, value in body["platform_tags"].items():
            code = str(platform).strip().lower()
            if not code or code in artwork_reader._RESERVED_TAG_KEYS:
                raise HTTPException(400, detail=f"{code!r} is not a platform")
            if value is None:
                tags.pop(code, None)                  # back to the automatic trim
            elif isinstance(value, list):
                tags[code] = [str(t).strip() for t in value if str(t).strip()]
            else:
                raise HTTPException(400, detail="each platform's tags must be a list or null")
        updates["tags"] = {**(updates.get("tags") or tags), **{
            k: v for k, v in tags.items() if k not in artwork_reader._RESERVED_TAG_KEYS}}
    if "tags" in body and isinstance(body["tags"], list):
        # Keep any real per-platform overrides from the RAW file (not the
        # cascaded ArtworkInfo), and replace the canonical set.
        #
        # ⚠ Writing `default` alone is NOT a replace. `_canonical_tag_list`
        # unions **core + default + auxiliary**, so on a work using the
        # core/auxiliary split a tag removed here stayed in `core` and came
        # straight back on the next read. Additions worked (they landed in
        # `default` and joined the union), which is exactly why this presented
        # as "saving works, removals don't stick".
        #
        # The submitted list IS the canonical set, so every canonical key has
        # to be rewritten from it. The core/auxiliary split is preserved rather
        # than flattened, because it is what keeps a heavily-tagged work
        # postable to FurAffinity at all (500-char budget, enforced by
        # rejection): a tag that was core stays core, and anything new joins
        # the auxiliary tail rather than displacing a curated priority tag.
        tags = dict(raw.get("tags") or {})
        submitted = [str(t).strip() for t in body["tags"] if str(t).strip()]
        old_core = {str(t).strip().lower() for t in (tags.get("core") or [])}

        if tags.get("core") or tags.get("auxiliary"):
            tags["core"] = [t for t in submitted if t.lower() in old_core]
            tags["auxiliary"] = [t for t in submitted if t.lower() not in old_core]
            # The legacy flat key would re-union whatever it still held.
            tags.pop("default", None)
        else:
            # A folder that never got the split keeps its legacy flat shape;
            # `default` alone is a true replace there.
            tags["default"] = submitted
        updates["tags"] = tags
    if not updates:
        raise HTTPException(400, detail="no editable fields provided")

    artwork_reader.save_artwork_metadata(name, updates)
    return {"status": "updated", "name": name}



@masterpieces_router.get("/{name}/tag-preview")
def tag_preview(name: str):
    """What each platform will actually receive as tags, and what it loses.

    The canonical set is meant to be rich and `core` is a priority ORDER, not the
    posted subset — so every platform is offered the whole list and trims from
    the tail. This makes that visible: without it, a budget quietly eating 40
    tags on DeviantArt looks identical to a work that was only tagged twice.
    """
    from posting import tag_budget

    try:
        raw = artwork_reader.read_raw_metadata(name)
    except FileNotFoundError:
        raise HTTPException(404, detail="Masterpiece not found")
    tags = dict(raw.get("tags") or {})
    canonical = artwork_reader._canonical_tag_list(tags)
    core_len = len(tags.get("core") or [])

    rows = []
    for code in _PREVIEW_PLATFORMS:
        override = tags.get(code)
        has_override = isinstance(override, list)
        source = [str(t) for t in override] if has_override else canonical
        # An override is posted verbatim — it is the user saying "exactly these"
        # — so it is reported as-is rather than re-trimmed behind their back.
        p = (tag_budget.preview(source, code) if not has_override else {
            "platform": code, "limit": tag_budget.describe(code),
            "sent": len(source), "total": len(source), "dropped": [],
            "chars": len(" ".join(source))})
        p["override"] = has_override
        rows.append(p)
    return {"name": name, "canonical": canonical, "core_count": core_len,
            "platforms": rows}

@masterpieces_router.post("/{name}/sync")
async def sync_masterpiece(name: str, body: dict | None = None):
    """Push the canonical record to every **editable** member (metadata only —
    never re-uploads the image). Members on non-editable platforms
    (Bluesky/e621/Itaku) are returned as skipped ``post-only``. Body (optional):
    {platforms?: [...]} to restrict the sync."""
    from posting import manager
    try:
        artwork_reader.load_artwork(name)
    except FileNotFoundError:
        raise HTTPException(404, detail="Masterpiece not found")
    platforms = (body or {}).get("platforms") or None
    # Same live guard as the publish endpoints. Sync creates nothing new but
    # OVERWRITES title/description/tags/rating on every live upload, which is
    # destructive to existing posts rather than additive — a stray call here
    # rewrites work that is already public.
    if not (body or {}).get("confirm_live"):
        raise HTTPException(
            400, detail="sync requires confirm_live=true (live-publish safety guard)")
    try:
        results = await manager.update_artwork(name, platforms=platforms)
    except Exception as e:
        logger.error("Masterpiece sync failed for %s: %s", name, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    synced = [r for r in results if r.get("success")]
    skipped = [r for r in results if r.get("skipped")]
    failed = [r for r in results if not r.get("success") and not r.get("skipped")]
    return {
        "status": "completed",
        "synced": len(synced),
        "skipped": len(skipped),
        "failed": len(failed),
        "results": results,
    }
