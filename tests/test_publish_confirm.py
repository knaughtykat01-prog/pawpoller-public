"""The shared publish confirmation and per-platform results (4.1.0).

docs/specs/publish_flow.md §8.1 / §8.5 / §7.4. Six publish triggers had no
confirmation at all; 4.0.11 added the server guard, this is the client half.
The codebase had no reusable modal — app.js says so — so this is also the
first one, and the tests hold three things in place:

* every one of the six triggers awaits the dialog BEFORE it calls the API;
* the artwork surfaces report WHICH platforms failed instead of "2 ok, 3 failed";
* nothing re-renders the page on partial failure, which is what wiped the
  message line that would have carried the errors.
"""
from __future__ import annotations

import re


def _src(path):
    return open(path, encoding="utf-8").read()


def _fn(src, opener):
    """The body of one method/function, from its opener to the next top-level
    method at the same indent (good enough for order checks)."""
    i = src.index(opener)
    indent = src[src.rfind("\n", 0, i) + 1:i]
    m = re.compile(rf"\n{re.escape(indent)}(?:async )?(?:function )?[A-Za-z_]\w*\(").search(src, i + len(opener))
    return src[i:m.start() if m else len(src)]


# ── The component ─────────────────────────────────────────────

def test_the_dialog_exists_and_shows_a_list_not_a_sentence():
    src = _src("frontend/js/components.js")
    i = src.index("confirmPublish(o) {")
    block = src[i:i + 5000]
    for must in ("modal-overlay open", "pub-confirm-list", "pub-confirm-row", "data-pub-cancel", "data-pub-ok"):
        assert must in block, f"dialog lacks {must}"


def test_the_button_label_carries_the_count():
    """The count in the button is the last defence against a stale preset —
    Quick Publish restores its ticks from localStorage, unread."""
    src = _src("frontend/js/components.js")
    i = src.index("confirmPublish(o) {")
    block = src[i:i + 5000]
    assert "data-pub-ok" in block and "${esc(verb)} to ${n}" in block


def test_escape_and_backdrop_cancel_and_focus_starts_on_cancel():
    """Enter from a stale keypress must not publish."""
    src = _src("frontend/js/components.js")
    i = src.index("confirmPublish(o) {")
    block = src[i:i + 5000]
    assert "e.key === 'Escape'" in block
    assert "e.target === ov" in block
    assert "[data-pub-cancel]').focus()" in block


def test_no_typed_phrase_gate():
    """Deliberate. A phrase typed on every publish is a phrase nobody reads."""
    src = _src("frontend/js/components.js")
    i = src.index("confirmPublish(o) {")
    block = src[i:i + 5000]
    assert "prompt(" not in block


def test_results_panel_names_the_platform_and_the_error():
    src = _src("frontend/js/components.js")
    i = src.index("publishResults(results, opts) {")
    block = src[i:i + 2500]
    assert "r.error" in block and "pub-result is-fail" in block or "is-fail" in block
    assert "external_url" in block, "a success should link to the post"


def test_the_two_new_blocks_are_styled():
    css = _src("frontend/css/components.css")
    for cls in (".pub-confirm-list", ".pub-confirm-row", ".pub-results", ".pub-result.is-fail"):
        assert cls in css, f"{cls} unstyled"


# ── The six triggers ──────────────────────────────────────────

SIX = [
    ("frontend/js/artwork.js", "async _save(publish) {", "API.publishArtwork("),
    ("frontend/js/artwork.js", "async _publishMore(name) {", "API.publishArtwork("),
    ("frontend/js/artwork.js", "async _qpPublish(scheduledLocal) {", "API.publishArtwork("),
    ("frontend/js/masterpieces.js", "async _publishNow(name) {", "API.publishArtwork("),
    ("frontend/js/posts.js", "async _submit(scheduledLocal) {", "API.createPost("),
    ("frontend/js/publish_check.js", "async function _submitDrip() {", "fetch('/api/editor/stories/"),
]


def test_every_trigger_confirms_before_it_calls_the_api():
    """Order matters: a confirm AFTER the call is decoration."""
    for path, opener, call in SIX:
        body = _fn(_src(path), opener)
        assert "Components.confirmPublish(" in body, f"{path} {opener}: no confirmation"
        assert body.index("Components.confirmPublish(") < body.index(call), (
            f"{path} {opener}: confirmation comes AFTER the API call")


def test_posts_confirms_before_the_post_record_is_created():
    """createPost writes a local post row. Confirming after it would leave a
    stray draft in the feed on every cancel."""
    body = _fn(_src("frontend/js/posts.js"), "async _submit(scheduledLocal) {")
    assert body.index("Components.confirmPublish(") < body.index("API.createPost(")


def test_schedules_are_not_confirmed_but_drip_is():
    """A single schedule is reversible from the Queue page. A drip is chapters
    × platforms in one click — spec §10 Q4."""
    qp = _fn(_src("frontend/js/artwork.js"), "async _qpPublish(scheduledLocal) {")
    assert "if (!scheduledLocal" in qp[:qp.index("Components.confirmPublish(")] or \
        "!scheduledLocal &&" in qp, "Quick Publish must skip the dialog when scheduling"
    drip = _fn(_src("frontend/js/publish_check.js"), "async function _submitDrip() {")
    assert "verb: 'Schedule'" in drip


def test_masterpiece_confirms_before_it_writes_overrides_to_disk():
    """_applyOverrides writes tag overrides into masterpiece.json BEFORE
    posting. A cancel after that would have silently changed the record."""
    body = _fn(_src("frontend/js/masterpieces.js"), "async _publishNow(name) {")
    assert body.index("Components.confirmPublish(") < body.index("_applyOverrides(")


# ── Results, and no re-render on partial failure ──────────────

def test_artwork_surfaces_show_which_platforms_failed():
    for path, opener in (("frontend/js/artwork.js", "async _publishMore(name) {"),
                         ("frontend/js/artwork.js", "async _save(publish) {"),
                         ("frontend/js/artwork.js", "async _qpPublish(scheduledLocal) {"),
                         ("frontend/js/masterpieces.js", "async _publishNow(name) {")):
        body = _fn(_src(path), opener)
        assert "showPublishResults(" in body, f"{path} {opener} still discards results[]"


def test_no_unconditional_rerender_after_publish():
    """artwork.js:1245 called renderDetail(name) one line after the toast, so
    any error text was wiped before it could be read."""
    for path, opener in (("frontend/js/artwork.js", "async _publishMore(name) {"),
                         ("frontend/js/masterpieces.js", "async _publishNow(name) {")):
        body = _fn(_src(path), opener)
        i = body.index("showPublishResults(")
        after = body[i:]
        assert "if (!fail)" in after or "if (fail)" in after, (
            f"{path} {opener}: the re-render must be conditional on there being no failures")


def test_the_old_count_only_toast_is_gone_from_the_artwork_paths():
    """`Published: 2 ok, 3 failed` told the user nothing actionable."""
    for path in ("frontend/js/artwork.js", "frontend/js/masterpieces.js"):
        src = _src(path)
        assert "`Published: ${ok} ok, ${fail} failed`" not in src, f"{path} still uses the count-only toast"
