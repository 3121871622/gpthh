"""图像协议层：按通道配置创建对应的协议实现。

对外暴露的主要入口：

* :func:``create_protocol`` —— 根据 ``Channel.protocol`` 创建协议实例；
* :func:``protocol_label`` —— 把协议 key 转成中文显示名；
* :data:``PROTOCOL_CLASSES`` —— 协议 key 到实现类的映射（供 UI / 测试引用）；
* :func:``with_version_prefix`` / :func:``version_was_added`` —— 版本段自动补全（需求 1）。

v1.0.2 起共支持 8 种协议：openai / gemini / grok / flux / sdwebui / comfyui / jimeng / tongyi。
"""

from __future__ import annotations

from ..models import (
    PROTOCOL_COMFYUI,
    PROTOCOL_FLUX,
    PROTOCOL_GEMINI,
    PROTOCOL_GROK,
    PROTOCOL_JIMENG,
    PROTOCOL_LABELS,
    PROTOCOL_OPENAI,
    PROTOCOL_SDWEBUI,
    PROTOCOL_SHORT_LABELS,
    PROTOCOL_TONGYI,
    Channel,
    normalize_protocol,
)
from .base import (
    DEFAULT_TIMEOUT,
    BaseImageProtocol,
    fallback_url_for,
    protocol_version_segment,
    version_was_added,
    with_version_prefix,
)
from .comfyui import ComfyUIProtocol
from .flux import FluxProtocol
from .gemini import GeminiProtocol
from .grok import GrokProtocol
from .jimeng import JimengProtocol
from .openai_compat import OpenAICompatProtocol
from .sdwebui import SDWebUIProtocol
from .tongyi import TongyiProtocol

#: 协议 key -> 实现类
_PROTOCOL_CLASSES = {
    PROTOCOL_OPENAI: OpenAICompatProtocol,
    PROTOCOL_GEMINI: GeminiProtocol,
    PROTOCOL_GROK: GrokProtocol,
    PROTOCOL_FLUX: FluxProtocol,
    PROTOCOL_SDWEBUI: SDWebUIProtocol,
    PROTOCOL_COMFYUI: ComfyUIProtocol,
    PROTOCOL_JIMENG: JimengProtocol,
    PROTOCOL_TONGYI: TongyiProtocol,
}

#: 对外只读视图（避免调用方直接改内部字典）
PROTOCOL_CLASSES = dict(_PROTOCOL_CLASSES)


def create_protocol(
    channel: Channel,
    timeout: float = DEFAULT_TIMEOUT,
    proxy: str = "",
) -> BaseImageProtocol:
    """根据通道的协议类型创建协议实例。

    未知协议 key 时回退到 OpenAI 兼容实现，保证配置写错也不会直接崩。
    ``proxy`` 形如 ``http://127.0.0.1:7890``，留空表示不使用代理。
    """
    raw = (getattr(channel, "protocol", "") or "").strip().lower()
    key = normalize_protocol(raw) or raw or PROTOCOL_OPENAI
    protocol_class = _PROTOCOL_CLASSES.get(key, OpenAICompatProtocol)
    return protocol_class(channel, timeout=timeout, proxy=proxy)


def protocol_label(key: str) -> str:
    """返回协议的中文短标签，例如 ``openai`` -> ``OpenAI 兼容``。"""
    raw = (key or "").strip().lower()
    normalized = normalize_protocol(raw) or raw
    if normalized in PROTOCOL_SHORT_LABELS:
        return PROTOCOL_SHORT_LABELS[normalized]
    if normalized in PROTOCOL_LABELS:
        return PROTOCOL_LABELS[normalized]
    return key or ""


def supported_protocols() -> list:
    """返回本模块支持的协议 key 列表（与展示顺序一致）。"""
    return list(_PROTOCOL_CLASSES.keys())


__all__ = [
    "DEFAULT_TIMEOUT",
    "PROTOCOL_CLASSES",
    "BaseImageProtocol",
    "ComfyUIProtocol",
    "FluxProtocol",
    "GeminiProtocol",
    "GrokProtocol",
    "JimengProtocol",
    "OpenAICompatProtocol",
    "SDWebUIProtocol",
    "TongyiProtocol",
    "create_protocol",
    "fallback_url_for",
    "protocol_label",
    "protocol_version_segment",
    "supported_protocols",
    "version_was_added",
    "with_version_prefix",
]
