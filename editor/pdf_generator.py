"""HTML→PDF generation for styled story files.

Primary backend is WeasyPrint (pure Python, works in Docker without a
browser). Falls back to Edge headless on Windows desktops if WeasyPrint
is unavailable for any reason.

Used by the editor's /regenerate endpoint and the standalone
regenerate_story.py CLI.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _weasyprint_available() -> bool:
    try:
        import weasyprint  # noqa: F401
        return True
    except Exception:
        return False


def _find_edge() -> str | None:
    for p in _EDGE_PATHS:
        if os.path.isfile(p):
            return p
    return None


def html_to_pdf(html_path: str | Path, pdf_path: str | Path) -> tuple[bool, str]:
    """Render an HTML file to PDF.

    Returns (success, backend_used). Backend is "weasyprint" or "edge".
    """
    html_path = Path(html_path)
    pdf_path = Path(pdf_path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    if _weasyprint_available():
        try:
            from weasyprint import HTML
            # base_url lets WeasyPrint resolve <link href="style.css"> and
            # any image src relative to the HTML file's directory.
            HTML(filename=str(html_path), base_url=str(html_path.parent)).write_pdf(
                str(pdf_path)
            )
            if pdf_path.is_file() and pdf_path.stat().st_size > 0:
                return True, "weasyprint"
        except Exception as e:
            logger.warning("WeasyPrint failed for %s: %s — trying Edge fallback", html_path, e)

    edge = _find_edge()
    if edge:
        html_url = "file:///" + str(html_path.resolve()).replace("\\", "/")
        pdf_fwd = str(pdf_path.resolve()).replace("\\", "/")
        try:
            # --no-pdf-header-footer suppresses the browser-added page
            # header (date) and footer (title/URL) that Chromium adds by
            # default. Without it the PDF shows "DD/MM/YYYY, HH:MM" at
            # the top-left and the filename/URL at the bottom — harmless
            # on screen but destroys print output.
            # --no-margins tells Chromium to defer entirely to the CSS
            # `@page` rule instead of overlaying its own ~0.5" default.
            subprocess.run(
                [edge, "--headless", "--disable-gpu",
                 "--no-margins", "--no-pdf-header-footer",
                 f"--print-to-pdf={pdf_fwd}", html_url],
                capture_output=True, text=True, timeout=120,
            )
            if pdf_path.is_file() and pdf_path.stat().st_size > 0:
                return True, "edge"
        except Exception as e:
            logger.warning("Edge fallback failed for %s: %s", html_path, e)

    return False, "none"


def get_backend() -> str:
    """Report which backend is currently usable. For diagnostics/UI."""
    if _weasyprint_available():
        return "weasyprint"
    if _find_edge():
        return "edge"
    return "none"
