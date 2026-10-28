"""Property tests: text going in and out — importers, story formats, journals (PROPTESTS).

Generated documents check what a hand-written example can't cover: the words always
survive, italics survive, user text never comes out as live HTML, and junk input stays fast.
The ``xfail(strict=True)`` cases are known bugs on the backlog; they flip to a failure
the moment the fix lands, so the marker gets removed with it.
"""
import html
import re
import time

import pytest

pytest.importorskip("hypothesis")
from hypothesis import given, settings, strategies as st  # noqa: E402

import pb_import  # noqa: E402
from editor import converter  # noqa: E402
from posting import journals  # noqa: E402
from routes import editor_api  # noqa: E402

word = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8)
phrase = st.lists(word, min_size=1, max_size=4).map(" ".join)
# prose a writer could type, including characters that mean something in HTML/BBCode/Markdown
prose_char = st.sampled_from(list("abcdefgh XYZ.,;:'\"!?-&<>/=()0123456789") + ["é", "—", "’"])
prose = st.text(alphabet=prose_char, min_size=1, max_size=60).filter(lambda s: s.strip())


# ── PostyBirb / story import: pb_import._markdown ──────────────────

@given(st.text(max_size=300).filter(lambda s: "<" not in s))
def test_markdown_leaves_plain_text_alone(s):
    assert pb_import._markdown(s) == html.unescape(s)


@given(st.lists(st.tuples(st.sampled_from(["p", "em", "i", "strong", "b", "span", "div"]),
                          st.sampled_from(["", ' class="x"', ' style="color:red"']),
                          phrase), min_size=1, max_size=6))
def test_markdown_keeps_words_and_marks(parts):
    out = pb_import._markdown("".join(f"<{t}{a}>{w}</{t}>" for t, a, w in parts))
    for t, _, w in parts:
        assert w in out.replace("*", "")
        if t in ("em", "i"):
            assert f"*{w}*" in out
        if t in ("strong", "b"):
            assert f"**{w}**" in out


@settings(max_examples=25)
@given(st.text(alphabet="<a b/>=\"'p\n", min_size=2000, max_size=20000))
def test_markdown_stays_fast_on_junk(s):
    t = time.perf_counter()
    pb_import._markdown(s)
    assert time.perf_counter() - t < 2.0


# ── Create Story file upload: routes.editor_api converters ─────────

@given(st.lists(word, min_size=1, max_size=5))
def test_bbcode_import_keeps_italics_and_bold(words):
    out = editor_api._convert_bbcode_to_md(" ".join(f"[i]{w}[/i] [B]{w}[/B]" for w in words))
    for w in words:
        assert f"*{w}*" in out and f"**{w}**" in out


@given(st.lists(phrase, min_size=1, max_size=5))
def test_html_import_keeps_plain_paragraphs(paras):
    out = editor_api._convert_html_to_md("".join(f"<p>{p}</p>" for p in paras))
    for p in paras:
        assert p in out


@given(st.lists(st.tuples(st.sampled_from(["em", "i"]), st.sampled_from(["", ' class="x"']),
                          st.sampled_from(["", "\n"]), phrase), min_size=1, max_size=5))
def test_html_import_keeps_italics(parts):
    out = editor_api._convert_html_to_md("".join(f"<p><{t}{a}>{nl}{w}{nl}</{t}></p>" for t, a, nl, w in parts))
    for *_, w in parts:
        assert re.search(r"\*\s*" + re.escape(w) + r"\s*\*", out)


@settings(max_examples=3)
@given(st.sampled_from(["em", "strong", "h1"]))
def test_html_import_stays_fast_on_unclosed_tags(tag):
    t = time.perf_counter()
    editor_api._convert_html_to_md(f"<{tag}>" * 12000 + "x")
    assert time.perf_counter() - t < 1.0


@given(st.lists(word, min_size=1, max_size=8))
def test_rtf_import_keeps_ungrouped_words(words):
    out = editor_api._strip_rtf(r"{\rtf1\ansi{\fonttbl{\f0 Times;}}\f0\fs24 " + r" \par ".join(words) + "}")
    for w in words:
        assert w in out


@given(st.lists(word, min_size=1, max_size=5), st.sampled_from([r"\i", r"\b", r"\rtlch\fcs1 \af0 \insrsid1"]))
def test_rtf_import_keeps_grouped_words(words, ctl):
    out = editor_api._strip_rtf(r"{\rtf1\ansi " + " ".join("{" + ctl + " " + w + "}" for w in words) + "}")
    for w in words:
        assert w in out


