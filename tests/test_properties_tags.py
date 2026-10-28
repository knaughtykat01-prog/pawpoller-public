"""Property tests: tag formatting and per-site tag limits (PROPTESTS).

Generated tags, not hand-picked ones: whatever goes in, e621 is never sent a tag it
refuses, nothing is lost or invented by a site's limits, and every limit holds.
"""
from collections import Counter

import pytest

pytest.importorskip("hypothesis")
from hypothesis import given, strategies as st  # noqa: E402

from posting import e621_tags, tag_budget  # noqa: E402

word = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8)
any_text = st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=20)
tagish = st.one_of(any_text, st.lists(word, min_size=1, max_size=3).map(" ".join),
                   st.sampled_from(["artist:Some One", "character:x", "rating:s", "Red Fox", "tail__fluff",
                                    "ARTIST:Name", "artist:", "-bad", "café", "a​b", "rating:"]))


@given(any_text)
def test_e621_format_is_idempotent(t):
    once = e621_tags.format_tag(t)
    assert e621_tags.format_tag(once) == once


@given(st.lists(tagish, max_size=30))
def test_e621_prepare_sends_only_valid_tags_and_accounts_for_every_one(tags):
    sent, left = e621_tags.prepare(tags)
    names = []
    for s in sent:
        name = s.split(":", 1)[1] if s.startswith(("artist:", "character:")) else s
        assert e621_tags.rule_broken(name) is None, s
        assert not any(c.isspace() for c in s), s
        names.append(name)
    assert len(names) == len(set(names))
    for d in left:
        assert d["rule"]
    # every non-blank tag is sent, left out with a reason, or a repeat of one that was sent
    left_raw = Counter(d["tag"] for d in left)
    for t in (t for t in tags if str(t).strip()):
        if left_raw[str(t)]:
            left_raw[str(t)] -= 1
            continue
        low = str(t).strip().lower()
        pre = next((p for p in ("artist:", "character:") if low.startswith(p)), "")
        assert e621_tags.format_tag(str(t).strip()[len(pre):]) in names, t


@given(st.lists(st.text(alphabet="abc_xyz", min_size=1, max_size=70), max_size=120),
       st.sampled_from(sorted(tag_budget.BUDGETS) + ["ib", "e6"]))
def test_fit_report_loses_nothing_keeps_order_and_respects_limits(tags, plat):
    kept, dropped = tag_budget.fit_report(tags, plat)
    assert Counter(kept) + Counter(d["tag"] for d in dropped) == Counter(tags)
    assert all(d["rule"] for d in dropped)
    rest = iter(tags)
    assert all(k in rest for k in kept)                      # a subsequence: order kept
    b = tag_budget.budget_for(plat)
    if b.get("count"):
        assert len(kept) <= b["count"]
    if b.get("chars"):
        assert len(" ".join(kept)) <= b["chars"]
    if b.get("per_tag_chars"):
        assert all(len(k) <= b["per_tag_chars"] for k in kept)
    if not b:
        assert kept == tags and not dropped


@given(st.lists(st.sampled_from(["a", "b", "c" * 70, "d"]), max_size=60), st.sampled_from(sorted(tag_budget.BUDGETS)))
def test_preview_sent_plus_dropped_is_the_total(tags, plat):
    p = tag_budget.preview(tags, plat)
    assert p["sent"] + len(p["dropped"]) == p["total"]
