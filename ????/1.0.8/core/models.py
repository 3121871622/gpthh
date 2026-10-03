"""数据模型定义（冻结接口，除主代理外请勿修改）。"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

PROTOCOL_OPENAI = "openai"
PROTOCOL_GEMINI = "gemini"
PROTOCOL_GROK = "grok"
PROTOCOL_FLUX = "flux"
PROTOCOL_SDWEBUI = "sdwebui"
PROTOCOL_COMFYUI = "comfyui"
PROTOCOL_JIMENG = "jimeng"
PROTOCOL_TONGYI = "tongyi"

PROTOCOL_KEYS: tuple[str, ...] = (
    PROTOCOL_OPENAI,
    PROTOCOL_GEMINI,
    PROTOCOL_GROK,
    PROTOCOL_FLUX,
    PROTOCOL_SDWEBUI,
    PROTOCOL_COMFYUI,
    PROTOCOL_JIMENG,
    PROTOCOL_TONGYI,
)

PROTOCOL_LABELS: dict[str, str] = {
    PROTOCOL_OPENAI: "OpenAI 兼容（/images/generations · /images/edits）",
    PROTOCOL_GEMINI: "Gemini 兼容（generateContent · inlineData）",
    PROTOCOL_GROK: "Grok 兼容（images + chat/completions 回退）",
    PROTOCOL_FLUX: "Flux 中转（OpenAI 风格 /images/generations）",
    PROTOCOL_SDWEBUI: "Stable Diffusion WebUI（/sdapi/v1/txt2img · img2img）",
    PROTOCOL_COMFYUI: "ComfyUI（/prompt 工作流）",
    PROTOCOL_JIMENG: "即梦 / 火山引擎（异步任务）",
    PROTOCOL_TONGYI: "通义万相 / DashScope（异步任务）",
}

PROTOCOL_SHORT_LABELS: dict[str, str] = {
    PROTOCOL_OPENAI: "OpenAI 兼容",
    PROTOCOL_GEMINI: "Gemini 兼容",
    PROTOCOL_GROK: "Grok 兼容",
    PROTOCOL_FLUX: "Flux 中转",
    PROTOCOL_SDWEBUI: "SD WebUI",
    PROTOCOL_COMFYUI: "ComfyUI",
    PROTOCOL_JIMENG: "即梦",
    PROTOCOL_TONGYI: "通义万相",
}

#: 协议顺序（协议列表 / 切换协议统一按此顺序展示）
PROTOCOL_ORDER: tuple[str, ...] = (
    PROTOCOL_OPENAI,
    PROTOCOL_GEMINI,
    PROTOCOL_GROK,
    PROTOCOL_FLUX,
    PROTOCOL_SDWEBUI,
    PROTOCOL_COMFYUI,
    PROTOCOL_JIMENG,
    PROTOCOL_TONGYI,
)

#: 中转站型协议（共用一份域名 + 密钥，只需自动补 /v1）
RELAY_PROTOCOLS: frozenset = frozenset(
    {PROTOCOL_OPENAI, PROTOCOL_GEMINI, PROTOCOL_GROK, PROTOCOL_FLUX}
)

#: 本地部署型协议（需用户自己填服务地址，不补 /v1）
SELFHOST_PROTOCOLS: frozenset = frozenset({PROTOCOL_SDWEBUI, PROTOCOL_COMFYUI})

#: 各协议的出厂默认模型（model = 生成模型，edit_model 为空表示与生成模型相同）
DEFAULT_PROTOCOL_MODELS: dict[str, dict[str, str]] = {
    PROTOCOL_OPENAI: {"model": "gpt-image-2", "edit_model": ""},
    PROTOCOL_GEMINI: {"model": "gemini-2.5-flash-image", "edit_model": ""},
    PROTOCOL_GROK: {"model": "grok-2-image", "edit_model": ""},
    PROTOCOL_FLUX: {"model": "flux-1.1-pro", "edit_model": ""},
    PROTOCOL_SDWEBUI: {"model": "", "edit_model": ""},
    PROTOCOL_COMFYUI: {"model": "", "edit_model": ""},
    PROTOCOL_JIMENG: {"model": "jimeng-3.0", "edit_model": ""},
    PROTOCOL_TONGYI: {"model": "wanx2.1-t2i-turbo", "edit_model": ""},
}

#: 协议别名（清理空格 / 连字符 / 括号并转小写后）-> 协议 key
_PROTOCOL_ALIASES: dict[str, str] = {
    "openai": PROTOCOL_OPENAI,
    "openai兼容": PROTOCOL_OPENAI,
    "openaicompatible": PROTOCOL_OPENAI,
    "gemini": PROTOCOL_GEMINI,
    "gemini兼容": PROTOCOL_GEMINI,
    "google": PROTOCOL_GEMINI,
    "googleai": PROTOCOL_GEMINI,
    "grok": PROTOCOL_GROK,
    "grok兼容": PROTOCOL_GROK,
    "xai": PROTOCOL_GROK,
    "flux": PROTOCOL_FLUX,
    "flux中转": PROTOCOL_FLUX,
    "blackforestlabs": PROTOCOL_FLUX,
    "sd": PROTOCOL_SDWEBUI,
    "sdwebui": PROTOCOL_SDWEBUI,
    "stablediffusion": PROTOCOL_SDWEBUI,
    "sdxl": PROTOCOL_SDWEBUI,
    "comfy": PROTOCOL_COMFYUI,
    "comfyui": PROTOCOL_COMFYUI,
    "jimeng": PROTOCOL_JIMENG,
    "即梦": PROTOCOL_JIMENG,
    "volcengine": PROTOCOL_JIMENG,
    "火山引擎": PROTOCOL_JIMENG,
    "tongyi": PROTOCOL_TONGYI,
    "wanx": PROTOCOL_TONGYI,
    "dashscope": PROTOCOL_TONGYI,
    "通义": PROTOCOL_TONGYI,
    "通义万相": PROTOCOL_TONGYI,
    "万相": PROTOCOL_TONGYI,
}

_PROTOCOL_CLEAN_TOKENS: tuple[str, ...] = (" ", "\t", "　", "-", "_", "/", "\\", "／", "(", ")", "（", "）")


def _clean_protocol_text(text: Any) -> str:
    """把协议输入清理成便于匹配的形式（去空格 / 连字符，转小写）。"""
    cleaned = str(text or "").strip().lower()
    for token in _PROTOCOL_CLEAN_TOKENS:
        cleaned = cleaned.replace(token, "")
    return cleaned


def _protocol_tokens(protocol: str) -> tuple[str, ...]:
    """返回某个协议的全部可匹配写法。"""
    tokens: list[str] = [protocol]
    label = PROTOCOL_SHORT_LABELS.get(protocol, "")
    if label:
        tokens.append(_clean_protocol_text(label))
    tokens.extend(alias for alias, key in _PROTOCOL_ALIASES.items() if key == protocol)
    ordered: list[str] = []
    for token in tokens:
        if token and token not in ordered:
            ordered.append(token)
    return tuple(ordered)


def normalize_protocol(text: str) -> str:
    """把 '1' / 'openai' / 'OpenAI兼容' / 'gemini' 等写法规范化为协议 key；失败返回空串。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    if raw.isdigit():
        index = int(raw) - 1
        if 0 <= index < len(PROTOCOL_ORDER):
            return PROTOCOL_ORDER[index]
        return ""
    cleaned = _clean_protocol_text(raw)
    if not cleaned:
        return ""
    matched: list[str] = []
    for protocol in PROTOCOL_ORDER:
        tokens = _protocol_tokens(protocol)
        if cleaned in tokens:
            return protocol
        if any(token in cleaned or cleaned in token for token in tokens):
            matched.append(protocol)
    if len(matched) == 1:
        return matched[0]
    return ""


