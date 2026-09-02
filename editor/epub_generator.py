"""EPUB 3.0 generation for stories with anchored MASTER.md.

Targets a Vellum-style novel layout: cover -> title page -> copyright ->
dedication -> author's note -> chapters -> (optional) content warning.
Each chapter gets a number-word heading ("One", "Two") and a drop cap
on the first paragraph.

Reuses anchor and body parsing from editor.converter so the input
contract is identical to the other regenerate formats.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from editor.converter import (
    FrontMatter,
    is_phone_display,
    is_pov_marker,
    is_text_message,
    parse_front_matter,
    parse_markdown_formatting,
    parse_semantic_anchor,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Number to word
# ---------------------------------------------------------------------------

_ONES = [
    "Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
    "Nine", "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen",
    "Sixteen", "Seventeen", "Eighteen", "Nineteen",
]
_TENS = [
    "", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy",
    "Eighty", "Ninety",
]


def _number_to_word(n: int) -> str:
    """1 -> 'One', 21 -> 'Twenty-One', 100 -> '100' (fallback)."""
    if 0 <= n < 20:
        return _ONES[n]
    if 20 <= n < 100:
        if n % 10 == 0:
            return _TENS[n // 10]
        return f"{_TENS[n // 10]}-{_ONES[n % 10]}"
    return str(n)


_HEADING_RE = re.compile(
    r"^(?P<kind>Chapter|Part|Section|Book)\s+(?P<num>\d+)(?:\s*[:.\-—]\s*(?P<title>.+))?$",
    re.IGNORECASE,
)


def _split_chapter_heading(heading: str) -> tuple[str | None, str]:
    """Split 'Part 1: The Seduction' -> ('Part One', 'The Seduction').

    Returns (number_label, title). number_label preserves the source
    prefix word ("Part", "Chapter", "Section", "Book") joined with the
    word-form number — so 'Chapter 12: Reckoning' becomes
    ('Chapter Twelve', 'Reckoning'). Returns (None, full_heading) when
    the heading has no recognisable numeric prefix (e.g. 'Epilogue',
    'Prologue').
    """
    m = _HEADING_RE.match(heading.strip())
    if m:
        num = int(m.group("num"))
        kind = m.group("kind").title()  # 'part' -> 'Part'
        title = (m.group("title") or "").strip()
        number_label = f"{kind} {_number_to_word(num)}"
        return number_label, title or kind
    return None, heading.strip()


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _render_inline(segments: list[tuple[str, bool, bool]]) -> str:
    """Render parse_markdown_formatting output as XHTML using <i>/<b>."""
    out: list[str] = []
    for text, italic, bold in segments:
        body = _esc(text)
        if italic and bold:
            body = f"<b><i>{body}</i></b>"
        elif italic:
            body = f"<i>{body}</i>"
        elif bold:
            body = f"<b>{body}</b>"
        out.append(body)
    return "".join(out)


def _format_paragraph(text: str) -> str:
    return _render_inline(parse_markdown_formatting(text))


def _format_paragraph_with_dropcap(text: str) -> str:
    """Format a paragraph and float its first alphabetic letter as a drop cap.

    Works whether or not the paragraph opens with an italic asterisk. If
    the first character isn't alphabetic (e.g. the paragraph opens with a
    quotation mark), no drop cap is applied — Vellum has the same
    fallback via the `first-letter-without-punctuation` CSS hook.
    """
    segments = parse_markdown_formatting(text)
    if not segments:
        return ""

    # Find the first segment containing an alphabetic character.
    drop_idx = -1
    drop_pos = -1
    for i, (seg_text, _it, _bo) in enumerate(segments):
        for j, ch in enumerate(seg_text):
            if ch.isalpha():
                drop_idx = i
                drop_pos = j
                break
        if drop_idx >= 0:
            break

    if drop_idx < 0:
        return _render_inline(segments)

    seg_text, italic, bold = segments[drop_idx]
    head = seg_text[:drop_pos]
    letter = seg_text[drop_pos]
    tail = seg_text[drop_pos + 1:]

    out: list[str] = []

    # Anything before the drop-cap segment renders normally.
    for prev_text, prev_it, prev_bo in segments[:drop_idx]:
        out.append(_render_inline([(prev_text, prev_it, prev_bo)]))

    # Any characters before the drop letter (e.g. a leading space or quote).
    if head:
        out.append(_render_inline([(head, italic, bold)]))

    # The drop cap itself — always roman, regardless of surrounding italics.
    out.append(f'<span class="drop-cap">{_esc(letter)}</span>')

    # The tail of the drop-letter segment, in its original style.
    if tail:
        out.append(_render_inline([(tail, italic, bold)]))

    # Everything after the drop-letter segment.
    for next_text, next_it, next_bo in segments[drop_idx + 1:]:
        out.append(_render_inline([(next_text, next_it, next_bo)]))

    return "".join(out)


# ---------------------------------------------------------------------------
# Body parsing (per chapter)
# ---------------------------------------------------------------------------

def _strip_trailing_separators(lines: list[str]) -> list[str]:
    """Drop trailing blank/`---`/end-marker lines from a chapter body.

    A `---` immediately before a chapter break (or at end-of-file) was
    a visual divider in the source markdown that has no place in the
    rendered chapter — it would emit a stray <hr class="basic-break"/>
    after the chapter's last paragraph and create a blank page in some
    EPUB readers.
    """
    end = len(lines)
    while end > 0:
        s = lines[end - 1].strip()
        if s == "" or s == "---":
            end -= 1
            continue
        # *End of <title>* marker is structural metadata, not body
        if re.match(r"^\*End of .+\*$", s):
            end -= 1
            continue
        break
    return lines[:end]


def _split_into_chapters(
    lines: list[str], body_start: int, *, default_title: str = "Chapter 1"
) -> list[dict]:
    """Walk the body and split into chapters at `# ` headings.

    Returns [{title, lines}, ...] where lines are the raw body lines for
    that chapter, excluding the heading itself. Trailing blank/separator
    lines are stripped from each chapter so a `---` between chapters in
    the source doesn't render as a stray <hr> at the end of the file.

    Single-chapter stories (no `# Chapter X` separator anywhere after the
    body anchor) fall back to a synthetic chapter containing the whole
    body, titled with `default_title` (the caller passes the story title).
    Chaptered stories are unaffected — content seen before the first
    `# ` heading is discarded as before.
    """
    chapters: list[dict] = []
    current: dict | None = None
    orphan_lines: list[str] = []  # body content seen before any '# ' heading
    i = body_start
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "<!-- @body -->":
            i += 1
            continue
        if stripped.startswith("# "):
            heading = stripped[2:].strip()
            if current is not None:
                current["lines"] = _strip_trailing_separators(current["lines"])
                chapters.append(current)
            current = {"title": heading, "lines": []}
            orphan_lines = []  # a heading exists; orphans no longer relevant
            i += 1
            continue
        if current is None:
            # Stash for single-chapter fallback below. In chaptered stories
            # this gets discarded the moment the first '# ' appears.
            orphan_lines.append(lines[i])
            i += 1
            continue
        current["lines"].append(lines[i])
        i += 1
    if current is not None:
        current["lines"] = _strip_trailing_separators(current["lines"])
        chapters.append(current)

    # Single-chapter story fallback — no `# Chapter X` heading anywhere
    # after the body anchor, but the body has content. Treat the whole
    # body as one chapter so EPUB / chapter-aware exporters don't choke.
    if not chapters and any(ln.strip() for ln in orphan_lines):
        chapters.append({
            "title": default_title,
            "lines": _strip_trailing_separators(orphan_lines),
        })
    return chapters


def _convert_chapter_body(lines: list[str]) -> list[str]:
    """Convert chapter body lines into a list of XHTML elements.

    Mirrors the behaviour of editor.converter._convert_body_styled_html
    but emits Vellum-style classes (`first first-in-chapter`, `subsq`,
    `basic-break`, `section-title subhead`, `text-message sent/received`).
    """
    out: list[str] = []
    paragraph_idx = 0
    current: list[str] = []
    pending_semantic: str | None = None

    def flush():
        nonlocal paragraph_idx
        if not current:
            return
        text = " ".join(current)
        current.clear()
        if paragraph_idx == 0:
            html = _format_paragraph_with_dropcap(text)
            out.append(
                f'<p class="first first-in-chapter"><span class="dropcap-wrap">{html}</span></p>'
            )
        else:
            html = _format_paragraph(text)
            out.append(f'<p class="subsq">{html}</p>')
        paragraph_idx += 1

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        # Anchor comments
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            sem = parse_semantic_anchor(stripped)
            if sem:
                flush()
                pending_semantic = sem
            i += 1
            continue

        # Section break
        if stripped == "---":
            flush()
            pending_semantic = None
            # If followed by `# ` it's a chapter break — drop it
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and lines[j].strip().startswith("# "):
                i += 1
                continue
            # End marker comes immediately after a `---`
            if j < len(lines) and re.match(r"^\*End of .+\*$", lines[j].strip()):
                i += 1
                continue
            out.append('<hr class="basic-break" />')
            # Reset so the next paragraph after a scene break also gets
            # full-width treatment? Vellum does not — only the first
            # paragraph of the *chapter* gets the drop cap, paragraphs
            # after scene breaks are normal. Keep paragraph_idx as-is.
            i += 1
            continue

        # End marker — render as a centred terminus
        if re.match(r"^\*End of .+\*$", stripped):
            flush()
            out.append('<p class="story-end">~ End ~</p>')
            i += 1
            continue

        # POV markers (**⟨ Name ⟩**) — in-chapter section subhead
        if is_pov_marker(stripped):
            flush()
            paragraph_idx = 0  # next para gets drop-cap as a fresh subsection start
            inner = stripped[2:-2].strip()
            out.append(
                f'<h2 class="section-title subhead">{_esc(inner)}</h2>'
            )
            i += 1
            continue

        # Phone display (semantic anchor or heuristic)
        if pending_semantic == "phone-incoming":
            flush()
            text = stripped.strip("*").strip()
            out.append(
                f'<p class="phone-display"><span class="phone-display-inner">{_esc(text)}</span></p>'
            )
            pending_semantic = None
            i += 1
            continue

        # Text messages
        if pending_semantic in ("text-sent", "text-received"):
            flush()
            cls = "sent" if pending_semantic == "text-sent" else "received"
            msg = is_text_message(stripped)
            if msg:
                sender = msg.group(1).strip()
                message = msg.group(2).strip()
                out.append(
                    f'<p class="text-message {cls}">'
                    f'<b>{_esc(sender)}</b> {_format_paragraph(message)}</p>'
                )
            else:
                out.append(
                    f'<p class="text-message {cls}">{_format_paragraph(stripped)}</p>'
                )
            pending_semantic = None
            i += 1
            continue

        # Heuristic fallbacks (no anchor)
        phone_m = is_phone_display(stripped)
        if phone_m:
            flush()
            out.append(
                f'<p class="phone-display"><span class="phone-display-inner">{_esc(phone_m.group(1))}</span></p>'
            )
            i += 1
            continue
        msg = is_text_message(stripped)
        if msg:
            flush()
            sender = msg.group(1).strip()
            message = msg.group(2).strip()
            out.append(
                f'<p class="text-message"><b>{_esc(sender)}</b> {_format_paragraph(message)}</p>'
            )
            i += 1
            continue

        if stripped == "":
            flush()
            pending_semantic = None
            i += 1
            continue

        current.append(stripped)
        i += 1

    flush()
    return out


# ---------------------------------------------------------------------------
# Per-document XHTML renderers
# ---------------------------------------------------------------------------

_XHTML_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">
<head>
  <title>{title}</title>
  <meta charset="utf-8" />
  <link rel="stylesheet" type="text/css" href="css/style.css" />
</head>"""


