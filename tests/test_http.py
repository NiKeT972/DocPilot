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
    def quota(*ids):
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": "Quota exceeded ... PerDay ... PerMinute",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                            "violations": [{"quotaId": i} for i in ids],
                        }
                    ],
                }
            },
        )

    assert is_daily_quota(quota("EmbedContentRequestsPerDayPerProjectPerModel-FreeTier"))
    assert not is_daily_quota(quota("EmbedContentRequestsPerMinutePerProjectPerModel-FreeTier"))
    assert not is_daily_quota(quota("RequestsPerMinute", "RequestsPerDay"))
    assert is_daily_quota(httpx.Response(429, text="Quota exceeded: requests per day"))
    assert not is_daily_quota(httpx.Response(429, text="per minute; see per day limits"))
    assert not is_daily_quota(httpx.Response(500, text="per day"))


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


async def test_429_backoff_has_floor(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("docpilot.httpx_retry.asyncio.sleep", fake_sleep)
    n = {"c": 0}

    def handler(request):
        n["c"] += 1
        return (
            httpx.Response(429, headers={"retry-after": "1"}) if n["c"] < 3 else httpx.Response(200, json={})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await post_json(c, "https://x.test", json={}, min_429_delay=10, max_delay=65)
    assert sleeps[0] >= 10 and sleeps[1] >= 20


async def test_groq_stream_retries_rate_limit_before_first_token(monkeypatch):
    from docpilot.llm import GroqLLM

    async def fake_sleep(s):
        pass

    monkeypatch.setattr("docpilot.llm.asyncio.sleep", fake_sleep)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, json={"error": {"message": "slow down"}})
        body = 'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "docpilot.llm.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler))
    )
    llm = GroqLLM("k", "openai/gpt-oss-120b")
    out = [t async for t in llm.stream([{"role": "user", "content": "hi"}])]
    assert out == ["Hello"] and calls["n"] == 2
