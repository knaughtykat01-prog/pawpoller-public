"""Core markdown-to-format conversion library for the story editor.

This is the SINGLE implementation of the markdown italic/bold parser,
shared by the editor live-preview, the regeneration pipeline, and
(eventually) the standalone CLI converter scripts.

Supports:
  - `*italic*`  → toggles italic state
  - `**bold**`  → toggles bold state
  - `***both***`→ toggles both states
  - Mixed: `*narration* "dialogue" *narration*` → alternating italic/roman
  - Nested: `*outer *inner* outer*` → italic, roman, italic, roman
  - Text messages: `**SENDER: msg**` → bold header
  - POV markers: `**⟨ Name ⟩**` → bold
  - Chapter headings: `# Chapter N: Title`
  - Section breaks: `---`
  - End marker: `*End of ...*` → `~ End ~`
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


def _default_author() -> str:
    """Return the default author from settings, or empty string if unavailable.

    Lazy import of config avoids coupling this pure-formatting module to the
    application config at import time.  Falls back to empty string so byline
    rendering degrades gracefully when no default is configured.
    """
    try:
        import config
        return config.get_settings().get("default_author", "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Core parser — handles *, **, *** correctly
# ---------------------------------------------------------------------------

def parse_markdown_formatting(line: str) -> list[tuple[str, bool, bool]]:
    """Parse a markdown line for `*` (italic) and `**` (bold) markers.

    Scans left-to-right, toggling italic/bold state at each marker.
    Returns list of (text, is_italic, is_bold) tuples.
    """
    segments: list[tuple[str, bool, bool]] = []
    current: list[str] = []
    in_italic = False
    in_bold = False
    i = 0
    while i < len(line):
        if line[i] == "*":
            j = i
            while j < len(line) and line[j] == "*":
                j += 1
            n_stars = j - i
            if current:
                segments.append(("".join(current), in_italic, in_bold))
                current = []
            if n_stars >= 3:
                in_italic = not in_italic
                in_bold = not in_bold
            elif n_stars == 2:
                in_bold = not in_bold
            else:
                in_italic = not in_italic
            i = j
        else:
            current.append(line[i])
            i += 1
    if current:
        segments.append(("".join(current), in_italic, in_bold))
    return segments


# ---------------------------------------------------------------------------
# HTML rendering (SoFurry / AO3 / SquidgeWorld body)
# ---------------------------------------------------------------------------

def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_html(segments: list[tuple[str, bool, bool]]) -> str:
    """Render parsed segments as HTML with <em> and <strong> tags."""
    parts: list[str] = []
    for text, italic, bold in segments:
        if not text:
            continue
        escaped = _escape_html(text)
        if italic and bold:
            parts.append(f"<em><strong>{escaped}</strong></em>")
        elif italic:
            parts.append(f"<em>{escaped}</em>")
        elif bold:
            parts.append(f"<strong>{escaped}</strong>")
        else:
            parts.append(escaped)
    return "".join(parts)


def format_paragraph_html(line: str) -> str:
    """Convert a markdown paragraph to HTML."""
    return render_html(parse_markdown_formatting(line))


# ---------------------------------------------------------------------------
# BBCode rendering (Inkbunny)
# ---------------------------------------------------------------------------

def render_bbcode(segments: list[tuple[str, bool, bool]]) -> str:
    """Render parsed segments as BBCode with [i] and [b] tags."""
    parts: list[str] = []
    for text, italic, bold in segments:
        if not text:
            continue
        if italic and bold:
            parts.append(f"[i][b]{text}[/b][/i]")
        elif italic:
            parts.append(f"[i]{text}[/i]")
        elif bold:
            parts.append(f"[b]{text}[/b]")
        else:
            parts.append(text)
    return "".join(parts)


def format_paragraph_bbcode(line: str) -> str:
    """Convert a markdown paragraph to BBCode."""
    return render_bbcode(parse_markdown_formatting(line))


# ---------------------------------------------------------------------------
# Structural detection helpers
# ---------------------------------------------------------------------------

def is_pov_marker(stripped: str) -> bool:
    """Detect POV markers: **⟨ Name ⟩** on own line."""
    if stripped.startswith("**") and stripped.endswith("**") and len(stripped) > 4:
        inner = stripped[2:-2]
        if "⟨" in inner and "⟩" in inner:
            return True
    return False


def is_text_message(stripped: str) -> re.Match | None:
    """Detect text messages: **Name:** message text on own line.

    Matches both formats:
      **Mika:** message text     (bold name only, message is plain)
      **MIKA: message text**     (entire line bold — legacy)
    """
    # Bold name + colon, message is plain (current convention)
    m = re.match(r"^\*\*(.+?):\*\*\s*(.+)$", stripped)
    if m:
        return m
    # Legacy: entire line bold including message
    return re.match(r"^\*\*([A-Z][A-Z\s❤♥]*?):\s*(.+?)\*\*$", stripped)


def is_phone_display(stripped: str) -> re.Match | None:
    """Detect phone call display: **NAME ❤** on own line."""
    return re.match(r"^\*\*([A-Z][A-Z\s]*[❤♥])\*\*$", stripped)


# Semantic anchor types recognised in body content
SEMANTIC_ANCHORS = {
    "text-sent": "text-sent",
    "text-received": "text-received",
    "phone-incoming": "phone-incoming",
}


def parse_semantic_anchor(stripped: str) -> str | None:
    """Check if a line is a semantic anchor comment.

    Returns the anchor type ('text-sent', 'text-received', 'phone-incoming')
    or None if not a semantic anchor.
    """
    m = re.match(r"^<!--\s*@(\S+)\s*-->$", stripped)
    if m and m.group(1) in SEMANTIC_ANCHORS:
        return SEMANTIC_ANCHORS[m.group(1)]
    return None


def detect_chapters(text: str) -> list[dict]:
    """Parse chapter headings from markdown text.

    Returns list of {index, title, line_start, line_end} dicts.
    """
    lines = text.split("\n")
    chapters: list[dict] = []
    for i, line in enumerate(lines):
        m = re.match(r"^#\s+(.+)$", line.strip())
        if m:
            title = m.group(1)
            # Close previous chapter
            if chapters:
                chapters[-1]["line_end"] = i - 1
            chapters.append({
                "index": len(chapters),
                "title": title,
                "line_start": i,
                "line_end": len(lines) - 1,
            })
    return chapters


# ---------------------------------------------------------------------------
# Full document conversion — markdown text → output format
# ---------------------------------------------------------------------------

@dataclass
class ConversionResult:
    """Result of a full markdown-to-format conversion."""
    output: str
    format: str
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class FrontMatter:
    """Structured front matter extracted from anchored MASTER.md.

    Anchors are HTML comments that mark structural sections:
      <!-- @title -->      → title heading
      <!-- @subtitle -->   → subtitle/tagline (optional)
      <!-- @byline -->     → author attribution (optional)
      <!-- @warning -->    → content warning block
      <!-- @disclaimer --> → disclaimer block
      <!-- @body -->       → boundary where story content begins
    """
    title: str = ""
    subtitle: str | None = None
    byline: str | None = None
    warning: str = ""
    disclaimer: str = ""
    fanfiction: str | None = None  # fan fiction notice (optional, for IP attribution)
    body_start_line: int = 0  # line index where @body begins


def parse_front_matter(text: str) -> FrontMatter | None:
    """Parse anchored front matter from MASTER.md.

    Looks for `<!-- @body -->` as the primary indicator that the file
    uses the anchor system. If absent, returns None (caller should
    fall back to heuristic parsing).

    Extracts text between each anchor pair. Content after each anchor
    runs until the next anchor or end of front matter.
    """
    lines = text.split("\n")

    # Quick check: does this file use anchors?
    body_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "<!-- @body -->":
            body_idx = i
            break
    if body_idx is None:
        return None  # no anchors, use heuristic fallback

    fm = FrontMatter(body_start_line=body_idx)

    # Parse front matter (everything before @body)
    current_section: str | None = None
    section_lines: dict[str, list[str]] = {}

    for i in range(body_idx):
        stripped = lines[i].strip()

        # Check for anchor markers
        anchor_m = re.match(r"^<!--\s*@(\w+)\s*-->$", stripped)
        if anchor_m:
            current_section = anchor_m.group(1)
            section_lines.setdefault(current_section, [])
            continue

        # Accumulate non-blank, non-separator lines into the current section
        if current_section and stripped and stripped != "---":
            section_lines[current_section].append(stripped)

    # Extract structured fields from raw section lines
    if "title" in section_lines:
        raw = " ".join(section_lines["title"])
        # Strip the leading # from the heading
        fm.title = re.sub(r"^#+\s*", "", raw).strip()

    if "subtitle" in section_lines:
        raw = " ".join(section_lines["subtitle"])
        # Strip italic markers
        fm.subtitle = raw.strip("* ").strip()

    if "byline" in section_lines:
        raw = " ".join(section_lines["byline"])
        fm.byline = raw.strip("* ").strip()

    if "warning" in section_lines:
        raw = "\n".join(section_lines["warning"])
        # Strip the **Content Warning**: prefix to get just the warning text
        raw = re.sub(r"^\*\*Content Warning\*?\*?:?\s*", "", raw, flags=re.IGNORECASE).strip()
        fm.warning = raw

    if "disclaimer" in section_lines:
        raw_lines = section_lines["disclaimer"]
        body_lines = [l for l in raw_lines if l.strip() != "**DISCLAIMER**"]
        fm.disclaimer = "\n".join(body_lines).strip()

    if "fanfiction" in section_lines:
        raw_lines = section_lines["fanfiction"]
        body_lines = [l for l in raw_lines if l.strip() != "**FAN FICTION NOTICE**"]
        fm.fanfiction = "\n".join(body_lines).strip()

    return fm


def render_front_matter_clean_html(fm: FrontMatter) -> list[str]:
    """Render FrontMatter as centred Clean HTML (AO3) paragraphs."""
    parts: list[str] = []
    c = lambda inner: f'<p style="text-align:center">{inner}</p>'

    parts.append(c(f"<strong>{_escape_html(fm.title)}</strong>"))
    if fm.subtitle:
        parts.append(c(f"<em>{_escape_html(fm.subtitle)}</em>"))
    if fm.byline:
        parts.append(c(f"<em>{_escape_html(fm.byline)}</em>"))
    parts.append(c(f"<strong>Content Warning</strong>: {_escape_html(fm.warning)}"))
    parts.append(c("<strong>DISCLAIMER</strong>"))
    parts.append(c(_escape_html(fm.disclaimer)))
    if fm.fanfiction:
        parts.append(c("<strong>FAN FICTION NOTICE</strong>"))
        parts.append(c(_escape_html(fm.fanfiction)))
    return parts


def render_front_matter_sofurry(fm: FrontMatter) -> list[str]:
    """Render FrontMatter as SoFurry HTML using SF's tag system."""
    parts: list[str] = []
    tc = lambda inner: f'<p style="text-align: center;">{inner}</p>'

    parts.append(f'<h1 style="text-align: center;">{_escape_html(fm.title)}</h1>')
    if fm.subtitle:
        parts.append(tc(f"<em>{_escape_html(fm.subtitle)}</em>"))
    if fm.byline:
        parts.append(tc(f"<em>{_escape_html(fm.byline)}</em>"))
    parts.append(tc(f"<strong>Content Warning</strong>: {_escape_html(fm.warning)}"))
    parts.append(tc("<strong>DISCLAIMER</strong>"))
    parts.append(tc(_escape_html(fm.disclaimer)))
    if fm.fanfiction:
        parts.append(tc("<strong>FAN FICTION NOTICE</strong>"))
        parts.append(tc(_escape_html(fm.fanfiction)))
    return parts