def _xhtml_doc(title: str, body: str) -> str:
    return f"""{_XHTML_HEAD.format(title=_esc(title))}
<body>
{body}
</body>
</html>
"""


def _render_cover_xhtml(cover_filename: str, story_title: str) -> str:
    body = (
        '<section id="cover" epub:type="cover">\n'
        f'  <img class="cover-image" src="images/{cover_filename}" alt="{_esc(story_title)}" role="doc-cover" />\n'
        '</section>'
    )
    return _xhtml_doc("Cover", body)


def _render_titlepage_xhtml(title: str, author: str, subtitle: str | None) -> str:
    parts = [
        '<section class="titlepage" epub:type="titlepage">',
        f'  <h1 class="titlepage-title">{_esc(title)}</h1>',
    ]
    if subtitle:
        parts.append(f'  <p class="titlepage-subtitle"><i>{_esc(subtitle)}</i></p>')
    parts.append(f'  <p class="titlepage-byline">{_esc(author)}</p>')
    parts.append('</section>')
    return _xhtml_doc(title, "\n".join(parts))


def _render_copyright_xhtml(title: str, author: str, year: int) -> str:
    body = f"""<section class="copyright" epub:type="copyright-page">
  <p class="copyright-content first">Copyright &#169; {year} by {_esc(author)}</p>
  <p class="copyright-content"><i>All rights reserved.</i></p>
  <p class="copyright-content">No part of this publication may be reproduced, distributed, or transmitted in any form or by any means without the prior written permission of the author, except as permitted by copyright law.</p>
  <p class="copyright-content">No part of this work may be used, ingested, scraped, or reproduced for the purpose of training, fine-tuning, evaluating, grounding, retrieving from, or otherwise developing or operating any artificial intelligence or machine learning system, including but not limited to large language models, chatbots, generative text or image systems, AI detection tools, and automated content-analysis systems.</p>
  <p class="copyright-content">The characters, events, and situations depicted in &#8220;{_esc(title)}&#8221; are entirely fictional. Any resemblance to actual persons (living or dead), places, or events is purely coincidental. All characters depicted are adults.</p>
</section>"""
    return _xhtml_doc("Copyright", body)


