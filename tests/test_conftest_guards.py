"""The suite's own safety guards — that they are CODE, not prose.

`tests/conftest.py` sets a handful of environment variables whose whole job is to
stop the suite from touching anything real: the Tech Centre client must never send
a report anywhere, and must not start its sender thread when `dashboard.py` is
imported.

⚠ Why this file exists. That block spent a while sitting INSIDE the `_db_template`
fixture's docstring, where it was inert text rather than executable code — so for
every run in that window neither variable was set and the guard did nothing, while
reading in the source exactly as though it did. Nothing failed, because a guard
that is silently absent looks identical to one that is working right up until the
moment it matters.

A comment cannot detect that. These asserts can.
"""
from __future__ import annotations

import ast
import os


def test_the_tech_centre_url_is_neutralised():
    """Empty URL disables the client outright."""
    assert os.environ.get("PAWPOLLER_TECH_CENTRE_URL") == ""


def test_the_tech_centre_sender_thread_is_off():
    assert os.environ.get("PAWPOLLER_TECH_CENTRE_THREAD") == "0"


def test_the_suite_is_in_test_mode():
    assert os.environ.get("PAWPOLLER_TEST_MODE") == "1"


def test_the_vault_key_is_a_throwaway():
    """Set by conftest so _get_vault_key() never reaches the real OS keyring."""
    assert os.environ.get("PAWPOLLER_VAULT_KEY")


def test_config_paths_point_at_a_temp_dir():
    """The vault is always-on — save_settings() writes VAULT_PATH on every save, so
    an un-redirected path would clobber the operator's real one."""
    import tempfile
    import config
    # Any temp root will do — conftest seeds one and the per-test fixture redirects
    # again to pytest's tmp_path. What matters is that none of them is a real file.
    tmp = os.path.realpath(tempfile.gettempdir()).lower()
    for p in (config.DB_PATH, config.SETTINGS_PATH, config.VAULT_PATH):
        assert os.path.realpath(str(p)).lower().startswith(tmp), \
            f"{p} is not inside a temp directory"


def test_the_guards_are_executable_statements_not_docstring_text():
    """The actual regression. Parse conftest and confirm the setdefault calls are
    real module-level statements — a string that merely contains them is not a guard.
    """
    src = open("tests/conftest.py", encoding="utf-8").read()
    tree = ast.parse(src)
    found = set()
    for node in tree.body:                      # module level only
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Attribute) and call.func.attr == "setdefault"):
            continue
        if call.args and isinstance(call.args[0], ast.Constant):
            found.add(call.args[0].value)
    assert "PAWPOLLER_TECH_CENTRE_URL" in found, \
        "the Tech Centre guard is not a module-level statement"
    assert "PAWPOLLER_TECH_CENTRE_THREAD" in found


def test_no_stray_code_is_buried_in_a_docstring():
    """The shape of the original mistake: an import line indented inside a triple
    quote. Cheap to check, and it would have caught it on the first run."""
    src = open("tests/conftest.py", encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        doc = ast.get_docstring(node) if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else None
        if not doc:
            continue
        for line in doc.splitlines():
            stripped = line.strip()
            assert not stripped.startswith(("import ", "from ")), \
                f"a docstring contains what looks like code: {stripped!r}"
            assert "os.environ" not in stripped, \
                f"a docstring contains what looks like a guard: {stripped!r}"


# ── Fixes from the 4.34.4 security review ───────────────────────────────────

def test_the_relaunch_script_is_not_written_to_the_shared_temp_dir():
    """The updater writes a script, chmods it 0755 and runs it. In /tmp that name is
    predictable, and another local user can pre-create it as a symlink for us to
    overwrite and mark executable.

    ⚠ This test exists because the review noted the fix had none: the one line the
    fix changed was the one line nothing asserted, so a refactor could put it back
    in /tmp silently. The shlex.quote convention got a test after it regressed once;
    this is the same lesson applied before it regresses.
    """
    src = open("updater.py", encoding="utf-8").read()
    i = src.index("def spawn_relauncher")
    block = src[i:i + 4000]
    assert "config.APPDATA_DIR" in block, "the script must land in a user-owned dir"
    assert "tempfile.gettempdir()" not in block, "back in the shared temp dir"


def test_the_channel_routes_are_gated_on_an_unconfigured_instance():
    """A poll needs a question and two options and nothing else -- no story, no
    publication, no upload. On an instance with no dashboard password that is a
    remote caller broadcasting under the operator's own brand, irreversibly, and it
    is cheaper to reach than any other publish endpoint in the app."""
    src = open("dashboard.py", encoding="utf-8").read()
    i = src.index("_SENSITIVE_WHEN_OPEN_PREFIXES = (")
    block = src[i:src.index(")", i)]
    assert '"/api/tg/channel"' in block
