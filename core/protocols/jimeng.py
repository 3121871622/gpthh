"""
即梦 / 火山引擎（Volcengine ARK）图像生成协议。

请求形态（**中转站型**，会按需自动补版本段）：

* 提交：``POST {base}/api/v1/images/generations``，JSON 体含
  ``model@`` / ``prompt@`` / ``size@`` / ``n@`` / ``response_format@``；
  图片编辑时追加 ``image@`` 字段（base64，单张）或多张时用 ``images@`` 数组；
* 轮询：提交响应里若带 ``task_id@``（也可能叫 ``id@`` / ``data.id@``），
  则轮询 ``GET {base}/api/v1/images/generations/{task_id}``，直到 ``status@`` 变成
  ``done@`` / ``succeeded@`` 或失败（``failed@`` / ``error@``）；
* 若提交响应里**直接带图**（同步返回的中转站），则跳过轮询。

轮询节奏：1.5s 起步，每次 +2.5s，上限 5s；总时长受通道超时控制。

鉴权：``Authorization: Bearer <key>@``。
"""

from __future__ import annotations

from typing import Any

from ..models import (
    PROTOCOL_JIMENG,
    Channel,
    GenerateRequest,
    GenerateResult,
    ProtocolError,
)
from .base import (
    DEFAULT_TIMEOUT,
    POLL_DONE,
    POLL_FAILED,
    POLL_PENDING,
    BaseImageProtocol,
    PollOutcome,
    looks_like_auto,
    truncate,
)

# 提交接口可能存在的路径（按顺序尝试，命中 404 就换下一个）
_SUBMIT_PATHS = (
    "api/v1/images/generations",
    "v1/images/generations",
    "images/generations",
)
# 轮询接口的后缀（跟在任务 id 前面）
_QUERY_PREFIXES = (
    "api/v1/images/generations/{0}",
    "v1/images/generations/{0}",
    "api/v1/tasks/{0}",
)
# 表示「任务已完成」的状态词
_DONE_STATUS = ("done", "succeeded", "success", "finished", "completed", "complete")
# 表示「任务失败」的状态词
_FAILED_STATUS = ("failed", "fail", "error", "canceled", "cancelled", "expired")


