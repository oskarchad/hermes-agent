"""HER-308: a proxy 5xx must not hide an upstream rate limit from auxiliary fallback."""

import asyncio
import json

import httpx
import openai
import pytest

from agent import auxiliary_client as ac
from hermes_constants import get_hermes_home


CLAUDE = "cc/claude-opus-5-5(medium)"
GPT = "cx/gpt-6-astra"
PROXY_BODY = {
    "error": {
        "message": '[claude/claude-opus-5-5(medium)] [429]: {"type":"error","error":'
        '{"type":"rate_limit_error","message":"This request would exceed your account\'s rate limit..."}}'
    }
}


@pytest.mark.parametrize("status,body,reason", [
    (503, PROXY_BODY, "rate limit"),
    (502, {"error": {"message": "upstream [429]: Too many requests"}}, "rate limit"),
    (500, {"error": {"type": "rate_limit_error"}}, "rate limit"),
    (503, {"error": {"message": "upstream [429]: quota exceeded"}}, "payment error"),
    (503, {"error": {"message": "upstream [429]: rate_limit_error: billing limit"}}, "payment error"),
    (503, {"error": {"message": "Service unavailable; try again"}}, None),
    (503, {"error": {"message": "request id 42901 failed"}}, None),
    (503, {"error": {"message": "request id 429 failed"}}, None),
    (400, PROXY_BODY, None),
    (429, {"error": {"message": "Too many requests"}}, "rate limit"),
])
def test_proxy_error_classification(status, body, reason):
    response = httpx.Response(status, json=body, request=httpx.Request("POST", "https://proxy.test/v1/chat/completions"))
    exc = openai.APIStatusError(f"Error code: {status} - {body}", response=response, body=body)
    assert ac._is_rate_limit_error(exc) is (reason == "rate limit")
    assert ac._is_payment_error(exc) is (reason == "payment error")
    assert next((label for predicate, label in ac._FALLBACK_REASONS if predicate(exc)), None) == reason
    if status == 503 and reason is None:
        assert ac._is_transient_transport_error(exc)


@pytest.mark.parametrize("rate_limited", [True, False], ids=["wrapped-429", "plain-503"])
def test_async_vision_uses_main_fallback_only_for_wrapped_limit(monkeypatch, rate_limited):
    # Real config, resolver, OpenAI SDK, and recovery ladder; only the HTTP boundary is simulated.
    config = {
        "model": {"provider": "custom:9router", "default": CLAUDE},
        "custom_providers": [{"name": "9router", "base_url": "https://proxy.test/v1", "api_key": "test-key"}],
        "auxiliary": {"vision": {"provider": "auto"}},
        "fallback_providers": [
            {"provider": "custom:9router", "model": CLAUDE},
            {"provider": "custom:9router", "model": GPT},
        ],
    }
    (get_hermes_home() / "config.yaml").write_text(json.dumps(config))
    requests = []
    clients = []
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "Describe this screenshot."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1hZ2U="}},
    ]}]

    def respond(request):
        assert request.url.host == "proxy.test"
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["model"] == CLAUDE:
            body = PROXY_BODY if rate_limited else {"error": {"message": "Service unavailable"}}
            return httpx.Response(503, json=body)
        assert payload["model"] == GPT
        return httpx.Response(200, json={
            "id": "test-completion", "object": "chat.completion", "created": 0, "model": GPT,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Test screenshot description"},
                         "finish_reason": "stop"}],
        })

    def http_client_kwargs(base_url, *, async_mode=False):
        transport = httpx.MockTransport(respond)
        client = (httpx.AsyncClient if async_mode else httpx.Client)(transport=transport)
        clients.append(client)
        return {"http_client": client}

    monkeypatch.setattr(ac, "_openai_http_client_kwargs", http_client_kwargs)
    ac._client_cache.clear()
    ac._reset_aux_unhealthy_cache()

    async def exercise():
        try:
            if rate_limited:
                route = {}
                result = await ac.async_call_llm(task="vision", messages=messages, route_info=route)
                assert result.choices[0].message.content == "Test screenshot description"
                assert route["model"] == GPT
            else:
                with pytest.raises(openai.InternalServerError):
                    await ac.async_call_llm(task="vision", messages=messages)
        finally:
            for client in clients:
                if isinstance(client, httpx.AsyncClient):
                    await client.aclose()
                else:
                    client.close()
            ac._client_cache.clear()
            ac._reset_aux_unhealthy_cache()

    asyncio.run(exercise())
    # Wrapped 5xx gets a transport retry, then the existing credential-rung retry
    # once the named provider identity is retained; plain 503 never enters that rung.
    assert [r["model"] for r in requests] == ([CLAUDE, CLAUDE, CLAUDE, GPT] if rate_limited else [CLAUDE, CLAUDE])
    assert all(r["messages"] == messages for r in requests)


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
def test_auto_vision_rotates_named_custom_pool_before_fallback(monkeypatch, async_mode):
    home = get_hermes_home()
    config = {
        "model": {"provider": "custom:review-proxy", "default": CLAUDE},
        "providers": {"review-proxy": {"base_url": "https://proxy.test/v1"}},
        "auxiliary": {"vision": {"provider": "auto"}},
        "fallback_providers": [{"provider": "custom:review-proxy", "model": GPT}],
    }
    (home / "config.yaml").write_text(json.dumps(config))
    (home / "auth.json").write_text(json.dumps({
        "version": 1, "providers": {}, "credential_pool": {"review-proxy": [
            {"id": "one", "label": "one", "auth_type": "api_key", "priority": 0,
             "source": "manual", "access_token": "test-first"},
            {"id": "two", "label": "two", "auth_type": "api_key", "priority": 1,
             "source": "manual", "access_token": "test-second"},
        ]},
    }))
    requests = []
    clients = []

    def respond(request):
        assert request.url.host == "proxy.test"
        payload = json.loads(request.content)
        key = request.headers["authorization"]
        requests.append((key, payload["model"]))
        if key == "Bearer test-first":
            return httpx.Response(401, json={"error": {"message": "Invalid API key"}})
        assert key == "Bearer test-second"
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": payload["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "healthy pool key"},
                         "finish_reason": "stop"}],
        })

    def http_client_kwargs(base_url, *, async_mode=False):
        client = (httpx.AsyncClient if async_mode else httpx.Client)(transport=httpx.MockTransport(respond))
        clients.append(client)
        return {"http_client": client}

    monkeypatch.setattr(ac, "_openai_http_client_kwargs", http_client_kwargs)
    ac._client_cache.clear()
    ac._reset_aux_unhealthy_cache()

    async def exercise():
        try:
            kwargs = {"task": "vision", "messages": [{"role": "user", "content": "Describe screenshot"}]}
            result = await ac.async_call_llm(**kwargs) if async_mode else ac.call_llm(**kwargs)
            assert result.choices[0].message.content == "healthy pool key"
        finally:
            for client in clients:
                if isinstance(client, httpx.AsyncClient):
                    await client.aclose()
                else:
                    client.close()
            ac._client_cache.clear()
            ac._reset_aux_unhealthy_cache()

    asyncio.run(exercise())
    assert requests == [("Bearer test-first", CLAUDE), ("Bearer test-second", CLAUDE)]
