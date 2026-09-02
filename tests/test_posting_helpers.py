"""Unit tests for pure helper functions in the posting module.

Runnable via ``python -m unittest tests.test_posting_helpers`` from the
PawPoller root. No external dependencies, no network, no DB — just
exercises the deterministic string / dict / form-parse helpers that
shipped across the 2.10.x session.

Lock in the behaviour of:
  - ``posting.manager._looks_like_deletion``
  - ``posting.platforms.ao3._strip_chapter_prefix``
  - ``posting.platforms.squidgeworld._strip_chapter_prefix``
  - ``clients.ao3.client._extract_work_form_fields``
  - ``clients.sqw.client._extract_work_form_fields``
"""

from __future__ import annotations

import os
import sys
import unittest

# Make the PawPoller root importable when running from ``tests/`` or root.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class TestLooksLikeDeletion(unittest.TestCase):
    """The deletion-error pattern matcher must catch real upstream deletions
    without false-positiving on unrelated 'not found' messages (file I/O,
    cache misses, etc.)."""

    def setUp(self):
        from posting.manager import _looks_like_deletion
        self.fn = _looks_like_deletion

    def test_none_and_empty_are_safe(self):
        self.assertFalse(self.fn(None))
        self.assertFalse(self.fn(""))

    def test_inkbunny_deletion_caught(self):
        self.assertTrue(
            self.fn("Edit failed: That submission has been deleted.")
        )

    def test_fa_deletion_caught(self):
        self.assertTrue(self.fn("FA: submission not found"))
        self.assertTrue(self.fn("Submission was not found on the server"))

    def test_ao3_deletion_caught(self):
        self.assertTrue(self.fn("AO3: work not found"))
        self.assertTrue(self.fn("Work has been deleted"))
        self.assertTrue(self.fn("Work does not exist"))

    def test_httpx_404_caught(self):
        self.assertTrue(self.fn("Client error '404 Not Found' for url ..."))
        self.assertTrue(self.fn("Server returned 404 Not Found"))

    def test_case_insensitive(self):
        self.assertTrue(self.fn("SUBMISSION HAS BEEN DELETED"))
        self.assertTrue(self.fn("Client Error '404"))

    def test_false_positives_rejected(self):
        # The old tuple had a bare 'not found' catch-all that would match these.
        # The tightened tuple must NOT match these unrelated errors.
        self.assertFalse(self.fn("File not found on disk"))
        self.assertFalse(self.fn("Image asset not found in cache"))
        self.assertFalse(self.fn("Python module not found: some_lib"))
        self.assertFalse(self.fn("User not found"))  # could be any auth error
        self.assertFalse(self.fn("Tag not found in local DB"))

    def test_unrelated_errors_rejected(self):
        self.assertFalse(self.fn("Connection reset by peer"))
        self.assertFalse(self.fn("SSL certificate expired"))
        self.assertFalse(self.fn("Invalid rating value"))


