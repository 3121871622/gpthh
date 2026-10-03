"""
通义万相（阿里云 DashScope）图像合成协议。

请求形态（**中转站型**）：

* 提交：``POST {base}/api/v1/services/aigc/text2image/image-synthesis``，
  必须带请求头 ``X-DashScope-Async: enable`` 走异步；JSON 体为
  ``{"model":..,"input":{"prompt":..},"parameters":{"size":"1024*1024","n":1}}@``；
* 轮询：``GET {base}/api/v1/tasks/{task_id}``，直到
  ``output.task_status@`` 变成 ``SUCCEEDED@`` / ``FAILED@``；
  成功时图片在 ``output.results[].url@``（DashScope 的图片是限时链接，插件直接用 URL）。
* 图片编辑：DashScope 的万相编辑模型（如 ``wanx2.1-imageedit``）走同一入口，
  在 ``input@`` 里追加 ``image_url@`` / ``base_image_url@`` 等字段；
  站点若不支持时会返回参数错误，此时自动**降级为纯生成**（把参考图写进提示词）。

尺寸写法：DashScope 用 ``1024*1024@``（星号），插件会自动把 ``1536x1024@`` 转成
``1536*1024@``；``auto@`` 时不传 size，交给服务端决定。

轮询节奏：1.5s 起步，每次 +2.5s，上限 5s；总时长受通道超时控制。

鉴权：``Authorization: Bearer <DashScope API Key>@``。
"""

from __future__ import annotations

from typing import Any

from ..models import (
    PROTOCOL_TONGYI,
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
    POLL_INTERVAL_START,
    POLL_INTERVAL_STEP,
    POLL_INTERVAL_MAX,
    BaseImageProtocol,
    PollOutcome,
    looks_like_auto,
    parse_size_pair,
    truncate,
)

# 提交路径（按顺序尝试，404 就换下一个）
_SUBMIT_PATHS = (
    "api/v1/services/aigc/text2image/image-synthesis",
    "v1/services/aigc/text2image/image-synthesis",
)
# 轮询路径模板
_TASK_PATHS = (
    "api/v1/tasks/{0}",
    "v1/tasks/{0}",
)
# DashScope 的任务状态词
_STATUS_DONE = ("succeeded", "success", "done", "finished")
_STATUS_FAILED = ("failed", "canceled", "cancelled", "unknown", "expired")
# 异步请求头（DashScope 约定）
_ASYNC_HEADERS = {"X-DashScope-Async": "enable"}


