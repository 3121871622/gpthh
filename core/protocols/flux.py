"""Flux 中转协议：OpenAI 风格的 ``/images/generations`` 与 ``/images/edits``。

请求形态（与 OpenAI 兼容，但中转站实现差异较大）：

* 文生图：``POST {base}/images/generations``（JSON，含 ``model`` / ``prompt`` / ``n`` / ``size``）；
  多数 Flux 中转站只认单张，故 ``n`` 大于 1 时会在响应只有一张的情况下不报错；
* 图片编辑：``POST {base}/images/edits``（multipart）。Flux 模型普遍**不支持**图片编辑，
  因此当编辑接口返回 404 / 405 / 400 等「接口不存在 / 不支持」信号时，
  自动**降级为纯生成**——把输入图片当作「参考图」写进提示词，仍然给用户一张图，
  而不是直接报错。

鉴权：``Authorization: Bearer <key>``（与 OpenAI 一致）。
"""

from __future__ import annotations

from typing import Any, Optional

from ..models import (
    PROTOCOL_FLUX,
    Channel,
    GeneratedImage,
    GenerateRequest,
    GenerateResult,
    ProtocolError,
)
from .base import DEFAULT_TIMEOUT, truncate
from .openai_compat import OpenAICompatProtocol

# 编辑接口不可用（接口不存在 / 不支持 multipart / 参数被拒）时降级为纯生成
_EDIT_UNAVAILABLE_STATUS = (400, 404, 405, 415, 422, 501)


class FluxProtocol(OpenAICompatProtocol):
    """Flux 中转协议：OpenAI 风格请求 + 编辑不可用时降级为纯生成。"""

    protocol_key: str = PROTOCOL_FLUX

    def __init__(
        self,
        channel: Channel,
        timeout: float = DEFAULT_TIMEOUT,
        proxy: str = "",
    ) -> None:
        super().__init__(channel, timeout, proxy)

    # ------------------------------------------------------------------ 主流程
    async def generate(self, req: GenerateRequest) -> GenerateResult:
        """文生图走 ``/images/generations``；图片编辑优先 ``/images/edits``。"""
        prompt = self._require_prompt(req)
        if not req.images:
            return await self._text_to_image(req, prompt)
        return await self._edit_with_fallback(req, prompt)

    async def list_models(self, include_non_image: bool = False) -> list:
        """读取模型；绘画失败返回空列表，完整目录保留错误供设置页展示。"""
        if include_non_image:
            return await super().list_models(include_non_image=True)
        try:
            return await super().list_models()
        except ProtocolError:
            return []
        except Exception:  # noqa: BLE001 - 模型列表失败不应影响绘画
            return []

    async def test(self) -> tuple:
        """连通性测试：绕开 ``list_models`` 的空列表兜底，真实反映接口是否可用。"""
        return await self._check_models(lambda: OpenAICompatProtocol.list_models(self))

    # ------------------------------------------------------------------ 编辑流程
    async def _edit_with_fallback(self, req: GenerateRequest, prompt: str) -> GenerateResult:
        """先试 ``/images/edits``；接口不存在或不支持时降级为纯生成。"""
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
            # 部分站点只认单数 image 字段，先原地重试一次再考虑降级
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
        if response.status in _EDIT_UNAVAILABLE_STATUS or response.status >= 500:
            return await self._draw_with_reference(req, prompt)
        response.ensure_ok("图片编辑请求")
        raise ProtocolError(
            "图片编辑请求失败（HTTP {0}）".format(response.status),
            status=response.status,
            detail=truncate(response.text, 500),
        )

    def _finish_edit(self, response: Any, model: str) -> GenerateResult:
        """解析 ``/images/edits`` 的成功响应。"""
        images = self._parse_images(response.data)
        if not images:
            raise ProtocolError(
                "接口未返回任何图片数据，请检查模型是否支持图片编辑",
                status=response.status,
                detail=truncate(response.text, 500),
            )
        return self._build_result(images, model=model, payload=response.data, status=response.status)

    async def _draw_with_reference(self, req: GenerateRequest, prompt: str) -> GenerateResult:
        """降级方案：把输入图片当作参考图写进提示词，仍走纯生成。

        说明：这里不会真的把图片发给模型（Flux 的中转站普遍不支持图片输入），
        只是保证用户能拿到一张图；提示词里会标注参考图数量，方便用户理解结果。
        """
        count = len([item for item in req.images if item.data])
        if count <= 0:
            raise ProtocolError("图片编辑失败：没有读取到可用的输入图片")
        merged = "{0}\n（参考图 {1} 张：该接口不支持图片编辑，已按纯文本描述生成）".format(prompt, count)
        generated = await self._text_to_image(
            GenerateRequest(
                prompt=merged,
                size=req.size,
                model=req.model,
                n=req.n,
                quality=req.quality,
                transparent_background=req.transparent_background,
            ),
            merged,
        )
        return generated


__all__ = ["FluxProtocol"]
