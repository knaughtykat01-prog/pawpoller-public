"""Sounds (spec 049): Off by default, one per kind per second, a refused play is dropped.

`sound.js` runs under node against a fake Audio / localStorage / clock, so the real gate is tested.
The server half is the `news` flag on poll rows, which the bell plays its sound for.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from routes.api import _poll_news

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "frontend" / "js" / "sound.js").read_text(encoding="utf-8")
node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run(steps: str, store: dict | None = None, refuse: bool = False) -> dict:
    """Run `steps` (JS using Sound and `tick(ms)`) and return what played."""
    harness = """
    const store = %s; let now = 1e6; const played = [];
    global.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } };
    global.document = { currentScript: { src: 'http://x/js/sound.js?v=9.9.9' } };
    global.window = {};
    Date.now = () => now;
    const tick = ms => { now += ms; };
    global.Audio = function (src) {
      this.play = () => { played.push([src, this.volume]); return %s; };
    };
    %s
    const Sound = window.Sound;
    %s
    console.log(JSON.stringify({ played, store }));
    """ % (json.dumps(store or {}), "Promise.reject(new Error('blocked'))" if refuse else "Promise.resolve()",
           JS, steps)
    r = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@node
def test_off_by_default_plays_nothing():
    assert _run("Sound.play('post_up');")["played"] == []


@node
def test_key_moments_plays_once_per_kind_per_second():
    out = _run("""
        for (let i = 0; i < 10; i++) Sound.play('post_up');   // ten posts finish together
        Sound.play('error');                                    // another kind still plays
        tick(1000); Sound.play('post_up');                      // a second later it plays again
        Sound.play('no_such_sound');
    """, store={"pp-sound": "key", "pp-sound-vol": "40"})
    assert [p[0] for p in out["played"]] == [
        "/sounds/post_up.mp3?v=9.9.9", "/sounds/error.mp3?v=9.9.9", "/sounds/post_up.mp3?v=9.9.9"]
    assert out["played"][0][1] == 0.4


@node
def test_a_refused_play_is_dropped_not_thrown():
    out = _run("Sound.play('news');", store={"pp-sound": "key"}, refuse=True)
    assert len(out["played"]) == 1          # tried once, the rejection swallowed, nothing queued


@node
def test_settings_round_trip_and_clamp():
    out = _run("Sound.set({mode: 'key', volume: 250}); console.log(JSON.stringify(Sound.get()));")
    assert out["store"] == {"pp-sound": "key", "pp-sound-vol": "100"}
    out = _run("Sound.set({mode: 'loud'});")
    assert out["store"]["pp-sound"] == "off"


def test_poll_news_flags_faves_or_comments_only():
    assert _poll_news({"status": "success"}, {"views": 50, "faves": 2}) is True
    assert _poll_news({"status": "success"}, {"views": 50, "comments": 0}) is False
    assert _poll_news({"status": "partial", "new_comments_found": 1}, None) is True
    assert _poll_news({"status": "error", "new_faves_found": 3}, None) is False