def default_model_for(protocol: str) -> str:
    """返回协议对应的出厂默认生成模型；协议无法识别时按 OpenAI 处理。"""
    key = normalize_protocol(protocol) or PROTOCOL_OPENAI
    bucket = DEFAULT_PROTOCOL_MODELS.get(key) or DEFAULT_PROTOCOL_MODELS[PROTOCOL_OPENAI]
    return bucket["model"]


def default_edit_model_for(protocol: str) -> str:
    """返回协议对应的出厂默认编辑模型（多为空串，表示与生成模型相同）。"""
    key = normalize_protocol(protocol) or PROTOCOL_OPENAI
    bucket = DEFAULT_PROTOCOL_MODELS.get(key) or DEFAULT_PROTOCOL_MODELS[PROTOCOL_OPENAI]
    return bucket["edit_model"]


def parse_choice(text: str, options: Sequence[str]) -> int:
    """把用户输入解析成 options 的下标；支持 1-based 序号、完全匹配（忽略大小写）、唯一子串匹配。

    解析失败返回 -1。
    """
    values = [str(item) for item in options]
    raw = str(text or "").strip()
    if not raw or not values:
        return -1
    if raw.isdigit():
        index = int(raw) - 1
        return index if 0 <= index < len(values) else -1
    lowered = raw.lower()
    for index, value in enumerate(values):
        if value.strip().lower() == lowered:
            return index
    hits = [index for index, value in enumerate(values) if lowered in value.lower()]
    if len(hits) == 1:
        return hits[0]
    hits = [index for index, value in enumerate(values) if value.lower() in lowered]
    if len(hits) == 1:
        return hits[0]
    return -1