def render_front_matter_bbcode(fm: FrontMatter) -> list[str]:
    """Render FrontMatter as BBCode."""
    parts: list[str] = []

    parts.append(f"[center][t]{fm.title}[/t][/center]")
    if fm.subtitle:
        parts.extend(["", f"[center][i]{fm.subtitle}[/i][/center]"])
    if fm.byline:
        parts.extend(["", f"[center][i]{fm.byline}[/i][/center]"])
    parts.extend(["", f"[center][b]Content Warning[/b]: {fm.warning}[/center]"])
    parts.extend(["", f"[center][b]DISCLAIMER[/b][/center]"])
    parts.extend(["", f"[center]{fm.disclaimer}[/center]"])
    if fm.fanfiction:
        parts.extend(["", f"[center][b]FAN FICTION NOTICE[/b][/center]"])
        parts.extend(["", f"[center]{fm.fanfiction}[/center]"])
    return parts


def render_front_matter_sqw(fm: FrontMatter, chapter_title: str,
                            warning_icon: str = "&#9888;") -> list[str]:
    """Render FrontMatter as SquidgeWorld warning-page div."""
    parts: list[str] = []
    parts.append('<div class="warning-page">')
    parts.append(f'    <h1 class="story-title">{_escape_html(fm.title)}</h1>')
    parts.append(f'    <p class="byline">by {_escape_html(fm.byline or _default_author())}</p>')
    parts.append('    <hr class="title-rule">')
    parts.append(f'    <h2 class="chapter-subtitle">{_escape_html(chapter_title)}</h2>')
    parts.append("")
    parts.append(f'    <p class="warning-heading">{warning_icon} CONTENT WARNING {warning_icon}</p>')
    parts.append("")
    parts.append(f'    <p class="warning-body">{_escape_html(fm.warning)}</p>')
    parts.append("")
    parts.append('    <hr class="warning-divider">')
    parts.append("")
    parts.append('    <p class="disclaimer-heading">DISCLAIMER</p>')
    parts.append("")
    parts.append(f'    <p class="disclaimer-body">{_escape_html(fm.disclaimer)}</p>')
    if fm.fanfiction:
        parts.append("")
        parts.append('    <hr class="warning-divider">')
        parts.append("")
        parts.append('    <p class="disclaimer-heading">FAN FICTION NOTICE</p>')
        parts.append("")
        parts.append(f'    <p class="disclaimer-body">{_escape_html(fm.fanfiction)}</p>')
    parts.append('</div>')
    return parts


def _is_warning_line(stripped: str) -> bool:
    """Detect content-warning/disclaimer lines from MASTER.md front matter."""
    return (
        stripped.startswith("**Content Warning**")
        or stripped.startswith("**Content Warning:**")
        or stripped.startswith("**Content Warning:")
        or stripped == "**DISCLAIMER**"
        or stripped.startswith("**DISCLAIMER**")
    )


def _center_html(inner: str) -> str:
    """Wrap HTML content in a centred paragraph."""
    return f'<p style="text-align:center">{inner}</p>'


def _convert_body_clean_html(lines: list[str], start: int = 0) -> tuple[list[str], dict]:
    """Convert body content (after front matter) to Clean HTML.

    Handles chapter headings, section breaks, POV markers, text messages,
    paragraphs — everything INSIDE the story. Front matter is handled
    separately by render_front_matter_clean_html().
    """
    body_parts: list[str] = []
    stats: dict = {
        "chapters": [], "section_breaks": 0, "paragraphs": 0,
        "pov_markers": [], "text_messages": 0, "end_marker": False,
    }
    i = start
    current_paragraph: list[str] = []

    def flush():
        if current_paragraph:
            text = " ".join(current_paragraph)
            converted = format_paragraph_html(text)
            body_parts.append(f"<p>{converted}</p>")
            stats["paragraphs"] += 1
            current_paragraph.clear()

    pending_semantic: str | None = None  # tracks semantic anchor for next content

    while i < len(lines):
        stripped = lines[i].strip()

        # Check for semantic anchors (<!-- @text-sent --> etc.)
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            sem = parse_semantic_anchor(stripped)
            if sem:
                flush()
                pending_semantic = sem
            # Skip all anchor comments (semantic or structural)
            i += 1
            continue

        if stripped == "---":
            flush()
            pending_semantic = None
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and lines[j].strip().startswith("# "):
                body_parts.append("<hr />")
            else:
                body_parts.append('<p style="text-align:center" class="section-break">* * *</p>')
                stats["section_breaks"] += 1
            i += 1
            continue

        if stripped.startswith("# "):
            flush()
            pending_semantic = None
            heading = _escape_html(stripped[2:].strip())
            body_parts.append(_center_html(f"<strong>{heading}</strong>"))
            stats["chapters"].append(heading)
            i += 1
            continue

        if re.match(r"^\*End of .+\*$", stripped):
            flush()
            body_parts.append(_center_html("<strong>~ End ~</strong>"))
            stats["end_marker"] = True
            i += 1
            continue

        if is_pov_marker(stripped):
            flush()
            inner = stripped[2:-2]
            body_parts.append(_center_html(f"<strong>{_escape_html(inner)}</strong>"))
            stats["pov_markers"].append(inner)
            i += 1
            continue

        # Semantic anchor handling: text messages + phone displays
        if pending_semantic == "phone-incoming":
            flush()
            # Strip bold markers if present
            text = stripped.strip("*").strip()
            body_parts.append(
                f'<div class="phone-display-wrap"><div class="phone-display">'
                f'{_escape_html(text)}</div></div>'
            )
            stats["text_messages"] += 1
            pending_semantic = None
            i += 1
            continue

        if pending_semantic in ("text-sent", "text-received"):
            flush()
            msg_class = "sent" if pending_semantic == "text-sent" else "received"
            # Parse **Name:** message pattern
            msg_m = is_text_message(stripped)
            if msg_m:
                sender = msg_m.group(1).strip()
                message = msg_m.group(2).strip()
                body_parts.append(
                    f'<div class="text-message {msg_class}">'
                    f'<strong>{_escape_html(sender)}</strong> '
                    f'{format_paragraph_html(message)}</div>'
                )
            else:
                # Fallback: no sender pattern, treat whole line as message
                body_parts.append(
                    f'<div class="text-message {msg_class}">'
                    f'{format_paragraph_html(stripped)}</div>'
                )
            stats["text_messages"] += 1
            pending_semantic = None
            i += 1
            continue

        # Non-anchored text message detection (heuristic fallback).
        # Emits the same div structure as the semantic-anchor branch
        # above so SquidgeWorld / AO3 work-skin CSS (.phone-display-wrap,
        # .text-message) styles these correctly even when the source
        # MASTER.md doesn't carry explicit `<!-- @phone-incoming -->` /
        # `<!-- @text-sent -->` / `<!-- @text-received -->` anchors.
        # Without explicit anchors we can't tell sent from received, so
        # text-message divs get no modifier class — the Work Skin's
        # base `.text-message` rule still applies.
        phone_m = is_phone_display(stripped)
        if phone_m:
            flush()
            caller = phone_m.group(1).strip()
            body_parts.append(
                f'<div class="phone-display-wrap"><div class="phone-display">'
                f'{_escape_html(caller)}</div></div>'
            )
            stats["text_messages"] += 1
            i += 1
            continue

        msg_m = is_text_message(stripped)
        if msg_m:
            flush()
            sender = msg_m.group(1).strip()
            message = msg_m.group(2).strip()
            body_parts.append(
                f'<div class="text-message">'
                f'<strong>{_escape_html(sender)}</strong> '
                f'{format_paragraph_html(message)}</div>'
            )
            stats["text_messages"] += 1
            i += 1
            continue

        if stripped == "":
            flush()
            pending_semantic = None
            i += 1
            continue

        current_paragraph.append(stripped)
        i += 1

    flush()
    return body_parts, stats


