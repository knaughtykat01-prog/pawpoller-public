"""Editor / converter tests.

10 read-only tests covering markdown → BBCode / Clean HTML / SoFurry
HTML / SquidgeWorld / Styled HTML (full + chapters) / EPUB, plus PDF
backend availability, theme parser, anchor parser. Uses a baked-in
fixture so no live story files are needed.
"""

from __future__ import annotations

import textwrap

from testing.registry import TestContext, register_test


# A minimal multi-chapter fixture exercising the anchor system, POV
# markers, text-message anchors, and the trailing end-marker.
_FIXTURE = textwrap.dedent("""\
    <!-- @title -->
    # Diagnostic Story

    <!-- @warning -->
    **Content Warning**: synthetic fixture, no real content.

    <!-- @disclaimer -->
    **DISCLAIMER**

    Fictional fixture used only by Diagnostics.

    <!-- @body -->

    ---
    # Chapter 1: First

    *The room hummed.* He listened.

    **⟨ Marcus ⟩**

    *Across the floor, the silence stretched.*

    ---
    # Chapter 2: Second

    *Morning came.*

    <!-- @text-sent -->
    *Marcus: you up?*

    <!-- @text-received -->
    *Kai: yeah*

    *End of Diagnostic Story*
""")


_STYLING_TEMPLATE = textwrap.dedent("""\
    ```html
    <!DOCTYPE html>
    <html>
    <head>
        <link rel="stylesheet" href="style.css">
        <title>{{STORY_TITLE}}</title>
        <style>body { background: {{BACKGROUND}}; color: {{TEXT_COLOUR}}; }</style>
    </head>
    <body>
        <div class="warning-page">
            <h1 class="story-title">{{STORY_TITLE}}</h1>
            <p class="byline">by {{AUTHOR_NAME}}</p>
            <p class="disclaimer-body">{{CONTENT_WARNING_TEXT}}</p>
            <p class="disclaimer-body">{{DISCLAIMER_TEXT}}</p>
            {{OPTIONAL_EXTRA_DISCLAIMERS}}
        </div>
        <div class="content">{{STORY_BODY}}</div>
        <div class="story-end">
            <hr class="end-rule">
            <p>THE END</p>
            <p class="signature">~ {{AUTHOR_NAME}} ~</p>
        </div>
    </body>
    </html>
    ```
""")


_CHAPTER_STYLING = textwrap.dedent("""\
    <!-- THEME_VARIABLES_START -->
    | Variable | Value |
    |---|---|
    | BACKGROUND | #111 |
    | TEXT_COLOUR | #eee |
    | TITLE_COLOUR | #fff |
    | BYLINE_COLOUR | #aaa |
    | ACCENT_COLOUR | #f80 |
    | WARNING_HEADING_COLOUR | #f44 |
    | WARNING_BODY_COLOUR | #ccc |
    | DISCLAIMER_HEADING_COLOUR | #ccc |
    | STORY_END_COLOUR | #aaa |
    | SIGNATURE_COLOUR | #f80 |
    | TEXT_SENT_COLOUR | #4c4 |
    | TEXT_RECEIVED_COLOUR | #f80 |
    | TITLE_TEXT_SHADOW | 0 0 25px rgba(0,0,0,0.5) |
    | SECTION_BREAK_SYMBOL | `* * *` |
    | WARNING_ICON | `&#9888;` |
    <!-- THEME_VARIABLES_END -->
""")


# ── Converter outputs ────────────────────────────────────────────────


@register_test(
    test_id="editor.converter.clean_html",
    name="Markdown → Clean HTML",
    category="Editor / Converter",
    description="Convert the fixture and verify expected HTML structure.",
)
async def t_clean_html(ctx: TestContext) -> None:
    from editor.converter import convert_to_clean_html

    result = convert_to_clean_html(_FIXTURE)
    out = result.output
    ctx.detail("bytes", len(out))
    ctx.detail("chapter_count", len(result.stats.get("chapters", [])))
    assert "<p>" in out, "no <p> tags emitted"
    assert "<em>" in out or "<i>" in out, "no italics emitted from *text*"
    assert "Marcus" in out, "POV marker dropped"


