"""Gemini 兼容协议：``/v1beta/models/{model}:generateContent`` 文生图与图片编辑。

要点：

* 鉴权优先使用 ``x-goog-api-key`` 请求头；若用户在 base_url 里已经带了 ``?key=``，
  则不再重复添加，避免站点报「重复密钥」错误；
* base_url 支持 ``https://generativelanguage.googleapis.com`` 或
  ``https://xxx/v1beta``（已以 ``/v1beta`` 结尾时不会重复追加）；
* 多图编辑 = content.parts 里放多个 ``inlineData`` 片段（官方文档 camelCase）；
* 响应解析 ``candidates[0].content.parts[]``，同时兼容中转站
  把图片包在 ``data[].b64_json`` 里的情况。
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import parse_qsl, quote

from ..models import (
    PROTOCOL_GEMINI,
    Channel,
    GeneratedImage,
    GenerateRequest,
    GenerateResult,
    ProtocolError,
    SIZE_RATIO_MAP,
    normalize_size,
    ratio_of_size,
)
from .base import (
    DEFAULT_TIMEOUT,
    BaseImageProtocol,
    build_image_from_node,
    decode_b64_image,
    iter_image_nodes,
    parse_size_pair,
    truncate,
)

# 生成配置：要求返回图片 + 文本
_RESPONSE_MODALITIES = ["IMAGE", "TEXT"]


class GeminiProtocol(BaseImageProtocol):
    """Gemini 兼容图像协议（Nano Banana / Gemini 图像模型 / 中转站）。"""

    protocol_key: str = PROTOCOL_GEMINI

    def __init__(
        self,
        channel: Channel,
        timeout: float = DEFAULT_TIMEOUT,
        proxy: str = "",
    ) -> None:
        super().__init__(channel, timeout, proxy)

    # ------------------------------------------------------------------ 主流程
    async def generate(self, req: GenerateRequest) -> GenerateResult:
        """调用 ``generateContent`` 完成文生图 / 单图编辑 / 多图编辑。"""
        prompt = self._require_prompt(req)
        is_edit = bool(req.images)
        model = self._resolve_model(req, edit=is_edit)
        model = self._strip_model_prefix(model)
        if not model:
            raise ProtocolError("未配置模型名，请在插件设置里填写 Gemini 图像模型（例如 gemini-2.5-flash-image）")
        root, base_params = self._api_root()
        url = "{}/models/{}:generateContent".format(root, quote(model, safe=""))

        parts: list[dict[str, Any]] = [{"text": prompt}]
        for image in req.images:
            payload = image.data or b""
            if not payload:
                continue
            parts.append(
                {
                    "inlineData": {
                        "mimeType": (image.mime or "").strip() or "image/png",
                        "data": image.to_b64(),
                    }
                }
            )

        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": list(_RESPONSE_MODALITIES)},
        }
        image_config = self._image_config(req.size)
        if image_config:
            body["generationConfig"]["imageConfig"] = image_config
        action = "文生图请求" if not is_edit else "图片编辑请求"
        result = await self._request("POST", url, json_body=body, params=base_params or None)

        # 一部分中转站对请求体写法比较挑剔，按以下顺序尝试回退：
        #   1) 原样重试：去掉 generationConfig（部分中转站不认这个字段）
        #   2) 将 parts 里的 inlineData/mimeType 换成 snake_case（部分中转站只认 SDK 写法）
        #   3) 两者同时应用
        if not result.ok and result.status in (400, 422):
            attempts: list[dict[str, Any]] = []
            generation = body.get("generationConfig")
            if isinstance(generation, dict) and "imageConfig" in generation:
                without_image_config = dict(body)
                without_image_config["generationConfig"] = dict(generation)
                without_image_config["generationConfig"].pop("imageConfig", None)
                attempts.append(without_image_config)
            stripped = dict(body)
            stripped.pop("generationConfig", None)
            attempts.append(stripped)
            if is_edit:
                attempts.append(_snake_case_parts(body))
                attempts.append(_snake_case_parts(stripped))
            for attempt in attempts:
                retry = await self._request("POST", url, json_body=attempt, params=base_params or None)
                if retry.ok:
                    result = retry
                    break
            else:
                raise ProtocolError(
                    "{}失败（HTTP {}）：{}".format(action, result.status, truncate(result.error_message(), 160)),
                    status=result.status,
                    detail=truncate(result.text, 500),
                )
        result.ensure_ok(action)

        images, texts, block_reason = self._parse_response(result.data)
        if not images:
            if block_reason:
                raise ProtocolError(
                    "请求被模型安全策略拦截：{}".format(block_reason),
                    status=result.status,
                    detail=truncate(result.text, 500),
                )
            raise ProtocolError(
                "接口未返回任何图片数据，请确认模型名称是否支持图片输出",
                status=result.status,
                detail=truncate(result.text, 500),
            )
        revised = "\n".join(texts).strip()
        if revised:
            for image in images:
                if not image.revised_prompt:
                    image.revised_prompt = revised
        return self._build_result(images, model=model, payload=result.data, status=result.status)

    @staticmethod
    def _image_config(size: Any) -> dict[str, str]:
        """把插件尺寸转换成 Gemini 的比例 / 分辨率提示。"""
        normalized = normalize_size(str(size or ""), "")
        if not normalized or normalized == "auto":
            return {}
        width, height = parse_size_pair(normalized, (1024, 1024), 0)
        config: dict[str, str] = {}
        ratio = SIZE_RATIO_MAP.get(normalized) or ratio_of_size(normalized)
        if ratio in {
            "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4",
            "9:16", "16:9", "21:9",
        }:
            config["aspectRatio"] = ratio
        longest = max(width, height)
        if longest >= 3500:
            config["imageSize"] = "4K"
        elif longest >= 1800:
            config["imageSize"] = "2K"
        else:
            config["imageSize"] = "1K"
        return config

    async def list_models(self, include_non_image: bool = False) -> list[str]:
        """读取 Gemini 模型；完整目录沿用辅助请求地址并保留所有模型和分页。"""
        if include_non_image:
            from ..assist import _headers_for, api_url

            url = api_url(self.channel.base_url, self.channel.protocol, "models")
            headers = _headers_for(self.channel, gemini=True)
            base_params = []
        else:
            root, base_params = self._api_root()
            url = "{}/models".format(root)
            headers = None
        entries: list = []
        page_tokens: set = set()
        params = base_params or None
        while True:
            result = await self._request("GET", url, params=params, headers=headers)
            result.ensure_ok("获取模型列表")
            entries.extend(self._find_model_entries(result.data))
            token = result.data.get("nextPageToken") if isinstance(result.data, dict) else None
            if not include_non_image or not isinstance(token, str) or not token:
                break
            if token in page_tokens:
                raise ProtocolError("模型目录分页重复，请检查供应商的 nextPageToken")
            page_tokens.add(token)
            params = {"pageToken": token}
        models: list[str] = []
        for entry in entries:
            if isinstance(entry, str):
                name = entry.strip()
                methods = None
            elif isinstance(entry, dict):
                name = next(
                    (entry[key].strip() for key in ("name", "id")
                     if isinstance(entry.get(key), str) and entry[key].strip()),
                    "",
                )
                methods = entry.get("supportedGenerationMethods")
            else:
                continue
            if not name:
                continue
            if not include_non_image and isinstance(methods, list):
                normalized = [str(item) for item in methods]
                if normalized and "generateContent" not in normalized:
                    continue
            models.append(self._strip_model_prefix(name))
        return sorted({item for item in models if item}, key=lambda value: value.lower())

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _find_model_entries(payload: Any) -> list:
        """从模型列表响应里找出存有模型条目的数组。

        兼容官方 ``{"models": [...]}`` 与部分中转站的 ``{"data": [...]}`` /
        ``{"result": {"data": [...]}}`` 包装。
        """
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        for key in ("models", "data", "result", "list", "items"):
            value = payload.get(key)
            if isinstance(value, list) and value:
                return value
        for key in ("result", "data", "response"):
            value = payload.get(key)
            if isinstance(value, dict):
                nested = GeminiProtocol._find_model_entries(value)
                if nested:
                    return nested
        return []

    def _headers(self) -> dict[str, str]:
        """Gemini 鉴权头：优先 ``x-goog-api-key``，base_url 已带 ``key=`` 时不再重复添加。"""
        headers = {"Accept": "application/json"}
        key = (self.channel.api_key or "").strip()
        if not key:
            return headers
        _, base_query = self._split_base(self.channel.base_url)
        if "key=" in base_query:
            return headers
        headers["x-goog-api-key"] = key
        return headers

    def _api_root(self) -> tuple[str, list[tuple[str, str]]]:
        """返回 ``(到 /v1beta 为止的根地址, base_url 自带的查询参数)``。"""
        main, query = self._split_base(self.channel.base_url)
        if not main:
            raise ProtocolError("未配置接口链接（base_url），请在插件设置里填写 Gemini 接口地址")
        main = main.rstrip("/")
        lowered = main.lower()
        if lowered.endswith("/v1beta/models"):
            main = main[: -len("/models")]
            lowered = main.lower()
        if not lowered.endswith("/v1beta"):
            main = "{}/v1beta".format(main)
        params = parse_qsl(query, keep_blank_values=True) if query else []
        return main, params

    @staticmethod
    def _split_base(base: str) -> tuple[str, str]:
        """把 base_url 拆成「主体 + 查询串」。"""
        raw = (base or "").strip()
        main, _, query = raw.partition("?")
        return main.strip(), query.strip()

    @staticmethod
    def _strip_model_prefix(model: str) -> str:
        """去掉模型名里可能带的 ``models/`` 前缀。"""
        name = (model or "").strip()
        if name.lower().startswith("models/"):
            name = name[len("models/"):]
        return name

    def _parse_response(self, payload: Any) -> tuple[list[GeneratedImage], list[str], str]:
        """解析响应，返回 ``(图片列表, 文本片段, 拦截原因)``。"""
        images: list[GeneratedImage] = []
        texts: list[str] = []
        block_reason = ""
        seen: set[str] = set()

        def _add(image: Optional[GeneratedImage]) -> None:
            if image is None or image.is_empty():
                return
            token = image.url or ""
            if not token and image.data:
                token = "{}:{}".format(len(image.data), image.data[:48])
            if token in seen:
                return
            seen.add(token)
            images.append(image)

        if isinstance(payload, dict):
            feedback = payload.get("promptFeedback")
            if isinstance(feedback, dict):
                block_reason = str(feedback.get("blockReason") or feedback.get("blockReasonMessage") or "")

            candidates = payload.get("candidates")
            if isinstance(candidates, list):
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    if not block_reason:
                        finish = str(candidate.get("finishReason") or "")
                        if finish and finish.upper() not in ("STOP", "MAX_TOKENS", "FINISH_REASON_UNSPECIFIED"):
                            block_reason = finish
                    content = candidate.get("content")
                    if isinstance(content, dict):
                        parts = content.get("parts")
                    elif isinstance(content, list):
                        parts = content
                    else:
                        parts = None
                    if isinstance(parts, list):
                        for part in parts:
                            self._collect_part(part, _add, texts)

        # 兜底：兼容中转站把图片放在 data[].b64_json / images[] 等情况
        for node in iter_image_nodes(payload):
            _add(build_image_from_node(node))

        return images, texts, block_reason

    @staticmethod
    def _collect_part(part: Any, add_image: Any, texts: list[str]) -> None:
        """从单个 part 中提取图片或文本。"""
        if not isinstance(part, dict):
            return
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict):
            data = inline.get("data") or inline.get("b64_json") or inline.get("bytesBase64Encoded")
            mime = inline.get("mimeType") or inline.get("mime_type") or inline.get("mime") or ""
            if isinstance(data, str) and data.strip():
                image = decode_b64_image(data, str(mime))
                if image is not None:
                    add_image(image)
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
        part_type = str(part.get("type") or "").lower()
        if part_type in ("image", "output_image", "image_url"):
            image = build_image_from_node(part)
            if image is not None:
                add_image(image)


__all__ = ["GeminiProtocol"]
