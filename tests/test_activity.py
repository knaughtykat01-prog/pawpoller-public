"""Activity registry (spec 017): the jobs behind the pill and tray."""
import asyncio

import pytest

from posting import activity


@pytest.fixture(autouse=True)
def _clean():
    activity._reset_for_tests()
    yield
    activity._reset_for_tests()


def _job(jid):
    return next(j for j in activity.snapshot() if j["id"] == jid)


def test_a_job_walks_its_lines_and_finishes():
    jid = activity.start("publish", "Sample Story — ch 3", ["fa", ("ib", "Inkbunny")])
    with activity.bound(jid):
        activity.step("fa", "Preparing")
        activity.step("fa", "Uploading", detail="page 2 of 3")
        assert _job(jid)["lines"][0]["step"] == "Uploading"
        activity.line_done("fa", True, url="https://example.com/1")
        activity.record("ib", {"status": "error", "error": "The login has expired"})
    activity.finish(jid)
    j = _job(jid)
    assert j["state"] == "failed" and j["finished"]
    fa, ib = j["lines"]
    assert (fa["state"], fa["url"]) == ("done", "https://example.com/1")
    assert (ib["label"], ib["state"], ib["error"]) == ("Inkbunny", "failed", "The login has expired")


def test_reporting_outside_a_job_does_nothing():
    activity.step("fa", "Uploading")
    activity.line_done("fa", True)
    assert activity.snapshot() == [] and activity.cancelled() is False


def test_cancel_stops_before_the_next_site():
    jid = activity.start("publish", "T", ["a", "b", "c"])
    with activity.bound(jid):
        activity.record("a", {"status": "success"})
        assert activity.cancel(jid) and activity.cancelled()
    activity.finish(jid)
    j = _job(jid)
    assert j["state"] == "cancelled"
    assert [ln["state"] for ln in j["lines"]] == ["done", "cancelled", "cancelled"]
    assert not activity.cancel(jid)          # finished: nothing left to cancel