def _render_dedication_xhtml(text: str) -> str:
    body = (
        '<section class="dedication" role="doc-dedication" epub:type="dedication">\n'
        f'  <p class="dedication-content">{_format_paragraph(text)}</p>\n'
        '</section>'
    )
    return _xhtml_doc("Dedication", body)


def _render_authors_note_xhtml(text: str, link_to_warning: bool) -> str:
    body_parts = ['<section class="authors-note">',
                  '  <h1 class="page-title">Author&#8217;s Note</h1>']
    for para in text.split("\n\n"):
        para = para.strip()
        if para:
            body_parts.append(f'  <p>{_format_paragraph(para)}</p>')
    if link_to_warning:
        body_parts.append(
            '  <p class="muted"><i>To avoid spoilers, a detailed '
            '<a href="content-warning.xhtml">content warning</a> is provided at '
            'the back of this book.</i></p>'
        )
    body_parts.append('</section>')
    return _xhtml_doc("Author’s Note", "\n".join(body_parts))


def _render_warning_xhtml(warning_text: str, *, at_back: bool) -> str:
    title = "Content Warning"
    body_parts = [f'<section class="content-warning" id="content-warning">',
                  f'  <h1 class="page-title">{title}</h1>']
    for para in warning_text.split("\n\n"):
        para = para.strip()
        if para:
            body_parts.append(f'  <p>{_format_paragraph(para)}</p>')
    if at_back:
        body_parts.append(
            '  <p class="muted"><i>This page is placed at the end of the book to avoid spoiling the reading experience.</i></p>'
        )
    body_parts.append('</section>')
    return _xhtml_doc(title, "\n".join(body_parts))


