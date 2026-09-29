"""A story page really renders to PDF through WeasyPrint (4.43.2).

PDF generation had 0% test coverage when WeasyPrint was bumped 68 → 70 for a security
fix — the whole suite passing said nothing about PDFs. `html_to_pdf` also falls back to
Edge on a WeasyPrint failure, which would hide a broken WeasyPrint on a Windows desktop
while the server (no Edge) simply failed. So this insists the WeasyPrint path worked.

Skips on a machine without WeasyPrint's native libraries (a bare Windows dev box), but
NEVER inside the server image (PAWPOLLER_TEST_IN_IMAGE), where they must be present.
"""
from __future__ import annotations

import base64
import os

import pytest

from editor import pdf_generator

# 1×1 PNG — exercises image loading as well as text layout.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _weasyprint_or_skip():
    try:
        import weasyprint  # noqa: F401
    except Exception as e:  # OSError when pango/cairo DLLs are missing
        if os.environ.get("PAWPOLLER_TEST_IN_IMAGE"):
            raise AssertionError(f"WeasyPrint can't load inside the server image: {e}")
        pytest.skip(f"WeasyPrint's native libraries aren't installed here ({type(e).__name__})")


def test_a_story_page_renders_through_weasyprint(tmp_path):
    _weasyprint_or_skip()
    (tmp_path / "cover.png").write_bytes(_PNG)
    (tmp_path / "style.css").write_text(
        "@page { size: A5; margin: 18mm }\nbody { font-family: serif; line-height: 1.5 }\n"
        "h1 { page-break-after: avoid }\n", encoding="utf-8")
    html = tmp_path / "story.html"
    html.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><link rel='stylesheet' href='style.css'></head>"
        "<body><h1>Sample Story</h1><img src='cover.png' alt='cover'>"
        + "".join(f"<p><em>Paragraph {i}</em> — café, naïve, “quotes”.</p>" for i in range(60))
        + "</body></html>", encoding="utf-8")
    ok, backend = pdf_generator.html_to_pdf(html, tmp_path / "out" / "story.pdf")
    assert ok and backend == "weasyprint", (ok, backend)
    data = (tmp_path / "out" / "story.pdf").read_bytes()
    assert data[:5] == b"%PDF-" and len(data) > 2000
    # Page count from WeasyPrint's own layout (PDF 1.7 compresses its page objects).
    from weasyprint import HTML
    assert len(HTML(filename=str(html), base_url=str(tmp_path)).render().pages) >= 2
