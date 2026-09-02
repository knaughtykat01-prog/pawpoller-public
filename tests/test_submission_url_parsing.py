"""Pasted submission URL → (platform, submission_id) (3.14.0).

The hand-link picker offers only *discovered* submissions — posts with no
publication row. A post PawPoller already recorded under its own site title is
therefore invisible to it, which is exactly the case a person hits: "I have the
link to this post, why can't I attach it to the piece". FA 37056160 on the
SecondFur account is the real example: polled, stored, given a publication row
under its FA title "Embarrassed", and never linked to *Growing Into It*.

The parser is DERIVED from `PLATFORM_TABLES[p]["url_template"]` — the same table
the forward direction already uses — rather than restating those shapes. A
second hand-written list is the failure this session hit three times over
(3.12.1, 3.12.2, 3.13.0): one fact, several declarations, no check.
"""
from __future__ import annotations

import pytest

from posting.submission_urls import (SUPPORTED_PLATFORMS, candidates_for,
                                     parse_submission_url)
from posting.sync import PLATFORM_TABLES


# ── derived, not restated ────────────────────────────────────────

def test_every_platform_with_a_template_is_parseable():
    """The whole point of deriving: adding a platform to PLATFORM_TABLES must
    give it URL parsing for free, with nothing else to remember."""
    expected = {p for p, cfg in PLATFORM_TABLES.items()
                if "{id}" in (cfg.get("url_template") or "")}
    assert set(SUPPORTED_PLATFORMS) == expected


@pytest.mark.parametrize("platform", [
    p for p, cfg in PLATFORM_TABLES.items() if "{id}" in (cfg.get("url_template") or "")
])
def test_a_templates_own_output_round_trips(platform):
    """Generate a URL the way the app does, parse it back, get the same pair.
    This is the property that keeps the two directions honest."""
    url = PLATFORM_TABLES[platform]["url_template"].format(id="12345")
    assert (platform, "12345") in candidates_for(url)


# ── the real-world shapes ────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.furaffinity.net/view/37056160/", ("fa", "37056160")),
    ("https://furaffinity.net/view/37056160", ("fa", "37056160")),
    ("http://www.furaffinity.net/view/37056160/", ("fa", "37056160")),
    ("https://www.furaffinity.net/full/37056160/", ("fa", "37056160")),
    ("https://e621.net/posts/1955656", ("e621", "1955656")),
    ("https://e621.net/post/show/1955656", ("e621", "1955656")),
    ("https://inkbunny.net/s/123456", ("ib", "123456")),
    ("https://sofurry.com/s/1YAApVD1", ("sf", "1YAApVD1")),
    ("https://sofurry.com/view/998877", ("sf", "998877")),
    ("https://www.weasyl.com/submission/55555", ("ws", "55555")),
    ("https://itaku.ee/images/4242", ("ik", "4242")),
    ("https://x.com/kii/status/2065580908430909818", ("tw", "2065580908430909818")),
    ("https://twitter.com/kii/status/2065580908430909818", ("tw", "2065580908430909818")),
])
def test_known_url_shapes(url, expected):
    assert parse_submission_url(url) == expected


def test_the_www_prefix_is_optional_in_both_directions():
    """Regression: the first implementation inserted `(?:www\\.)?` and THEN
    stripped the literal `www.` from the template — which ate the `www.` inside
    the group it had just added, so a bare `furaffinity.net/...` stopped
    matching while the www form kept working."""
    with_www = parse_submission_url("https://www.furaffinity.net/view/999/")
    without = parse_submission_url("https://furaffinity.net/view/999/")
    assert with_www == without == ("fa", "999")


def test_a_handle_segment_in_a_template_matches_any_handle():
    """bsky's template is `profile/_/post/{id}` — the `_` stands in for a handle
    the poller does not know, so a real URL has something else there."""
    assert parse_submission_url(
        "https://bsky.app/profile/kii.bsky.social/post/3mq6vufw7pn26"
    ) == ("bsky", "3mq6vufw7pn26")


def test_deviantart_takes_the_numeric_id_off_the_public_url():
    """DA's public URL is `/{user}/art/{slug}-{digits}` and those digits are the
    id `da_submissions` keys on — which is the half of the DAID mismatch that a
    pasted link can actually supply."""
    assert parse_submission_url(
        "https://www.deviantart.com/secondfur/art/Some-Title-1370480056"
    ) == ("da", "1370480056")


# ── refusing to guess ────────────────────────────────────────────

@pytest.mark.parametrize("junk", [
    "", "   ", "not a url", "https://example.com/whatever",
    "https://www.furaffinity.net/user/secondfur",   # a profile, not a submission
    "https://e621.net/",
])
def test_a_non_submission_url_resolves_to_nothing(junk):
    assert candidates_for(junk) == []
    assert parse_submission_url(junk) is None


def test_a_bare_id_is_not_guessed_at():
    """`37056160` alone cannot name its own platform. Returning a guess would
    link the wrong post on the wrong site; the caller offers a picker instead."""
    assert candidates_for("37056160") == []


def test_candidates_are_deduplicated_and_ordered():
    """A URL matching both an alternate and a template pattern yields one entry,
    alternates first because they are the narrower match."""
    got = candidates_for("https://e621.net/post/show/1955656")
    assert got == [("e621", "1955656")]


def test_a_profile_url_is_not_mistaken_for_a_submission():
    """`furaffinity.net/user/{name}` has the same shape as `/view/{id}` — the
    pattern has to be anchored to the right path segment."""
    assert parse_submission_url("https://www.furaffinity.net/user/secondfur") is None
