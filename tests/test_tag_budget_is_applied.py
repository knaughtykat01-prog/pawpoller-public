"""The tag budget must reach the wire, not just the preview (3.17.0).

3.12.0 shipped the budget table, the fitter, and a per-platform preview, and its
changelog said the posting side "was already how `build_artwork_package`
worked". It was not. `load_artwork` cascaded the canonical list into every
poster's key with `setdefault`, and `build_artwork_package` reads a PRESENT
per-platform key as the user saying "post exactly these" — so every platform
looked hand-overridden, `fit_tags_to_platform` never ran, and its only
production caller was dead code for four days.

What made it survive review is that the two halves disagreed *silently*:

  * the preview endpoint reads `read_raw_metadata` — the raw JSON — so it kept
    reporting the correct trim ("DeviantArt: 30 of 38 — 8 cut");
  * the poster read the cascaded `ArtworkInfo`, so it sent all 38.

One fact, two readers, no test tying them together — the same shape as 3.12.1,
3.12.2 and 3.13.0. The tests below are that tie: they assert against the wire,
and they assert the two readers agree.

Only DeviantArt ever complained, because it is the only platform whose
`validate()` carries an upper bound. FurAffinity's 500-character keyword string
and SoFurry's 97-tag cap were being exceeded in silence.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from posting import artwork_reader, tag_budget


@pytest.fixture()
def client():
    from dashboard import app
    return TestClient(app)


def _make(tmp_path, monkeypatch, tags: dict, name: str = "BudgetTarget") -> str:
    root = tmp_path / "artwork"
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "image.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (folder / "masterpiece.json").write_text(json.dumps({
        "title": "Budget Target", "rating": "general", "image": "image.jpg",
        "tags": tags,
    }), encoding="utf-8")
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: root)
    return name


@pytest.fixture()
def work(tmp_path, monkeypatch):
    """50 canonical tags — over DA's 30, under SoFurry's 97."""
    return _make(tmp_path, monkeypatch, {
        "core": [f"core{i}" for i in range(10)],
        "auxiliary": [f"aux{i}" for i in range(40)],
    })


def _package_tags(name: str, platform: str) -> list[str]:
    art = artwork_reader.load_artwork(name)
    return list(artwork_reader.build_artwork_package(art, platform).tags)


# ── the bug, stated directly ─────────────────────────────────────

def test_deviantart_receives_thirty_not_the_whole_set(work):
    """The regression, at the only place that matters: the package handed to
    the poster. Asserting `fit_tags_to_platform(50, 'da') == 30` — which the
    3.12.0 tests did — passes happily while nothing calls it."""
    assert len(_package_tags(work, "da")) == 30


def test_the_canonical_list_is_not_silently_declared_an_override(work):
    """A platform with no key in `tags` has no override, so it must be fitted.
    The cascade made `source.get('da')` non-None and stole this branch."""
    art = artwork_reader.load_artwork(work)
    assert "da" not in art.tags_by_platform, (
        "load_artwork must not invent per-platform keys — a present key means "
        "'post exactly these' to build_artwork_package")


@pytest.mark.parametrize("platform", ["ib", "fa", "ws", "sf", "da", "ik",
                                      "bsky", "e621", "fn"])
def test_no_platform_is_sent_more_than_its_budget_allows(work, platform):
    """Generalised past DeviantArt: the platforms with no `validate()` bound
    were failing silently, which is worse than failing loudly."""
    sent = _package_tags(work, platform)
    assert sent == tag_budget.fit(sent, platform), (
        f"{platform} was handed tags its own budget would trim")


def test_furaffinitys_limit_is_characters_and_is_respected(tmp_path, monkeypatch):
    """FA's cap is a 500-character keyword STRING, not a count — the failure
    mode nothing would have reported, since FA's validate has no tag bound."""
    work = _make(tmp_path, monkeypatch, {
        "core": [f"core_tag_number_{i:03d}" for i in range(40)],
        "auxiliary": [f"auxiliary_tag_number_{i:03d}" for i in range(40)],
    }, name="LongTags")
    sent = _package_tags(work, "fa")
    assert len(" ".join(sent)) <= 500
    assert len(sent) < 80, "something must have been trimmed"


# ── the two readers must agree ───────────────────────────────────

@pytest.mark.parametrize("platform", ["fa", "ib", "e621", "sf", "ws", "da",
                                      "fn", "ik"])
def test_the_preview_promises_exactly_what_the_package_delivers(
        client, work, platform):
    """The property that would have caught this on day one. The preview reads
    the raw JSON and the package reads ArtworkInfo; while those two disagree,
    the UI is lying about what is being posted."""
    rows = {p["platform"]: p for p in
            client.get(f"/api/masterpieces/{work}/tag-preview").json()["platforms"]}
    assert len(_package_tags(work, platform)) == rows[platform]["sent"]


# ── a real override still wins ───────────────────────────────────

def test_a_hand_written_override_is_still_posted_verbatim(tmp_path, monkeypatch):
    """The cascade has to go without taking overrides with it: a key the USER
    put in `tags` remains "post exactly these", untrimmed by the fitter."""
    work = _make(tmp_path, monkeypatch, {
        "core": [f"core{i}" for i in range(10)],
        "auxiliary": [f"aux{i}" for i in range(40)],
        "da": ["chosen_a", "chosen_b", "chosen_c"],
    }, name="Overridden")
    assert _package_tags(work, "da") == ["chosen_a", "chosen_b", "chosen_c"]


def test_an_override_on_one_platform_does_not_affect_another(tmp_path, monkeypatch):
    work = _make(tmp_path, monkeypatch, {
        "core": [f"core{i}" for i in range(10)],
        "auxiliary": [f"aux{i}" for i in range(40)],
        "da": ["chosen_a"],
    }, name="OverriddenOne")
    assert len(_package_tags(work, "ib")) == 50


# ── the loud symptom ─────────────────────────────────────────────

def test_deviantart_validate_passes_a_richly_tagged_piece(work):
    """The user-visible failure: a 38-tag piece failed `validate()` and never
    reached `upload()`, which fits to 30 and would have succeeded. Validation
    now measures the list the uploader actually sends."""
    from posting.platforms.deviantart import DeviantArtPoster
    art = artwork_reader.load_artwork(work)
    package = artwork_reader.build_artwork_package(art, "da")
    assert DeviantArtPoster().validate(package) == []


def test_deviantart_validate_still_catches_an_over_long_title(work):
    from posting.platforms.deviantart import DeviantArtPoster
    art = artwork_reader.load_artwork(work)
    package = artwork_reader.build_artwork_package(art, "da")
    package.title = "x" * 51
    assert any("title" in e for e in DeviantArtPoster().validate(package))
