"""ESCHELPER: Utils.html`…` escapes every value unless it's marked raw or is itself html``."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

UTILS = Path(__file__).resolve().parent.parent / "frontend" / "js" / "utils.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")


def _node(body: str):
    prelude = f"const fs = require('fs'); eval(fs.readFileSync({json.dumps(str(UTILS))}, 'utf8') + ';globalThis.Utils = Utils;');\n"
    r = subprocess.run(["node", "-e", prelude + body], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_values_escape_raw_and_nested_dont():
    out = _node("""
        const h = Utils.html, name = '<img onerror=x>"';
        const row = n => h`<li>${n}</li>`;
        console.log(JSON.stringify([
            String(h`<b title="${name}">${name}</b>`),
            String(h`<ul>${['a<', 'b'].map(row)}</ul>`),
            String(h`${Utils.raw('<i>ok</i>')}${null}${undefined}${false}${0}`),
            Utils.escapeHtml(5), Utils.escapeHtml(0), Utils.escapeHtml(null),
        ]));
    """)
    assert out == [
        '<b title="&lt;img onerror=x&gt;&quot;">&lt;img onerror=x&gt;&quot;</b>',
        "<ul><li>a&lt;</li><li>b</li></ul>",
        "<i>ok</i>0",
        "5", "0", "",
    ]