@register_test(
    test_id="editor.converter.sofurry_html",
    name="Markdown → SoFurry HTML",
    category="Editor / Converter",
    description="SF-specific HTML (h2/h3, text-center divs).",
)
async def t_sofurry_html(ctx: TestContext) -> None:
    from editor.converter import convert_to_sofurry_html

    result = convert_to_sofurry_html(_FIXTURE)
    out = result.output
    ctx.detail("bytes", len(out))
    assert "text-center" in out or "<h2" in out, "no SF-specific markup present"


@register_test(
    test_id="editor.converter.bbcode",
    name="Markdown → BBCode",
    category="Editor / Converter",
    description="BBCode output with [i] italics.",
)
async def t_bbcode(ctx: TestContext) -> None:
    from editor.converter import convert_to_bbcode

    result = convert_to_bbcode(_FIXTURE)
    out = result.output
    ctx.detail("bytes", len(out))
    assert "[i]" in out and "[/i]" in out, "no [i] italic tags found"


@register_test(
    test_id="editor.converter.sqw_chapters",
    name="Markdown → SquidgeWorld per-chapter",
    category="Editor / Converter",
    description="Splits the fixture into N chapter HTML documents.",
)
async def t_sqw_chapters(ctx: TestContext) -> None:
    from editor.converter import convert_to_sqw_chapters

    results = convert_to_sqw_chapters(_FIXTURE)
    ctx.detail("chapter_count", len(results))
    assert len(results) == 2, f"expected 2 chapters, got {len(results)}"
    for r in results:
        assert r.output, "empty chapter output"


@register_test(
    test_id="editor.converter.styled_html_full",
    name="Markdown → Styled HTML (full story)",
    category="Editor / Converter",
    description="Full-story render with theme tokens + template.",
)
async def t_styled_full(ctx: TestContext) -> None:
    from editor.converter import (
        convert_to_styled_html_external_css,
        parse_chapter_styling,
    )

    theme = parse_chapter_styling(_CHAPTER_STYLING)
    out = convert_to_styled_html_external_css(
        _FIXTURE, theme, _STYLING_TEMPLATE, mode="full", css_href="style.css"
    )
    assert out.full_story is not None, "no full_story returned"
    html = out.full_story.output
    ctx.detail("bytes", len(html))
    assert "Diagnostic Story" in html
    assert 'class="story-end"' in html, "story-end block missing"
    # Last chapter still keeps THE END (full mode)
    assert "THE END" in html


@register_test(
    test_id="editor.converter.styled_chapters_end_marker",
    name="Styled HTML: THE END only on last chapter (regression 2.18.20)",
    category="Editor / Converter",
    description=(
        "Verifies the chapter-end footer fix: per-chapter Styled HTML "
        "shows 'Continued in …' on non-final chapters and 'THE END' "
        "only on the last."
    ),
)
async def t_styled_chapter_end_marker(ctx: TestContext) -> None:
    from editor.converter import (
        convert_to_styled_html_external_css,
        parse_chapter_styling,
    )

    theme = parse_chapter_styling(_CHAPTER_STYLING)
    out = convert_to_styled_html_external_css(
        _FIXTURE, theme, _STYLING_TEMPLATE, mode="chapters", css_href="style.css"
    )
    assert len(out.chapters) == 2, f"expected 2 chapters, got {len(out.chapters)}"
    first = out.chapters[0].output
    last = out.chapters[-1].output
    ctx.detail("first_has_continued", "Continued in" in first)
    ctx.detail("first_has_the_end", "THE END" in first)
    ctx.detail("last_has_the_end", "THE END" in last)
    assert "Continued in" in first, "non-final chapter missing 'Continued in'"
    assert "THE END" not in first, "non-final chapter still has THE END"
    assert "THE END" in last, "final chapter missing THE END"