def resolve_choice(text: str, options: Sequence[str]) -> tuple[int, str]:
    """返回 (index, value)；解析失败返回 (-1, "")。"""
    values = [str(item) for item in options]
    index = parse_choice(text, values)
    if index < 0:
        return (-1, "")
    return (index, values[index])

DEFAULT_SIZE = "1024x1024"

_SIZE_ALIASES: dict[str, str] = {
    "1:1": "1024x1024",
    "square": "1024x1024",
    "3:2": "1536x1024",
    "landscape": "1536x1024",
    "2:3": "1024x1536",
    "portrait": "1024x1536",
    "1024": "1024x1024",
    "1536": "1536x1024",
    "1k": "1024x1024",
    "2k": "2048x2048",
    "4k": "4096x4096",
    "9:16": "720x1280",
    "16:9": "1280x720",
    "3:4": "1152x1536",
    "4:3": "1536x1152",
    "21:9": "2048x878",
    "9:21": "878x2048",
}


def normalize_size(size: str | None, default: str = DEFAULT_SIZE) -> str:
    """规范化尺寸写法，例如 1:1 -> 1024x1024。"""
    if not isinstance(size, str):
        return default
    cleaned = size.strip().lower().replace(" ", "")
    if not cleaned:
        return default
    cleaned = cleaned.replace("*", "x").replace("×", "x").replace("X", "x")
    if cleaned in _SIZE_ALIASES:
        return _SIZE_ALIASES[cleaned]
    if cleaned == "auto" or cleaned == "自动":
        return "auto"
    if "x" in cleaned:
        left, _, right = cleaned.partition("x")
        if left.isdigit() and right.isdigit():
            return f"{int(left)}x{int(right)}"
    return size.strip()


def looks_like_size(size: Any) -> bool:
    """判断字符串是否形如尺寸。"""
    if not isinstance(size, str):
        return False
    cleaned = size.strip().lower().replace("*", "x").replace("×", "x")
    if not cleaned:
        return False
    if cleaned == "auto":
        return True
    left, sep, right = cleaned.partition("x")
    return bool(sep) and left.isdigit() and right.isdigit()


# ============================================================ 尺寸能力矩阵

#: 各协议支持的尺寸（空集合表示「不限制」）。
#: 用于切换协议时自动剔除不受支持的尺寸，避免直接 400。
PROTOCOL_SIZE_SUPPORT: dict[str, frozenset] = {
    # 官方只支持 1024x1024 / 1536x1024 / 1024x1536，且均为固定档位
    PROTOCOL_OPENAI: frozenset({"1024x1024", "1536x1024", "1024x1536", "auto"}),
    # Gemini 支持自由宽高，但建议 1:1 / 16:9 / 9:16 附近
    PROTOCOL_GEMINI: frozenset({"1024x1024", "1536x1024", "1024x1536", "auto"}),
    PROTOCOL_GROK: frozenset({"1024x1024", "1536x1024", "1024x1536", "auto"}),
    PROTOCOL_FLUX: frozenset({"1024x1024", "1536x1024", "1024x1536", "auto"}),
    # SD / ComfyUI 支持任意 8 的倍数，不限制
    PROTOCOL_SDWEBUI: frozenset(),
    PROTOCOL_COMFYUI: frozenset(),
    # 即梦与通义万相为常见档位
    PROTOCOL_JIMENG: frozenset({"1024x1024", "1536x1024", "1024x1536", "auto"}),
    PROTOCOL_TONGYI: frozenset({"1024x1024", "1536x1024", "1024x1536", "auto"}),
}

