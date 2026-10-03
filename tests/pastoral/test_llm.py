from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from aiohttp import web

from infrastructure.llm import call_log
from infrastructure.llm.client import LLMClient, PrivateLLMError
from pastoral_bot.config import BotSettings
from pastoral_bot.llm import LLMUnavailable, PastoralLLM

SECRET = "исповедь_контрольная_фраза_не_попадает_в_журнал"


@pytest_asyncio.fixture
async def private_provider(monkeypatch):
    state = {"status": 200, "body": {
        "choices": [{"message": {"content": '{"text":"Здравствуйте","source_ids":[],"referral":null}'}, "finish_reason": "stop"}],
        "usage": {"cost": 0.003, "prompt_tokens": 10, "completion_tokens": 12},
    }, "requests": []}

    async def respond(request):
        state["requests"].append(await request.json())
        return web.json_response(state["body"], status=state["status"])

    app = web.Application()
    app.router.add_post("/api/v1/chat/completions", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr("infrastructure.llm.client.OPENROUTER_BASE", f"http://127.0.0.1:{port}/api/v1")
    yield state
    await runner.cleanup()


@pytest.fixture
def private_logs(caplog):
    logger = logging.getLogger("LLMClient")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="LLMClient")
    yield caplog
    logger.removeHandler(caplog.handler)


@pytest.mark.asyncio
async def test_private_call_routes_zdr_schema_and_keeps_only_numeric_metadata(private_provider, private_logs, monkeypatch):
    rows = []
    monkeypatch.setattr(call_log, "append", rows.append)
    adapter = PastoralLLM(BotSettings(openrouter_api_key="key"))
    result = await adapter.complete([{"role": "user", "content": SECRET}])
    assert str(result.cost) == "0.003"
    request = private_provider["requests"][0]
    assert request["provider"]["zdr"] is True
    assert request["provider"]["require_parameters"] is True
    assert request["provider"]["max_price"] == {"prompt": 0.3, "completion": 2.5, "request": 0}
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["max_tokens"] == 1200
    assert "tools" not in request
    assert rows == []
    assert SECRET not in private_logs.text
    assert "Здравствуйте" not in private_logs.text
    assert "cost=0.003" in private_logs.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [
    (401, {"error": {"message": SECRET}}),
    (429, {"error": {"message": SECRET}}),
    (200, {"choices": [{"error": {"message": SECRET}}]}),
    (200, {"unexpected": SECRET}),
])
async def test_all_provider_errors_are_safe_and_not_retried(private_provider, private_logs, monkeypatch, status, body):
    private_provider.update(status=status, body=body)
    rows = []
    monkeypatch.setattr(call_log, "append", rows.append)
    adapter = PastoralLLM(BotSettings(openrouter_api_key="key"))
    with pytest.raises(LLMUnavailable) as caught:
        await adapter.complete([{"role": "user", "content": SECRET}])
    assert len(private_provider["requests"]) == 1
    assert SECRET not in str(caught.value)
    assert SECRET not in private_logs.text
    assert rows == []


@pytest.mark.asyncio
async def test_truncated_reply_text_is_not_logged(private_provider, private_logs):
    private_provider["body"] = {
        "choices": [{"message": {"content": SECRET}, "finish_reason": "length"}],
        "usage": {"cost": "untrusted text " + SECRET},
    }
    result = await PastoralLLM(BotSettings()).complete([{"role": "user", "content": SECRET}])
    assert result.finish_reason == "length"
    assert result.cost is None
    assert SECRET not in private_logs.text


@pytest.mark.asyncio
async def test_private_client_cannot_accidentally_use_full_corpus_methods(monkeypatch):
    rows = []
    monkeypatch.setattr(call_log, "append", rows.append)
    client = LLMClient("key", model="google/gemini-2.5-flash", private_transport=True)
    with pytest.raises(PrivateLLMError):
        await client.complete([{"role": "user", "content": SECRET}])
    with pytest.raises(PrivateLLMError):
        await client.complete_with_tools([{"role": "user", "content": SECRET}], tools=[])
    with pytest.raises(PrivateLLMError):
        await client.generate_image(SECRET, "image-model")
    with pytest.raises(PrivateLLMError):
        async for _ in client.stream([{"role": "user", "content": SECRET}]):
            pass
    assert rows == []


@pytest.mark.asyncio
async def test_price_ceiling_violation_disables_further_calls(private_provider):
    private_provider["body"]["usage"]["cost"] = 1.0
    adapter = PastoralLLM(BotSettings())
    with pytest.raises(LLMUnavailable) as caught:
        await adapter.complete([{"role": "user", "content": "Проверка"}])
    assert caught.value.cost == 1
    with pytest.raises(LLMUnavailable):
        await adapter.complete([{"role": "user", "content": "Проверка"}])
    assert len(private_provider["requests"]) == 1


@pytest.mark.asyncio
async def test_adapter_refuses_oversized_input_before_network(private_provider):
    adapter = PastoralLLM(BotSettings(max_input_tokens=4096))
    with pytest.raises(LLMUnavailable):
        await adapter.complete([{"role": "user", "content": "я" * 5000}])
    assert private_provider["requests"] == []


@pytest.mark.asyncio
async def test_transport_exception_content_is_scrubbed(private_logs, monkeypatch):
    @asynccontextmanager
    async def broken(self, payload, **kwargs):
        raise RuntimeError("request failure " + SECRET)
        yield  # pragma: no cover

    monkeypatch.setattr(LLMClient, "_open", broken)
    with pytest.raises(LLMUnavailable) as caught:
        await PastoralLLM(BotSettings()).complete([{"role": "user", "content": SECRET}])
    assert SECRET not in str(caught.value)
    assert SECRET not in private_logs.text
