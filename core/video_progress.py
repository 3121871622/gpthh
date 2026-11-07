"""视频时长交互与进度提示；估算值始终与接口真实进度区分。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import re
import time
from typing import Any


VIDEO_CANCEL_WORDS = frozenset(("取消", "取消视频", "取消生成", "cancel"))
_DURATION_REPLY = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?\s*(?:s|秒)?", re.IGNORECASE)
_DURATION_VALUE = re.compile(r"([0-9]{1,3})\s*(?:s|秒)?", re.IGNORECASE)


def is_video_duration_reply(text: str) -> bool:
    text = str(text or "").strip()
    return text.lower() in VIDEO_CANCEL_WORDS or bool(_DURATION_REPLY.fullmatch(text))


def parse_video_seconds(text: str) -> int:
    match = _DURATION_VALUE.fullmatch(str(text or "").strip())
    if not match or not 1 <= int(match.group(1)) <= 120:
        raise ValueError("请回复 1–120 的整数秒数，例如 10、10s 或 10秒；发送「取消」退出。")
    return int(match.group(1))


@dataclass
class VideoInput:
    prompt: str
    command: str
    expires_at: float
    images: tuple = ()
    ready: bool = False
    timer: Any = None


class VideoImageConfig:
    """保留采图结果以识别多图，其他图片设置仍委托原配置。"""

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg

    def max_input_images(self) -> int:
        return 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.cfg, name)


class _TemplateValues(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class VideoProgress:
    """独立定时播报；发送失败不打断轮询，也不会触发第二次生成。"""

    def __init__(
        self, send: Any, *, prompt: str, model: str, seconds: int,
        interval: float = 10, start_template: str = "", progress_template: str = "",
        done_template: str = "", started: float | None = None,
        clock: Any = time.monotonic, sleep: Any = asyncio.sleep,
    ) -> None:
        self.send = send
        self.prompt, self.model, self.seconds = prompt, model, seconds
        self.interval = max(0.01, float(interval))
        self.clock, self.sleep = clock, sleep
        self.estimate_started = self.clock()
        self.started = self.estimate_started if started is None else started
        self.start_template = start_template or (
            "🎬 视频任务已提交\n模型：{model} · 时长：{seconds}秒\n"
            "{progress_type}{progress}% · 已等待 {elapsed}秒\n任务 ID：{task_id}"
        )
        self.progress_template = progress_template or (
            "🎬 {progress_type}{progress}% · 已等待 {elapsed}秒\n"
            "状态：{status}\n任务 ID：{task_id}"
        )
        self.done_template = done_template or "🎬 视频已就绪，100% · 用时 {elapsed}秒\n任务 ID：{task_id}"
        self.task_id = ""
        self.status = "queued"
        self.real_progress: float | None = None
        self.ready = False
        self._ticker: asyncio.Task | None = None

    async def update(self, task: Any) -> None:
        self.task_id = str(getattr(task, "id", "") or self.task_id)
        self.status = str(getattr(task, "status", "") or self.status)
        if getattr(task, "progress_known", False):
            value = getattr(task, "progress", None)
            if not isinstance(value, bool):
                try:
                    number = float(value)
                except (TypeError, ValueError, OverflowError):
                    number = math.nan
                if math.isfinite(number):
                    self.real_progress = max(0.0, min(100.0, number))

    def values(self) -> dict:
        elapsed = max(0, int(self.clock() - self.started))
        if self.ready:
            progress, kind, status = 100, "实际进度 ", "视频已就绪"
        elif self.real_progress is not None:
            progress = min(99, self.real_progress)
            kind = "实际进度 "
            status = self.status
            if self.real_progress >= 100:
                status = "上游生成已完成，正在准备视频；就绪前显示上限为99%"
        else:
            # 固定等待指示在有限时间到99后停留，不代表模型完成度或精确耗时。
            steps = (1, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 97, 99)
            index = max(0, int((self.clock() - self.estimate_started) / self.interval))
            progress = steps[min(index, len(steps) - 1)]
            kind, status = "估算进度 ", self.status
        status = {
            "queued": "排队中", "in_progress": "生成中",
            "completed": "上游生成已完成，正在准备视频", "failed": "生成失败",
            "cancelled": "已取消",
        }.get(status, status)
        if isinstance(progress, float) and progress.is_integer():
            progress = int(progress)
        return {
            "prompt": self.prompt, "model": self.model, "seconds": self.seconds,
            "elapsed": elapsed, "progress": progress, "progress_type": kind,
            "status": status, "task_id": self.task_id,
        }

    def render(self, template: str) -> str:
        values = self.values()
        try:
            message = str(template).format_map(_TemplateValues(values))
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            message = "🎬 {progress_type}{progress}% · 已等待 {elapsed}秒\n任务 ID：{task_id}".format(**values)
        if self.ready:
            if "100%" not in message:
                message += "\n进度：100%（视频已就绪）"
        elif self.real_progress is None:
            message += "\n（估算进度，不代表接口实际进度；生成耗时由上游决定。）"
        elif self.real_progress >= 100:
            message += "\n上游生成已完成，正在准备视频；就绪前显示上限为99%。"
        return message

    async def _emit(self, template: str) -> bool:
        try:
            await asyncio.wait_for(self.send(self.render(template)), timeout=min(10, self.interval))
            return True
        except Exception:
            return False

    async def start(self, task: Any) -> None:
        await self.update(task)
        await self._emit(self.start_template)
        self._ticker = asyncio.create_task(self._run(), name="gpt-image-video-progress")

    async def _run(self) -> None:
        while True:
            await self.sleep(self.interval)
            await self._emit(self.progress_template)

    async def stop(self) -> None:
        ticker, self._ticker = self._ticker, None
        if ticker is not None:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)

    async def complete(self) -> None:
        await self.stop()
        self.ready = True
        await self._emit(self.done_template)