#: 尺寸 -> 宽高比（用于尺寸推荐与比例提取）
SIZE_RATIO_MAP: dict[str, str] = {
    "1024x1024": "1:1",
    "1536x1024": "3:2",
    "1024x1536": "2:3",
    "1280x720": "16:9",
    "720x1280": "9:16",
    "1536x864": "16:9",
    "864x1536": "9:16",
    "2048x1152": "16:9",
    "1152x2048": "9:16",
    "2048x2048": "1:1",
    "2048x878": "21:9",
    "878x2048": "9:21",
}

#: 比例 -> 推荐尺寸（提示词里写了比例时，自动用对应尺寸）
RATIO_SIZE_MAP: dict[str, str] = {
    "1:1": "1024x1024",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
    "16:9": "1280x720",
    "9:16": "720x1280",
    "4:3": "1536x1152",
    "3:4": "1152x1536",
    "21:9": "2048x878",
}

#: 常见 4k / 2k 分辨率写法 -> 尺寸，供提示词提取使用
RESOLUTION_SIZE_MAP: dict[str, str] = {
    "1k": "1024x1024",
    "2k": "2048x2048",
    "4k": "2048x2048",
    "1080p": "1920x1080",
    "720p": "1280x720",
}


def protocol_supports_size(protocol: str, size: str) -> bool:
    """判断尺寸是否在协议的常见能力范围内。

    该结果只用于界面提示和兼容性提示，不能用于覆盖用户已选尺寸。
    OpenAI 兼容服务的实现差异很大，最终应由服务端决定是否接受请求。
    """
    key = normalize_protocol(protocol) or PROTOCOL_OPENAI
    supported = PROTOCOL_SIZE_SUPPORT.get(key)
    if not supported:
        return True
    normalized = normalize_size(size, "")
    if not normalized:
        return False
    if normalized == "auto":
        return "auto" in supported
    if normalized in supported:
        return True
    # 宽高比相同也算支持（例如 1280x720 对 Gemini）
    ratio = SIZE_RATIO_MAP.get(normalized) or ratio_of_size(normalized)
    if not ratio:
        return False
    for item in supported:
        if SIZE_RATIO_MAP.get(item) == ratio or ratio_of_size(item) == ratio:
            return True
    return False


def ratio_of_size(size: str) -> str:
    """把 1536x1024 转成 3:2；无法计算时返回空串。"""
    normalized = normalize_size(size, "")
    if not normalized or normalized == "auto":
        return ""
    left, sep, right = normalized.partition("x")
    if not sep or not left.isdigit() or not right.isdigit():
        return ""
    width = int(left)
    height = int(right)
    if width <= 0 or height <= 0:
        return ""
    from math import gcd

    divisor = gcd(width, height)
    return "%d:%d" % (width // divisor, height // divisor)


def filter_sizes_for_protocol(protocol: str, sizes: Sequence[str]) -> list:
    """过滤出某协议可用的尺寸；全部不可用时回退到默认尺寸。"""
    kept: list = []
    for size in sizes or []:
        text = str(size or "").strip()
        if not text:
            continue
        if protocol_supports_size(protocol, text) and text not in kept:
            kept.append(text)
    if kept:
        return kept
    key = normalize_protocol(protocol) or PROTOCOL_OPENAI
    supported = PROTOCOL_SIZE_SUPPORT.get(key)
    if not supported:
        return list(DEFAULT_SIZES_FALLBACK)
    ordered = [DEFAULT_SIZE]
    for item in ("1536x1024", "1024x1536"):
        if item in supported:
            ordered.append(item)
    return ordered


#: 协议都不支持时的最后兜底尺寸
DEFAULT_SIZES_FALLBACK: tuple = ("1024x1024", "1536x1024", "1024x1536")


# ----------------------------------------------------- 提示词里的尺寸 / 比例 / 分辨率

#: 匹配「9:16」「16:9」这类比例
_RATIO_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{1,2})(?!\d)")
#: 匹配「1024x1024」「1024*1024」「1024×1024」
_SIZE_RE = re.compile(r"(?<!\d)(\d{3,5})\s*[x*×X]\s*(\d{3,5})(?!\d)")
#: 匹配「4k」「2K」「1080p」
_RESOLUTION_RE = re.compile(r"(?<![\w.])(4k|2k|1k|1080p|720p)(?![\w])", re.IGNORECASE)
#: 匹配「比例」「宽屏」等中文关键词旁的写法
_CN_RATIO_MAP: dict = {
    "横屏": "16:9",
    "横版": "16:9",
    "宽屏": "16:9",
    "竖屏": "9:16",
    "竖版": "9:16",
    "方图": "1:1",
    "正方形": "1:1",
    "头像": "1:1",
    "电影感": "",
}


