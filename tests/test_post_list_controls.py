"""Post lists get a Type filter, a date sort and a Type column (backlog SUBFILTER).

The artwork pages have search + rating + type; the post pages had a search box alone,
although every row already carries a `content_type` and a date. One shared binder now
serves all six (X, Bluesky, Mastodon, Threads, Instagram, Tumblr) rather than six copies
of the same controls.

Verified in a browser against the real app.js: the dropdown is built from the types
actually present (four rows of post/reply/quote produced "All types · Post · Quote ·
Reply", no Repost), filtering by type returned the two posts newest-first, the search box
still filtered, and Oldest first reversed the order.
"""
from __future__ import annotations

import re

import pytest

APP = open("frontend/js/app.js", encoding="utf-8").read()
COMPONENTS = open("frontend/js/components.js", encoding="utf-8").read()

POST_PLATFORMS = ["BSKY", "MAST", "TUM", "THR", "IG", "TW"]


class TestTheSharedControl:
    def test_the_dropdown_is_built_from_the_rows(self):
        body = APP[APP.index("_bindPostSearch(allRows, gridRenderer, opts)"):][:2600]
        assert "new Set((allRows || []).map(r => r.content_type)" in body
        assert "types.length > 1" in body, "one type is not worth a filter"

    def test_it_offers_newest_and_oldest(self):
        body = APP[APP.index("_bindPostSearch(allRows, gridRenderer, opts)"):][:2600]
        assert "'Newest first'" in body and "'Oldest first'" in body

    def test_the_labels_are_escaped(self):
        """Type codes come from polled data, and they are put into option markup."""
        body = APP[APP.index("_bindPostSearch(allRows, gridRenderer, opts)"):][:2600]
        assert body.count("Utils.escapeHtml") >= 2

    def test_it_needs_no_extra_markup_on_the_page(self):
        body = APP[APP.index("_bindPostSearch(allRows, gridRenderer, opts)"):][:2600]
        assert "insertAdjacentElement('afterend', sel)" in body


@pytest.mark.parametrize("code", POST_PLATFORMS)
def test_every_post_page_uses_it(code):
    binder = re.search(r"_bind%sSearch\(allSubmissions, gridRenderer\) \{(.*?)\n    \}," % code,
                       APP, re.S)
    assert binder, f"{code} binder missing"
    assert "this._bindPostSearch(" in binder.group(1), f"{code} still has its own copy"


def test_bluesky_finally_shows_the_post_type():
    """Every other post table had a Type column; Bluesky's did not."""
    table = COMPONENTS[COMPONENTS.index("bskySubmissionsTable("):][:2000]
    assert 'data-label="Type"' in table
    assert 'BSKY_TYPE_LABELS[s.content_type]' in table
    assert '<th data-sort="content_type">Type</th>' in table


def test_no_post_table_is_missing_its_date():
    for code in ("bsky", "tw", "ig", "mast", "tum", "thr"):
        table = COMPONENTS[COMPONENTS.index(f"{code}SubmissionsTable("):][:2200]
        assert "formatDate" in table, f"{code} shows no date"