def convert_to_clean_html(markdown_text: str) -> ConversionResult:
    """Convert full MASTER.md text to body-only Clean HTML (AO3/SQW).

    If the file has anchors (<!-- @body -->), uses structured front matter
    parsing. Otherwise falls back to heuristic parsing.
    """
    lines = markdown_text.split("\n")

    # Try anchor-based parsing first
    fm = parse_front_matter(markdown_text)
    if fm is not None:
        # Render front matter
        front_parts = render_front_matter_clean_html(fm)
        # Render body (everything after @body)
        body_parts, body_stats = _convert_body_clean_html(lines, fm.body_start_line + 1)
        # Combine
        all_parts = front_parts + body_parts
        stats = {
            "title": fm.title, "subtitle": fm.subtitle,
            **body_stats,
        }
        output = "\n".join(all_parts)
        return ConversionResult(output=output, format="clean_html", stats=stats)

    # --- HEURISTIC FALLBACK (for non-anchored files) ---
    body_parts: list[str] = []
    stats = {
        "title": None, "subtitle": None, "chapters": [],
        "section_breaks": 0, "paragraphs": 0,
        "pov_markers": [], "text_messages": 0, "end_marker": False,
    }
    warnings: list[str] = []

    title_seen = False
    subtitle_done = False
    in_warning_block = False
    i = 0
    current_paragraph: list[str] = []

    def flush():
        if current_paragraph:
            text = " ".join(current_paragraph)
            converted = format_paragraph_html(text)
            if in_warning_block:
                body_parts.append(_center_html(converted))
            else:
                body_parts.append(f"<p>{converted}</p>")
            stats["paragraphs"] += 1
            current_paragraph.clear()

    while i < len(lines):
        stripped = lines[i].strip()

        if stripped == "---":
            flush()
            in_warning_block = False
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and lines[j].strip().startswith("# "):
                body_parts.append("<hr />")
            else:
                body_parts.append('<p style="text-align:center" class="section-break">* * *</p>')
                stats["section_breaks"] += 1
            i += 1
            continue

        if stripped.startswith("# "):
            flush()
            in_warning_block = False
            heading = _escape_html(stripped[2:].strip())
            body_parts.append(_center_html(f"<strong>{heading}</strong>"))
            if not title_seen:
                title_seen = True
                stats["title"] = heading
            else:
                stats["chapters"].append(heading)
                subtitle_done = True
            i += 1
            continue

        if title_seen and not subtitle_done:
            if stripped == "":
                flush()
                i += 1
                continue
            if re.match(r"^\*[^*]+\*$", stripped):
                inner = stripped[1:-1]
                if not inner.startswith("End of "):
                    flush()
                    body_parts.append(_center_html(f"<em>{_escape_html(inner)}</em>"))
                    subtitle_done = True
                    stats["subtitle"] = inner
                    i += 1
                    continue
            else:
                subtitle_done = True

        if _is_warning_line(stripped):
            flush()
            in_warning_block = True
            converted = format_paragraph_html(stripped)
            body_parts.append(_center_html(converted))
            stats["paragraphs"] += 1
            i += 1
            continue

        if re.match(r"^\*End of .+\*$", stripped):
            flush()
            body_parts.append(_center_html("<strong>~ End ~</strong>"))
            stats["end_marker"] = True
            i += 1
            continue

        if is_pov_marker(stripped):
            flush()
            inner = stripped[2:-2]
            body_parts.append(_center_html(f"<strong>{_escape_html(inner)}</strong>"))
            stats["pov_markers"].append(inner)
            i += 1
            continue

        phone_m = is_phone_display(stripped)
        if phone_m:
            flush()
            caller = phone_m.group(1).strip()
            body_parts.append(_center_html(f"<strong>{_escape_html(caller)}</strong>"))
            stats["text_messages"] += 1
            i += 1
            continue

        msg_m = is_text_message(stripped)
        if msg_m:
            flush()
            sender = msg_m.group(1).strip()
            message = msg_m.group(2).strip()
            body_parts.append(
                f"<p><strong>{_escape_html(sender)}:</strong> {_escape_html(message)}</p>"
            )
            stats["text_messages"] += 1
            i += 1
            continue

        if stripped == "":
            flush()
            i += 1
            continue

        current_paragraph.append(stripped)
        i += 1

    flush()

    output = "\n".join(body_parts)
    return ConversionResult(output=output, format="clean_html", stats=stats, warnings=warnings)


def _convert_body_sofurry(lines: list[str], start: int = 0) -> tuple[list[str], dict]:
    """Convert body content to SoFurry HTML (after front matter)."""
    body_parts: list[str] = []
    stats: dict = {
        "chapters": [], "section_breaks": 0, "paragraphs": 0,
        "pov_markers": [], "text_messages": 0, "end_marker": False,
    }
    tc = lambda inner: f'<p style="text-align: center;">{inner}</p>'  # TipTap centered block
    i = start
    current_paragraph: list[str] = []

    def flush():
        if current_paragraph:
            text = " ".join(current_paragraph)
            body_parts.append(f"<p>{format_paragraph_html(text)}</p>")
            stats["paragraphs"] += 1
            current_paragraph.clear()

    pending_semantic: str | None = None

    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            sem = parse_semantic_anchor(stripped)
            if sem:
                flush()
                pending_semantic = sem
            i += 1
            continue
        if stripped == "---":
            flush()
            pending_semantic = None
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and lines[j].strip().startswith("# "):
                body_parts.append("<hr>")
            else:
                body_parts.append(tc("* * *"))
                stats["section_breaks"] += 1
            i += 1
            continue
        if stripped.startswith("# "):
            flush()
            pending_semantic = None
            heading = _escape_html(stripped[2:].strip())
            body_parts.append(f'<h2 style="text-align: center;">{heading}</h2>')
            stats["chapters"].append(heading)
            i += 1
            continue
        if re.match(r"^\*End of .+\*$", stripped):
            flush()
            body_parts.append(tc("<strong>~ End ~</strong>"))
            stats["end_marker"] = True
            i += 1
            continue
        if is_pov_marker(stripped):
            flush()
            inner = stripped[2:-2]
            body_parts.append(tc(f"<strong>{_escape_html(inner)}</strong>"))
            stats["pov_markers"].append(inner)
            i += 1
            continue

        # Semantic anchor: phone display
        if pending_semantic == "phone-incoming":
            flush()
            text = stripped.strip("*").strip()
            body_parts.append(tc(f"<strong>{_escape_html(text)}</strong>"))
            stats["text_messages"] += 1
            pending_semantic = None
            i += 1
            continue

        # Semantic anchor: text messages
        if pending_semantic in ("text-sent", "text-received"):
            flush()
            align = "right" if pending_semantic == "text-sent" else "left"
            msg_m = is_text_message(stripped)
            if msg_m:
                sender, message = msg_m.group(1).strip(), msg_m.group(2).strip()
                body_parts.append(f'<p style="text-align: {align};"><strong>{_escape_html(sender)}:</strong> {format_paragraph_html(message)}</p>')
            else:
                body_parts.append(f'<p style="text-align: {align};">{format_paragraph_html(stripped)}</p>')
            stats["text_messages"] += 1
            pending_semantic = None
            i += 1
            continue

        # Non-anchored heuristic fallbacks
        phone_m = is_phone_display(stripped)
        if phone_m:
            flush()
            body_parts.append(tc(f"<strong>{_escape_html(phone_m.group(1).strip())}</strong>"))
            stats["text_messages"] += 1
            i += 1
            continue
        msg_m = is_text_message(stripped)
        if msg_m:
            flush()
            sender, message = msg_m.group(1).strip(), msg_m.group(2).strip()
            body_parts.append(f'<p><strong>{_escape_html(sender)}:</strong> {format_paragraph_html(message)}</p>')
            stats["text_messages"] += 1
            i += 1
            continue
        if stripped == "":
            flush()
            pending_semantic = None
            i += 1
            continue
        current_paragraph.append(stripped)
        i += 1
    flush()
    return body_parts, stats


