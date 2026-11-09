"""更新维护状态与真实图片/视频入口生命周期的无网络回归。"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
import re
import sys
import time
import types
from unittest.mock import AsyncMock, Mock
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import updates
from core.commands import KIND_DRAW, KIND_EDIT, KIND_VIDEO, ParsedCommand
from core.config import PluginConfig
from core.state import SessionState
from core.video_progress import VideoImageConfig, VideoInput, VIDEO_CANCEL_WORDS, parse_video_seconds
from core.videos import MAX_REFERENCE_BYTES, MAX_REFERENCE_IMAGES, MAX_REFERENCE_TOTAL_BYTES


class FakeStar:
    def __init__(self, context):
        self.context = context


def load_lifecycle_methods():
    """仅隔离宿主启动、落盘与网络依赖，不改动被测方法体。"""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Main")
    names = {
        "__init__", "initialize", "_resolve_data_dir", "_draw", "_video",
        "_video_duration_reply", "_video_key", "_discard_video_input", "_video_done",
    }
    harness = ast.ClassDef(
        name="LifecycleHarness", bases=[ast.Name(id="FakeStar", ctx=ast.Load())],
        keywords=[], decorator_list=[],
        body=[node for node in original.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names],
    )
    globals_ast = [node for node in tree.body if
                   (isinstance(node, ast.FunctionDef) and node.name == "_clean_prompt") or
                   (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in {"_MENTION_RE", "_SPACE_RE"} for target in node.targets))]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + globals_ast + [harness], type_ignores=[])
    namespace = dict(
        asyncio=asyncio, time=time, re=re, uuid=uuid, FakeStar=FakeStar,
        logger=logging.getLogger("test-update-lifecycle"), PluginConfig=PluginConfig,
        SessionState=SessionState, PluginStats=lambda path: Mock(), FunStats=lambda path: Mock(),
        _config_module=types.SimpleNamespace(), _DrawingCommandFilter=types.SimpleNamespace(plugin=None),
        register_web_apis=Mock(return_value=True), mark_plugin_ready=Mock(wraps=updates.mark_plugin_ready),
        maintenance_message=updates.maintenance_message, PLUGIN_VERSION="test-build",
        _text_of=lambda value: "" if value is None else str(value),
        _COOLDOWN_HINT="冷却 {seconds} 秒", KIND_DRAW=KIND_DRAW, KIND_EDIT=KIND_EDIT,
        VideoInput=VideoInput, VideoImageConfig=VideoImageConfig,
        VIDEO_CANCEL_WORDS=VIDEO_CANCEL_WORDS, parse_video_seconds=parse_video_seconds,
        MAX_REFERENCE_BYTES=MAX_REFERENCE_BYTES, MAX_REFERENCE_IMAGES=MAX_REFERENCE_IMAGES,
        MAX_REFERENCE_TOTAL_BYTES=MAX_REFERENCE_TOTAL_BYTES,
        extract_image_urls=AsyncMock(side_effect=lambda prompt: (prompt, [])),
        collect_input_images=AsyncMock(return_value=[]),
    )
    exec(compile(ast.fix_missing_locations(module), str(ROOT / "main.py"), "exec"), namespace)
    return namespace["LifecycleHarness"], namespace


def make_plugin():
    harness, namespace = load_lifecycle_methods()
    plugin = harness(types.SimpleNamespace(), {"video_enabled": True, "video_model": "fixture-video"})
    plugin._reply = AsyncMock()
    plugin._draw_impl = AsyncMock()
    plugin._run_video = AsyncMock()
    plugin._cooldown_left = lambda event: 0
    plugin.stats.has_session.return_value = False
    event = types.SimpleNamespace(unified_msg_origin="test-session", get_sender_id=lambda: "test-user")
    return plugin, event, namespace


def start_maintenance(plugin):
    plugin.context._gpthh_maintenance_v1 = {
        "active_job_id": "fixture-job",
        "jobs": {"fixture-job": {"status": "queued", "message": "正在更新插件"}},
    }
    assert updates.maintenance_message(plugin)


def pending_video(plugin, event):
    key = plugin._video_key(event)
    pending = VideoInput("大草原", "文生视频", time.monotonic() + 120)
    pending.ready = True
    plugin._video_inputs[key] = pending
    return key, pending


def test_initialize_registers_empty_image_tasks_and_marks_ready_last():
    async def run():
        plugin, _, namespace = make_plugin()
        assert plugin._image_tasks == set()
        assert updates.runtime_info(plugin)["ready"] is False
        order = []

        async def load():
            assert updates.runtime_info(plugin)["ready"] is False
            order.append("state")

        def register(instance):
            assert instance is plugin
            assert updates.runtime_info(plugin)["ready"] is False
            order.append("web")
            return True

        plugin.state.load = AsyncMock(side_effect=load)
        namespace["register_web_apis"].side_effect = register
        await plugin.initialize()
        assert order == ["state", "web"]
        namespace["mark_plugin_ready"].assert_called_once_with(plugin)
        assert updates.runtime_info(plugin)["ready"] is True

    asyncio.run(run())


def test_cancelled_initialize_does_not_mark_ready():
    async def run():
        plugin, _, namespace = make_plugin()
        plugin.state.load = AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await plugin.initialize()
        namespace["mark_plugin_ready"].assert_not_called()
        assert updates.runtime_info(plugin)["ready"] is False

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
@pytest.mark.parametrize("has_session", [False, True])
def test_draw_releases_registered_task_for_every_exit(outcome, has_session):
    async def run():
        plugin, event, _ = make_plugin()
        plugin.stats.has_session.return_value = has_session
        seen = []

        async def draw_impl(*args):
            seen.append(asyncio.current_task() in plugin._image_tasks)
            await asyncio.sleep(0)
            if outcome == "failure":
                raise RuntimeError("fixture failure")
            if outcome == "cancel":
                raise asyncio.CancelledError()

        plugin._draw_impl.side_effect = draw_impl
        parsed = ParsedCommand(kind=KIND_EDIT, arg="换成夜景")
        if outcome == "success":
            await plugin._draw(event, parsed)
        else:
            expected = RuntimeError if outcome == "failure" else asyncio.CancelledError
            with pytest.raises(expected):
                await plugin._draw(event, parsed)
        assert seen == [True]
        assert plugin._image_tasks == set()
        if has_session and outcome == "failure":
            plugin.stats.record_failure.assert_called_once()
        else:
            plugin.stats.record_failure.assert_not_called()
        if has_session and outcome == "cancel":
            plugin.stats.record_cancel.assert_called_once()
        else:
            plugin.stats.record_cancel.assert_not_called()

    asyncio.run(run())


def test_maintenance_blocks_draw_before_registering_task():
    async def run():
        plugin, event, _ = make_plugin()
        start_maintenance(plugin)
        await plugin._draw(event, ParsedCommand(kind=KIND_DRAW, arg="一只猫"))
        plugin._draw_impl.assert_not_awaited()
        plugin._reply.assert_awaited_once_with(event, updates.maintenance_message(plugin))
        assert not plugin._image_tasks
        plugin.stats.has_session.assert_not_called()

    asyncio.run(run())


def test_maintenance_blocks_video_before_collecting_or_creating_tasks():
    async def run():
        plugin, event, namespace = make_plugin()
        start_maintenance(plugin)
        await plugin._video(event, ParsedCommand(kind=KIND_VIDEO, arg="大草原", command="文生视频"))
        namespace["collect_input_images"].assert_not_awaited()
        namespace["extract_image_urls"].assert_not_awaited()
        plugin._run_video.assert_not_awaited()
        assert not plugin._video_inputs and not plugin._video_pending and not plugin._video_tasks
        plugin._reply.assert_awaited_once_with(event, updates.maintenance_message(plugin))

    asyncio.run(run())


def test_maintenance_blocks_duration_submission_without_consuming_pending():
    async def run():
        plugin, event, _ = make_plugin()
        key, pending = pending_video(plugin, event)
        start_maintenance(plugin)
        await plugin._video_duration_reply(event, "10s")
        plugin._run_video.assert_not_awaited()
        assert plugin._video_inputs[key] is pending
        assert not plugin._video_pending and not plugin._video_tasks
        plugin._reply.assert_awaited_once_with(event, updates.maintenance_message(plugin))

    asyncio.run(run())


def test_maintenance_keeps_video_cancel_available():
    async def run():
        plugin, event, _ = make_plugin()
        key, _ = pending_video(plugin, event)
        start_maintenance(plugin)
        await plugin._video_duration_reply(event, "取消")
        assert key not in plugin._video_inputs
        assert not plugin._video_pending and not plugin._video_tasks
        plugin._run_video.assert_not_awaited()
        plugin._reply.assert_awaited_once_with(event, "已取消本次视频生成，未提交任务。")

    asyncio.run(run())


@pytest.mark.parametrize("registration", ["unsupported", "exception"])
def test_initialize_without_registered_web_api_stays_not_ready(registration):
    async def run():
        plugin, _, namespace = make_plugin()
        namespace["register_web_apis"].return_value = False
        if registration == "exception":
            namespace["register_web_apis"].side_effect = RuntimeError("fixture registration failure")
        await plugin.initialize()
        namespace["register_web_apis"].assert_called_once_with(plugin)
        namespace["mark_plugin_ready"].assert_not_called()
        assert updates.runtime_info(plugin)["ready"] is False

    asyncio.run(run())
