"""Regression guard: every platform's /auth/connect handler must call its
poller's ``_get_or_create_client`` with the right number of positional args.

The pollers were refactored to multi-account credential signatures
``_get_or_create_client(settings, <creds...>)`` but the route connect handlers
kept calling the old single-arg ``_get_or_create_client(overlay)`` form. That
produced a 500 ``TypeError: _get_or_create_client() missing N required
positional arguments`` on every connect attempt (caught in prod for X + Bluesky,
fixed across all 8 platforms in 2.37.1).

This test parses each route file's actual call and compares its positional-arg
count to the poller function's real signature — no network, no DB.
"""

import ast
import importlib
import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# platform -> (route file, poller module)
# NOTE: "da" is intentionally absent. As of 2.47.0 the DeviantArt connect
# handler validates with a throwaway DAClient instead of the shared
# _get_or_create_client singleton (avoids mutating the poll client mid-cycle —
# see the 2.47.0 security note), so it doesn't follow this wiring pattern.
PLATFORMS = {
    "ao3": ("routes/ao3_api.py", "polling.ao3_poller"),
    "bsky": ("routes/bsky_api.py", "polling.bsky_poller"),
    "ik": ("routes/ik_api.py", "polling.ik_poller"),
    "sf": ("routes/sf_api.py", "polling.sf_poller"),
    "sqw": ("routes/sqw_api.py", "polling.sqw_poller"),
    "tw": ("routes/tw_api.py", "polling.tw_poller"),
    "wp": ("routes/wp_api.py", "polling.wp_poller"),
}


def _connect_call_argcount(route_path: Path) -> int:
    """Return the positional-arg count of the _get_or_create_client(...) call."""
    tree = ast.parse(route_path.read_text(encoding="utf-8"))
    counts = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_get_or_create_client"
        ):
            assert not any(isinstance(a, ast.Starred) for a in node.args), (
                "unexpected *args in connect call"
            )
            counts.append(len(node.args))
    assert len(counts) == 1, (
        f"expected exactly one _get_or_create_client call in {route_path.name}, "
        f"found {len(counts)}"
    )
    return counts[0]


@pytest.mark.parametrize("platform", sorted(PLATFORMS))
def test_connect_call_matches_poller_signature(platform):
    route_rel, poller_mod = PLATFORMS[platform]
    poller = importlib.import_module(poller_mod)
    sig = inspect.signature(poller._get_or_create_client)
    required = [
        p
        for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    expected = len(required)
    actual = _connect_call_argcount(ROOT / route_rel)
    assert actual == expected, (
        f"{platform}: connect handler passes {actual} positional args to "
        f"_get_or_create_client but the poller requires {expected} "
        f"({[p.name for p in required]})"
    )