def _convert_body_bbcode(lines: list[str], start: int = 0) -> tuple[list[str], dict]:
    """Convert body content to BBCode (after front matter)."""
    output_lines: list[str] = []
    stats: dict = {
        "chapters": [], "section_breaks": 0, "paragraphs": 0,
        "pov_markers": [], "text_messages": 0, "end_marker": False,
    }
    i = start

    pending_semantic: str | None = None

    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            sem = parse_semantic_anchor(stripped)
            if sem:
                pending_semantic = sem
            i += 1
            continue
        if stripped == "---":
            pending_semantic = None
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and lines[j].strip().startswith("# "):
                output_lines.extend(["", "[center]───────────────────[/center]", ""])
            elif j < len(lines) and re.match(r"^\*End of .+\*$", lines[j].strip()):
                output_lines.extend(["", "[center]───────────────────[/center]", ""])
            else:
                output_lines.extend(["", "[center]* * *[/center]", ""])
                stats["section_breaks"] += 1
            i += 1
            continue
        if stripped.startswith("# "):
            pending_semantic = None
            heading = stripped[2:].strip()
            output_lines.extend(["", f"[center][b]{heading}[/b][/center]"])
            stats["chapters"].append(heading)
            i += 1
            continue
        if re.match(r"^\*End of .+\*$", stripped):
            output_lines.extend(["", "[center][b]~ End ~[/b][/center]"])
            stats["end_marker"] = True
            i += 1
            continue
        if is_pov_marker(stripped):
            inner = stripped[2:-2]
            output_lines.extend(["", f"[center][b]{inner}[/b][/center]", ""])
            stats["pov_markers"].append(inner)
            i += 1
            continue

        # Semantic anchor: phone display
        if pending_semantic == "phone-incoming":
            text = stripped.strip("*").strip()
            output_lines.extend(["", f"[center]───── [color=#2c6e2c]📱 {text}[/color] ─────[/center]", ""])
            stats["text_messages"] += 1
            pending_semantic = None
            i += 1
            continue

        # Semantic anchor: text messages
        if pending_semantic in ("text-sent", "text-received"):
            msg_m = is_text_message(stripped)
            if pending_semantic == "text-sent":
                color, align = "#508c46", "right"
            else:
                color, align = "#a07818", "left"
            if msg_m:
                sender, message = msg_m.group(1).strip(), msg_m.group(2).strip()
                output_lines.append(f"[{align}][color={color}][b]{sender}:[/b] {format_paragraph_bbcode(message)}[/color][/{align}]")
            else:
                output_lines.append(f"[{align}][color={color}]{format_paragraph_bbcode(stripped)}[/color][/{align}]")
            stats["text_messages"] += 1
            pending_semantic = None
            i += 1
            continue

        # Non-anchored heuristic fallbacks
        phone_m = is_phone_display(stripped)
        if phone_m:
            output_lines.extend(["", f"[center]───── [color=#2c6e2c]📱 {phone_m.group(1).strip()}[/color] ─────[/center]", ""])
            stats["text_messages"] += 1
            i += 1
            continue
        msg_m = is_text_message(stripped)
        if msg_m:
            sender, message = msg_m.group(1).strip(), msg_m.group(2).strip()
            output_lines.append(f"[b]{sender}:[/b] {format_paragraph_bbcode(message)}")
            stats["text_messages"] += 1
            i += 1
            continue
        if stripped == "":
            output_lines.append("")
            pending_semantic = None
            i += 1
            continue
        output_lines.append(format_paragraph_bbcode(stripped))
        stats["paragraphs"] += 1
        i += 1
    return output_lines, stats


def convert_to_sofurry_html(markdown_text: str) -> ConversionResult:
    """Convert full MASTER.md text to SoFurry-specific HTML.

    If the file has anchors, uses structured parsing.
    Otherwise falls back to heuristic parsing.
    """
    lines = markdown_text.split("\n")

    fm = parse_front_matter(markdown_text)
    if fm is not None:
        front_parts = render_front_matter_sofurry(fm)
        body_parts, body_stats = _convert_body_sofurry(lines, fm.body_start_line + 1)
        all_parts = front_parts + body_parts
        stats = {"title": fm.title, "subtitle": fm.subtitle, **body_stats}
        return ConversionResult(output="\n".join(all_parts), format="sofurry_html", stats=stats)

    # --- HEURISTIC FALLBACK ---
    body_parts: list[str] = []
    stats = {
        "title": None, "subtitle": None, "chapters": [],
        "section_breaks": 0, "paragraphs": 0,
        "pov_markers": [], "text_messages": 0, "end_marker": False,
    }
    title_seen = False
    subtitle_done = False
    in_warning_block = False
    i = 0
    current_paragraph: list[str] = []
    tc = lambda inner: f'<p style="text-align: center;">{inner}</p>'

    def flush():
        if current_paragraph:
            text = " ".join(current_paragraph)
            converted = format_paragraph_html(text)
            body_parts.append(tc(converted) if in_warning_block else f"<p>{converted}</p>")
            stats["paragraphs"] += 1
            current_paragraph.clear()

    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "---":
            flush(); in_warning_block = False
            j = i + 1
            while j < len(lines) and lines[j].strip() == "": j += 1
            body_parts.append("<hr>" if j < len(lines) and lines[j].strip().startswith("# ") else tc("* * *"))
            if not (j < len(lines) and lines[j].strip().startswith("# ")): stats["section_breaks"] += 1
            i += 1; continue
        if stripped.startswith("# "):
            flush(); in_warning_block = False; heading = _escape_html(stripped[2:].strip())
            if not title_seen:
                body_parts.append(f'<h1 style="text-align: center;">{heading}</h1>'); title_seen = True; stats["title"] = heading
            else:
                body_parts.append(f'<h2 style="text-align: center;">{heading}</h2>'); stats["chapters"].append(heading); subtitle_done = True
            i += 1; continue
        if title_seen and not subtitle_done:
            if stripped == "": flush(); i += 1; continue
            if re.match(r"^\*[^*]+\*$", stripped):
                inner = stripped[1:-1]
                if not inner.startswith("End of "):
                    flush(); body_parts.append(tc(f"<em>{_escape_html(inner)}</em>")); subtitle_done = True; stats["subtitle"] = inner; i += 1; continue
            subtitle_done = True
        if _is_warning_line(stripped):
            flush(); in_warning_block = True; body_parts.append(tc(format_paragraph_html(stripped))); stats["paragraphs"] += 1; i += 1; continue
        if re.match(r"^\*End of .+\*$", stripped):
            flush(); body_parts.append(tc("<strong>~ End ~</strong>")); stats["end_marker"] = True; i += 1; continue
        if is_pov_marker(stripped):
            flush(); body_parts.append(tc(f"<strong>{_escape_html(stripped[2:-2])}</strong>")); stats["pov_markers"].append(stripped[2:-2]); i += 1; continue
        phone_m = is_phone_display(stripped)
        if phone_m:
            flush(); body_parts.append(tc(f"<strong>{_escape_html(phone_m.group(1).strip())}</strong>")); stats["text_messages"] += 1; i += 1; continue
        msg_m = is_text_message(stripped)
        if msg_m:
            flush(); s, m = msg_m.group(1).strip(), msg_m.group(2).strip()
            align = "right" if "MAYA" in s.upper() else "left"
            body_parts.append(f'<p style="text-align: {align};"><strong>{_escape_html(s)}:</strong> {_escape_html(m)}</p>'); stats["text_messages"] += 1; i += 1; continue
        if stripped == "": flush(); i += 1; continue
        current_paragraph.append(stripped); i += 1
    flush()
    return ConversionResult(output="\n".join(body_parts), format="sofurry_html", stats=stats)


def convert_to_bbcode(markdown_text: str) -> ConversionResult:
    """Convert full MASTER.md text to BBCode (Inkbunny).

    If the file has anchors, uses structured parsing.
    Otherwise falls back to heuristic parsing.
    """
    lines = markdown_text.split("\n")

    fm = parse_front_matter(markdown_text)
    if fm is not None:
        front_parts = render_front_matter_bbcode(fm)
        body_parts, body_stats = _convert_body_bbcode(lines, fm.body_start_line + 1)
        all_parts = front_parts + body_parts
        stats = {"title": fm.title, "subtitle": fm.subtitle, **body_stats}
        return ConversionResult(output="\n".join(all_parts), format="bbcode", stats=stats)

    # --- HEURISTIC FALLBACK ---
    output_lines: list[str] = []
    stats = {
        "title": None, "subtitle": None, "chapters": [],
        "section_breaks": 0, "paragraphs": 0,
        "pov_markers": [], "text_messages": 0, "end_marker": False,
    }
    title_seen = False
    subtitle_done = False
    in_warning_block = False
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "---":
            in_warning_block = False
            j = i + 1
            while j < len(lines) and lines[j].strip() == "": j += 1
            if j < len(lines) and lines[j].strip().startswith("# "):
                output_lines.extend(["", "[center]───────────────────[/center]", ""])
            elif j < len(lines) and re.match(r"^\*End of .+\*$", lines[j].strip()):
                output_lines.extend(["", "[center]───────────────────[/center]", ""])
            else:
                output_lines.extend(["", "[center]* * *[/center]", ""]); stats["section_breaks"] += 1
            i += 1; continue
        if stripped.startswith("# "):
            in_warning_block = False; heading = stripped[2:].strip()
            if not title_seen:
                output_lines.append(f"[center][t]{heading}[/t][/center]"); title_seen = True; stats["title"] = heading
            else:
                output_lines.extend(["", f"[center][b]{heading}[/b][/center]"]); stats["chapters"].append(heading); subtitle_done = True
            i += 1; continue
        if _is_warning_line(stripped):
            in_warning_block = True; output_lines.append(f"[center]{format_paragraph_bbcode(stripped)}[/center]"); stats["paragraphs"] += 1; i += 1; continue
        if title_seen and not subtitle_done:
            if stripped == "": output_lines.append(""); i += 1; continue
            if re.match(r"^\*[^*]+\*$", stripped):
                inner = stripped[1:-1]
                if not inner.startswith("End of "):
                    output_lines.extend(["", f"[center][i]{inner}[/i][/center]", ""]); subtitle_done = True; stats["subtitle"] = inner; i += 1; continue
            subtitle_done = True
        if re.match(r"^\*End of .+\*$", stripped):
            output_lines.extend(["", "[center][b]~ End ~[/b][/center]"]); stats["end_marker"] = True; i += 1; continue
        if is_pov_marker(stripped):
            output_lines.extend(["", f"[center][b]{stripped[2:-2]}[/b][/center]", ""]); stats["pov_markers"].append(stripped[2:-2]); i += 1; continue
        phone_m = is_phone_display(stripped)
        if phone_m:
            output_lines.extend(["", f"[center]───── [color=#4a9eff]📱 {phone_m.group(1).strip()}[/color] ─────[/center]", ""]); stats["text_messages"] += 1; i += 1; continue
        msg_m = is_text_message(stripped)
        if msg_m:
            s, m = msg_m.group(1).strip(), msg_m.group(2).strip()
            color = "#4a9eff" if "MAYA" in s.upper() else "#aab0bc"
            align = "right" if "MAYA" in s.upper() else "left"
            output_lines.append(f"[{align}][color={color}][b]{s}[/b]: {m}[/color][/{align}]"); stats["text_messages"] += 1; i += 1; continue
        if stripped == "": output_lines.append(""); i += 1; continue
        converted = format_paragraph_bbcode(stripped)
        output_lines.append(f"[center]{converted}[/center]" if in_warning_block else converted); stats["paragraphs"] += 1; i += 1

    return ConversionResult(output="\n".join(output_lines), format="bbcode", stats=stats)


