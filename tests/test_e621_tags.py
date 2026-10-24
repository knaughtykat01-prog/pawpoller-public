"""e621 tag format and rules (055, E621TAGS).

One formatter shared by post, edit and the pre-check; every rule from e621ng's
``tag_name_validator.rb`` leaves the tag out with a plain reason instead of sinking the
upload.
"""
import pytest

from posting import e621_tags
from posting.artwork_reader import _e621_categorised
from posting.platforms.base import StoryUploadPackage
from posting.platforms.e621 import E621Poster


@pytest.mark.parametrize("raw,want", [
    ("Red Fox", "red_fox"),
    ("  tail__fluff ", "tail_fluff"),
    ("fur_", "fur"),
    ("_fur", "fur"),
    ("Big   Ears", "big_ears"),
])
def test_format(raw, want):
    assert e621_tags.format_tag(raw) == want


@pytest.mark.parametrize("tag,rule", [
    ("", "empty"),
    ("___", "empty"),
    ("-wolf", "can't start with -"),
    ("~wolf", "can't start with ~"),
    ("+wolf", "can't start with +"),
    ("`wolf", "can't start with `"),
    ("(wolf)", "can't start with ("),
    ("[wolf]", "can't start with ["),
    ("{wolf}", "can't start with {"),
    ("/wolf", "can't start with /"),
    (":wolf", "can't start with :"),
    ("wo*lf", "contains *"),
    ("wo,lf", "contains ,"),
    ("#wip", "contains #"),
    ("100%", "contains %"),
    ("wo$lf", "contains $"),
    ("wo\\lf", "contains \\"),
    ("wo--lf", "has a double -"),
    ("wo~~lf", "has a double ~"),
    ("wo​lf", "has a hidden character"),
    ("wo\x07lf", "has a hidden character"),
    ("café", "has accents or symbols e621 doesn't allow"),
    ("rating:explicit", "starts with rating:, which e621 reserves"),
    ("order:score", "starts with order:, which e621 reserves"),
    ("species:fox", "starts with species:, which e621 reserves"),
    ("oc:fox", "starts with oc:, which e621 reserves"),
    ("arttags:3", "starts with arttags:, which e621 reserves"),
])
def test_each_rule(tag, rule):
    if tag.strip("_"):
        assert e621_tags.rule_broken(tag) == rule
    else:
        assert e621_tags.rule_broken(tag) == "empty"


def test_good_tags_pass():
    for t in ("red_fox", "solo", "5_toes", "x-ray", "wolf_(character)", "2026"):
        assert e621_tags.rule_broken(t) is None, t


def test_prepare_formats_dedupes_and_reports():
    sent, left = e621_tags.prepare(["Red Fox", "red_fox", "café", "#wip", "solo", "  "])
    assert sent == ["red_fox", "solo"]
    assert left == [
        {"tag": "café", "rule": "has accents or symbols e621 doesn't allow"},
        {"tag": "#wip", "rule": "contains #"},
    ]


def test_own_prefixes_stay_and_count_once():
    sent, left = e621_tags.prepare(["artist:Second Fur", "second_fur", "character:Inkwolf"])
    assert sent == ["artist:second_fur", "character:inkwolf"]
    assert left == []


def test_categorised_formats_names_before_the_prefix():
    out = _e621_categorised(["Second Fur", "solo"], ["Second Fur"], "artist")
    assert out == ["artist:second_fur", "solo"]


def test_categorised_leaves_a_bad_name_unprefixed_for_prepare_to_report():
    out = _e621_categorised(["solo"], ["Pénwright"], "artist")
    assert out == ["Pénwright", "solo"]
    sent, left = e621_tags.prepare(out)
    assert sent == ["solo"] and left[0]["tag"] == "Pénwright"


def _pkg(tags, file_path="x.png"):
    return StoryUploadPackage(story_name="Sample Story", chapter_index=0, chapter_title="",
                              platform="e621", title="t", description="d", tags=tags,
                              rating="adult", file_path=file_path)


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def upload_post(self, **kw):
        self.calls.append(("upload", kw))
        return {"post_id": "1", "url": "https://e621.net/posts/1"}

    async def edit_post(self, post_id, **kw):
        self.calls.append(("edit", kw))
        return {"url": f"https://e621.net/posts/{post_id}"}


def _poster():
    p, c = E621Poster(), _FakeClient()

    async def _ensure():
        return c
    p._ensure_client = _ensure      # type: ignore[method-assign]
    return p, c


@pytest.mark.asyncio
async def test_post_sends_formatted_tags_and_names_left_out():
    p, c = _poster()
    r = await p.post(_pkg(["Red Fox", "Solo", "Big Ears", "Anthro", "café", "#wip"]))
    assert r.success
    assert c.calls[0][1]["tag_string"] == "red_fox solo big_ears anthro"
    assert [d["tag"] for d in r.extra["tags_left_out"]] == ["café", "#wip"]


@pytest.mark.asyncio
async def test_post_refused_before_sending_when_left_out_drops_below_floor():
    p, c = _poster()
    r = await p.post(_pkg(["solo", "anthro", "red fox", "café"]))
    assert not r.success and not c.calls
    assert "Add 1 more" in r.error and "café (has accents" in r.error


@pytest.mark.asyncio
async def test_edit_uses_the_same_formatting():
    p, c = _poster()
    r = await p.edit("9", _pkg(["Red Fox", "Solo", "Big Ears", "Anthro", "#wip"]))
    assert r.success
    assert c.calls[0][1]["tags"] == ["red_fox", "solo", "big_ears", "anthro"]
    assert r.extra["tags_left_out"] == [{"tag": "#wip", "rule": "contains #"}]


def test_validate_counts_prepared_tags():
    p = E621Poster()
    errs = p.validate(_pkg(["solo", "Solo", "SOLO", "anthro", "fox"], file_path=""))
    assert any("this piece has 3" in e for e in errs)
