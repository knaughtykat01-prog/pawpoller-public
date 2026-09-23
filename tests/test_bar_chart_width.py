"""The Overview chart's Bar view drew nothing while Line drew fine (backlog BARCHART).

Chart.js sizes bars on a time axis from the SMALLEST interval between points, so a single
"check now" snapshot taken a minute after a scheduled one shrinks every bar in the chart to a
hairline — measured in a browser against the shipped Chart.js build: 0.06 px wide, i.e. an empty
chart, from one close pair in an otherwise evenly spaced month. `Charts._barThickness()` sizes
bars from the canvas and the number of readings instead, so the spacing of the readings can no
longer make them vanish.

The maths is a pure function, so node runs it directly; the rest is a source check in the style
of test_hash_names_and_tg_chart.py, since the browser code has no runner here.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

CHARTS = open("frontend/js/charts.js", encoding="utf-8").read()


class TestTheBarViewIsWired:
    def test_bar_datasets_carry_an_explicit_thickness(self):
        assert "barThickness," in CHARTS, "the dataset must pass barThickness through"
        assert "this._barThickness(snapshots.length, ctx.clientWidth)" in CHARTS

    def test_only_the_bar_view_sets_one(self):
        """Lines must stay untouched — undefined leaves Chart.js on its own sizing."""
        assert "isBar ? this._barThickness" in CHARTS


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
class TestTheThicknessMaths:
    @staticmethod
    def _run(cases):
        src = CHARTS + "\nconsole.log(JSON.stringify(%s.map(a => Charts._barThickness(a[0], a[1]))));" % json.dumps(cases)
        out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr[-500:]
        return json.loads(out.stdout)

    def test_bars_are_always_wide_enough_to_see(self):
        counts = [1, 2, 7, 30, 96, 400, 5000]
        widths = self._run([[c, 600] for c in counts])
        assert all(w >= 2 for w in widths), dict(zip(counts, widths))

    def test_bars_never_swallow_the_chart(self):
        assert all(w <= 24 for w in self._run([[1, 1600], [2, 1600], [30, 600]]))

    def test_a_canvas_that_has_not_been_laid_out_yet_still_gets_a_width(self):
        """Widgets mount before layout, so clientWidth can be 0 — it must not give 0-wide bars."""
        hidden, laid_out = self._run([[30, 0], [30, 600]])
        assert hidden == laid_out

    def test_more_readings_never_give_wider_bars(self):
        widths = self._run([[c, 600] for c in (5, 20, 50, 200)])
        assert widths == sorted(widths, reverse=True), widths
