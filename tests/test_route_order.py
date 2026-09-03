"""Per-submission snapshot endpoints must reach their own handler.

Starlette matches routes in **registration order**, and the ``:path`` converter
is greedy — it happily matches a value containing slashes. So a router that
registers ``/submissions/{submission_id:path}`` before
``/submissions/{submission_id:path}/snapshots`` sends every snapshot request to
the DETAIL handler with a submission id of ``"<id>/snapshots"``, which is not a
post, so it answers 404.

Nine platforms shipped that way — bsky, e621, fbr, fn, ig, mast, pix, thr and
tum, every one of the routers that uses ``:path`` — and the failure is invisible
from the code: both routes exist, both are correct, and the only symptom is that
a submission's history chart is permanently empty on those platforms while
working on the ones whose ids are plain integers. Fixed in 4.0.10 by moving the
more specific route above the greedy one.

This test is deliberately end-to-end through the real app rather than an
inspection of decorator order, because registration order is not the only thing
that could break it.
"""
from __future__ import annotations

import pytest

# One entry per mounted platform router. Inkbunny is the unprefixed one.
SNAPSHOT_PATHS = {
    "ib": "/api/submissions/1/snapshots",
    "fa": "/api/fa/submissions/1/snapshots",
    "ws": "/api/ws/submissions/1/snapshots",
    "sf": "/api/sf/submissions/1/snapshots",
    "sqw": "/api/sqw/submissions/1/snapshots",
    "ao3": "/api/ao3/submissions/1/snapshots",
    "da": "/api/da/submissions/1/snapshots",
    "wp": "/api/wp/submissions/1/snapshots",
    "ik": "/api/ik/submissions/1/snapshots",
    "bsky": "/api/bsky/submissions/1/snapshots",
    "tw": "/api/tw/submissions/1/snapshots",
    "mast": "/api/mast/submissions/1/snapshots",
    "tum": "/api/tum/submissions/1/snapshots",
    "pix": "/api/pix/submissions/1/snapshots",
    "thr": "/api/thr/submissions/1/snapshots",
    "ig": "/api/ig/submissions/1/snapshots",
    "e621": "/api/e621/submissions/1/snapshots",
    "fn": "/api/fn/submissions/1/snapshots",
    "fbr": "/api/fbr/submissions/1/snapshots",
    "tg": "/api/tg/submissions/1/snapshots",
}


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import dashboard
    return TestClient(dashboard.app)


@pytest.mark.parametrize("code,path", sorted(SNAPSHOT_PATHS.items()))
def test_snapshot_route_is_not_swallowed_by_the_detail_route(client, code, path):
    """An unknown id must return an EMPTY series, not 404.

    404 is the tell: it means the detail handler answered, having been given
    "1/snapshots" as the submission id.
    """
    r = client.get(path)
    assert r.status_code == 200, (
        f"{code}: {path} returned {r.status_code} — the greedy "
        f"/submissions/{{id:path}} route is registered first and swallowed it")
    assert "snapshots" in r.json()


def test_every_platform_router_is_covered():
    """A new platform router must be added to this test, or its snapshot route
    ships unverified — which is exactly how nine of them shipped broken."""
    from database import platform_metrics as pm
    missing = sorted(set(pm.ALL_CODES) - set(SNAPSHOT_PATHS))
    assert not missing, f"no snapshot-route check for {missing}"