def extract_size_hints(prompt: str) -> dict:
    """从提示词里提取尺寸提示。

    返回：``{"size": "尺寸或空串", "ratio": "比例或空串", "source": "命中位置"}``

    优先级：显式尺寸（如 1024x1024）> 分辨率（如 4k）> 比例（如 9:16）> 中文关键词（如 竖版）
    """
    text = str(prompt or "")
    result = {"size": "", "ratio": "", "source": ""}
    if not text:
        return result

    match = _SIZE_RE.search(text)
    if match:
        candidate = normalize_size("%sx%s" % (match.group(1), match.group(2)), "")
        if candidate and looks_like_size(candidate):
            result["size"] = candidate
            result["ratio"] = ratio_of_size(candidate)
            result["source"] = "size"
            return result

    match = _RESOLUTION_RE.search(text)
    if match:
        key = match.group(1).lower()
        size = RESOLUTION_SIZE_MAP.get(key, "")
        if size:
            result["size"] = size
            result["ratio"] = ratio_of_size(size)
            result["source"] = "resolution"
            return result

    match = _RATIO_RE.search(text)
    if match:
        width = int(match.group(1))
        height = int(match.group(2))
        if 0 < width <= 32 and 0 < height <= 32:
            ratio = "%d:%d" % (width, height)
            size = RATIO_SIZE_MAP.get(ratio, "")
            if not size:
                # 未预设的比例：以 1024 为基准换算
                if width >= height:
                    size = normalize_size("1024x%d" % max(1, round(1024 * height / width)), "")
                else:
                    size = normalize_size("%dx1024" % max(1, round(1024 * width / height)), "")
            result["ratio"] = ratio
            result["size"] = size
            result["source"] = "ratio"
            return result

    for keyword, ratio in _CN_RATIO_MAP.items():
        if keyword and keyword in text and ratio:
            size = RATIO_SIZE_MAP.get(ratio, "")
            if size:
                result["ratio"] = ratio
                result["size"] = size
                result["source"] = "keyword"
                return result

    return result


def strip_size_hints(prompt: str) -> str:
    """把提示词里的尺寸 / 比例写法剔除，避免模型把文字画进图里。"""
    text = str(prompt or "")
    text = _SIZE_RE.sub(" ", text)
    text = _RESOLUTION_RE.sub(" ", text)
    text = _RATIO_RE.sub(" ", text)
    for keyword, ratio in _CN_RATIO_MAP.items():
        if keyword and ratio:
            text = text.replace(keyword, " ")
    return re.sub(r"[ \t\u3000]{2,}", " ", text).strip()


def _guess_mime(name: str, fallback: str = "image/png") -> str:
    mime, _ = mimetypes.guess_type(name)
    return mime or fallback