def convert_to_sqw_chapters(markdown_text: str, warning_icon: str = "&#9888;") -> list[ConversionResult]:
    """Convert anchored MASTER.md to per-chapter SquidgeWorld body HTML.

    Returns a list of ConversionResult, one per chapter. Chapter 1 gets
    the full warning-page div; subsequent chapters get a bare title block.

    Requires anchored MASTER.md (returns empty list if no anchors).
    """
    fm = parse_front_matter(markdown_text)
    if fm is None:
        return []

    lines = markdown_text.split("\n")
    body_lines = lines[fm.body_start_line + 1:]

    # Detect chapters in the body section
    body_text = "\n".join(body_lines)
    chapters = detect_chapters(body_text)

    if not chapters:
        return []

    results: list[ConversionResult] = []

    for ch_idx, ch in enumerate(chapters):
        ch_lines = body_text.split("\n")[ch["line_start"]:ch["line_end"] + 1]
        # Convert body content (skip the chapter heading line itself)
        body_start = 1  # skip the # heading line
        while body_start < len(ch_lines) and ch_lines[body_start].strip() == "":
            body_start += 1

        body_parts, stats = _convert_body_clean_html(ch_lines, body_start)

        # Build the chapter HTML
        parts: list[str] = []

        if ch_idx == 0:
            # Chapter 1: full warning-page div
            parts.extend(render_front_matter_sqw(fm, ch["title"], warning_icon))
        else:
            # Chapter 2+: bare title block (no warning page)
            parts.append(f'<h1 class="story-title">{_escape_html(fm.title)}</h1>')
            if fm.byline:
                parts.append(f'<p class="byline">{_escape_html(fm.byline)}</p>')
            else:
                fallback = _default_author()
                if fallback:
                    parts.append(f'<p class="byline">by {_escape_html(fallback)}</p>')
            parts.append('<hr class="title-rule">')
            parts.append(f'<h2 class="chapter-subtitle">{_escape_html(ch["title"])}</h2>')

        parts.extend(body_parts)

        output = "\n".join(parts)
        results.append(ConversionResult(
            output=output,
            format="sqw",
            stats={"chapter_index": ch_idx, "chapter_title": ch["title"], **stats},
        ))

    return results


# ---------------------------------------------------------------------------
# Styled HTML — complete themed documents with embedded CSS
# ---------------------------------------------------------------------------

# Theme variables that every styled story needs.
STYLED_HTML_THEME_KEYS = [
    "BACKGROUND", "TEXT_COLOUR", "TITLE_COLOUR", "BYLINE_COLOUR",
    "ACCENT_COLOUR", "WARNING_HEADING_COLOUR", "WARNING_BODY_COLOUR",
    "DISCLAIMER_HEADING_COLOUR", "STORY_END_COLOUR", "SIGNATURE_COLOUR",
    "TEXT_SENT_COLOUR", "TEXT_RECEIVED_COLOUR",
    "TITLE_TEXT_SHADOW", "SECTION_BREAK_SYMBOL", "WARNING_ICON",
    "PRINT_APPROACH",
]


def parse_chapter_styling(text: str) -> dict:
    """Parse a CHAPTER_STYLING.md file and extract theme variables.

    Handles two source formats:
      1. Markdown table rows:  ``| Role | #hex | Description |``
      2. Inline code in bold labels:  ``**Warning icon:** `&#9888;` ``

    Returns a dict with the 14 STYLED_HTML_THEME_KEYS (values are strings).
    Missing keys are omitted — caller should validate completeness.
    """
    theme: dict = {}

    # --- Map table "Role" names to canonical variable names ---
    role_map = {
        "background": "BACKGROUND",
        "body text": "TEXT_COLOUR",
        "titles/headings": "TITLE_COLOUR",
        "title": "TITLE_COLOUR",
        "byline": "BYLINE_COLOUR",
        "secondary (byline, disclaimer)": "BYLINE_COLOUR",
        "accent": "ACCENT_COLOUR",
        "accent (headings, dividers)": "ACCENT_COLOUR",
        "warning heading": "WARNING_HEADING_COLOUR",
        "warning body": "WARNING_BODY_COLOUR",
        "disclaimer heading": "DISCLAIMER_HEADING_COLOUR",
        "disclaimer": "DISCLAIMER_HEADING_COLOUR",
        "story end": "STORY_END_COLOUR",
        "signature": "SIGNATURE_COLOUR",
        "section break": "SECTION_BREAK_COLOUR",  # extra, not one of the 14
    }

    for line in text.split("\n"):
        stripped = line.strip()

        # --- Markdown table rows: ``| Role | #hex | Description |`` ---
        if stripped.startswith("|") and stripped.count("|") >= 3:
            cells = [c.strip() for c in stripped.split("|")]
            # cells[0] is empty (before first |), cells[-1] may be empty
            cells = [c for c in cells if c]
            if len(cells) >= 2:
                role = cells[0].lower()
                value = cells[1]
                # Skip header rows
                if role in ("role", "---", "------") or value in ("hex", "---", "-----"):
                    continue
                canon = role_map.get(role)
                if canon and value.startswith("#"):
                    theme[canon] = value
                # Also match when column 1 IS the canonical variable name
                # (e.g. | `BACKGROUND` | `#0e1018` |)
                role_clean = role.strip("`").upper()
                value_clean = value.strip("`").strip()
                # Strip parenthetical descriptions like "(⚠)" or "(star outline)"
                value_clean = re.sub(r"\s*\(.*?\)\s*$", "", value_clean).strip().rstrip("`")
                if role_clean in STYLED_HTML_THEME_KEYS and value_clean:
                    theme[role_clean] = value_clean

        # --- Bold-label inline code: ``**Label:** `value` `` ---
        label_m = re.match(r"^\*\*(.+?):\*?\*?\s*`(.+?)`", stripped)
        if label_m:
            label = label_m.group(1).strip().lower()
            value = label_m.group(2).strip()
            label_map = {
                "section break": "SECTION_BREAK_SYMBOL",
                "warning icon": "WARNING_ICON",
                "print approach": "PRINT_APPROACH",
            }
            canon = label_map.get(label)
            if canon:
                theme[canon] = value

        # --- Top-level metadata: ``**Theme:** Name`` ---
        if stripped.lower().startswith("**theme:**"):
            theme["_THEME_NAME"] = stripped.split(":", 1)[1].strip().strip("*").strip()

        # --- Print approach from plain text ---
        if stripped.lower().startswith("**print approach:**"):
            pa = stripped.split(":", 1)[1].strip().strip("*").strip("`").strip()
            # Normalise: strip parenthetical notes like "(dark background)"
            pa = re.sub(r"\s*\(.*?\)\s*$", "", pa).strip()
            theme["PRINT_APPROACH"] = pa

        # --- Title text shadow (often in Typography or Structural section) ---
        if "text-shadow" in stripped.lower() and "title" in stripped.lower():
            shadow_m = re.search(r"(text-shadow:\s*[^;`]+)", stripped)
            if shadow_m:
                theme["TITLE_TEXT_SHADOW"] = shadow_m.group(1).rstrip(";").strip()

    # Derive defaults for variables that can be inferred
    if "DISCLAIMER_HEADING_COLOUR" not in theme and "TITLE_COLOUR" in theme:
        theme["DISCLAIMER_HEADING_COLOUR"] = theme["TITLE_COLOUR"]
    if "STORY_END_COLOUR" not in theme and "TITLE_COLOUR" in theme:
        theme["STORY_END_COLOUR"] = theme["TITLE_COLOUR"]
    if "SIGNATURE_COLOUR" not in theme and "WARNING_HEADING_COLOUR" in theme:
        theme["SIGNATURE_COLOUR"] = theme["WARNING_HEADING_COLOUR"]
    if "WARNING_BODY_COLOUR" not in theme and "BYLINE_COLOUR" in theme:
        theme["WARNING_BODY_COLOUR"] = theme["BYLINE_COLOUR"]
    if "PRINT_APPROACH" not in theme:
        theme["PRINT_APPROACH"] = "colour-preserve"
    if "TITLE_TEXT_SHADOW" not in theme:
        theme["TITLE_TEXT_SHADOW"] = ""
    if "WARNING_ICON" not in theme:
        theme["WARNING_ICON"] = "&#9888;"
    if "SECTION_BREAK_SYMBOL" not in theme:
        theme["SECTION_BREAK_SYMBOL"] = "* &ensp; * &ensp; *"

    return theme