# ── EPUB ─────────────────────────────────────────────────────────────


@register_test(
    test_id="editor.epub.generator",
    name="EPUB structural build",
    category="Editor / Converter",
    description="Build an EPUB and assert mimetype + container.xml + chapter spine present.",
)
async def t_epub_generator(ctx: TestContext) -> None:
    import json
    import tempfile
    import zipfile
    from pathlib import Path

    try:
        from editor.epub_generator import build_epub
    except ImportError:
        raise ctx.skip("epub_generator not available")

    # build_epub expects a story directory with Markdown/MASTER.md.
    # Synthesize one in a temp dir from the fixture.
    with tempfile.TemporaryDirectory() as tmp:
        story_dir = Path(tmp) / "Diagnostic_Story"
        (story_dir / "Markdown").mkdir(parents=True)
        (story_dir / "Markdown" / "MASTER.md").write_text(_FIXTURE, encoding="utf-8")
        (story_dir / "story.json").write_text(
            json.dumps({"title": "Diagnostic Story", "author": "Tester"}),
            encoding="utf-8",
        )
        out_path = build_epub(story_dir)
        epub_path = Path(out_path)
        assert epub_path.is_file(), f"epub not produced at {epub_path}"
        size = epub_path.stat().st_size
        ctx.detail("bytes", size)
        assert size > 1000, "EPUB suspiciously small"
        with zipfile.ZipFile(epub_path) as z:
            names = z.namelist()
            ctx.detail("file_count", len(names))
            assert "mimetype" in names, "mimetype missing"
            assert "META-INF/container.xml" in names, "container.xml missing"
            container = z.read("META-INF/container.xml").decode("utf-8")
            assert "rootfile" in container and ".opf" in container, "container.xml malformed"


# ── PDF backend ──────────────────────────────────────────────────────


@register_test(
    test_id="editor.pdf.backend_available",
    name="PDF backend available",
    category="Editor / Converter",
    description="get_backend() returns weasyprint or edge (not 'none').",
)
async def t_pdf_backend(ctx: TestContext) -> None:
    from editor.pdf_generator import get_backend

    backend = get_backend()
    ctx.detail("backend", backend)
    assert backend in ("weasyprint", "edge"), f"no PDF backend available: {backend}"


# ── Theme parser ─────────────────────────────────────────────────────


@register_test(
    test_id="editor.theme.parser",
    name="CHAPTER_STYLING.md parser",
    category="Editor / Converter",
    description="parse_chapter_styling returns all configured theme keys.",
)
async def t_theme_parser(ctx: TestContext) -> None:
    from editor.converter import parse_chapter_styling

    theme = parse_chapter_styling(_CHAPTER_STYLING)
    ctx.detail("key_count", len(theme))
    expected = {
        "BACKGROUND",
        "TEXT_COLOUR",
        "TITLE_COLOUR",
        "BYLINE_COLOUR",
        "ACCENT_COLOUR",
        "SIGNATURE_COLOUR",
    }
    missing = expected - set(theme.keys())
    assert not missing, f"missing theme keys: {missing}"


# ── Anchor parser ────────────────────────────────────────────────────


@register_test(
    test_id="editor.anchors.parser",
    name="Anchor parser (front-matter sections)",
    category="Editor / Converter",
    description="parse_front_matter pulls title / warning / disclaimer fields.",
)
async def t_anchor_parser(ctx: TestContext) -> None:
    from editor.converter import parse_front_matter

    fm = parse_front_matter(_FIXTURE)
    assert fm is not None, "parse_front_matter returned None"
    ctx.detail("title", fm.title)
    ctx.detail("warning_len", len(fm.warning) if fm.warning else 0)
    ctx.detail("disclaimer_len", len(fm.disclaimer) if fm.disclaimer else 0)
    assert fm.title == "Diagnostic Story"
    assert fm.warning, "warning anchor not parsed"
    assert fm.disclaimer, "disclaimer anchor not parsed"
