"""视频两步交互的无网络回归测试；隔离宿主并运行真实入口方法。"""
from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
import re
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.commands import KIND_DRAW, KIND_EDIT, KIND_VIDEO, ParsedCommand, parse_command
from core.stats import PluginStats
from core.video_progress import (
    VIDEO_CANCEL_WORDS, VideoImageConfig, VideoInput, VideoProgress,
    is_video_duration_reply, parse_video_seconds,
)


class FakeVideoError(Exception):
    pass


def load_main_methods():
    """AST 仅剥离 AstrBot 启动依赖，不改动任何被测函数体。"""
    source = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    original = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "Main")
    names = {
        "_video_key", "_discard_video_input", "_video_input_matches", "_video",
        "_video_duration_reply", "_video_done", "_run_video", "_deliver_video",
        "terminate", "_handle",
    }
    selected = ast.ClassDef(
        name="Harness", bases=[], keywords=[], decorator_list=[],
        body=[node for node in original.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names],
    )
    globals_ast = [n for n in source.body if
        (isinstance(n, ast.FunctionDef) and n.name == "_clean_prompt") or
        (isinstance(n, ast.ClassDef) and n.name == "_DrawingCommandFilter") or
        (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {"_MENTION_RE", "_SPACE_RE"} for t in n.targets))]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + globals_ast + [selected], type_ignores=[])
    namespace = dict(
        asyncio=asyncio, time=time, uuid=uuid, tempfile=tempfile, re=re,
        logger=logging.getLogger("test-video"), VideoError=FakeVideoError,
        _text_of=lambda value: "" if value is None else str(value),
        _channel_ready=lambda channel: (bool(channel and channel.base_url), "未配置供应商"),
        _COOLDOWN_HINT="冷却 {seconds} 秒", _SCOPE_EXEMPT_KINDS=frozenset(),
        filter=types.SimpleNamespace(CustomFilter=object), _is_qq_official=lambda e: False,
        KIND_VIDEO=KIND_VIDEO, KIND_DRAW=KIND_DRAW, KIND_EDIT=KIND_EDIT,
        ParsedCommand=ParsedCommand, parse_command=parse_command,
        VideoInput=VideoInput, VideoImageConfig=VideoImageConfig, VideoProgress=VideoProgress,
        VIDEO_CANCEL_WORDS=VIDEO_CANCEL_WORDS, is_video_duration_reply=is_video_duration_reply,
        parse_video_seconds=parse_video_seconds,
    )
    exec(compile(ast.fix_missing_locations(module), str(ROOT / "main.py"), "exec"), namespace)
    return namespace["Harness"], namespace


Harness, MAIN_GLOBALS = load_main_methods()


def task(status="queued", *, url="", progress=0, known=False):
    return types.SimpleNamespace(id="test-task", status=status, url=url, progress=progress, progress_known=known)


class FakeClient:
    instances = []
    release = None
    submit_error = None
    result_status = "completed"
    updates = ()

    def __init__(self, **kwargs):
        self.kwargs, self.submits, self.closed, self.waiting = kwargs, 0, False, False
        self.__class__.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def submit(self, prompt, model, *, seconds, images=()):
        self.submits += 1
        self.prompt, self.model, self.seconds, self.images = prompt, model, seconds, images
        if self.submit_error:
            raise self.submit_error
        return task()

    async def wait(self, initial, on_progress=None):
        self.waiting = True
        if on_progress:
            await on_progress(initial)
            for update in self.updates:
                await on_progress(update)
        if self.release:
            await self.release.wait()
        result = task(self.result_status, url="https://example.invalid/video.mp4", progress=100, known=True)
        if on_progress:
            await on_progress(result)
        return result


