"""
Stable Diffusion WebUI（AUTOMATIC1111 / Forge）协议。

请求形态（**自部署**，不走 /v1，也不补版本段）：

* 文生图：``POST {base}/sdapi/v1/txt2img``，JSON 体含 ``prompt`` / ``negative_prompt`` /
  ``width`` / ``height`` / ``steps`` / ``cfg_scale`` / ``batch_size``；
* 图片编辑：``POST {base}/sdapi/v1/img2img``，在文生图参数上追加
  ``init_images``（base64 数组）与 ``denoising_strength``（重绘幅度）；
* 模型列表：``GET {base}/sdapi/v1/sd-models``，取各条目的 ``title`` / ``model_name``。

响应形态：``images`` 为 base64（可带 ``data:image/png;base64,`` 前缀）数组，取第一张即结果。

鉴权：WebUI 默认无鉴权；若用 ``--api-auth`` 启动则走 HTTP Basic，
此时把「用户名:密码」直接填在插件的密钥栏即可（插件会转成 Basic 头）。
"""

from __future__ import annotations

import base64
from typing import Any

from ..models import (
    PROTOCOL_SDWEBUI,
    Channel,
    GeneratedImage,
    GenerateRequest,
    GenerateResult,
    ProtocolError,
)
from .base import (
    DEFAULT_TIMEOUT,
    BaseImageProtocol,
    decode_b64_image,
    parse_size_pair,
    truncate,
)

# SD 的宽高必须是 8 的整数倍
_SIZE_MULTIPLE = 8
# 采样步数 / 提示词引导强度的出厂默认（WebUI 自身默认即 20 / 7）
DEFAULT_STEPS = 28
DEFAULT_CFG_SCALE = 7.0
# img2img 的默认重绘幅度（太低没变化，太高会完全变样）
DEFAULT_DENOISING = 0.6
# 负向提示词默认值（去掉常见崩坏特征）
DEFAULT_NEGATIVE = "lowres, bad anatomy, bad hands, text, error, watermark, blurry"


class SDWebUIProtocol(BaseImageProtocol):
    """Stable Diffusion WebUI 协议（自部署，不补 /v1）。"""

    protocol_key: str = PROTOCOL_SDWEBUI

    def __init__(
        self,
        channel: Channel,
        timeout: float = DEFAULT_TIMEOUT,
        proxy: str = "",
    ) -> None:
        super().__init__(channel, timeout, proxy)

    # ------------------------------------------------------------ 主流程
    async def generate(self, req: GenerateRequest) -> GenerateResult:
        """有输入图片走 img2img，否则走 txt2img。"""
        prompt = self._require_prompt(req)
        editing = bool(req.images)
        body = self._build_body(req, prompt, editing=editing)
        if editing:
            endpoint, action = "sdapi/v1/img2img", "图片编辑请求"
        else:
            endpoint, action = "sdapi/v1/txt2img", "文生图请求"
        result = await self._request("POST", self._url(endpoint), json_body=body)
        result.ensure_ok(action)
        images = self._parse_images(result.data)
        if not images:
            raise ProtocolError(
                "接口未返回任何图片数据，请确认 WebUI 已用 API 模式启动，且模型已加载",
                status=result.status,
                detail=truncate(result.text, 500),
            )
        return self._build_result(
            images,
            model=self._model_name(),
            payload=result.data,
            status=result.status,
        )

    async def list_models(self) -> list:
        """读取 ``sdapi/v1/sd-models``；取 title（回退 model_name / filename）。"""
        result = await self._request("GET", self._url("sdapi/v1/sd-models"))
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
                name = str(
                    item.get("title")
                    or item.get("model_name")
                    or item.get("name")
                    or item.get("filename")
                    or ""
                ).strip()
            else:
                continue
            if name:
                names.append(name)
        return sorted(set(names), key=lambda value: value.lower())


    # ------------------------------------------------------------ 鉴权
    def _headers(self) -> dict:
        """WebUI 默认不校验；用 ``--api-auth`` 启动时，密钥栏填「用户名:密码」走 Basic 鉴权。"""
        headers = super()._headers()
        key = (self.channel.api_key or "").strip()
        if ":" in key:
            token = base64.b64encode(key.encode("utf-8")).decode("ascii")
            headers.pop("Authorization", None)
            headers["Authorization"] = "Basic {0}".format(token)
        return headers


    # ------------------------------------------------------------ 参数组装
    def _build_body(self, req: GenerateRequest, prompt: str, *, editing: bool) -> dict:
        """把统一的请求对象翻译成 WebUI 的 ``txt2img`` / ``img2img`` 参数。"""
        width, height = parse_size_pair(req.size, (1024, 1024), _SIZE_MULTIPLE)
        body: dict = {
            "prompt": prompt,
            "negative_prompt": DEFAULT_NEGATIVE,
            "width": width,
            "height": height,
            "steps": DEFAULT_STEPS,
            "cfg_scale": DEFAULT_CFG_SCALE,
            "sampler_name": "DPM++ 2M Karras",
            "batch_size": self._normalize_n(req.n),
        }
        if editing:
            encoded: list = []
            for image in req.images:
                payload = image.data or b""
                if not payload:
                    continue
                encoded.append(base64.b64encode(payload).decode("ascii"))
            if not encoded:
                raise ProtocolError("图片编辑失败：没有读取到可用的输入图片")
            body["init_images"] = encoded
            body["denoising_strength"] = DEFAULT_DENOISING
        return body

    def _model_name(self) -> str:
        """请求体里不回显模型名时用通道配置兜底（便于日志与文案展示）。"""
        return (self.channel.model or "").strip() or "SD WebUI"

    # ------------------------------------------------------------ 解析
    def _parse_images(self, payload: Any) -> list:
        """解析 ``images`` 数组（base64 或 data-url）。"""
        if not isinstance(payload, dict):
            return []
        entries = payload.get("images")
        if not isinstance(entries, list):
            return []
        images: list = []
        for entry in entries:
            if isinstance(entry, dict):
                entry = entry.get("image") or entry.get("data") or ""
            if not isinstance(entry, str) or not entry.strip():
                continue
            image = decode_b64_image(entry, "image/png")
            if image is not None and not image.is_empty():
                images.append(image)
        return images


__all__ = ["SDWebUIProtocol"]