def _render_chapter_xhtml(idx: int, number_word: str | None, chapter_title: str,
                          body_paras: list[str]) -> str:
    chapter_id = f"chapter-{idx}"
    heading_parts = ['<header class="chapter-heading">']
    if number_word:
        heading_parts.append(f'  <p class="chapter-number">{_esc(number_word)}</p>')
    heading_parts.append(f'  <h1 class="chapter-title">{_esc(chapter_title)}</h1>')
    heading_parts.append('</header>')

    body_html = "\n".join(["  " + line for line in heading_parts + body_paras])
    body = (
        f'<section id="{chapter_id}" class="chapter" role="doc-chapter" epub:type="chapter">\n'
        f'{body_html}\n'
        '</section>'
    )
    title = chapter_title if not number_word else f"{number_word}. {chapter_title}"
    return _xhtml_doc(title, body)


def _render_toc_xhtml(spine_entries: list[dict]) -> str:
    """nav doc with toc + landmarks."""
    items = "\n".join(
        f'      <li><a href="{e["href"]}">{_esc(e["nav_label"])}</a></li>'
        for e in spine_entries if e.get("in_toc", True)
    )
    landmarks = []
    for e in spine_entries:
        if e.get("landmark"):
            landmarks.append(
                f'      <li><a href="{e["href"]}" epub:type="{e["landmark"]}">{_esc(e["nav_label"])}</a></li>'
            )
    landmarks_html = "\n".join(landmarks)

    body = f"""<nav role="doc-toc" epub:type="toc">
    <h1>Contents</h1>
    <ol>
{items}
    </ol>
  </nav>
  <nav epub:type="landmarks">
    <ol>
{landmarks_html}
    </ol>
  </nav>"""
    # Slightly different head for toc — needs the nav property tag.
    return _xhtml_doc("Contents", body)


# ---------------------------------------------------------------------------
# Package files
# ---------------------------------------------------------------------------

_CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml" />
  </rootfiles>
