"""Laurels reuses a load from the last 30 s (OVERVIEWSPEED): the achievement watcher's catch-up
re-downloaded everything the Laurels page had just fetched. Runs laurels.js under node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = (Path(__file__).resolve().parent.parent / "frontend" / "js" / "laurels.js").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_recent_load_is_reused_and_an_old_one_is_not():
    harness = """
    global.window = {}; global.document = { addEventListener(){}, readyState: 'complete' };
    let now = 1e6; Date.now = () => now;
    %s
    let fetches = 0;
    window.Laurels._loadFresh = async () => { fetches++; return { medals: [] }; };
    (async () => {
      await window.Laurels._load(); await window.Laurels._load();
      now += 31000; await window.Laurels._load();
      console.log(JSON.stringify(fetches));
    })();
    """ % JS
    r = subprocess.run(["node", "-"], input=harness, capture_output=True, text=True, encoding="utf-8", timeout=60)  # stdin: too long for a Windows command line
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == 2
