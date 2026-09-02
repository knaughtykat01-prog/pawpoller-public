"""Separate a variant from its master + rename variants (2.189.0).

Two gaps in the 2.158 variants feature: merging a variant IN deleted the absorbed
folder with nothing to reconstitute it (a one-way door), and a variant's label
couldn't be edited at all — renaming meant DELETE + re-declare, and DELETE
re-keys members to primary, so a cosmetic edit silently threw away every
per-variant stat attribution. Spec: docs/specs/masterpiece_variant_split.md
"""
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import config
from database.db import get_connection
from database import masterpiece_queries as mq


def _png(colour=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (24, 24), colour).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_settings", {**config.get_settings(),
                                              "artwork_archive_path": str(tmp_path)}, raising=False)
    from posting import artwork_reader
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: tmp_path)
    from fastapi import FastAPI
    from routes.masterpieces_api import masterpieces_router
    app = FastAPI()
    app.include_router(masterpieces_router)
    return TestClient(app)


def _make_art(title="A Piece", colour=(200, 30, 30)):
    from posting import artwork_reader
    return artwork_reader.create_artwork(
        title=title, image_filename="orig.png", image_bytes=_png(colour),
        description="keep me", rating="adult", tags={"default": ["fox", "ref"]},
        characters=["Ki"])


def _declare(client, name, key, label):
    """Add a second image to the folder and declare it as a variant."""
    from posting import artwork_reader
    art = artwork_reader.load_artwork(name)
    (Path(art.path) / f"{key}.png").write_bytes(_png((10, 80, 200)))
    r = client.post(f"/api/masterpieces/{name}/variants",
                    json={"key": key, "image": f"{key}.png", "label": label})
    assert r.status_code == 200, r.text
    return f"{key}.png"


# ── Rename ──────────────────────────────────────────────────────────

def test_rename_label_only(client):
    name = _make_art()
    _declare(client, name, "nsfw", "NSFW")
    r = client.patch(f"/api/masterpieces/{name}/variants/nsfw", json={"label": "Spicy"})
    assert r.status_code == 200, r.text
    labels = {v["key"]: v["label"] for v in client.get(f"/api/masterpieces/{name}").json()["variants"]}
    assert labels["nsfw"] == "Spicy"


def test_rename_key_migrates_member_attribution(client):
    """The whole point: a rename must not cost per-variant stats."""
    name = _make_art()
    _declare(client, name, "nsfw", "NSFW")
    conn = get_connection()
    mq.add_member(conn, name, "fa", "111", variant_key="nsfw")
    conn.commit()
    conn.close()

    r = client.patch(f"/api/masterpieces/{name}/variants/nsfw",
                     json={"key": "spicy", "label": "Spicy"})
    assert r.status_code == 200, r.text
    assert r.json()["members_rekeyed"] == 1

    conn = get_connection()
    try:
        assert [m["submission_id"] for m in mq.get_members(conn, name, "spicy")] == ["111"]
        assert mq.get_members(conn, name, "nsfw") == []
    finally:
        conn.close()


def test_rename_key_collision_409(client):
    name = _make_art()
    _declare(client, name, "nsfw", "NSFW")
    _declare(client, name, "rough", "Rough")
    r = client.patch(f"/api/masterpieces/{name}/variants/rough", json={"key": "nsfw"})
    assert r.status_code == 409


def test_primary_key_cannot_change_but_label_can(client):
    """'' is the anchor the whole variant scheme keys off, so it's re-labelable
    but never re-keyable. An empty path segment can't route, so the handler is
    called directly."""
    from fastapi import HTTPException
    from routes import masterpieces_api as api
    from posting import artwork_reader
    name = _make_art()
    _declare(client, name, "nsfw", "NSFW")   # seeds the '' primary entry

    with pytest.raises(HTTPException) as ei:
        api.rename_variant(name, "", {"key": "nope"})
    assert ei.value.status_code == 400

    # ...but re-labelling the primary is fine.
    assert api.rename_variant(name, "", {"label": "Clean"})["status"] == "renamed"
    labels = {v["key"]: v["label"] for v in api._raw_variants(name)}
    assert labels[""] == "Clean"
    assert artwork_reader.load_artwork(name)  # record still loads


def test_rename_unknown_variant_404(client):
    name = _make_art()
    assert client.patch(f"/api/masterpieces/{name}/variants/ghost",
                        json={"label": "x"}).status_code == 404


# ── Separate from master ────────────────────────────────────────────

