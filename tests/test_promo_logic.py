"""Promo Maker v2 layout logic (4.15.0, docs/specs/promo_maker_v2.md §1–§2).

The tool draws on a <canvas>, so the interesting logic — where a word splits into
segments, how a highlight becomes one continuous run, how bold changes a justified
line, how a style toggle coalesces — lives in `frontend/js/promo.js`. These tests
drive the real module through **node** (the `test_search_query.py` pattern) with a
stub context whose `measureText` is deterministic: 10px per character, 12px per
character in bold. Nothing here needs a browser.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "frontend" / "js" / "promo.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is needed to exercise the JS layout")

PRELUDE = """
    const P = require(%s);
    // A canvas context stub: only what layout() touches.
    const ctx = {
        font: '',
        measureText(t) {
            const bold = /(^|\\s)700(\\s|$)/.test(this.font);
            return { width: t.length * (bold ? 12 : 10) };
        },
    };
    const base = () => ({
        version: 2, text: '', highlights: [], styles: [],
        size: { preset: 'custom', w: 1000, h: 1000 }, bg: 'blush', bgImage: null,
        font: { family: 'sans', px: 20 }, footer: '', story: null,
    });
    const dim = { w: 1000, h: 1000 };
""" % json.dumps(str(MODULE))


def _node(body: str) -> dict:
    script = textwrap.dedent(PRELUDE) + textwrap.dedent(body)
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_module_loads_outside_a_browser():
    out = _node("process.stdout.write(JSON.stringify({keys: Object.keys(P.SIZES), min: P.MIN_SIDE}))")
    assert out["keys"] == ["square", "portrait", "story", "custom"] and out["min"] == 320


def test_a_mid_word_highlight_cuts_the_word_into_segments():
    out = _node("""
        const s = base(); s.text = 'unbelievable';
        s.highlights = [{ start: 2, end: 8, color: '#f7b6d2' }];      // 'believ'
        const L = P.layout(ctx, s, dim);
        const segs = L.lines[0].segs.map(g => [g.text, !!g.hl]);
        process.stdout.write(JSON.stringify(segs));
    """)
    assert out == [["un", False], ["believ", True], ["able", False]]


def test_a_highlight_is_one_run_across_words_and_the_gap_between_them():
    out = _node("""
        const s = base(); s.text = 'right where it belongs';
        s.highlights = [{ start: 0, end: 22, color: '#f7b6d2' }];
        const L = P.layout(ctx, s, dim);
        const line = L.lines[0];
        const runs = P.runs(line, g => (g.hl && !g.hl.censor) ? g.hl : null);
        const first = line.segs[0], last = line.segs[line.segs.length - 1];
        process.stdout.write(JSON.stringify({
            n: runs.length, from: runs[0].from, to: runs[0].to,
            firstX: first.x, lastEnd: last.x + last.w, words: line.words.length }));
    """)
    assert out["n"] == 1 and out["words"] == 4
    assert out["from"] == out["firstX"] and out["to"] == out["lastEnd"]      # spans the gaps, not four rects


def test_a_run_stops_at_the_end_of_a_line_and_restarts_on_the_next():
    out = _node("""
        const s = base(); s.text = 'aaaaaaaaaa bbbbbbbbbb cccccccccc dddddddddd';
        s.highlights = [{ start: 0, end: 43, color: '#f7b6d2' }];
        const narrow = { w: 320, h: 1000 };                              // textW = 320 - 2*18 - 2*19 = 246 -> two 100px words per line
        const L = P.layout(ctx, s, narrow);
        const per = L.lines.map(l => P.runs(l, g => g.hl).length);
        process.stdout.write(JSON.stringify({ lines: L.lines.length, per }));
    """)
    assert out["lines"] == 2 and out["per"] == [1, 1]


def test_two_adjacent_highlights_of_different_colour_are_two_runs():
    out = _node("""
        const s = base(); s.text = 'one two';
        s.highlights = [{ start: 0, end: 3, color: '#a' }, { start: 4, end: 7, color: '#b' }];
        const L = P.layout(ctx, s, dim);
        const runs = P.runs(L.lines[0], g => g.hl);
        process.stdout.write(JSON.stringify(runs.map(r => r.key.color)));
    """)
    assert out == ["#a", "#b"]


def test_bold_widens_a_word_and_the_justified_line_still_fills_the_width():
    out = _node("""
        const s = base();
        s.text = 'alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau';
        const plain = P.layout(ctx, s, dim);
        s.styles = [{ start: 0, end: 5, b: true }];                      // 'alpha' bold
        const bold = P.layout(ctx, s, dim);
        const fill = line => {
            const last = line.segs[line.segs.length - 1];
            return Math.round(last.x + last.w);
        };
        process.stdout.write(JSON.stringify({
            plainW: plain.lines[0].words[0].w, boldW: bold.lines[0].words[0].w,
            textRight: Math.round(bold.cardX + bold.pad + bold.textW),
            fillPlain: fill(plain.lines[0]), fillBold: fill(bold.lines[0]),
            justify: bold.lines[0].justify }));
    """)
    assert out["plainW"] == 50 and out["boldW"] == 60
    assert out["justify"] is True
    assert out["fillBold"] == out["textRight"] == out["fillPlain"]         # slack recomputed from true widths


def test_unformatted_wrapping_is_unchanged_from_v1():
    """Same words per line as the 2.138.0 algorithm: greedy fill, ragged last line."""
    out = _node("""
        const s = base(); s.text = 'aaaaaaaaaa bbbbbbbbbb cccccccccc dddddddddd eeeeeeeeee';
        const L = P.layout(ctx, s, { w: 320, h: 1000 });                  // textW 246: two 100px words + a 10px gap per line
        process.stdout.write(JSON.stringify(L.lines.map(l => [l.words.map(w => w.text), l.justify])));
    """)
    assert out == [[["aaaaaaaaaa", "bbbbbbbbbb"], True], [["cccccccccc", "dddddddddd"], True], [["eeeeeeeeee"], False]]


def test_toggle_style_adds_then_removes_and_coalesces():
    out = _node("""
        let st = [];
        st = P.toggleStyle(st, 11, 'b', 0, 5);                            // bold 'hello'
        const a = JSON.parse(JSON.stringify(st));
        st = P.toggleStyle(st, 11, 'i', 3, 8);                            // italic across the boundary
        const b = JSON.parse(JSON.stringify(st));
        st = P.toggleStyle(st, 11, 'b', 0, 5);                            // every char bold -> remove
        const c = JSON.parse(JSON.stringify(st));
        st = P.toggleStyle(st, 11, 'i', 0, 8);                            // not all italic -> add to all
        const d = JSON.parse(JSON.stringify(st));
        process.stdout.write(JSON.stringify({ a, b, c, d }));
    """)
    assert out["a"] == [{"start": 0, "end": 5, "b": True, "i": False, "u": False, "s": False, "x": False}]
    assert [(r["start"], r["end"], r["b"], r["i"]) for r in out["b"]] == [(0, 3, True, False), (3, 5, True, True), (5, 8, False, True)]
    assert [(r["start"], r["end"], r["b"], r["i"]) for r in out["c"]] == [(3, 8, False, True)]
    assert [(r["start"], r["end"], r["i"]) for r in out["d"]] == [(0, 8, True)]


def test_underline_and_strike_runs_are_continuous_across_spaces():
    out = _node("""
        const s = base(); s.text = 'one two three';
        s.styles = [{ start: 0, end: 7, u: true }, { start: 8, end: 13, s: true }];
        const L = P.layout(ctx, s, dim);
        const u = P.runs(L.lines[0], g => g.st.u), k = P.runs(L.lines[0], g => g.st.s);
        process.stdout.write(JSON.stringify({ u: u.length, uTo: u[0].to, k: k.length,
            twoEnd: L.lines[0].segs.find(g => g.text === 'two').x + 30 }));
    """)
    assert out["u"] == 1 and out["k"] == 1
    assert out["uTo"] == out["twoEnd"]                                       # one underline under 'one two'


def test_add_highlight_trims_overlaps_instead_of_stacking():
    out = _node("""
        let h = [];
        h = P.addHighlight(h, 0, 10, { color: '#a' });
        h = P.addHighlight(h, 4, 6, { color: '#b' });
        process.stdout.write(JSON.stringify(h));
    """)
    assert [(r["start"], r["end"], r["color"]) for r in out] == [(0, 4, "#a"), (4, 6, "#b"), (6, 10, "#a")]


def test_a_scratched_out_word_keeps_its_highlight_underneath():
    """The reference card scribbles over a word that sits on a colour wash; the wash
    stays. So censor is a style flag (x), not a highlight that displaces the colour."""
    out = _node("""
        const s = base(); s.text = 'no underwear tonight';
        s.highlights = [{ start: 0, end: 20, color: '#p' }];
        s.styles = P.toggleStyle([], 20, 'x', 3, 12);                  // 'underwear'
        const L = P.layout(ctx, s, dim);
        const line = L.lines[0];
        const hl = P.runs(line, g => g.hl), x = P.runs(line, g => g.st.x);
        const seg = line.segs.find(g => g.text === 'underwear');
        process.stdout.write(JSON.stringify({ hl: hl.length, x: x.length, both: !!(seg.hl && seg.st.x),
            legacy: P.fromSpec({ version: 2, text: 'ab cd', highlights: [{ start: 0, end: 2, censor: true }, { start: 3, end: 5, color: '#q' }] }, null) }));
    """)
    assert out["hl"] == 1 and out["x"] == 1 and out["both"] is True          # one wash, one scribble, on the same word
    legacy = out["legacy"]
    assert legacy["highlights"] == [{"start": 3, "end": 5, "color": "#q"}]
    assert legacy["styles"] == [{"start": 0, "end": 2, "x": True}]


def test_custom_size_is_clamped_and_rounded():
    out = _node("""
        process.stdout.write(JSON.stringify([P.clampSize(100, 9000), P.clampSize(1200.6, NaN), P.clampSize(1080, 1350)]));
    """)
    assert out == [{"w": 320, "h": 4096}, {"w": 1201, "h": 1080}, {"w": 1080, "h": 1350}]


def test_footer_handle_insertion():
    out = _node("""
        process.stdout.write(JSON.stringify([
            P.footerWithHandle('', 'SecondFur'),
            P.footerWithHandle('Read now', '@SecondFur'),
            P.footerWithHandle('@old · Read now', 'SecondFur'),
        ]));
    """)
    assert out == ["@SecondFur", "@SecondFur · Read now", "@SecondFur · Read now"]


def test_first_line_of_each_paragraph_is_indented_and_still_justified_to_the_edge():
    out = _node("""
        const s = base();
        s.text = 'alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau\\nsecond para here';
        const L = P.layout(ctx, s, dim);
        const left = L.cardX + L.pad;
        const right = Math.round(left + L.textW);
        const ends = L.lines.map(l => { const last = l.segs[l.segs.length - 1]; return Math.round(last.x + last.w); });
        process.stdout.write(JSON.stringify({ indent: L.indent, x0: L.lines[0].segs[0].x - left, x1: L.lines[1].segs[0].x - left,
            firstEnd: ends[0], right, secondPara: L.lines[L.lines.length - 1].segs[0].x - left, justify0: L.lines[0].justify }));
    """)
    assert out["indent"] == 24                                              # 20px * 1.2
    assert out["x0"] == 24 and out["x1"] == 0                               # first line indented, the wrap is not
    assert out["justify0"] is True and out["firstEnd"] == out["right"]      # justified to the same right edge
    assert out["secondPara"] == 24                                          # every paragraph's first line


def test_smart_quotes_keep_offsets_and_open_or_close_by_position():
    out = _node("""
        const s = base(); s.text = '"What?" I don\\'t know.';
        s.highlights = [{ start: 0, end: 7, color: '#a' }];
        const L = P.layout(ctx, s, dim);
        process.stdout.write(JSON.stringify({ q: P.smartQuotes('"What?"'), a: P.smartQuotes("don't"), o: P.smartQuotes("'tis"),
            segs: L.lines[0].segs.map(g => g.text), hl: L.lines[0].segs.map(g => !!g.hl) }));
    """)
    assert out["q"] == "\u201cWhat?\u201d" and out["a"] == "don\u2019t" and out["o"] == "\u2018tis"
    assert out["segs"][0] == "\u201cWhat?\u201d" and out["hl"] == [True, False, False, False]


def test_node_check_parses_the_module():
    r = subprocess.run(["node", "--check", str(MODULE)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr

def test_pages_round_trip_through_the_spec():
    out = _node("""
        // a 4.16.0 single-card spec becomes one page; page 1 doubles as the flat shape
        const one = P.fromSpec({ version: 2, text: 'only page', highlights: [{ start: 0, end: 4, color: '#a' }], styles: [],
                                 size: { w: 1080, h: 1080 }, font: { family: 'lora', px: 60 } }, null);
        const multi = P.fromSpec({ version: 2, text: 'p1', highlights: [], styles: [], size: { w: 1080, h: 1080 },
                                   font: { family: 'lora', px: 60 },
                                   pages: [{ text: 'p1', highlights: [], styles: [] },
                                           { text: 'p2 words', highlights: [{ start: 0, end: 2, color: '#b' }], styles: [{ start: 3, end: 8, b: true }] }] }, null);
        const multiText = multi.text;               // read before the edits below mutate the same object
        P._state = multi;
        // Being on page 2 means the flat trio holds page 2 (what _gotoPage does without a DOM).
        P._state.page = 1;
        P._state.highlights = multi.pages[1].highlights; P._state.styles = multi.pages[1].styles;
        P._state.text = 'p2 words edited';          // an edit on the flat trio
        const spec = P.toSpec();
        process.stdout.write(JSON.stringify({
            onePages: one.pages.length, oneText: one.pages[0].text, oneHl: one.highlights.length,
            multiPages: multi.pages.length, multiText,
            specPages: spec.pages.map(p => p.text), specFlat: spec.text, specStyles: spec.pages[1].styles.length }));
    """)
    assert out["onePages"] == 1 and out["oneText"] == "only page" and out["oneHl"] == 1
    assert out["multiPages"] == 2 and out["multiText"] == "p1"
    assert out["specPages"] == ["p1", "p2 words edited"] and out["specFlat"] == "p1" and out["specStyles"] == 1


def test_zip_store_writes_an_archive_python_can_open(tmp_path):
    import zipfile
    target = tmp_path / "cards.zip"
    _node("""
        const fs = require('fs');
        const a = new TextEncoder().encode('first page bytes');
        const b = new Uint8Array([0x89, 0x50, 0x4E, 0x47, 0, 1, 2, 3]);
        const zip = P.zipStore([{ name: 'promo-1.png', data: a }, { name: 'promo-2.png', data: b }]);
        const again = P.zipStore([{ name: 'promo-1.png', data: a }, { name: 'promo-2.png', data: b }]);
        fs.writeFileSync(%s, Buffer.from(zip));
        process.stdout.write(JSON.stringify({ size: zip.length, deterministic: Buffer.compare(Buffer.from(zip), Buffer.from(again)) === 0 }));
    """ % json.dumps(str(target)))
    with zipfile.ZipFile(target) as z:
        assert z.namelist() == ["promo-1.png", "promo-2.png"]
        assert z.read("promo-1.png") == b"first page bytes"
        assert z.read("promo-2.png") == bytes([0x89, 0x50, 0x4E, 0x47, 0, 1, 2, 3])
        assert z.testzip() is None                                       # every CRC matches
        assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())
