"""
ComfyUI 协议（``/prompt`` 提交工作流 + ``/history`` 轮询取图）。

请求形态（**自部署**，不走 /v1，也不补版本段）：

* 提交：``POST {base}/prompt``，体为 ``{"prompt": <API 格式工作流>, "client_id": "..."}；
* 轮询：``GET {base}/history/{prompt_id}``，直到出现 ``outputs`` 里的 ``images``；
* 取图：``GET {base}/view?filename=..&subfolder=..&type=output``（插件记录 URL，由发送层下载）；
* 模型列表：``GET {base}/object_info/CheckpointLoaderSimple`` 取 checkpoint 名列表（尽力而为）。

工作流来源：优先用通道配置里的自定义工作流（``Channel.extra["workflow"]``，JSON 字符串或 dict）。
没有自定义工作流时，使用内置的**极简文生图工作流**（加载默认 checkpoint 的两个常用节点名）。
图片编辑：把输入图片先 ``POST {base}/upload/image`` 上传，再替换工作流里第一个
``LoadImage`` 节点的 ``image`` 字段；没有 ``LoadImage`` 节点时按纯生成处理。

鉴权：默认无；ComfyUI 反代常用 Basic，此时密钥栏填「用户名:密码」。
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any, Optional

from ..models import (
    PROTOCOL_COMFYUI,
    Channel,
    GeneratedImage,
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
    parse_size_pair,
    truncate,
)

# 内置工作流的节点 id 与常用节点类名
_NODE_CHECKPOINT = "1"
_NODE_LATENT = "2"
_NODE_SAMPLER = "3"
_NODE_VAE = "4"
_NODE_SAVE = "5"
# 默认 checkpoint 名（用户可用模型列表切换，或在工作流里改）
DEFAULT_CHECKPOINT = "anything-v5-PrtRE.safetensors"
# 采样器 / 调度器默认值
DEFAULT_SAMPLER = "euler"
DEFAULT_SCHEDULER = "normal"
DEFAULT_STEPS = 25
DEFAULT_CFG = 7.0
# 单次工作流里最多保留多少张图
_MAX_OUTPUTS = 10


def build_default_workflow(
    positive: str,
    negative: str,
    width: int,
    height: int,
    checkpoint: str = "",
    steps: int = DEFAULT_STEPS,
    cfg: float = DEFAULT_CFG,
    seed: int = 0,
    batch: int = 1,
) -> dict:
    """内置的极简文生图工作流（API 格式，节点 id 为字符串）。

    这是「能跑起来」的最小集合：CheckpointLoaderSimple -> CLIPTextEncode(正/负)
    -> EmptyLatentImage -> KSampler -> VAEDecode -> SaveImage。
    用户可在插件配置里贴自己的完整工作流来覆盖它。
    """
    checkpoint = (checkpoint or "").strip() or DEFAULT_CHECKPOINT
    return {
        _NODE_CHECKPOINT: {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": positive, "clip": [_NODE_CHECKPOINT, 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative, "clip": [_NODE_CHECKPOINT, 1]},
        },
        _NODE_LATENT: {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": int(width), "height": int(height), "batch_size": int(batch)},
        },
        _NODE_SAMPLER: {
            "class_type": "KSampler",
            "inputs": {
                "seed": int(seed) if seed else 0,
                "steps": int(steps),
                "cfg": float(cfg),
                "sampler_name": DEFAULT_SAMPLER,
                "scheduler": DEFAULT_SCHEDULER,
                "denoise": 1.0,
                "model": [_NODE_CHECKPOINT, 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": [_NODE_LATENT, 0],
            },
        },
        _NODE_VAE: {
            "class_type": "VAEDecode",
            "inputs": {"samples": [_NODE_SAMPLER, 0], "vae": [_NODE_CHECKPOINT, 2]},
        },
        _NODE_SAVE: {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "astrbot", "images": [_NODE_VAE, 0]},
        },
    }


class ComfyUIProtocol(BaseImageProtocol):
    """ComfyUI 协议：提交工作流后轮询历史记录取图。"""

    protocol_key: str = PROTOCOL_COMFYUI

    def __init__(
        self,
        channel: Channel,
        timeout: float = DEFAULT_TIMEOUT,
        proxy: str = "",
    ) -> None:
        super().__init__(channel, timeout, proxy)
        self.client_id = uuid.uuid4().hex
        # 工作流里有 LoadImage 节点时，记录其 id 以便注入输入图片
        self._load_image_node = ""

    # ------------------------------------------------------------ 主流程
    async def generate(self, req: GenerateRequest) -> GenerateResult:
        """构造工作流 -> 提交 -> 轮询历史 -> 取图。"""
        prompt = self._require_prompt(req)
        workflow = self._build_workflow(req, prompt)
        if req.images and self._load_image_node:
            name = await self._upload_image(req.images[0])
            node = workflow.get(self._load_image_node)
            if isinstance(node, dict):
                inputs = node.get("inputs")
                if isinstance(inputs, dict):
                    inputs["image"] = name
        action = "图片编辑请求" if req.images else "文生图请求"
        prompt_id = await self._submit(workflow, action)
        images = await self._wait_for_images(prompt_id, action)
        if not images:
            raise ProtocolError(
                "ComfyUI 未返回图片：请检查工作流节点是否已正确连接（SaveImage 节点）",
            )
        return self._build_result(
            images,
            model=(self.channel.model or "").strip() or "ComfyUI",
            payload={"prompt_id": prompt_id},
            status=200,
        )

    async def list_models(self) -> list:
        """尽力而为地读取 checkpoint 列表（``/object_info``）；失败返回空列表。"""
        try:
            result = await self._request(
                "GET",
                self._url("object_info/CheckpointLoaderSimple"),
            )
        except ProtocolError:
            return []
        if not result.ok or not isinstance(result.data, dict):
            return []
        node = result.data.get("CheckpointLoaderSimple")
        if not isinstance(node, dict):
            return []
        required = node.get("input", {}).get("required", {}) if isinstance(node.get("input"), dict) else {}
        ckpt = required.get("ckpt_name") if isinstance(required, dict) else None
        names: Any = None
        if isinstance(ckpt, list) and ckpt and isinstance(ckpt[0], list):
            names = ckpt[0]
        if not isinstance(names, list):
            return []
        return [str(item) for item in names if str(item).strip()]

    # ------------------------------------------------------------ 工作流
    def _build_workflow(self, req: GenerateRequest, prompt: str) -> dict:
        """优先使用用户自定义工作流；没有就用内置模板，并把提示词 / 尺寸注入进去。"""
        custom = self._custom_workflow()
        width, height = parse_size_pair(req.size, (1024, 1024), 8)
        count = self._normalize_n(req.n)
        if custom:
            workflow = json.loads(json.dumps(custom))
            self._inject(workflow, prompt, width, height, count)
            self._load_image_node = self._find_load_image(workflow)
            return workflow
        checkpoint = (self.channel.model or "").strip()
        return build_default_workflow(
            positive=prompt,
            negative="",
            width=width,
            height=height,
            checkpoint=checkpoint,
            batch=count,
        )

    def _custom_workflow(self) -> Optional[dict]:
        """从 ``channel.extra["workflow"]`` 读取自定义工作流（JSON 字符串或 dict）。"""
        extra = getattr(self.channel, "extra", None) or {}
        if not isinstance(extra, dict):
            return None
        raw = extra.get("workflow") or extra.get("comfy_workflow")
        if isinstance(raw, dict):
            return raw if raw else None
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except Exception:  # noqa: BLE001 - 用户填的 JSON 可能不合法
                return None
            return parsed if isinstance(parsed, dict) else None
        return None

    @staticmethod
    def _inject(workflow: dict, prompt: str, width: int, height: int, batch: int) -> None:
        """把提示词 / 尺寸写进自定义工作流的对应节点（按 class_type 识别）。"""
        positive_done = False
        for node in workflow.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            class_type = str(node.get("class_type") or "")
            if class_type == "CLIPTextEncode" and not positive_done:
                inputs["text"] = prompt
                positive_done = True
            elif class_type == "EmptyLatentImage":
                inputs["width"] = int(width)
                inputs["height"] = int(height)
                inputs["batch_size"] = int(batch)

    @staticmethod
    def _find_load_image(workflow: dict) -> str:
        """找出第一个 ``LoadImage`` 节点，返回其节点 id（没有则返回空串）。"""
        for node_id, node in workflow.items():
            if isinstance(node, dict) and str(node.get("class_type") or "") == "LoadImage":
                return str(node_id)
        return ""

    # ------------------------------------------------------------ 提交与轮询
    async def _submit(self, workflow: dict, action: str) -> str:
        """提交工作流，返回服务端的 prompt_id。"""
        body = {"prompt": workflow, "client_id": self.client_id}
        result = await self._request("POST", self._url("prompt"), json_body=body)
        result.ensure_ok(action + "（提交工作流）")
        data = result.data if isinstance(result.data, dict) else {}
        prompt_id = str(data.get("prompt_id") or "").strip()
        if not prompt_id:
            # 提交失败时 ComfyUI 会返回 node_errors，尽量把原因透出给用户
            errors = data.get("node_errors") or data.get("error")
            detail = truncate(json.dumps(errors, ensure_ascii=False), 400) if errors else truncate(result.text, 400)
            raise ProtocolError("ComfyUI 未接受该工作流，请检查节点参数与模型名", status=result.status, detail=detail)
        return prompt_id

    async def _wait_for_images(self, prompt_id: str, action: str) -> list:
        """按 base 的轮询节奏查询 ``/history/{prompt_id}``，直到拿到图片。"""
        url = self._url("history/{0}".format(prompt_id))

        async def probe() -> PollOutcome:
            result = await self._request("GET", url)
            if not result.ok:
                return PollOutcome(POLL_FAILED, message="查询任务状态失败（HTTP {0}）".format(result.status))
            return self._parse_history(result.data, prompt_id)

        outcome = await self._poll_until_done(probe, action=action)
        return outcome.images

    def _parse_history(self, payload: Any, prompt_id: str) -> PollOutcome:
        """解析 ``/history/...`` 的返回：还没有记录 = 处理中，有 images = 完成。"""
        if not isinstance(payload, dict):
            return PollOutcome(POLL_PENDING)
        entry = payload.get(prompt_id)
        if entry is None and len(payload) == 1:
            # 有的版本直接用 prompt_id 作为唯一键，这里兜底取唯一一项
            entry = list(payload.values())[0]
        if not isinstance(entry, dict):
            return PollOutcome(POLL_PENDING)
        status = entry.get("status")
        if isinstance(status, dict):
            status_str = str(status.get("status_str") or "").lower()
            completed = bool(status.get("completed"))
            if status_str in ("error", "failed") or (not completed and status.get("messages") and status_str == "error"):
                return PollOutcome(
                    POLL_FAILED,
                    payload=status,
                    message="ComfyUI 执行工作流失败（{0}）".format(status_str or "unknown"),
                )
        outputs = entry.get("outputs")
        if not isinstance(outputs, dict) or not outputs:
            return PollOutcome(POLL_PENDING)
        urls = self._collect_urls(outputs)
        if not urls:
            return PollOutcome(POLL_PENDING)
        images = [GeneratedImage(url=url, mime="image/png") for url in urls[:_MAX_OUTPUTS]]
        return PollOutcome(POLL_DONE, images=images, payload=entry)

    def _collect_urls(self, outputs: dict) -> list:
        """把 ``outputs.*.images[]`` 转成可下载的 ``/view`` 链接。"""
        urls: list = []
        for node in outputs.values():
            if not isinstance(node, dict):
                continue
            entries = node.get("images")
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list):
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                filename = str(item.get("filename") or "").strip()
                if not filename:
                    continue
                params = [("filename", filename)]
                subfolder = str(item.get("subfolder") or "").strip()
                if subfolder:
                    params.append(("subfolder", subfolder))
                params.append(("type", str(item.get("type") or "output")))
                # 用相对路径拼接，交给 _url 补全（ComfyUI 是自部署，不会补 /v1）
                query = "&".join("{0}={1}".format(k, v) for k, v in params)
                urls.append(self._url("view?{0}".format(query)))
        return urls

    async def _upload_image(self, image: Any) -> str:
        """把输入图片上传到 ``/upload/image``，返回服务端给的文件名。"""
        payload = image.data or b""
        if not payload:
            raise ProtocolError("图片编辑失败：输入图片内容为空")
        import aiohttp

        form = aiohttp.FormData()
        form.add_field(
            "image",
            payload,
            filename=(image.filename or "input.png"),
            content_type=(image.mime or "image/png"),
        )
        form.add_field("overwrite", "true")
        result = await self._request("POST", self._url("upload/image"), data=form)
        result.ensure_ok("上传参考图")
        data = result.data if isinstance(result.data, dict) else {}
        name = str(data.get("name") or "").strip()
        if not name:
            raise ProtocolError("ComfyUI 上传参考图失败：未返回文件名", detail=truncate(result.text, 300))
        subfolder = str(data.get("subfolder") or "").strip()
        if subfolder:
            return "{0}/{1}".format(subfolder, name)
        return name

    # ------------------------------------------------------------ 鉴权
    def _headers(self) -> dict:
        """默认无鉴权；密钥栏填「用户名:密码」时走 Basic。"""
        headers = super()._headers()
        key = (self.channel.api_key or "").strip()
        if ":" in key:
            token = base64.b64encode(key.encode("utf-8")).decode("ascii")
            headers.pop("Authorization", None)
            headers["Authorization"] = "Basic {0}".format(token)
        return headers


__all__ = ["ComfyUIProtocol", "build_default_workflow"]