def test_split_creates_own_masterpiece_and_moves_members(client):
    name = _make_art(title="Midnight Snack")
    img = _declare(client, name, "nsfw", "NSFW")
    conn = get_connection()
    mq.add_member(conn, name, "fa", "111", variant_key="nsfw")
    mq.add_member(conn, name, "ib", "222")           # primary — must stay put
    conn.commit()
    conn.close()

    r = client.post(f"/api/masterpieces/{name}/variants/nsfw/split", json={})
    assert r.status_code == 200, r.text
    new_name = r.json()["new_name"]
    assert r.json()["members_moved"] == 1

    # Title derives from parent + label, so the variant-suggester still sees a family.
    from posting import artwork_reader
    new_art = artwork_reader.load_artwork(new_name)
    assert new_art.title == "Midnight Snack (NSFW)"
    assert (Path(new_art.path) / new_art.image).is_file()
    # Inherited canonical metadata.
    raw = artwork_reader.read_raw_metadata(new_name)
    assert raw["description"] == "keep me"
    assert raw["characters"] == ["Ki"]

    conn = get_connection()
    try:
        # The variant's member moved and is now the NEW record's primary.
        moved = mq.get_members(conn, new_name)
        assert [(m["platform"], m["submission_id"], m["variant_key"]) for m in moved] == [("fa", "111", "")]
        # The parent kept its own primary member and lost the variant's.
        assert [m["submission_id"] for m in mq.get_members(conn, name)] == ["222"]
    finally:
        conn.close()

    # Parent lost the entry AND the file.
    assert client.get(f"/api/masterpieces/{name}").json()["variants"] == []
    assert not (Path(artwork_reader.load_artwork(name).path) / img).exists()


def test_split_primary_refused(client):
    """The primary IS the master — separating it is meaningless."""
    from fastapi import HTTPException
    from routes import masterpieces_api as api
    name = _make_art()
    _declare(client, name, "nsfw", "NSFW")
    with pytest.raises(HTTPException) as ei:
        api.split_variant(name, "", {})
    assert ei.value.status_code == 400


def test_split_unknown_variant_404(client):
    name = _make_art()
    assert client.post(f"/api/masterpieces/{name}/variants/ghost/split",
                       json={}).status_code == 404


def test_split_honours_explicit_new_name(client):
    name = _make_art()
    _declare(client, name, "rough", "Rough")
    r = client.post(f"/api/masterpieces/{name}/variants/rough/split",
                    json={"new_name": "Totally Separate"})
    assert r.status_code == 200, r.text
    from posting import artwork_reader
    assert artwork_reader.load_artwork(r.json()["new_name"]).title == "Totally Separate"


def test_merge_uniquifies_a_colliding_key(client):
    """Regression (2.189.1): a merge must not dead-end on a derived-key clash.

    The key comes from the label the frontend slugifies — the user never picks
    one — and renaming a variant leaves its key stale, so a fresh "NSFW" label
    can collide with an old 'nsfw' key whose label now reads something else.
    Reported from live on Tricia_Reference_Sheet_*.
    """
    keep = _make_art(title="Tricia Clothed")
    _declare(client, keep, "nsfw", "SFW Nude")   # stale key, drifted label

    absorb = _make_art(title="Tricia NSFW", colour=(20, 200, 90))
    conn = get_connection()
    mq.add_member(conn, absorb, "fa", "777")
    conn.commit()
    conn.close()

    r = client.post("/api/masterpieces/merge-as-variant",
                    json={"keep": keep, "absorb": absorb, "key": "nsfw", "label": "NSFW"})
    assert r.status_code == 200, r.text
    assert r.json()["key"] == "nsfw-2"          # uniquified, not 409

    keys = {v["key"]: v["label"] for v in client.get(f"/api/masterpieces/{keep}").json()["variants"]}
    assert keys["nsfw"] == "SFW Nude"           # the existing one is untouched
    assert keys["nsfw-2"] == "NSFW"
    conn = get_connection()
    try:
        assert [m["submission_id"] for m in mq.get_members(conn, keep, "nsfw-2")] == ["777"]
    finally:
        conn.close()