class JimengProtocol(BaseImageProtocol):
    """即梦 / 火山引擎协议：提交任务 + 轮询任务状态。"""

    protocol_key: str = PROTOCOL_JIMENG

    def __init__(
        self,
        channel: Channel,
        timeout: float = DEFAULT_TIMEOUT,
        proxy: str = "",
    ) -> None:
        super().__init__(channel, timeout, proxy)
        # 记住本次可用的提交路径，后续请求直接复用，少做一次无用探测
        self._submit_path = ""

    # ------------------------------------------------------------ 主流程
    async def generate(self, req: GenerateRequest) -> GenerateResult:
        """提交任务；若返回 task_id 则轮询，否则直接从响应里取图。"""
        prompt = self._require_prompt(req)
        editing = bool(req.images)
        action = "图片编辑请求" if editing else "文生图请求"
        model = self._resolve_model(req, edit=editing)
        body = self._build_body(req, prompt, model, editing)
        result = await self._submit(body, action)
        images = self._pick_images(result.data, urls_first=True)
        if images:
            return self._build_result(images, model=model, payload=result.data, status=result.status)
        task_id = self._extract_task_id(result.data)
        if not task_id:
            raise ProtocolError(
                "接口未返回图片，也没有返回任务 ID，请检查模型名与接口地址",
                status=result.status,
                detail=truncate(result.text, 500),
            )
        images = await self._wait_for_images(task_id, action)
        return self._build_result(
            images,
            model=model,
            payload={"task_id": task_id},
            status=result.status,
        )

    async def list_models(self) -> list:
        """按 OpenAI 风格读取 ``/models@``；取不到返回空列表。"""
        try:
            result = await self._request("GET", self._url("models"))
        except ProtocolError:
            return []
        if not result.ok:
            return []
        entries = result.data
        if isinstance(entries, dict):
            entries = entries.get("data") or entries.get("models") or []
        if not isinstance(entries, list):
            return []
        names: list = []
        for item in entries:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict):
                name = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
            else:
                continue
            if name:
                names.append(name)
        return sorted(set(names), key=lambda value: value.lower())

    # ------------------------------------------------------------ 请求体
    @staticmethod
    def _build_body(req: GenerateRequest, prompt: str, model: str, editing: bool) -> dict:
        """组装提交体；尺寸为 auto 时不传 size（部分站点不认 auto）。"""
        body: dict = {
            "model": model,
            "prompt": prompt,
            "n": JimengProtocol._normalize_n(req.n),
            "response_format": "b64_json",
        }
        size = str(req.size or "").strip()
        if not looks_like_auto(size):
            body["size"] = size
        if editing:
            encoded: list = []
            for image in req.images:
                if image.data:
                    encoded.append(image.to_b64())
            if not encoded:
                raise ProtocolError("图片编辑失败：没有读取到可用的输入图片")
            # 单图用 image、多图用 images（即梦的两套写法都被覆盖）
            body["image"] = encoded[0]
            if len(encoded) > 1:
                body["images"] = encoded
        return body

    async def _submit(self, body: dict, action: str) -> Any:
        """依次尝试候选路径，直到有一个不返回 404/405。"""
        candidates = [self._submit_path] if self._submit_path else list(_SUBMIT_PATHS)
        last = None
        for path in candidates:
            if not path:
                continue
            result = await self._request("POST", self._url(path), json_body=body)
            if result.ok:
                self._submit_path = path
                return result
            last = result
            if not result.endpoint_missing:
                break
        if last is None:
            raise ProtocolError("{}失败：没有可用的接口路径".format(action))
        last.ensure_ok(action)
        raise ProtocolError(
            "{}失败（HTTP {}）".format(action, last.status),
            status=last.status,
            detail=truncate(last.text, 500),
        )

    # ------------------------------------------------------------ 轮询
    @staticmethod
    def _extract_task_id(payload: Any) -> str:
        """从各种包装里取任务 ID（task_id / id / data.task_id …）。"""
        if isinstance(payload, dict):
            for key in ("task_id", "taskId", "id"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for key in ("data", "result", "output"):
                nested = JimengProtocol._extract_task_id(payload.get(key))
                if nested:
                    return nested
        return ""

    async def _wait_for_images(self, task_id: str, action: str) -> list:
        """轮询任务状态直到出图 / 失败 / 超时。"""
        async def probe() -> PollOutcome:
            return await self._query_once(task_id)

        outcome = await self._poll_until_done(probe, action=action)
        return outcome.images

    async def _query_once(self, task_id: str) -> PollOutcome:
        """查询一次任务状态；路径也做 404 兼容。"""
        last = None
        for template in _QUERY_PREFIXES:
            result = await self._request("GET", self._url(template.format(task_id)))
            if result.ok:
                return self._parse_task(result.data, result.status)
            last = result
            if not result.endpoint_missing:
                break
        if last is None:
            return PollOutcome(POLL_FAILED, message="查询任务状态失败：没有可用的接口路径")
        if last.status in (404, 405):
            return PollOutcome(POLL_FAILED, payload=last.text, message="查询任务状态的接口不存在（HTTP {}）".format(last.status))
        return PollOutcome(
            POLL_FAILED,
            payload=last.data,
            message="查询任务状态失败（HTTP {}）：{}".format(last.status, truncate(last.error_message(), 120)),
        )

    def _parse_task(self, payload: Any, status: int) -> PollOutcome:
        """解析任务查询响应。"""
        images = self._pick_images(payload, urls_first=True)
        state_text = self._extract_status(payload)
        if images and (not state_text or state_text in _DONE_STATUS):
            return PollOutcome(POLL_DONE, images=images, payload=payload)
        if state_text in _FAILED_STATUS:
            return PollOutcome(
                POLL_FAILED,
                images=images,
                payload=payload,
                message="任务执行失败：{}".format(truncate(self._extract_reason(payload), 160) or state_text),
            )
        if images:
            # 状态字段没给全但已经有图，按完成处理
            return PollOutcome(POLL_DONE, images=images, payload=payload)
        return PollOutcome(POLL_PENDING, payload=payload)

    @staticmethod
    def _extract_status(payload: Any) -> str:
        """取 status 字段并转小写（兼容 data.status 嵌套）。"""
        if isinstance(payload, dict):
            for key in ("status", "state", "task_status"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip().lower()
            for key in ("data", "result", "output"):
                nested = JimengProtocol._extract_status(payload.get(key))
                if nested:
                    return nested
        return ""

    @staticmethod
    def _extract_reason(payload: Any) -> str:
        """取失败原因（message / error.message / fail_reason …）。"""
        if isinstance(payload, dict):
            for key in ("fail_reason", "message", "msg", "reason", "error_message"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            error = payload.get("error")
            if isinstance(error, dict):
                for key in ("message", "msg", "code"):
                    value = error.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            elif isinstance(error, str) and error.strip():
                return error.strip()
            for key in ("data", "result", "output"):
                nested = JimengProtocol._extract_reason(payload.get(key))
                if nested:
                    return nested
        return ""


__all__ = ["JimengProtocol"]
