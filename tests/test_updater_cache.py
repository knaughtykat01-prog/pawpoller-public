"""The update check is cached: hosted seats share one IP and GitHub's 60/hour."""
import updater


class _Resp:
    def __init__(self, status, headers=None, data=None):
        self.status_code, self.headers, self._data = status, headers or {}, data or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _client(responses, calls):
    class C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, *a, **k):
            calls.append(1)
            return responses.pop(0)
    return C


def test_good_answer_is_reused(monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "_CACHE", {})
    monkeypatch.setattr(updater.httpx, "Client", _client([_Resp(200, data={"tag_name": "v0.0.1"})], calls))
    for _ in range(4):
        assert updater.check_for_update()["available"] is False
    assert len(calls) == 1


def test_rate_limit_backs_off_until_reset(monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "_CACHE", {})
    import time
    reset = str(int(time.time()) + 600)
    monkeypatch.setattr(updater.httpx, "Client", _client(
        [_Resp(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset})], calls))
    for _ in range(4):
        assert updater.check_for_update()["error"] == "GitHub rate limit"
    assert len(calls) == 1


def test_other_errors_are_not_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "_CACHE", {})
    monkeypatch.setattr(updater.httpx, "Client", _client([_Resp(500), _Resp(500)], calls))
    updater.check_for_update(); updater.check_for_update()
    assert len(calls) == 2
