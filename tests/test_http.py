import httpx
import pytest

from docpilot.embeddings import BATCH_SIZE, MAX_BATCH_CHARS, _batches, l2_normalize
from docpilot.httpx_retry import UpstreamError, _retry_after, is_daily_quota, post_json


def test_l2_normalize():
    v = l2_normalize([3.0, 4.0])
    assert v == pytest.approx([0.6, 0.8])
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]


def test_batches_respect_count_and_size():
    texts = ["x" * 1000] * 250
    groups = _batches(texts)
    assert sum(len(g) for g in groups) == 250
    assert all(len(g) <= BATCH_SIZE and sum(map(len, g)) <= MAX_BATCH_CHARS for g in groups)


def test_retry_after_header_and_google_retry_info():
    r = httpx.Response(429, headers={"retry-after": "7"})
    assert _retry_after(r) == 7
    body = {
        "error": {
            "code": 429,
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}],
        }
    }
    assert _retry_after(httpx.Response(429, json=body)) == 37
    assert _retry_after(httpx.Response(429, json={"error": {}})) is None


def test_daily_quota_detection():
    assert is_daily_quota(httpx.Response(429, text="Quota exceeded for metric: ...requests_per_day"))
    assert not is_daily_quota(httpx.Response(429, text="per minute"))


async def test_post_json_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("docpilot.httpx_retry.asyncio.sleep", fake_sleep)

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await post_json(c, "https://x.test", json={}) == {"ok": True}
    assert calls["n"] == 3 and all(s >= 2 for s in sleeps)


async def test_post_json_gives_up_on_400_and_daily_quota(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr("docpilot.httpx_retry.asyncio.sleep", fake_sleep)
    seen = {"n": 0}

    def handler(request):
        seen["n"] += 1
        return httpx.Response(429, json={"error": {"message": "Quota exceeded: requests per day"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(UpstreamError) as e:
            await post_json(c, "https://x.test", json={}, service="Svc")
    assert e.value.status == 429 and seen["n"] == 1
