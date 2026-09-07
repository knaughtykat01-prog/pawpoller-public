"""Methods must live on their class, not inside a module-level function (4.14.4).

3.36.0 added three ``DAClient`` methods (``oauth_edit_deviation``, ``_eclipse_csrf_token``,
``napi_set_description``) by appending them to the END of ``clients/da/client.py`` with
class indentation — after the module-level ``_chunks()``. Python parsed them as functions
nested inside ``_chunks``, so ``DAClient`` never had them, and every DeviantArt image edit
from 3.36.0 to 4.14.3 raised ``AttributeError: 'DAClient' object has no attribute
'oauth_edit_deviation'`` on the live server. The unit tests never noticed because they
mock the client.

Two guards, so it cannot happen again in any client:

* no function that takes ``self`` may sit inside a module-level function anywhere in the
  code folders (a class defined inside a function is skipped — those are legitimate);
* every ``client.<name>(`` the DeviantArt platform calls must be an attribute of the real
  ``DAClient``.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FOLDERS = ("clients", "posting", "routes", "polling", "auth", "database", "utils")


def _nested_methods(func: ast.AST, path: Path, top: str, out: list[str]) -> None:
    for child in ast.iter_child_nodes(func):
        if isinstance(child, ast.ClassDef):
            continue                                   # a class inside a function is fine
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = child.args.args
            if args and args[0].arg == "self":
                out.append(f"{path.relative_to(REPO).as_posix()}:{child.lineno} "
                           f"{child.name}() is nested inside {top}() — indentation put it "
                           f"outside its class")
        _nested_methods(child, path, top, out)


def _orphaned_methods(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _nested_methods(node, path, node.name, out)
    return out


def _code_files():
    files = sorted(REPO.glob("*.py"))
    for folder in FOLDERS:
        d = REPO / folder
        if d.is_dir():
            files += sorted(d.rglob("*.py"))
    return [f for f in files if "__pycache__" not in f.parts]


def test_no_method_is_nested_inside_a_module_level_function():
    found: list[str] = []
    for path in _code_files():
        found += _orphaned_methods(path)
    assert not found, "\n".join(found)


def test_the_da_platform_only_calls_methods_the_real_client_has():
    from clients.da.client import DAClient

    src = (REPO / "posting" / "platforms" / "deviantart.py").read_text(encoding="utf-8")
    used = sorted(set(re.findall(r"\bclient\.(\w+)\(", src)))
    assert "oauth_edit_deviation" in used and "napi_set_description" in used
    missing = [name for name in used if not hasattr(DAClient, name)]
    assert not missing, f"DAClient lacks: {missing}"


@pytest.mark.parametrize("name", ["oauth_edit_deviation", "_eclipse_csrf_token", "napi_set_description"])
def test_the_three_editing_methods_are_on_daclient(name):
    from clients.da.client import DAClient
    assert callable(getattr(DAClient, name))