class FakeEvent:
    def __init__(self, sender="user", message="文生视频 大草原", origin="platform:group:123"):
        self.sender, self.message, self.unified_msg_origin = sender, message, origin
        self.send = AsyncMock()
        self.is_at_or_wake_command = False

    def get_sender_id(self):
        return self.sender

    def get_message_str(self):
        return self.message

    def is_private_chat(self):
        return False

    def plain_result(self, value):
        return value

    def chain_result(self, value):
        return value


class VideoCommandTests(unittest.TestCase):
    def test_default_aliases(self):
        for command in ("生成视频", "视频生成", "文生视频", "图生视频"):
            for separator in (" ", ""):
                with self.subTest(command=command, separator=separator):
                    parsed = parse_command(command + separator + "大草原", object())
                    self.assertEqual((parsed.kind, parsed.arg, parsed.command), (KIND_VIDEO, "大草原", command))

    def test_custom_alias_and_existing_commands(self):
        cfg = types.SimpleNamespace(video_commands=lambda: ["拍片"])
        self.assertEqual(parse_command("拍片 日落", cfg).kind, KIND_VIDEO)
        self.assertEqual(parse_command("绘画 一只猫", cfg).kind, KIND_DRAW)
        self.assertEqual(parse_command("图片编辑 换成夜景", cfg).kind, KIND_EDIT)
        self.assertIsNone(parse_command("我想生成视频", cfg))

    def test_video_image_config_disables_only_truncation(self):
        cfg = types.SimpleNamespace(max_input_images=lambda: 1, reply_reference_image=lambda: False)
        wrapper = VideoImageConfig(cfg)
        self.assertEqual(wrapper.max_input_images(), 0)
        self.assertFalse(wrapper.reply_reference_image())


class VideoLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeClient.instances, FakeClient.updates = [], ()
        FakeClient.release = FakeClient.submit_error = None
        FakeClient.result_status = "completed"
        MAIN_GLOBALS["VideoClient"] = FakeClient
        MAIN_GLOBALS["extract_image_urls"] = AsyncMock(side_effect=lambda prompt: (prompt, []))
        MAIN_GLOBALS["collect_input_images"] = AsyncMock(return_value=[])
        self.plugin = Harness()
        self.plugin.cfg = types.SimpleNamespace(
            video_enabled=lambda: True, video_max_concurrent=lambda: 2,
            video_model=lambda: "test-video-model", video_supplier=lambda: "",
            video_api_mode=lambda: "legacy", video_timeout=lambda: 600,
            video_poll_interval=lambda: 5, proxy=lambda: "",
            video_duration_timeout=lambda: 120, video_progress_interval=lambda: 10,
            video_start_prompt=lambda: "{model} {seconds}s {progress_type}{progress}% {task_id}",
            video_progress_prompt=lambda: "{progress_type}{progress}% {elapsed}s {task_id}",
            video_done_prompt=lambda: "完成 {progress}% {task_id}",
            trigger_mode=lambda: "at", max_input_images=lambda: 1,
            reply_reference_image=lambda: True,
        )
        self.plugin._video_tasks, self.plugin._video_pending = set(), set()
        self.plugin._video_inputs, self.plugin._video_closing = {}, False
        self.plugin._reply = AsyncMock()
        self.plugin._cooldown_left, self.plugin._mark_cooldown = Mock(return_value=0), Mock()
        self.plugin._state_view = AsyncMock(return_value={"channel": types.SimpleNamespace(
            name="test", base_url="https://example.invalid", api_key="unused-test-value",
        )})
        self.plugin._maybe_translate = AsyncMock(side_effect=lambda c, prompt, editing: prompt)

        async def deliver(event, client, result, on_ready=None):
            if on_ready:
                await on_ready()

        self.plugin._deliver_video = AsyncMock(side_effect=deliver)
        self.plugin.stats = PluginStats()
        self.plugin._menu_command_override = Mock(return_value=None)
        self.plugin._platform_ok = Mock(return_value=True)
        self.plugin._record_context = Mock()
        self.plugin._is_blocked = Mock(return_value=False)
        self.plugin._scope_ok = Mock(return_value=True)
        self.plugin._hint_group_off = AsyncMock()
        self.plugin._needs_master, self.plugin._is_master = Mock(return_value=False), Mock(return_value=False)
        self.plugin._is_duplicate_event = Mock(return_value=False)
        MAIN_GLOBALS["_DrawingCommandFilter"].plugin = self.plugin
        self.event = FakeEvent()
        self.parsed = ParsedCommand(kind=KIND_VIDEO, arg="大草原", command="文生视频")

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def begin(self, event=None, parsed=None):
        await self.plugin._video(event or self.event, parsed or self.parsed)

    async def confirm(self, value="10", *, sender="user", origin="platform:group:123"):
        reply = FakeEvent(sender, value, origin)
        await self.plugin._handle(reply)
        return reply

    async def finish(self):
        await asyncio.gather(*tuple(self.plugin._video_tasks))
        await asyncio.sleep(0)

    async def wait_for_client(self):
        for _ in range(100):
            if FakeClient.instances and FakeClient.instances[0].waiting:
                return
            await asyncio.sleep(0)
        self.fail("后台客户端未进入等待状态")

    async def test_description_waits_without_submit_or_cooldown(self):
        await self.begin()
        self.assertEqual(len(self.plugin._video_inputs), 1)
        self.assertFalse(self.plugin._video_tasks)
        self.assertFalse(FakeClient.instances)
        self.plugin._mark_cooldown.assert_not_called()
        self.assertIn("10s", self.plugin._reply.await_args.args[1])

    async def test_full_4k_prompt_triggers_and_submits_exact_text(self):
        prompt = (
            "4K电影级推广视频，广州塔夜景，绚丽霓虹灯光，珠江波光倒影，"
            "无人机环绕流畅运镜，繁华都市夜景，氛围感强烈，城市地标，"
            "色彩鲜亮，画面顺滑无闪烁，无文字、无水印，商业宣传片风格"
        )
        self.plugin.cfg.strict_trigger = lambda: True
        event = FakeEvent(message="文生视频" + prompt)
        event.is_at_or_wake_command = True
        command_filter = MAIN_GLOBALS["_DrawingCommandFilter"]()
        self.assertTrue(command_filter.filter(event, None))
        await self.plugin._handle(event)
        self.assertIn("回复视频时长", self.plugin._reply.await_args.args[1])
        self.assertEqual(self.plugin._video_inputs[self.plugin._video_key(event)].prompt, prompt)
        self.assertFalse(FakeClient.instances)
        self.assertFalse(self.plugin._video_tasks)
        reply = FakeEvent(message="10s")
        # 已开始的两步交互允许同一会话同一用户直接回复秒数。
        self.assertTrue(command_filter.filter(reply, None))
        await self.plugin._handle(reply)
        await self.finish()
        self.assertEqual(len(FakeClient.instances), 1)
        client = FakeClient.instances[0]
        self.assertEqual((client.submits, client.seconds, client.prompt), (1, 10, prompt))
        self.assertFalse(self.plugin._video_inputs)
        self.assertFalse(self.plugin._video_pending)

    async def test_8000_character_prompt_passes_entry_without_truncation(self):
        prompt = "4K" + "景" * 7998
        self.assertEqual(len(prompt), 8000)
        self.plugin.cfg.strict_trigger = lambda: True
        event = FakeEvent(message="文生视频" + prompt)
        event.is_at_or_wake_command = True
        self.assertTrue(MAIN_GLOBALS["_DrawingCommandFilter"]().filter(event, None))
        await self.plugin._handle(event)
        self.assertIn("回复视频时长", self.plugin._reply.await_args.args[1])
        self.assertEqual(self.plugin._video_inputs[self.plugin._video_key(event)].prompt, prompt)
        self.assertFalse(FakeClient.instances)
        await self.confirm("10s")
        await self.finish()
        self.assertEqual(len(FakeClient.instances), 1)
        self.assertEqual(FakeClient.instances[0].prompt, prompt)
        self.assertEqual(FakeClient.instances[0].seconds, 10)

    async def test_8001_character_prompt_replies_with_limit_at_entry(self):
        prompt = "4K" + "景" * 7999
        self.assertEqual(len(prompt), 8001)
        self.plugin.cfg.strict_trigger = lambda: True
        event = FakeEvent(message="文生视频" + prompt)
        event.is_at_or_wake_command = True
        self.assertTrue(MAIN_GLOBALS["_DrawingCommandFilter"]().filter(event, None))
        await self.plugin._handle(event)
        self.plugin._reply.assert_awaited_once()
        self.assertIn("最多 8000 个字符", self.plugin._reply.await_args.args[1])
        self.assertFalse(self.plugin._video_inputs)
        self.assertFalse(FakeClient.instances)
        self.assertFalse(self.plugin._video_tasks)
        MAIN_GLOBALS["extract_image_urls"].assert_not_awaited()
        MAIN_GLOBALS["collect_input_images"].assert_not_awaited()

    async def test_submit_once_uses_duration_reply_event(self):
        await self.begin()
        reply = await self.confirm("10s")
        await self.confirm("10秒")
        await self.finish()
        client = FakeClient.instances[0]
        self.assertEqual((client.submits, client.seconds, client.prompt), (1, 10, "大草原"))
        self.assertIs(self.plugin._deliver_video.await_args.args[0], reply)
        self.assertTrue(client.closed)
        self.assertEqual(self.plugin.stats.snapshot()["totals"]["success"], 1)
        self.assertEqual(self.plugin.stats.snapshot()["totals"]["images"], 0)
        self.assertFalse(self.plugin._video_pending)
        self.assertFalse(self.plugin._video_inputs)
        self.assertIn("100%", reply.send.await_args.args[0])
        self.event.send.assert_not_awaited()

    async def test_duration_forms_invalid_values_and_boundaries(self):
        for value, expected in (("1", 1), ("120s", 120), ("10秒", 10)):
            with self.subTest(value=value):
                await self.begin()
                await self.confirm(value)
                await self.finish()
                self.assertEqual(FakeClient.instances[-1].seconds, expected)
        await self.begin()
        for value in ("0", "121", "-1", "1.5s", "999999999999999999"):
            with self.subTest(value=value):
                previous = len(FakeClient.instances)
                await self.confirm(value)
                self.assertEqual(len(FakeClient.instances), previous)
                self.assertIn("1–120", self.plugin._reply.await_args.args[1])
                self.assertEqual(len(self.plugin._video_inputs), 1)

    async def test_other_user_conversation_and_chat_are_ignored(self):
        await self.begin()
        await self.confirm("10", sender="other")
        await self.confirm("10", origin="platform:group:other")
        await self.confirm("聊天内容")
        self.assertFalse(FakeClient.instances)
        self.assertEqual(len(self.plugin._video_inputs), 1)

    async def test_waiting_reply_passes_filter_without_at_only_for_owner(self):
        await self.begin()
        filter_instance = MAIN_GLOBALS["_DrawingCommandFilter"]()
        self.assertTrue(filter_instance.filter(FakeEvent(message="10秒"), None))
        self.assertFalse(filter_instance.filter(FakeEvent(sender="other", message="10秒"), None))
        self.assertFalse(filter_instance.filter(FakeEvent(message="10", origin="other"), None))
        self.assertFalse(filter_instance.filter(FakeEvent(message="聊天内容"), None))
        self.plugin._is_duplicate_event.return_value = True
        self.assertFalse(filter_instance.filter(FakeEvent(message="10秒"), None))

    async def test_cancel_and_timeout_release_waiting_images(self):
        await self.begin()
        pending = next(iter(self.plugin._video_inputs.values()))
        await self.confirm("取消")
        self.assertTrue(pending.timer.cancelled())
        self.assertFalse(self.plugin._video_inputs)
        self.assertFalse(FakeClient.instances)
        self.plugin.cfg.video_duration_timeout = lambda: 0.015
        await self.begin()
        await asyncio.sleep(0.04)
        self.assertFalse(self.plugin._video_inputs)
        await self.confirm("10")
        self.assertFalse(FakeClient.instances)

    async def test_expired_reply_and_old_timer_do_not_touch_new_request(self):
        await self.begin()
        key = self.plugin._video_key(self.event)
        old = self.plugin._video_inputs[key]
        old.expires_at = time.monotonic() - 1
        await self.plugin._video_duration_reply(FakeEvent(message="10"), "10")
        self.assertIn("超时", self.plugin._reply.await_args.args[1])
        await self.begin()
        current = self.plugin._video_inputs[key]
        self.plugin._discard_video_input(key, old)
        self.assertIs(self.plugin._video_inputs[key], current)

    async def test_duplicate_waiting_and_generation_concurrency(self):
        FakeClient.release = asyncio.Event()
        await self.begin()
        await self.begin()
        self.assertEqual(len(self.plugin._video_inputs), 1)
        await self.begin(FakeEvent("second"))
        await self.begin(FakeEvent("third"))
        await self.confirm()
        await self.confirm(sender="second")
        await self.confirm(sender="third")
        self.assertEqual(len(self.plugin._video_tasks), 2)
        self.assertEqual(len(self.plugin._video_pending), 2)
        self.assertEqual(len(self.plugin._video_inputs), 1)
        await self.begin()
        self.assertIn("已有一个视频任务", self.plugin._reply.await_args.args[1])
        FakeClient.release.set()
        await self.finish()
        self.assertEqual(sum(c.submits for c in FakeClient.instances), 2)

    async def test_waiting_request_capacity_is_bounded(self):
        for index in range(33):
            await self.begin(FakeEvent(str(index)))
        self.assertEqual(len(self.plugin._video_inputs), 32)
        self.assertIn("请求较多", self.plugin._reply.await_args.args[1])
        self.assertFalse(FakeClient.instances)

    async def test_disabled_empty_model_prompt_limit_and_cooldown(self):
        self.plugin.cfg.video_enabled = lambda: False
        await self.begin()
        self.plugin.cfg.video_enabled = lambda: True
        self.plugin.cfg.video_model = lambda: ""
        await self.begin()
        self.assertIn("模型名称", self.plugin._reply.await_args.args[1])
        self.plugin.cfg.video_model = lambda: "test-video-model"
        await self.begin(parsed=ParsedCommand(KIND_VIDEO))
        await self.begin(parsed=ParsedCommand(KIND_VIDEO, "a" * 8001))
        self.plugin._cooldown_left.return_value = 10
        await self.begin()
        self.assertFalse(self.plugin._video_tasks)
        self.assertFalse(self.plugin._video_inputs)
        self.assertFalse(FakeClient.instances)

    async def test_reply_rechecks_permission_cooldown_and_settings(self):
        await self.begin()
        self.plugin._is_blocked.return_value = True
        await self.confirm()
        self.plugin._is_blocked.return_value = False
        self.plugin._scope_ok.return_value = False
        await self.confirm()
        self.plugin._hint_group_off.assert_awaited_once()
        self.plugin._scope_ok.return_value = True
        self.plugin._needs_master.return_value = True
        await self.confirm()
        self.assertIn("主人", self.plugin._reply.await_args.args[1])
        self.plugin._needs_master.return_value = False
        self.plugin._cooldown_left.return_value = 5
        await self.confirm()
        self.assertIn("冷却", self.plugin._reply.await_args.args[1])
        self.plugin._cooldown_left.return_value = 0
        self.plugin.cfg.video_model = lambda: ""
        await self.confirm()
        self.assertIn("模型名称", self.plugin._reply.await_args.args[1])
        self.plugin.cfg.video_enabled = lambda: False
        await self.confirm()
        self.assertFalse(self.plugin._video_inputs)
        self.assertFalse(FakeClient.instances)

    async def test_single_reference_and_translation_are_passed_to_submit(self):
        reference = types.SimpleNamespace(data=b"reference-image")
        MAIN_GLOBALS["collect_input_images"].return_value = [reference]
        self.plugin._maybe_translate.side_effect = lambda c, p, editing: "translated camera movement"
        await self.begin(parsed=ParsedCommand(KIND_VIDEO, "  镜头  推进  ", command="图生视频"))
        await self.confirm("10秒")
        await self.finish()
        client = FakeClient.instances[0]
        self.assertEqual(client.images, (reference,))
        self.assertEqual(client.prompt, "translated camera movement")
        self.assertEqual(self.plugin._maybe_translate.await_args.args[1:], ("镜头 推进", True))
        wrapper = MAIN_GLOBALS["collect_input_images"].await_args.args[1]
        self.assertEqual(wrapper.max_input_images(), 0)

    async def test_generic_command_auto_detects_reference_and_strips_url(self):
        reference = types.SimpleNamespace(data=b"reference-image")
        MAIN_GLOBALS["collect_input_images"].return_value = [reference]
        MAIN_GLOBALS["extract_image_urls"].side_effect = lambda p: ("风吹草地", ["https://example.invalid/ref.png"])
        await self.begin(parsed=ParsedCommand(KIND_VIDEO, "风吹草地 https://example.invalid/ref.png", command="生成视频"))
        await self.confirm()
        await self.finish()
        self.assertEqual(FakeClient.instances[0].prompt, "风吹草地")
        self.assertEqual(len(FakeClient.instances[0].images), 1)

    async def test_reference_missing_multiple_and_explicit_text_mode_rejected(self):
        await self.begin(parsed=ParsedCommand(KIND_VIDEO, "镜头推进", command="图生视频"))
        self.assertIn("需要一张", self.plugin._reply.await_args.args[1])
        MAIN_GLOBALS["collect_input_images"].return_value = [types.SimpleNamespace(data=b"a"), types.SimpleNamespace(data=b"b")]
        await self.begin(parsed=ParsedCommand(KIND_VIDEO, "镜头推进", command="生成视频"))
        self.assertIn("一次只支持一张", self.plugin._reply.await_args.args[1])
        MAIN_GLOBALS["collect_input_images"].return_value = [types.SimpleNamespace(data=b"a")]
        await self.begin()
        self.assertIn("改用", self.plugin._reply.await_args.args[1])
        self.assertFalse(self.plugin._video_inputs)
        self.assertFalse(FakeClient.instances)

    async def test_broken_reference_and_collection_failure_do_not_submit(self):
        MAIN_GLOBALS["extract_image_urls"].side_effect = lambda p: ("镜头推进", ["https://example.invalid/ref.png"])
        await self.begin(parsed=ParsedCommand(KIND_VIDEO, "镜头推进 https://example.invalid/ref.png", command="生成视频"))
        self.assertIn("未能读取", self.plugin._reply.await_args.args[1])
        MAIN_GLOBALS["collect_input_images"].side_effect = RuntimeError("private-image-url")
        await self.begin()
        self.assertNotIn("private-image-url", self.plugin._reply.await_args.args[1])
        self.assertFalse(self.plugin._video_inputs)
        self.assertFalse(FakeClient.instances)

    async def test_submit_error_not_retried(self):
        FakeClient.submit_error = FakeVideoError("提交超时，未自动重发")
        await self.begin()
        await self.confirm()
        await self.finish()
        self.assertEqual(FakeClient.instances[0].submits, 1)
        self.assertEqual(self.plugin.stats.snapshot()["totals"]["failed"], 1)
        self.assertFalse(self.plugin._video_pending)

    async def test_terminate_cancels_input_polling_progress_and_closes_client(self):
        FakeClient.release = asyncio.Event()
        await self.begin()
        await self.confirm()
        await self.wait_for_client()
        await self.begin(FakeEvent("other"))
        pending = next(iter(self.plugin._video_inputs.values()))
        await self.plugin.terminate()
        self.assertTrue(FakeClient.instances[0].closed)
        self.assertTrue(pending.timer.cancelled())
        self.assertFalse(self.plugin._video_pending)
        self.assertFalse(self.plugin._video_inputs)
        self.assertEqual(self.plugin.stats.snapshot()["totals"]["cancelled"], 1)
        self.assertFalse([t for t in asyncio.all_tasks() if t.get_name() == "gpt-image-video-progress"])
        await self.begin()
        self.assertFalse(self.plugin._video_tasks)

    async def test_upstream_cancelled_or_failed_never_delivers(self):
        for result_status, counter in (("cancelled", "cancelled"), ("failed", "failed")):
            with self.subTest(result_status=result_status):
                FakeClient.result_status = result_status
                await self.begin()
                reply = await self.confirm()
                await self.finish()
                self.assertEqual(self.plugin.stats.snapshot()["totals"][counter], 1)
                self.plugin._deliver_video.assert_not_awaited()
                self.assertFalse(any("100%" in str(call.args[0]) for call in reply.send.await_args_list))

    async def test_progress_send_failure_does_not_fail_generation(self):
        await self.begin()
        reply = FakeEvent(message="10")
        reply.send.side_effect = RuntimeError("send failed")
        await self.plugin._handle(reply)
        await self.finish()
        self.assertEqual(FakeClient.instances[0].submits, 1)
        self.assertEqual(self.plugin.stats.snapshot()["totals"]["success"], 1)

    async def test_delivery_failure_no_exception_secret_echo(self):
        self.plugin._deliver_video.side_effect = RuntimeError("private-sensitive-value")
        await self.begin()
        await self.confirm()
        await self.finish()
        message = self.plugin._reply.await_args.args[1]
        self.assertIn("已生成，但投递失败", message)
        self.assertNotIn("private-sensitive-value", message)
        self.assertEqual(self.plugin.stats.snapshot()["totals"]["failed"], 1)

    async def test_named_supplier_does_not_replace_image_model(self):
        self.plugin.cfg.video_supplier = lambda: "video-supplier"
        channel = types.SimpleNamespace(name="video-supplier", base_url="https://example.invalid/video", api_key="unused")
        self.plugin.cfg.build_channel = Mock(return_value=channel)
        await self.begin()
        await self.confirm()
        await self.finish()
        self.plugin.cfg.build_channel.assert_called_once_with(supplier="video-supplier", protocol="openai", model="test-video-model")
        self.assertEqual(FakeClient.instances[0].kwargs["base_url"], channel.base_url)

    async def test_submission_ack_precedes_missing_supplier_error(self):
        await self.begin()
        order = []
        reply = FakeEvent(message="10")

        async def record_send(message):
            order.append(message)

        async def record_reply(event, message):
            order.append(message)

        reply.send.side_effect = record_send
        self.plugin._reply.side_effect = record_reply
        self.plugin._state_view.return_value = {"channel": None}
        await self.plugin._handle(reply)
        await self.finish()
        self.assertEqual(len(order), 2)
        self.assertIn("正在提交", order[0])
        self.assertIn("未配置供应商", order[1])
        self.assertFalse(FakeClient.instances)

    async def test_submission_ack_precedes_immediate_submit_error(self):
        await self.begin()
        order = []
        reply = FakeEvent(message="10")

        async def record_send(message):
            order.append(message)

        async def record_reply(event, message):
            order.append(message)

        reply.send.side_effect = record_send
        self.plugin._reply.side_effect = record_reply
        FakeClient.submit_error = FakeVideoError("参数被立即拒绝")
        await self.plugin._handle(reply)
        await self.finish()
        self.assertEqual(len(order), 2)
        self.assertIn("正在提交", order[0])
        self.assertIn("参数被立即拒绝", order[1])
        self.assertEqual(FakeClient.instances[0].submits, 1)

    async def test_submission_ack_precedes_client_creation_and_success(self):
        await self.begin()
        order = []
        reply = FakeEvent(message="10")

        async def record_send(message):
            if not order:
                self.assertFalse(FakeClient.instances)
            order.append(message)

        reply.send.side_effect = record_send
        await self.plugin._handle(reply)
        await self.finish()
        self.assertIn("正在提交", order[0])
        self.assertIn("100%", order[-1])
        self.assertEqual(self.plugin.stats.snapshot()["totals"]["success"], 1)

    async def test_missing_supplier_releases_capacity_without_submit(self):
        self.plugin._state_view.return_value = {"channel": None}
        await self.begin()
        await self.confirm()
        await self.finish()
        self.assertFalse(FakeClient.instances)
        self.assertFalse(self.plugin._video_pending)
        self.assertEqual(self.plugin.stats.snapshot()["totals"]["sessions_started"], 0)

    async def test_ready_callback_precedes_video_then_link_fallback(self):
        module = types.ModuleType("astrbot.api.message_components")
        module.Video = types.SimpleNamespace(fromURL=lambda url: ("video", url))
        order = []

        async def send(value):
            order.append("video" if isinstance(value, list) else "link")
            if isinstance(value, list):
                raise RuntimeError("unsupported")

        async def ready():
            order.append("100%")

        self.event.send.side_effect = send
        result = task("completed", url="https://example.invalid/v.mp4")
        with patch.dict(sys.modules, {"astrbot.api.message_components": module}):
            await Harness._deliver_video(self.event, None, result, on_ready=ready)
        self.assertEqual(order, ["100%", "video", "link"])
        self.assertIn(result.url, self.event.send.await_args.args[0])

    async def test_video_and_link_failure_propagates(self):
        module = types.ModuleType("astrbot.api.message_components")
        module.Video = types.SimpleNamespace(fromURL=lambda url: ("video", url))
        self.event.send.side_effect = RuntimeError("delivery failed")
        result = task("completed", url="https://example.invalid/v.mp4")
        with patch.dict(sys.modules, {"astrbot.api.message_components": module}):
            with self.assertRaises(RuntimeError):
                await Harness._deliver_video(self.event, None, result)
        self.assertEqual(self.event.send.await_count, 2)

    async def test_authenticated_download_completes_before_100_and_cleans_file(self):
        module = types.ModuleType("astrbot.api.message_components")
        module.Video = types.SimpleNamespace(fromFileSystem=lambda path: ("file", path))
        downloaded, order = [], []

        async def download(result, directory):
            target = Path(directory) / "video.mp4"
            target.write_bytes(b"test-video-content")
            downloaded.append(target)
            order.append("download")
            return target

        async def ready():
            self.assertTrue(downloaded[0].exists())
            order.append("100%")

        async def send(value):
            order.append("video")

        self.event.send.side_effect = send
        with patch.dict(sys.modules, {"astrbot.api.message_components": module}):
            await Harness._deliver_video(self.event, types.SimpleNamespace(download=download), task("completed"), on_ready=ready)
        self.assertEqual(order, ["download", "100%", "video"])
        self.assertFalse(downloaded[0].exists())

    async def test_download_failure_never_calls_ready(self):
        module = types.ModuleType("astrbot.api.message_components")
        module.Video = types.SimpleNamespace(fromFileSystem=lambda path: ("file", path))
        client = types.SimpleNamespace(download=AsyncMock(side_effect=FakeVideoError("下载失败")))
        ready = AsyncMock()
        with patch.dict(sys.modules, {"astrbot.api.message_components": module}):
            with self.assertRaises(FakeVideoError):
                await Harness._deliver_video(self.event, client, task("completed"), on_ready=ready)
        ready.assert_not_awaited()
        self.event.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