@dataclass
class Channel:
    """一个绘画接口通道（运行时请求载体）。

    v1.0.1 起通道不再直接对应配置里的条目：``name`` 表示**供应商名**
    （中转站名称），协议与模型由配置层在 ``build_channel()`` 时组合进来。
    """

    protocol: str = PROTOCOL_OPENAI
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    edit_model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.protocol = (self.protocol or PROTOCOL_OPENAI).strip().lower()
        if self.protocol not in PROTOCOL_LABELS:
            self.protocol = PROTOCOL_OPENAI
        self.name = (self.name or "").strip()
        self.base_url = (self.base_url or "").strip().rstrip("/")
        self.model = (self.model or "").strip()
        self.edit_model = (self.edit_model or "").strip()

    @property
    def protocol_label(self) -> str:
        return PROTOCOL_SHORT_LABELS.get(self.protocol, self.protocol)

    @property
    def masked_key(self) -> str:
        if not self.api_key:
            return ""
        if len(self.api_key) <= 8:
            return "*" * len(self.api_key)
        return f"{self.api_key[:4]}****{self.api_key[-4:]}"

    @property
    def generate_model(self) -> str:
        return self.model or "gpt-image-2"

    @property
    def edit_model_name(self) -> str:
        return self.edit_model or self.model or "gpt-image-2"

    @property
    def is_ready(self) -> bool:
        return bool(self.base_url)

    def with_overrides(self, *, model: str | None = None, edit_model: str | None = None) -> "Channel":
        """返回应用了模型覆盖的新通道对象。"""
        clone = Channel(
            protocol=self.protocol,
            name=self.name,
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            edit_model=self.edit_model,
            extra=dict(self.extra),
        )
        if model:
            clone.model = model.strip()
        if edit_model:
            clone.edit_model = edit_model.strip()
        return clone

    @classmethod
    def from_config_entry(cls, entry: Any) -> "Channel | None":
        """从旧版通道条目 / 供应商条目构造通道；非法输入返回 None。"""
        if not isinstance(entry, dict):
            return None
        flattened: dict[str, Any] = {}

        def merge(value: Any) -> None:
            if not isinstance(value, dict):
                return
            for key in ("config", "data", "settings", "value"):
                nested = value.get(key)
                if isinstance(nested, dict):
                    merge(nested)
            for key, item in value.items():
                if key in {"api_key", "token", "secret"} and not str(item or "").strip():
                    if str(flattened.get(key) or "").strip():
                        continue
                flattened[key] = item

        merge(entry)
        protocol_value = (
            flattened.get("protocol")
            or flattened.get("type")
            or flattened.get("provider")
            or flattened.get("__template_key")
            or PROTOCOL_OPENAI
        )
        protocol = str(protocol_value)
        if protocol.strip().lower() in {"supplier", "channel", "provider"}:
            protocol = PROTOCOL_OPENAI
        channel = cls(
            protocol=protocol,
            name=str(flattened.get("name") or ""),
            base_url=str(flattened.get("base_url") or ""),
            api_key=str(flattened.get("api_key") or ""),
            model=str(flattened.get("model") or ""),
            edit_model=str(flattened.get("edit_model") or ""),
            extra={
                key: value
                for key, value in flattened.items()
                if key
                not in {
                    "__template_key",
                    "protocol",
                    "type",
                    "provider",
                    "config",
                    "data",
                    "settings",
                    "value",
                    "name",
                    "base_url",
                    "api_key",
                    "model",
                    "edit_model",
                }
            },
        )
        if not channel.name:
            channel.name = channel.protocol_label
        return channel

    def to_config_entry(self) -> dict[str, Any]:
        """导出为旧版通道配置条目（``providers`` 键，向后兼容）。"""
        entry: dict[str, Any] = {
            "__template_key": self.protocol,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "edit_model": self.edit_model,
        }
        for key, value in self.extra.items():
            entry.setdefault(key, value)
        return entry

    def to_supplier_entry(self) -> dict[str, Any]:
        """导出为供应商配置条目（只含名称 / 地址 / 密钥，协议与模型另行存储）。"""
        return {
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
        }