def _build_print_styles(theme: dict) -> str:
    """Build the @media print CSS block based on PRINT_APPROACH.

    Returns the complete block including the @media wrapper, ready to
    replace ``{{PRINT_STYLES}}`` in the template.
    """
    approach = theme.get("PRINT_APPROACH", "colour-preserve")

    if approach == "grayscale":
        return (
            "/* Print Styles */\n"
            "        @media print {\n"
            "            @page {\n"
            "                margin: 0;\n"
            "                size: A4;\n"
            "            }\n"
            "\n"
            "            body {\n"
            "                background: white;\n"
            "                padding: 0;\n"
            "                font-size: 11pt;\n"
            "                max-width: none;\n"
            "                color: black;\n"
            "            }\n"
            "\n"
            "            .print-container {\n"
            "                padding: 2cm 2.5cm;\n"
            "                -webkit-box-decoration-break: clone;\n"
            "                box-decoration-break: clone;\n"
            "            }\n"
            "\n"
            "            .story-title {\n"
            "                font-size: 24pt;\n"
            "                color: black;\n"
            "            }\n"
            "\n"
            "            .title-rule,\n"
            "            .end-rule {\n"
            "                background: #333;\n"
            "            }\n"
            "\n"
            "            .section-break {\n"
            "                color: #333;\n"
            "                page-break-after: avoid;\n"
            "            }\n"
            "\n"
            "            .chapter-heading {\n"
            "                page-break-before: always;\n"
            "                color: black;\n"
            "            }\n"
            "\n"
            "            .chapter-heading:first-of-type {\n"
            "                page-break-before: avoid;\n"
            "            }\n"
            "\n"
            "            .chapter-divider {\n"
            "                background: #333;\n"
            "            }\n"
            "\n"
            "            p {\n"
            "                orphans: 3;\n"
            "                widows: 3;\n"
            "            }\n"
            "        }"
        )

    # colour-preserve (default)
    bg = theme.get("BACKGROUND", "#1a1118")
    text = theme.get("TEXT_COLOUR", "#e0d6cc")
    title = theme.get("TITLE_COLOUR", "#e8ddd0")
    accent = theme.get("ACCENT_COLOUR", "#8b2030")
    warn_h = theme.get("WARNING_HEADING_COLOUR", "#c4a040")
    warn_b = theme.get("WARNING_BODY_COLOUR", "#c8b8a8")
    disc_h = theme.get("DISCLAIMER_HEADING_COLOUR", title)
    sig = theme.get("SIGNATURE_COLOUR", warn_h)

    return (
        "/* Print Styles */\n"
        "        @media print {\n"
        "            @page {\n"
        "                margin: 0;\n"
        "                size: A4;\n"
        "            }\n"
        "\n"
        "            html {\n"
        "                -webkit-print-color-adjust: exact;\n"
        "                print-color-adjust: exact;\n"
        f"                background: {bg};\n"
        "            }\n"
        "\n"
        "            body {\n"
        f"                background: {bg};\n"
        "                padding: 0;\n"
        "                font-size: 11pt;\n"
        "                max-width: none;\n"
        f"                color: {text};\n"
        "            }\n"
        "\n"
        "            .print-container {\n"
        "                padding: 2cm 2.5cm;\n"
        "                -webkit-box-decoration-break: clone;\n"
        "                box-decoration-break: clone;\n"
        "            }\n"
        "\n"
        "            .story-title {\n"
        "                font-size: 24pt;\n"
        f"                color: {title};\n"
        "                text-shadow: none;\n"
        "            }\n"
        "\n"
        "            .title-rule,\n"
        "            .end-rule {\n"
        f"                background: {accent};\n"
        "            }\n"
        "\n"
        "            .warning-heading {\n"
        f"                color: {warn_h};\n"
        "            }\n"
        "\n"
        "            .warning-body,\n"
        "            .disclaimer-body {\n"
        f"                color: {warn_b};\n"
        "            }\n"
        "\n"
        "            .disclaimer-heading {\n"
        f"                color: {disc_h};\n"
        "            }\n"
        "\n"
        "            .section-break {\n"
        f"                color: {accent};\n"
        "                page-break-after: avoid;\n"
        "            }\n"
        "\n"
        "            .chapter-heading {\n"
        "                page-break-before: always;\n"
        f"                color: {title};\n"
        "            }\n"
        "\n"
        "            .chapter-heading:first-of-type {\n"
        "                page-break-before: avoid;\n"
        "            }\n"
        "\n"
        "            .chapter-divider {\n"
        f"                background: {accent};\n"
        "            }\n"
        "\n"
        "            .signature {\n"
        f"                color: {sig};\n"
        "            }\n"
        "\n"
        "            p {\n"
        "                orphans: 3;\n"
        "                widows: 3;\n"
        "            }\n"
        "        }"
    )


def _fill_template(template: str, theme: dict, fm: FrontMatter,
                    story_body_html: str) -> str:
    """Replace all ``{{PLACEHOLDER}}`` tokens in the HTML/CSS template.

    Handles the 14 colour variables, print styles, front-matter content,
    and the story body paragraphs.
    """
    doc = template

    # --- CSS colour variables ---
    simple_replacements = {
        "{{BACKGROUND}}": theme.get("BACKGROUND", "#1a1118"),
        "{{TEXT_COLOUR}}": theme.get("TEXT_COLOUR", "#e0d6cc"),
        "{{TITLE_COLOUR}}": theme.get("TITLE_COLOUR", "#e8ddd0"),
        "{{BYLINE_COLOUR}}": theme.get("BYLINE_COLOUR", "#b89a80"),
        "{{ACCENT_COLOUR}}": theme.get("ACCENT_COLOUR", "#8b2030"),
        "{{WARNING_HEADING_COLOUR}}": theme.get("WARNING_HEADING_COLOUR", "#c4a040"),
        "{{WARNING_BODY_COLOUR}}": theme.get("WARNING_BODY_COLOUR", "#c8b8a8"),
        "{{DISCLAIMER_HEADING_COLOUR}}": theme.get("DISCLAIMER_HEADING_COLOUR",
                                                     theme.get("TITLE_COLOUR", "#e8ddd0")),
        "{{STORY_END_COLOUR}}": theme.get("STORY_END_COLOUR",
                                           theme.get("TITLE_COLOUR", "#e8ddd0")),
        "{{SIGNATURE_COLOUR}}": theme.get("SIGNATURE_COLOUR",
                                           theme.get("WARNING_HEADING_COLOUR", "#c4a040")),
        "{{TEXT_SENT_COLOUR}}": theme.get("TEXT_SENT_COLOUR", "#508c46"),
        "{{TEXT_RECEIVED_COLOUR}}": theme.get("TEXT_RECEIVED_COLOUR",
                                               theme.get("ACCENT_COLOUR", "#8b2030")),
    }
    for placeholder, value in simple_replacements.items():
        doc = doc.replace(placeholder, value)

    # --- Title text shadow: insert the property or remove the line ---
    shadow = theme.get("TITLE_TEXT_SHADOW", "")
    if shadow:
        doc = doc.replace("{{TITLE_TEXT_SHADOW}}", shadow)
    else:
        # Remove the entire placeholder line (including leading whitespace)
        doc = re.sub(r"\n\s*\{\{TITLE_TEXT_SHADOW\}\}", "", doc)

    # --- Print styles ---
    doc = doc.replace("{{PRINT_STYLES}}", _build_print_styles(theme))

    # --- Chapter heading CSS (inject after the em rule) ---
    chapter_css = (
        "\n"
        "        /* Chapter Headings */\n"
        "        .chapter-heading {\n"
        "            text-align: center;\n"
        "            font-size: 1.6rem;\n"
        "            font-weight: bold;\n"
        "            letter-spacing: 0.08em;\n"
        f"            color: {theme.get('TITLE_COLOUR', '#e8ddd0')};\n"
        "            margin: 3rem 0 2rem;\n"
        "            font-variant: small-caps;\n"
        "        }\n"
        "\n"
        "        /* Chapter Dividers */\n"
        "        .chapter-divider {\n"
        "            width: 200px;\n"
        "            height: 1px;\n"
        f"            background: {theme.get('ACCENT_COLOUR', '#8b2030')};\n"
        "            margin: 3rem auto;\n"
        "            border: none;\n"
        "        }\n"
    )
    # Insert before the section-break rule
    section_break_marker = "        /* Section Breaks */"
    if section_break_marker in doc:
        doc = doc.replace(section_break_marker,
                          chapter_css + "\n" + section_break_marker)

    # --- Story content ---
    title_escaped = _escape_html(fm.title)
    # Strip "by " prefix — the template already includes "by {{AUTHOR_NAME}}"
    raw_byline = fm.byline or _default_author()
    if raw_byline.lower().startswith("by "):
        raw_byline = raw_byline[3:].strip()
    author = _escape_html(raw_byline)
    warning_icon = theme.get("WARNING_ICON", "&#9888;")

    doc = doc.replace("{{STORY_TITLE}}", title_escaped)
    doc = doc.replace("{{AUTHOR_NAME}}", author)
    doc = doc.replace("{{WARNING_ICON}}", warning_icon)

    # Warning text (may contain line breaks — join into one block)
    warning_text = _escape_html(fm.warning).replace("\n", "<br>\n            ")
    doc = doc.replace("{{CONTENT_WARNING_TEXT}}", warning_text)

    # Disclaimer text
    disclaimer_text = _escape_html(fm.disclaimer).replace("\n", "<br>\n            ")
    doc = doc.replace("{{DISCLAIMER_TEXT}}", disclaimer_text)

    # Fan fiction notice (optional extra disclaimers)
    if fm.fanfiction:
        ff_html = (
            "\n"
            '        <hr class="warning-divider">\n'
            "\n"
            '        <p class="disclaimer-heading">FAN FICTION NOTICE</p>\n'
            "\n"
            '        <p class="disclaimer-body">\n'
            f"            {_escape_html(fm.fanfiction)}\n"
            "        </p>"
        )
        doc = doc.replace("{{OPTIONAL_EXTRA_DISCLAIMERS}}", ff_html)
    else:
        doc = doc.replace("{{OPTIONAL_EXTRA_DISCLAIMERS}}", "")

    # Story body paragraphs
    doc = doc.replace("{{STORY_PARAGRAPHS}}", story_body_html)

    return doc