@given(st.sampled_from(["café", "naïve", "über", "señor"]))
def test_rtf_import_keeps_accents(w):
    enc = "".join(c if ord(c) < 128 else f"\\'{c.encode('cp1252')[0]:02x}" for c in w)
    assert w in editor_api._strip_rtf(r"{\rtf1\ansi " + enc + r"\par}")


# ── MASTER.md → every site format: editor.converter ────────────────

_ALLOWED_TAG = re.compile(r"</?(p|em|strong|h[1-6]|hr|br|div|span|center|blockquote)\b[^<>]*>")


@given(prose)
def test_inline_formatting_keeps_every_character(line):
    segs = converter.parse_markdown_formatting(line)
    assert "".join(t for t, _, _ in segs) == line.replace("*", "")


@given(prose)
def test_paragraph_html_never_carries_raw_markup(line):
    out = converter.format_paragraph_html(line)
    stripped = re.sub(r"</?(em|strong)>", "", out)
    assert "<" not in stripped and ">" not in stripped
    assert html.unescape(stripped) == line.replace("*", "")


@given(st.lists(st.tuples(prose, st.booleans()), min_size=1, max_size=6), st.sampled_from(["clean_html", "sofurry_html"]))
def test_story_html_keeps_words_and_italics_and_escapes(paras, fmt):
    body = "\n\n".join(f"*{p.strip()}*" if ital else p for p, ital in paras)
    out = converter.convert(f"# Chapter 1: Start\n\n{body}", fmt).output
    text = html.unescape(_ALLOWED_TAG.sub("", out))
    assert "<" not in _ALLOWED_TAG.sub("", out).replace("&lt;", "")      # user < > & never live markup
    for p, ital in paras:
        for w in re.findall(r"[a-zXYZ]{3,}", p):
            assert w in text
        if ital and re.search(r"[a-z]", p):
            assert "<em>" in out


@given(st.lists(st.tuples(phrase, st.booleans()), min_size=1, max_size=6))
def test_story_bbcode_keeps_words_and_italics(paras):
    body = "\n\n".join(f"*{p}*" if ital else p for p, ital in paras)
    out = converter.convert(f"# Chapter 1: Start\n\n{body}", "bbcode").output
    for p, ital in paras:
        assert (f"[i]{p}[/i]" if ital else p) in out


@given(st.lists(phrase, min_size=2, max_size=5), st.sampled_from(["clean_html", "sofurry_html", "bbcode"]))
def test_dash_scene_breaks_survive(paras, fmt):
    r = converter.convert("# Chapter 1: Start\n\n" + "\n\n---\n\n".join(paras), fmt)
    assert r.stats["section_breaks"] == len(paras) - 1


@given(st.lists(phrase, min_size=2, max_size=4), st.sampled_from(["***", "* * *"]))
def test_star_scene_breaks_survive(paras, brk):
    r = converter.convert("# Chapter 1: Start\n\n" + f"\n\n{brk}\n\n".join(paras), "clean_html")
    assert r.stats["section_breaks"] == len(paras) - 1


# ── Journals: Markdown subset → DeviantArt HTML / FA BBCode ────────

_JOURNAL_TAG = re.compile(r'</?(p|br|h3|ul|li|em|strong)>|<a href="[^"<>]*">|</a>')


@given(st.lists(st.one_of(prose, phrase.map(lambda p: f"*{p}*"), phrase.map(lambda p: f"**{p}**"),
                          phrase.map(lambda p: f"[{p}](https://example.com/{p.replace(' ', '-')})"),
                          st.just("https://example.com/x?a=1&b=2"), st.just("# Heading"), st.just("- item")),
                min_size=1, max_size=8).map(lambda xs: "\n\n".join(xs)))
def test_journal_html_escapes_user_text_and_links_stay_http(md):
    out = journals.to_html(md)
    assert "<" not in _JOURNAL_TAG.sub("", out)
    for href in re.findall(r'<a href="([^"]*)"', out):
        assert href.startswith(("https://", "http://"))


@given(st.lists(phrase, min_size=1, max_size=5))
def test_journal_formats_keep_italics_and_bold(words):
    md = " ".join(f"*{w}* **{w}**" for w in words)
    h, bb = journals.to_html(md), journals.to_bbcode(md)
    for w in words:
        assert f"<em>{w}</em>" in h and f"<strong>{w}</strong>" in h
        assert f"[i]{w}[/i]" in bb and f"[b]{w}[/b]" in bb


def test_rtf_import_joins_emoji_halves():
    """Word writes a character past U+FFFF as two \\uN codes; lone halves made the MASTER.md write fail."""
    out = editor_api._strip_rtf(r"{\rtf1\ansi\uc1 hi \u-10179?\u-8576? there\par}")
    assert out == "hi \U0001F680 there"
    out.encode("utf-8")