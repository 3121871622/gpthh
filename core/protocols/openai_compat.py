"""OpenAI 兼容协议：``/images/generations`` 文生图 + ``/images/edits`` 图片编辑。

兼容官方 OpenAI、绝大多数中转站以及各类「OpenAI 兼容」图像接口：

* 文生图使用 JSON 请求；尺寸为 ``auto`` 时不传 ``size`` 字段（部分站点不认 auto）；
* 图片编辑使用 ``multipart/form-data``，优先用 ``image[]`` 字段，若站点返回
  400/422 则自动退化为重复的 ``image`` 字段重试一次；
* 响应解析兼容 ``data[]`` / ``images[]`` / ``output[]`` / ``result.data[]`` 等常见包装，
  单个条目里既支持 ``b64_json``（含 ``data:image/png;base64,`` 前缀），也支持 ``url``。
"""

from __future__ import annotations

from typing import Any

import aiohttp

from ..models import (
    PROTOCOL_OPENAI,
    Channel,
    GeneratedImage,
    GenerateRequest,
    GenerateResult,
    ProtocolError,
)
from .base import (
    DEFAULT_TIMEOUT,
    BaseImageProtocol,
    build_image_from_node,
    iter_image_nodes,
    sniff_image_mime,
    truncate,
)

# 明显不是图像模型的名称片段，用于过滤模型列表
_NON_IMAGE_HINTS = (
    "embedding",
    "embed",
    "whisper",
    "tts",
    "speech",
    "audio",
    "rerank",
    "moderation",
    "transcribe",
)
# 图像模型常见关键词，用于让模型列表更符合直觉（排在前面）
_IMAGE_HINTS = (
    "image",
    "dall-e",
    "dalle",
    "gpt-image",
    "flux",
    "stable-diffusion",
    "sdxl",
    "sd3",
    "seedream",
    "seededit",
    "qwen-image",
    "kolors",
    "jimeng",
    "hunyuan",
    "nano-banana",
    "grok",
    "imagen",
)

# mime -> 文件扩展名（用于 multipart 文件名，部分站点会按扩展名判断格式）
_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}


def _extension_for_mime(mime: str) -> str:
    """根据 MIME 返回常见图片扩展名，未知类型默认 ``.png``。"""
    return _MIME_EXTENSIONS.get((mime or "").strip().lower(), ".png")