def _convert_body_styled_html(lines: list[str], start: int,
                               theme: dict) -> tuple[list[str], dict]:
    """Convert body lines to styled HTML elements.

    Produces:
      - ``<h2 class="chapter-heading">`` for chapter headings
      - ``<hr class="chapter-divider">`` before headings (except the first)
      - ``<div class="section-break">SYMBOL</div>`` for section breaks
      - ``<p>...</p>`` for paragraphs (using format_paragraph_html)
      - ``<div class="story-end">`` wrapper for the end marker
    """
    body_parts: list[str] = []
    stats: dict = {
        "chapters": [], "section_breaks": 0, "paragraphs": 0,
        "pov_markers": [], "text_messages": 0, "end_marker": False,
    }
    symbol = theme.get("SECTION_BREAK_SYMBOL", "* &ensp; * &ensp; *")
    I = "    "  # 4-space indent
    first_chapter_seen = False
    i = start
    current_paragraph: list[str] = []

    def flush():
        if current_paragraph:
            text = " ".join(current_paragraph)
            converted = format_paragraph_html(text)
            body_parts.append(f"{I}<p>{converted}</p>")
            stats["paragraphs"] += 1
            current_paragraph.clear()

    pending_semantic: str | None = None

    while i < len(lines):
        stripped = lines[i].strip()

        # Check for semantic anchors
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            sem = parse_semantic_anchor(stripped)
            if sem:
                flush()
                pending_semantic = sem
            i += 1
            continue

        # Section break or chapter divider
        if stripped == "---":
            flush()
            pending_semantic = None
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and lines[j].strip().startswith("# "):
                pass
            elif j < len(lines) and re.match(r"^\*End of .+\*$", lines[j].strip()):
                pass
            else:
                body_parts.append(f'{I}<div class="section-break">{symbol}</div>')
                stats["section_breaks"] += 1
            i += 1
            continue

        # Chapter heading
        if stripped.startswith("# "):
            flush()
            pending_semantic = None
            heading = _escape_html(stripped[2:].strip())
            if first_chapter_seen:
                body_parts.append("")
                body_parts.append(f'{I}<hr class="chapter-divider">')
            body_parts.append("")
            body_parts.append(f'{I}<h2 class="chapter-heading">{heading}</h2>')
            body_parts.append("")
            stats["chapters"].append(heading)
            first_chapter_seen = True
            i += 1
            continue

        # End marker
        if re.match(r"^\*End of .+\*$", stripped):
            flush()
            stats["end_marker"] = True
            i += 1
            continue

        # POV markers
        if is_pov_marker(stripped):
            flush()
            inner = stripped[2:-2]
            body_parts.append(f"{I}<p><strong>{_escape_html(inner)}</strong></p>")
            stats["pov_markers"].append(inner)
            i += 1
            continue

        # Semantic anchor: phone display
        if pending_semantic == "phone-incoming":
            flush()
            text = stripped.strip("*").strip()
            body_parts.append(
                f'{I}<div class="phone-display-wrap">'
                f'<div class="phone-display">{_escape_html(text)}</div></div>'
            )
            stats["text_messages"] += 1
            pending_semantic = None
            i += 1
            continue

        # Semantic anchor: text messages
        if pending_semantic in ("text-sent", "text-received"):
            flush()
            msg_class = "sent" if pending_semantic == "text-sent" else "received"
            msg_m = is_text_message(stripped)
            if msg_m:
                sender = msg_m.group(1).strip()
                message = msg_m.group(2).strip()
                body_parts.append(
                    f'{I}<div class="text-message {msg_class}">'
                    f"<strong>{_escape_html(sender)}</strong> "
                    f"{format_paragraph_html(message)}</div>"
                )
            else:
                body_parts.append(
                    f'{I}<div class="text-message {msg_class}">'
                    f"{format_paragraph_html(stripped)}</div>"
                )
            stats["text_messages"] += 1
            pending_semantic = None
            i += 1
            continue

        # Non-anchored heuristic fallbacks
        phone_m = is_phone_display(stripped)
        if phone_m:
            flush()
            caller = phone_m.group(1).strip()
            body_parts.append(
                f'{I}<div class="phone-display-wrap">'
                f'<div class="phone-display">{_escape_html(caller)}</div></div>'
            )
            stats["text_messages"] += 1
            i += 1
            continue

        msg_m = is_text_message(stripped)
        if msg_m:
            flush()
            sender = msg_m.group(1).strip()
            message = msg_m.group(2).strip()
            body_parts.append(
                f'{I}<div class="text-message">'
                f"<strong>{_escape_html(sender)}</strong>"
                f"{format_paragraph_html(message)}</div>"
            )
            stats["text_messages"] += 1
            i += 1
            continue

        # Blank line
        if stripped == "":
            flush()
            pending_semantic = None
            i += 1
            continue

        # Regular paragraph text
        current_paragraph.append(stripped)
        i += 1

    flush()
    return body_parts, stats


def convert_to_styled_html(
    markdown_text: str,
    theme: dict,
    template: str,
    *,
    mode: str = "full",
    chapter_index: int | None = None,
) -> ConversionResult | list[ConversionResult]:
    """Convert anchored MASTER.md to Styled HTML with embedded CSS.

    Parameters
    ----------
    markdown_text : str
        The full MASTER.md content.
    theme : dict
        The 14 colour variables (see STYLED_HTML_THEME_KEYS).
        Use parse_chapter_styling() to build this from a CHAPTER_STYLING.md.
    template : str
        The STYLING_REFERENCE.md HTML template (the ``<!DOCTYPE html>``
        block extracted from the code fence, or the raw template string).
    mode : str
        ``"full"`` — single document containing the entire story.
        ``"chapters"`` — list of per-chapter documents.
        ``"single_chapter"`` — one chapter only (requires chapter_index).
    chapter_index : int or None
        When mode is ``"single_chapter"``, which chapter (0-based).

    Returns
    -------
    ConversionResult or list[ConversionResult]
        For ``"full"`` and ``"single_chapter"`` modes, a single result.
        For ``"chapters"`` mode, a list of results (one per chapter).
    """
    fm = parse_front_matter(markdown_text)
    if fm is None:
        return ConversionResult(
            output="", format="styled_html",
            warnings=["No anchors found — styled HTML requires anchored MASTER.md"],
        )

    lines = markdown_text.split("\n")
    body_lines = lines[fm.body_start_line + 1:]

    # --- Extract the raw HTML template from the code fence if needed ---
    clean_template = _extract_html_template(template)

    if mode == "full":
        # Full-story single document
        body_parts, body_stats = _convert_body_styled_html(body_lines, 0, theme)
        body_html = "\n".join(body_parts)
        doc = _fill_template(clean_template, theme, fm, body_html)
        stats = {"title": fm.title, "subtitle": fm.subtitle, "mode": "full", **body_stats}
        return ConversionResult(output=doc, format="styled_html", stats=stats)

    # --- Per-chapter modes ---
    body_text = "\n".join(body_lines)
    chapters = detect_chapters(body_text)

    if not chapters:
        # No chapters detected — treat as single document
        body_parts, body_stats = _convert_body_styled_html(body_lines, 0, theme)
        body_html = "\n".join(body_parts)
        doc = _fill_template(clean_template, theme, fm, body_html)
        stats = {"title": fm.title, "subtitle": fm.subtitle, "mode": "full", **body_stats}
        return ConversionResult(output=doc, format="styled_html", stats=stats,
                                warnings=["No chapters detected — produced full document"])

    if mode == "single_chapter":
        if chapter_index is None or chapter_index < 0 or chapter_index >= len(chapters):
            return ConversionResult(
                output="", format="styled_html",
                warnings=[f"Invalid chapter_index={chapter_index}, "
                          f"story has {len(chapters)} chapters"],
            )
        return _build_styled_chapter(
            clean_template, theme, fm, body_text, chapters,
            chapter_index, include_warning_page=(chapter_index == 0),
        )

    # mode == "chapters"
    results: list[ConversionResult] = []
    for ch_idx in range(len(chapters)):
        result = _build_styled_chapter(
            clean_template, theme, fm, body_text, chapters,
            ch_idx, include_warning_page=(ch_idx == 0),
        )
        results.append(result)
    return results


