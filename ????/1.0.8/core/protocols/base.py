"""图像协议基类：统一 HTTP 会话、URL 拼接、响应解析与错误处理。

本模块只依赖 ``aiohttp``（AstrBot 内置依赖），负责：

* 懒创建并复用 ``aiohttp.ClientSession``，``close()`` 时统一释放连接；
* 统一使用 ``aiohttp.ClientTimeout(total=self.timeout)`` 控制超时；
* 把网络 / HTTP 错误统一转换为 :class:`~..models.ProtocolError`；
* 提供通用的图片响应解析工具，供三个协议实现复用。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from ..models import (
    RELAY_PROTOCOLS,
    SELFHOST_PROTOCOLS,
    Channel,
    GeneratedImage,
    GenerateRequest,
    GenerateResult,
    ProtocolError,
    normalize_protocol,
)

# 默认超时时间（秒），与插件配置里的「绘画接口超时时间」保持一致
DEFAULT_TIMEOUT = 600.0
# 错误详情里保留的最大字符数
DETAIL_LIMIT = 500
# 服务端表示「该接口不存在」的常见状态码
ENDPOINT_MISSING_STATUS = (404, 405, 501)
# 触发「版本段回退」的状态码：自动补过版本段后收到 404，就用原始地址再试一次
VERSION_FALLBACK_STATUS = (404,)
# 版本段回退映射的容量上限（避免长跑进程里无限增长）
VERSION_FALLBACK_LIMIT = 64
# 「自动补版本段得到的 URL」-> 「用原始 base_url 拼出来的 URL」
_VERSION_FALLBACK_MAP: dict = {}

#: 自动补版本段时使用的默认版本前缀（中转站型协议）
DEFAULT_VERSION_SEGMENT = "v1"
#: 个别协议的首选版本段不同（Gemini 官方是 v1beta）
_PROTOCOL_VERSION_SEGMENTS = {"GEMINI": "v1beta"}
#: 末段是否是版本段（v1 / v1beta / v4 / v1alpha ...）
_VERSION_SEGMENT_RE = re.compile(r"^v\d+[a-z0-9]*$", re.IGNORECASE)
#: 路径里是否已包含版本段（例如 /v1/images/generations）
_VERSION_IN_PATH_RE = re.compile(r"(?:^|/)v\d+[a-z0-9]*(?:/|$)", re.IGNORECASE)
#: 相邻重复的版本段（/v1/v1、/v1beta/v1beta ...）
_DUP_VERSION_RE = re.compile(r"/(v\d+[a-z0-9]*)/\1(?=/|$)", re.IGNORECASE)


def _append_query(url: str, query: str) -> str:
    """把查询串合并回 URL（内部小工具）。"""
    if not query:
        return url
    return "{0}{1}{2}".format(url, "&" if "?" in url else "?", query)


def protocol_version_segment(protocol: str = "") -> str:
    """返回某协议自动补全时使用的版本段（`v1` / `v1beta` …）。"""
    key = (protocol or "").strip().upper()
    return _PROTOCOL_VERSION_SEGMENTS.get(key, DEFAULT_VERSION_SEGMENT)


def _split_base_query(base: str) -> tuple:
    """把地址拆成 `(主体, 查询串)`；主体去掉末尾多余斜杠。"""
    raw = (base or "").strip()
    if not raw:
        return "", ""
    main, _, query = raw.partition("?")
    return main.strip().rstrip("/"), query.strip()


def _path_part(main: str) -> str:
    """取出地址主体里的路径部分（去掉协议与域名）。"""
    text = (main or "").strip()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    if "/" in text:
        text = text.split("/", 1)[1]
    else:
        text = ""
    return text.strip("/")


def _needs_version_segment(main: str) -> bool:
    """判断地址主体是否需要在末尾补一个版本段。"""
    path = _path_part(main)
    if not path:
        # 只有域名，例如 https://api.example.com
        return True
    segments = [item for item in path.split("/") if item]
    if segments and _VERSION_SEGMENT_RE.match(segments[-1]):
        # 已经是 .../v1、.../v1beta
        return False
    if _VERSION_IN_PATH_RE.search("/" + path + "/"):
        # 路径里已经出现版本段，例如 /v1/images/generations
        return False
    return True


def _path_has_version(path: str) -> bool:
    """判断相对路径本身是否已经带了版本段（例如 @``@v1/models@``@）。"""
    text = (path or "").strip().lstrip("/")
    if not text:
        return False
    first = text.split("/", 1)[0]
    return bool(_VERSION_SEGMENT_RE.match(first))


def _collapse_duplicate_version(url: str) -> str:
    """折叠相邻的重复版本段（@``@.../v1/v1/...@``@ -> @``@.../v1/...@``@）。

    用户可能既在 base_url 写了 @``@/v1@``@，又在相对路径里写了 @``@v1/xxx@``@，
    这里做一次兜底收敛，避免发出 @``@/v1/v1/@``@ 这种必 404 的地址。
    """
    if not url:
        return url
    return _DUP_VERSION_RE.sub(r"/v\1", url)


def with_version_prefix(base_url: str, protocol: str = "", path: str = "") -> str:
    """按需给 @``@base_url@``@ 补上版本段，返回补全后的地址（需求 1）。

    用户的填写习惯各不相同：有人只写域名 @``@https://api.example.com@``@，有人写
    @``@https://api.example.com/v1@``@，也有人直接贴完整接口地址。本函数统一收口：

    * 只有**中转站型协议**（@``@RELAY_PROTOCOLS@``@）才补版本段；自部署型
      （sdwebui / comfyui）与未知协议保持原样，完全尊重用户填写；
    * 地址已是 @``@.../v1@``@、@``@.../v1beta@``@、@``@.../v2@``@ 这类形态时不重复补；
    * 地址已指向完整接口路径（如 @``@.../v1/images/generations@``@）时不补；
    * @``@path@``@ 自带版本段（如 @``@v1/models@``@）时也不补，避免出现 @``@/v1/v1@``@；
    * @``@base_url@``@ 自带查询串（@``@?key=x@``@）时保留并放在最后；
    * @``@path@``@ 已是完整 @``@http(s)://@``@ 链接时直接返回它。

    本函数只负责「该不该补、补在哪儿」，不追加 @``@path@``@；
    真正的拼接由 :meth:@``@BaseImageProtocol._url@``@ 完成（并会折叠重复版本段）。
    """
    raw_path = (path or "").strip()
    if raw_path.lower().startswith(("http://", "https://")):
        return raw_path
    raw_base = (base_url or "").strip()
    if not raw_base:
        return raw_path
    main, query = _split_base_query(raw_base)
    if not main:
        return raw_base
    key = normalize_protocol(protocol) or (protocol or "").strip().lower()
    if key in SELFHOST_PROTOCOLS or key not in RELAY_PROTOCOLS:
        return _append_query(main, query)
    if not _needs_version_segment(main):
        return _append_query(main, query)
    if _path_has_version(raw_path):
        # 版本段已经写在相对路径里了，不再重复补
        return _append_query(main, query)
    return _append_query("{0}/{1}".format(main, protocol_version_segment(key)), query)


def version_was_added(base_url: str, protocol: str = "") -> bool:
    """判断 :func:`with_version_prefix` 是否真的会补版本段（供 404 回退判断）。"""
    raw = (base_url or "").strip()
    if not raw:
        return False
    main, _ = _split_base_query(raw)
    if not main:
        return False
    key = normalize_protocol(protocol) or (protocol or "").strip().lower()
    if key in SELFHOST_PROTOCOLS or key not in RELAY_PROTOCOLS:
        return False
    return _needs_version_segment(main)


def _remember_version_fallback(mapping: dict, auto_url: str, raw_url: str) -> None:
    """记下「自动补版本段后的 URL」与「原始 base_url 拼出的 URL」的对应关系。

    ``mapping`` 由调用方（协议实例）持有，**按实例隔离**：
    不同通道即使用同一个域名，也不会互相干扰。
    只在自动补过版本段时调用；超过容量上限时丢弃最旧的记录。
    """
    if not isinstance(mapping, dict):
        return
    if not auto_url or not raw_url or auto_url == raw_url:
        return
    try:
        mapping[auto_url] = raw_url
        if len(mapping) > VERSION_FALLBACK_LIMIT:
            for key in list(mapping.keys())[: len(mapping) - VERSION_FALLBACK_LIMIT]:
                mapping.pop(key, None)
    except Exception:  # noqa: BLE001 - 记录失败不影响主流程
        return


def fallback_url_for(url: str, mapping: Optional[dict] = None) -> str:
    """若该 URL 是自动补版本段得到的，返回用原始 base_url 拼出的备选 URL；否则返回空串。

    ``mapping`` 省略时读取模块级登记表（供外部工具使用）；
    协议实例内部始终传入自己的实例级映射，避免跨通道串味。
    """
    table = mapping if isinstance(mapping, dict) else _VERSION_FALLBACK_MAP
    return table.get((url or "").strip(), "")


def _join_raw(base: str, path: str) -> str:
    """把 base 与 path 直接拼起来（不做版本段补全）。"""
    raw_path = (path or "").strip()
    if raw_path.lower().startswith(("http://", "https://")):
        return raw_path
    raw_base = (base or "").strip()
    if not raw_base:
        return raw_path
    main, _, base_query = raw_base.partition("?")
    main = main.rstrip("/")
    relative = raw_path.lstrip("/")
    url = "{0}/{1}".format(main, relative) if relative else main
    if base_query:
        if "?" in url:
            head, _, tail = url.partition("?")
            url = "{0}?{1}&{2}".format(head, tail, base_query)
        else:
            url = "{0}?{1}".format(url, base_query)
    return url

# 明确表示 base64 图片的字段名
_B64_KEYS = ("b64_json", "b64", "base64", "image_base64", "imageBase64")
# 可能表示图片链接的字段名
_URL_KEYS = ("url", "image_url", "image", "imageUrl", "link", "uri")
# 包装结构里常见的容器字段名
_CONTAINER_KEYS = (
    "data",
    "images",
    "image",
    "output",
    "outputs",
    "result",
    "response",
    "items",
    "artifacts",
)
# 明确表示图片的节点类型
_IMAGE_TYPES = (
    "image",
    "image_url",
    "output_image",
    "input_image",
    "image_generation",
    "b64_json",
)


# 轮询状态：仍在处理 / 已完成 / 已失败
POLL_PENDING = "pending"
POLL_DONE = "done"
POLL_FAILED = "failed"
# 轮询间隔（秒）：起步 1.5s，逐步放宽到 5s
POLL_INTERVAL_START = 1.5
POLL_INTERVAL_STEP = 2.5
POLL_INTERVAL_MAX = 5.0
# 尺寸默认值（各协议都可接受的安全值）
DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 1024


class PollOutcome:
    """一次轮询查询的结果（供 jimeng / tongyi / comfyui 等异步协议复用）。"""

    __slots__ = ("state", "images", "payload", "message")

    def __init__(
        self,
        state: str,
        images: Optional[list] = None,
        payload: Any = None,
        message: str = "",
    ) -> None:
        self.state = state or POLL_PENDING
        self.images: list = list(images or [])
        self.payload = payload
        self.message = message or ""

    @property
    def is_pending(self) -> bool:
        """任务是否仍在处理中。"""
        return self.state == POLL_PENDING

    @property
    def is_done(self) -> bool:
        """任务是否已成功完成。"""
        return self.state == POLL_DONE


def parse_size_pair(
    size: Any,
    default: tuple = (DEFAULT_WIDTH, DEFAULT_HEIGHT),
    multiple_of: int = 0,
) -> tuple:
    """把 ``1536x1024`` 拆成 ``(width, height)``。

    * ``auto`` / 空值 / 非法值 -> ``default``；
    * ``multiple_of`` 大于 1 时，宽高会向下取整到它的整数倍（SD / ComfyUI 常用 8）；
    * 结果至少为 ``multiple_of``（默认 64），避免出现 0 或负数。
    """
    width, height = default
    text = str(size or "").strip().lower().replace("*", "x").replace("×", "x")
    if text and text != "auto":
        left, sep, right = text.partition("x")
        if sep and left.strip().isdigit() and right.strip().isdigit():
            width = int(left.strip())
            height = int(right.strip())
    step = int(multiple_of) if multiple_of and multiple_of > 1 else 0
    if step:
        floor = max(step, 64)
        width = max(floor, (int(width) // step) * step)
        height = max(floor, (int(height) // step) * step)
    if width <= 0 or height <= 0:
        width, height = default
    return int(width), int(height)


def looks_like_auto(size: Any) -> bool:
    """判断尺寸是否为「自动」（不传尺寸，交给服务端决定）。"""
    text = str(size or "").strip().lower()
    return (not text) or text == "auto"


def truncate(text: Any, limit: int = DETAIL_LIMIT) -> str:
    """把任意文本裁剪到指定长度，用于错误详情，避免日志爆炸。"""
    raw = "" if text is None else str(text)
    raw = raw.strip()
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "..."


def _current_loop() -> asyncio.AbstractEventLoop:
    """获取当前事件循环，兼容 Python 3.8~3.12（含无运行中 loop 的场景）。"""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.get_event_loop()


def _channel_proxy(channel: Any) -> str:
    """从通道的 extra 中读取代理配置（由配置层写入）。"""
    try:
        extra = getattr(channel, "extra", None) or {}
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(extra, dict):
        return ""
    return str(extra.get("proxy") or "").strip()


def _proxy_kwargs(proxy: str) -> dict:
    """把代理地址转换成 ``aiohttp.ClientSession`` 的构造参数。"""
    value = (proxy or "").strip()
    if not value:
        return {}
    if "://" not in value:
        value = "http://" + value
    if value.split("://", 1)[0].lower() not in ("http", "https", "socks4", "socks5"):
        return {}
    return {"proxy": value}


def parse_json_text(text: str) -> Any:
    """尽力把响应文本解析成 JSON，失败返回 ``None``。

    兼容极少数中转站返回的 SSE 风格 ``data: {...}`` 文本。
    """
    if not text:
        return None
    candidate = text.strip()
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except Exception:  # noqa: BLE001 - 继续尝试其它格式
        pass
    if "data:" in candidate:
        parsed: Any = None
        for line in candidate.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                parsed = json.loads(chunk)
            except Exception:  # noqa: BLE001 - 跳过无法解析的行
                continue
        if parsed is not None:
            return parsed
    return None


def extract_error_message(payload: Any, fallback: str = "") -> str:
    """从错误响应里提取可读的报错文本。"""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            for key in ("message", "msg", "detail", "description", "code"):
                value = error.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        elif isinstance(error, str) and error.strip():
            return error.strip()
        for key in ("message", "msg", "detail", "error_description", "reason", "err_msg"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        response = payload.get("response")
        if isinstance(response, dict):
            nested = extract_error_message(response, "")
            if nested:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = extract_error_message(item, "")
            if nested:
                return nested
    return fallback


def sniff_image_mime(data: bytes, fallback: str = "") -> str:
    """根据文件头推断图片 MIME，识别不了时返回 ``fallback``。"""
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


def decode_b64_image(
    value: str,
    mime_hint: str = "",
    *,
    require_image_magic: bool = False,
) -> Optional[GeneratedImage]:
    """把 base64 字符串（含 ``data:`` 前缀）解码成 :class:`GeneratedImage`。

    ``require_image_magic=True`` 时，只有解码结果的文件头确实是图片才接受，
    用于 ``data`` 这类含义不明确的字段，避免把普通文本误判成图片。
    """
    payload = (value or "").strip()
    if not payload:
        return None
    mime = (mime_hint or "").strip() or "image/png"
    if payload.startswith("data:"):
        header, _, payload = payload.partition(",")
        meta = header[5:]
        detected = meta.split(";")[0].strip() if meta else ""
        if detected:
            mime = detected
    compact = "".join(payload.split())
    if not compact:
        return None
    try:
        raw = base64.b64decode(compact + "=" * ((-len(compact)) % 4))
    except Exception:  # noqa: BLE001 - 非法 base64 直接跳过
        return None
    if not raw:
        return None
    detected_mime = sniff_image_mime(raw, "")
    if require_image_magic and not detected_mime:
        return None
    return GeneratedImage(data=raw, mime=detected_mime or mime)


def looks_like_image_node(node: Any) -> bool:
    """判断一个字典节点是否直接携带图片数据。"""
    if not isinstance(node, dict):
        return False
    for key in _B64_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return True
    for key in _URL_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip().startswith(("http://", "https://", "data:")):
            return True
        if isinstance(value, dict):
            inner = value.get("url")
            if isinstance(inner, str) and inner.strip():
                return True
    data_value = node.get("data")
    if isinstance(data_value, str) and data_value.strip():
        node_type = str(node.get("type") or "").lower()
        if node_type in _IMAGE_TYPES or not node_type:
            return True
    return False


def iter_image_nodes(payload: Any, _depth: int = 0) -> list[dict]:
    """从各种响应包装（``data[]`` / ``images[]`` / ``output[]`` / ``result.data[]`` 等）里收集图片节点。"""
    if _depth > 5 or payload is None:
        return []
    if isinstance(payload, list):
        nodes: list[dict] = []
        for item in payload:
            nodes.extend(iter_image_nodes(item, _depth + 1))
        return nodes
    if not isinstance(payload, dict):
        return []
    if looks_like_image_node(payload):
        return [payload]
    nodes = []
    for key in _CONTAINER_KEYS:
        if key in payload:
            nodes.extend(iter_image_nodes(payload[key], _depth + 1))
    if not nodes:
        # 兜底：遇到未知包装结构时轻微遍历一层，提升兼容性
        for value in payload.values():
            if isinstance(value, (list, dict)):
                nodes.extend(iter_image_nodes(value, _depth + 1))
    return nodes


def build_image_from_node(node: Any) -> Optional[GeneratedImage]:
    """把单个图片节点转换成 :class:`GeneratedImage`，无法识别时返回 ``None``。"""
    if not isinstance(node, dict):
        return None
    hint = node.get("mime_type") or node.get("mimeType") or node.get("content_type") or ""
    if not isinstance(hint, str):
        hint = ""
    revised = node.get("revised_prompt") or node.get("caption") or ""
    revised_text = revised.strip() if isinstance(revised, str) else ""
    node_type = str(node.get("type") or "").lower()

    for key in _B64_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            image = decode_b64_image(value, hint)
            if image is not None:
                image.revised_prompt = revised_text
                return image

    for key in _URL_KEYS:
        value = node.get(key)
        if isinstance(value, dict):
            value = value.get("url") or value.get("data")
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text.startswith("data:"):
                image = decode_b64_image(text, hint)
            elif text.startswith(("http://", "https://")):
                image = GeneratedImage(url=text, mime=hint or "image/png")
            else:
                # 少数中转站会把纯 base64 直接放进 image 字段
                image = decode_b64_image(text, hint, require_image_magic=True)
            if image is not None and not image.is_empty():
                image.revised_prompt = revised_text
                return image

    data_value = node.get("data")
    if isinstance(data_value, str) and data_value.strip():
        if node_type in _IMAGE_TYPES or not node_type:
            image = decode_b64_image(data_value, hint, require_image_magic=True)
            if image is not None:
                image.revised_prompt = revised_text
                return image
    return None


def summarize_payload(payload: Any) -> dict:
    """生成不含大段 base64 的响应摘要，便于写入 ``GenerateResult.raw`` 与日志。"""
    if isinstance(payload, dict):
        return {str(key): _summarize_value(value, 1) for key, value in payload.items()}
    return {"payload": _summarize_value(payload, 1)}


def _summarize_value(value: Any, depth: int) -> Any:
    if isinstance(value, str):
        if len(value) > 160:
            return "<str {} chars>".format(len(value))
        return value
    if isinstance(value, dict):
        if depth >= 4:
            return "<dict>"
        return {str(key): _summarize_value(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        if depth >= 4:
            return "<list>"
        return [_summarize_value(item, depth + 1) for item in value]
    return value


class HttpResult:
    """一次 HTTP 请求的结果（响应体已完整读取）。"""

    __slots__ = ("status", "text", "data", "headers", "elapsed")

    def __init__(
        self,
        status: int,
        text: str,
        data: Any = None,
        headers: Optional[dict[str, str]] = None,
        elapsed: float = 0.0,
    ) -> None:
        self.status = int(status)
        self.text = text or ""
        self.data = data
        self.headers: dict[str, str] = headers or {}
        self.elapsed = float(elapsed or 0.0)

    @property
    def ok(self) -> bool:
        """HTTP 状态码是否为 2xx。"""
        return 200 <= self.status < 300

    @property
    def endpoint_missing(self) -> bool:
        """服务端是否表示该接口不存在（用于自动回退）。"""
        return self.status in ENDPOINT_MISSING_STATUS

    def error_message(self) -> str:
        """优先取服务端报错信息，没有则退回响应文本摘要。"""
        return extract_error_message(self.data, "") or truncate(self.text, 200)

    def ensure_ok(self, action: str = "请求") -> "HttpResult":
        """非 2xx 时抛出带状态码与响应详情的 :class:`ProtocolError`。"""
        if self.ok:
            return self
        detail_message = self.error_message()
        message = "{}失败（HTTP {}）".format(action, self.status)
        if detail_message:
            message = "{}：{}".format(message, truncate(detail_message, 160))
        raise ProtocolError(message, status=self.status, detail=truncate(self.text, DETAIL_LIMIT))


class BaseImageProtocol(ABC):
    """图像生成 / 编辑协议基类。"""

    protocol_key: str = ""

    def __init__(
        self,
        channel: Channel,
        timeout: float = DEFAULT_TIMEOUT,
        proxy: str = "",
    ) -> None:
        self.channel = channel
        try:
            value = float(timeout)
        except (TypeError, ValueError):
            value = DEFAULT_TIMEOUT
        if value <= 0:
            value = DEFAULT_TIMEOUT
        self.timeout = value
        # HTTP 代理：优先用显式传入的值，其次读 channel.extra["proxy"]
        self.proxy = (proxy or "").strip() or _channel_proxy(channel)
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None
        # 「自动补版本段后的 URL」-> 「原始 base_url 拼出的 URL」，仅本实例有效
        self._version_fallbacks: dict = {}

    # ------------------------------------------------------------------ 抽象接口
    @abstractmethod
    async def generate(self, req: GenerateRequest) -> GenerateResult:
        """执行一次文生图或图片编辑请求。"""

    @abstractmethod
    async def list_models(self) -> list[str]:
        """获取该通道可用的模型名列表。"""

    # ------------------------------------------------------------------ 通用能力
    async def test(self) -> tuple[bool, str]:
        """默认连通性测试：拉取模型列表，子类可覆盖。"""
        return await self._check_models(self.list_models)

    async def close(self) -> None:
        """释放底层连接（插件卸载 / 重载时调用）。"""
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()

    async def _check_models(self, fetcher: Callable[[], Awaitable[list[str]]]) -> tuple[bool, str]:
        """统一的模型列表自检逻辑，供 ``test()`` 复用。"""
        try:
            models = await fetcher()
        except ProtocolError as exc:
            if exc.status in ENDPOINT_MISSING_STATUS:
                return (
                    True,
                    "接口可达，但站点未提供模型列表接口（HTTP {}），请确认接口链接（base_url）是否正确".format(
                        exc.status
                    ),
                )
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - 任何异常都转成可读文本
            return False, "连接失败：{}".format(exc)
        if models:
            return True, "连接成功，共获取到 {} 个模型".format(len(models))
        return True, "连接成功（该接口未返回模型列表）"

    def _url(self, path: str) -> str:
        """把相对路径拼接到 ``channel.base_url`` 上（必要时自动补版本段）。

        用户只填域名时（如 ``https://api.example.com``），这里会自动补成
        ``.../v1/xxx``；已带版本段或已是完整接口路径时不重复追加。
        自动补全的地址会登记到「版本段回退表」，供 :meth:``_request`` 在收到 404 时
        改用**原始 base_url** 重试一次（需求 1 的兜底）。
        """
        base = (self.channel.base_url or "").strip()
        if not base:
            raise ProtocolError("未配置接口链接（base_url），请在插件设置里填写中转站地址")
        url = self._join_url(base, path, self.channel.protocol)
        if version_was_added(base, self.channel.protocol):
            # 只在「确实自动补过版本段」时登记，供 404 回退使用
            _remember_version_fallback(self._version_fallbacks, url, _join_raw(base, path))
        return url

    def resolved_base_url(self) -> str:
        """返回「按需补过版本段」之后的 base_url（供自检 / 状态展示使用）。"""
        return with_version_prefix(self.channel.base_url, self.channel.protocol)

    def raw_base_url(self) -> str:
        """返回用户原样填写的 base_url（不含自动补的版本段）。"""
        return (self.channel.base_url or "").strip()

    @staticmethod
    def _join_url(base: str, path: str, protocol: str = "") -> str:
        """健壮地拼接 base 与 path（需求 1：可自动补版本段）。

        * ``path`` 已是完整 http(s) 链接时原样返回；
        * 去掉 base 末尾的 ``/`` 与 path 开头的 ``/``，避免出现重复斜杠；
        * 保留 base 自带的查询串（例如 ``?key=xxx``），并与 path 查询串合并；
        * 传入 ``protocol`` 时先按 :func:``with_version_prefix`` 补版本段
          （只有中转站型协议会补；自部署型 sdwebui / comfyui 保持原样）。
        """
        # 必须把 path 一起传进去：path 自带版本段（如 v1/models）时不能再补，
        # 否则会拼出 /v1/v1/models 这种 404 地址。
        return _join_raw(with_version_prefix(base, protocol, path), path)

    def _headers(self) -> dict[str, str]:
        """默认请求头：Bearer 鉴权（OpenAI 兼容风格）。"""
        headers = {"Accept": "application/json"}
        key = (self.channel.api_key or "").strip()
        if key:
            headers["Authorization"] = "Bearer {}".format(key)
        return headers

    # ------------------------------------------------------------------ 请求工具
    async def _ensure_session(self) -> aiohttp.ClientSession:
        """懒创建并复用 ``aiohttp.ClientSession``。"""
        if self._session is not None and not self._session.closed:
            return self._session
        loop = _current_loop()
        if self._session_lock is None or self._lock_loop is not loop:
            # 在事件循环内创建，避免 Python 3.8 把锁绑定到已关闭的旧 loop 上
            self._session_lock = asyncio.Lock()
            self._lock_loop = loop
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    headers={"User-Agent": "AstrBot-Plugin-gpt-image/1.0.8"},
                    **_proxy_kwargs(self.proxy),
                )
        return self._session

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        json_body: Any = None,
        data: Any = None,
        data_factory: Optional[Callable[[], Any]] = None,
        timeout: Optional[float] = None,
    ) -> HttpResult:
        """发送一次 HTTP 请求；失败时按需做「版本段回退」。

        需求 1 兜底：若 url 是「自动补过版本段」拼出来的，而服务端返回 404，
        则用**原始 base_url** 拼出的地址再试一次；两次都失败时返回第一次的结果，
        保证报错信息仍指向真实问题（提示用户地址可能填得不合适）。

        ``data_factory``：可重复调用的请求体构造函数。multipart 体（aiohttp.FormData）
        只能被消费一次，回退重试时必须在工厂里新建一份，所以编辑类请求一律用工厂传参。
        """
        result = await self._request_once(
            method,
            url,
            headers=headers,
            params=params,
            json_body=json_body,
            data=data,
            data_factory=data_factory,
            timeout=timeout,
        )
        if result.status not in VERSION_FALLBACK_STATUS:
            return result
        fallback = fallback_url_for(url, self._version_fallbacks)
        if not fallback:
            return result
        retry = await self._request_once(
            method,
            fallback,
            headers=headers,
            params=params,
            json_body=json_body,
            data=data,
            data_factory=data_factory,
            timeout=timeout,
        )
        # 回退成功就用回退结果；回退也失败则保留第一次的结果（更利于排查配置问题）
        return retry if retry.ok else result

    async def _request_once(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        json_body: Any = None,
        data: Any = None,
        data_factory: Optional[Callable[[], Any]] = None,
        timeout: Optional[float] = None,
    ) -> HttpResult:
        """真正发一次请求；网络与超时异常统一转成 :class:`ProtocolError`。"""
        session = await self._ensure_session()
        merged_headers = self._headers()
        if headers:
            merged_headers.update(headers)
        total = float(timeout) if timeout and timeout > 0 else self.timeout
        client_timeout = aiohttp.ClientTimeout(total=total)
        started = time.monotonic()
        # 优先使用工厂：保证每次重试都拿到全新的请求体（multipart 只能用一次）
        payload = data_factory() if callable(data_factory) else data
        try:
            async with session.request(
                method.upper(),
                url,
                headers=merged_headers,
                params=params,
                json=json_body,
                data=payload,
                timeout=client_timeout,
            ) as response:
                text = await response.text(errors="replace")
                return HttpResult(
                    status=response.status,
                    text=text,
                    data=parse_json_text(text),
                    headers={str(k).lower(): str(v) for k, v in response.headers.items()},
                    elapsed=time.monotonic() - started,
                )
        except asyncio.TimeoutError as exc:
            raise ProtocolError(
                "请求超时（{} 秒），可在插件设置里调大「绘画接口超时时间」".format(total),
                detail=truncate(str(exc)),
            ) from exc
        except aiohttp.ClientError as exc:
            raise ProtocolError(
                "请求失败：{}".format(exc.__class__.__name__),
                detail=truncate(str(exc)) or truncate(repr(exc)),
            ) from exc
        except OSError as exc:
            raise ProtocolError("网络连接失败：{}".format(exc), detail=truncate(str(exc))) from exc

    # ------------------------------------------------------------------ 请求参数
    @staticmethod
    def _require_prompt(req: GenerateRequest) -> str:
        """校验提示词非空。"""
        prompt = (req.prompt or "").strip()
        if not prompt:
            raise ProtocolError("提示词不能为空，请在指令后补充要生成或修改的内容")
        return prompt

    def _resolve_model(self, req: GenerateRequest, *, edit: bool = False) -> str:
        """决定本次请求使用的模型名。

        编辑请求时，若调用方只给了生成模型（或没给），优先使用通道配置的编辑模型。
        """
        requested = (req.model or "").strip()
        generate_model = self.channel.generate_model
        if not edit:
            return requested or generate_model
        if not requested or requested == generate_model:
            return self.channel.edit_model_name or generate_model
        return requested

    @staticmethod
    def _normalize_n(value: Any) -> int:
        """规范生成张数：至少 1 张，最多 10 张。"""
        try:
            count = int(value)
        except (TypeError, ValueError):
            return 1
        if count < 1:
            return 1
        return min(count, 10)

    @staticmethod
    def _normalize_size(size: Any) -> str:
        """规范尺寸字段；``auto`` 表示交给服务端决定。"""
        return str(size or "").strip()


    async def _poll_until_done(
        self,
        probe: Callable[[], Awaitable[PollOutcome]],
        *,
        timeout: Optional[float] = None,
        interval_start: float = POLL_INTERVAL_START,
        interval_step: float = POLL_INTERVAL_STEP,
        interval_max: float = POLL_INTERVAL_MAX,
        action: str = "任务",
    ) -> PollOutcome:
        """按「1.5s 起步、逐步放宽到 5s」的节奏轮询，直到完成或超时。

        * ``probe`` 是子类提供的「查询一次」协程，返回 :class:`PollOutcome`；
        * 总时长受 ``timeout``（默认取通道超时）控制，超时抛 :class:`ProtocolError`；
        * ``probe`` 自身抛出的 :class:`ProtocolError` 会被缓冲：偶发失败不立刻中断，
          但连续失败 3 次或超时仍未拿到结果时就抛出最后一次的错误（避免把网络抖动当致命错误）。
        """
        total = float(timeout) if timeout and timeout > 0 else self.timeout
        deadline = time.monotonic() + total
        interval = float(interval_start) if interval_start and interval_start > 0 else POLL_INTERVAL_START
        step = float(interval_step) if interval_step and interval_step > 0 else 0.0
        ceiling = float(interval_max) if interval_max and interval_max > 0 else interval
        last_error: Optional[ProtocolError] = None
        failures = 0
        while True:
            try:
                outcome = await probe()
            except ProtocolError as exc:
                last_error = exc
                failures += 1
                if failures >= 3:
                    raise
                outcome = None
            else:
                failures = 0
            if outcome is not None:
                if outcome.is_done:
                    return outcome
                if not outcome.is_pending:
                    raise ProtocolError(
                        outcome.message or "{}执行失败".format(action),
                        detail=truncate(str(outcome.payload), DETAIL_LIMIT),
                    )
            if time.monotonic() >= deadline:
                break
            # 睡眠时长不超过剩余时间，避免超时后还要多等一个间隔
            remain = deadline - time.monotonic()
            await asyncio.sleep(max(0.05, min(interval, remain)))
            if step:
                interval = min(ceiling, interval + step)
        if last_error is not None:
            raise last_error
        raise ProtocolError(
            "{}超时（已等待 {:.0f} 秒），可在插件设置里调大「绘画接口超时时间」".format(action, total)
        )

    @staticmethod
    def _pick_images(payload: Any, urls_first: bool = False) -> list:
        """从任意响应结构里提取图片，兼容 ``data[]`` / ``images[]`` / ``output[]`` 等包装。

        ``urls_first=True`` 时优先采用图片链接（异步任务的图片常是临时链接）。
        """
        images: list = []
        for node in iter_image_nodes(payload):
            image = build_image_from_node(node)
            if image is not None and not image.is_empty():
                images.append(image)
        if urls_first:
            images.sort(key=lambda item: 0 if item.url else 1)
        unique: list = []
        seen: set = set()
        for image in images:
            token = image.url or ""
            if not token and image.data:
                token = "{0}:{1}".format(len(image.data), image.data[:32])
            if token in seen:
                continue
            seen.add(token)
            unique.append(image)
        return unique

    def _build_result(
        self,
        images: list[GeneratedImage],
        *,
        model: str,
        payload: Any,
        status: int,
    ) -> GenerateResult:
        """组装统一的返回结果。"""
        raw = summarize_payload(payload)
        raw.setdefault("_status", status)
        raw.setdefault("_model", model)
        return GenerateResult(images=images, protocol=self.protocol_key, model=model, raw=raw)


__all__ = [
    "DEFAULT_TIMEOUT",
    "DETAIL_LIMIT",
    "BaseImageProtocol",
    "HttpResult",
    "build_image_from_node",
    "decode_b64_image",
    "extract_error_message",
    "iter_image_nodes",
    "looks_like_image_node",
    "POLL_DONE",
    "POLL_FAILED",
    "POLL_PENDING",
    "PollOutcome",
    "looks_like_auto",
    "parse_json_text",
    "parse_size_pair",
    "sniff_image_mime",
    "fallback_url_for",
    "protocol_version_segment",
    "summarize_payload",
    "truncate",
    "version_was_added",
    "with_version_prefix",
]