class OpenAICompatProtocol(BaseImageProtocol):
    """OpenAI 兼容图像接口协议。"""

    protocol_key: str = PROTOCOL_OPENAI

    def __init__(
        self,
        channel: Channel,
        timeout: float = DEFAULT_TIMEOUT,
        proxy: str = "",
    ) -> None:
        super().__init__(channel, timeout, proxy)

    # ------------------------------------------------------------------ 主流程
    async def generate(self, req: GenerateRequest) -> GenerateResult:
        """文生图（无输入图片）或图片编辑 / 多图编辑（1..N 张输入图片）。"""
        prompt = self._require_prompt(req)
        if req.images:
            return await self._edit(req, prompt)
        return await self._text_to_image(req, prompt)

    async def list_models(self, include_non_image: bool = False) -> list[str]:
        """``GET {base}/models``，取 ``data[].id``（兼容 ``models[].name``）。

        默认仍只返回适合出图的模型；设置 ``include_non_image`` 时保留文本、
        视觉等模型，供翻译和图片转提示词功能选择。
        """
        if include_non_image:
            from ..assist import _headers_for, api_url

            url = api_url(self.channel.base_url, self.channel.protocol, "models")
            result = await self._request("GET", url, headers=_headers_for(self.channel))
        else:
            url = self._url("models")
            result = await self._request("GET", url)
        result.ensure_ok("获取模型列表")
        return self._parse_models(result.data, include_non_image=include_non_image)

    # ------------------------------------------------------------------ 文生图
    async def _text_to_image(self, req: GenerateRequest, prompt: str) -> GenerateResult:
        model = self._resolve_model(req, edit=False)
        size = self._normalize_size(req.size)
        count = self._normalize_n(req.n)
        body: dict[str, Any] = {"model": model, "prompt": prompt, "n": count}
        if size and size.lower() != "auto":
            body["size"] = size
        quality = str(getattr(req, "quality", "auto") or "auto").lower()
        if quality in {"auto", "low", "medium", "high", "xhigh", "max"}:
            body["quality"] = quality
        if getattr(req, "transparent_background", False):
            body["background"] = "transparent"
        url = self._url("images/generations")
        result = await self._request("POST", url, json_body=body)
        result.ensure_ok("文生图请求")
        images = self._parse_images(result.data)
        if not images:
            raise ProtocolError(
                "接口未返回任何图片数据，请检查模型名与接口地址是否正确",
                status=result.status,
                detail=truncate(result.text, 500),
            )
        return self._build_result(images, model=model, payload=result.data, status=result.status)

    # ------------------------------------------------------------------ 图片编辑
    async def _edit(self, req: GenerateRequest, prompt: str) -> GenerateResult:
        model = self._resolve_model(req, edit=True)
        url = self._url("images/edits")
        first = await self._request(
            "POST",
            url,
            data_factory=lambda: self._build_edit_form(
                req, prompt, model, field_name="image[]"
            ),
        )
        result = first
        if first.status in (400, 422):
            # 部分中转站只认单数 image 字段，退化为重复字段重试一次
            retry = await self._request(
                "POST",
                url,
                data_factory=lambda: self._build_edit_form(
                    req, prompt, model, field_name="image"
                ),
            )
            if retry.ok:
                result = retry
            else:
                raise ProtocolError(
                    "图片编辑请求失败（HTTP {}）：{}".format(
                        retry.status,
                        truncate(retry.error_message() or first.error_message(), 160),
                    ),
                    status=retry.status,
                    detail=truncate("第一次尝试（image[]）：{}\n第二次尝试（image）：{}".format(
                        truncate(first.text, 200), truncate(retry.text, 280)
                    ), 500),
                )
        result.ensure_ok("图片编辑请求")
        images = self._parse_images(result.data)
        if not images:
            raise ProtocolError(
                "接口未返回任何图片数据，请检查模型是否支持图片编辑",
                status=result.status,
                detail=truncate(result.text, 500),
            )
        return self._build_result(images, model=model, payload=result.data, status=result.status)

    def _build_edit_form(
        self,
        req: GenerateRequest,
        prompt: str,
        model: str,
        *,
        field_name: str = "image[]",
    ) -> aiohttp.FormData:
        """构造图片编辑的 multipart 表单（每个输入图片一个字段）。"""
        form = aiohttp.FormData()
        for index, image in enumerate(req.images, start=1):
            data = image.data or b""
            if not data:
                continue
            # content-type 用图片真实 mime：优先文件头嗅探，其次调用方声明
            mime = sniff_image_mime(data, "") or (image.mime or "").strip() or "image/png"
            filename = "img{}{}".format(index, _extension_for_mime(mime))
            form.add_field(
                field_name,
                data,
                filename=filename,
                content_type=mime,
            )
        form.add_field("prompt", prompt)
        form.add_field("model", model)
        size = self._normalize_size(req.size)
        if size and size.lower() != "auto":
            form.add_field("size", size)
        quality = str(getattr(req, "quality", "auto") or "auto").lower()
        if quality in {"auto", "low", "medium", "high", "xhigh", "max"}:
            form.add_field("quality", quality)
        if getattr(req, "transparent_background", False):
            form.add_field("background", "transparent")
        form.add_field("n", str(self._normalize_n(req.n)))
        return form

    # ------------------------------------------------------------------ 解析
    def _parse_images(self, payload: Any) -> list[GeneratedImage]:
        """从响应里解析出所有图片（兼容多种包装结构）。"""
        images: list[GeneratedImage] = []
        for node in iter_image_nodes(payload):
            image = build_image_from_node(node)
            if image is not None and not image.is_empty():
                images.append(image)
        return images

    def _parse_models(self, payload: Any, include_non_image: bool = False) -> list[str]:
        """解析嵌套模型目录；完整目录保留全部有效名称，绘画目录沿用原过滤规则。"""
        entries: Any = None
        if isinstance(payload, dict):
            for key in ("data", "models", "result", "list", "items"):
                value = payload.get(key)
                if isinstance(value, list) and value:
                    entries = value
                    break
            if entries is None:
                for key in ("data", "models", "result", "response", "list", "items"):
                    value = payload.get(key)
                    if isinstance(value, dict):
                        entries = self._parse_models(value, include_non_image=include_non_image)
                        if entries:
                            return entries
        elif isinstance(payload, list):
            entries = payload
        if not isinstance(entries, list):
            return []

        names: list[str] = []
        for item in entries:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict):
                name = next(
                    (item[key].strip() for key in ("id", "name", "model", "model_name")
                     if isinstance(item.get(key), str) and item[key].strip()),
                    "",
                )
            else:
                continue
            if name:
                names.append(name)

        unique = sorted(set(names), key=lambda value: value.lower())
        if include_non_image:
            return unique
        filtered = [name for name in unique if not self._is_non_image_model(name)]
        if not filtered:
            return unique
        return sorted(filtered, key=self._model_sort_key)

    @staticmethod
    def _is_non_image_model(name: str) -> bool:
        """判断模型名是否明显与图片无关。"""
        lowered = name.lower()
        return any(hint in lowered for hint in _NON_IMAGE_HINTS)

    @staticmethod
    def _model_sort_key(name: str) -> tuple[int, str]:
        """图像相关模型排前面，其余按字母序。"""
        lowered = name.lower()
        priority = 0 if any(hint in lowered for hint in _IMAGE_HINTS) else 1
        return priority, lowered


__all__ = ["OpenAICompatProtocol"]
