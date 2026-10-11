"""The first-run setup asks what you make and how you work (spec 032, SETUPREVAMP).

Static checks over the wizard source; the flow itself was walked in a browser.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
WIZ = APP[APP.index("async renderSetupWizard()"):APP.index("renderLogin() {")]
API_PY = (ROOT / "routes" / "api.py").read_text(encoding="utf-8")


def _orders():
    return [line for line in WIZ.splitlines() if "return ['welcome'" in line]


def test_the_new_steps_are_in_every_full_path_and_paired_skips_the_server_ones():
    full = [o for o in _orders() if "'platforms'" in o]
    paired = [o for o in _orders() if "'pairing'" in o]
    assert len(full) == 2 and len(paired) == 1
    for o in full:
        for step in ("'make'", "'sites'", "'interval'", "'posting'", "'hear'"):
            assert step in o, (step, o)
        assert o.index("'make'") < o.index("'archive'") < o.index("'sites'") < o.index("'platforms'")
    assert "'hear'" in paired[0] and "'sites'" not in paired[0] and "'interval'" not in paired[0]


def test_the_story_folder_is_only_asked_of_writers():
    assert "const keep = (s) => (s !== 'archive' || makesAll().includes('stories')) && !answered[s];" in WIZ
    assert all(o.rstrip().endswith(".filter(keep);") for o in _orders() if "'archive'" in o)


def test_the_interval_choices_are_ones_the_server_accepts():
    offered = {int(m) for m in re.findall(r"\$\{opt\((\d+),", WIZ)}
    assert offered == {60, 240, 720}
    allowed = re.search(r"\{(15, 30, 60, 120, 240, 360, 480, 600, 720)\}", API_PY)
    assert allowed and offered <= {int(x) for x in allowed.group(1).split(",")}


def test_every_theme_the_app_offers_is_one_the_server_saves():
    """Quill and Quill Dark (the default looks) were missing from the server's list, so choosing
    them never reached settings.json."""
    themes = set(re.findall(r"\{ id: '([a-z_0-9]+)',\s+name:", APP[APP.index("THEMES: ["):APP.index("applyTheme(themeId)")]))
    block = API_PY[API_PY.index('if "theme" in body:'):API_PY.index('update["theme"] = theme_val')]
    saved = set(re.findall(r'"([a-z_0-9]+)"', block)) - {"theme"}
    assert themes and themes <= saved, themes - saved


def test_only_track_and_skips_change_nothing():
    nxt = WIZ[WIZ.index("if (currentStep === 'posting' && answers.posts === 'post')"):][:400]
    assert "savePostingSettings" in nxt
    skip = WIZ[WIZ.index("document.getElementById('setup-skip')"):][:400]
    assert "answers.interval = null" in skip and "answers.posts = null" in skip


def test_under_18_installs_are_only_offered_general():
    assert "ageLocked() ? ['general'] : ['general', 'mature', 'adult']" in WIZ


def test_go_to_the_dashboard_marks_the_tour_seen():
    tour = (ROOT / "frontend" / "js" / "tour.js").read_text(encoding="utf-8")
    assert "skip };" in tour and "function skip(name)" in tour
    assert "if (!tour) window.Tour?.skip('getting-started');" in WIZ
    assert 'id="setup-finish-tour"' in WIZ
