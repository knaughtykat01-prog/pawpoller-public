"""FurryNetwork rejects the whole PATCH over one bad tag (3.9.10).

Prod, with the 422 body finally surfaced by 3.9.9:

    already uploaded (id 1896468) but metadata PATCH failed (HTTP 422):
    {"errors":{"tags":[null,null,null,null,null,null,null,["INVALID"],
                       null,null,["INVALID","TOO_SHORT"]]}}

Index 7 and index 10 of the canonical tag list for *Blows_a_kiss* are
``kii_(secondfur)`` and ``<3``. Every other tag was fine, and FN still failed
the entire submission — leaving the image uploaded as an untitled draft.

The rules encoded here were MEASURED against FN's own validator (see the table
in ``clients/fn/client.py``), not inferred: letters/digits/underscore/hyphen
only, three characters minimum.
"""
from __future__ import annotations

import pytest

from clients.fn.client import sanitize_tags


def test_the_two_tags_that_broke_prod():
    kept, dropped = sanitize_tags(
        ["anthro", "solo", "male", "white_tiger", "tiger", "felid", "mammal",
         "kii_(secondfur)", "blowing_kiss", "heart", "<3"])
    # The parenthetical artist tag survives with its meaning intact...
    assert "kii_secondfur" in kept
    assert "kii_(secondfur)" not in kept
    # ...and the one that cannot be rescued is dropped, not silently mangled.
    assert dropped == ["<3"]
    assert "<3" not in kept and "3" not in kept
    # Nothing else was touched.
    assert kept[:7] == ["anthro", "solo", "male", "white_tiger", "tiger",
                        "felid", "mammal"]


@pytest.mark.parametrize("tag", [
    "abc", "white_tiger", "heart-shape", "Upper_Case", "123",
])
def test_tags_fn_accepts_are_left_alone(tag):
    """Measured OK against the live validator — a sanitiser that rewrites these
    would be changing tags for no reason."""
    kept, dropped = sanitize_tags([tag])
    assert kept == [tag]
    assert dropped == []


@pytest.mark.parametrize("tag,expected", [
    ("kii_(secondfur)", "kii_secondfur"),   # measured: the result is accepted
    ("two words", "two_words"),
    ("tag.dot", "tag_dot"),
    ("tag+plus", "tag_plus"),
    ("tag'apos", "tag_apos"),
    ("tag:colon", "tag_colon"),
    ("tag/slash", "tag_slash"),
    ("tag!bang", "tag_bang"),
])
def test_illegal_characters_become_underscores(tag, expected):
    """Deleting them instead would run words together — 'two words' would become
    the unsearchable 'twowords'."""
    kept, _ = sanitize_tags([tag])
    assert kept == [expected]


@pytest.mark.parametrize("tag", ["a", "ab", "12", "<3", "!!", "", "  "])
def test_anything_under_three_characters_is_dropped(tag):
    kept, dropped = sanitize_tags([tag])
    assert kept == []
    assert dropped == [tag]


def test_leading_and_trailing_punctuation_does_not_leave_stubs():
    """'(tiger)' must not become '_tiger_' — FN allows underscores, so that
    would be *accepted* as a different, wrong tag."""
    kept, _ = sanitize_tags(["(tiger)", "--felid--", "_mammal_"])
    assert kept == ["tiger", "felid", "mammal"]


def test_collapsing_never_produces_a_double_underscore():
    kept, _ = sanitize_tags(["a (b) c"])
    assert kept == ["a_b_c"]


def test_sanitising_can_create_duplicates_and_they_are_removed():
    """'two words' and 'two_words' are distinct upstream but identical to FN;
    sending both would be a duplicate, not two tags."""
    kept, _ = sanitize_tags(["two words", "two_words", "TWO_WORDS"])
    assert kept == ["two_words"]


def test_order_is_preserved():
    """Tags arrive core-first and that order is meaningful everywhere else in
    the app — the sanitiser must not reshuffle it."""
    kept, _ = sanitize_tags(["zebra", "anthro", "<3", "male"])
    assert kept == ["zebra", "anthro", "male"]


def test_empty_input_is_not_an_error():
    assert sanitize_tags([]) == ([], [])
    assert sanitize_tags(None) == ([], [])


def test_the_uploader_actually_uses_it():
    """A sanitiser nothing calls is the bug still shipping."""
    import inspect

    from clients.fn import client as fn_client

    src = inspect.getsource(fn_client.FnClient.upload_artwork)
    assert "sanitize_tags" in src
    assert '"tags": fn_tags' in src, "the PATCH must send the sanitised list"


def test_dropped_tags_are_reported_not_swallowed():
    """Silently losing a tag is its own bug — the user has to be able to find
    out why '<3' never appeared on FurryNetwork."""
    import inspect

    from clients.fn import client as fn_client

    src = inspect.getsource(fn_client.FnClient.upload_artwork)
    assert "logger.warning" in src and "dropped" in src