def test_upload_variant_adds_a_labeled_render(client):
    """2.190.2: upload a fresh image straight in as a variant (no prior folder
    file, no other masterpiece to merge)."""
    name = _make_art(title="Ki")
    r = client.post(f"/api/masterpieces/{name}/variants/upload",
                    data={"label": "NSFW", "rating": "adult"},
                    files={"file": ("nsfw.png", _png((10, 80, 200)), "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["key"] == "nsfw"
    from posting import artwork_reader
    vs = {v["key"]: v for v in client.get(f"/api/masterpieces/{name}").json()["variants"]}
    # Seeds the primary + the uploaded one; the new file is in the folder.
    assert set(vs) == {"", "nsfw"}
    assert vs["nsfw"]["label"] == "NSFW" and vs["nsfw"]["rating"] == "adult"
    imgs = {f.name for f in artwork_reader.load_artwork(name).path.iterdir()
            if f.suffix.lower() in artwork_reader.IMAGE_EXTENSIONS}
    assert vs["nsfw"]["image"] in imgs


def test_upload_variant_derives_unique_key_from_label(client):
    name = _make_art(title="Ki")
    client.post(f"/api/masterpieces/{name}/variants/upload", data={"label": "Rough"},
                files={"file": ("a.png", _png(), "image/png")})
    r2 = client.post(f"/api/masterpieces/{name}/variants/upload", data={"label": "Rough"},
                     files={"file": ("b.png", _png((5, 5, 5)), "image/png")})
    assert r2.json()["key"] == "rough-2"          # uniquified, no collision


def test_upload_variant_rejects_non_image(client):
    name = _make_art(title="Ki")
    r = client.post(f"/api/masterpieces/{name}/variants/upload", data={"label": "x"},
                    files={"file": ("notes.txt", b"hi", "text/plain")})
    assert r.status_code == 415


def test_merge_carries_absorbs_own_variants(client):
    """2.189.2: folding a piece that ITSELF has variants must carry them all
    across — not copy only its hero and flatten its members (the old behaviour,
    which silently deleted the other variant images)."""
    keep = _make_art(title="Alpha")
    absorb = _make_art(title="Bravo", colour=(20, 200, 90))
    rough_img = _declare(client, absorb, "rough", "Rough")   # absorb's OWN variant
    conn = get_connection()
    mq.add_member(conn, absorb, "fa", "100")                    # absorb primary
    mq.add_member(conn, absorb, "ib", "200", variant_key="rough")  # absorb's rough
    conn.commit()
    conn.close()

    r = client.post("/api/masterpieces/merge-as-variant",
                    json={"keep": keep, "absorb": absorb, "key": "beta", "label": "Beta"})
    assert r.status_code == 200, r.text
    assert r.json()["variants_added"] == 2          # primary + rough, not 1

    from posting import artwork_reader
    art = artwork_reader.load_artwork(keep)
    vs = {v["key"]: v for v in client.get(f"/api/masterpieces/{keep}").json()["variants"]}
    # Alpha's own primary, Bravo's primary as 'beta', Bravo's rough as 'beta-rough'.
    assert set(vs) == {"", "beta", "beta-rough"}
    assert vs["beta"]["label"] == "Beta"
    assert vs["beta-rough"]["label"] == "Beta — Rough"
    # BOTH of Bravo's images were copied in (hero + rough), not just the hero.
    imgs = sorted(f.name for f in Path(art.path).iterdir()
                  if f.suffix.lower() in artwork_reader.IMAGE_EXTENSIONS)
    assert len(imgs) == 3                            # alpha hero + 2 carried
    assert vs["beta"]["image"] in imgs and vs["beta-rough"]["image"] in imgs

    conn = get_connection()
    try:
        # Members keep their per-variant attribution — NOT flattened onto one key.
        assert [m["submission_id"] for m in mq.get_members(conn, keep, "beta")] == ["100"]
        assert [m["submission_id"] for m in mq.get_members(conn, keep, "beta-rough")] == ["200"]
    finally:
        conn.close()
    # Absorb is gone.
    assert client.get(f"/api/masterpieces/{absorb}").status_code == 404


def test_carry_then_split_restores_a_sub_variant(client):
    """The carried sub-variant is a first-class variant, so Separate pulls it
    back out cleanly with its member."""
    keep = _make_art(title="Alpha")
    absorb = _make_art(title="Bravo", colour=(20, 200, 90))
    _declare(client, absorb, "rough", "Rough")
    conn = get_connection()
    mq.add_member(conn, absorb, "ib", "200", variant_key="rough")
    conn.commit()
    conn.close()
    client.post("/api/masterpieces/merge-as-variant",
                json={"keep": keep, "absorb": absorb, "key": "beta", "label": "Beta"})

    s = client.post(f"/api/masterpieces/{keep}/variants/beta-rough/split", json={})
    assert s.status_code == 200, s.text
    conn = get_connection()
    try:
        assert [m["submission_id"] for m in mq.get_members(conn, s.json()["new_name"])] == ["200"]
    finally:
        conn.close()


def test_merge_then_split_round_trips(client):
    """The property that makes folding safe: merge-as-variant is now undoable."""
    keep = _make_art(title="Keeper")
    absorb = _make_art(title="Absorbed", colour=(20, 200, 90))
    conn = get_connection()
    mq.add_member(conn, absorb, "fa", "999")
    conn.commit()
    conn.close()

    m = client.post("/api/masterpieces/merge-as-variant",
                    json={"keep": keep, "absorb": absorb, "key": "alt", "label": "Alt"})
    assert m.status_code == 200, m.text
    assert m.json()["members_moved"] == 1

    s = client.post(f"/api/masterpieces/{keep}/variants/alt/split", json={})
    assert s.status_code == 200, s.text
    restored = s.json()["new_name"]

    conn = get_connection()
    try:
        # The original site-link survived merge → split with its attribution.
        assert [(x["platform"], x["submission_id"], x["variant_key"])
                for x in mq.get_members(conn, restored)] == [("fa", "999", "")]
    finally:
        conn.close()
    # Keep is back to a plain piece.
    assert client.get(f"/api/masterpieces/{keep}").json()["variants"] == []
