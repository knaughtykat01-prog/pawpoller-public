"""4.77.3: the moving guide is on Getting Started, and it ends with "Check your sites".

It lived only behind a Settings link, so a tester looking in Getting Started never found it;
and a move left DeviantArt (new address, browser cookie) and expired cookies for the tester
to discover one failure at a time.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent / "frontend" / "js" / "platform_guides.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_getting_started_shows_the_moving_guide():
    script = (
        "const app={innerHTML:''};"
        "global.window={PLATFORMS:[{code:'da',label:'DeviantArt'}]};"
        "global.document={getElementById:()=>app,addEventListener(){},querySelector:()=>null,"
        "querySelectorAll:()=>[],readyState:'complete'};"
        "eval(require('fs').readFileSync(" + json.dumps(str(JS)) + ",'utf8'));"
        "window.Guides.renderHub();console.log(app.innerHTML);"
    )
    html = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    assert 'data-guide="move"' in html and "Moving your PawPoller" in html
    assert html.index('data-guide="move"') < html.index('data-guide="da"'), "moving comes first"


def test_the_moving_guide_ends_with_checking_your_sites():
    src = JS.read_text(encoding="utf-8")
    move = src[src.index("    move: {"):src.index("    trello: {")]
    assert "Check your sites" in move and "Authorise posting" in move and "da_cookie" in move
