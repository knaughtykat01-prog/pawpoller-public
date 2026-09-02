"""Core / auxiliary tag split on artwork metadata.

A work's `tags` dict is keyed by platform, with three reserved keys:
`core` (the 20-25 that matter, already in priority order), `auxiliary` (the
long tail) and the legacy flat `default`.

The split exists because the tag budget is per platform — FurAffinity REJECTS
a submission whose joined tag string exceeds 500 characters rather than
truncating it, so a heavily-tagged work could not be posted there at all.
Trimming from the tail means whatever survives is what was declared to matter.
"""
from __future__ import annotations

import json

import pytest

from posting import artwork_reader as ar


def _folder(tmp_path, tags: dict, name: str = "Piece"):
    d = tmp_path / name
    d.mkdir()
    (d / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "masterpiece.json").write_text(json.dumps({
        "title": "A Piece", "image": "img.png", "rating": "adult", "tags": tags,
    }), encoding="utf-8")
    return d


def test_canonical_list_is_core_then_auxiliary():
    tags = {"core": ["inkwolf", "tiger", "vixen"], "auxiliary": ["bed", "greyscale"]}
    assert ar._canonical_tag_list(tags) == [
        "inkwolf", "tiger", "vixen", "bed", "greyscale"]


def test_canonical_list_dedupes_across_the_two_lists():
    tags = {"core": ["tiger", "anthro"], "auxiliary": ["Tiger", "bed"]}
    assert ar._canonical_tag_list(tags) == ["tiger", "anthro", "bed"]


def test_legacy_default_still_reads():
    """Folders written before the split must keep working untouched."""
    assert ar._canonical_tag_list({"default": ["a", "b"]}) == ["a", "b"]


def test_legacy_default_and_auxiliary_combine():
    tags = {"default": ["a", "b"], "auxiliary": ["c"]}
    assert ar._canonical_tag_list(tags) == ["a", "b", "c"]


def test_fa_budget_trims_from_the_tail_and_fits_500_chars():
    tags = [f"tag{i:02d}_reasonably_long_name" for i in range(40)]
    fitted = ar.fit_tags_to_platform(tags, "fa")
    assert len(" ".join(fitted)) <= 500
    # Dropped from the END, so the core survives.
    assert fitted == tags[:len(fitted)]
    assert len(fitted) < len(tags)


def test_otw_budget_caps_tag_count():
    assert len(ar.fit_tags_to_platform(["x"] * 90, "ao3")) == 75
    assert len(ar.fit_tags_to_platform(["x"] * 90, "sqw")) == 75


def test_platform_without_a_budget_gets_everything():
    tags = [f"tag{i:02d}_reasonably_long_name" for i in range(40)]
    assert ar.fit_tags_to_platform(tags, "ib") == tags
    assert ar.fit_tags_to_platform(tags, "e621") == tags


def test_warns_when_the_budget_eats_into_core(caplog):
    """A platform too tight even for the core set is a tagging problem the user
    must be told about — never silently swallowed."""
    tags = [f"tag{i:02d}_quite_a_long_tag_name_here" for i in range(40)]
    ar.fit_tags_to_platform(tags, "fa", core_count=30)
    assert "cut into the core set" in caplog.text


def test_build_package_sends_core_first_and_within_budget(tmp_path, monkeypatch):
    core = [f"core{i:02d}_tag_name_padded" for i in range(18)]
    aux = [f"aux{i:02d}_tag_name_padded" for i in range(40)]
    _folder(tmp_path, {"core": core, "auxiliary": aux})
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path)

    art = ar.load_artwork("Piece")
    canonical = ar._canonical_tag_list(art.tags_by_platform)
    assert canonical[:18] == core

    fa = ar.fit_tags_to_platform(canonical, "fa", core_count=len(core))
    assert len(" ".join(fa)) <= 500
    # Every surviving tag is a prefix of the canonical order.
    assert fa == canonical[:len(fa)]


def test_variant_inherits_parent_tags_when_it_has_none():
    parent = {"core": ["a", "b"], "auxiliary": ["c"]}
    assert ar.variant_tags(parent, {"key": "sketch", "label": "Sketch"}) == parent


def test_variant_with_its_own_tags_overrides_the_parent():
    parent = {"core": ["explicit_a"], "auxiliary": ["explicit_b"]}
    variant = {"key": "sfw", "label": "SFW", "tags": {"core": ["clean_a"]}}
    assert ar.variant_tags(parent, variant) == {"core": ["clean_a"]}


def test_variant_with_an_empty_tags_dict_still_inherits():
    parent = {"core": ["a"]}
    assert ar.variant_tags(parent, {"label": "X", "tags": {}}) == parent


# ── The label->tags rules live in scripts/reorder_tags.py ────────────────────

