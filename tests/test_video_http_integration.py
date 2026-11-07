"""从聊天输入到真实本机 HTTP 轮询，验证两种视频协议的完整接线。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import tempfile
import types
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.config import DEFAULT_VIDEO_START_PROMPT, DEFAULT_VIDEO_PROGRESS_PROMPT, DEFAULT_VIDEO_DONE_PROMPT
from core.models import ImageInput
from core.stats import PluginStats
from core.videos import VideoClient, VideoError
from test_video_commands import FakeEvent, load_main_methods


@asynccontextmanager
async def local_api(handler):
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("mode", ["legacy", "openai"])
@pytest.mark.parametrize("with_image", [False, True])
def test_two_step_chat_through_http_to_video_delivery(mode, with_image):
    async def scenario():
        calls, queries = [], []
        reference = ImageInput(data=b"\x89PNG\r\n\x1a\nreference", mime="image/png")
        endpoint = "/v1/video/generations" if mode == "legacy" else "/v1/videos"

        async def handler(request):
            assert request.headers["Authorization"] == "Bearer test-only-key"
            if request.method == "POST":
                assert request.path == endpoint
                if request.content_type == "multipart/form-data":
                    payload = {}
                    async for part in await request.multipart():
                        payload[part.name] = bytes(await part.read()) if part.filename else await part.text()
                else:
                    payload = await request.json()
                calls.append(payload)
                assert payload["model"] == "test-video-model"
                assert payload["prompt"] == "大草原"
                assert payload["duration" if mode == "legacy" else "seconds"] == (10 if mode == "legacy" else "10")
                if with_image:
                    if mode == "legacy":
                        assert payload["image"].startswith("data:image/png;base64,")
                    else:
                        assert payload["input_reference"] == reference.data
                else:
                    assert not any(k in payload for k in ("image", "input_reference"))
                return web.json_response({"task_id": "integration-task", "status": "queued"})
            assert request.method == "GET"
            assert request.path == endpoint + "/integration-task"
            queries.append(request.path)
            await asyncio.sleep(0.04)
            if len(queries) == 1:
                return web.json_response({"task_id": "integration-task", "status": "in_progress", "progress": "40%"})
            if len(queries) == 2:
                # 上游提前报告100时，机器人仍须等到成片就绪。
                return web.json_response({"task_id": "integration-task", "status": "in_progress", "progress": 100})
            return web.json_response({"task_id": "integration-task", "status": "completed", "url": "https://example.invalid/ready.mp4"})

        async with local_api(handler) as base, _plugin(base, mode, with_image, reference) as (plugin, namespace):
            first = FakeEvent(message=("图生视频" if with_image else "文生视频") + " 大草原")
            await plugin._handle(first)
            assert not calls
            assert any("回复视频时长" in str(c.args[0]) for c in first.send.call_args_list)
            await plugin._handle(FakeEvent(sender="other-user", message="10"))
            await plugin._handle(FakeEvent(message="10", origin="different:group:999"))
            assert not calls
            second = FakeEvent(message="10s")
            await plugin._handle(second)
            jobs = tuple(plugin._video_tasks)
            assert len(jobs) == 1
            # 服务器尚未完成时再次收到秒数，也不得再提交。
            await plugin._handle(FakeEvent(message="10秒"))
            await asyncio.wait_for(asyncio.gather(*jobs), 3)
            await asyncio.sleep(0)
            sent = [c.args[0] for c in second.send.call_args_list]
            video_positions = [i for i, msg in enumerate(sent) if isinstance(msg, list)]
            assert len(video_positions) == 1
            done_positions = [i for i, msg in enumerate(sent) if isinstance(msg, str) and "100%" in msg]
            assert len(done_positions) == 1
            assert done_positions[0] < video_positions[0]
            assert any("40%" in str(msg) and "实际进度" in str(msg) for msg in sent)
            assert any("99%" in str(msg) for msg in sent[:done_positions[0]])
            assert sent[video_positions[0]] == [("video", "https://example.invalid/ready.mp4")]
            assert len(calls) == 1 and len(queries) == 3
            assert not plugin._video_inputs and not plugin._video_pending and not plugin._video_tasks
            count = second.send.call_count
            await asyncio.sleep(0.04)
            assert second.send.call_count == count

    asyncio.run(scenario())


@asynccontextmanager
async def _plugin(base, mode, with_image, reference):
    harness, namespace = load_main_methods()
    plugin = harness()
    plugin.cfg = types.SimpleNamespace(
        video_enabled=lambda: True, video_model=lambda: "test-video-model",
        video_supplier=lambda: "", video_api_mode=lambda: mode, video_timeout=lambda: 2,
        video_poll_interval=lambda: 0.01, video_progress_interval=lambda: 0.01,
        video_duration_timeout=lambda: 120, video_max_concurrent=lambda: 2,
        video_start_prompt=lambda: DEFAULT_VIDEO_START_PROMPT,
        video_progress_prompt=lambda: DEFAULT_VIDEO_PROGRESS_PROMPT,
        video_done_prompt=lambda: DEFAULT_VIDEO_DONE_PROMPT,
        proxy=lambda: "", max_input_images=lambda: 1, reply_reference_image=lambda: True,
    )
    plugin._video_inputs, plugin._video_tasks, plugin._video_pending = {}, set(), set()
    plugin._video_closing = False
    plugin._cooldown_left = Mock(return_value=0)
    plugin._mark_cooldown = Mock()
    plugin._state_view = AsyncMock(return_value={"channel": types.SimpleNamespace(
        name="local-test", base_url=base, api_key="test-only-key",
    )})
    plugin._maybe_translate = AsyncMock(side_effect=lambda channel, prompt, editing: prompt)
    plugin._platform_ok = Mock(return_value=True)
    plugin._scope_ok = Mock(return_value=True)
    plugin._is_blocked = Mock(return_value=False)
    plugin._record_context = Mock()
    plugin._needs_master = Mock(return_value=False)
    plugin._is_master = Mock(return_value=False)
    plugin._menu_command_override = Mock(return_value=None)
    plugin._query = AsyncMock()

    async def reply(event, text):
        await event.send(event.plain_result(text))

    plugin._reply = reply
    namespace.update(VideoClient=VideoClient, VideoError=VideoError,
        extract_image_urls=AsyncMock(side_effect=lambda text: (text, [])),
        collect_input_images=AsyncMock(return_value=[reference] if with_image else []))
    components = types.ModuleType("astrbot.api.message_components")
    components.Video = types.SimpleNamespace(fromURL=lambda url: ("video", url))
    with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"astrbot.api.message_components": components}):
        plugin.stats = PluginStats(directory)
        try:
            yield plugin, namespace
        finally:
            for pending in tuple(plugin._video_inputs.values()):
                if pending.timer is not None:
                    pending.timer.cancel()
            jobs = tuple(plugin._video_tasks)
            for job in jobs:
                job.cancel()
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)