</container>
"""


def _render_content_opf(*, title: str, author: str, language: str, identifier: str,
                        modified_iso: str, manifest_items: list[dict],
                        spine_idrefs: list[str]) -> str:
    manifest_lines = []
    for item in manifest_items:
        attrs = [f'id="{item["id"]}"', f'href="{item["href"]}"',
                 f'media-type="{item["media_type"]}"']
        if item.get("properties"):
            attrs.append(f'properties="{item["properties"]}"')
        manifest_lines.append("    <item " + " ".join(attrs) + " />")
    manifest_html = "\n".join(manifest_lines)

    spine_html = "\n".join(f'    <itemref idref="{idref}" />' for idref in spine_idrefs)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" xml:lang="{language}" unique-identifier="pub-id" prefix="schema: http://schema.org/">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title id="title">{_esc(title)}</dc:title>
    <dc:creator id="creator">{_esc(author)}</dc:creator>
    <meta refines="#creator" property="role" scheme="marc:relators">aut</meta>
    <dc:language>{language}</dc:language>
    <dc:identifier id="pub-id">urn:uuid:{identifier}</dc:identifier>
    <meta refines="#pub-id" scheme="xsd:string" property="identifier-type">uuid</meta>
    <meta property="dcterms:modified">{modified_iso}</meta>
    <meta property="schema:accessMode">textual</meta>
    <meta property="schema:accessMode">visual</meta>
    <meta property="schema:accessModeSufficient">textual</meta>
    <meta property="schema:accessibilityFeature">structuralNavigation</meta>
    <meta property="schema:accessibilityFeature">tableOfContents</meta>
    <meta property="schema:accessibilityFeature">readingOrder</meta>
    <meta property="schema:accessibilityHazard">none</meta>
    <meta name="generator" content="PawPoller" />
  </metadata>
  <manifest>
{manifest_html}
  </manifest>
  <spine>
{spine_html}
  </spine>
</package>
"""


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """/* PawPoller EPUB stylesheet — Vellum-style novel layout */

body {
  font-family: Palatino, "Palatino Linotype", "Book Antiqua", Georgia, serif;
  margin: 0 1em;
}

h1, h2, h3, h4 {
  page-break-inside: avoid;
  margin: 0;
}

p {
  margin: 0;
  text-indent: 0;
  line-height: 1.45;
  text-align: justify;
  hyphens: auto;
  -webkit-hyphenate-limit-lines: 2;
  -webkit-hyphenate-limit-after: 4;
  -webkit-hyphenate-limit-before: 4;
}

p.subsq {
  text-indent: 1.5em;
}

p.first {
  text-indent: 0;
}

.dropcap-wrap {
  display: block;
}

.drop-cap {
  float: left;
  font-size: 4em;
  line-height: 0.85;
  padding: 0.05em 0.08em 0 0;
  font-style: normal;
  font-weight: normal;
  font-family: Palatino, "Palatino Linotype", "Book Antiqua", Georgia, serif;
}

hr.basic-break {
  border: 0;
  height: 1.4em;
  margin: 0.7em 0;
}

p.story-end {
  text-align: center;
  text-indent: 0;
  margin: 2em 0 1em;
  font-style: italic;
}

.chapter-heading {
  margin: 2em 0 1.5em;
  text-align: center;
  /* No page-break-before — each chapter is its own spine file, so the
     reader already starts every chapter on a new page. Doubling up
     causes a blank page before the heading on some readers. */
}

.chapter-number {
  text-transform: uppercase;
  letter-spacing: 0.2em;
  font-size: 0.95em;
  margin-bottom: 0.5em;
}

.chapter-title {
  font-size: 1.6em;
  font-weight: normal;
  letter-spacing: 0.05em;
}

.section-title.subhead {
  margin: 1.4em 0 0.7em;
  text-align: center;
  font-size: 1em;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  font-weight: normal;
}

/* Title page */
.titlepage {
  text-align: center;
  margin-top: 4em;
}
.titlepage-title {
  font-size: 2.4em;
  font-weight: normal;
  letter-spacing: 0.04em;
  margin-bottom: 0.4em;
}
.titlepage-subtitle {
  font-size: 1.1em;
  margin: 0.3em 0 2em;
}
.titlepage-byline {
  font-size: 1em;
  text-transform: uppercase;
  letter-spacing: 0.25em;
}

/* Copyright */
.copyright p {
  font-size: 90%;
  text-indent: 0;
  margin-bottom: 0.6em;
  text-align: justify;
}

/* Dedication */
.dedication {
  margin-top: 6em;
  text-align: center;
}
.dedication-content {
  font-style: italic;
  text-indent: 0;
}

/* Author's note + content warning */
.authors-note, .content-warning {
  margin-top: 2em;
}
.authors-note .page-title, .content-warning .page-title {
  text-align: center;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  font-size: 1.2em;
  font-weight: normal;
  margin-bottom: 1.2em;
}
.authors-note p, .content-warning p {
  margin-bottom: 0.6em;
  text-indent: 0;
}
.muted {
  font-size: 90%;
  color: #555;
  text-align: center;
}

/* Cover */
body.cover, section#cover {
  margin: 0;
  padding: 0;
  text-align: center;
}
.cover-image {
  max-width: 100%;
  max-height: 100%;
  height: auto;
}

/* Text messages — rendered as a sender-tagged card so they work
   regardless of whether the source uses @text-sent / @text-received
   anchors or the legacy bold-line shorthand. iMessage left/right
   bubbles need protagonist context the source doesn't carry. */
p.text-message {
  margin: 0.6em 1.5em;
  padding: 0.55em 0.9em;
  background: #f3f3f3;
  border-left: 3px solid #b0b0b0;
  border-radius: 0.4em;
  text-indent: 0;
  text-align: left;
  hyphens: none;
  font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: 0.92em;
  line-height: 1.35;
}
p.text-message b {
  display: block;
  font-size: 0.78em;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: #5a5a5a;
  margin-bottom: 0.25em;
  font-weight: 600;
}
/* When the source DOES use anchors, give sent/received a subtle tint
   so a writer who sets them up gets the iMessage hint for free. */
p.text-message.sent {
  background: #e6efff;
  border-left-color: #6f8ec0;
}
p.text-message.received {
  background: #f0f0f0;
  border-left-color: #9a9a9a;
}

p.phone-display {
  text-align: center;
  text-indent: 0;
  margin: 1.2em 0;
}
p.phone-display .phone-display-inner {
  display: inline-block;
  border: 1px solid #888;
  border-radius: 1em;
  padding: 0.4em 1.2em;
  font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
  letter-spacing: 0.08em;
  font-size: 0.9em;
}
"""


