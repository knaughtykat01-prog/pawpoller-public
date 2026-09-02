"""Unified art detail page (2.193.0).

Clicking a piece from Masterpieces and clicking the same piece from Artwork used
to open two different pages over ONE record (masterpiece.json is a back-compat
superset of artwork.json; both endpoints load through artwork_reader.load_artwork;
there is no discriminator in the data). The renderers merged onto the Masterpiece
one, with both routes kept live.

These cover the backend half of that merge:
  * the two detail payloads are interchangeable (variant rollups, hero-first
    images, and the Artwork-only fields now served by the masterpiece route)
  * a variant tile carries a '?v=<key>' route so it opens on ITS render rather
    than the master hero — the original complaint
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from posting import artwork_reader
from routes.artwork_api import artwork_router
from routes.masterpieces_api import masterpieces_router


@pytest.fixture
def artwork_archive(tmp_path, monkeypatch):
    arch = tmp_path / "Artwork"
    arch.mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: arch)
    return arch


def _piece_with_variant(arch):
    """A piece whose hero sorts AFTER its alt, so hero-first ordering is testable."""
    name = artwork_reader.create_artwork(
        title="Ki Ref", image_filename="zeta.png", image_bytes=b"hero", rating="adult")
    d = arch / name
    (d / "alpha.png").write_bytes(b"nsfw")
    artwork_reader.save_artwork_metadata(name, {"variants": [
        {"key": "", "label": "SFW", "image": "zeta.png", "rating": ""},
        {"key": "nsfw", "label": "NSFW", "image": "alpha.png", "rating": "adult"},
    ]})
    return name


def _client(*routers):
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    return TestClient(app)


# ── payload alignment ─────────────────────────────────────────

def test_artwork_detail_orders_images_hero_first(artwork_archive):
    """One renderer paints the strip in payload order, so the hero must lead.

    'alpha.png' sorts before the hero 'zeta.png', so plain sorted() put the hero
    in the middle of its own gallery.
    """
    name = _piece_with_variant(artwork_archive)
    d = _client(artwork_router).get(f"/api/artwork/images/{name}").json()
    assert d["images"][0] == "zeta.png"
    assert set(d["images"]) == {"zeta.png", "alpha.png"}


def test_artwork_detail_variants_carry_rollups(artwork_archive):
    """Variants gain totals + member_count, matching the masterpiece payload."""
    name = _piece_with_variant(artwork_archive)
    d = _client(artwork_router).get(f"/api/artwork/images/{name}").json()
    for v in d["variants"]:
        assert "totals" in v, f"variant {v['key']!r} has no totals"
        assert "member_count" in v
        assert v["member_count"] == 0        # nothing linked yet
        assert v["totals"]["views"] == 0


def test_masterpiece_detail_serves_the_artwork_only_fields(artwork_archive):
    """The four things only the Artwork page had must come from this payload too,
    or the unified page would need a second request to render publish/alt-text."""
    name = _piece_with_variant(artwork_archive)
    d = _client(masterpieces_router).get(f"/api/masterpieces/{name}").json()
    for key in ("alt_text", "publications", "titles", "descriptions", "categories"):
        assert key in d, f"masterpiece payload is missing {key}"
    assert d["publications"] == []
    # And it keeps everything it already had.
    for key in ("variants", "images", "locations", "totals", "canonical_tags"):
        assert key in d


def test_both_detail_payloads_agree_on_variants_and_images(artwork_archive):
    """Either route must be able to feed the same renderer."""
    name = _piece_with_variant(artwork_archive)
    c = _client(artwork_router, masterpieces_router)
    a = c.get(f"/api/artwork/images/{name}").json()
    m = c.get(f"/api/masterpieces/{name}").json()

    assert a["images"] == m["images"]                       # both hero-first
    assert [v["key"] for v in a["variants"]] == [v["key"] for v in m["variants"]]
    assert [v["member_count"] for v in a["variants"]] == \
           [v["member_count"] for v in m["variants"]]


# ── alt text on the canonical record ──────────────────────────

def test_masterpiece_patch_accepts_alt_text(artwork_archive):
    """Previously editable only on the Artwork page, so a piece opened as a
    Masterpiece had no way to set the Bluesky image description."""
    name = _piece_with_variant(artwork_archive)
    c = _client(artwork_router, masterpieces_router)

    r = c.patch(f"/api/masterpieces/{name}",
                json={"alt_text": "A grey wolf in a red jacket grins."})
    assert r.status_code == 200
    assert c.get(f"/api/masterpieces/{name}").json()["alt_text"] == \
        "A grey wolf in a red jacket grins."
    # Visible on the other route too — same record.
    assert c.get(f"/api/artwork/images/{name}").json()["alt_text"] == \
        "A grey wolf in a red jacket grins."


def test_masterpiece_patch_alt_text_leaves_other_fields_alone(artwork_archive):
    name = _piece_with_variant(artwork_archive)
    c = _client(masterpieces_router)
    c.patch(f"/api/masterpieces/{name}", json={"alt_text": "described"})
    d = c.get(f"/api/masterpieces/{name}").json()
    assert d["title"] == "Ki Ref"
    assert d["rating"] == "adult"
    assert [v["key"] for v in d["variants"]] == ["", "nsfw"]


# ── the variant deep-link ─────────────────────────────────────

def test_variant_tile_route_selects_that_variant(artwork_archive, monkeypatch):
    """The original complaint: a variant tile dropped its key and opened the
    master hero. Each tile now carries '?v=<key>'."""
    import dashboard
    monkeypatch.setattr(dashboard.config, "is_dashboard_auth_required", lambda: False)
    c = TestClient(dashboard.app)

    name = _piece_with_variant(artwork_archive)
    works = c.get("/api/works", params={"type": "artwork"}).json()["works"]
    w = next(x for x in works if x["name"] == name)

    # Primary is the master card; only the non-primary variant gets a tile.
    assert [v["label"] for v in w["variants"]] == ["NSFW"]
    tile = w["variants"][0]
    assert tile["detail_route"].endswith("?v=nsfw")
    # The tile route must target the same piece, not a different name.
    assert tile["detail_route"].startswith(w["detail_route"])
    # The master card route stays selector-free, so it still opens the hero.
    assert "?v=" not in w["detail_route"]