def _script():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "reorder_tags.py"
    spec = importlib.util.spec_from_file_location("reorder_tags", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_nsfw_variant_is_not_treated_as_sfw():
    """`"sfw" in "nsfw"` is True — a substring test stripped the explicit tags
    off the very variants that needed them. Must match on token boundaries."""
    rt = _script()
    index = rt.load_index()
    parent = ["anthro", "solo", "male", "penis", "erection", "masturbation"]
    tags, note = rt.variant_tag_proposal(parent, "NSFW", index)
    assert tags == parent
    assert note == ""


def test_sfw_variant_strips_the_explicit_tiers():
    rt = _script()
    index = rt.load_index()
    parent = ["anthro", "solo", "male", "penis", "erection", "masturbation"]
    tags, note = rt.variant_tag_proposal(parent, "SFW", index)
    assert "penis" not in tags and "masturbation" not in tags
    assert "anthro" in tags and "solo" in tags
    assert "stripped" in note


def test_clean_is_line_art_not_safe_for_work():
    """In this catalogue Clean sits beside Lined/Base/Sketch/Messy, so it means
    clean line art — treating it as SFW wrongly stripped explicit tags."""
    rt = _script()
    index = rt.load_index()
    parent = ["anthro", "solo", "penis"]
    tags, note = rt.variant_tag_proposal(parent, "Clean", index)
    assert "penis" in tags
    assert note == ""
    assert "line_art" in tags


def test_label_implied_tags_are_added():
    rt = _script()
    index = rt.load_index()
    tags, _ = rt.variant_tag_proposal(["anthro"], "WIP GIF", index)
    assert "animated" in tags and "wip" in tags
    tags, _ = rt.variant_tag_proposal(["anthro"], "Sketch", index)
    assert "sketch" in tags


def test_build_package_posts_the_selected_variant(tmp_path, monkeypatch):
    """A variant is different content: its own image, its own rating, its own
    tags. Posting an SFW render under the parent's explicit tags would mis-tag
    it on anything that reads tags literally."""
    d = _folder(tmp_path, {"core": ["anthro", "solo", "penis"], "auxiliary": ["bed"]})
    (d / "sfw.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    meta = json.loads((d / "masterpiece.json").read_text(encoding="utf-8"))
    meta["variants"] = [{
        "key": "sfw", "label": "SFW", "image": "sfw.png", "rating": "general",
        "tags": {"core": ["anthro", "solo"], "auxiliary": ["bed"]},
    }]
    (d / "masterpiece.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path)

    art = ar.load_artwork("Piece")
    assert len(art.variants) == 1

    primary = ar.build_artwork_package(art, "ib")
    variant = ar.build_artwork_package(art, "ib", variant_key="sfw")

    assert "penis" in primary.tags and primary.rating == "adult"
    assert "penis" not in variant.tags and variant.rating == "general"
    assert variant.file_path.endswith("sfw.png")


def test_variant_without_own_tags_inherits_for_posting(tmp_path, monkeypatch):
    d = _folder(tmp_path, {"core": ["anthro", "penis"]})
    (d / "alt.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    meta = json.loads((d / "masterpiece.json").read_text(encoding="utf-8"))
    meta["variants"] = [{"key": "alt", "label": "Messy", "image": "alt.png"}]
    (d / "masterpiece.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path)

    art = ar.load_artwork("Piece")
    pkg = ar.build_artwork_package(art, "ib", variant_key="alt")
    assert pkg.tags == ["anthro", "penis"]      # inherited
    assert pkg.rating == "adult"                # falls back to the parent


def test_variant_description_falls_back_to_the_work():
    assert ar.variant_description("parent blurb", {"label": "Sketch"}) == "parent blurb"
    assert ar.variant_description("parent blurb", {"label": "X", "description": "   "}) == "parent blurb"


def test_variant_description_overrides_when_present():
    assert ar.variant_description("parent", {"description": "just the lines"}) == "just the lines"


def test_variant_description_beats_the_per_platform_description(tmp_path, monkeypatch):
    """A variant is different CONTENT, not a different audience. If a
    per-platform description won, an SFW render would go out captioned with the
    parent's explicit blurb — the same failure the variant tag split prevents."""
    d = _folder(tmp_path, {"core": ["anthro"]})
    (d / "sfw.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    meta = json.loads((d / "masterpiece.json").read_text(encoding="utf-8"))
    meta["description"] = "explicit parent blurb"
    meta["descriptions"] = {"fa": "explicit FA-specific blurb"}
    meta["variants"] = [{"key": "sfw", "label": "SFW", "image": "sfw.png",
                         "description": "clean version, nothing rude"}]
    (d / "masterpiece.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path)

    art = ar.load_artwork("Piece")
    parent = ar.build_artwork_package(art, "fa")
    variant = ar.build_artwork_package(art, "fa", variant_key="sfw")
    assert "explicit FA-specific blurb" in parent.description
    assert "clean version, nothing rude" in variant.description
    assert "explicit" not in variant.description


def test_description_override_still_outranks_a_variant(tmp_path, monkeypatch):
    d = _folder(tmp_path, {"core": ["anthro"]})
    (d / "v.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    meta = json.loads((d / "masterpiece.json").read_text(encoding="utf-8"))
    meta["variants"] = [{"key": "v", "label": "V", "image": "v.png",
                         "description": "variant blurb"}]
    (d / "masterpiece.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path)
    art = ar.load_artwork("Piece")
    pkg = ar.build_artwork_package(art, "ib", variant_key="v",
                                   description_override="explicit override")
    assert "explicit override" in pkg.description


def test_unknown_variant_key_is_an_error_not_a_silent_primary(tmp_path, monkeypatch):
    _folder(tmp_path, {"core": ["anthro"]})
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path)
    art = ar.load_artwork("Piece")
    with pytest.raises(ValueError, match="no variant with key"):
        ar.build_artwork_package(art, "ib", variant_key="nope")


def test_explicit_platform_override_still_wins(tmp_path):
    tags = {"core": ["a", "b"], "auxiliary": ["c"], "fa": ["only_this"]}
    # The reserved keys must not be mistaken for platform overrides.
    assert "fa" not in ar._RESERVED_TAG_KEYS
    assert ar._canonical_tag_list(tags) == ["a", "b", "c"]
    assert tags["fa"] == ["only_this"]
