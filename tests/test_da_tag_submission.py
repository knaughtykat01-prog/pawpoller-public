"""DeviantArt tags have to arrive as an array (3.17.2).

3.17.0 got the right THIRTY tags into the package. They still did not reach the
deviation, because the client sent them like this:

```python
data["tags"] = ", ".join(tags[:30])
```

DA's schema types `tags` as an **array of strings**, each "Letters, numbers and
underscore only". A single comma-and-space-joined string is neither, so the
whole set was discarded and the deviation published untagged — verified on the
live post: correct title, no tags, empty description.

Two separate faults in one line:

  * **shape** — an array parameter sent as one scalar. The same file already
    indexes `galleryids[i]` and `mature_classification[i]`, and carries a
    comment explaining that the bare `[]` form keeps only the last value;
  * **content** — the catalogue is tagged booru-style, so `kii_(secondfur)`
    and `oral_(sex)` are ordinary tags that FA, Inkbunny and e621 accept and DA
    rejects. Sanitising follows `clients/fn/client.sanitize_tags`, which solved
    the identical problem for FurryNetwork.
"""
from __future__ import annotations

import pytest

from clients.da.client import sanitize_tags


# ── sanitising ───────────────────────────────────────────────────

def test_a_parenthetical_tag_survives_as_underscores():
    """`kii_(secondfur)` is the character tag on most of the catalogue. It
    must not be deleted, and it must not collapse to `kiisecondfur` — the
    underscore is what keeps it readable and searchable."""
    kept, dropped = sanitize_tags(["kii_(secondfur)"])
    assert kept == ["kii_secondfur"]
    assert dropped == []


@pytest.mark.parametrize("raw,clean", [
    ("oral_(sex)", "oral_sex"),
    ("dar_(penwright)", "dar_penwright"),
    ("white tiger", "white_tiger"),
    ("anthro", "anthro"),
    ("tiger-stripes", "tiger_stripes"),
    ("__leading", "leading"),
    ("trailing__", "trailing"),
])
def test_tags_are_reduced_to_letters_numbers_underscore(raw, clean):
    assert sanitize_tags([raw])[0] == [clean]


def test_runs_of_underscores_collapse():
    """`a_(b)_c` would otherwise become `a__b__c`, which reads as a typo."""
    assert sanitize_tags(["a_(b)_c"])[0] == ["a_b_c"]


def test_a_tag_with_nothing_usable_is_dropped_not_blanked():
    """`<3` reduces to nothing. An empty tag would be rejected by DA and means
    nothing on its own, so it is reported as dropped."""
    kept, dropped = sanitize_tags(["<3", "anthro"])
    assert kept == ["anthro"]
    assert dropped == ["<3"]


def test_sanitising_can_create_duplicates_and_they_are_removed():
    """`white tiger` and `white_tiger` are distinct upstream and identical
    afterwards; sending both would have DA reject the pair."""
    assert sanitize_tags(["white tiger", "white_tiger"])[0] == ["white_tiger"]


def test_every_kept_tag_satisfies_das_stated_rule():
    import re
    kept, _ = sanitize_tags(["kii_(secondfur)", "oral_(sex)", "white tiger",
                             "anthro", "<3", "tiger-stripes", "solo"])
    assert all(re.fullmatch(r"[A-Za-z0-9_]+", t) for t in kept)


def test_no_tags_is_not_an_error():
    assert sanitize_tags(None) == ([], [])
    assert sanitize_tags([]) == ([], [])


# ── the wire shape ───────────────────────────────────────────────

class _FakeResponse:
    status_code = 200

    def json(self):
        return {"itemid": 123, "deviationid": "DEV-1", "url": "https://x/y"}

    text = ""


class _FakeHttp:
    """Captures what the client would actually send."""

    def __init__(self):
        self.calls = []

    async def post(self, url, data=None, files=None, timeout=None, **kw):
        self.calls.append({"url": url, "data": dict(data or {})})
        return _FakeResponse()


@pytest.fixture()
def client(tmp_path):
    from clients.da.client import DAClient
    c = DAClient()
    c._http = _FakeHttp()
    return c


@pytest.mark.asyncio
async def test_submit_sends_tags_as_indexed_array_not_a_joined_string(client, tmp_path):
    """The regression, at the wire. `tags` as one comma-joined value is what
    published every deviation untagged."""
    f = tmp_path / "i.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    await client.oauth_stash_submit(
        str(f), title="T", artist_comments="c",
        tags=["anthro", "solo", "kii_(secondfur)"], access_token="tok")
    sent = client._http.calls[0]["data"]
    assert sent["tags[0]"] == "anthro"
    assert sent["tags[1]"] == "solo"
    assert sent["tags[2]"] == "kii_secondfur"
    assert "tags" not in sent, "the scalar form is what DA discarded"
    assert not any("," in v for k, v in sent.items() if k.startswith("tags"))


@pytest.mark.asyncio
async def test_submit_still_sends_the_description(client, tmp_path):
    """`artist_comments` is the ONLY description field DA exposes — neither
    /stash/publish nor /deviation/edit accepts one — so it must not be lost
    while fixing the tags beside it."""
    f = tmp_path / "i.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    await client.oauth_stash_submit(
        str(f), title="T", artist_comments="A real description",
        tags=["anthro"], access_token="tok")
    assert client._http.calls[0]["data"]["artist_comments"] == "A real description"


@pytest.mark.asyncio
async def test_publish_also_carries_the_tags(client):
    """`tags` is a deviation-level field on publish, not only stash metadata."""
    await client.oauth_stash_publish(123, tags=["anthro", "solo"], access_token="tok")
    sent = client._http.calls[0]["data"]
    assert sent["tags[0]"] == "anthro" and sent["tags[1]"] == "solo"


@pytest.mark.asyncio
async def test_publish_sets_the_no_ai_flag_by_default(client):
    """The project's stated position is that the artists it serves object to
    generative AI; DA exposes an explicit opt-out, so it is on by default."""
    await client.oauth_stash_publish(123, access_token="tok")
    assert client._http.calls[0]["data"]["noai"] == "1"


@pytest.mark.asyncio
async def test_is_ai_generated_is_never_claimed(client):
    await client.oauth_stash_publish(123, access_token="tok")
    assert "is_ai_generated" not in client._http.calls[0]["data"]


@pytest.mark.asyncio
async def test_the_thirty_tag_ceiling_still_holds(client, tmp_path):
    f = tmp_path / "i.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    await client.oauth_stash_submit(
        str(f), title="T", tags=[f"tag{i}" for i in range(40)], access_token="tok")
    sent = client._http.calls[0]["data"]
    assert len([k for k in sent if k.startswith("tags[")]) == 30


# ── the poster computes them once ────────────────────────────────

def test_the_poster_fits_tags_once_for_both_calls():
    """Submit and publish must receive the SAME list. Two `tag_budget.fit(...)`
    calls is the "one fact, several declarations" shape that caused 3.17.0."""
    import inspect
    from posting.platforms import deviantart
    src = inspect.getsource(deviantart.DeviantArtPoster.post)
    # Anchor on the branch keyword only — 3.34.0 moved the image-type tuple out
    # to the module constant _IMAGE_TYPES (so post/edit/replace_file cannot drift),
    # and an anchor carrying the literal '(' silently stopped matching.
    image_branch = src[src.index('if package.file_type in '):src.index('else:')]
    assert image_branch.count("tag_budget.fit(") == 1, (
        "fit once and pass the same list to submit and publish")
    assert image_branch.count("tags=da_tags") == 2