def _build_styled_chapter(
    template: str, theme: dict, fm: FrontMatter,
    body_text: str, chapters: list[dict],
    ch_idx: int, include_warning_page: bool,
) -> ConversionResult:
    """Build a single styled HTML chapter document."""
    ch = chapters[ch_idx]
    is_last_chapter = ch_idx == len(chapters) - 1
    next_ch = chapters[ch_idx + 1] if not is_last_chapter else None
    ch_lines = body_text.split("\n")[ch["line_start"]:ch["line_end"] + 1]

    # Skip the chapter heading line itself (it's in the body handler)
    body_start = 0

    body_parts, body_stats = _convert_body_styled_html(ch_lines, body_start, theme)
    body_html = "\n".join(body_parts)

    if not include_warning_page:
        # For chapters 2+, strip the warning page from the template and
        # replace it with just the title + chapter heading
        doc = _fill_template(template, theme, fm, body_html)
        # Remove the warning-page div contents, replace with minimal header
        doc = _replace_warning_page_with_header(doc, fm, theme, ch["title"])
    else:
        doc = _fill_template(template, theme, fm, body_html)

    # The styling template hard-codes a `<div class="story-end">` "THE END"
    # block at the end of every document. For non-final chapter files that's
    # wrong — it claims the story ends here when there are more chapters
    # behind it. Replace with a per-chapter "End of X / Continued in Y"
    # block; only the actual last chapter keeps the THE END footer.
    if not is_last_chapter and next_ch is not None:
        doc = _replace_story_end_with_chapter_end(doc, ch["title"], next_ch["title"])

    # Update the <title> tag to include chapter name
    chapter_title = ch["title"]
    doc = doc.replace(
        f"<title>{_escape_html(fm.title)}</title>",
        f"<title>{_escape_html(fm.title)} - {_escape_html(chapter_title)}</title>",
    )

    stats = {
        "title": fm.title, "chapter_index": ch_idx,
        "chapter_title": chapter_title, "mode": "chapter",
        "is_last_chapter": is_last_chapter,
        **body_stats,
    }
    return ConversionResult(output=doc, format="styled_html", stats=stats)


def _replace_story_end_with_chapter_end(
    doc: str, ch_title: str, next_title: str,
) -> str:
    """Swap the template's static THE END block for a per-chapter footer.

    The styling template includes a fixed ``<div class="story-end">`` block
    that reads "THE END" plus the author signature. That belongs on the
    final chapter only — every earlier chapter file shows a stale
    "THE END" claim that contradicts the next file's existence.

    The replacement keeps the ``story-end`` class so the existing CSS
    (centring, padding, end-rule) still applies, and adds ``chapter-end``
    as a hook for any future per-chapter restyling.
    """
    se_start = doc.find('<div class="story-end">')
    if se_start == -1:
        return doc
    se_end = doc.find("</div>", se_start)
    if se_end == -1:
        return doc
    se_end += len("</div>")

    ch_title_esc = _escape_html(ch_title)
    next_title_esc = _escape_html(next_title)
    replacement = (
        '<div class="story-end chapter-end">\n'
        '        <hr class="end-rule">\n'
        f"        <p>End of {ch_title_esc}</p>\n"
        f'        <p class="signature">Continued in {next_title_esc}</p>\n'
        "    </div>"
    )
    return doc[:se_start] + replacement + doc[se_end:]


def _replace_warning_page_with_header(doc: str, fm: FrontMatter,
                                       theme: dict, chapter_title: str) -> str:
    """Replace the warning-page div with a minimal title header for chapters 2+."""
    # Find and replace the warning-page div
    wp_start = doc.find('<div class="warning-page">')
    wp_end = doc.find("</div>", wp_start)
    if wp_start == -1 or wp_end == -1:
        return doc
    # Find the closing </div> that matches the warning-page div
    wp_end += len("</div>")

    title_escaped = _escape_html(fm.title)
    raw_byline = fm.byline or _default_author()
    if raw_byline.lower().startswith("by "):
        raw_byline = raw_byline[3:].strip()
    author = _escape_html(raw_byline)
    header = (
        f'    <div class="warning-page" style="page-break-after:avoid;margin-bottom:2rem;padding-bottom:1rem">\n'
        f'        <h1 class="story-title">{title_escaped}</h1>\n'
        f'        <p class="byline">by {author}</p>\n'
        f'        <hr class="title-rule">\n'
        f'    </div>'
    )
    doc = doc[:wp_start] + header + doc[wp_end:]
    return doc


def _extract_html_template(template: str) -> str:
    """Extract the HTML template from a STYLING_REFERENCE.md or raw HTML.

    If the input contains a fenced code block (```html ... ```), extract
    the first one. Otherwise return the input as-is (assumed to be raw HTML).
    """
    if "<!DOCTYPE html>" in template and "```" not in template:
        return template  # already raw HTML

    # Extract from code fence
    m = re.search(r"```html\s*\n(<!DOCTYPE html>.*?)\n```", template, re.DOTALL)
    if m:
        return m.group(1)

    # Fallback: if it starts with <!DOCTYPE, use as-is
    stripped = template.strip()
    if stripped.startswith("<!DOCTYPE"):
        return stripped

    return template


def generate_styled_css(theme: dict, template: str) -> str:
    """Generate the standalone CSS file content from a theme + template.

    Extracts the <style> block from the filled template, strips the
    <style> tags, and returns pure CSS. This is the content of style.css
    that all Styled HTML files reference via <link>.
    """
    # Build a dummy document just to get the filled CSS
    dummy_fm = FrontMatter(title="__DUMMY__", warning="__DUMMY__", disclaimer="__DUMMY__")
    doc = _fill_template(_extract_html_template(template), theme, dummy_fm, "")

    # Extract the <style> block
    m = re.search(r"<style>(.*?)</style>", doc, re.DOTALL)
    if not m:
        return ""
    return m.group(1).strip()


@dataclass
class StyledHtmlOutput:
    """Output from the external-CSS styled HTML generator.

    Contains both the CSS (for style.css) and the HTML documents
    (which use <link href="style.css"> instead of embedded <style>).
    """
    css: str
    full_story: ConversionResult | None = None
    chapters: list[ConversionResult] = field(default_factory=list)


def convert_to_styled_html_external_css(
    markdown_text: str,
    theme: dict,
    template: str,
    *,
    mode: str = "full",
    css_href: str = "style.css",
) -> StyledHtmlOutput:
    """Generate Styled HTML with external CSS reference.

    Returns a StyledHtmlOutput with:
      - css: the standalone CSS file content
      - full_story or chapters: HTML documents using <link> instead of <style>

    The HTML documents reference the CSS via:
      <link rel="stylesheet" href="{css_href}">
    """
    # Generate the CSS
    css = generate_styled_css(theme, template)

    # Generate the HTML with embedded CSS (reuse existing function)
    if mode == "full":
        result = convert_to_styled_html(markdown_text, theme, template, mode="full")
        # Replace <style>...</style> with <link>
        html = _replace_style_with_link(result.output, css_href)
        output = StyledHtmlOutput(
            css=css,
            full_story=ConversionResult(output=html, format="styled_html", stats=result.stats),
        )
        return output

    # mode == "chapters"
    results = convert_to_styled_html(markdown_text, theme, template, mode="chapters")
    if isinstance(results, ConversionResult):
        results = [results]
    ch_outputs = []
    for r in results:
        html = _replace_style_with_link(r.output, css_href)
        ch_outputs.append(ConversionResult(output=html, format="styled_html", stats=r.stats))
    return StyledHtmlOutput(css=css, chapters=ch_outputs)


def _replace_style_with_link(html: str, css_href: str) -> str:
    """Replace the embedded <style>...</style> block with a <link> tag."""
    return re.sub(
        r"<style>.*?</style>",
        f'<link rel="stylesheet" href="{_escape_html(css_href)}">',
        html,
        count=1,
        flags=re.DOTALL,
    )


def convert(markdown_text: str, target_format: str, **kwargs) -> ConversionResult:
    """Convert markdown to the specified format.

    Supported formats: 'clean_html', 'sofurry_html', 'bbcode', 'styled_html'
    For SQW per-chapter output, use convert_to_sqw_chapters() directly.
    For styled HTML per-chapter output, use convert_to_styled_html() directly.

    Styled HTML requires additional kwargs:
      - theme: dict with the 14 colour variables
      - template: str with the HTML/CSS template
      - mode: str ('full', 'chapters', 'single_chapter')
      - chapter_index: int (for 'single_chapter' mode)
    """
    if target_format == "clean_html":
        return convert_to_clean_html(markdown_text)
    elif target_format == "sofurry_html":
        return convert_to_sofurry_html(markdown_text)
    elif target_format == "bbcode":
        return convert_to_bbcode(markdown_text)
    elif target_format == "styled_html":
        theme = kwargs.get("theme")
        template = kwargs.get("template")
        if not theme or not template:
            raise ValueError("styled_html format requires 'theme' and 'template' kwargs")
        result = convert_to_styled_html(
            markdown_text, theme, template,
            mode=kwargs.get("mode", "full"),
            chapter_index=kwargs.get("chapter_index"),
        )
        if isinstance(result, list):
            raise ValueError(
                "styled_html 'chapters' mode returns a list — "
                "use convert_to_styled_html() directly"
            )
        return result
    else:
        raise ValueError(f"Unsupported format: {target_format}")