@dataclass
class ImageInput:
    """待发送给模型的输入图片。"""

    data: bytes = b""
    mime: str = "image/png"
    filename: str = "image.png"
    source: str = ""

    @property
    def size(self) -> int:
        return len(self.data or b"")

    def is_empty(self) -> bool:
        return not self.data

    @classmethod
    def from_path(cls, path: str, **kwargs: Any) -> "ImageInput":
        with open(path, "rb") as fp:
            data = fp.read()
        name = os.path.basename(path) or "image.png"
        return cls(
            data=data,
            mime=str(kwargs.pop("mime", "") or _guess_mime(name)),
            filename=str(kwargs.pop("filename", "") or name),
            **kwargs,
        )

    @classmethod
    def from_bytes(cls, data: bytes, **kwargs: Any) -> "ImageInput":
        filename = str(kwargs.pop("filename", "") or "image.png")
        return cls(
            data=data,
            mime=str(kwargs.pop("mime", "") or _guess_mime(filename)),
            filename=filename,
            **kwargs,
        )

    @classmethod
    def from_base64(cls, b64: str, **kwargs: Any) -> "ImageInput":
        payload = (b64 or "").strip()
        mime = str(kwargs.pop("mime", "") or "image/png")
        if payload.startswith("data:"):
            header, _, payload = payload.partition(",")
            meta = header[5:]
            if ";" in meta:
                detected, _, _ = meta.partition(";")
                if detected:
                    mime = detected
        payload = "".join(payload.split())
        padding = len(payload) % 4
        if padding:
            payload += "=" * (4 - padding)
        try:
            data = base64.b64decode(payload)
        except Exception as exc:  # noqa: BLE001 - 统一转换为空数据
            raise ValueError(f"base64 图片解码失败: {exc}") from exc
        return cls.from_bytes(data, mime=mime, **kwargs)

    def to_data_url(self) -> str:
        return f"data:{self.mime};base64,{self.to_b64()}"

    def to_b64(self) -> str:
        return base64.b64encode(self.data or b"").decode("ascii")


@dataclass
class GenerateRequest:
    """一次生成或编辑请求。"""

    prompt: str = ""
    images: list[ImageInput] = field(default_factory=list)
    size: str = DEFAULT_SIZE
    model: str = ""
    n: int = 1
    quality: str = "auto"
    transparent_background: bool = False

    @property
    def is_edit(self) -> bool:
        return bool(self.images)

    @property
    def image_count(self) -> int:
        return len(self.images)


@dataclass
class GeneratedImage:
    """模型返回的单张图片。"""

    data: bytes | None = None
    url: str | None = None
    mime: str = "image/png"
    revised_prompt: str = ""

    def is_empty(self) -> bool:
        return not self.data and not self.url


@dataclass
class GenerateResult:
    """一次生成请求的完整结果。"""

    images: list[GeneratedImage] = field(default_factory=list)
    protocol: str = ""
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not any(not image.is_empty() for image in self.images)

    @property
    def count(self) -> int:
        return len([image for image in self.images if not image.is_empty()])


class ProtocolError(Exception):
    """协议层统一异常。"""

    def __init__(self, message: str, *, status: int | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message}（{self.detail}）"
        return self.message


__all__ = [
    "DEFAULT_PROTOCOL_MODELS",
    "DEFAULT_SIZE",
    "DEFAULT_SIZES_FALLBACK",
    "PROTOCOL_COMFYUI",
    "PROTOCOL_FLUX",
    "PROTOCOL_GEMINI",
    "PROTOCOL_GROK",
    "PROTOCOL_JIMENG",
    "PROTOCOL_KEYS",
    "PROTOCOL_LABELS",
    "PROTOCOL_OPENAI",
    "PROTOCOL_ORDER",
    "PROTOCOL_SHORT_LABELS",
    "PROTOCOL_SIZE_SUPPORT",
    "PROTOCOL_SDWEBUI",
    "PROTOCOL_TONGYI",
    "RATIO_SIZE_MAP",
    "RELAY_PROTOCOLS",
    "RESOLUTION_SIZE_MAP",
    "SELFHOST_PROTOCOLS",
    "SIZE_RATIO_MAP",
    "extract_size_hints",
    "filter_sizes_for_protocol",
    "protocol_supports_size",
    "ratio_of_size",
    "strip_size_hints",
    "Channel",
    "GeneratedImage",
    "GenerateRequest",
    "GenerateResult",
    "ImageInput",
    "ProtocolError",
    "default_edit_model_for",
    "default_model_for",
    "looks_like_size",
    "normalize_protocol",
    "normalize_size",
    "parse_choice",
    "resolve_choice",
]