def test_finished_jobs_expire_and_are_capped(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(activity.time, "time", lambda: now[0])
    old = activity.start("publish", "old", ["a"])
    activity.finish(old)
    now[0] += activity.KEEP_SECONDS + 1
    running = activity.start("publish", "running", ["a"])
    assert [j["id"] for j in activity.snapshot()] == [running]   # expired; running kept
    for i in range(activity.KEEP_MAX + 5):
        activity.finish(activity.start("publish", f"j{i}", ["a"]))
    snap = activity.snapshot()
    assert len([j for j in snap if j["finished"]]) == activity.KEEP_MAX and running in [j["id"] for j in snap]


def test_a_background_task_reports_and_a_crash_fails_the_unfinished_lines():
    async def good():
        activity.step("fa", "Uploading")
        await asyncio.sleep(0)
        activity.record("fa", {"status": "success", "url": "u"})

    async def bad():
        activity.record("fa", {"status": "success"})
        raise RuntimeError("disk full")

    async def main():
        a = activity.start("publish", "good", ["fa"])
        b = activity.start("publish", "bad", ["fa", "ib"])
        await activity.run_in_job(a, good)
        await activity.run_in_job(b, bad)
        return a, b

    a, b = asyncio.run(main())
    assert _job(a)["state"] == "done"
    jb = _job(b)
    # The crash is the job's; a site never reached is "not sent", not blamed for it.
    assert jb["state"] == "failed" and jb["error"] == "disk full"
    assert (jb["lines"][1]["state"], jb["lines"][1]["step"]) == ("cancelled", "Not sent")
    assert activity.current() is None          # nothing leaks out of the task


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.posting_api import posting_router
    from routes.api import router
    app = FastAPI()
    app.include_router(posting_router)
    app.include_router(router)
    return TestClient(app)


def _fake_post_story(calls):
    """Stands in for manager.post_story: reports like the real one, fa ok / ib fails."""
    async def post_story(story_name, platforms, chapters=None, **kw):
        out = []
        for p in platforms:
            if activity.cancelled():
                break
            calls.append(p)
            activity.step(p, "Uploading")
            await asyncio.sleep(0.01)
            ok = p != "ib"
            r = {"platform": p, "success": ok, "external_url": f"https://x/{p}" if ok else "",
                 "error": "" if ok else "The login has expired"}
            out.append(r)
            activity.line_done(p, ok, url=r["external_url"], error=r["error"])
        return out
    return post_story


def _wait_finished(c, jid, timeout=5.0):
    import time as _t
    end = _t.time() + timeout
    while _t.time() < end:
        job = next((j for j in c.get("/api/activity").json()["jobs"] if j["id"] == jid), None)
        if job and job["finished"]:
            return job
        _t.sleep(0.02)
    raise AssertionError("job never finished")


def test_a_background_publish_returns_a_job_and_reports_each_site(monkeypatch):
    from posting import manager
    calls = []
    monkeypatch.setattr(manager, "post_story", _fake_post_story(calls))
    with _client() as c:
        body = {"story_name": "Sample_Story", "platforms": ["fa", "ib"], "chapters": [3]}
        assert c.post("/api/posting/post", json={**body, "background": True}).status_code == 400  # guard kept
        r = c.post("/api/posting/post", json={**body, "background": True, "confirm_live": True})
        assert r.status_code == 200 and r.json()["status"] == "started"
        job = _wait_finished(c, r.json()["job_id"])
        assert job["title"] == "Sample Story — ch 3" and job["state"] == "failed"
        assert [(ln["key"], ln["state"]) for ln in job["lines"]] == [("fa", "done"), ("ib", "failed")]
        # Retry re-runs ib alone, in the same job.
        assert c.post(f"/api/activity/{job['id']}/retry/ib").status_code == 200
        _wait_finished(c, job["id"])
        assert calls == ["fa", "ib", "ib"]
        # Without the flag the route waits and answers as it always did.
        old = c.post("/api/posting/post", json={**body, "confirm_live": True}).json()
        assert old["status"] == "completed" and old["successes"] == 1


def test_cancel_route_and_polls_in_the_snapshot(monkeypatch):
    jid = activity.start("publish", "T", ["fa"])
    with _client() as c:
        assert c.post(f"/api/activity/{jid}/cancel").status_code == 200
        assert c.post("/api/activity/nope/cancel").status_code == 404
        snap = c.get("/api/activity").json()
        assert set(snap) == {"jobs", "polls"} and snap["jobs"][0]["cancel"] is True


def test_retry_is_refused_where_it_could_double_post():
    """An automatic retry already queued, or part of the site already live → no manual Retry."""
    from posting.manager import _act_settle

    async def rerun(key):
        pass

    async def main():
        jid = activity.start("publish", "T", ["fa", "ib", "ws"], retry=rerun)
        with activity.bound(jid):
            _act_settle("fa", [{"platform": "fa", "success": False, "error": "timeout", "retry_queued": True}])
            _act_settle("ib", [{"platform": "ib", "success": True}, {"platform": "ib", "success": False, "error": "x"}])
            _act_settle("ws", [{"platform": "ws", "success": False, "error": "login expired"}])
        activity.finish(jid)
        j = _job(jid)
        fa, ib, ws = j["lines"]
        assert "try again by itself" in fa["error"] and fa["retryable"] is False
        assert ib["retryable"] is False and ws["retryable"] is True
        assert not await activity.retry(jid, "fa") and not await activity.retry(jid, "ib")
        assert await activity.retry(jid, "ws")

    asyncio.run(main())


def test_retry_reruns_one_line_in_the_same_job():
    calls = []

    async def rerun(key):
        calls.append(key)
        activity.record(key, {"status": "success", "url": "again"})

    async def main():
        jid = activity.start("publish", "T", ["fa", "ib"], retry=rerun)
        with activity.bound(jid):
            activity.record("fa", {"status": "success"})
            activity.record("ib", {"status": "error", "error": "timeout"})
        activity.finish(jid)
        assert _job(jid)["state"] == "failed" and _job(jid)["can_retry"]
        assert not await activity.retry(jid, "fa")     # it succeeded: never posted twice
        assert await activity.retry(jid, "ib")
        assert not await activity.retry(jid, "ib")     # already re-running
        await asyncio.sleep(0.01)
        return jid

    jid = asyncio.run(main())
    j = _job(jid)
    assert calls == ["ib"] and j["state"] == "done" and j["lines"][1]["url"] == "again"
