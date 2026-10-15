"""Post text sent as HTML keeps apostrophes and quotes literal (4.76.1).

DeviantArt showed "isn&#x27;t" on a posted deviation: html.escape's default also encodes
quotes, and DA doesn't decode &#x27;. Text nodes don't need quotes encoded.
"""
from clients.da.client import description_to_da_html
from clients.ng.client import _as_paragraphs
from posting.journals import _inline


def test_da_description_keeps_apostrophes():
    out = description_to_da_html('The sparkle isn\'t "just" the background <3 & more')
    assert "isn't" in out and '"just"' in out
    assert "&#x27;" not in out and "&quot;" not in out
    assert "&lt;3 &amp; more" in out          # markup is still escaped


def test_newgrounds_and_journal_text_keep_apostrophes():
    assert "isn't" in _as_paragraphs("isn't")
    out = _inline('isn\'t [a "link"](https://example.com/?q="x")', "html")
    assert "isn't" in out and '"link"' in out
    assert 'href="https://example.com/?q=&quot;x&quot;"' in out   # attributes still quote-safe
