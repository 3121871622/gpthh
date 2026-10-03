# -*- coding: utf-8 -*-
"""智能辅助：中文提示词翻译、图片反推提示词，以及纯本地的结构化兜底。

本模块提供三类能力（需求 7 / 11）：

1. ``translate_prompt`` —— 把用户的中文口语描述交给文本模型，改写成适合
   文生图模型的结构化英文提示词；
2. ``image_to_prompt`` —— 走多模态 ``image_url`` 把一张图片反推成中文提示词；
3. ``build_structured_prompt`` —— **不依赖任何接口**的本地兜底，按
   ``Subject / Scene / Lighting / Style / Quality`` 拼出结构化英文模板。

设计原则
--------
* **永不抛异常**：所有网络 / 解析 / 参数异常都在内部消化，失败统一返回空串，
  由调用方决定是否回落到 ``build_structured_prompt``。绘画主流程绝不因为
  辅助功能挂掉而中断。
* **不复用绘画协议实例**：这里直接发轻量 ``chat/completions`` 请求，自带
  独立的 ``aiohttp.ClientSession``（用完即关），避免和长耗时的出图请求抢连接。
* **地址自动补版本段**：用户在中转站地址里只填域名（如
  ``https://api.example.com``）时，这里会自动补成 ``.../v1/chat/completions``；
  已经写了 ``/v1``、``/v1beta`` 或完整接口路径时不会重复追加。
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiohttp

from .models import PROTOCOL_GEMINI

# ---------------------------------------------------------------- 兼容导入（只读）
# 代理 A 正在改写 core/protocols/base.py，这里做容错导入：
# 万一 import 时恰好是半成品，也不应该让整个插件启动失败。
try:  # pragma: no cover - 正常路径
    from .protocols.base import DEFAULT_TIMEOUT as _BASE_TIMEOUT  # type: ignore
except Exception:  # noqa: BLE001
    _BASE_TIMEOUT = 600.0

try:  # pragma: no cover - 正常路径
    from .protocols.base import parse_json_text as _shared_parse_json  # type: ignore
except Exception:  # noqa: BLE001
    _shared_parse_json = None  # type: ignore[assignment]


__all__ = [
    "DEFAULT_TRANSLATE_SYSTEM_PROMPT",
    "DEFAULT_VISION_SYSTEM_PROMPT",
    "MODEL_TIMEOUT_CAP",
    "api_url",
    "build_structured_prompt",
    "extract_text",
    "image_to_prompt",
    "join_api_url",
    "translate_prompt",
]


#: 辅助请求的最长等待时间：翻译 / 反推属于轻量请求，不该跟着 600 秒的出图超时走
MODEL_TIMEOUT_CAP = 180.0

#: 反推图片时允许下载的最大体积（超过则不下载，直接让服务端自己去取链接）
MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: 翻译用的兜底系统提示词（配置为空时使用）
DEFAULT_TRANSLATE_SYSTEM_PROMPT = (
    "你是资深 AI 绘画提示词工程师。请把用户的中文口语描述改写为适合文生图模型的结构化英文提示词：\n"
    "1. 忠实保留用户的主体、动作、风格意图，不要凭空添加人物或品牌；\n"
    "2. 按「主体 + 外观细节 + 场景 + 光线 + 镜头/构图 + 风格 + 画质」的顺序组织，逗号分隔；\n"
    "3. 只输出英文提示词本身，不要解释、不要 Markdown 代码块、不要引号。"
)

#: 图片反推提示词用的系统提示词
DEFAULT_VISION_SYSTEM_PROMPT = (
    "你是图像分析专家，擅长把图片还原成可复用的绘画提示词。"
    "请用中文输出：先一句话概括主体，再按「主体细节 / 场景 / 光线 / 构图镜头 / 风格 / 画质」"
    "分条列出关键信息；只描述看得见的内容，不要猜测人物身份，不要输出 Markdown 代码块。"
)

#: 版本段：v1 / v1beta / v2.1 / v3-alpha 之类
_VERSION_SEGMENT_RE = re.compile(r"^v\d+(?:[a-z0-9._\-]*)$", re.IGNORECASE)
#: 已经是完整接口路径的结尾（用于先剥离，再补版本段）
_ENDPOINT_TAILS = (
    ("chat", "completions"),
    ("images", "generations"),
    ("images", "edits"),
    ("images", "variations"),
    ("models",),
)
#: 这些末段属于「站点根路径」，后面应补上版本段
_GENERIC_TAILS = frozenset(
    {"api", "openai", "openai-compatible", "compatible", "completions", "relay", "proxy"}
)
#: 自部署协议不需要版本段
_SELFHOST_PROTOCOLS = frozenset({"sdwebui", "comfyui"})
#: 各协议默认版本段
_PROTOCOL_VERSIONS: Dict[str, str] = {PROTOCOL_GEMINI: "v1beta"}
_DEFAULT_VERSION = "v1"
#: 走原生协议（而非 OpenAI 兼容）的协议 key
_NATIVE_PROTOCOLS = frozenset({PROTOCOL_GEMINI})
# ------------------------------------------------------------------ 基础小工具
def _clip(value: Any, limit: int = 300) -> str:
    """把日志用的文本裁剪到指定长度。"""
    text = "" if value is None else str(value)
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _channel_attr(channel: Any, name: str, default: Any = "") -> Any:
    """安全读取通道字段（通道可能是 None / 随便传进来的对象）。"""
    try:
        value = getattr(channel, name, default)
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else value


def _channel_proxy(channel: Any, override: str = "") -> str:
    """按「显式传参 -> channel.extra['proxy']」的顺序取代理地址。"""
    value = str(override or "").strip()
    if value:
        return value
    try:
        extra = getattr(channel, "extra", None) or {}
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(extra, dict):
        return str(extra.get("proxy") or "").strip()
    return ""


def _timeout_seconds(timeout: Any) -> float:
    """把用户配置的超时换算成辅助请求的超时（上限 MODEL_TIMEOUT_CAP）。"""
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        try:
            value = float(_BASE_TIMEOUT)
        except (TypeError, ValueError):
            value = 600.0
    if value <= 0:
        value = 600.0
    return min(value, MODEL_TIMEOUT_CAP)


def _parse_json(text: Any) -> Any:
    """优先用 base.py 的实现，缺失时退回本地 json 解析。"""
    if _shared_parse_json is not None:
        try:
            return _shared_parse_json(text)
        except Exception:  # noqa: BLE001
            pass
    raw = "" if text is None else str(text).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- URL 拼接与版本
def _split_base(base_url: str) -> Tuple[str, List[str], str]:
    """把 base_url 拆成 ``(origin, 路径段列表, query)``。"""
    raw = str(base_url or "").strip()
    if not raw:
        return "", [], ""
    main, _, query = raw.partition("?")
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://[^/]+)(/.*)?$", main.strip())
    if not match:
        return main.strip().rstrip("/"), [], query
    origin = match.group(1)
    path = (match.group(2) or "").strip("/")
    segments = [seg for seg in path.split("/") if seg]
    return origin, segments, query


def _default_version(protocol: str) -> str:
    """返回该协议默认的版本段。"""
    return _PROTOCOL_VERSIONS.get(str(protocol or "").strip().lower(), _DEFAULT_VERSION)


def api_url(base_url: str, protocol: str = "", path: str = "") -> str:
    """在 base_url 上拼出完整接口地址，并在需要时自动补版本段。

    * ``https://api.example.com`` + ``chat/completions`` → ``.../v1/chat/completions``
    * ``https://api.example.com/v1`` → 不再重复追加
    * ``https://api.example.com/v1/chat/completions`` → 原样识别，不重复拼接
    * Gemini 原生接口默认补 ``/v1beta``
    * ``sdwebui`` / ``comfyui`` 等自部署协议不补版本段
    """
    relative = str(path or "").strip().lstrip("/")
    origin, segments, query = _split_base(base_url)
    if not origin:
        return relative

    lowered = [seg.lower() for seg in segments]
    # 1) 用户可能直接粘贴了完整接口地址：先把结尾的端点路径剥掉
    for tail in _ENDPOINT_TAILS:
        size = len(tail)
        if len(lowered) >= size and tuple(lowered[-size:]) == tail:
            segments = segments[:-size]
            lowered = lowered[:-size]
            break

    protocol_key = str(protocol or "").strip().lower()
    has_version = any(_VERSION_SEGMENT_RE.match(seg) for seg in segments)
    if not has_version and protocol_key not in _SELFHOST_PROTOCOLS:
        last = lowered[-1] if lowered else ""
        if not segments or last in _GENERIC_TAILS:
            segments = list(segments) + [_default_version(protocol_key)]

    pieces = [origin] + segments
    if relative:
        pieces.append(relative)
    url = "/".join(pieces)
    if query:
        url = "{}?{}".format(url, query)
    return url


#: ``api_url`` 的别名（语义更直白，供 main.py 调用）
join_api_url = api_url


# -------------------------------------------------------------------- 请求头
def _headers_for(channel: Any, gemini: bool = False) -> Dict[str, str]:
    """构造请求头：统一 Bearer，Gemini 额外带上 ``x-goog-api-key``。"""
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    key = str(_channel_attr(channel, "api_key", "") or "").strip()
    if key:
        headers["Authorization"] = "Bearer {}".format(key)
        if gemini:
            headers["x-goog-api-key"] = key
    return headers


def _session_kwargs(proxy: str) -> Dict[str, Any]:
    """把代理地址转换成 ``aiohttp.ClientSession`` 的参数。"""
    value = str(proxy or "").strip()
    if not value:
        return {}
    if "://" not in value:
        value = "http://" + value
    if value.split("://", 1)[0].lower() not in ("http", "https", "socks4", "socks5"):
        return {}
    return {"proxy": value}


async def _post_json(
    url: str,
    payload: Any,
    headers: Dict[str, str],
    timeout: float,
    proxy: str,
) -> Tuple[bool, Any, str]:
    """发送一次 JSON POST，返回 ``(是否成功, 解析后的响应, 错误文本)``。"""
    timeout_cfg = aiohttp.ClientTimeout(total=float(timeout))
    try:
        async with aiohttp.ClientSession(timeout=timeout_cfg, **_session_kwargs(proxy)) as session:
            async with session.post(url, json=payload, headers=headers) as response:
                text = await response.text(errors="replace")
                data = _parse_json(text)
                if 200 <= response.status < 300:
                    return True, data, ""
                detail = ""
                if isinstance(data, dict):
                    error = data.get("error")
                    if isinstance(error, dict):
                        detail = str(error.get("message") or error.get("msg") or "")
                    elif isinstance(error, str):
                        detail = error
                    if not detail:
                        detail = str(data.get("message") or data.get("msg") or "")
                return False, data, "HTTP {} {}".format(response.status, _clip(detail or text, 160))
    except Exception as exc:  # noqa: BLE001 - 辅助功能不允许把异常抛给调用方
        return False, None, "{}: {}".format(exc.__class__.__name__, _clip(exc, 160))


async def _fetch_image_bytes(url: str, timeout: float, proxy: str) -> bytes:
    """把图片链接下载成 bytes（失败返回空 bytes），用于服务端不接受外链时重试。"""
    timeout_cfg = aiohttp.ClientTimeout(total=float(min(timeout, 60.0)))
    try:
        async with aiohttp.ClientSession(timeout=timeout_cfg, **_session_kwargs(proxy)) as session:
            async with session.get(url, headers={"Accept": "image/*"}) as response:
                if not (200 <= response.status < 300):
                    return b""
                data = await response.read()
    except Exception:  # noqa: BLE001
        return b""
    if not data or len(data) > MAX_IMAGE_BYTES:
        return b""
    return data
# ------------------------------------------------------------------ 文本提取
def _guess_mime(data: bytes, fallback: str = "image/png") -> str:
    """按文件头猜图片 MIME，猜不到时用 ``fallback``。"""
    if not data:
        return fallback
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return fallback


def _clean_text(value: Any) -> str:
    """清洗模型返回的文本：去代码块围栏、去常见引导词、统一换行。"""
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    for prefix in ("英文提示词：", "英文提示词:", "提示词：", "提示词:", "Prompt:", "prompt:", "PROMPT:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text.strip()


def _text_from_content(content: Any) -> str:
    """从 message.content 里取文本（兼容 str 与多段 parts 两种形态）。"""
    if isinstance(content, str):
        return _clean_text(content)
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text)
        return _clean_text("\n".join(chunks))
    if isinstance(content, dict):
        return _clean_text(content.get("text") or content.get("content") or "")
    return ""


def extract_text(payload: Any) -> str:
    """从各种大模型响应结构里提取文本，取不到返回空串。"""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return _clean_text(payload)
    if isinstance(payload, list):
        for item in payload:
            text = extract_text(item)
            if text:
                return text
        return ""
    if not isinstance(payload, dict):
        return ""
    if payload.get("error"):
        # 明确是错误响应，不再去里面掏文本，避免把报错当成提示词
        return ""

    # OpenAI 兼容：choices[0].message.content / choices[0].text
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            message = choice.get("message")
            if isinstance(message, dict):
                text = _text_from_content(message.get("content"))
                if text:
                    return text
                text = _clean_text(message.get("reasoning_content") or "")
                if text:
                    return text
            text = _text_from_content(choice.get("text"))
            if text:
                return text
        elif isinstance(choice, str):
            text = _clean_text(choice)
            if text:
                return text

    # Gemini：candidates[0].content.parts[*].text
    candidates = payload.get("candidates")
    if isinstance(candidates, list) and candidates:
        candidate = candidates[0]
        if isinstance(candidate, dict):
            content = candidate.get("content")
            text = _text_from_content(content.get("parts") if isinstance(content, dict) else content)
            if text:
                return text
            text = _clean_text(candidate.get("text") or "")
            if text:
                return text

    # OpenAI Responses / 其它包装
    for key in ("output_text", "response", "text", "content", "result", "message", "data"):
        node = payload.get(key)
        if isinstance(node, str):
            text = _clean_text(node)
            if text:
                return text
        elif isinstance(node, (dict, list)):
            text = extract_text(node)
            if text:
                return text
    return ""


def _to_inline_data(value: Any) -> Optional[Tuple[str, str]]:
    """把 ``data:`` 链接拆成 ``(mime, base64数据)``；不是 data 链接时返回 None。"""
    text = str(value or "").strip()
    if not text.startswith("data:"):
        return None
    header, _, payload = text.partition(",")
    mime = "image/png"
    meta = header[5:]
    if meta:
        mime = meta.split(";")[0].strip() or mime
    compact = "".join(payload.split())
    if not compact:
        return None
    return mime, compact


def _build_payload(
    channel: Any,
    model: str,
    messages: Sequence[Dict[str, Any]],
    gemini: bool = False,
    temperature: float = 0.3,
) -> Any:
    """按协议组装请求体；``messages`` 为 ``{"role","content"}`` 列表。"""
    model_name = str(model or "").strip() or str(_channel_attr(channel, "model", "") or "").strip()
    if gemini:
        system_parts = []
        contents = []
        for item in messages:
            role = str(item.get("role") or "user")
            content = item.get("content")
            if role == "system":
                text = _text_from_content(content)
                if text:
                    system_parts.append({"text": text})
                continue
            parts = []
            if isinstance(content, str):
                parts.append({"text": content})
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    kind = str(part.get("type") or "")
                    if kind == "text":
                        parts.append({"text": str(part.get("text") or "")})
                    elif kind == "image_url":
                        raw = part.get("image_url")
                        url_value = raw.get("url") if isinstance(raw, dict) else raw
                        inline = _to_inline_data(url_value)
                        if inline is not None:
                            mime, data = inline
                            parts.append({"inlineData": {"mimeType": mime, "data": data}})
            if parts:
                contents.append({"role": "user" if role == "user" else "model", "parts": parts})
        payload = {"contents": contents, "generationConfig": {"temperature": float(temperature)}}
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}
        return payload

    return {
        "model": model_name,
        "messages": list(messages),
        "temperature": float(temperature),
        "stream": False,
    }
def _image_reference(image_ref: Any) -> Tuple[str, str]:
    """归一化图片引用，返回 ``(kind, value)``，kind ∈ ``url`` / ``data``。

    支持：http(s) 链接、``data:`` 链接、裸 base64、bytes，以及带
    ``data`` / ``url`` / ``file`` 字段的对象（如 ``ImageInput``）。
    """
    if image_ref is None:
        return "", ""
    if isinstance(image_ref, (bytes, bytearray)):
        raw = bytes(image_ref)
        if not raw:
            return "", ""
        return "data", "data:{};base64,{}".format(_guess_mime(raw), base64.b64encode(raw).decode("ascii"))
    if isinstance(image_ref, dict):
        for key in ("url", "image_url", "link"):
            value = image_ref.get(key)
            if isinstance(value, str) and value.strip():
                return _image_reference(value)
        for key in ("data", "b64", "base64", "data_uri", "file"):
            value = image_ref.get(key)
            if value:
                return _image_reference(value)
        return "", ""
    if isinstance(image_ref, (list, tuple)):
        for item in image_ref:
            kind, value = _image_reference(item)
            if value:
                return kind, value
        return "", ""

    # 带属性的对象（ImageInput / 消息组件等）
    for attr in ("data_uri", "url", "b64_json"):
        value = getattr(image_ref, attr, None)
        if isinstance(value, str) and value.strip():
            return _image_reference(value)
    raw_bytes = getattr(image_ref, "data", None)
    if isinstance(raw_bytes, (bytes, bytearray)) and raw_bytes:
        return _image_reference(bytes(raw_bytes))
    for attr in ("base64", "b64"):
        value = getattr(image_ref, attr, None)
        if isinstance(value, str) and value.strip():
            return _image_reference(value)

    text = str(image_ref).strip()
    if not text:
        return "", ""
    lowered = text.lower()
    if lowered.startswith("data:"):
        return "data", text
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return "url", text
    # 其它情况按裸 base64 处理
    compact = "".join(text.split())
    try:
        base64.b64decode(compact + "=" * ((-len(compact)) % 4), validate=False)
    except Exception:  # noqa: BLE001
        return "", ""
    return "data", "data:{};base64,{}".format("image/png", compact)


def _image_message_part(kind: str, value: str, gemini: bool = False) -> Optional[Dict[str, Any]]:
    """把图片引用转成协议对应的消息片段。"""
    if not value:
        return None
    if kind != "data":
        return None if gemini else {"type": "image_url", "image_url": {"url": value}}
    inline = _to_inline_data(value)
    if inline is None:
        return None
    if gemini:
        mime, data = inline
        return {"inlineData": {"mimeType": mime, "data": data}}
    return {"type": "image_url", "image_url": {"url": value}}


#: 反推图片时给模型看的中文指令
_VISION_INSTRUCTION: str = "请描述这张图片，并给出可用于重绘的绘画提示词。"


def _vision_messages(part: Dict[str, Any]) -> List[Dict[str, Any]]:
    """组装反推提示词的消息体。"""
    return [
        {"role": "system", "content": DEFAULT_VISION_SYSTEM_PROMPT},
        {"role": "user", "content": [{"type": "text", "text": _VISION_INSTRUCTION}, part]},
    ]


# ------------------------------------------------------------------ 对外能力
async def translate_prompt(
    channel: Any,
    model: str = "",
    text: str = "",
    system_prompt: str = "",
    timeout: Any = 0.0,
    proxy: str = "",
) -> str:
    """把中文口语描述改写成结构化英文提示词；失败一律返回空串。

    参数：``(channel, model, text, system_prompt, timeout, proxy)``。
    """
    prompt_text = str(text or "").strip()
    if not prompt_text or channel is None:
        return ""
    base_url = str(_channel_attr(channel, "base_url", "") or "").strip()
    if not base_url:
        return ""

    protocol = str(_channel_attr(channel, "protocol", "") or "").strip().lower()
    gemini = protocol in _NATIVE_PROTOCOLS
    system_text = str(system_prompt or "").strip() or DEFAULT_TRANSLATE_SYSTEM_PROMPT
    model_name = str(model or "").strip() or str(_channel_attr(channel, "model", "") or "").strip()
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": prompt_text},
    ]
    target = "models/{}:generateContent".format(model_name) if gemini else "chat/completions"
    url = api_url(base_url, protocol, target)
    payload = _build_payload(channel, model_name, messages, gemini=gemini, temperature=0.3)
    ok, data, _error = await _post_json(
        url, payload, _headers_for(channel, gemini=gemini), _timeout_seconds(timeout), _channel_proxy(channel, proxy)
    )
    if not ok:
        return ""
    return extract_text(data)


async def image_to_prompt(
    channel: Any,
    model: str = "",
    image_ref: Any = "",
    timeout: Any = 0.0,
    proxy: str = "",
) -> str:
    """把一张图片反推成中文提示词；失败一律返回空串。"""
    if channel is None:
        return ""
    base_url = str(_channel_attr(channel, "base_url", "") or "").strip()
    if not base_url:
        return ""
    kind, value = _image_reference(image_ref)
    if not value:
        return ""

    protocol = str(_channel_attr(channel, "protocol", "") or "").strip().lower()
    gemini = protocol in _NATIVE_PROTOCOLS
    model_name = str(model or "").strip() or str(_channel_attr(channel, "model", "") or "").strip()
    seconds = _timeout_seconds(timeout)
    proxies = _channel_proxy(channel, proxy)
    headers = _headers_for(channel, gemini=gemini)
    target = "models/{}:generateContent".format(model_name) if gemini else "chat/completions"
    url = api_url(base_url, protocol, target)

    # Gemini 原生接口不认外链，先自己下载成 base64 再发
    if gemini and kind == "url":
        raw = await _fetch_image_bytes(value, seconds, proxies)
        if not raw:
            return ""
        kind = "data"
        value = "data:{};base64,{}".format(_guess_mime(raw), base64.b64encode(raw).decode("ascii"))

    part = _image_message_part(kind, value, gemini=gemini)
    if part is None:
        return ""
    payload = _build_payload(channel, model_name, _vision_messages(part), gemini=gemini, temperature=0.2)
    ok, data, _error = await _post_json(url, payload, headers, seconds, proxies)

    if not ok and kind == "url":
        # 服务端取不到外链时，自己下载后重试一次（不少中转站不代理外链）
        raw = await _fetch_image_bytes(value, seconds, proxies)
        if raw:
            data_uri = "data:{};base64,{}".format(_guess_mime(raw), base64.b64encode(raw).decode("ascii"))
            retry_part = _image_message_part("data", data_uri, gemini=gemini)
            if retry_part is not None:
                retry_payload = _build_payload(
                    channel, model_name, _vision_messages(retry_part), gemini=gemini, temperature=0.2
                )
                ok, data, _error = await _post_json(url, retry_payload, headers, seconds, proxies)
    if not ok:
        return ""
    return extract_text(data)
# ------------------------------------------------------------------ 本地兜底
#: 中英关键词对照（用于本地结构化兜底；同一类别内按「长词优先」匹配）
_KEYWORD_TABLE: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...] = (
    (
        "subject",
        (
            ("小猫", "a cute kitten"),
            ("猫咪", "a cat"),
            ("猫", "a cat"),
            ("小狗", "a cute puppy"),
            ("狗", "a dog"),
            ("少女", "a young girl"),
            ("女孩", "a girl"),
            ("男生", "a young man"),
            ("男孩", "a boy"),
            ("角色", "the character"),
            ("人物", "a person"),
            ("机器人", "a robot"),
            ("飞船", "a spaceship"),
            ("城堡", "a castle"),
            ("城市", "a city"),
            ("风景", "a landscape"),
            ("雪山", "snowy mountains"),
            ("森林", "a forest"),
            ("花", "flowers"),
            ("龙", "a dragon"),
            ("美食", "a plate of food"),
            ("咖啡", "a cup of coffee"),
        ),
    ),
    (
        "style",
        (
            ("蒸汽朋克", "steampunk style"),
            ("赛博朋克", "cyberpunk style"),
            ("二次元", "anime style"),
            ("日系", "Japanese anime style"),
            ("国风", "Chinese traditional style"),
            ("水墨", "ink wash painting style"),
            ("油画", "oil painting style"),
            ("水彩", "watercolor style"),
            ("像素", "pixel art"),
            ("手办", "collectible figurine style"),
            ("写实", "photorealistic"),
            ("插画", "illustration"),
            ("科幻", "science fiction style"),
            ("奇幻", "fantasy style"),
            ("极简", "minimalist style"),
            ("复古", "retro style"),
            ("卡通", "cartoon style"),
            ("3D", "3D render"),
            ("3d", "3D render"),
        ),
    ),
    (
        "scene",
        (
            ("夜景", "night scene"),
            ("背景", "detailed background"),
            ("室内", "indoor scene"),
            ("室外", "outdoor scene"),
            ("海边", "by the sea"),
            ("街道", "on a street"),
            ("宇宙", "in outer space"),
            ("星空", "under a starry sky"),
            ("水面", "on a mirror-like water surface"),
        ),
    ),
    (
        "lighting",
        (
            ("逆光", "backlighting"),
            ("侧光", "side lighting"),
            ("柔光", "soft lighting"),
            ("霓虹", "neon lighting"),
            ("黄昏", "warm sunset light"),
            ("夕阳", "warm sunset light"),
            ("电影感", "cinematic lighting"),
            ("氛围", "moody atmosphere"),
            ("发光", "glowing highlights"),
            ("体积光", "volumetric light"),
        ),
    ),
    (
        "quality",
        (
            ("超高画质", "ultra high quality"),
            ("4k", "4K resolution"),
            ("8k", "8K resolution"),
            ("高清", "high definition"),
            ("细节", "fine details"),
            ("锐利", "sharp focus"),
        ),
    ),
)

#: 各字段的默认兜底值（保证模板永远完整）
_FIELD_DEFAULTS: Dict[str, str] = {
    "scene": "a clean, well-composed scene",
    "lighting": "soft natural lighting",
    "style": "high quality digital illustration",
    "quality": "ultra detailed, 4K, sharp focus, high dynamic range",
}

#: 字段输出顺序
_FIELD_ORDER: Tuple[str, ...] = ("subject", "scene", "lighting", "style", "quality")

#: 字段英文标签
_FIELD_LABELS: Dict[str, str] = {
    "subject": "Subject",
    "scene": "Scene",
    "lighting": "Lighting",
    "style": "Style",
    "quality": "Quality",
}


def _split_fragments(text: str) -> List[str]:
    """把一段描述切成片段，便于逐段匹配关键词。"""
    pieces = re.split(r"[，。、；：,;.!?！？\n\t]+", str(text or ""))
    return [piece.strip() for piece in pieces if piece and piece.strip()]


def _match_field(
    fragments: Sequence[str],
    table: Tuple[Tuple[str, str], ...],
) -> Tuple[str, List[int]]:
    """在片段里匹配关键词，返回 ``(英文串, 已命中的片段索引列表)``。"""
    hits = []
    used = []
    for index, fragment in enumerate(fragments):
        lowered = fragment.lower()
        matched = []
        for key, value in table:
            probe = key.lower()
            if probe and probe in lowered and value not in matched:
                matched.append(value)
        if matched:
            hits.extend(matched)
            used.append(index)
    return ", ".join(hits), used


def _translate_phrases(text: str) -> str:
    """把已知的中文词替换成英文，未知内容原样保留（不做猜测）。"""
    result = str(text or "").strip()
    if not result:
        return ""
    for _field, table in _KEYWORD_TABLE:
        for key, value in table:
            if key in result:
                result = result.replace(key, " " + value + " ")
    result = re.sub(r"\s+", " ", result).strip()
    result = result.strip(" ,，。、;；")
    return result


def build_structured_prompt(text: str, style: str = "") -> str:
    """纯本地兜底：把中文口语拼成结构化英文提示词模板。

    输出形如::

        Subject: a cute kitten | Scene: night scene | Lighting: neon lighting
        | Style: cyberpunk style | Quality: ultra detailed, 4K, sharp focus

    不依赖任何接口，因此可以放心作为 ``translate_prompt`` 失败后的回落。
    """
    raw = str(text or "").strip()
    style_text = str(style or "").strip()
    if not raw and not style_text:
        return ""

    fragments = _split_fragments(raw)
    used = []
    matched = {}
    for field, table in _KEYWORD_TABLE:
        hits, hit_index = _match_field(fragments, table)
        if hits:
            matched[field] = hits
        for index in hit_index:
            if index not in used:
                used.append(index)

    leftover = [fragment for index, fragment in enumerate(fragments) if index not in used]
    leftover_text = _translate_phrases("，".join(leftover)) if leftover else ""
    subject_hits = matched.get("subject", "")
    if subject_hits and leftover_text:
        subject = "{}, {}".format(subject_hits, leftover_text)
    else:
        subject = subject_hits or leftover_text or _translate_phrases(raw) or raw
    if not subject:
        subject = _FIELD_DEFAULTS["style"]

    style_value = ", ".join(part for part in (matched.get("style", ""), style_text) if part)
    values = {
        "subject": subject,
        "scene": matched.get("scene", "") or _FIELD_DEFAULTS["scene"],
        "lighting": matched.get("lighting", "") or _FIELD_DEFAULTS["lighting"],
        "style": style_value or _FIELD_DEFAULTS["style"],
        "quality": matched.get("quality", "") or _FIELD_DEFAULTS["quality"],
    }
    parts = [
        "{}: {}".format(_FIELD_LABELS[key], values[key])
        for key in _FIELD_ORDER
        if values.get(key)
    ]
    if not parts:
        return ""
    return " | ".join(parts)