class TestStripChapterPrefixAO3(unittest.TestCase):
    """OTW Archive auto-prefixes chapters with 'Chapter N:' on display.
    Stripping the raw prefix avoids 'Chapter 1: Chapter 1: The Counter'."""

    def setUp(self):
        from posting.platforms.ao3 import _strip_chapter_prefix
        self.fn = _strip_chapter_prefix

    def test_simple_chapter_prefix(self):
        self.assertEqual(self.fn("Chapter 1: The Counter"), "The Counter")
        self.assertEqual(self.fn("Chapter 12: The Final Scene"), "The Final Scene")

    def test_part_prefix(self):
        self.assertEqual(self.fn("Part 2: Revelation"), "Revelation")

    def test_prelude_and_epilogue(self):
        self.assertEqual(self.fn("Prelude: The Storm"), "The Storm")
        self.assertEqual(self.fn("Epilogue: After"), "After")

    def test_prefix_with_em_dash(self):
        self.assertEqual(self.fn("Chapter 1 — The Counter"), "The Counter")
        self.assertEqual(self.fn("Chapter 1 - The Counter"), "The Counter")
        self.assertEqual(self.fn("Chapter 1 – The Counter"), "The Counter")

    def test_case_insensitive(self):
        self.assertEqual(self.fn("CHAPTER 1: The Counter"), "The Counter")
        self.assertEqual(self.fn("chapter 1: the counter"), "the counter")

    def test_no_prefix_preserved(self):
        self.assertEqual(self.fn("The Counter"), "The Counter")
        self.assertEqual(self.fn("Late Shift"), "Late Shift")

    def test_empty_and_none_safe(self):
        self.assertEqual(self.fn(""), "")
        self.assertEqual(self.fn(None), None)

    def test_prefix_only_keeps_original(self):
        # If the ENTIRE title is just "Chapter 1:" (weird but possible),
        # don't return empty — fall back to the raw title.
        self.assertEqual(self.fn("Chapter 1:"), "Chapter 1:")

    def test_trailing_whitespace_stripped(self):
        self.assertEqual(self.fn("Chapter 1:   The Counter"), "The Counter")


class TestStripChapterPrefixSQW(unittest.TestCase):
    """SQW's helper is a verbatim copy of AO3's — same behaviour expected."""

    def setUp(self):
        from posting.platforms.squidgeworld import _strip_chapter_prefix
        self.fn = _strip_chapter_prefix

    def test_simple(self):
        self.assertEqual(self.fn("Chapter 1: Opening"), "Opening")

    def test_matches_ao3_helper(self):
        """The two helpers must behave identically — they're copy-paste
        siblings. Flag any divergence so we don't accidentally drift."""
        from posting.platforms.ao3 import _strip_chapter_prefix as ao3_fn
        samples = [
            "Chapter 1: The Counter",
            "Part 2: Revelation",
            "Prelude: The Storm",
            "Epilogue: After",
            "Chapter 1 — The Counter",
            "The Counter",
            "",
            None,
            "Chapter 1:",
            "CHAPTER 1: The Counter",
        ]
        for s in samples:
            self.assertEqual(self.fn(s), ao3_fn(s), f"divergence on {s!r}")