class TongyiProtocol(BaseImageProtocol):
    """通义万相 / DashScope 协议：异步任务 + 轮询。"""

    protocol_key: str = PROTOCOL_TONGYI

    def __init__(
        self,
        channel: Channel,
        timeout: float = DEFAULT_TIMEOUT,
        proxy: str = "",
    ) -> None:
        super().__init__(channel, timeout, proxy)
        self._submit_path = ""

    # ------------------------------------------------------------ 主流程
    async def generate(self, req: GenerateRequest) -> GenerateResult:
        """提交异步任务并轮询取图；编辑不被支持时降级为纯生成。"""
        prompt = self._require_prompt(req)
        editing = bool(req.images)
        action = "图片编辑请求" if editing else "文生图请求"
        model = self._resolve_model(req, edit=editing)
        body = self._build_body(req, prompt, model, editing)
        submitted = await self._submit(body, action)
        images = self._pick_images(submitted.data, urls_first=True)
        if images:
            return self._build_result(images, model=model, payload=submitted.data, status=submitted.status)
        if editing and self._is_parameter_rejected(submitted):
            # 站点不认 image_url 之类的编辑参数：降级为纯生成，保证用户体验
            fallback = self._build_body(
                GenerateRequest(prompt=self._reference_prompt(prompt, req), size=req.size, model=req.model, n=req.n),
                self._reference_prompt(prompt, req),
                model,
                False,
            )
            submitted = await self._submit(fallback, "文生图请求（编辑降级）")
            images = self._pick_images(submitted.data, urls_first=True)
            if images:
                return self._build_result(images, model=model, payload=submitted.data, status=submitted.status)
        task_id = self._extract_task_id(submitted.data)
        if not task_id:
            raise ProtocolError(
                "接口未返回图片，也没有返回任务 ID，请检查模型名与接口地址是否正确",
                status=submitted.status,
                detail=truncate(submitted.text, 500),
            )
        images = await self._wait_for_images(task_id, action)
        return self._build_result(
            images,
            model=model,
            payload={"task_id": task_id},
            status=submitted.status,
        )

    async def list_models(self) -> list:
        """DashScope 没有统一的模型列表接口，返回空列表（前端会提示手动填写）。"""
        return []

    # ------------------------------------------------------------ 请求体
    @staticmethod
    def _build_body(req: GenerateRequest, prompt: str, model: str, editing: bool) -> dict:
        """组装 DashScope 的 ``input/parameters@`` 结构。"""
        parameters: dict = {"n": TongyiProtocol._normalize_n(req.n)}
        size = str(req.size or "").strip()
        if not looks_like_auto(size):
            width, height = parse_size_pair(size, (1024, 1024), 0)
            parameters["size"] = "{0}*{1}".format(width, height)
        inputs: dict = {"prompt": prompt}
        if editing:
            urls = [item.source for item in req.images if getattr(item, "source", "")]
            urls = [item for item in urls if item.startswith(("http://", "https://"))]
            if urls:
                # 有公网链接时优先用它（DashScope 只接受 URL 形式的参考图）
                inputs["base_image_url"] = urls[0]
                if len(urls) > 1:
                    inputs["image_url"] = urls[1]
            else:
                # 没有链接时退回 base64（部分中转站支持）
                payloads = [item.to_b64() for item in req.images if item.data]
                if not payloads:
                    raise ProtocolError("图片编辑失败：没有读取到可用的输入图片")
                inputs["base_image_url"] = "data:image/png;base64,{0}".format(payloads[0])
                if len(payloads) > 1:
                    parameters["multi_image"] = len(payloads)
        return {"model": model, "input": inputs, "parameters": parameters}

    @staticmethod
    def _reference_prompt(prompt: str, req: GenerateRequest) -> str:
        """编辑降级时把参考图信息写进提示词，让结果至少贴合用户意图。"""
        count = len([item for item in req.images if item.data])
        if count <= 0:
            return prompt
        return "{0}\n（参考图 {1} 张：该接口不支持图片编辑参数，已按纯文本描述生成）".format(prompt, count)

    async def _submit(self, body: dict, action: str) -> Any:
        """提交异步任务（带 ``X-DashScope-Async: enable``）。"""
        candidates = [self._submit_path] if self._submit_path else list(_SUBMIT_PATHS)
        last = None
        for path in candidates:
            if not path:
                continue
            result = await self._request(
                "POST",
                self._url(path),
                json_body=body,
                headers=dict(_ASYNC_HEADERS),
            )
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

    @staticmethod
    def _is_parameter_rejected(result: Any) -> bool:
        """判断是否为「参数不被支持」（400/422），用于决定编辑降级。"""
        try:
            return int(getattr(result, "status", 0)) in (400, 422)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------ 轮询
    @staticmethod
    def _extract_task_id(payload: Any) -> str:
        """取 ``output.task_id@``（兼容顶层 task_id / id）。"""
        if isinstance(payload, dict):
            for key in ("task_id", "taskId", "id"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for key in ("output", "data", "result"):
                nested = TongyiProtocol._extract_task_id(payload.get(key))
                if nested:
                    return nested
        return ""

    async def _wait_for_images(self, task_id: str, action: str) -> list:
        """轮询 ``/api/v1/tasks/{task_id}@`` 直到出图 / 失败 / 超时。"""
        async def probe() -> PollOutcome:
            return await self._query_once(task_id)

        outcome = await self._poll_until_done(
            probe,
            action=action,
            interval_start=POLL_INTERVAL_START,
            interval_step=POLL_INTERVAL_STEP,
            interval_max=POLL_INTERVAL_MAX,
        )
        return outcome.images

    async def _query_once(self, task_id: str) -> PollOutcome:
        """查询一次任务状态。"""
        last = None
        for template in _TASK_PATHS:
            result = await self._request("GET", self._url(template.format(task_id)))
            if result.ok:
                return self._parse_task(result.data)
            last = result
            if not result.endpoint_missing:
                break
        if last is None:
            return PollOutcome(POLL_FAILED, message="查询任务状态失败：没有可用的接口路径")
        return PollOutcome(
            POLL_FAILED,
            payload=last.data,
            message="查询任务状态失败（HTTP {}）：{}".format(last.status, truncate(last.error_message(), 120)),
        )

    def _parse_task(self, payload: Any) -> PollOutcome:
        """解析 DashScope 的任务查询响应。"""
        status = self._extract_status(payload)
        images = self._pick_images(payload, urls_first=True)
        if status in _STATUS_FAILED:
            return PollOutcome(
                POLL_FAILED,
                images=images,
                payload=payload,
                message="任务执行失败：{}".format(self._extract_reason(payload) or status),
            )
        if images and (not status or status in _STATUS_DONE):
            return PollOutcome(POLL_DONE, images=images, payload=payload)
        if images:
            return PollOutcome(POLL_DONE, images=images, payload=payload)
        return PollOutcome(POLL_PENDING, payload=payload)

    @staticmethod
    def _extract_status(payload: Any) -> str:
        """取 ``output.task_status@`` 之类的状态字段并转小写。"""
        if isinstance(payload, dict):
            for key in ("task_status", "status", "state"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip().lower()
            for key in ("output", "data", "result"):
                nested = TongyiProtocol._extract_status(payload.get(key))
                if nested:
                    return nested
        return ""

    @staticmethod
    def _extract_reason(payload: Any) -> str:
        """取失败原因（output.message / code / message …）。"""
        if isinstance(payload, dict):
            for key in ("message", "msg", "code", "reason"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for key in ("output", "data", "result"):
                nested = TongyiProtocol._extract_reason(payload.get(key))
                if nested:
                    return nested
        return ""


__all__ = ["TongyiProtocol"]
