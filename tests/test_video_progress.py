"""验证估算标识、真实进度优先、定时播报和完成提示门槛。"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.video_progress import VideoProgress, is_video_duration_reply, parse_video_seconds


def task(progress=0, known=False, status="in_progress"):
    return types.SimpleNamespace(id="task-test", status=status, progress=progress, progress_known=known)


class VideoDurationTests(unittest.TestCase):
    def test_accepts_integer_seconds_forms(self):
        for value, expected in (("10", 10), (" 10s ", 10), ("10S", 10), ("10 秒", 10), ("1", 1), ("120", 120)):
            with self.subTest(value=value):
                self.assertTrue(is_video_duration_reply(value))
                self.assertEqual(parse_video_seconds(value), expected)

    def test_invalid_duration_keeps_interaction_but_unrelated_chat_is_ignored(self):
        for value in ("0", "121", "-1", "+3", "2.5秒", "9" * 5000):
            with self.subTest(value=value[:20]):
                self.assertTrue(is_video_duration_reply(value))
                with self.assertRaises(ValueError):
                    parse_video_seconds(value)
        for value in ("", "你好", "帮我画10只猫", "10 seconds please"):
            self.assertFalse(is_video_duration_reply(value))
        self.assertTrue(is_video_duration_reply("取消"))
        self.assertTrue(is_video_duration_reply("CANCEL"))


class VideoProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = [100.0]
        self.send = AsyncMock()
        self.progress = VideoProgress(
            self.send, prompt="大草原", model="test-video-model", seconds=10,
            interval=10, clock=lambda: self.now[0],
        )

    async def asyncTearDown(self):
        await self.progress.stop()

    async def test_estimate_finite_steps_reach_99_and_never_100(self):
        expected = (1, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 97, 99)
        for index, number in enumerate(expected):
            with self.subTest(index=index):
                self.now[0] = 100 + index * 10
                values = self.progress.values()
                self.assertEqual(values["progress"], number)
                self.assertEqual(values["progress_type"], "估算进度 ")
                self.assertEqual(values["elapsed"], index * 10)
                self.assertIn("估算", self.progress.render("进度：{progress}%"))
                self.assertNotIn("100%", self.progress.render("进度：{progress}%"))
        self.now[0] = 1000000
        self.assertEqual(self.progress.values()["progress"], 99)

    async def test_estimate_starts_at_one_after_slow_submission(self):
        self.progress = VideoProgress(self.send, prompt="x", model="test-video-model", seconds=10,
                                      interval=10, clock=lambda: 600, started=100)
        self.assertEqual(self.progress.values()["elapsed"], 500)
        self.assertEqual(self.progress.values()["progress"], 1)

    async def test_real_zero_is_not_treated_as_missing_progress(self):
        await self.progress.update(task(0, True))
        self.now[0] += 90
        values = self.progress.values()
        self.assertEqual(values["progress"], 0)
        self.assertEqual(values["progress_type"], "实际进度 ")
        self.assertNotIn("估算", self.progress.render("{progress_type}{progress}%"))

    async def test_real_progress_stalls_without_switching_to_estimate(self):
        await self.progress.update(task(42, True))
        for elapsed in (10, 100, 10000):
            self.now[0] = 100 + elapsed
            await self.progress.update(task(0, False))
            values = self.progress.values()
            self.assertEqual(values["progress"], 42)
            self.assertEqual(values["elapsed"], elapsed)
            self.assertEqual(values["progress_type"], "实际进度 ")

    async def test_first_real_value_replaces_estimate(self):
        self.now[0] += 100
        self.assertEqual(self.progress.values()["progress"], 95)
        await self.progress.update(task(20, True))
        self.assertEqual(self.progress.values()["progress"], 20)
        self.assertEqual(self.progress.values()["progress_type"], "实际进度 ")

    async def test_invalid_nonfinite_values_do_not_override_real_progress(self):
        await self.progress.update(task(30.5, True))
        for value in (None, "invalid", float("nan"), float("inf"), True):
            await self.progress.update(task(value, True))
            self.assertEqual(self.progress.values()["progress"], 30.5)

    async def test_upstream_100_waits_for_ready_callback(self):
        await self.progress.update(task(100, True, "completed"))
        self.assertEqual(self.progress.values()["progress"], 99)
        self.assertIn("显示上限", self.progress.render("{progress_type}{progress}%"))
        self.assertNotIn("100%", self.progress.render("{progress_type}{progress}%"))
        await self.progress.complete()
        self.assertEqual(self.progress.values()["progress"], 100)
        self.assertIn("100%", self.send.await_args.args[0])

    async def test_status_is_localized(self):
        for status, label in (("queued", "排队中"), ("in_progress", "生成中"),
                              ("completed", "上游生成已完成，正在准备视频"),
                              ("failed", "生成失败"), ("cancelled", "已取消")):
            await self.progress.update(task(known=False, status=status))
            self.assertEqual(self.progress.values()["status"], label)

    async def test_template_values_errors_and_required_estimate_label(self):
        await self.progress.update(task())
        rendered = self.progress.render("{prompt}|{model}|{seconds}|{elapsed}|{progress}|{status}|{task_id}|{unknown}")
        self.assertIn("大草原|test-video-model|10|0|1|生成中|task-test|{unknown}", rendered)
        self.assertIn("估算进度", rendered)
        self.assertIn("任务 ID", self.progress.render("{prompt"))
        self.progress.done_template = "视频准备好了"
        await self.progress.complete()
        self.assertIn("100%", self.send.await_args.args[0])

    async def test_ticker_runs_without_api_updates_and_stops_cleanly(self):
        gates = asyncio.Queue()
        intervals = []

        async def sleep(interval):
            intervals.append(interval)
            await gates.get()

        self.progress.sleep = sleep
        await self.progress.start(task(known=False, status="queued"))
        for index in range(1, 4):
            self.now[0] += 10
            gates.put_nowait(None)
            for _ in range(50):
                if self.send.await_count >= index + 1:
                    break
                await asyncio.sleep(0)
            self.assertEqual(self.send.await_count, index + 1)
            self.assertIn(str(index * 10) + "%", self.send.await_args.args[0])
        await self.progress.stop()
        count = self.send.await_count
        gates.put_nowait(None)
        await asyncio.sleep(0)
        self.assertEqual(self.send.await_count, count)
        self.assertTrue(all(value == 10 for value in intervals))
        self.assertIsNone(self.progress._ticker)

    async def test_send_failure_isolated_from_progress_and_completion(self):
        self.send.side_effect = RuntimeError("platform unavailable")
        await self.progress.start(task())
        self.now[0] += 10
        self.assertFalse(await self.progress._emit(self.progress.progress_template))
        await self.progress.complete()
        self.assertTrue(self.progress.ready)
        self.assertIsNone(self.progress._ticker)

    async def test_cancellation_propagates(self):
        self.send.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.progress.start(task())
        self.assertIsNone(self.progress._ticker)


if __name__ == "__main__":
    unittest.main()
