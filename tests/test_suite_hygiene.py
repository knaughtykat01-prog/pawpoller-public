"""The suite stays honest and quick: no new fixed waits, and nothing that hides a flaky test.

Ideas taken from Landmines (an Oxlint rule set for JS tests: `no-fixed-sleep`, `no-flake-masking`), written
for pytest. A fixed wait (`time.sleep(2)`, `page.wait_for_timeout(500)`) is either too long, and the suite pays
it on every run, or too short on a slow runner, and the test flakes. Wait on the thing itself instead:
`page.wait_for_selector` / `wait_for_function`, a `threading.Event`, or a loop with a deadline.

The waits already in the suite are frozen per file in BASELINE: a file may have fewer, never more. When a
merge brings a file in over its count, the failure names the file and its count: write that in by hand.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
FIXED_WAIT = re.compile(r"(?<![\w.])(?:time\.|asyncio\.)?sleep\(\s*[\d.]+\s*\)|\.wait_for_timeout\(\s*\d")
# A retry plugin, a rerun flag or marker, or a CI step allowed to fail: each turns a real failure green.
MASKING = re.compile(r"pytest-rerunfailures|pytest_rerunfailures|--reruns\b|mark\.flaky\(|continue-on-error:\s*true")

BASELINE = {
    "test_a11y_js.py": 7,
    "test_accessibility_axe.py": 10,
    "test_account_mail.py": 2,
    "test_activity.py": 3,
    "test_alert_consistency.py": 1,
    "test_ao3_current_browser.py": 1,
    "test_chapter_after_preview_only.py": 1,
    "test_countask.py": 3,
    "test_frontend_runs.py": 12,
    "test_homebox_agent.py": 1,
    "test_metadata_strip.py": 1,
    "test_move.py": 3,
    "test_move_direct.py": 1,
    "test_patreon.py": 1,
    "test_pb_upload.py": 1,
    "test_platform_posters.py": 1,
    "test_polling_ownership_gate.py": 1,
    "test_proxy_trust.py": 1,
    "test_seat_pacing.py": 2,
    "test_signin_codes.py": 1,
    "test_standalone_bridge.py": 1,
    "test_update_gate.py": 2,
}


def _counts() -> dict[str, int]:
    out = {}
    for f in sorted(TESTS.glob("test_*.py")):
        if f.name == Path(__file__).name:
            continue
        n = len(FIXED_WAIT.findall(f.read_text(encoding="utf-8")))
        if n:
            out[f.name] = n
    return out


def test_no_new_fixed_waits():
    over = {f: (n, BASELINE.get(f, 0)) for f, n in _counts().items() if n > BASELINE.get(f, 0)}
    assert not over, ("new fixed waits (file: now, allowed). Wait on a selector, a function, an Event or a "
                      "deadline loop instead: " + ", ".join(f"{f}: {n}, {b}" for f, (n, b) in over.items()))


def test_nothing_hides_a_flaky_test():
    files = [*TESTS.glob("*.py"), ROOT / "pytest.ini", *ROOT.glob("requirements*.txt"), *(ROOT / ".github" / "workflows").glob("*.yml")]
    hits = [f"{f.relative_to(ROOT)}:{i}" for f in files if f.exists() and f.name != Path(__file__).name
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1) if MASKING.search(line)]
    assert not hits, "a retry, rerun or allowed failure would turn a real failure green: " + ", ".join(hits)
