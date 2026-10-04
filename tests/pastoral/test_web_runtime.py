from types import SimpleNamespace

import aiohttp
import pytest

import pastoral_bot.__main__ as runtime
from pastoral_bot.config import BotSettings


@pytest.mark.asyncio
async def test_http_runtime_requires_built_frontend_before_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "__main__.py"))
    with pytest.raises(RuntimeError, match="miniapp_build_missing"):
        await runtime.start_web(SimpleNamespace(), BotSettings(web_port=8091))


@pytest.mark.asyncio
async def test_http_runtime_serves_static_without_access_logger_and_releases_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "__main__.py"))
    directory = tmp_path / "miniapp" / "dist"
    directory.mkdir(parents=True)
    (directory / "index.html").write_text("<html>miniapp</html>")
    settings = BotSettings(web_host="127.0.0.1").model_copy(update={"web_port": 0})
    runner = await runtime.start_web(SimpleNamespace(), settings)
    port = runner.addresses[0][1]
    assert runner._kwargs["access_log"] is None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/") as response:
                assert response.status == 200
                assert await response.text() == "<html>miniapp</html>"
            async with session.get(f"http://127.0.0.1:{port}/api/state") as response:
                assert response.status == 401
                assert response.headers["Cache-Control"] == "no-store"
    finally:
        await runner.cleanup()
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientConnectorError):
            await session.get(f"http://127.0.0.1:{port}/healthz")