class TestExtractWorkFormFields(unittest.TestCase):
    """The safe-overlay pattern depends on this parser pulling every
    work[*] field out of the edit form. Fragile HTML; worth locking."""

    def setUp(self):
        from clients.sqw.client import _extract_work_form_fields as sqw_fn
        from clients.ao3.client import _extract_work_form_fields as ao3_fn
        self.sqw_fn = sqw_fn
        self.ao3_fn = ao3_fn

    _SAMPLE_FORM = """
    <html><body>
    <form action="/works/123" method="post">
      <input type="hidden" name="authenticity_token" value="csrf-xyz-456">
      <input type="hidden" name="_method" value="patch">
      <input type="text" name="work[title]" value="Late Shift">
      <textarea name="work[summary]">A story about patterns.</textarea>
      <input type="text" name="work[freeform_string]" value="raccoon, wolf, slow burn">
      <input type="checkbox" name="work[archive_warning_strings][]" value="No Archive Warnings Apply" checked>
      <input type="checkbox" name="work[archive_warning_strings][]" value="Graphic Depictions Of Violence">
      <input type="checkbox" name="work[category_strings][]" value="M/M" checked>
      <input type="checkbox" name="work[category_strings][]" value="F/F">
      <select name="work[rating_string]">
        <option value="Explicit">Explicit</option>
        <option value="Mature" selected>Mature</option>
        <option value="Teen And Up Audiences">Teen</option>
      </select>
      <input type="text" name="work[fandom_string]" value="Original Work">
      <input type="text" name="work[relationship_string]" value="Ryan/Silas">
      <input type="text" name="work[character_string]" value="Ryan (Raccoon), Silas (Wolf)">
      <input type="hidden" name="work[work_skin_id]" value="10230931">
      <input type="hidden" name="chapter[author_attributes][ids][]" value="99123">
      <input type="submit" name="commit" value="Save As Draft">
    </form>
    </body></html>
    """

    def test_sqw_extracts_csrf_token(self):
        token, fields = self.sqw_fn(self._SAMPLE_FORM)
        self.assertEqual(token, "csrf-xyz-456")

    def test_sqw_extracts_text_inputs(self):
        _, fields = self.sqw_fn(self._SAMPLE_FORM)
        d = dict(fields)
        self.assertEqual(d["work[title]"], "Late Shift")
        self.assertEqual(d["work[freeform_string]"], "raccoon, wolf, slow burn")
        self.assertEqual(d["work[fandom_string]"], "Original Work")
        self.assertEqual(d["work[relationship_string]"], "Ryan/Silas")

    def test_sqw_extracts_textarea(self):
        _, fields = self.sqw_fn(self._SAMPLE_FORM)
        d = dict(fields)
        self.assertEqual(d["work[summary]"], "A story about patterns.")

    def test_sqw_extracts_select_selected_option(self):
        _, fields = self.sqw_fn(self._SAMPLE_FORM)
        d = dict(fields)
        self.assertEqual(d["work[rating_string]"], "Mature")

    def test_sqw_only_extracts_checked_checkboxes(self):
        _, fields = self.sqw_fn(self._SAMPLE_FORM)
        warnings = [v for n, v in fields if n == "work[archive_warning_strings][]"]
        self.assertIn("No Archive Warnings Apply", warnings)
        self.assertNotIn("Graphic Depictions Of Violence", warnings)

        cats = [v for n, v in fields if n == "work[category_strings][]"]
        self.assertEqual(cats, ["M/M"])  # F/F not checked

    def test_sqw_skips_submit_button(self):
        _, fields = self.sqw_fn(self._SAMPLE_FORM)
        self.assertFalse(any(n == "commit" for n, _ in fields))

    def test_sqw_skips_auth_token_and_method(self):
        """These are extracted separately — the fields list shouldn't
        contain them as ambiguous entries."""
        _, fields = self.sqw_fn(self._SAMPLE_FORM)
        self.assertFalse(any(n == "authenticity_token" for n, _ in fields))
        self.assertFalse(any(n == "_method" for n, _ in fields))

    def test_sqw_keeps_pseud_and_author_attrs(self):
        """chapter[author_attributes][ids][] is needed on chapter edits."""
        _, fields = self.sqw_fn(self._SAMPLE_FORM)
        self.assertTrue(
            any(n == "chapter[author_attributes][ids][]" for n, _ in fields)
        )

    def test_ao3_extracts_equivalently(self):
        """AO3's helper is a verbatim port — must produce the same output
        for the same input."""
        sqw_token, sqw_fields = self.sqw_fn(self._SAMPLE_FORM)
        ao3_token, ao3_fields = self.ao3_fn(self._SAMPLE_FORM)
        self.assertEqual(sqw_token, ao3_token)
        # Order should match too since both helpers walk the same regex
        self.assertEqual(sqw_fields, ao3_fields)

    def test_missing_csrf_token_raises(self):
        with self.assertRaises(RuntimeError):
            self.sqw_fn("<form></form>")  # no authenticity_token

    def test_html_entities_decoded_in_textarea(self):
        html = '''
        <form action="/works/1">
          <input name="authenticity_token" value="x">
          <textarea name="work[summary]">Cats &amp; dogs &#39;vs&#39; &quot;fish&quot;</textarea>
        </form>
        '''
        _, fields = self.sqw_fn(html)
        d = dict(fields)
        self.assertEqual(d["work[summary]"], "Cats & dogs 'vs' \"fish\"")


if __name__ == "__main__":
    unittest.main()
