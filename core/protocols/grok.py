"""Grok 兼容协议：默认走 OpenAI 风格 ``/images/*``，编辑失败时回退到 ``/chat/completions``。

主路径与 OpenAI 兼容协议一致：

* 文生图 ``POST {base}/images/generations``（JSON）；
* 图片编辑 ``POST {base}/images/edits``（multipart）。

部分中转站 / xAI 官方接口并不支持 ``/images/edits``，此时（404 / 405 / 400）
自动回退到 ``POST {base}/chat/completions``：把提示词与图片以
``content`` 数组形式发给多模态模型（图片用 data-url），再从返回文本里
用正则提取 markdown 图片链接或 base64 图片。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..models import (
    PROTOCOL_GROK,
    Channel,
    GeneratedImage,
    GenerateRequest,
    GenerateResult,
    ProtocolError,
)
from .base import (
    DEFAULT_TIMEOUT,
    build_image_from_node,
    decode_b64_image,
    iter_image_nodes,
    sniff_image_mime,
    truncate,
)
from .openai_compat import OpenAICompatProtocol

# 编辑接口不可用（接口不存在 / 方法不允许 / 参数被拒）时回退到 chat/completions
_EDIT_FALLBACK_STATUS = (400, 404, 405, 422, 501)

# markdown 图片：![alt](url) 或 ![alt](<url>)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s<>]+)>?\s*\)")
# 裸图片链接（http/https 且路径像图片）
_BARE_IMAGE_RE = re.compile(r"https?://[^\s\"'<>)\]]+?\.(?:png|jpe?g|webp|gif|bmp)(?:\?[^\s\"'<>)\]]*)?", re.I)
# data-url 图片
_DATA_URL_RE = re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+")
# 裸露的 base64 图片（长度足够长时才认为是图片）
_RAW_B64_RE = re.compile(r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/]{512,}={0,2})(?![A-Za-z0-9+/=])")


class GrokProtocol(OpenAICompatProtocol):
    """Grok 兼容协议（``/images/*`` 优先，``/chat/completions`` 兜底）。"""

    protocol_key: str = PROTOCOL_GROK

    def __init__(
        self,
        channel: Channel,
        timeout: float = DEFAULT_TIMEOUT,
        proxy: str = "",
    ) -> None:
        super().__init__(channel, timeout, proxy)

    # ------------------------------------------------------------------ 主流程
    async def generate(self, req: GenerateRequest) -> GenerateResult:
        """文生图直接走 ``/images/generations``；图片编辑失败时回退到对话接口。"""
        prompt = self._require_prompt(req)
        if not req.images:
            return await self._text_to_image(req, prompt)
        return await self._edit_with_fallback(req, prompt)

    async def list_models(self, include_non_image: bool = False) -> list[str]:
        """读取模型；绘画失败返回空列表，完整目录保留错误供设置页展示。"""
        if include_non_image:
            return await super().list_models(include_non_image=True)
        try:
            return await super().list_models()
        except ProtocolError:
            return []
        except Exception:  # noqa: BLE001 - 模型列表失败不应影响绘画
            return []

    async def test(self) -> tuple[bool, str]:
        """连通性测试：绕开``list_models`` 的空列表兜底，真实反映接口是否可用。"""
        return await self._check_models(lambda: OpenAICompatProtocol.list_models(self))

    # ------------------------------------------------------------------ 编辑流程
    async def _edit_with_fallback(self, req: GenerateRequest, prompt: str) -> GenerateResult:
        """先尝试 ``/images/edits``，不可用时回退到 ``/chat/completions``。"""
        model = self._resolve_model(req, edit=True)
        url = self._url("images/edits")
        response = await self._request(
            "POST",
            url,
            data_factory=lambda: self._build_edit_form(
                req, prompt, model, field_name="image[]"
            ),
        )
        if response.status == 400:
            # 部分中转站只认单数 image 字段，先重试一次
            retry = await self._request(
                "POST",
                url,
                data_factory=lambda: self._build_edit_form(
                req, prompt, model, field_name="image"
            ),
            )
            if retry.ok:
                return self._finish_edit(retry, model)
            response = retry
        if response.ok:
            return self._finish_edit(response, model)
        if self._can_fallback(response.status):
            return await self._edit_via_chat(req, prompt)
        response.ensure_ok("图片编辑请求")
        raise ProtocolError(
            "图片编辑请求失败（HTTP {}）".format(response.status),
            status=response.status,
            detail=truncate(response.text, 500),
        )

    def _finish_edit(self, response: Any, model: str) -> GenerateResult:
        """解析 ``/images/edits`` 成功响应。"""
        images = self._parse_images(response.data)
        if not images:
            raise ProtocolError(
                "接口未返回任何图片数据，请检查模型是否支持图片编辑",
                status=response.status,
                detail=truncate(response.text, 500),
            )
        return self._build_result(images, model=model, payload=response.data, status=response.status)

    async def _edit_via_chat(self, req: GenerateRequest, prompt: str) -> GenerateResult:
        """回退方案：把多图 + 文本发给 ``/chat/completions``，从返回文本里提取图片。"""
        model = self._resolve_model(req, edit=True)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in req.images:
            data = image.data or b""
            if not data:
                continue
            mime = (image.mime or "").strip() or sniff_image_mime(data, "image/png") or "image/png"
            data_url = "data:{};base64,{}".format(mime, image.to_b64())
            content.append({"type": "image_url", "image_url": {"url": data_url}})
        if len(content) == 1:
            raise ProtocolError("图片编辑失败：没有可用的输入图片")
        body = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
        }
        url = self._url("chat/completions")
        response = await self._request("POST", url, json_body=body)
        response.ensure_ok("图片编辑请求（chat/completions 回退）")

        images = self._extract_images_from_chat(response.data)
        if not images:
            raise ProtocolError(
                "接口未返回图片：该中转站可能不支持图片编辑，请改用其它通道或模型",
                status=response.status,
                detail=truncate(response.text, 500),
            )
        return self._build_result(images, model=model, payload=response.data, status=response.status)

    # ------------------------------------------------------------------ 解析
    def _extract_images_from_chat(self, payload: Any) -> list[GeneratedImage]:
        """从 ``chat/completions`` 响应里提取图片（文本里的链接 / base64，或结构化内容）。"""
        images: list[GeneratedImage] = []
        texts: list[str] = []

        if isinstance(payload, dict):
            choices = payload.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    message = choice.get("message") or choice.get("delta") or {}
                    if not isinstance(message, dict):
                        continue
                    content = message.get("content")
                    if isinstance(content, str):
                        texts.append(content)
                    elif isinstance(content, list):
                        for item in content:
                            if not isinstance(item, dict):
                                continue
                            item_type = str(item.get("type") or "").lower()
                            text = item.get("text")
                            if isinstance(text, str) and text.strip():
                                texts.append(text)
                            if item_type in ("image", "output_image", "image_url", "input_image"):
                                image = build_image_from_node(item)
                                if image is not None and not image.is_empty():
                                    images.append(image)
                            nested = item.get("image_url")
                            if isinstance(nested, dict):
                                image = build_image_from_node(nested)
                                if image is not None and not image.is_empty():
                                    images.append(image)

        # 兜底：兼容返回体里直接塞了 images[] / data[] 的情况
        for node in iter_image_nodes(payload):
            image = build_image_from_node(node)
            if image is not None and not image.is_empty():
                images.append(image)

        for text in texts:
            images.extend(self._extract_images_from_text(text))

        unique: list[GeneratedImage] = []
        seen: set[str] = set()
        for image in images:
            token = image.url or ""
            if not token and image.data:
                token = "{}:{}".format(len(image.data), image.data[:32])
            if token in seen:
                continue
            seen.add(token)
            unique.append(image)
        return unique

    def _extract_images_from_text(self, text: str) -> list[GeneratedImage]:
        """从模型返回的文本里提取图片链接 / base64 图片。"""
        images: list[GeneratedImage] = []
        if not text:
            return images

        for match in _DATA_URL_RE.finditer(text):
            image = decode_b64_image(match.group(0), "")
            if image is not None:
                images.append(image)
        consumed = {(match.start(), match.end()) for match in _DATA_URL_RE.finditer(text)}

        for match in _MD_IMAGE_RE.finditer(text):
            target = match.group(1).strip().strip("\"'")
            if not target:
                continue
            if target.startswith("data:"):
                image = decode_b64_image(target, "")
            elif target.startswith(("http://", "https://")):
                image = GeneratedImage(url=target, mime="image/png")
            else:
                image = decode_b64_image(target, "", require_image_magic=True)
            if image is not None and not image.is_empty():
                images.append(image)
            consumed.add((match.start(), match.end()))

        for match in _BARE_IMAGE_RE.finditer(text):
            span = (match.start(), match.end())
            if span in consumed or any(start <= span[0] < end for start, end in consumed):
                continue
            images.append(GeneratedImage(url=match.group(0), mime="image/png"))
            consumed.add(span)

        for match in _RAW_B64_RE.finditer(text):
            span = (match.start(), match.end())
            if any(start <= span[0] < end for start, end in consumed):
                continue
            image = decode_b64_image(match.group(1), "", require_image_magic=True)
            if image is not None:
                images.append(image)
        return images

    # ------------------------------------------------------------------ 文生图
    async def _text_to_image(self, req: GenerateRequest, prompt: str) -> GenerateResult:
        """文生图：优先 ``/images/generations``，失败时回退对话接口。"""
        try:
            return await super()._text_to_image(req, prompt)
        except ProtocolError as exc:
            if exc.status is None or not self._can_fallback(exc.status):
                raise
            return await self._draw_via_chat(req, prompt)

    @staticmethod
    def _can_fallback(status: Optional[int]) -> bool:
        """判断该 HTTP 状态是否值得尝试 ``/chat/completions`` 回退。"""
        if status is None:
            return False
        return status in _EDIT_FALLBACK_STATUS or status >= 500

    async def _draw_via_chat(self, req: GenerateRequest, prompt: str) -> GenerateResult:
        """回退方案：用 ``/chat/completions`` 做文生图。"""
        model = self._resolve_model(req, edit=False)
        body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        url = self._url("chat/completions")
        response = await self._request("POST", url, json_body=body)
        response.ensure_ok("文生图请求（chat/completions 回退）")
        images = self._extract_images_from_chat(response.data)
        if not images:
            raise ProtocolError(
                "接口未返回图片：该中转站可能不支持图片生成，请改用其它通道或模型",
                status=response.status,
                detail=truncate(response.text, 500),
            )
        return self._build_result(images, model=model, payload=response.data, status=response.status)


__all__ = ["GrokProtocol"]