# ---------------------------------------------------------------------------
# Build orchestrator
# ---------------------------------------------------------------------------

def build_epub(story_dir: str | Path, output_path: str | Path | None = None,
               *,
               warning_position: str = "front",
               year: int | None = None) -> Path:
    """Build an EPUB for the story at story_dir.

    Args:
        story_dir: path to a Complete_Stories/<Story> directory containing
            Markdown/MASTER.md and story.json.
        output_path: where to write the .epub. Defaults to
            <story_dir>/Markdown/<title>.epub.
        warning_position: 'front' (after author's note) or 'back' (after
            chapters, with a forward link from the author's note).
        year: copyright year (defaults to current year).

    Returns:
        The path the epub was written to.
    """
    story_dir = Path(story_dir)
    if warning_position not in ("front", "back"):
        raise ValueError(f"warning_position must be 'front' or 'back' (got {warning_position!r})")

    # ---- Load source artefacts
    master_path = story_dir / "Markdown" / "MASTER.md"
    if not master_path.is_file():
        raise FileNotFoundError(f"MASTER.md not found at {master_path}")
    text = master_path.read_text(encoding="utf-8")

    story_json_path = story_dir / "story.json"
    story_meta: dict = {}
    if story_json_path.is_file():
        try:
            story_meta = json.loads(story_json_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to load story.json at %s: %s", story_json_path, e)

    fm = parse_front_matter(text)
    if fm is None:
        # Synthesize minimal front matter so we still produce something usable.
        fm = FrontMatter(title=story_meta.get("title", story_dir.name), body_start_line=0)

    title = fm.title or story_meta.get("title") or story_dir.name
    author = fm.byline or story_meta.get("author") or "Unknown Author"
    # story.json's subtitle (set via the metadata drawer) takes precedence
    # over the MASTER.md `<!-- @subtitle -->` anchor — the drawer is the
    # canonical UI surface for editing this field.
    subtitle = (story_meta.get("subtitle") or fm.subtitle or "").strip() or None
    year = year or datetime.now().year

    # ---- Pick cover
    cover_src: Path | None = None
    cover_jpg = story_dir / "cover.jpg"
    if cover_jpg.is_file():
        cover_src = cover_jpg
    else:
        rel = (story_meta.get("images") or {}).get("cover")
        if rel:
            cand = story_dir / rel
            if cand.is_file():
                cover_src = cand

    # ---- Split chapters
    lines = text.split("\n")
    body_start = fm.body_start_line if fm else 0
    # Pass story title as default — used by single-chapter stories
    # (no '# Chapter X' separator in body) so the whole body becomes
    # one synthetic chapter titled with the story name.
    raw_chapters = _split_into_chapters(lines, body_start, default_title=title)
    if not raw_chapters:
        raise ValueError(f"No chapters found in {master_path} after body anchor")

    # ---- Build chapter xhtml
    chapter_files: list[dict] = []
    spine_idrefs: list[str] = []
    manifest_items: list[dict] = []

    # Title-page / copyright / dedication / authors-note files
    titlepage_xhtml = _render_titlepage_xhtml(title, author, subtitle)
    copyright_xhtml = _render_copyright_xhtml(title, author, year)
    dedication_text = (story_meta.get("dedication") or "").strip()
    authors_note_text = (fm.disclaimer or story_meta.get("authors_note") or "").strip()
    warning_text = fm.warning or ""
    fanfiction_text = (fm.fanfiction or "").strip()

    spine_entries: list[dict] = []  # used for toc
    files_to_write: dict[str, str | bytes] = {}

    # Cover (only if we have a cover image)
    if cover_src:
        cover_filename = cover_src.name
        files_to_write[f"OEBPS/images/{cover_filename}"] = cover_src.read_bytes()
        media = "image/jpeg" if cover_src.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        manifest_items.append({
            "id": "cover-image", "href": f"images/{cover_filename}",
            "media_type": media, "properties": "cover-image",
        })
        cover_xhtml = _render_cover_xhtml(cover_filename, title)
        files_to_write["OEBPS/cover.xhtml"] = cover_xhtml
        manifest_items.append({"id": "cover", "href": "cover.xhtml",
                               "media_type": "application/xhtml+xml"})
        spine_idrefs.append("cover")
        spine_entries.append({"href": "cover.xhtml", "nav_label": "Cover",
                              "in_toc": True, "landmark": "cover"})

    # Title page
    files_to_write["OEBPS/titlepage.xhtml"] = titlepage_xhtml
    manifest_items.append({"id": "titlepage", "href": "titlepage.xhtml",
                           "media_type": "application/xhtml+xml"})
    spine_idrefs.append("titlepage")
    spine_entries.append({"href": "titlepage.xhtml", "nav_label": title, "in_toc": True})

    # Copyright
    files_to_write["OEBPS/copyright.xhtml"] = copyright_xhtml
    manifest_items.append({"id": "copyright", "href": "copyright.xhtml",
                           "media_type": "application/xhtml+xml"})
    spine_idrefs.append("copyright")
    spine_entries.append({"href": "copyright.xhtml", "nav_label": "Copyright",
                          "in_toc": True, "landmark": "copyright-page"})

    # Dedication (optional)
    if dedication_text:
        files_to_write["OEBPS/dedication.xhtml"] = _render_dedication_xhtml(dedication_text)
        manifest_items.append({"id": "dedication", "href": "dedication.xhtml",
                               "media_type": "application/xhtml+xml"})
        spine_idrefs.append("dedication")
        spine_entries.append({"href": "dedication.xhtml", "nav_label": "Dedication",
                              "in_toc": True})

    # Author's note (drawn from disclaimer + optional fanfiction notice)
    note_blocks: list[str] = []
    if authors_note_text:
        note_blocks.append(authors_note_text)
    if fanfiction_text:
        note_blocks.append(fanfiction_text)
    if note_blocks:
        combined_note = "\n\n".join(note_blocks)
        files_to_write["OEBPS/authors-note.xhtml"] = _render_authors_note_xhtml(
            combined_note, link_to_warning=(warning_position == "back" and bool(warning_text))
        )
        manifest_items.append({"id": "authors-note", "href": "authors-note.xhtml",
                               "media_type": "application/xhtml+xml"})
        spine_idrefs.append("authors-note")
        spine_entries.append({"href": "authors-note.xhtml",
                              "nav_label": "Author’s Note", "in_toc": True})

    # Front-of-book content warning (default position)
    if warning_text and warning_position == "front":
        files_to_write["OEBPS/content-warning.xhtml"] = _render_warning_xhtml(
            warning_text, at_back=False,
        )
        manifest_items.append({"id": "content-warning", "href": "content-warning.xhtml",
                               "media_type": "application/xhtml+xml"})
        spine_idrefs.append("content-warning")
        spine_entries.append({"href": "content-warning.xhtml",
                              "nav_label": "Content Warning", "in_toc": True})

    # First-bodymatter landmark target — set on first chapter below.
    first_chapter_filename: str | None = None

    # Chapters
    for idx, ch in enumerate(raw_chapters, start=1):
        number_word, chapter_title = _split_chapter_heading(ch["title"])
        body_paras = _convert_chapter_body(ch["lines"])
        chapter_xhtml = _render_chapter_xhtml(idx, number_word, chapter_title, body_paras)
        filename = f"chapter-{idx:03d}.xhtml"
        files_to_write[f"OEBPS/{filename}"] = chapter_xhtml
        item_id = f"chapter-{idx:03d}"
        manifest_items.append({"id": item_id, "href": filename,
                               "media_type": "application/xhtml+xml"})
        spine_idrefs.append(item_id)
        nav_label = chapter_title
        if number_word:
            nav_label = f"{idx}. {chapter_title}"
        entry = {"href": filename, "nav_label": nav_label, "in_toc": True}
        if first_chapter_filename is None:
            entry["landmark"] = "bodymatter"
            first_chapter_filename = filename
        spine_entries.append(entry)
        chapter_files.append({"id": item_id, "filename": filename, "title": nav_label})

    # Back-of-book content warning (Vellum convention)
    if warning_text and warning_position == "back":
        files_to_write["OEBPS/content-warning.xhtml"] = _render_warning_xhtml(
            warning_text, at_back=True,
        )
        manifest_items.append({"id": "content-warning", "href": "content-warning.xhtml",
                               "media_type": "application/xhtml+xml"})
        spine_idrefs.append("content-warning")
        spine_entries.append({"href": "content-warning.xhtml",
                              "nav_label": "Content Warning", "in_toc": True})

    # Stylesheet
    files_to_write["OEBPS/css/style.css"] = _CSS
    manifest_items.append({"id": "stylesheet", "href": "css/style.css",
                           "media_type": "text/css"})

    # TOC nav doc — comes after spine items so it can reference them.
    files_to_write["OEBPS/toc.xhtml"] = _render_toc_xhtml(spine_entries)
    manifest_items.append({"id": "toc", "href": "toc.xhtml",
                           "media_type": "application/xhtml+xml",
                           "properties": "nav"})

    # ---- Package
    identifier = str(uuid.uuid4()).upper()
    modified_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    opf = _render_content_opf(
        title=title, author=author, language="en",
        identifier=identifier, modified_iso=modified_iso,
        manifest_items=manifest_items, spine_idrefs=spine_idrefs,
    )
    files_to_write["OEBPS/content.opf"] = opf
    files_to_write["META-INF/container.xml"] = _CONTAINER_XML

    # ---- Output path
    if output_path is None:
        stem = master_path.stem  # MASTER -> 'MASTER'
        # Prefer the story title for the filename
        safe_title = re.sub(r"[^A-Za-z0-9._\- ]", "", title).strip().replace(" ", "_") or stem
        output_path = story_dir / "EPUB" / f"{safe_title}.epub"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ---- Write zip with mimetype first, uncompressed.
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        # Then everything else, ordered for legibility
        ordered_keys = sorted(files_to_write.keys())
        for key in ordered_keys:
            data = files_to_write[key]
            if isinstance(data, str):
                data = data.encode("utf-8")
            zf.writestr(key, data, compress_type=zipfile.ZIP_DEFLATED)

    logger.info("Wrote EPUB %s (%d chapters, %d files)",
                output_path, len(chapter_files), len(files_to_write) + 1)
    return output_path


# ---------------------------------------------------------------------------
# CLI for ad-hoc testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Build an EPUB from a story directory.")
    parser.add_argument("story_dir", help="Path to Complete_Stories/<Story> directory")
    parser.add_argument("-o", "--output", default=None,
                        help="Output path (default: <story>/Markdown/<title>.epub)")
    parser.add_argument("--warning-position", choices=["front", "back"], default="front",
                        help="Where the content-warning page goes")
    parser.add_argument("--year", type=int, default=None,
                        help="Copyright year (default: current year)")
    args = parser.parse_args()

    out = build_epub(args.story_dir, args.output,
                     warning_position=args.warning_position, year=args.year)
    print(f"Wrote {out}")
