"""Unit tests for the generic artwork importer's pure helpers
(posting.artwork_importer). The full import_artwork() path needs DB + network +
filesystem, so it's exercised live, not here; these cover the mapping + guard
logic that makes one importer work safely across platforms.
"""
from posting.artwork_importer import (
    norm_rating, image_url, pick_ext, is_image, parse_tags, media_url_list,
)


def test_norm_rating_maps_per_platform_terms():
    assert norm_rating("General") == "general"    # FA
    assert norm_rating("Clean") == "general"      # SoFurry
    assert norm_rating("explicit") == "adult"     # Weasyl
    assert norm_rating("Adult") == "adult"        # FA / SF
    assert norm_rating("Mature") == "mature"
    assert norm_rating("") == ""
    assert norm_rating(None) == ""


def test_image_url_prefers_full_res_and_ignores_page_url():
    assert image_url({"download_url": "full", "thumbnail_url": "t"}) == "full"  # FA
    assert image_url({"media_url": "full", "thumbnail_url": "t"}) == "full"     # Weasyl
    assert image_url({"thumbnail_url": "t"}) == "t"                            # SF / DA / IK
    assert image_url({"thumb_url": "tb"}) == "tb"                              # Inkbunny thumbnail
    # A generic page `url` (Inkbunny stores the submission page here) is NOT an image source.
    assert image_url({"url": "https://inkbunny.net/s/123"}) == ""
    assert image_url({}) == ""


def test_pick_ext_from_url_magic_then_content_type():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    assert pick_ext("https://x/y/file.jpg", "", b"") == ".jpg"        # from URL
    assert pick_ext("https://x/y/noext", "image/png", b"") == ".png"  # from Content-Type
    assert pick_ext("https://x/y/noext", "", png) == ".png"          # from magic bytes
    assert pick_ext("https://x/y/noext", "", b"") == ".png"          # default


def test_is_image_guard_rejects_html():
    assert is_image("image/jpeg", b"")                       # by header
    assert is_image("", b"\xff\xd8\xff\xe0\x00\x10JFIF")     # by JPEG magic bytes
    assert not is_image("text/html", b"<!DOCTYPE html><html>")  # HTML rejected
    assert not is_image("", b"not an image at all")


def test_parse_tags_handles_json_list_csv_and_empty():
    assert parse_tags('["wolf", "anthro"]') == ["wolf", "anthro"]
    assert parse_tags(["a", "b"]) == ["a", "b"]
    assert parse_tags("a, b, c") == ["a", "b", "c"]
    assert parse_tags("") == []
    assert parse_tags(None) == []


def test_media_url_list_multi_image_single_and_fallback():
    # Multi-image post: JSON array (as stored) → every full-res URL, in order.
    assert media_url_list({"media_urls": '["a.jpg", "b.jpg", "c.jpg"]'}) == [
        "a.jpg", "b.jpg", "c.jpg"]
    # Already a list (pre-serialisation) works too.
    assert media_url_list({"media_urls": ["a.jpg", "b.jpg"]}) == ["a.jpg", "b.jpg"]
    # De-duped, order preserved.
    assert media_url_list({"media_urls": '["a.jpg", "a.jpg", "b.jpg"]'}) == ["a.jpg", "b.jpg"]
    # Empty media_urls → fall back to the single best URL (older/single-image rows).
    assert media_url_list({"media_urls": "", "thumbnail_url": "t.jpg"}) == ["t.jpg"]
    assert media_url_list({"download_url": "full.jpg"}) == ["full.jpg"]
    # Nothing at all → empty (import then raises "no image URL").
    assert media_url_list({}) == []
    # Blank/whitespace entries are dropped.
    assert media_url_list({"media_urls": '["a.jpg", "", "  "]'}) == ["a.jpg"]
