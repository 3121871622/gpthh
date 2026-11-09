"""异步视频任务：一次提交、限时轮询，与图片协议相互独立。"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from .models import ImageInput

MAX_JSON_BYTES = 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_REFERENCE_IMAGES = 10
MAX_REFERENCE_TOTAL_BYTES = 100 * 1024 * 1024
_REFERENCE_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_STATUSES = {
    "queued": "queued", "pending": "queued", "submitted": "queued",
    "not_start": "queued", "not_started": "queued", "accepted": "queued",
    "in_progress": "in_progress", "processing": "in_progress",
    "running": "in_progress", "generating": "in_progress",
    "success": "completed", "succeeded": "completed", "completed": "completed",
    "done": "completed", "finished": "completed",
    "failure": "failed", "failed": "failed", "error": "failed",
    "cancelled": "cancelled", "canceled": "cancelled", "expired": "failed",
}


class VideoError(RuntimeError):
    """可直接展示给用户的脱敏错误。"""


class _RetryQuery(VideoError):
    def __init__(self, message: str, delay: float = 0) -> None:
        super().__init__(message)
        self.delay = delay


@dataclass(frozen=True)
class VideoTask:
    id: str
    status: str
    url: str = ""
    progress: float = 0
    error: str = ""
    progress_known: bool = False


def redact_error(value: Any, api_key: str = "") -> str:
    text = str(value or "")
    if api_key:
        text = text.replace(api_key, "[已隐藏密钥]")
    text = re.sub(r"(?i)\b(?:sk-|github_pat_|ghp_)[A-Za-z0-9_-]+", "[已隐藏密钥]", text)
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [已隐藏]", text)
    # 上游错误中可能包含带签名的下载地址，不把查询参数写入统计或日志。
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[已隐藏参数]", text)
    return text[:300]


def _http_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    try:
        parts = urlsplit(value)
        port = parts.port
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or (port is not None and not 1 <= port <= 65535)
                or parts.username or parts.password or any(ord(c) < 32 for c in value)):
            return ""
        return value
    except ValueError:
        return ""


def _result_url(value: Any, depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(value, str):
        return _http_url(value)
    if isinstance(value, list):
        for item in value[:20]:
            found = _result_url(item, depth + 1)
            if found:
                return found
    if isinstance(value, dict):
        for key in ("video_url", "url", "download_url", "output_url"):
            found = _http_url(value.get(key))
            if found:
                return found
        for key in ("result", "video", "videos", "output", "outputs", "data"):
            found = _result_url(value.get(key), depth + 1)
            if found:
                return found
    return ""


def parse_task(payload: Any, expected_id: str = "", api_key: str = "") -> VideoTask:
    """兼容 New API 的外层任务、内层上游结果与 OpenAI 视频对象。"""
    if not isinstance(payload, dict):
        raise VideoError("视频接口返回的不是任务对象。")
    if payload.get("code") not in (None, 0, 200, "0", "200", "success", "SUCCESS"):
        raise VideoError("视频接口拒绝请求：" + redact_error(payload.get("message") or payload.get("code"), api_key))
    record = payload
    # New API 外层 data 是网关任务，内层 data 的 id 是上游任务，不能拿来查询网关。
    for _ in range(3):
        if record.get("task_id") or record.get("id"):
            break
        nested = record.get("data")
        if not isinstance(nested, dict):
            break
        record = nested
    raw_id = record.get("task_id") or record.get("id") or expected_id
    task_id = str(raw_id or "").strip()
    if not task_id or len(task_id) > 256 or any(ord(c) < 32 for c in task_id):
        error = record.get("error") or payload.get("error")
        if error:
            raise VideoError("视频提交失败：" + redact_error(error, api_key))
        raise VideoError("视频接口没有返回任务 ID，已停止，未重复提交。")
    if expected_id and task_id != expected_id:
        raise VideoError("视频查询返回了其他任务的 ID，已停止轮询。")
    status = str(record.get("status") or "").strip().lower().replace("-", "_")
    if status not in _STATUSES:
        raise VideoError("视频接口返回未知任务状态：" + redact_error(status or "(空)", api_key))
    normalized = _STATUSES[status]
    raw_progress = record.get("progress")
    progress_known = False
    progress = 0.0
    try:
        if raw_progress is not None and not isinstance(raw_progress, bool):
            value_text = str(raw_progress).strip()
            if value_text.endswith("%"):
                value_text = value_text[:-1].strip()
            value = float(value_text)
            if math.isfinite(value):
                progress = max(0.0, min(100.0, value))
                progress_known = True
    except (ValueError, TypeError):
        pass
    reason = record.get("fail_reason") or record.get("error") or ""
    if normalized in ("failed", "cancelled"):
        nested = record.get("data")
        if isinstance(nested, dict):
            detail = nested.get("error") or nested.get("message") or ""
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("code") or ""
            if isinstance(reason, dict):
                reason = reason.get("message") or reason.get("code") or ""
            if detail and detail != reason:
                reason = (str(reason) + "；" if reason else "") + str(detail)
    if isinstance(reason, dict):
        reason = reason.get("message") or reason.get("code") or "上游任务失败"
    return VideoTask(task_id, normalized, _result_url(record), progress, redact_error(reason, api_key), progress_known)


class VideoClient:
    def __init__(self, base_url: str, api_key: str, *, api_mode: str = "legacy",
                 timeout: float = 900, poll_interval: float = 5, proxy: str | None = None) -> None:
        base = _http_url(base_url)
        if not base:
            raise VideoError("请先配置有效的视频供应商 HTTP(S) 地址。")
        parts = urlsplit(base)
        if parts.query or parts.fragment:
            raise VideoError("供应商地址应为 API 根地址，不应包含查询参数或片段。")
        if not api_key or not api_key.strip():
            raise VideoError("请先在供应商配置中填写 API Key。")
        if api_mode not in ("legacy", "openai", "images"):
            raise VideoError("视频接口模式必须为 legacy、openai 或 images。")
        path = parts.path.rstrip("/")
        if not re.search(r"/v\d+(?:beta\d*)?$", path):
            path += "/v1"
        self.base_url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        self.api_key = api_key.strip()
        self.api_mode = api_mode
        self.timeout = max(1.0, float(timeout))
        self.poll_interval = max(0.01, float(poll_interval))
        self.proxy = proxy or None
        self._session: aiohttp.ClientSession | None = None

    @property
    def endpoint(self) -> str:
        return self.base_url + ("/video/generations" if self.api_mode == "legacy" else "/videos")

    async def __aenter__(self) -> "VideoClient":
        self._session = aiohttp.ClientSession(trust_env=True)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise VideoError("视频客户端尚未打开。")
        return self._session

    async def _json(self, method: str, url: str, *, payload: dict | None = None,
                    form: aiohttp.FormData | None = None,
                    remaining: float | None = None) -> Any:
        limit = min(60.0, self.timeout, remaining if remaining is not None else self.timeout)
        try:
            body = {"data": form} if form is not None else {"json": payload}
            async with self._client().request(
                method, url, **body,
                headers={"Authorization": "Bearer " + self.api_key, "Accept": "application/json",
                         "User-Agent": "gpthh-video/1.1.0"},
                timeout=aiohttp.ClientTimeout(total=max(0.01, limit)),
                proxy=self.proxy, allow_redirects=False,
            ) as response:
                if method == "GET" and (response.status == 429 or response.status >= 500):
                    try:
                        delay = max(0.0, min(30.0, float(response.headers.get("Retry-After", "0"))))
                    except ValueError:
                        delay = 0.0
                    raise _RetryQuery("视频状态查询暂时失败（HTTP %s）。" % response.status, delay)
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_JSON_BYTES:
                        raise VideoError("视频接口响应过大，已停止读取。")
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise VideoError("视频接口未返回 JSON（HTTP %s），请检查 API 地址和接口模式。" % response.status) from None
                if not 200 <= response.status < 300:
                    error = (data.get("error") or data.get("message") or "请求失败") if isinstance(data, dict) else "请求失败"
                    if isinstance(error, dict):
                        error = error.get("message") or error.get("code") or "请求失败"
                    message = redact_error(error, self.api_key)
                    if (method == "POST" and response.status == 400 and self.api_mode != "images"
                            and "images" in message and "旧版单图" in message):
                        message += "；请将视频接口模式改为 images（JSON 多图）后手动重试，本次未自动重发。"
                    raise VideoError("视频接口 HTTP %s：%s" % (response.status, message))
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if method == "GET":
                raise _RetryQuery("视频状态查询连接失败，请稍后查询任务。") from None
            # POST 超时也可能已经扣费并创建任务，因此绝不自动重试提交。
            raise VideoError("视频提交连接中断或超时，上游可能已创建任务；未自动重发，请检查中转站任务记录。") from None

    async def submit(self, prompt: str, model: str, *, seconds: int,
                     images: Sequence[ImageInput] = ()) -> VideoTask:
        if not prompt or not prompt.strip():
            raise VideoError("请在生成视频指令后填写内容。")
        if len(prompt) > 8000:
            raise VideoError("视频提示词过长，请控制在 8000 字符以内。")
        if not model or not model.strip():
            raise VideoError("视频模型名称不能为空。")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 120:
            raise VideoError("视频时长必须为 1 到 120 之间的整数秒；具体可用时长以模型为准。")
        if self.api_mode == "images":
            if len(images) > MAX_REFERENCE_IMAGES:
                raise VideoError("JSON 多图视频最多支持 %s 张参考图片。" % MAX_REFERENCE_IMAGES)
        elif len(images) > 1:
            raise VideoError("当前视频接口模式仅支持一张参考图片；多图请切换 images（JSON 多图）模式。")
        references: list[tuple[ImageInput, str]] = []
        total_bytes = 0
        for index, reference in enumerate(images, 1):
            if (not isinstance(reference, ImageInput)
                    or not isinstance(reference.data, (bytes, bytearray)) or not reference.data):
                raise VideoError("第 %s 张视频参考图片为空或格式无效，请重新发送图片。" % index)
            mime = str(reference.mime or "").lower().strip()
            if mime not in _REFERENCE_EXTENSIONS:
                raise VideoError("第 %s 张视频参考图片仅支持 JPEG、PNG 或 WebP 格式。" % index)
            if reference.size > MAX_REFERENCE_BYTES:
                raise VideoError("第 %s 张视频参考图片超过 20 MiB 上限。" % index)
            total_bytes += reference.size
            if total_bytes > MAX_REFERENCE_TOTAL_BYTES:
                raise VideoError("视频参考图片合计超过 100 MiB 上限。")
            references.append((reference, mime))
        image, mime = references[0] if references else (None, "")
        payload = {"model": model.strip(), "prompt": prompt.strip()}
        if self.api_mode == "images":
            # 数组协议只发送 images，单图也用一元素数组，避免与旧字段冲突。
            payload["duration"] = seconds
            if references:
                payload["images"] = ["data:%s;base64,%s" % (
                    mime, base64.b64encode(reference.data).decode("ascii"))
                    for reference, mime in references]
        elif self.api_mode == "legacy":
            # New API v1.0.0-rc.48 dto/video.go 的字段为 image（单数）及 duration。
            payload["duration"] = seconds
            payload["response_format"] = "url"
            if image is not None:
                payload["image"] = "data:%s;base64,%s" % (mime, base64.b64encode(image.data).decode("ascii"))
        else:
            payload["seconds"] = str(seconds)
            if image is not None:
                # openai_video 主机协议以 multipart 文件字段 input_reference 接收参考图。
                form = aiohttp.FormData()
                for name, value in payload.items():
                    form.add_field(name, str(value))
                form.add_field("input_reference", image.data,
                               filename="reference." + _REFERENCE_EXTENSIONS[mime], content_type=mime)
                return parse_task(await self._json("POST", self.endpoint, form=form), api_key=self.api_key)
        return parse_task(await self._json("POST", self.endpoint, payload=payload), api_key=self.api_key)

    async def wait(self, task: VideoTask,
                   on_progress: Callable[[VideoTask], Awaitable[None]] | None = None) -> VideoTask:
        """先通知初始状态，再通知每次成功查询；通知失败不影响上游任务。"""
        deadline = time.monotonic() + self.timeout
        failures = 0

        async def notify(current: VideoTask) -> None:
            if on_progress is not None:
                try:
                    await on_progress(current)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # 平台发送失败不能使已扣费任务中断，也不记录可能含凭据的异常文本。
                    pass

        await notify(task)
        while True:
            if task.status == "completed":
                if not task.url and self.api_mode in ("legacy", "images"):
                    raise VideoError("视频任务 %s 已完成，但未返回有效视频地址。" % task.id)
                return task
            if task.status == "cancelled":
                return task
            if task.status == "failed":
                raise VideoError("视频任务 %s 失败：%s" % (task.id, task.error or "上游未提供原因"))
            left = deadline - time.monotonic()
            if left <= 0:
                raise VideoError("等待视频任务 %s 超时；上游可能仍在运行，未自动重新提交。" % task.id)
            await asyncio.sleep(min(self.poll_interval, left))
            left = deadline - time.monotonic()
            if left <= 0:
                continue
            try:
                data = await self._json("GET", self.endpoint + "/" + quote(task.id, safe=""), remaining=left)
                task = parse_task(data, expected_id=task.id, api_key=self.api_key)
                failures = 0
                await notify(task)
            except _RetryQuery as exc:
                failures += 1
                if failures >= 3:
                    raise VideoError("任务 %s 连续三次查询失败；未重新提交，请稍后在中转站查询。" % task.id) from None
                await asyncio.sleep(min(max(self.poll_interval, exc.delay), max(0, deadline - time.monotonic())))

    async def download(self, task: VideoTask, directory: str | Path) -> Path:
        """OpenAI content 需要认证；重定向或外部 URL 不携带供应商密钥。"""
        if task.status != "completed":
            raise VideoError("视频尚未完成，暂不能下载。")
        if task.url:
            url = task.url
            auth = False
        elif self.api_mode == "openai":
            url = self.endpoint + "/" + quote(task.id, safe="") + "/content"
            auth = True
        else:
            raise VideoError("视频任务没有下载地址。")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / ("video-" + uuid.uuid4().hex + ".mp4")
        temporary = target.with_suffix(".part")
        try:
            for _ in range(5):
                headers = {"User-Agent": "gpthh-video/1.1.0"}
                if auth:
                    headers["Authorization"] = "Bearer " + self.api_key
                async with self._client().get(url, headers=headers, proxy=self.proxy,
                        timeout=aiohttp.ClientTimeout(total=min(self.timeout, 120)), allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        from urllib.parse import urljoin
                        next_url = _http_url(urljoin(url, response.headers.get("Location", "")))
                        if not next_url or (urlsplit(url).scheme == "https" and urlsplit(next_url).scheme != "https"):
                            raise VideoError("视频下载重定向地址无效。")
                        # 下载站即便同源也不再发送 API Key，避免签名 URL 接收多余凭据。
                        url, auth = next_url, False
                        continue
                    if response.status != 200:
                        raise VideoError("视频已生成，但下载失败（HTTP %s）。" % response.status)
                    mime = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                    if not (mime.startswith("video/") or mime == "application/octet-stream"):
                        raise VideoError("视频下载返回了非视频内容。")
                    if response.content_length and response.content_length > MAX_VIDEO_BYTES:
                        raise VideoError("视频超过 100 MiB 下载上限。")
                    size = 0
                    with temporary.open("wb") as handle:
                        async for chunk in response.content.iter_chunked(65536):
                            size += len(chunk)
                            if size > MAX_VIDEO_BYTES:
                                raise VideoError("视频超过 100 MiB 下载上限。")
                            handle.write(chunk)
                    if not size:
                        raise VideoError("视频下载内容为空。")
                    temporary.replace(target)
                    return target
            raise VideoError("视频下载重定向次数过多。")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise VideoError("视频已生成，但下载超时或连接失败。") from None
        finally:
            temporary.unlink(missing_ok=True)
