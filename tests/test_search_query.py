"""Field-scoped Library search (3.14.0).

The shelf search was a substring match on title + name. Fine for "find the
piece called Blep", useless for "white tiger pieces by Inkwolf that aren't
posted yet" — the question you actually have once the catalogue passes a
hundred works and each carries thirty tags.

The grammar lives in `frontend/js/search_query.js` because the Library filters
a cached list client-side; putting it in Python would mean a round trip per
keystroke, and writing it in BOTH would be the "one fact, several declarations"
trap this session has already hit three times (3.12.1, 3.12.2, 3.13.0).

So these tests drive the real module through **node**, rather than asserting on
its source text. Grepping for `'tag:'` would prove the string exists, not that
`-tag:cum` excludes anything — and a search box that silently matches nothing
is worse than no search box, because it looks like an empty catalogue.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "frontend" / "js" / "search_query.js"
BOOKSHELF = ROOT / "frontend" / "js" / "bookshelf.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is needed to exercise the JS grammar")

WORKS = [
    {"name": "A", "title": "Tiger Pinup", "content_type": "artwork",
     "tags": ["white_tiger", "anthro", "cum"], "platforms": ["fa", "ib"],
     "artist_name": "Inkwolf", "persona_names": ["Main"], "rating": "explicit",
     "publication_count": 2, "needs_artist": False, "is_junk": False, "series": ""},
    {"name": "B", "title": "Lynx Sketch", "content_type": "artwork",
     "tags": ["lynx", "anthro"], "platforms": [], "artist_name": "",
     "persona_names": [], "rating": "safe", "publication_count": 0,
     "needs_artist": True, "is_junk": False, "series": ""},
    {"name": "C", "title": "Hidden Thing", "content_type": "artwork",
     "tags": ["white_tiger", "oral_(sex)"], "platforms": [], "artist_name": "Boo",
     "persona_names": [], "rating": "explicit", "publication_count": 0,
     "needs_artist": False, "is_junk": True, "series": ""},
    {"name": "D", "title": "A Story", "content_type": "story",
     "tags": ["prose"], "platforms": ["ao3"], "artist_name": "",
     "persona_names": ["Alt"], "rating": "explicit", "publication_count": 1,
     "needs_artist": False, "is_junk": False, "series": "Sample Series"},
]


def _run(queries: list[str]) -> dict[str, list[str]]:
    """Filter WORKS by each query inside node; returns {query: [names]}."""
    script = textwrap.dedent(f"""
        const SQ = require({json.dumps(str(MODULE))});
        const works = {json.dumps(WORKS)};
        const out = {{}};
        for (const q of {json.dumps(queries)}) {{
            out[q] = SQ.filter(works, q).map(w => w.name);
        }}
        process.stdout.write(JSON.stringify(out));
    """)
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ── the basics still work ────────────────────────────────────────

def test_an_empty_query_changes_nothing():
    assert _run([""])[""] == ["A", "B", "C", "D"]


def test_bare_words_still_match_title_and_name():
    """The old behaviour is the default; nobody should have to learn a syntax
    to find a piece by its name."""
    assert _run(["tiger"])["tiger"] == ["A"]


# ── fields ───────────────────────────────────────────────────────

def test_tag_matches_exactly_not_as_a_substring():
    """`tag:tiger` must not drag in `white_tiger` — booru tags are whole tokens
    and substring matching would make negation useless (`-tag:cum` would also
    strip `cum_on_face`, which is a different tag)."""
    r = _run(["tag:white_tiger", "tag:tiger"])
    assert r["tag:white_tiger"] == ["A", "C"]
    assert r["tag:tiger"] == []


@pytest.mark.parametrize("query", ["-tag:cum", "tag_exclude:cum"])
def test_both_spellings_of_exclusion_work(query):
    """`-tag:` is what an e621 user types; `tag_exclude:` is what you reach for
    when unsure the short form is supported. Supporting one is a coin flip."""
    assert _run([query])[query] == ["B", "C", "D"]


def test_terms_and_together():
    q = "tag:white_tiger -tag:cum"
    assert _run([q])[q] == ["C"]


def test_a_comma_is_or_within_one_field():
    q = "tag:lynx,prose"
    assert _run([q])[q] == ["B", "D"]


def test_artist_is_a_substring_and_case_insensitive():
    r = _run(["artist:inkwolf", "artist:inkw"])
    assert r["artist:inkwolf"] == ["A"]
    assert r["artist:inkw"] == ["A"]


def test_platform_rating_type_and_series():
    r = _run(["platform:fa", "rating:safe", "type:story", "series:sample"])
    assert r["platform:fa"] == ["A"]
    assert r["rating:safe"] == ["B"]
    assert r["type:story"] == ["D"]
    assert r["series:sample"] == ["D"]


def test_status_asks_a_question_about_several_fields():
    r = _run(["status:draft", "status:unattributed", "status:posted"])
    assert r["status:draft"] == ["B", "C"]
    assert r["status:unattributed"] == ["B"]
    assert r["status:posted"] == ["A", "D"]


# ── the awkward inputs ───────────────────────────────────────────

def test_a_wildcard_matches_a_prefix():
    q = "tag:white*"
    assert _run([q])[q] == ["A", "C"]


def test_a_tag_with_regex_characters_is_matched_literally():
    """`oral_(sex)` is a real booru tag shape. Un-escaped it is a regex group
    and would either throw or match the wrong thing."""
    q = "tag:oral_(sex)"
    assert _run([q])[q] == ["C"]


def test_quotes_keep_spaces_together():
    q = 'series:"sample series"'
    assert _run([q])[q] == ["D"]


def test_an_unterminated_quote_still_searches():
    """You are searching WHILE typing. Blanking the shelf between the opening
    quote and the closing one makes the box feel broken."""
    q = 'artist:"Inkw'
    assert _run([q])[q] == ["A"]


def test_an_unknown_field_falls_back_to_text_rather_than_matching_nothing():
    """`colour:blue` should look for that literal text, not silently return an
    empty shelf — a search that mysteriously goes blank teaches distrust."""
    r = _run(["colour:blue", "hidden"])
    assert r["colour:blue"] == []      # nothing has that text...
    assert r["hidden"] == ["C"]        # ...but plain text still works


def test_a_bare_colon_is_treated_as_text():
    q = ":thing"
    assert _run([q])[q] == []


# ── junk interaction ─────────────────────────────────────────────

def test_status_junk_finds_the_hidden_piece():
    q = "status:junk"
    assert _run([q])[q] == ["C"]


def test_the_library_lets_a_status_junk_query_through_its_hide_rule():
    """`_filtered` hides junk by default (3.13.1). Typing `status:junk` and
    getting nothing back would be the filter lying, so the hide has to stand
    aside — and it must consult the PARSED query, not a substring of the raw
    input, or `-status:junk` would also open the gate."""
    src = BOOKSHELF.read_text(encoding="utf-8")
    assert "SearchQuery.wantsJunk(parsed)" in src


def test_wants_junk_is_not_fooled_by_a_negated_term():
    script = textwrap.dedent(f"""
        const SQ = require({json.dumps(str(MODULE))});
        process.stdout.write(JSON.stringify({{
            plain: SQ.wantsJunk(SQ.parse('status:junk')),
            negated: SQ.wantsJunk(SQ.parse('-status:junk')),
            excluded: SQ.wantsJunk(SQ.parse('status_exclude:junk')),
            unrelated: SQ.wantsJunk(SQ.parse('tag:x')),
        }}));
    """)
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got == {"plain": True, "negated": False, "excluded": False, "unrelated": False}


# ── wiring ───────────────────────────────────────────────────────

def test_the_module_is_loaded_before_the_library_that_uses_it():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert html.index("search_query.js") < html.index("bookshelf.js")


def test_the_library_degrades_to_substring_search_if_the_module_is_missing():
    """A bad deploy should cost you the fancy syntax, not the search box."""
    src = BOOKSHELF.read_text(encoding="utf-8")
    assert "window.SearchQuery" in src
    assert "(w.title || '').toLowerCase().includes(q)" in src


# ── the payload the grammar needs ────────────────────────────────
#
# `tag:` can only work if /api/works actually carries tags. It did not until
# 3.14.0 — the work dict had title, rating, platforms and stats but nothing
# describing the piece.

def test_works_carry_their_tags():
    from routes.submissions_api import assemble_works
    r = assemble_works(
        stories=[{"name": "S", "title": "S", "tags": ["prose"]}],
        artworks=[{"name": "A", "title": "A",
                   "tags": {"core": ["tiger", "anthro"], "auxiliary": ["smile"]}}],
        pubs=[], acct_to_persona={}, personas={}, junk={})
    by = {w["name"]: w for w in r["works"]}
    assert by["A"]["tags"] == ["tiger", "anthro", "smile"]
    assert by["S"]["tags"] == ["prose"]


def test_artwork_tags_use_the_same_flatten_as_the_posters():
    """Search has to agree with publishing about what a piece is tagged. If the
    two flattened differently, `tag:x` would find pieces that don't post `x`."""
    from posting.artwork_reader import _canonical_tag_list
    from routes.submissions_api import assemble_works
    tags = {"core": ["b", "a"], "default": ["a", "c"], "auxiliary": ["d", "B"]}
    r = assemble_works(stories=[], artworks=[{"name": "A", "title": "A", "tags": tags}],
                       pubs=[], acct_to_persona={}, personas={}, junk={})
    assert r["works"][0]["tags"] == _canonical_tag_list(tags)


def test_a_work_with_no_tags_gets_an_empty_list_not_a_missing_key():
    """`(w.tags || [])` in the grammar covers it, but a missing key would also
    break any consumer that trusts the shape."""
    from routes.submissions_api import assemble_works
    r = assemble_works(stories=[{"name": "S", "title": "S"}],
                       artworks=[{"name": "A", "title": "A"}],
                       pubs=[], acct_to_persona={}, personas={}, junk={})
    assert all(w["tags"] == [] for w in r["works"])
